# 飞书控制机器人部署指南

通过飞书「自建应用」机器人，在群里 @机器人 远程控制实盘策略（停止/重启）。

项目实现：`vnpy_domestic/trader/feishu_http_control.py`（HTTP 回调模式），跑在 `run_cta.py` 的**父进程**（常驻，非交易时段也能 @机器人）。

> 注意：本教程讲的是**自建应用机器人**（App ID / App Secret，收 @消息 + 回复）。通知用的「群机器人 webhook」（`open-apis/bot/v2/hook/xxx`）是另一套，两者独立，不混淆。

---

## 一、消息流转原理（长连接 vs Webhook）

无论哪种模式，你 @机器人 发的消息第一步都是先到飞书服务器，飞书识别是发给机器人后转发给你的程序。区别在转发方式：

| 模式 | 通道 | 公网要求 | 说明 |
|:----|:-----|:---------|:-----|
| **长连接**（WebSocket） | 程序主动连飞书，建好常驻通道 | 无需公网 IP | 消息直接走已建通道，不填地址 |
| **Webhook**（HTTP 回调） | 飞书按你填的地址主动发 HTTP 请求 | 需公网 IP 或内网穿透 | 每次消息临时发一次请求，发完即走 |

一句话总结：**长连接是提前占好一条专属通道，消息来了直接走通道；Webhook 是每次有消息，飞书按你给的地址上门送一次，送完就走。**

项目用的是 **Webhook（HTTP 回调）** 模式。

---

## 二、前置步骤：创建并配置应用机器人

1. 打开[飞书开发者后台](https://open.feishu.cn/app)，点击「创建企业自建应用」，填写应用名称、描述、图标，确认创建。
2. 进入刚创建的应用详情页，左侧「应用能力」>「添加应用能力」，找到「机器人」卡片点击添加，开启机器人能力。
3. 左侧「权限管理」，搜索并开通：**接收群聊中 @机器人 消息事件**（`im:message.group_at_msg:readonly`）。
4. 左侧「事件与回调」，订阅方式选「将事件发送至开发者服务器」（HTTP 回调），请求地址填 `<公网入口>/webhook/feishu`。同一页「加密策略」区域记下 `Encrypt Key` 和 `Verification Token`（两个字段挨在一起）。
5. 左侧「版本管理与发布」，创建版本并发布，等待管理员审核通过。
6. 记录 `App ID`、`App Secret`（应用详情页）、`Encrypt Key`、`Verification Token`（事件与回调页）。

---

## 三、凭证配置（secrets.yaml）

凭证不硬编码在代码里，从 `.vntrader/secrets.yaml` 读取（`load_feishu_control` 按当前目录 → 包目录 → 用户家目录依次搜索）：

```yaml
# ── 飞书控制（可选，群里 @机器人 停止/重启策略，HTTP 回调模式）──
feishu_app_id: "cli_xxx"                  # 自建应用 App ID
feishu_app_secret: "xxx"                  # App Secret
feishu_encrypt_key: ""                    # 事件与回调 → Encrypt Key（没开加密留空）
feishu_verification_token: "xxx"          # 事件与回调 → Verification Token（验签必填）
feishu_bot_open_id: "ou_xxx"              # 机器人 open_id（@识别）
feishu_host: "127.0.0.1"                  # 监听地址，Linux 服务器改 0.0.0.0
feishu_public_url: "https://你的域名"     # 本地 Windows 用 localtunnel 穿透后的公网地址
```

不配置 `feishu_app_id` 则整个飞书控制不启用，通知功能（群机器人 webhook）不受影响。

---

## 四、项目控制代码（feishu_http_control.py）

```python
"""飞书 HTTP 回调控制：群里 @机器人 停止/重启 实盘策略（HTTP 回调模式，可选功能）。

不配置 feishu_app_id 则不启用，通知功能（notification_manager 的群 webhook）不受影响。
"""
import json
import logging
import os
import warnings
from collections import deque
from pathlib import Path

import yaml

# 压制 lark_oapi 的 pkg_resources 弃用告警（setuptools>=81 触发，不影响功能）
warnings.filterwarnings("ignore", message=".*pkg_resources.*deprecated.*")

# 飞书 API 请求必须直连，不能走 Clash 代理（否则 reply 被 RemoteDisconnected 打断）
os.environ["NO_PROXY"] = os.environ.get("NO_PROXY", "") + ",open.feishu.cn,*.feishu.cn,127.0.0.1,localhost"
os.environ["no_proxy"] = os.environ.get("no_proxy", "") + ",open.feishu.cn,*.feishu.cn,127.0.0.1,localhost"

logger = logging.getLogger("feishu_control")


def load_feishu_control() -> dict:
    """从 secrets.yaml 读飞书控制凭证，缺 app_id 返回空 dict（= 不启用控制）"""
    secrets = {}
    for p in [Path.cwd() / ".vntrader" / "secrets.yaml",
              Path(__file__).resolve().parent.parent.parent / ".vntrader" / "secrets.yaml",
              Path.home() / ".vntrader" / "secrets.yaml"]:
        if p.exists():
            with open(p, encoding="utf-8") as f:
                secrets = yaml.safe_load(f) or {}
            break
    return {
        "app_id": secrets.get("feishu_app_id", ""),
        "app_secret": secrets.get("feishu_app_secret", ""),
        "encrypt_key": secrets.get("feishu_encrypt_key", ""),
        "verification_token": secrets.get("feishu_verification_token", ""),
        "bot_open_id": secrets.get("feishu_bot_open_id", ""),
        "host": secrets.get("feishu_host", "127.0.0.1"),
        "public_url": secrets.get("feishu_public_url", ""),
    }


def reply_text(app_id, app_secret, msg_id, text):
    """执行完成后回复飞书消息（run_cta 主循环调用）"""
    if not app_id or not msg_id:
        return
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        req = ReplyMessageRequest.builder().message_id(msg_id).request_body(
            ReplyMessageRequestBody.builder().msg_type("text")
            .content(json.dumps({"text": text})).build()
        ).build()
        client.im.v1.message.reply(req)
    except Exception as e:
        logger.error(f"飞书回复失败: {e}")


def build_control_app(ctrl_queue, app_id, app_secret, encrypt_key,
                      verification_token, bot_open_id):
    """构建 FastAPI app：回调验签解密 + 指令入队 + 回复"""
    import lark_oapi as lark
    from lark_oapi.core.model import RawRequest
    from lark_oapi.api.im.v1 import (
        P2ImMessageReceiveV1,
        ReplyMessageRequest, ReplyMessageRequestBody,
    )
    from fastapi import FastAPI, Request
    from fastapi.responses import Response

    def _reply(client, msg_id, text):
        req = ReplyMessageRequest.builder().message_id(msg_id).request_body(
            ReplyMessageRequestBody.builder().msg_type("text")
            .content(json.dumps({"text": text})).build()
        ).build()
        client.im.v1.message.reply(req)

    def _is_mentioned(mentions):
        for m in mentions or []:
            if m.id.open_id == bot_open_id:
                return True
        return False

    _seen_msg_ids = deque(maxlen=200)   # message_id 去重（飞书重试/重复推送只处理一次，替代 3 秒窗口）

    def on_message(data: P2ImMessageReceiveV1):
        msg = data.event.message
        if msg.message_id in _seen_msg_ids:
            logger.info(f"[飞书控制] 重复消息，跳过: {msg.message_id}")
            return
        _seen_msg_ids.append(msg.message_id)
        mentions = getattr(msg, "mentions", None) or []
        mention_ids = [m.id.open_id for m in mentions] if mentions else []
        logger.info(f"[飞书控制] 收到消息 type={msg.message_type} mentions={mention_ids} bot_open_id={bot_open_id}")
        if msg.message_type != "text" or not _is_mentioned(mentions):
            logger.info(f"[飞书控制] 忽略: type={msg.message_type} 被@={_is_mentioned(mentions)}")
            return
        content = json.loads(msg.content)
        text = content.get("text", "").strip()
        for m in mentions:
            text = text.replace(m.key, "").strip()
        logger.info(f"[飞书控制] 识别文本: {text!r}")

        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        cmd = text.lower()
        if "停止" in cmd:
            ctrl_queue.put(("stop", msg.message_id))
            _reply(client, msg.message_id, "收到停止指令，执行中...")
        elif "重启" in cmd:
            ctrl_queue.put(("restart", msg.message_id))
            _reply(client, msg.message_id, "收到重启指令，执行中...")
        else:
            _reply(client, msg.message_id, "收到信息")

    # 注意：on_message 必须先定义再 register（否则 NameError）
    event_handler = lark.EventDispatcherHandler.builder(encrypt_key, verification_token) \
        .register_p2_im_message_receive_v1(on_message) \
        .build()

    app = FastAPI()

    @app.post("/webhook/feishu")
    async def feishu_webhook(request: Request):
        # 这个版本的 lark-oapi 没有 fastapi adapter，手动构造 RawRequest
        body = await request.body()
        logger.info(f"[飞书控制] 收到HTTP请求 body={body[:300]!r}")
        raw_req = RawRequest()
        raw_req.uri = request.url.path
        raw_req.body = body
        raw_req.headers = dict(request.headers)
        raw_resp = event_handler.do(raw_req)   # 内部完成解密 + 验签 + URL 验证
        return Response(content=raw_resp.content,
                        status_code=raw_resp.status_code,
                        headers=dict(raw_resp.headers))

    return app


def start_control(app, host="127.0.0.1", port=3000):
    """阻塞运行 uvicorn（放独立线程；非主线程安全）"""
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    uvicorn.Server(config).run()
```

**关键逻辑：**

- `build_control_app` 构建 FastAPI app，回调入口 `/webhook/feishu`，内部走 `event_handler.do()` 完成解密 + 验签 + URL 验证（challenge）。
- `on_message` 只处理 @了 `bot_open_id` 的文本消息，识别「停止」/「重启」后把 `(cmd, msg_id)` 元组塞进 `ctrl_queue`，由 `run_cta.py` 主循环执行。
- `_seen_msg_ids`（`deque(maxlen=200)`）做 message_id 去重，防飞书重试/重复推送只处理一次。
- `reply_text` 用 `lark.Client` + `im.v1.message.reply` 回复，执行完成后由主循环调用（「已完成」是真实执行完才发）。

---

## 五、run_cta.py 调用方式

飞书控制跑在**父进程**，`run_cta.py` 里这样启动（交易时段判断之前就起来，保证非交易时段也能 @机器人）：

```python
# ── 飞书控制（放父进程，常驻，非交易时段也能用）──
ctrl_queue = queue.Queue()
feishu = load_feishu_control()
if feishu.get("app_id"):
    app = build_control_app(ctrl_queue, feishu["app_id"], feishu["app_secret"],
                            feishu["encrypt_key"], feishu["verification_token"],
                            feishu["bot_open_id"])
    threading.Thread(target=start_control,
                     args=(app, feishu["host"], 3000), daemon=True).start()
    print(f"飞书 HTTP 控制已启动（父进程），监听 {feishu['host']}:3000", flush=True)
    if feishu.get("public_url"):
        print(f"飞书后台「请求地址」填: {feishu['public_url']}/webhook/feishu", flush=True)
```

主循环每轮 `get_nowait()` 取指令执行：

- 「停止」：`manual_stop = True`（挡住交易时段自动启动），通过 `Pipe` 通知子进程正常退出（撤挂单 + 写持仓），30 秒超时 terminate 兜底，完成后 `reply_text(... "停止已完成")`。
- 「重启」：`manual_stop = False`，正常退出旧子进程后重新 spawn（全新 CTP 连接 + 策略初始化），完成后回复 `重启已完成 (PID xxx)`。

完整逻辑见 `run_cta.py` 的 `run_parent()`。

---

## 六、Windows 本地测试

### 1. 安装依赖

```powershell
pip install fastapi uvicorn lark-oapi pyyaml
```

### 2. 配置 secrets.yaml

在项目 `.vntrader/secrets.yaml` 填好飞书控制凭证（见第三节），并设置 CTP 环境变量（本地可临时填假值，飞书控制在父进程先起，不受 CTP 连接影响）：

```powershell
set CTP_USER=test
set CTP_PASSWORD=test
```

### 3. 内网穿透

新开一个 PowerShell 窗口：

```powershell
npx localtunnel --port 3000
```

拿到临时公网地址，填到 `feishu_public_url`，后台请求地址填 `<地址>/webhook/feishu`。

### 4. 启动

```powershell
python run_cta.py
```

看到 `飞书 HTTP 控制已启动（父进程），监听 127.0.0.1:3000` 即控制已起。

> localtunnel 免费版每次重跑地址都会变、易断线，只适合本地临时测试；正式环境用下面 Ubuntu + 固定公网 IP。

---

## 七、Ubuntu 服务器部署

### 1. 安装依赖

```bash
apt update -y
apt install python3 python3-pip -y
pip3 install fastapi uvicorn lark-oapi pyyaml
```

### 2. 配置

`.vntrader/secrets.yaml` 里 `feishu_host` 改 `0.0.0.0`，`feishu_public_url` 填 `http://你的公网IP`。

### 3. 开放防火墙

```bash
ufw allow 3000/tcp
```

### 4. 后台常驻启动

```bash
nohup python3 run_cta.py > run.log 2>&1 &
tail -f run.log
```

看到 `飞书 HTTP 控制已启动` 即成功。

### 5. 后台配置

请求地址填：`http://你的公网IP:3000/webhook/feishu`，保存发布。

---

## 八、通用验证步骤

把机器人加到目标群，@机器人 发「停止」/「重启」/任意文本，观察：

1. 机器人先回「收到停止指令，执行中...」（回调线程即时回）
2. 主循环执行完回「停止已完成」（真实执行完才回）
3. 终端日志打印 `[飞书控制] 收到消息 type=...` 和 `识别文本: ...`

能完整走完即全流程跑通。

---

## 九、常见坑

| 坑 | 现象 | 解决 |
|:---|:-----|:-----|
| `Verification Token` 空/填错 | 回调地址验证 500 `invalid verification_token`，challenge 不返回 | 后台「事件与回调」填 `Verification Token`，builder 第二参数传入（必填） |
| `on_message` 写在 register 之后 | `NameError: name 'on_message' is not defined` | 先 `def` 再 `.register_p2_im_message_receive_v1(...)` |
| reply 被 Clash 代理拦截 | 能收到消息但回复发不出，`RemoteDisconnected` | 模块顶部设 `NO_PROXY`（大小写都设，见代码头部） |
| localtunnel 地址变化 | 隔天 @机器人收不到消息 | 隧道重启地址就变，后台回调地址要同步改成新地址；正式环境用固定公网 IP |
| 后台还选着「长连接」 | HTTP 回调版本一直收不到消息 | 飞书压根没发 HTTP 推送，去后台把订阅方式切到「将事件发送至开发者服务器」 |
| 用「3 秒窗口」过滤旧消息 | localtunnel 延迟超 3 秒，消息时灵时不灵 | 用 `message_id` 去重（`deque(maxlen=200)`），不误杀延迟消息 |

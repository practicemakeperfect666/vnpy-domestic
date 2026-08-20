# 飞书控制机器人部署指南

通过飞书「自建应用」机器人，在群里 @机器人 远程控制实盘策略（停止/重启）。

> 注意：本教程讲的是**自建应用机器人**（App ID / App Secret，收 @消息 + 回复）。通知用的「群机器人 webhook」（`open-apis/bot/v2/hook/xxx`）是另一套，两者独立，不混淆。

---

## 一、消息流转原理（长连接 vs Webhook）

无论哪种模式，你 @机器人 发的消息第一步都是先到飞书服务器，飞书识别是发给机器人后转发给你的程序。区别在转发方式：

| 模式 | 通道 | 公网要求 | 说明 |
|:----|:-----|:---------|:-----|
| **长连接**（WebSocket） | 程序主动连飞书，建好常驻通道 | 无需公网 IP | 消息直接走已建通道，不填地址 |
| **Webhook**（HTTP 回调） | 飞书按你填的地址主动发 HTTP 请求 | 需公网 IP 或内网穿透 | 每次消息临时发一次请求，发完即走 |

一句话总结：**长连接是提前占好一条专属通道，消息来了直接走通道；Webhook 是每次有消息，飞书按你给的地址上门送一次，送完就走。**

本项目的 `feishu_http_control.py` 用的是 **Webhook（HTTP 回调）** 模式。

---

## 二、前置步骤：创建并配置应用机器人

1. 打开[飞书开发者后台](https://open.feishu.cn/app)，点击「创建企业自建应用」，填写应用名称、描述、图标，确认创建。
2. 进入刚创建的应用详情页，左侧「应用能力」>「添加应用能力」，找到「机器人」卡片点击添加，开启机器人能力。
3. 左侧「权限管理」，搜索并开通：**接收群聊中 @机器人 消息事件**（`im:message.group_at_msg:readonly`）。
4. 左侧「事件与回调」，配置订阅方式（选「将事件发送至开发者服务器」= HTTP 回调）和请求地址，配置规则见[飞书开放平台文档](https://open.feishu.cn/document/)。这一步先记下 `Encrypt Key` 和 `Verification Token`（在同一页「加密策略」区域，两个字段挨在一起）。
5. 左侧「版本管理与发布」，创建版本并发布，等待管理员审核通过。
6. 记录应用的 `App ID`、`App Secret`（应用详情页）、`Encrypt Key`、`Verification Token`（事件与回调页），后面代码要用。

---

## 三、第一部分：Windows 本地开发测试

### 1. 前置准备

- 本地已安装 Python 3.8+
- 已完成上述应用机器人创建，拿到 `App ID` / `App Secret` / `Encrypt Key` / `Verification Token`

### 2. 安装依赖

```powershell
pip install fastapi uvicorn lark-oapi
```

### 3. 编写代码

新建 `feishu_receiver.py`，写入以下代码，替换自己的应用凭证：

```python
from fastapi import FastAPI, Request
from fastapi.responses import Response
import lark_oapi as lark
from lark_oapi.core.model import RawRequest

app = FastAPI()

# 替换成自己的应用凭证（开发者后台获取）
APP_ID = "你的App ID"
APP_SECRET = "你的App Secret"
ENCRYPT_KEY = "你的Encrypt Key"               # 没开加密留空 ""
VERIFICATION_TOKEN = "你的Verification Token"  # 验签必填，否则回调验证 500

# 消息处理函数：必须先定义，再 register（写反会 NameError）
def handle_receive_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    print("===== 收到飞书消息 =====")
    print(f"发送人open_id: {data.event.sender.sender_id.open_id}")
    print(f"消息内容: {msg.content}")
    print(f"群聊ID: {msg.chat_id}")

event_handler = lark.EventDispatcherHandler.builder(ENCRYPT_KEY, VERIFICATION_TOKEN) \
    .register_p2_im_message_receive_v1(handle_receive_message) \
    .build()

@app.post("/webhook/feishu")
async def feishu_webhook(request: Request):
    # 当前 lark-oapi 版本无 fastapi adapter，手动构造 RawRequest
    raw_req = RawRequest()
    raw_req.uri = request.url.path
    raw_req.body = await request.body()
    raw_req.headers = dict(request.headers)
    raw_resp = event_handler.do(raw_req)  # 内部完成解密 + 验签 + URL 验证(challenge)
    return Response(content=raw_resp.content, status_code=raw_resp.status_code,
                    headers=dict(raw_resp.headers))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000)
```

### 4. 启动本地服务

在 PowerShell 进入代码目录，执行：

```powershell
python feishu_receiver.py
```

看到 `Uvicorn running on http://0.0.0.0:3000` 即启动成功。

### 5. 内网穿透配置

新开一个 PowerShell 窗口，执行：

```powershell
npx localtunnel --port 3000
```

拿到输出的临时公网地址，拼上 `/webhook/feishu` 填入开发者后台的请求地址即可测试。

> localtunnel 免费版每次重跑地址都会变、易断线，只适合本地临时测试；正式环境用下面 Ubuntu + 固定公网 IP。

---

## 四、第二部分：Ubuntu 服务器部署

### 1. 前置准备

- 服务器具备固定公网 IP，已开放 SSH 登录
- Ubuntu 20.04+
- 已完成应用机器人创建，确认权限、事件配置完成

### 2. 安装系统依赖

```bash
# 更新系统源
apt update -y

# 安装 Python3 和 pip
apt install python3 python3-pip -y

# 安装 Python 库
pip3 install fastapi uvicorn lark-oapi
```

### 3. 编写代码

```bash
mkdir -p /opt/feishu_bot && cd /opt/feishu_bot
vim feishu_receiver.py
```

把上面 Windows 部分**完全相同的代码**复制进去，替换好应用凭证，`ESC` 输入 `:wq` 保存退出。

### 4. 开放防火墙端口

```bash
ufw allow 3000/tcp
```

### 5. 后台常驻启动

```bash
# 后台启动，日志输出到 feishu_run.log
nohup python3 feishu_receiver.py > feishu_run.log 2>&1 &

# 查看启动日志，确认服务正常
tail -f feishu_run.log
```

看到 `Uvicorn running on http://0.0.0.0:3000` 即启动成功，`Ctrl+C` 退出日志查看。

### 6. 后台配置

在应用配置页[飞书开放平台](https://open.feishu.cn/app)中，请求地址填写：

```
http://你的Ubuntu服务器公网IP:3000/webhook/feishu
```

保存后发布应用。

---

## 五、通用验证步骤

把机器人添加到目标群聊，@机器人 发送任意消息，查看对应终端日志，能打印出消息内容即全流程跑通。

---

## 六、常见坑

| 坑 | 现象 | 解决 |
|:---|:-----|:-----|
| `Verification Token` 空/填错 | 回调地址验证 500 `invalid verification_token`，challenge 不返回 | 后台「事件与回调」页填 `Verification Token`，代码 builder 第二参数传入（必填） |
| 处理函数写在 register 之后 | `NameError: name 'handle_receive_message' is not defined` | 先 `def` 再 `.register_p2_im_message_receive_v1(...)` |
| reply 被 Clash 代理拦截 | 能收到消息但回复发不出，`RemoteDisconnected` | 进程启动设 `NO_PROXY=open.feishu.cn,*.feishu.cn,127.0.0.1,localhost`（大小写都设） |
| localtunnel 地址变化 | 隔天 @机器人收不到消息 | 隧道重启地址就变，后台回调地址要同步改成新地址；正式环境用固定公网 IP |
| 后台还选着「长连接」 | HTTP 回调版本一直收不到消息 | 飞书压根没发 HTTP 推送，去后台把订阅方式切到「将事件发送至开发者服务器」 |

---

## 七、与项目实盘控制的关系

本项目实际的飞书控制实现是 `vnpy_domestic/trader/feishu_http_control.py`，在上述最小示例基础上多了：@识别（`bot_open_id`）、停止/重启指令入队、`message_id` 去重（防飞书重试重复触发）、回复（reply_text）、跑在父进程（常驻，非交易时段也能 @机器人）。最小示例用于验证链路，控制逻辑直接复用该模块即可。

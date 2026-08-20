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

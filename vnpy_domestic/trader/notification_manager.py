# notification_manager.py
import base64
import hashlib
import hmac
import platform
import queue
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import psutil
import requests
import yaml
from vnpy.trader.constant import Direction, Status


class NotificationManager:
    """
    统一通知管理器
    支持钉钉和飞书，可自由切换
    """

    # ============================================================
    # 配置区域 - 请在此修改你的通知配置
    # 推荐：在 .vntrader/secrets.yaml 中填写，启动时自动加载
    # ============================================================

    # 通知类型: "dingtalk", "feishu", "both"
    NOTIFY_TYPE = "both"

    # 钉钉配置（默认占位值，会被 secrets.yaml 覆盖）
    DINGTALK_WEBHOOK = ""
    DINGTALK_SECRET = ""

    # 飞书配置（默认占位值，会被 secrets.yaml 覆盖）
    FEISHU_WEBHOOK = ""

    # ============================================================

    def __init__(
        self,
        notify_type: str | None = None,
        dingtalk_webhook: str | None = None,
        dingtalk_secret: str | None = None,
        feishu_webhook: str | None = None,
        config_path: Path | None = None,
    ):
        """
        初始化通知管理器

        Parameters
        ----------
        notify_type : 通知类型 ("dingtalk", "feishu", "both")，不指定则从 secrets.yaml 或类配置读取
        dingtalk_webhook : 钉钉 webhook 地址（显式传入 > secrets.yaml > 类配置）
        dingtalk_secret : 钉钉签名密钥
        feishu_webhook : 飞书 webhook 地址
        config_path : secrets.yaml 路径，不指定则自动查找
        """
        # 从 secrets.yaml 加载（如果有的话）
        secrets = {}
        if config_path:
            yaml_paths = [config_path]
        else:
            yaml_paths = [
                Path.cwd() / ".vntrader" / "secrets.yaml",
                Path(__file__).resolve().parent.parent.parent
                / ".vntrader"
                / "secrets.yaml",
                Path.home() / ".vntrader" / "secrets.yaml",
            ]
        for p in yaml_paths:
            if p.exists():
                try:
                    with open(p, encoding="utf-8") as f:
                        secrets = yaml.safe_load(f) or {}
                except Exception:
                    pass
                break

        self.notify_type = (
            notify_type or secrets.get("notify_type", "") or self.NOTIFY_TYPE
        )
        self.dingtalk_webhook = (
            dingtalk_webhook
            or secrets.get("dingtalk_webhook", "")
            or self.DINGTALK_WEBHOOK
        )
        self.dingtalk_secret = (
            dingtalk_secret
            or secrets.get("dingtalk_secret", "")
            or self.DINGTALK_SECRET
        )
        self.feishu_webhook = (
            feishu_webhook or secrets.get("feishu_webhook", "") or self.FEISHU_WEBHOOK
        )

        self.log_callback = None

        # ── 异步发送：交易事件线程只入队，HTTP 在后台线程做 ──
        # 队列有界：webhook 挂了/被限流时丢最旧的，绝不反压交易线程。
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._closed = threading.Event()
        self._worker_local = threading.local()
        self._thread = threading.Thread(
            target=self._worker, name="notify-worker", daemon=True
        )
        self._thread.start()

    def set_log_callback(self, callback):
        """设置日志回调函数"""
        self.log_callback = callback

    def _log(self, message: str):
        """输出日志"""
        if self.log_callback:
            self.log_callback(message)
        else:
            print(f"[NotificationManager] {message}")

    # ============================================================
    # 钉钉相关方法
    # ============================================================
    def _get_dingtalk_sign(self) -> tuple[str, str]:
        """获取钉钉签名"""
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.dingtalk_secret.encode("utf-8")
        string_to_sign = f"{timestamp}\n{self.dingtalk_secret}"
        hmac_code = hmac.new(
            secret_enc, string_to_sign.encode("utf-8"), hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return timestamp, sign

    def _send_dingtalk_text(self, text: str) -> bool:
        """发送钉钉文本消息"""
        if not self.dingtalk_webhook:
            self._log("钉钉 webhook 未配置")
            return False

        try:
            timestamp, sign = self._get_dingtalk_sign()
            url = f"{self.dingtalk_webhook}&timestamp={timestamp}&sign={sign}"
            data = {"msgtype": "text", "text": {"content": text}}
            resp = requests.post(url, json=data, timeout=5)
            if resp.status_code == 200 and resp.json().get("errcode") == 0:
                self._log("钉钉消息发送成功")
                return True
            else:
                self._log(f"钉钉发送失败：{resp.text}")
                return False
        except Exception as e:
            self._log(f"钉钉发送异常：{e}")
            return False

    # ============================================================
    # 飞书相关方法
    # ============================================================
    def _send_feishu_text(self, text: str) -> bool:
        """发送飞书文本消息"""
        if not self.feishu_webhook:
            self._log("飞书 webhook 未配置")
            return False

        try:
            data = {"msg_type": "text", "content": {"text": text}}

            resp = requests.post(self.feishu_webhook, json=data, timeout=5)
            result = resp.json()

            if result.get("code") == 0:
                self._log("飞书消息发送成功")
                return True
            else:
                self._log(f"飞书发送失败：{result}")
                return False
        except Exception as e:
            self._log(f"飞书发送异常：{e}")
            return False

    # ============================================================
    # 统一发送接口
    # ============================================================
    def send_text(self, text: str, force_type: str | None = None) -> bool:
        """把消息放入发送队列（**不阻塞调用方**）。

        HTTP 在后台线程 `_worker` 里做，交易事件线程只做一次入队。返回值表示
        "是否成功入队"，不代表已送达；队列满（webhook 挂了/被限流）时丢弃最旧的
        一条并返回 False，绝不反压交易线程。
        """
        if self._closed.is_set():
            return False
        try:
            self._queue.put_nowait((text, force_type))
            return True
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._log("通知队列已满，丢弃最旧的一条")
            return False

    def _send_now(self, text: str, force_type: str | None = None) -> bool:
        """同步发送（原 send_text 实现；只在 worker 线程或测试里调用）。"""
        notify_type = force_type or self.notify_type

        if notify_type == "dingtalk":
            return self._send_dingtalk_text(text)
        elif notify_type == "feishu":
            return self._send_feishu_text(text)
        elif notify_type == "both":
            ok1 = self._send_dingtalk_text(text)
            ok2 = self._send_feishu_text(text)
            self._log(f"钉钉: {'✓' if ok1 else '✗'}, 飞书: {'✓' if ok2 else '✗'}")
            return ok1 or ok2
        else:
            self._log(f"未知的通知类型: {notify_type}")
            return False

    def _worker(self) -> None:
        """后台发送线程：串行取队列，单条异常不影响后续。"""
        self._worker_local.active = True
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._closed.is_set():
                    break
                continue
            if item is None:
                break
            text, force_type = item
            try:
                self._send_now(text, force_type)
            except Exception as exc:  # 兜底：worker 线程绝不能死
                self._log(f"通知发送异常：{exc}")

    def is_worker_thread(self) -> bool:
        """当前线程是不是通知 worker（引擎 write_log 用它挡住"发送失败回环"）。"""
        return bool(getattr(self._worker_local, "active", False))

    def close(self, timeout: float = 5.0) -> None:
        """退出前把队列里的消息尽量发完（最多等 timeout 秒）。"""
        if self._closed.is_set():
            return
        deadline = time.time() + timeout
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.05)
        self._closed.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=1.0)

    # ============================================================
    # 辅助方法：格式化各类消息
    # ============================================================
    def send_test_message(self) -> bool:
        """发送测试消息，用于验证配置是否正确"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = f"""📢 测试消息

这是一条测试消息，用于验证通知配置是否正确。

⏰ 时间：{now}
📱 钉钉：{"✅ 已配置" if self.dingtalk_webhook else "❌ 未配置"}
📱 飞书：{"✅ 已配置" if self.feishu_webhook else "❌ 未配置"}
🔄 通知类型：{self.notify_type}

✅ 配置验证中，请确认能收到此消息。"""
        return self.send_text(text)

    def send_status_summary(
        self, messages: list, total: int, idx: int, seq: int = 0
    ) -> bool:
        """发送策略状态汇总"""
        suffix = f" [{idx}/{total}]" if total > 1 else ""
        seq_tag = f" {seq}" if seq else ""
        header = f"📊 策略状态 ({datetime.now().strftime('%H:%M')}){seq_tag}{suffix}"
        bar = "─" * 30
        body = "\n\n".join(messages)
        text = f"{header}\n{bar}\n{body}\n{bar}"
        return self.send_text(text)

    @staticmethod
    def format_position_text(positions) -> str:
        """格式化持仓信息为纯文本"""
        if not positions:
            return "  无持仓"

        real_positions = [p for p in positions if p.volume > 0]
        if not real_positions:
            return "  无持仓"

        lines = ["  合约           方向  手数  成本价     浮动盈亏"]
        lines.append("  " + "-" * 50)

        for p in real_positions:
            dir_str = "多" if p.direction == Direction.LONG else "空"
            line = (
                f"  {p.vt_symbol:<14} {dir_str:<4} {p.volume:>4}  "
                f"{p.price:>8.2f}  {p.pnl:>10.2f}"
            )
            lines.append(line)

        return "\n".join(lines)

    def send_account_report(self, main_engine, order_stats: dict = None) -> None:
        """发送账户资金 + 持仓 + 系统状态报告"""
        try:
            accounts = main_engine.get_all_accounts()
            if accounts:
                acc = accounts[0]
                balance = acc.balance
                available = acc.available
            else:
                balance = available = 0.0

            positions = main_engine.get_all_positions()
            pos_text = self.format_position_text(positions)

            # ── 订单状态统计 ──
            order_lines = ""
            if order_stats:
                STATUS_KEYS = [
                    "提交中",
                    "未成交",
                    "部分成交",
                    "全部成交",
                    "已撤销",
                    "拒单",
                ]
                parts = []
                for label in STATUS_KEYS:
                    cnt = order_stats.get(label, 0)
                    if cnt:
                        parts.append(f"{label}:{cnt}")
                if parts:
                    total = order_stats.get("总计", 0)
                    order_lines = f"  当日订单: {'  '.join(parts)}  |  共{total}笔\n"

            try:
                cpu = f"{psutil.cpu_percent(interval=1)}%"
                mem = f"{psutil.virtual_memory().percent}%"
                disk = f"{psutil.disk_usage('/').percent}%"
                boot = datetime.fromtimestamp(psutil.boot_time())
                uptime = str(datetime.now() - boot).split(".")[0]
            except Exception:
                cpu = mem = disk = uptime = "N/A"

            # ── 挂单信息 ──
            active_orders = main_engine.get_all_active_orders()
            pending_lines = ""
            if active_orders:
                DIR_MAP = {Direction.LONG: "多", Direction.SHORT: "空"}
                STATUS_MAP = {
                    Status.SUBMITTING: "提交中",
                    Status.NOTTRADED: "未成交",
                    Status.PARTTRADED: "部成",
                }
                lines = []
                for o in active_orders:
                    strat_name = (
                        o.reference.replace("CtaStrategy_", "") if o.reference else ""
                    )
                    dir_label = DIR_MAP.get(o.direction, "?")
                    status_label = STATUS_MAP.get(o.status, str(o.status))
                    status_code = o.status.name  # SUBMITTING / NOTTRADED / PARTTRADED
                    lines.append(
                        f"    {strat_name} {o.symbol} {dir_label} @{o.price} "
                        f"{o.traded}/{o.volume}手 {status_label}({status_code})"
                    )
                pending_lines = "  挂单:\n" + "\n".join(lines) + "\n\n"

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sep = "─" * 40
            text = (
                f"📊 账户报告 ({now_str})\n"
                f"{sep}\n"
                f"  动态权益: {balance:>12.2f}\n"
                f"  可用资金: {available:>12.2f}\n"
                f"{order_lines}"
                f"{pending_lines}"
                f"  持仓:\n{pos_text}\n"
                f"\n"
                f"  运行: {uptime}  |  CPU: {cpu}\n"
                f"  内存: {mem}  |  磁盘: {disk}"
            )
            self.send_text(text)
        except Exception as e:
            self._log(f"账户报告生成异常: {e}")

    def send_rollover_init_summary(self, results: dict) -> None:
        """发送初始化换月检查汇总通知"""
        if not results:
            return

        rolled = [n for n, r in results.items() if r == "rolled"]
        pending = [n for n, r in results.items() if r in ("pending", "blocked")]
        no_change = [n for n, r in results.items() if r == "no_change"]
        failed = [n for n, r in results.items() if r == "failed"]

        lines = []
        if rolled:
            lines.append("🔄 已换月（空仓）：" + "、".join(rolled))
        if pending:
            lines.append("⏳ 待盘中移仓（有持仓）：" + "、".join(pending))
        if no_change:
            lines.append("✅ 无需换月：" + "、".join(no_change))
        if failed:
            lines.append("❌ 检查失败：" + "、".join(failed))

        if lines:
            self.send_text("📋 启动换月检查\n" + "\n".join(lines))

    # ============================================================
    # 系统信息与通知
    # ============================================================

    def send_startup_notification(self) -> bool:
        """发送启动通知"""
        message = f"""
🚀 **CTA策略系统启动**
⏰ 时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
📊 状态: 系统初始化完成，策略已加载

系统信息:
• 主机: {platform.node()}
• 系统: {platform.system()} {platform.release()}
• Python: {platform.python_version()}
        """
        return self.send_text(message)

    def send_shutdown_notification(self) -> bool:
        """发送关闭通知"""
        message = f"""
🛑 **CTA策略系统关闭**
⏰ 时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
📊 状态: 系统正常退出
        """
        return self.send_text(message)


# 测试入口
if __name__ == "__main__":
    print("=" * 50)
    print("通知管理器测试")
    print("=" * 50)

    nm = NotificationManager()

    print("\n发送测试消息...")
    success = nm.send_test_message()

    if success:
        print("\n✅ 测试消息发送成功！请检查钉钉/飞书是否收到消息。")
    else:
        print("\n❌ 测试消息发送失败，请检查配置。")

    print("\n当前配置:")
    print(f"  通知类型: {nm.notify_type}")
    print(f"  钉钉 Webhook: {nm.dingtalk_webhook[:50]}...")
    print(f"  飞书 Webhook: {nm.feishu_webhook[:50]}...")

# notification_manager.py
import time
import hmac
import hashlib
import base64
import urllib.parse
import platform
from typing import Optional
from datetime import datetime
from pathlib import Path
import requests
import psutil
import yaml


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
        notify_type: Optional[str] = None,
        dingtalk_webhook: Optional[str] = None,
        dingtalk_secret: Optional[str] = None,
        feishu_webhook: Optional[str] = None,
        config_path: Optional[Path] = None,
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
                Path(__file__).resolve().parent.parent.parent / ".vntrader" / "secrets.yaml",
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

        self.notify_type = notify_type or secrets.get("notify_type", "") or self.NOTIFY_TYPE
        self.dingtalk_webhook = dingtalk_webhook or secrets.get("dingtalk_webhook", "") or self.DINGTALK_WEBHOOK
        self.dingtalk_secret = dingtalk_secret or secrets.get("dingtalk_secret", "") or self.DINGTALK_SECRET
        self.feishu_webhook = feishu_webhook or secrets.get("feishu_webhook", "") or self.FEISHU_WEBHOOK
        
        self.log_callback = None

        # 速率限制（当前已关闭）
        self._last_send_time: float = 0.0
        self._min_send_interval: float = 0.0

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
        hmac_code = hmac.new(secret_enc, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
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
            data = {
                "msgtype": "text",
                "text": {"content": text}
            }
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
            data = {
                "msg_type": "text",
                "content": {"text": text}
            }
            
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
    def send_text(self, text: str, force_type: Optional[str] = None) -> bool:
        """
        发送文本消息
        
        Parameters
        ----------
        text : 消息内容（由调用方生成）
        force_type : 强制使用的通知类型（"dingtalk"/"feishu"），不指定则使用默认配置
        
        Returns
        -------
        bool : 是否发送成功
        """
        # ── 速率限制 ──
        now = time.time()
        if now - self._last_send_time < self._min_send_interval:
            self._log(f"速率限制跳过：距上次发送仅 {now - self._last_send_time:.1f}s")
            return False
        self._last_send_time = now

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
    
    # ============================================================
    # 辅助方法：格式化各类消息
    # ============================================================
    def send_test_message(self) -> bool:
        """发送测试消息，用于验证配置是否正确"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        text = f"""📢 测试消息

这是一条测试消息，用于验证通知配置是否正确。

⏰ 时间：{now}
📱 钉钉：{'✅ 已配置' if self.dingtalk_webhook else '❌ 未配置'}
📱 飞书：{'✅ 已配置' if self.feishu_webhook else '❌ 未配置'}
🔄 通知类型：{self.notify_type}

✅ 配置验证中，请确认能收到此消息。"""
        return self.send_text(text)

    def send_status_summary(self, messages: list, total: int, idx: int, seq: int = 0) -> bool:
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
            dir_str = "多" if p.direction.value == "多" else "空"
            line = (f"  {p.vt_symbol:<14} {dir_str:<4} {p.volume:>4}  "
                    f"{p.price:>8.2f}  {p.pnl:>10.2f}")
            lines.append(line)

        return "\n".join(lines)

    def send_account_report(self, main_engine) -> None:
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

            try:
                cpu = f"{psutil.cpu_percent(interval=1)}%"
                mem = f"{psutil.virtual_memory().percent}%"
                disk = f"{psutil.disk_usage('/').percent}%"
                boot = datetime.fromtimestamp(psutil.boot_time())
                uptime = str(datetime.now() - boot).split('.')[0]
            except Exception:
                cpu = mem = disk = uptime = "N/A"

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sep = "─" * 40
            text = (
                f"📊 账户报告 ({now_str})\n"
                f"{sep}\n"
                f"  动态权益: {balance:>12.2f}\n"
                f"  可用资金: {available:>12.2f}\n"
                f"\n"
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
        blocked = [n for n, r in results.items() if r == "blocked"]
        no_change = [n for n, r in results.items() if r == "no_change"]
        failed = [n for n, r in results.items() if r == "failed"]

        lines = []
        if rolled:
            lines.append("🔄 已换月：" + "、".join(rolled))
        if blocked:
            lines.append("⚠️ 主力已变化但持仓不为零：" + "、".join(blocked))
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
⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
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
⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
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

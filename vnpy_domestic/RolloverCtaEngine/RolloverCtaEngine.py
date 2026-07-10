# rollover_cta_engine.py
import time
import csv
import os
from datetime import datetime
from typing import Optional, Dict
from concurrent.futures import Future

import requests
from vnpy.event import Event, EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import (
    SubscribeRequest,
    TradeData,
    OrderData,
)
from vnpy.trader.constant import Direction, Status
from vnpy.trader.utility import save_json
from vnpy.trader.event import EVENT_TIMER, EVENT_ACCOUNT

from vnpy_ctastrategy import CtaTemplate
from vnpy_ctastrategy.engine import CtaEngine

from vnpy_domestic.trader.notification_manager import NotificationManager


class RolloverCtaEngine(CtaEngine):
    """
    支持自动换月的 CTA 引擎，并集成：
    - 订单/成交实时通知（钉钉/飞书）+ CSV 持久化
    - 每5分钟策略运行状态监控
    - 主力合约变化自动换月（持仓为0时）
    """

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine)

        # 初始化通知管理器（配置已在 notification_manager.py 中）
        self.notify = NotificationManager()
        # 设置日志回调
        self.notify.set_log_callback(self.write_log)

        # CSV 文件存储
        self.strategy_csv_files: Dict[str, Dict[str, str]] = {}

        # 发单时间跟踪（用于成交延迟监控）
        self.order_submit_times: Dict[str, datetime] = {}

        # 定时监控相关
        self.last_monitor_time: float = 0.0
        self.monitor_interval: int = 420  # 7分钟 = 420秒

        # P&L 追踪（用于平仓推送盈亏）
        self.strategy_avg_price: Dict[str, float] = {}
        self.strategy_cumulative_pl: Dict[str, float] = {}

        # 订单缓存（用于滑点计算）
        self.orders: dict = {}

        # CTP 连接监控（账户事件作为心跳）
        self.last_activity_time: float = time.time()
        self.disconnect_alerted: bool = False

        # 主力合约缓存 {品种: 合约代码}，进程级，子进程退出自动清空
        self._main_contract_cache: dict[str, str] = {}

        # 初始化时换月检查结果（用于启动汇总通知）
        self._rollover_init_results: dict[str, str] = {}

        # 策略状态发送计数（防重复追踪）
        self._status_seq: int = 0

        # 注册定时器事件（每秒触发）
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

        # 注册账户事件（用于 CTP 连接心跳检测）
        self.event_engine.register(EVENT_ACCOUNT, self._on_account_event)

    # ----------------------------------------------------------------------
    # write_log 重写：日志写文件 + 错误级日志推手机
    # ----------------------------------------------------------------------
    def write_log(self, msg: str, strategy: CtaTemplate = None) -> None:
        """重写：写日志 + 推送失败/错误级日志到手机"""
        super().write_log(msg, strategy)
        if "失败" in msg or "错误" in msg:
            self.notify.send_text(f"⚠️ CTA 异常\n  {msg.strip()}")

    # ----------------------------------------------------------------------
    # CSV 初始化
    # ----------------------------------------------------------------------
    def _init_csv_files(self, strategy_name: str, vt_symbol: str) -> None:
        """为策略创建订单和成交 CSV 文件（保存在 .vntrader/ 下）"""
        vntrader_dir = ".vntrader"
        os.makedirs(vntrader_dir, exist_ok=True)

        base_name = f"{strategy_name}_{vt_symbol.replace('.', '_')}"
        order_csv = os.path.join(vntrader_dir, f"{base_name}_orders.csv")
        trade_csv = os.path.join(vntrader_dir, f"{base_name}_trades.csv")

        if not os.path.exists(order_csv):
            with open(order_csv, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "datetime", "symbol", "exchange", "orderid",
                    "direction", "offset", "price", "volume",
                    "traded", "status"
                ])

        if not os.path.exists(trade_csv):
            with open(trade_csv, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "datetime", "symbol", "exchange", "orderid",
                    "tradeid", "direction", "offset", "price", "volume",
                    "slippage", "delay_ms"
                ])

        self.strategy_csv_files[strategy_name] = {
            "order": order_csv,
            "trade": trade_csv
        }

    def _append_csv(self, label: str, csv_path: str, row: list) -> None:
        """追加一行到 CSV 文件"""
        try:
            with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(row)
        except Exception as e:
            self.write_log(f"{label} CSV写入异常: {e}")

    # ----------------------------------------------------------------------
    # 主力合约获取（新浪）
    # ----------------------------------------------------------------------
    def _fetch_main_contract(self, variety: str, retry: int = 3) -> Optional[str]:
        """获取主力合约（进程级缓存，子进程退出自动清空）"""
        cached = self._main_contract_cache.get(variety)
        if cached:
            return cached

        for attempt in range(retry):
            try:
                now = datetime.now()
                symbols = [f"{variety}0"]
                for offset in range(24):
                    y = now.year + (now.month + offset - 1) // 12
                    m = (now.month + offset - 1) % 12 + 1
                    symbols.append(f"{variety}{str(y)[2:]}{m:02d}")

                query = ",".join(f"nf_{s}" for s in symbols)
                url = f"https://hq.sinajs.cn/list={query}"
                headers = {
                    "Referer": "https://finance.sina.com.cn",
                    "User-Agent": "Mozilla/5.0"
                }
                resp = requests.get(url, headers=headers, timeout=10)
                resp.encoding = "gbk"
                if resp.status_code != 200:
                    continue

                target_position = None
                month_positions = {}
                for line in resp.text.strip().split(";"):
                    if '="' not in line:
                        continue
                    key_part, _, val = line.partition('="')
                    val = val.rstrip('"')
                    if not val:
                        continue
                    fields = val.split(",")
                    if len(fields) <= 13:
                        continue
                    symbol = key_part.rsplit("nf_", 1)[-1]
                    position = fields[13]
                    if symbol == f"{variety}0":
                        target_position = position
                    else:
                        month_positions[symbol] = position

                if target_position is None:
                    continue

                for month_symbol, pos in month_positions.items():
                    if pos == target_position:
                        self._main_contract_cache[variety] = month_symbol
                        return month_symbol
            except Exception:
                if attempt < retry - 1:
                    time.sleep(2)
        return None

    # ----------------------------------------------------------------------
    # 换月执行
    # ----------------------------------------------------------------------
    def _execute_rollover(self, strategy: CtaTemplate, new_vt_symbol: str) -> bool:
        """执行换月，成功返回 True，失败返回 False"""
        strategy_name = strategy.strategy_name
        old_vt_symbol = strategy.vt_symbol

        # 先验证新合约在 CTP 中存在，不存在则跳过（不碰任何状态）
        contract = self.main_engine.get_contract(new_vt_symbol)
        if not contract:
            self.notify.send_text(
                f"⚠️ 换月失败：找不到新合约 {new_vt_symbol}\n"
                f"  策略: {strategy_name}\n"
                f"  旧合约: {old_vt_symbol}（保持不变）"
            )
            self.write_log(f"换月失败：找不到新合约 {new_vt_symbol}，跳过换月", strategy)
            return False

        # 所有检查通过，执行换月
        self.stop_strategy(strategy_name)
        self.strategy_setting[strategy_name]["vt_symbol"] = new_vt_symbol
        save_json(self.setting_filename, self.strategy_setting)
        strategy.vt_symbol = new_vt_symbol

        req = SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange)
        self.main_engine.subscribe(req, contract.gateway_name)

        if old_vt_symbol in self.symbol_strategy_map:
            strategies = self.symbol_strategy_map[old_vt_symbol]
            if strategy in strategies:
                strategies.remove(strategy)
                if not strategies:
                    del self.symbol_strategy_map[old_vt_symbol]
        self.symbol_strategy_map.setdefault(new_vt_symbol, []).append(strategy)

        strategy.inited = False
        strategy.trading = False
        self.call_strategy_func(strategy, strategy.on_init)

        data = self.strategy_data.get(strategy_name, {})
        for name in strategy.variables:
            value = data.get(name, None)
            if value is not None:
                setattr(strategy, name, value)
        strategy.inited = True
        self.start_strategy(strategy_name)

        # 发送换月成功日志
        self.write_log(f"策略 [{strategy_name}] 已自动换月：{old_vt_symbol} -> {new_vt_symbol}", strategy)
        return True

    def _check_and_notify_rollover(self, strategy: CtaTemplate) -> None:
        self.write_log(f"策略 [{strategy.strategy_name}] 开始换月检查")

        try:
            symbol, exchange = strategy.vt_symbol.split(".")
            variety = ''.join(c for c in symbol if not c.isdigit())
            if not variety:
                return
        except Exception:
            return

        main_contract = self._fetch_main_contract(variety.upper())
        if not main_contract:
            self.write_log(f"换月检查失败：无法获取 {variety} 主力合约信息")
            return

        if main_contract.lower() == symbol:
            return

        new_vt_symbol = f"{main_contract.lower()}.{exchange}"
        self.write_log(f"策略 [{strategy.strategy_name}] 检测到主力合约变化：{strategy.vt_symbol} -> {new_vt_symbol}")

        if strategy.pos == 0:
            old_vt_symbol = strategy.vt_symbol
            if self._execute_rollover(strategy, new_vt_symbol):
                self.notify.send_text(
                    f"🔄 换月完成\n"
                    f"  策略: {strategy.strategy_name}\n"
                    f"  旧合约: {old_vt_symbol} → {new_vt_symbol}"
                )
        else:
            self.write_log(f"策略持仓不为零({strategy.pos})，暂不换月")
            self.notify.send_text(
                f"⚠️ 主力已变化，持仓不为零暂不换月\n"
                f"  策略: {strategy.strategy_name}\n"
                f"  旧合约: {strategy.vt_symbol}\n"
                f"  新主力: {new_vt_symbol}\n"
                f"  当前持仓: {strategy.pos} 手"
            )

    # ----------------------------------------------------------------------
    # CTP 连接心跳
    # ----------------------------------------------------------------------
    def _on_account_event(self, event: Event) -> None:
        """账户事件 → 连接心跳（收到 account 说明 CTP 还在线）"""
        self.last_activity_time = time.time()
        if self.disconnect_alerted:
            self.disconnect_alerted = False
            self.notify.send_text(
                f"🟢 CTP 连接已恢复 ({datetime.now().strftime('%H:%M:%S')})"
            )
            self.write_log("CTP 连接已恢复")

    # ----------------------------------------------------------------------
    # 订单 / 成交事件（通知 + CSV）
    # ----------------------------------------------------------------------
    def process_order_event(self, event: Event) -> None:
        super().process_order_event(event)
        self.last_activity_time = time.time()

        order: OrderData = event.data
        # 缓存订单用于滑点计算
        self.orders[order.vt_orderid] = order

        strategy = self.orderid_strategy_map.get(order.vt_orderid, None)
        if not strategy:
            return

        strategy_name = strategy.strategy_name

        # ── 记录发单时间（首次出现时） ──
        if order.vt_orderid not in self.order_submit_times:
            self.order_submit_times[order.vt_orderid] = datetime.now()

        # ── CSV 写入 ──
        if strategy_name not in self.strategy_csv_files:
            self._init_csv_files(strategy_name, strategy.vt_symbol)

        order_dict = {
            "datetime": str(order.datetime),
            "symbol": order.symbol,
            "exchange": order.exchange.value,
            "orderid": order.orderid,
            "direction": order.direction.value,
            "offset": order.offset.value,
            "price": order.price,
            "volume": order.volume,
            "traded": order.traded,
            "status": order.status.value,
        }

        csv_file = self.strategy_csv_files[strategy_name]["order"]
        self._append_csv(f"订单 {strategy_name}", csv_file, [
            order_dict["datetime"], order_dict["symbol"], order_dict["exchange"],
            order_dict["orderid"], order_dict["direction"], order_dict["offset"],
            order_dict["price"], order_dict["volume"], order_dict["traded"],
            order_dict["status"]
        ])

        # ── 通知：仅 rejected 推手机，其他只写本地日志 ──
        if order.status == Status.REJECTED:
            self.notify.send_text(
                f"❌ 订单被拒\n"
                f"  策略: {strategy_name}\n"
                f"  合约: {order.symbol}\n"
                f"  方向: {order.direction.value} {order.offset.value}\n"
                f"  价格: {order.price:.2f}  数量: {order.volume}\n"
                f"  时间: {order.datetime}"
            )
            self.write_log(f"订单被拒 {strategy_name} {order.vt_symbol} price={order.price}")
        else:
            self.write_log(
                f"订单更新 {strategy_name} {order.symbol} "
                f"{order.direction.value} {order.offset.value} "
                f"price={order.price} vol={order.volume} "
                f"traded={order.traded} status={order.status.value}"
            )

    def process_trade_event(self, event: Event) -> None:
        trade: TradeData = event.data
        if trade.vt_tradeid in self.vt_tradeids:
            return
        self.vt_tradeids.add(trade.vt_tradeid)
        self.last_activity_time = time.time()

        strategy = self.orderid_strategy_map.get(trade.vt_orderid, None)
        if not strategy:
            return

        strategy_name = strategy.strategy_name

        # ── 持仓翻转检测（记录旧持仓） ──
        old_pos = strategy.pos

        # 更新持仓
        if trade.direction == Direction.LONG:
            strategy.pos += trade.volume
        else:
            strategy.pos -= trade.volume

        new_pos = strategy.pos

        self.sync_strategy_data(strategy)
        self.put_strategy_event(strategy)

        # ── 滑点 + 延迟计算 ──
        slippage = 0.0
        delay_ms = 0.0
        order = self.orders.get(trade.vt_orderid)
        if order and order.price > 0:
            if trade.direction == Direction.LONG:
                slippage = trade.price - order.price
            else:
                slippage = order.price - trade.price
        submit_time = self.order_submit_times.get(trade.vt_orderid)
        if submit_time:
            delay_ms = (datetime.now() - submit_time).total_seconds() * 1000

        # ── 延迟异常 → 暂停开仓 ──
        if delay_ms > 2000:
            strategy.trading = False
            self.notify.send_text(
                f"⚠️ 成交延迟异常\n"
                f"  策略: {strategy_name}\n"
                f"  合约: {trade.symbol}\n"
                f"  延迟: {delay_ms:.0f}ms (>2000ms)\n"
                f"  已暂停开仓"
            )
            self.write_log(f"延迟异常暂停开仓 {strategy_name} delay={delay_ms:.0f}ms")

        # ── P&L 追踪 ──
        pl = 0.0
        if old_pos == 0 and new_pos != 0:
            # 开仓：记录开仓均价
            self.strategy_avg_price[strategy_name] = trade.price
        elif old_pos * new_pos > 0 and abs(new_pos) > abs(old_pos):
            # 加仓：加权平均
            old_avg = self.strategy_avg_price.get(strategy_name, trade.price)
            added_vol = abs(new_pos) - abs(old_pos)
            self.strategy_avg_price[strategy_name] = (
                (old_avg * abs(old_pos) + trade.price * added_vol) / abs(new_pos)
            )
        elif abs(new_pos) < abs(old_pos):
            # 减仓或平仓
            avg_price = self.strategy_avg_price.get(strategy_name)
            if avg_price:
                closed_vol = abs(old_pos) - abs(new_pos)
                direction_mult = 1 if old_pos > 0 else -1
                pl = (trade.price - avg_price) * closed_vol * direction_mult
                self.strategy_cumulative_pl[strategy_name] = \
                    self.strategy_cumulative_pl.get(strategy_name, 0) + pl
                if new_pos == 0:
                    # 完全平仓，清除均价
                    self.strategy_avg_price.pop(strategy_name, None)

        # ── 持仓翻转通知（含 P&L） ──
        if old_pos == 0 and new_pos != 0:
            self.notify.send_text(
                f"🟢 开仓\n"
                f"  策略: {strategy_name}\n"
                f"  合约: {trade.symbol}\n"
                f"  方向: {trade.direction.value}\n"
                f"  价格: {trade.price:.2f}  数量: {trade.volume}\n"
                f"  持仓: {strategy.pos:+d}"
            )
        elif old_pos != 0 and new_pos == 0:
            notify_text = (
                f"🔴 平仓\n"
                f"  策略: {strategy_name}\n"
                f"  合约: {trade.symbol}\n"
                f"  平仓价: {trade.price:.2f}  数量: {trade.volume}"
            )
            if pl != 0:
                notify_text += f"\n  本次盈亏: {pl:+.2f} 点"
            cum_pl = self.strategy_cumulative_pl.get(strategy_name)
            if cum_pl:
                notify_text += f"\n  累计盈亏: {cum_pl:+.2f} 点"
            self.notify.send_text(notify_text)

        # ── 本地日志 ──
        if slippage != 0 or delay_ms > 0:
            self.write_log(
                f"成交 {strategy_name} {trade.symbol} "
                f"{trade.direction.value} vol={trade.volume} "
                f"price={trade.price:.2f} "
                f"slippage={slippage:+.2f} delay={delay_ms:.0f}ms"
            )

        if strategy.pos == 0:
            self._check_and_notify_rollover(strategy)

        self.call_strategy_func(strategy, strategy.on_trade, trade)

        # ── 成交 CSV ──
        if strategy_name not in self.strategy_csv_files:
            self._init_csv_files(strategy_name, strategy.vt_symbol)

        trade_dict = {
            "datetime": str(trade.datetime),
            "symbol": trade.symbol,
            "exchange": trade.exchange.value,
            "orderid": trade.orderid,
            "tradeid": trade.tradeid,
            "direction": trade.direction.value,
            "offset": trade.offset.value,
            "price": trade.price,
            "volume": trade.volume,
            "slippage": round(slippage, 2),
            "delay_ms": round(delay_ms, 0),
        }

        csv_file = self.strategy_csv_files[strategy_name]["trade"]
        self._append_csv(f"成交 {strategy_name}", csv_file, [
            trade_dict["datetime"], trade_dict["symbol"], trade_dict["exchange"],
            trade_dict["orderid"], trade_dict["tradeid"], trade_dict["direction"],
            trade_dict["offset"], trade_dict["price"], trade_dict["volume"],
            trade_dict["slippage"], trade_dict["delay_ms"],
        ])

    # ----------------------------------------------------------------------
    # 定时监控（每5分钟，自动提取策略状态，合并分批发送）
    # ----------------------------------------------------------------------
    def process_timer_event(self, event: Event) -> None:
        """定时器事件：断连检测（每秒）+ 状态监控（每5分钟）"""
        current_time = time.time()

        # ── CTP 断连检测（每秒检查；恢复由 _on_account_event 处理） ──
        if not self.disconnect_alerted and current_time - self.last_activity_time > 120:
            self.disconnect_alerted = True
            self.notify.send_text(
                f"🔴 CTP 连接异常！已 {int(current_time - self.last_activity_time)} 秒无数据"
            )
            self.write_log(f"CTP 连接异常告警：{int(current_time - self.last_activity_time)}s 无活动")

        # ── 策略监控 ──
        if current_time - self.last_monitor_time < self.monitor_interval:
            return
        self.last_monitor_time = current_time

        messages = []
        for strategy_name, strategy in self.strategies.items():
            if not strategy.inited or not strategy.trading:
                continue

            # 优先使用策略自定义的监控消息
            if hasattr(strategy, "get_monitor_message") and callable(strategy.get_monitor_message):
                try:
                    custom_msg = strategy.get_monitor_message()
                    if custom_msg:
                        messages.append(custom_msg)
                        continue
                except Exception as e:
                    self.write_log(f"获取策略 {strategy_name} 自定义消息失败: {e}")

            # 自动生成消息
            lines = [
                f"▸ {strategy.__class__.__name__}  ({strategy_name})",
                f"  合约: {strategy.vt_symbol}  |  持仓: {strategy.pos:+d}",
            ]

            # BarGenerator 状态
            if hasattr(strategy, "bg") and strategy.bg is not None:
                bg = strategy.bg
                clz = bg.__class__.__name__
                win = getattr(bg, "window", 0)
                if hasattr(bg, "enable_trading_filter"):
                    lines.append(f"  K线: {clz}  |  window={win}  |  时段过滤=开启")
                else:
                    lines.append(f"  K线: {clz}  |  window={win}")

            # ArrayManager 状态
            if hasattr(strategy, "am") and strategy.am is not None:
                am = strategy.am
                lines.append(f"  缓存: {am.count}/{am.size}  |  ready={am.inited}")

            # SaveStrategy 无 ArrayManager，显示 bar_count
            if hasattr(strategy, "bar_count") and not (hasattr(strategy, "am") and strategy.am is not None):
                lines.append(f"  已保存: {strategy.bar_count} 条Bar")

            # 策略关键变量（过滤引擎状态字段）
            _engine_vars = {"inited", "trading", "pos"}
            values = []
            for name in strategy.parameters + strategy.variables:
                if name in _engine_vars:
                    continue
                value = getattr(strategy, name, None)
                if value is not None:
                    if isinstance(value, float):
                        values.append(f"{name}={value:.2f}")
                    else:
                        values.append(f"{name}={value}")
            if values:
                lines.append(f"  {'  '.join(values)}")

            messages.append("\n".join(lines))

        if not messages:
            return

        # 分批发送
        MAX_LEN = 5000
        separator = "\n\n"
        timestamp = datetime.now().strftime("%H:%M:%S")
        header = f"📊 策略状态 ({timestamp})"

        batches: list[list[str]] = []
        current_batch: list[str] = []
        current_len = len(header)

        for msg in messages:
            add_len = len(separator) + len(msg) if current_batch else len(msg)
            if current_len + add_len <= MAX_LEN:
                current_batch.append(msg)
                current_len += add_len
            else:
                batches.append(current_batch)
                current_batch = [msg]
                current_len = len(header) + len(msg)
        if current_batch:
            batches.append(current_batch)

        total = len(batches)
        self._status_seq += 1
        for idx, batch in enumerate(batches, start=1):
            self.notify.send_status_summary(batch, total, idx, seq=self._status_seq)

    # ----------------------------------------------------------------------
    # _init_strategy — 初始化前检查换月（仅记录结果，不发通知）
    # ----------------------------------------------------------------------
    def _init_strategy(self, strategy_name: str) -> None:
        """翻写：初始化前检查是否需要换月，仅记录结果不发通知"""
        strategy = self.strategies.get(strategy_name)
        if strategy:
            self._check_rollover_for_init(strategy)
            # 换月成功时 _execute_rollover 已完成全套 on_init + inited=True + start
            # 跳过父类 _init_strategy 避免重复操作
            if strategy.inited:
                return
        super()._init_strategy(strategy_name)

    def _check_rollover_for_init(self, strategy: CtaTemplate) -> None:
        """初始化时的换月检查：只记录结果，不发送通知"""
        self.write_log(f"策略 [{strategy.strategy_name}] 开始换月检查（初始化）")
        name = strategy.strategy_name

        try:
            symbol, exchange = strategy.vt_symbol.split(".")
            variety = ''.join(c for c in symbol if not c.isdigit())
            if not variety:
                self._rollover_init_results[name] = "no_change"
                return
        except Exception:
            self._rollover_init_results[name] = "no_change"
            return

        main_contract = self._fetch_main_contract(variety.upper())
        if not main_contract:
            self.write_log(f"换月检查失败：无法获取 {variety} 主力合约信息")
            self._rollover_init_results[name] = "failed"
            return

        if main_contract.lower() == symbol:
            self._rollover_init_results[name] = "no_change"
            return

        new_vt_symbol = f"{main_contract.lower()}.{exchange}"
        self.write_log(f"策略 [{name}] 检测到主力合约变化：{strategy.vt_symbol} -> {new_vt_symbol}")

        if strategy.pos == 0:
            old_vt_symbol = strategy.vt_symbol
            if self._execute_rollover(strategy, new_vt_symbol):
                self.write_log(f"策略 [{name}] 初始化时已自动换月：{old_vt_symbol} -> {new_vt_symbol}")
                self._rollover_init_results[name] = "rolled"
            else:
                self._rollover_init_results[name] = "failed"
        else:
            self.write_log(f"策略持仓不为零({strategy.pos})，暂不换月")
            self._rollover_init_results[name] = "blocked"

    def send_rollover_init_summary(self) -> None:
        """发送初始化换月检查汇总通知"""
        self.notify.send_rollover_init_summary(self._rollover_init_results)
        self._rollover_init_results.clear()



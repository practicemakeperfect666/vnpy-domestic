# rollover_cta_engine.py
import time
import csv
from datetime import datetime
from typing import Optional, Dict

import requests
from vnpy.event import Event, EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import (
    SubscribeRequest,
    TradeData,
    OrderData,
)
from vnpy.trader.constant import Direction, Status, Offset
from vnpy.trader.utility import save_json, get_file_path
from vnpy.trader.event import EVENT_TIMER, EVENT_ACCOUNT

from vnpy_ctastrategy import CtaTemplate
from vnpy_ctastrategy.engine import CtaEngine

from vnpy_domestic.trader.notification_manager import NotificationManager
from vnpy_domestic.trader.position_lots import trading_day, settle_close

# CTP InstrumentID 字母小写的交易所，其余（CZCE/CFFEX）大写
LOWER_CASE_EXCHANGES = ("DCE", "SHFE", "INE", "GFEX")


def normalize_vt_symbol(vt_symbol: str) -> str:
    """统一合约代码为 CTP InstrumentID 格式（字母大小写 + 年份位数）

    rb2610/cu2610/sc2610（SHFE/INE 小写）、m2701/si2611（DCE/GFEX 小写）、
    MA701/IF2609（CZCE/CFFEX 大写，CZCE 年份 1 位）
    """
    if "." not in vt_symbol:
        return vt_symbol

    symbol, exchange = vt_symbol.rsplit(".", 1)
    exchange = exchange.upper()
    variety = "".join(c for c in symbol if not c.isdigit())
    digits = symbol[len(variety):]
    if not variety or not digits.isdigit():
        return f"{symbol}.{exchange}"

    if exchange == "CZCE":
        if len(digits) == 4:            # RM2701 -> RM701
            digits = digits[1:]
    elif len(digits) == 3:              # rb610 -> rb2610
        digits = str(datetime.now().year)[2] + digits

    if exchange in LOWER_CASE_EXCHANGES:
        variety = variety.lower()
    else:
        variety = variety.upper()

    return f"{variety}{digits}.{exchange}"


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
        self.strategy_lots: Dict[str, list] = {}       # strategy_name -> [[vol, price, day], ...]
        self.strategy_cumulative_pl: Dict[str, float] = {}

        # 订单缓存（用于滑点计算）
        self.orders: dict = {}

        # 每日订单状态统计（挂撤单/未成交等）
        self._daily_order_counts: dict = {}
        self._daily_order_ids: set = set()   # 当天唯一订单号（"共N笔"去重用）
        self._order_stat_date: str = ""

        # NOTTRADED 通知去重（避免 CTP 重复推送刷屏）
        self._notified_nottraded: set = set()

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
    def _init_csv_files(self, strategy_name: str) -> None:
        """为策略创建订单/成交/持仓 CSV（每策略一个文件夹，在 .vntrader/strategy_records/ 下）"""
        folder = get_file_path(f"strategy_records/{strategy_name}")
        folder.mkdir(parents=True, exist_ok=True)
        order_csv = folder / "orders.csv"
        trade_csv = folder / "trades.csv"
        position_csv = folder / "positions.csv"

        if not order_csv.exists():
            with open(order_csv, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "datetime", "symbol", "exchange", "orderid",
                    "direction", "offset", "price", "volume",
                    "traded", "status"
                ])

        if not trade_csv.exists():
            with open(trade_csv, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "datetime", "symbol", "exchange", "orderid",
                    "tradeid", "direction", "offset", "price", "volume",
                    "slippage", "delay_ms", "avg_price"
                ])

        if not position_csv.exists():
            with open(position_csv, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "datetime", "symbol", "exchange", "direction", "offset",
                    "trade_price", "trade_volume",
                    "pos", "long_pos", "short_pos", "long_avg", "short_avg",
                    "avg_cost", "cumulative_pl"
                ])

        self.strategy_csv_files[strategy_name] = {
            "order": str(order_csv),
            "trade": str(trade_csv),
            "position": str(position_csv)
        }

    def _append_csv(self, label: str, csv_path: str, row: list) -> None:
        """追加一行到 CSV 文件"""
        try:
            with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(row)
        except Exception as e:
            self.write_log(f"{label} CSV写入异常: {e}")

    @staticmethod
    def _lots_avg(lots) -> float:
        """剩余批次加权均价，空则 0"""
        if not lots:
            return 0.0
        total_v = sum(b[0] for b in lots)
        if total_v == 0:
            return 0.0
        return sum(b[0] * b[1] for b in lots) / total_v

    def _append_position_csv(self, strategy: CtaTemplate, strategy_name: str,
                             trade_dict: dict) -> None:
        """每次成交后追加一行持仓快照（锁仓策略多空分记，非锁仓走引擎批次）"""
        is_locked = hasattr(strategy, "long_pos") and hasattr(strategy, "short_pos")
        long_pos = getattr(strategy, "long_pos", 0) or 0
        short_pos = getattr(strategy, "short_pos", 0) or 0
        if is_locked:
            cum_pl = getattr(strategy, "realized_pl", 0) or 0
            long_avg = self._lots_avg(getattr(strategy, "long_lots", None))
            short_avg = self._lots_avg(getattr(strategy, "short_lots", None))
            avg_cost = 0.0
        else:
            cum_pl = self.strategy_cumulative_pl.get(strategy_name, 0) or 0
            long_avg = short_avg = 0.0
            avg_cost = self._lots_avg(self.strategy_lots.get(strategy_name))
        csv_file = self.strategy_csv_files[strategy_name]["position"]
        self._append_csv(f"持仓 {strategy_name}", csv_file, [
            trade_dict["datetime"], trade_dict["symbol"], trade_dict["exchange"],
            trade_dict["direction"], trade_dict["offset"],
            trade_dict["price"], trade_dict["volume"],
            strategy.pos, long_pos, short_pos,
            round(long_avg, 2), round(short_avg, 2),
            round(avg_cost, 2), round(cum_pl, 2),
        ])

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

        # 记录 stop 前是否在运行（初始化时为 False，避免和 start_all_strategies 重复启动）
        was_trading = strategy.trading

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
        # 换月后保留累计盈亏（历史已实现盈亏跨合约延续）
        cum_pl = data.get("cumulative_pl")
        if cum_pl:
            self.strategy_cumulative_pl[strategy_name] = cum_pl
        strategy.inited = True
        # 仅当原来就在运行时才 restart（定时换月）；初始化时跳过让 start_all_strategies 统一启动
        if was_trading:
            self.start_strategy(strategy_name)

        # 发送换月成功日志
        self.write_log(f"策略 [{strategy_name}] 已自动换月：{old_vt_symbol} -> {new_vt_symbol}", strategy)
        return True

    def _has_position(self, strategy: CtaTemplate) -> bool:
        """判断策略是否实际有持仓（含锁仓：净持仓为0但多空单边非零）"""
        if strategy.pos != 0:
            return True
        long_pos = getattr(strategy, "long_pos", 0) or 0
        short_pos = getattr(strategy, "short_pos", 0) or 0
        return long_pos != 0 or short_pos != 0

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

        new_vt_symbol = normalize_vt_symbol(f"{main_contract}.{exchange}")
        if new_vt_symbol == normalize_vt_symbol(strategy.vt_symbol):
            return

        self.write_log(f"策略 [{strategy.strategy_name}] 检测到主力合约变化：{strategy.vt_symbol} -> {new_vt_symbol}")

        if not self._has_position(strategy):
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

        # ── 每日订单状态统计（"总计"按唯一订单号去重，见 get_daily_order_stats）──
        status_key: str = order.status.value
        self._daily_order_ids.add(order.vt_orderid)
        self._daily_order_counts[status_key] = self._daily_order_counts.get(status_key, 0) + 1

        # ── 记录发单时间（首次出现时） ──
        if order.vt_orderid not in self.order_submit_times:
            self.order_submit_times[order.vt_orderid] = datetime.now()

        # ── CSV 写入 ──
        if strategy_name not in self.strategy_csv_files:
            self._init_csv_files(strategy_name)

        csv_file = self.strategy_csv_files[strategy_name]["order"]
        self._append_csv(f"订单 {strategy_name}", csv_file, [
            str(order.datetime), order.symbol, order.exchange.value,
            order.orderid, order.direction.value, order.offset.value,
            order.price, order.volume, order.traded, order.status.value,
        ])

        # ── 通知：rejected + 平仓未成交推手机 ──
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
        elif order.status == Status.NOTTRADED:
            if order.vt_orderid not in self._notified_nottraded:
                self._notified_nottraded.add(order.vt_orderid)
                label = "平仓单" if order.offset in (Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY) else "开仓单"
                notify_text = (
                    f"⚠️ {label}未成交\n"
                    f"  策略: {strategy_name}\n"
                    f"  合约: {order.symbol}\n"
                    f"  方向: {order.direction.value} {order.offset.value}\n"
                    f"  价格: {order.price:.2f}  数量: {order.volume}"
                )
                # 平仓单：加预估盈亏（挂单价 vs 开仓成本价）
                if label == "平仓单":
                    lots = self.strategy_lots.get(strategy_name)
                    if lots:
                        avg_price = lots[0][1]  # FIFO 队头成本
                        direction_mult = -1 if order.direction == Direction.LONG else 1
                        est_pl = (order.price - avg_price) * order.volume * direction_mult
                        notify_text += f"\n  预估盈亏: {est_pl:+.2f} 点"
                self.notify.send_text(notify_text)
        elif order.status == Status.CANCELLED:
            self.notify.send_text(
                f"🟡 订单已撤销\n"
                f"  策略: {strategy_name}\n"
                f"  合约: {order.symbol}\n"
                f"  方向: {order.direction.value} {order.offset.value}\n"
                f"  价格: {order.price:.2f}  数量: {order.volume}"
            )
            self.write_log(f"订单撤销 {strategy_name} {order.vt_symbol} price={order.price}")
        else:
            self.write_log(
                f"订单更新 {strategy_name} {order.symbol} "
                f"{order.direction.value} {order.offset.value} "
                f"price={order.price} vol={order.volume} "
                f"traded={order.traded} status={order.status.value}"
            )

        # 终态订单（撤单/拒单）无成交，清理缓存防内存泄漏
        if order.status in (Status.REJECTED, Status.CANCELLED):
            self._cleanup_order_cache(order.vt_orderid)

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

        # 清理 NOTTRADED 通知记录
        self._notified_nottraded.discard(trade.vt_orderid)

        # 更新持仓
        if trade.direction == Direction.LONG:
            strategy.pos += trade.volume
        else:
            strategy.pos -= trade.volume

        self.put_strategy_event(strategy)

        # ── 滑点计算 ──
        slippage = 0.0
        order = self.orders.get(trade.vt_orderid)
        if order and order.price > 0:
            if trade.direction == Direction.LONG:
                slippage = trade.price - order.price
            else:
                slippage = order.price - trade.price

        # ── 延迟计算（所有订单统一统计：成交时间 - 发单时间） ──
        delay_ms = 0.0
        if order:
            submit_time = self.order_submit_times.get(trade.vt_orderid)
            if submit_time and trade.datetime:
                td = trade.datetime
                if td.tzinfo is not None:
                    td = td.astimezone().replace(tzinfo=None)  # → local naive
                delay_ms = (td - submit_time).total_seconds() * 1000

        # ── 持仓均价快照（成交前，写 CSV 用；开仓时无持仓记 0） ──
        avg_price_before = self._lots_avg(self.strategy_lots.get(strategy_name))

        # ── P&L 追踪（批次队列按交易所规则匹配；锁仓策略多空分记跳过由策略自算） ──
        pl = 0.0
        is_locked = hasattr(strategy, "long_pos") and hasattr(strategy, "short_pos")
        if not is_locked:
            if trade.offset == Offset.OPEN:
                lots = self.strategy_lots.setdefault(strategy_name, [])
                lots.append([trade.volume, trade.price, trading_day(trade.datetime)])
            else:
                lots = self.strategy_lots.get(strategy_name)
                if lots:
                    direction_mult = 1 if trade.direction == Direction.SHORT else -1
                    today = trading_day(trade.datetime)
                    pl = settle_close(lots, trade.exchange, trade.offset,
                                      trade.volume, today, trade.price, direction_mult)
                    self.strategy_cumulative_pl[strategy_name] = \
                        self.strategy_cumulative_pl.get(strategy_name, 0) + pl

        # ── 本地日志 ──
        if slippage != 0 or delay_ms > 0:
            self.write_log(
                f"成交 {strategy_name} {trade.symbol} "
                f"{trade.direction.value} vol={trade.volume} "
                f"price={trade.price:.2f} "
                f"slippage={slippage:+.2f} delay={delay_ms:.0f}ms"
            )

        self.call_strategy_func(strategy, strategy.on_trade, trade)

        # 换月检查（on_trade 之后，锁仓的 long_pos/short_pos 已更新为最新值）
        if not self._has_position(strategy):
            self._check_and_notify_rollover(strategy)

        # ── 开平仓通知（on_trade 之后，锁仓的 long_pos/short_pos 已更新） ──
        if trade.offset == Offset.OPEN:
            if is_locked:
                pos_str = f"多{getattr(strategy, 'long_pos', 0)}/空{getattr(strategy, 'short_pos', 0)}"
            else:
                pos_str = f"{strategy.pos:+d}"
            self.notify.send_text(
                f"🟢 开仓\n"
                f"  策略: {strategy_name}\n"
                f"  合约: {trade.symbol}\n"
                f"  方向: {trade.direction.value}\n"
                f"  价格: {trade.price:.2f}  数量: {trade.volume}\n"
                f"  持仓: {pos_str}"
            )
        elif trade.offset in (Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY):
            notify_text = (
                f"🔴 平仓\n"
                f"  策略: {strategy_name}\n"
                f"  合约: {trade.symbol}\n"
                f"  平仓价: {trade.price:.2f}  数量: {trade.volume}"
            )
            if is_locked:
                notify_text += f"\n  剩余: 多{getattr(strategy, 'long_pos', 0)}/空{getattr(strategy, 'short_pos', 0)}"
                lock_pl = getattr(strategy, "last_close_pl", 0) or 0
                if lock_pl:
                    notify_text += f"\n  本次盈亏: {lock_pl:+.2f} 点"
                lock_cum = getattr(strategy, "realized_pl", 0) or 0
                if lock_cum:
                    notify_text += f"\n  累计盈亏: {lock_cum:+.2f} 点"
            else:
                if pl != 0:
                    notify_text += f"\n  本次盈亏: {pl:+.2f} 点"
                cum_pl = self.strategy_cumulative_pl.get(strategy_name)
                if cum_pl:
                    notify_text += f"\n  累计盈亏: {cum_pl:+.2f} 点"
            self.notify.send_text(notify_text)

        self.sync_strategy_data(strategy)

        # ── 成交 CSV ──
        if strategy_name not in self.strategy_csv_files:
            self._init_csv_files(strategy_name)

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
            "avg_price": round(avg_price_before or trade.price, 2),
        }

        csv_file = self.strategy_csv_files[strategy_name]["trade"]
        self._append_csv(f"成交 {strategy_name}", csv_file, [
            trade_dict["datetime"], trade_dict["symbol"], trade_dict["exchange"],
            trade_dict["orderid"], trade_dict["tradeid"], trade_dict["direction"],
            trade_dict["offset"], trade_dict["price"], trade_dict["volume"],
            trade_dict["slippage"], trade_dict["delay_ms"], trade_dict["avg_price"],
        ])

        # ── 持仓 CSV（每次成交后的持仓快照）──
        self._append_position_csv(strategy, strategy_name, trade_dict)

        # 订单全部成交后清理缓存，防内存泄漏（撤单/拒单在 process_order_event 清理）
        if order is not None and order.status == Status.ALLTRADED:
            self._cleanup_order_cache(trade.vt_orderid)

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

    def send_daily_pl_summary(self) -> None:
        """收盘退出前发送当日盈亏汇总（已实现盈亏，点·手 × 合约乘数 = 金额）"""
        lines = []
        total = 0.0
        for name, strategy in self.strategies.items():
            is_locked = hasattr(strategy, "long_pos") and hasattr(strategy, "short_pos")
            if is_locked:
                pl_pts = getattr(strategy, "realized_pl", 0) or 0
                long_pos = getattr(strategy, "long_pos", 0) or 0
                short_pos = getattr(strategy, "short_pos", 0) or 0
                pos_str = f"多{long_pos}/空{short_pos}"
                if pl_pts == 0 and long_pos == 0 and short_pos == 0:
                    continue
            else:
                pl_pts = self.strategy_cumulative_pl.get(name, 0) or 0
                pos_str = f"{strategy.pos:+d}"
                if pl_pts == 0 and strategy.pos == 0:
                    continue

            contract = self.main_engine.get_contract(strategy.vt_symbol)
            size = contract.size if contract else 0
            if size > 0:
                total += pl_pts * size
                pl_str = f"{pl_pts * size:+.2f} 元"
            else:
                pl_str = f"{pl_pts:+.2f} 点·手"
            lines.append(
                f"  {name}  {strategy.vt_symbol}\n"
                f"    已实现: {pl_str} | 持仓: {pos_str}"
            )

        sep = "─" * 30
        if not lines:
            text = f"📊 当日盈亏汇总\n{sep}\n  今日无交易"
        else:
            text = f"📊 当日盈亏汇总\n{sep}\n" + "\n\n".join(lines) + f"\n{sep}\n  合计已实现: {total:+.2f} 元"
        self.notify.send_text(text)

    def reset_daily_pl(self) -> None:
        """每日收盘归零累计盈亏：次日从 0 起统计当日盈亏（隔夜持仓按逐日盯市已结算，不影响）"""
        for name, strategy in self.strategies.items():
            if hasattr(strategy, "realized_pl"):
                strategy.realized_pl = 0.0
            self.strategy_cumulative_pl[name] = 0.0

    # ----------------------------------------------------------------------
    # _init_strategy — 初始化前检查换月（仅记录结果，不发通知）
    # ----------------------------------------------------------------------
    def sync_strategy_data(self, strategy: CtaTemplate) -> None:
        """重写：持久化策略变量 + 引擎持仓批次 + 累计盈亏（重启恢复，避免平仓盈亏丢失）"""
        data: dict = strategy.get_variables()
        data.pop("inited")
        data.pop("trading")
        lots = self.strategy_lots.get(strategy.strategy_name)
        if lots:
            data["lots"] = [list(b) for b in lots]
        else:
            data.pop("lots", None)
        cum_pl = self.strategy_cumulative_pl.get(strategy.strategy_name)
        if cum_pl:
            data["cumulative_pl"] = cum_pl
        else:
            data.pop("cumulative_pl", None)
        self.strategy_data[strategy.strategy_name] = data
        save_json(self.data_filename, self.strategy_data)

    def add_strategy(
        self, class_name: str, strategy_name: str, vt_symbol: str, setting: dict
    ) -> None:
        """添加策略前统一合约代码大小写与年份位数（配置写错也能订阅成功）"""
        fixed = normalize_vt_symbol(vt_symbol)
        if fixed != vt_symbol:
            self.write_log(f"策略 [{strategy_name}] 合约代码已规范化：{vt_symbol} -> {fixed}")
        super().add_strategy(class_name, strategy_name, fixed, setting)

    def _init_strategy(self, strategy_name: str) -> None:
        """翻写：初始化前检查是否需要换月，仅记录结果不发通知"""
        strategy = self.strategies.get(strategy_name)
        if strategy:
            self._check_rollover_for_init(strategy)
            # 换月成功时 _execute_rollover 已完成全套 on_init + inited=True + start
            # 跳过父类 _init_strategy 避免重复操作
            if strategy.inited:
                return
        try:
            super()._init_strategy(strategy_name)
        except Exception as e:
            self.write_log(f"策略 {strategy_name} 初始化异常: {e}")
            # 兜底：线程池吞异常时手动补 set inited
            s = self.strategies.get(strategy_name)
            if s and not s.inited:
                s.inited = True
                self.put_strategy_event(s)
                self.write_log(f"策略 {strategy_name} 初始化完成（兜底）")

        # 恢复引擎持仓批次 + 累计盈亏（持久化字段，重启后平仓盈亏仍可算）
        data = self.strategy_data.get(strategy_name, {})
        lots = data.get("lots")
        if lots:
            self.strategy_lots[strategy_name] = [list(b) for b in lots]
        cum_pl = data.get("cumulative_pl")
        if cum_pl:
            self.strategy_cumulative_pl[strategy_name] = cum_pl

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

        new_vt_symbol = normalize_vt_symbol(f"{main_contract}.{exchange}")
        if new_vt_symbol == normalize_vt_symbol(strategy.vt_symbol):
            self._rollover_init_results[name] = "no_change"
            return

        self.write_log(f"策略 [{name}] 检测到主力合约变化：{strategy.vt_symbol} -> {new_vt_symbol}")

        if not self._has_position(strategy):
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

    def _cleanup_order_cache(self, vt_orderid: str) -> None:
        """订单终结后清理缓存，防内存泄漏（全部成交/撤单/拒单时调用）"""
        self.order_submit_times.pop(vt_orderid, None)
        self.orders.pop(vt_orderid, None)
        self._notified_nottraded.discard(vt_orderid)

    def get_daily_order_stats(self) -> dict:
        """返回每日订单状态统计，跨自然日自动清零（"总计"=唯一订单数）"""
        today = datetime.now().strftime("%Y%m%d")
        if self._order_stat_date != today:
            self._daily_order_counts.clear()
            self._daily_order_ids.clear()
            self._order_stat_date = today
        result = dict(self._daily_order_counts)
        result["总计"] = len(self._daily_order_ids)
        return result



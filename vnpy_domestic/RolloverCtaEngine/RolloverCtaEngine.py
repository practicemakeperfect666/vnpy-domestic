# rollover_cta_engine.py
import copy
import os
import threading
import time
from datetime import date, datetime, timedelta

import psutil
from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_TIMER
from vnpy.trader.object import (
    OrderData,
    OrderRequest,
    SubscribeRequest,
    TradeData,
)
from vnpy.trader.utility import save_json
from vnpy_ctastrategy import CtaTemplate
from vnpy_ctastrategy.engine import CtaEngine

from vnpy_domestic.trader.notification_manager import NotificationManager
from vnpy_domestic.trader.position_lots import settle_close, trading_day

from pathlib import Path

from vnpy_domestic.rollover import RolloverTable

# 仓库根（vnpy_domestic/RolloverCtaEngine/RolloverCtaEngine.py → 上两级）
REPO_ROOT = Path(__file__).resolve().parents[2]
# 换月表超过这么多天没更新就拒绝拿它判主力（daily.py 每天会覆盖）
MAX_TABLE_STALE_DAYS = 10

from vnpy_domestic.monitor import (
    Account as MonitorAccount,
    AccountIntradayPnl as MonitorAccountIntradayPnl,
    AccountPosition as MonitorAccountPosition,
    Contract as MonitorContract,
    DailyPnl as MonitorDailyPnl,
    Kline as MonitorKline,
    Log as MonitorLog,
    Order as MonitorOrder,
    Position as MonitorPosition,
    StrategyIntraday as MonitorStrategyIntraday,
    StrategyStatus as MonitorStrategyStatus,
    SystemMetric as MonitorSystemMetric,
    Trade as MonitorTrade,
    TradeRound as MonitorTradeRound,
    MonitorWriter,
)

# CTP InstrumentID 字母小写的交易所，其余（CZCE/CFFEX）大写
LOWER_CASE_EXCHANGES = ("DCE", "SHFE", "INE", "GFEX")


def _variety_of(vt_symbol: str) -> str:
    """'m2701.DCE' -> 'm'（去掉月份数字）"""
    symbol = vt_symbol.rsplit(".", 1)[0]
    return "".join(c for c in symbol if not c.isdigit())


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
    digits = symbol[len(variety) :]
    if not variety or not digits.isdigit():
        return f"{symbol}.{exchange}"

    if exchange == "CZCE":
        if len(digits) == 4:  # RM2701 -> RM701
            digits = digits[1:]
    elif len(digits) == 3:  # rb610 -> rb2610
        digits = str(datetime.now().year)[2] + digits

    if exchange in LOWER_CASE_EXCHANGES:
        variety = variety.lower()
    else:
        variety = variety.upper()

    return f"{variety}{digits}.{exchange}"


class RolloverCtaEngine(CtaEngine):
    """
    支持自动换月的 CTA 引擎，并集成：
    - 订单/成交实时通知（钉钉/飞书）+ 监控写库（monitor.db）
    - 每5分钟策略运行状态监控
    - 主力合约变化自动换月（持仓为0时）
    """

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine)

        # 初始化通知管理器（配置已在 notification_manager.py 中）
        self.notify = NotificationManager()
        # 设置日志回调
        self.notify.set_log_callback(self.write_log)

        # 发单/撤单指令时刻 + 延迟缓存（用于延迟监控）
        self.order_send_times: dict[str, datetime] = {}   # 发单指令时刻
        self.order_cancel_times: dict[str, datetime] = {}  # 撤单指令时刻
        self.order_queue_delay: dict[str, float] = {}      # 报单→入队延迟(ms)
        self.order_cancel_delay: dict[str, float] = {}     # 撤单→撤单终态延迟(ms)

        # 定时监控相关
        self.last_monitor_time: float = 0.0
        self.monitor_interval: int = 420  # 7分钟 = 420秒

        # P&L 追踪（用于平仓推送盈亏）
        self.strategy_lots: dict[
            str, list
        ] = {}  # strategy_name -> [[vol, price, day], ...]
        self.strategy_cumulative_pl: dict[str, float] = {}

        # 订单缓存（用于滑点计算）
        self.orders: dict = {}

        # 每日订单状态统计（挂撤单/未成交等；按唯一订单号记最新状态）
        self._daily_order_status: dict = {}  # vt_orderid -> 最新 status value
        self._order_stat_date: str = ""

        # NOTTRADED 通知去重（避免 CTP 重复推送刷屏）
        self._notified_nottraded: set = set()

        # CTP 连接监控（账户事件作为心跳）
        self.last_activity_time: float = time.time()
        self.disconnect_alerted: bool = False

        # 主力合约缓存 {品种: 合约代码}，进程级，子进程退出自动清空
        self._main_contract_cache: dict[str, str] = {}

        # 换月移仓：冻结中的策略（冻结期间 send_order 直接丢弃）、迁移自身下单旁路
        self._migrating: set[str] = set()
        self._rollover_failed_until: dict[str, float] = {}
        # 移仓成交后待执行的"切合约"动作（只由事件线程消费）
        self._pending_switch: dict | None = None
        self._migration_ctx: bool = False
        # 定时换月检查（秒）
        self.rollover_check_interval: int = 60
        # 双尺子：策略指标用复权价（tick 乘因子），下单价格再除回真实价
        self.adjust_strategy_prices: bool = \
            os.getenv("ROLLOVER_ADJUST_PRICES", "0") == "1"
        # 每日换月表更新提醒（收盘后，仅交易日；每天只提醒一次）
        self.rollover_table_remind_at: tuple[int, int] = (15, 10)
        self._table_reminded_day: date | None = None
        # 移仓委托超价跳数（2~3 跳基本能吃透盘口）
        self.rollover_slippage_ticks: int = int(os.getenv("ROLLOVER_SLIPPAGE_TICKS", "3"))
        self._last_rollover_check: float = 0.0

        # 初始化时换月检查结果（用于启动汇总通知）
        self._rollover_init_results: dict[str, str] = {}

        # 策略状态发送计数（防重复追踪）
        self._status_seq: int = 0

        # 注册定时器事件（每秒触发）
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

        # 注册账户事件（用于 CTP 连接心跳检测）
        self.event_engine.register(EVENT_ACCOUNT, self._on_account_event)

        # ── 监控写库（可选：设置 MONITOR_DB_PATH 才启用，非阻塞不干扰交易） ──
        self.monitor = None
        db_path = os.getenv("MONITOR_DB_PATH")
        if db_path:
            self.monitor = MonitorWriter(db_path)
        self.account = os.getenv("CTP_MODE", "real")

        # ── 快照缓存（浮动盈亏/保证金计算用） ──
        self.last_prices: dict = {}  # vt_symbol -> last_price
        self._last_snapshot_time: float = 0.0
        self.snapshot_interval: int = 3  # 快照间隔（秒）
        self._accountid: str = ""
        self._last_balance: float = 0.0
        self._last_available: float = 0.0
        self._last_frozen: float = 0.0
        self._today_realized_pl: float = 0.0
        self._today_trading_day: str = ""
        self._last_intraday_time: float = 0.0
        psutil.cpu_percent(interval=None)  # 建立 CPU 采样基线，快照用非阻塞采样

    # ----------------------------------------------------------------------
    # write_log 重写：日志写文件 + 错误级日志推手机
    # ----------------------------------------------------------------------
    def write_log(self, msg: str, strategy: CtaTemplate = None) -> None:
        """重写：写日志 + 推送失败/错误级日志到手机"""
        super().write_log(msg, strategy)
        monitor = getattr(self, "monitor", None)
        if monitor:
            level = "ERROR" if ("失败" in msg or "错误" in msg) else "INFO"
            monitor.write(
                MonitorLog(
                    ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    level=level,
                    strategy=strategy.strategy_name if strategy else "",
                    account=getattr(self, "account", "real"),
                    message=msg,
                )
            )
        if "失败" in msg or "错误" in msg:
            self.notify.send_text(f"⚠️ CTA 异常\n  {msg.strip()}")

    @staticmethod
    def _lots_avg(lots) -> float:
        """剩余批次加权均价，空则 0"""
        if not lots:
            return 0.0
        total_v = sum(b[0] for b in lots)
        if total_v == 0:
            return 0.0
        return sum(b[0] * b[1] for b in lots) / total_v

    def _calc_position_pnl(
        self, vt_symbol: str, direction: str, volume: float, avg_price: float
    ) -> float:
        """浮动盈亏 = (最新价 - 持仓均价) × 手数 × 乘数 × 方向"""
        if not avg_price or not volume:
            return 0.0
        last = self.last_prices.get(vt_symbol)
        if not last:
            return 0.0
        contract = self.main_engine.get_contract(vt_symbol)
        size = contract.size if contract else 0
        if not size:
            return 0.0
        mult = 1 if direction == "long" else -1
        return (last - avg_price) * volume * size * mult

    def _write_position_snapshot(
        self, strategy: CtaTemplate, strategy_name: str
    ) -> None:
        """每次成交后写持仓快照（锁仓策略多空分记，非锁仓走引擎批次）"""
        is_locked = hasattr(strategy, "long_pos") and hasattr(strategy, "short_pos")
        long_pos = getattr(strategy, "long_pos", 0) or 0
        short_pos = getattr(strategy, "short_pos", 0) or 0
        if is_locked:
            long_avg = self._lots_avg(getattr(strategy, "long_lots", None))
            short_avg = self._lots_avg(getattr(strategy, "short_lots", None))
            avg_cost = 0.0
        else:
            long_avg = short_avg = 0.0
            avg_cost = self._lots_avg(self.strategy_lots.get(strategy_name))

        # ── 监控写库：持仓快照 ──
        monitor = getattr(self, "monitor", None)
        if monitor:
            account = getattr(self, "account", "real")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            vt_symbol = strategy.vt_symbol
            td = trading_day(datetime.now())
            if is_locked:
                if long_pos:
                    monitor.write(
                        MonitorPosition(
                            strategy=strategy_name,
                            account=account,
                            vt_symbol=vt_symbol,
                            direction="LONG",
                            volume=long_pos,
                            avg_price=round(long_avg, 2),
                            pnl=round(
                                self._calc_position_pnl(
                                    vt_symbol, "long", long_pos, long_avg
                                ),
                                2,
                            ),
                            trading_day=td,
                            snapshot_time=now,
                        )
                    )
                if short_pos:
                    monitor.write(
                        MonitorPosition(
                            strategy=strategy_name,
                            account=account,
                            vt_symbol=vt_symbol,
                            direction="SHORT",
                            volume=short_pos,
                            avg_price=round(short_avg, 2),
                            pnl=round(
                                self._calc_position_pnl(
                                    vt_symbol, "short", short_pos, short_avg
                                ),
                                2,
                            ),
                            trading_day=td,
                            snapshot_time=now,
                        )
                    )
            elif strategy.pos != 0:
                direction = "long" if strategy.pos > 0 else "short"
                monitor.write(
                    MonitorPosition(
                        strategy=strategy_name,
                        account=account,
                        vt_symbol=vt_symbol,
                        direction=direction.upper(),
                        volume=abs(strategy.pos),
                        avg_price=round(avg_cost, 2),
                        pnl=round(
                            self._calc_position_pnl(
                                vt_symbol, direction, abs(strategy.pos), avg_cost
                            ),
                            2,
                        ),
                        trading_day=td,
                        snapshot_time=now,
                    )
                )

    # ----------------------------------------------------------------------
    # 主力合约获取（读换月复权表，与回测同源）
    # ----------------------------------------------------------------------
    def _get_rollover_table(self) -> RolloverTable:
        """加载/热更新换月表：文件 mtime 变了就重读（daily.py 每天覆盖它）。"""
        path = Path(os.getenv("ROLLOVER_TABLE") or (REPO_ROOT / "data" / "rollover.json"))
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise RuntimeError(f"换月表不可读：{path}（{exc}）") from exc

        if getattr(self, "_table_mtime", None) != mtime:
            table = RolloverTable.load(path)
            self._table = table
            self._table_mtime = mtime
            self.write_log(
                f"换月表已加载：{path}（anchor={table.anchor}，"
                f"data_through={table.data_through}，{len(table.products())} 个品种）"
            )
        return self._table

    def _fetch_main_contract(self, variety: str, retry: int = 0) -> str | None:
        """主力合约：从换月表读取（不再查新浪）。

        表由回测仓 `scripts/daily.py` 每日生成，回测与实盘同源。
        表缺失、过期超限、或没有该品种时返回 None —— 不猜、也不静默退回新浪，
        否则「回测一条线、实盘另一条线」的老问题会回来。
        """
        cached = self._main_contract_cache.get(variety)
        if cached:
            return cached

        try:
            table = self._get_rollover_table()
            segment = table.latest(variety.upper())
        except Exception as exc:
            self.write_log(f"读取换月表失败（{variety}）：{exc}")
            return None

        stale_days = (datetime.now().date() - table.data_through).days
        if stale_days > MAX_TABLE_STALE_DAYS:
            msg = (f"换月表已过期 {stale_days} 天（data_through={table.data_through}），"
                   f"不用于判断 {variety} 主力；请先跑回测仓的 scripts/daily.py")
            self.write_log(msg)
            self.notify.send_text(f"🚨 {msg}")
            return None

        symbol = segment.contract.split(".")[0]
        self._main_contract_cache[variety] = symbol
        self.write_log(
            f"{variety} 主力（读表）：{symbol}（该段起 {segment.day}，"
            f"因子 {segment.factor:.6f}，表更新到 {table.data_through}）"
        )
        return symbol

    # ----------------------------------------------------------------------
    # 换月执行
    # ----------------------------------------------------------------------
    def _execute_rollover(self, strategy: CtaTemplate, new_vt_symbol: str) -> bool:
        """空仓换月：只切合约 + 重建策略状态（持仓换月走 _migrate_position）"""
        strategy_name = strategy.strategy_name
        old_vt_symbol = strategy.vt_symbol
        was_trading = strategy.trading

        # 先验证新合约在 CTP 中存在，不存在则跳过（不碰任何状态）
        contract = self.main_engine.get_contract(new_vt_symbol)
        if not contract:
            self.notify.send_text(
                f"⚠️ 换月失败：找不到新合约 {new_vt_symbol}\n"
                f"  策略: {strategy_name}\n"
                f"  旧合约: {old_vt_symbol}（保持不变）"
            )
            self.write_log(
                f"换月失败：找不到新合约 {new_vt_symbol}，跳过换月", strategy
            )
            return False

        self.stop_strategy(strategy_name)
        self._switch_symbol(strategy, new_vt_symbol, was_trading)
        self.write_log(
            f"策略 [{strategy_name}] 已自动换月（空仓）：{old_vt_symbol} -> {new_vt_symbol}",
            strategy,
        )
        return True

    # 换月后不应带过去的"价格类"变量（旧合约的点位挂到新合约上会立刻触发/永不触发止损）
    _PRICE_VAR_HINTS = ("price", "entry", "stop", "trail", "peak", "trough",
                        "high", "low", "atr", "boll", "kk_", "band")

    @classmethod
    def _is_price_like_var(cls, name: str) -> bool:
        lower = name.lower()
        if lower in ("cumulative_pl", "lots", "pos"):
            return False
        if lower.startswith("ma") and lower[2:].isdigit():   # ma20 / ma60
            return True
        return any(hint in lower for hint in cls._PRICE_VAR_HINTS)

    def _switch_symbol(self, strategy: CtaTemplate, new_vt_symbol: str,
                       was_trading: bool) -> None:
        """切 vt_symbol + 重新 on_init + 恢复变量（价格类丢弃）。"""
        strategy_name = strategy.strategy_name
        old_vt_symbol = strategy.vt_symbol

        self.strategy_setting[strategy_name]["vt_symbol"] = new_vt_symbol
        save_json(self.setting_filename, self.strategy_setting)
        strategy.vt_symbol = new_vt_symbol

        contract = self.main_engine.get_contract(new_vt_symbol)
        if contract:
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
        dropped = 0
        for name in strategy.variables:
            value = data.get(name, None)
            if value is None:
                continue
            if self._is_price_like_var(name):
                dropped += 1           # 旧合约的价格类状态不带过去
                continue
            setattr(strategy, name, value)
        cum_pl = data.get("cumulative_pl")
        if cum_pl:
            self.strategy_cumulative_pl[strategy_name] = cum_pl
        strategy.inited = True

        if was_trading:
            self.start_strategy(strategy_name)
        if dropped:
            self.write_log(
                f"换月恢复变量：丢弃 {dropped} 个价格类变量（旧合约点位不适用于新合约）",
                strategy,
            )

    # ----------------------------------------------------------------------
    # 持仓换月移仓
    # ----------------------------------------------------------------------
    def _ctp_position(self, vt_symbol: str) -> tuple[float, float, float, float]:
        """(多头量, 空头量, 多头昨仓, 空头昨仓) —— 以 CTP 持仓为准。"""
        long_vol = short_vol = long_yd = short_yd = 0.0
        for pos in self.main_engine.get_all_positions():
            if pos.vt_symbol != vt_symbol:
                continue
            if pos.direction == Direction.LONG:
                long_vol += pos.volume
                long_yd += pos.yd_volume
            else:
                short_vol += pos.volume
                short_yd += pos.yd_volume
        return long_vol, short_vol, long_yd, short_yd

    def _aggressive_price(self, vt_symbol: str, direction: Direction,
                          ticks: int | None = None) -> float | None:
        """超价委托：买 = 卖一 + N×tick，卖 = 买一 − N×tick，夹在涨跌停内。

        移仓要的是"一定成交"，不是"占便宜"——超 2~3 跳基本能吃透盘口，
        代价是滑价，所以每次都把对手价和委托价记进日志，事后能算清成本。
        """
        ticks = self.rollover_slippage_ticks if ticks is None else ticks
        tick = self.main_engine.get_tick(vt_symbol)
        contract = self.main_engine.get_contract(vt_symbol)
        if tick is None or contract is None:
            return None

        pricetick = contract.pricetick or 0
        if direction == Direction.LONG:
            base = tick.ask_price_1 or tick.last_price
            if not base:
                return None
            price = base + ticks * pricetick
            if tick.limit_up:
                price = min(price, tick.limit_up)     # 不能超涨停，否则是废单
        else:
            base = tick.bid_price_1 or tick.last_price
            if not base:
                return None
            price = base - ticks * pricetick
            if tick.limit_down:
                price = max(price, tick.limit_down)

        self.write_log(
            f"移仓超价 {ticks} 跳：{vt_symbol} {direction.value} "
            f"对手价 {base:.3f} → 委托 {price:.3f}"
        )
        return price

    @staticmethod
    def _close_plan(exchange: str, qty: float, yd: float) -> list[tuple[Offset, float]]:
        """平仓 offset 规划：上期/能源中心要分平昨/平今，其余净持仓交易所用 CLOSE。"""
        if exchange.upper() not in ("SHFE", "INE"):
            return [(Offset.CLOSE, qty)]
        plan: list[tuple[Offset, float]] = []
        yest = min(yd, qty)
        if yest > 0:
            plan.append((Offset.CLOSEYESTERDAY, yest))
        if qty - yest > 0:
            plan.append((Offset.CLOSETODAY, qty - yest))
        return plan

    def _send_manual(self, vt_symbol: str, direction: Direction, offset: Offset,
                     price: float, volume: float) -> str | None:
        """绕过策略直接下单（移仓专用：不经过 strategy.vt_symbol / 冻结检查）。"""
        contract = self.main_engine.get_contract(vt_symbol)
        if contract is None:
            self.write_log(f"移仓下单失败：CTP 找不到合约 {vt_symbol}")
            return None
        req = OrderRequest(
            symbol=contract.symbol, exchange=contract.exchange,
            direction=direction, type=OrderType.LIMIT, volume=volume,
            price=price, offset=offset, reference="rollover",
        )
        vt_orderid = self.main_engine.send_order(req, contract.gateway_name)
        self.order_send_times[vt_orderid] = datetime.now()
        return vt_orderid

    def _wait_traded(self, order_ids: list[str], expect: float,
                     timeout: float = 3.0) -> bool:
        """等这些订单成交到 expect 手（超时返回 False）。"""
        if not order_ids:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            traded = 0.0
            finished = True
            for vid in order_ids:
                order = self.main_engine.get_order(vid)
                if order is None:
                    finished = False
                    continue
                traded += order.traded
                if order.status not in (Status.ALLTRADED, Status.CANCELLED, Status.REJECTED):
                    finished = False
            if traded >= expect and finished:
                return True
            time.sleep(0.2)
        return False

    def _migrate_position(self, strategy: CtaTemplate, new_vt_symbol: str) -> None:
        """启动移仓：在工作线程里做，事件线程必须保持空闲。

        vnpy 的 EventEngine 是单线程跑回调的，`process_order_event` 和
        `process_timer_event` 排在同一队列。如果在回调里 while+sleep 等成交，
        成交回报永远进不来，`get_order()` 不会更新 → 每腿必然超时。
        所以这里只负责起线程 + 上锁，具体流程在 `_migrate_position_worker`。
        """
        name = strategy.strategy_name
        if name in self._migrating:
            return
        self._migrating.add(name)          # 先上锁，避免下一次 timer 再起一个线程
        thread = threading.Thread(
            target=self._migrate_position_worker,
            args=(strategy, new_vt_symbol),
            name=f"rollover-{name}",
            daemon=True,
        )
        thread.start()

    def _migrate_position_worker(self, strategy: CtaTemplate, new_vt_symbol: str) -> bool:
        """持仓换月：冻结 → 撤单 → 开新 → 平旧 → 切状态，每一步都通知。

        失败即回滚：把已经开到新合约的仓位再平掉，老合约保持不动。
        由 `_migrate_position` 在工作线程里调用（`_migrating` 已加锁）。
        """
        name = strategy.strategy_name
        old_vt_symbol = strategy.vt_symbol
        was_trading = strategy.trading

        long_vol, short_vol, long_yd, short_yd = self._ctp_position(old_vt_symbol)
        if long_vol and short_vol:
            self.notify.send_text(
                f"⚠️ 换月移仓跳过：{name} 在 {old_vt_symbol} 上多空双向持仓"
                f"（多 {long_vol:g} / 空 {short_vol:g}），请手工处理"
            )
            self._migrating.discard(name)
            return False

        qty = long_vol or short_vol
        if not qty:
            self.notify.send_text(
                f"ℹ️ {name} 在 {old_vt_symbol} 上没有 CTP 持仓，按空仓换月处理"
            )
            ok = self._execute_rollover(strategy, new_vt_symbol)
            self._migrating.discard(name)
            return ok

        if self.main_engine.get_contract(new_vt_symbol) is None:
            self.notify.send_text(
                f"⚠️ 换月移仓失败：CTP 找不到新合约 {new_vt_symbol}\n"
                f"  策略: {name}  旧合约: {old_vt_symbol}（保持不变）"
            )
            self._rollover_failed_until[name] = time.time() + 300
            self._migrating.discard(name)
            return False

        is_long = long_vol > 0
        yd = long_yd if is_long else short_yd
        open_dir = Direction.LONG if is_long else Direction.SHORT
        close_dir = Direction.SHORT if is_long else Direction.LONG
        side = "多" if is_long else "空"

        self._migrating.add(name)
        self.notify.send_text(
            f"🔒 换月移仓开始（策略已冻结）\n"
            f"  策略: {name}\n"
            f"  旧: {old_vt_symbol}  {side} {qty:g} 手\n"
            f"  新: {new_vt_symbol}"
        )
        self.write_log(
            f"换月移仓开始：{old_vt_symbol} -> {new_vt_symbol}（{side} {qty:g} 手）", strategy
        )

        opened: list[tuple[str, float]] = []      # (vt_orderid, volume) 已在新合约开的仓
        closed_old = 0.0                          # 老合约实际已平掉的手数
        try:
            # 1) 撤掉全部挂单
            self.cancel_all(name)
            self.notify.send_text(f"🧹 {name}：已撤销 {old_vt_symbol} 上的全部挂单")

            # 2) 在新合约开仓（对手价 = 市价效果）
            price = self._aggressive_price(new_vt_symbol, open_dir)
            if not price:
                raise RuntimeError(f"{new_vt_symbol} 没有行情，无法移仓")
            vid = self._send_manual(new_vt_symbol, open_dir, Offset.OPEN, price, qty)
            if not vid or not self._wait_traded([vid], qty):
                raise RuntimeError(f"新合约开仓未成交（{new_vt_symbol} {qty:g} 手 @ {price}）")
            opened.append((vid, qty))
            self.notify.send_text(
                f"🟢 {name}：新合约已开 {qty:g} 手 @ {price}（{new_vt_symbol}）"
            )

            # 3) 平掉老合约（按交易所规则拆平昨/平今）
            exchange = old_vt_symbol.rsplit(".", 1)[-1]
            for offset, vol in self._close_plan(exchange, qty, yd):
                old_price = self._aggressive_price(old_vt_symbol, close_dir)
                if not old_price:
                    raise RuntimeError(f"{old_vt_symbol} 没有行情，无法平旧仓")
                vid2 = self._send_manual(old_vt_symbol, close_dir, offset, old_price, vol)
                if not vid2 or not self._wait_traded([vid2], vol):
                    raise RuntimeError(
                        f"平旧仓未成交（{old_vt_symbol} {offset.value} {vol:g} 手 @ {old_price}）"
                    )
                closed_old += vol
                self.notify.send_text(
                    f"🔵 {name}：旧合约已平 {vol:g} 手（{offset.value}）@ {old_price}"
                )

        except Exception as exc:
            self.write_log(f"换月移仓失败：{exc}", strategy)

            if closed_old > 0:
                # 阶段 B：老仓已经平掉一部分。此时回滚新仓会制造反向敞口，
                # 所以只往前走——继续把老仓平完，平完即视为移仓成功。
                remaining = qty - closed_old
                self.notify.send_text(
                    f"⚠️ {name}：平旧仓中断\n  {exc}\n"
                    f"  老仓已平 {closed_old:g} 手，还剩 {remaining:g} 手，继续补平…"
                )
                for attempt in range(1, 4):
                    old_price = self._aggressive_price(old_vt_symbol, close_dir)
                    if not old_price:
                        break
                    vid = self._send_manual(
                        old_vt_symbol, close_dir, Offset.CLOSE, old_price, remaining)
                    if vid and self._wait_traded([vid], remaining):
                        closed_old += remaining
                        remaining = 0.0
                        break
                    self.write_log(
                        f"第 {attempt} 次补平未成交，剩 {remaining:g} 手", strategy
                    )

                if remaining > 0:
                    # 半完成：两边都有仓，必须人工接管
                    old_vol, _, _, _ = self._ctp_position(old_vt_symbol)
                    new_vol, _, _, _ = self._ctp_position(new_vt_symbol)
                    self.notify.send_text(
                        f"🚨 换月移仓失败，需要人工接管\n"
                        f"  策略: {name}（已停止、已冻结）\n"
                        f"  旧: {old_vt_symbol}  CTP {old_vol:g} 手\n"
                        f"  新: {new_vt_symbol}  CTP {new_vol:g} 手\n"
                        f"  ⚠️ 策略账内 {strategy.pos:+g} 手，与实际不一致\n"
                        f"  老仓还剩 {remaining:g} 手没平掉，请手工处理后再恢复策略"
                    )
                    try:
                        self.stop_strategy(name)
                    except Exception:
                        pass
                    self._rollover_failed_until[name] = time.time() + 3600
                    self._migrating.discard(name)
                    return False
                # 补平完成 → 落到下面的成功收尾

            else:
                # 阶段 A：老仓一手没动，可以安全回滚新仓
                for vid, vol in opened:
                    order = self.main_engine.get_order(vid)
                    filled = order.traded if order else 0
                    if filled <= 0:
                        continue
                    tick_price = self._aggressive_price(new_vt_symbol, close_dir)
                    if not tick_price:
                        self.notify.send_text(
                            f"⚠️ 回滚失败：{new_vt_symbol} 没有行情，请手工平掉 {filled:g} 手"
                        )
                        break
                    # 刚开的仓是"今仓"：SHFE/INE 必须平今，净持仓交易所用平仓
                    new_exchange = new_vt_symbol.rsplit(".", 1)[-1]
                    rb_offset = self._close_plan(new_exchange, filled, 0.0)[0][0]
                    rollback_id = self._send_manual(
                        new_vt_symbol, close_dir, rb_offset, tick_price, filled)
                    if rollback_id and self._wait_traded([rollback_id], filled):
                        self.notify.send_text(
                            f"↩️ 已回滚：{new_vt_symbol} 平掉 {filled:g} 手，老合约保持不动"
                        )
                    else:
                        self.notify.send_text(
                            f"🚨 回滚未完成，需人工接管\n  {new_vt_symbol} 可能还有 "
                            f"{filled:g} 手（策略账外），请立即检查"
                        )
                # 失败后 5 分钟内不再重试，避免刷屏和坏行情里反复下单
                self._rollover_failed_until[name] = time.time() + 300
                self._migrating.discard(name)
                return False

        # 4) 成功：只投递切换请求，由事件线程执行 stop/switch/start
        #    （策略对象只能被事件线程碰；这里继续冻结，直到切换完成）
        self._pending_switch = {
            "name": name, "new": new_vt_symbol, "old": old_vt_symbol,
            "qty": qty, "side": side, "was_trading": was_trading,
        }
        self.write_log(
            f"移仓成交完成，等待切换合约：{old_vt_symbol} -> {new_vt_symbol}（{side} {qty:g} 手）",
            strategy,
        )
        return True

    def _has_position(self, strategy: CtaTemplate) -> bool:
        """判断策略是否实际有持仓（含锁仓：净持仓为0但多空单边非零）"""
        if strategy.pos != 0:
            return True
        long_pos = getattr(strategy, "long_pos", 0) or 0
        short_pos = getattr(strategy, "short_pos", 0) or 0
        return long_pos != 0 or short_pos != 0

    def _check_and_notify_rollover(self, strategy: CtaTemplate) -> None:

        try:
            symbol, exchange = strategy.vt_symbol.split(".")
            variety = "".join(c for c in symbol if not c.isdigit())
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

        self.write_log(
            f"策略 [{strategy.strategy_name}] 检测到主力合约变化：{strategy.vt_symbol} -> {new_vt_symbol}"
        )

        if not self._has_position(strategy):
            old_vt_symbol = strategy.vt_symbol
            if self._execute_rollover(strategy, new_vt_symbol):
                self.notify.send_text(
                    f"🔄 换月完成（空仓）\n"
                    f"  策略: {strategy.strategy_name}\n"
                    f"  旧合约: {old_vt_symbol} → {new_vt_symbol}"
                )
        else:
            # 有持仓：冻结 + 撤单 + 开新 + 平旧（失败自动回滚）
            self._migrate_position(strategy, new_vt_symbol)

    # ----------------------------------------------------------------------
    # 定时换月检查
    # ----------------------------------------------------------------------
    @staticmethod
    def _in_rollover_window(now: datetime | None = None) -> bool:
        """移仓要真实成交，只在有连续交易的时段做（避开收盘/开盘瞬间）。"""
        t = (now or datetime.now()).time()
        hm = (t.hour, t.minute)
        return (
            (9, 5) <= hm <= (14, 45)
            or (21, 5) <= hm <= (22, 55)
            or (0, 5) <= hm <= (2, 20)
        )

    def _apply_pending_switch(self) -> None:
        """在**事件线程**里执行移仓后的切合约 + 解冻（由定时器调用）。

        移仓线程只负责下单和投递，绝不直接碰策略对象——vnpy 的
        `stop/on_init/start` 和 `on_bar` 必须在同一条线程上串行。
        """
        pending = getattr(self, "_pending_switch", None)
        if not pending:
            return
        self._pending_switch = None

        name = pending["name"]
        new_vt_symbol = pending["new"]
        old_vt_symbol = pending["old"]
        strategy = self.strategies.get(name)

        try:
            if strategy is None:
                raise RuntimeError(f"策略 {name} 已不存在")
            self.stop_strategy(name)
            self._switch_symbol(strategy, new_vt_symbol, pending["was_trading"])

            long_vol, short_vol, _, _ = self._ctp_position(new_vt_symbol)
            volume = long_vol or short_vol
            if volume:
                strategy.pos = volume if long_vol >= short_vol else -volume

            variety = _variety_of(new_vt_symbol)
            if variety:
                self._main_contract_cache[variety.upper()] = new_vt_symbol.rsplit(".", 1)[0]

            self.write_log(
                f"换月移仓完成：{old_vt_symbol} -> {new_vt_symbol}"
                f"（{pending['side']} {pending['qty']:g} 手）",
                strategy,
            )
            self.notify.send_text(
                f"✅ 换月移仓完成\n"
                f"  策略: {name}\n"
                f"  {old_vt_symbol} {pending['side']} {pending['qty']:g} 手 → "
                f"{new_vt_symbol} {pending['side']} {pending['qty']:g} 手\n"
                f"  已解冻，策略恢复运行"
            )
        except Exception as exc:
            self.write_log(f"移仓后切换合约失败：{exc}", strategy)
            self.notify.send_text(
                f"⚠️ 移仓已成交但切换合约失败\n  策略: {name}\n  {exc}\n"
                f"  当前: {new_vt_symbol}（请手工检查后重启策略）"
            )
        finally:
            self._migrating.discard(name)
            self._last_rollover_check = time.time()

    def _check_rollovers_periodically(self) -> None:
        """定时把所有在跑的策略过一遍（有持仓也会移仓，不再等空仓）。"""
        for name, strategy in list(self.strategies.items()):
            if name in self._migrating:
                continue
            if not strategy.inited or not strategy.trading:
                continue
            if time.time() < self._rollover_failed_until.get(name, 0.0):
                continue
            try:
                self._check_and_notify_rollover(strategy)
            except Exception as exc:
                msg = f"换月检查失败（{name}）：{exc}"
                self.write_log(msg, strategy)
                self.notify.send_text(f"⚠️ {msg}")

    def _check_table_freshness(self) -> None:
        """收盘后检查换月表是否已更新到当天，没更新就提醒一次（每天一条）。

        节假日不会误报：要求"今天确实有过市场活动"（CTP 有 tick/account 事件），
        周末直接跳过。
        """
        now = datetime.now()
        if now.weekday() >= 5:                       # 周末不提醒
            return
        if (now.hour, now.minute) < self.rollover_table_remind_at:
            return
        if getattr(self, "_table_reminded_day", None) == now.date():   # 今天已经提醒过
            return
        # 今天没有市场活动（节假日 / 未登录）→ 不提醒
        if datetime.fromtimestamp(self.last_activity_time).date() != now.date():
            return

        try:
            table = self._get_rollover_table()
        except Exception as exc:
            self.write_log(f"换月表检查失败：{exc}")
            self.notify.send_text(f"⚠️ 换月表检查失败：{exc}")
            self._table_reminded_day = now.date()
            return

        if table.data_through >= now.date():         # 已更新到今天
            return

        self._table_reminded_day = now.date()
        lag = (now.date() - table.data_through).days
        self.write_log(
            f"换月表未更新：data_through={table.data_through}，落后 {lag} 天",
            None,
        )
        self.notify.send_text(
            f"📅 换月表待更新\n"
            f"  当前 data_through: {table.data_through}（落后 {lag} 天）\n"
            f"  今天: {now.date()}\n"
            f"  请跑: cd vnpy_backtester && python scripts\\daily.py\n"
            f"  超过 {MAX_TABLE_STALE_DAYS} 天将拒绝换月（策略继续跑在老合约上）"
        )

    # ----------------------------------------------------------------------
    # CTP 连接心跳
    # ----------------------------------------------------------------------
    def _on_account_event(self, event: Event) -> None:
        """账户事件 → 连接心跳（收到 account 说明 CTP 还在线）"""
        self.last_activity_time = time.time()
        data = event.data
        self._accountid = data.accountid
        self._last_balance = data.balance
        self._last_available = getattr(data, "available", data.balance - data.frozen)
        self._last_frozen = data.frozen
        if self.disconnect_alerted:
            self.disconnect_alerted = False
            self.notify.send_text(
                f"🟢 CTP 连接已恢复 ({datetime.now().strftime('%H:%M:%S')})"
            )
            self.write_log("CTP 连接已恢复")

    def _factor_of(self, vt_symbol: str) -> float:
        """当前合约在换月表里的因子；不在主力段（或不调整）时返回 1.0。"""
        if not getattr(self, "adjust_strategy_prices", False):
            return 1.0
        variety = _variety_of(vt_symbol)
        contract = vt_symbol.rsplit(".", 1)[0].upper()
        if not variety:
            return 1.0
        try:
            segment = self._get_rollover_table().latest(variety)
        except Exception:
            return 1.0
        if segment.contract.split(".")[0].upper() != contract:
            return 1.0
        return segment.factor or 1.0

    def _adjust_tick(self, tick):
        """返回一个价格字段已乘上因子的 tick 副本（指标用复权价）。"""
        factor = self._factor_of(tick.vt_symbol)
        if factor == 1.0:
            return tick
        adjusted = copy.copy(tick)
        for name in ("last_price", "open_price", "high_price", "low_price",
                     "pre_close", "limit_up", "limit_down",
                     "bid_price_1", "bid_price_2", "bid_price_3", "bid_price_4", "bid_price_5",
                     "ask_price_1", "ask_price_2", "ask_price_3", "ask_price_4", "ask_price_5"):
            value = getattr(adjusted, name, None)
            if value:
                setattr(adjusted, name, value * factor)
        return adjusted

    def _adjust_trade(self, trade):
        """成交回报换成复权价（给策略用）。engine 自己的统计仍用原对象。"""
        factor = self._factor_of(trade.vt_symbol)
        if factor == 1.0:
            return trade
        adjusted = copy.copy(trade)
        adjusted.price = trade.price * factor
        return adjusted

    def _adjust_order(self, order):
        """委托回报换成复权价（给策略用）。"""
        factor = self._factor_of(order.vt_symbol)
        if factor == 1.0:
            return order
        adjusted = copy.copy(order)
        if order.price:
            adjusted.price = order.price * factor
        return adjusted

    def process_tick_event(self, event: Event) -> None:
        """重写：①指标用复权价（可选）②缓存最新价（快照用）。

        给策略的 tick 是复权价，策略算出来的指标/信号就不会在换月时断层；
        下单时 `send_order` 再把价格除回真实价（双尺子）。
        """
        tick = event.data
        if getattr(self, "adjust_strategy_prices", False):
            adjusted = self._adjust_tick(tick)
            if adjusted is not tick:
                event = Event(event.type, adjusted)
        super().process_tick_event(event)
        if tick.last_price:
            self.last_prices[normalize_vt_symbol(tick.vt_symbol)] = tick.last_price

    def _snapshot_realtime(self) -> None:
        """定时快照：策略状态 + 账户持仓/资金（保证金/浮动盈亏用 CTP 持仓）"""
        monitor = getattr(self, "monitor", None)
        if not monitor:
            return
        account = getattr(self, "account", "real")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        intraday = time.time() - self._last_intraday_time >= 900
        if intraday:
            self._last_intraday_time = time.time()
        for name, strategy in self.strategies.items():
            last = self.last_prices.get(strategy.vt_symbol)
            contract = self.main_engine.get_contract(strategy.vt_symbol)
            size = contract.size if contract else 0
            avg_price = self._lots_avg(self.strategy_lots.get(name))
            pnl = 0.0
            if strategy.pos and last is not None and avg_price and size:
                pnl = round((last - avg_price) * strategy.pos * size, 2)
            monitor.write(
                MonitorStrategyStatus(
                    strategy=name,
                    account=account,
                    symbol=strategy.vt_symbol,
                    status="running" if strategy.trading else "stopped",
                    pos=strategy.pos,
                    long_pos=getattr(strategy, "long_pos", 0) or 0,
                    short_pos=getattr(strategy, "short_pos", 0) or 0,
                    pnl=pnl,
                    parameters={p: getattr(strategy, p) for p in strategy.parameters},
                    variables={
                        v: getattr(strategy, v) for v in strategy.variables if hasattr(strategy, v)
                    },
                    update_time=now,
                )
            )
            if intraday:
                realized = self.strategy_cumulative_pl.get(name, 0.0)
                monitor.write(
                    MonitorStrategyIntraday(
                        strategy=name,
                        account=account,
                        symbol=strategy.vt_symbol,
                        realized_pl=round(realized, 2),
                        floating_pl=round(pnl, 2),
                        equity=round(realized + pnl, 2),
                        snapshot_time=now,
                    )
                )

        # 系统指标（3s 刷新，cpu 非阻塞采样）
        uptime_seconds = int(time.time() - psutil.boot_time())
        monitor.write(
            MonitorSystemMetric(
                ts=now,
                cpu=psutil.cpu_percent(interval=None),
                mem=psutil.virtual_memory().percent,
                disk=psutil.disk_usage("/").percent,
                uptime=f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m",
            )
        )

        self._write_account_positions()

    def _write_account_positions(self) -> None:
        """Write CTP account-level real positions + intraday P&L (separate
        from strategy positions in the positions table)."""
        monitor = getattr(self, "monitor", None)
        if not monitor:
            return
        account = getattr(self, "account", "real")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        floating_pl = 0.0
        total_margin = 0.0
        for pos in self.main_engine.get_all_positions():
            floating_pl += pos.pnl
            if not pos.volume:
                continue
            # 净持仓交易所（DCE/CZCE/GFEX）CTP 返回 direction=NET、volume 可负，
            # 拆成多/空让前端能显示方向与正手数
            if pos.direction == Direction.NET:
                direction = "LONG" if pos.volume > 0 else "SHORT"
                volume = abs(pos.volume)
            else:
                direction = pos.direction.name
                volume = pos.volume
            contract = self.main_engine.get_contract(pos.vt_symbol)
            size = contract.size if contract else 0
            total_margin += volume * pos.price * size * 0.08
            monitor.write(
                MonitorAccountPosition(
                    account=account,
                    accountid=self._accountid,
                    vt_symbol=pos.vt_symbol,
                    direction=direction,
                    volume=volume,
                    price=round(pos.price, 2),
                    pnl=round(pos.pnl, 2),
                    snapshot_time=now,
                )
            )
        today = trading_day(datetime.now())
        if self._today_trading_day != today:
            self._today_trading_day = today
            self._today_realized_pl = 0.0
        monitor.write(
            MonitorAccountIntradayPnl(
                account=account,
                accountid=self._accountid,
                trading_day=today,
                realized_pl=round(self._today_realized_pl, 2),
                floating_pl=round(floating_pl, 2),
                balance=self._last_balance,
                snapshot_time=now,
            )
        )
        monitor.write(
            MonitorAccount(
                account=account,
                accountid=self._accountid,
                balance=self._last_balance,
                available=self._last_available,
                frozen=self._last_frozen,
                margin=round(total_margin, 2),
                pnl=round(floating_pl, 2),
                snapshot_time=now,
            )
        )

    # ----------------------------------------------------------------------
    # 发单/撤单打点（override 基类，记录指令发出时刻用于延迟监控）
    def send_order(
        self, strategy, direction, offset, price, volume, stop, lock, net
    ) -> list:
        # 正在换月移仓：策略自己的下单全部丢弃（迁移单走 _migration_ctx 旁路）
        if getattr(strategy, "strategy_name", "") in self._migrating and not self._migration_ctx:
            self.write_log(
                f"策略 [{strategy.strategy_name}] 正在换月移仓，忽略下单 "
                f"{direction.value} {offset.value} {volume}@{price}",
                strategy,
            )
            return []
        # 双尺子：策略在复权价空间算指标/挂价，下单前换回真实价并落到 tick 网格
        factor = self._factor_of(strategy.vt_symbol)
        if factor != 1.0 and price:
            contract = self.main_engine.get_contract(strategy.vt_symbol)
            tick_size = getattr(contract, "pricetick", 0) or 0
            if tick_size:
                price = round(price / factor / tick_size) * tick_size
        send_time = datetime.now()
        vt_orderids = super().send_order(
            strategy, direction, offset, price, volume, stop, lock, net
        )
        for vid in vt_orderids or []:
            self.order_send_times[vid] = send_time
        return vt_orderids

    def cancel_order(self, strategy, vt_orderid: str) -> None:
        self.order_cancel_times[vt_orderid] = datetime.now()
        super().cancel_order(strategy, vt_orderid)

    # 订单 / 成交事件（通知 + 监控写库）
    # ----------------------------------------------------------------------
    def process_order_event(self, event: Event) -> None:
        # 双尺子：交给基类（会转给策略 on_order）的是复权价副本，
        # 下面 engine 自己的滑点/通知统计仍用原始真实价对象
        real_order: OrderData = event.data
        if getattr(self, "adjust_strategy_prices", False):
            adjusted = self._adjust_order(real_order)
            if adjusted is not real_order:
                event = Event(event.type, adjusted)
        super().process_order_event(event)
        self.last_activity_time = time.time()

        order: OrderData = real_order
        # 缓存订单用于滑点计算
        self.orders[order.vt_orderid] = order

        strategy = self.orderid_strategy_map.get(order.vt_orderid, None)
        if not strategy:
            return

        strategy_name = strategy.strategy_name

        # ── 每日订单状态统计（每单只记最新状态，见 get_daily_order_stats）──
        self._daily_order_status[order.vt_orderid] = order.status.value

        # ── 延迟：报单→入队 / 撤单→撤单终态（首次到达对应状态时计算并缓存） ──
        # 入队延迟 = 发单 → 网关首次回报。立即成交的单直接 ALLTRADED、不经过
        # NOTTRADED，故以"非 SUBMITTING"作为入队标志，而非 NOTTRADED。
        if order.vt_orderid not in self.order_queue_delay and order.status != Status.SUBMITTING:
            send_time = self.order_send_times.get(order.vt_orderid)
            self.order_queue_delay[order.vt_orderid] = (
                round((datetime.now() - send_time).total_seconds() * 1000, 0)
                if send_time
                else 0.0
            )
        if order.status == Status.CANCELLED and order.vt_orderid not in self.order_cancel_delay:
            cancel_time = self.order_cancel_times.get(order.vt_orderid)
            self.order_cancel_delay[order.vt_orderid] = (
                round((datetime.now() - cancel_time).total_seconds() * 1000, 0)
                if cancel_time
                else 0.0
            )

        # ── 监控写库：订单 ──
        monitor = getattr(self, "monitor", None)
        if monitor:
            monitor.write(
                MonitorOrder(
                    vt_orderid=order.vt_orderid,
                    strategy=strategy_name,
                    account=getattr(self, "account", "real"),
                    symbol=order.symbol,
                    exchange=order.exchange.value,
                    direction=order.direction.name,
                    offset=order.offset.name,
                    price=order.price,
                    volume=order.volume,
                    traded=order.traded,
                    status=order.status.name,
                    submit_time=str(order.datetime),
                    queue_delay_ms=self.order_queue_delay.get(order.vt_orderid, 0.0),
                    cancel_delay_ms=self.order_cancel_delay.get(order.vt_orderid, 0.0),
                )
            )

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
            self.write_log(
                f"订单被拒 {strategy_name} {order.vt_symbol} price={order.price}"
            )
        elif order.status == Status.NOTTRADED:
            if order.vt_orderid not in self._notified_nottraded:
                self._notified_nottraded.add(order.vt_orderid)
                label = (
                    "平仓单"
                    if order.offset
                    in (Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY)
                    else "开仓单"
                )
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
                        est_pl = (
                            (order.price - avg_price) * order.volume * direction_mult
                        )
                        contract = self.main_engine.get_contract(order.vt_symbol)
                        size = contract.size if contract else 0
                        notify_text += f"\n  预估盈亏: {est_pl * size:+.2f} 元"
                self.notify.send_text(notify_text)
        elif order.status == Status.CANCELLED:
            self.notify.send_text(
                f"🟡 订单已撤销\n"
                f"  策略: {strategy_name}\n"
                f"  合约: {order.symbol}\n"
                f"  方向: {order.direction.value} {order.offset.value}\n"
                f"  价格: {order.price:.2f}  数量: {order.volume}"
            )
            self.write_log(
                f"订单撤销 {strategy_name} {order.vt_symbol} price={order.price}"
            )
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

        # ── P&L 追踪（批次队列按交易所规则匹配；锁仓策略多空分记跳过由策略自算） ──
        pl = 0.0
        pl_amount = 0.0
        is_locked = hasattr(strategy, "long_pos") and hasattr(strategy, "short_pos")
        if not is_locked:
            if trade.offset == Offset.OPEN:
                lots = self.strategy_lots.setdefault(strategy_name, [])
                lots.append(
                    [
                        trade.volume,
                        trade.price,
                        trading_day(trade.datetime),
                        trade.datetime,
                    ]
                )
            else:
                lots = self.strategy_lots.get(strategy_name)
                if lots:
                    direction_mult = 1 if trade.direction == Direction.SHORT else -1
                    today = trading_day(trade.datetime)
                    # 逐日盯市：隔日仓平仓成本基准 = CTP 持仓成本价(对隔日仓=昨结算价)
                    prev_settle = None
                    hold_direction = (
                        Direction.LONG if direction_mult == 1 else Direction.SHORT
                    )
                    for pos in self.main_engine.get_all_positions():
                        if pos.vt_symbol != trade.vt_symbol:
                            continue
                        if pos.direction in (Direction.NET, hold_direction) and pos.yd_volume > 0:
                            prev_settle = pos.price
                            break
                    pl, details = settle_close(
                        lots,
                        trade.exchange,
                        trade.offset,
                        trade.volume,
                        today,
                        trade.price,
                        direction_mult,
                        prev_settle=prev_settle,
                        with_details=True,
                    )
                    # 点·手 → 元（乘合约乘数 size），与浮动盈亏/账户权益口径对齐
                    contract = self.main_engine.get_contract(trade.vt_symbol)
                    size = contract.size if contract else 0
                    pl_amount = pl * size
                    self.strategy_cumulative_pl[strategy_name] = (
                        self.strategy_cumulative_pl.get(strategy_name, 0) + pl_amount
                    )
                    self._today_realized_pl += pl_amount

                    # ── 监控写库：平仓配对 trade_rounds ──
                    monitor = getattr(self, "monitor", None)
                    if monitor and details:
                        direction_str = "LONG" if direction_mult == 1 else "SHORT"
                        close_dt = trade.datetime
                        if close_dt is not None and close_dt.tzinfo is not None:
                            close_dt = close_dt.astimezone().replace(tzinfo=None)
                        account = getattr(self, "account", "real")
                        for vol, entry_price, _day, pair_pl, open_dt in details:
                            holding = 0.0
                            if open_dt is not None and close_dt is not None:
                                od = open_dt
                                if od.tzinfo is not None:
                                    od = od.astimezone().replace(tzinfo=None)
                                holding = round((close_dt - od).total_seconds(), 0)
                            monitor.write(
                                MonitorTradeRound(
                                    strategy=strategy_name,
                                    symbol=trade.symbol,
                                    exchange=trade.exchange.value,
                                    account=account,
                                    direction=direction_str,
                                    entry_price=round(entry_price, 2),
                                    exit_price=round(trade.price, 2),
                                    volume=vol,
                                    pnl=round(pair_pl * size, 2),
                                    holding_seconds=holding,
                                    open_time=str(_day),
                                    close_time=str(close_dt) if close_dt else "",
                                    trading_day=today,
                                )
                            )

        # ── 本地日志 ──
        if slippage != 0:
            self.write_log(
                f"成交 {strategy_name} {trade.symbol} "
                f"{trade.direction.value} vol={trade.volume} "
                f"price={trade.price:.2f} "
                f"slippage={slippage:+.2f}"
            )

        # 双尺子：策略的 on_bar 是复权价，on_trade 也必须同尺子，否则会算出假盈利
        self.call_strategy_func(strategy, strategy.on_trade, self._adjust_trade(trade))

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
                if pl_amount != 0:
                    notify_text += f"\n  本次盈亏: {pl_amount:+.2f} 元"
                cum_pl = self.strategy_cumulative_pl.get(strategy_name)
                if cum_pl:
                    notify_text += f"\n  累计盈亏: {cum_pl:+.2f} 元"
            self.notify.send_text(notify_text)

        self.sync_strategy_data(strategy)

        # ── 监控写库：成交 ──
        monitor = getattr(self, "monitor", None)
        if monitor:
            monitor.write(
                MonitorTrade(
                    vt_tradeid=trade.vt_tradeid,
                    vt_orderid=trade.vt_orderid,
                    strategy=strategy_name,
                    account=getattr(self, "account", "real"),
                    symbol=trade.symbol,
                    exchange=trade.exchange.value,
                    price=trade.price,
                    volume=trade.volume,
                    slippage=round(slippage, 2),
                    trade_time=str(trade.datetime),
                )
            )

        # ── 持仓快照（每次成交后写库）──
        self._write_position_snapshot(strategy, strategy_name)

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
            self.write_log(
                f"CTP 连接异常告警：{int(current_time - self.last_activity_time)}s 无活动"
            )

        # ── 快照：账户 + 持仓浮动盈亏 ──
        if current_time - self._last_snapshot_time >= self.snapshot_interval:
            self._last_snapshot_time = current_time
            self._snapshot_realtime()

        # ── 定时换月检查（默认每 60s，只在交易时段）──
        if (current_time - self._last_rollover_check >= self.rollover_check_interval
                and self._in_rollover_window()):
            self._last_rollover_check = current_time
            self._check_rollovers_periodically()

        # ── 换月表更新提醒（收盘后每天一次）──
        self._check_table_freshness()

        # ── 消费移仓线程投递的切换请求（必须在事件线程执行）──
        self._apply_pending_switch()

        # ── 策略监控 ──
        if current_time - self.last_monitor_time < self.monitor_interval:
            return
        self.last_monitor_time = current_time

        # ── 监控写库：K线/合约（策略状态 + 系统指标已移至快照）──
        monitor = getattr(self, "monitor", None)
        if monitor:
            for name, s in self.strategies.items():
                self._attach_kline_writer(s)
                self._write_contract(s.vt_symbol)

        messages = []
        for strategy_name, strategy in self.strategies.items():
            if not strategy.inited or not strategy.trading:
                continue

            # 优先使用策略自定义的监控消息
            if hasattr(strategy, "get_monitor_message") and callable(
                strategy.get_monitor_message
            ):
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
            if hasattr(strategy, "bar_count") and not (
                hasattr(strategy, "am") and strategy.am is not None
            ):
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
        """收盘退出前发送当日盈亏汇总（已实现盈亏，元）"""
        lines = []
        total = 0.0
        for name, strategy in self.strategies.items():
            is_locked = hasattr(strategy, "long_pos") and hasattr(strategy, "short_pos")
            if is_locked:
                # 锁仓策略 realized_pl 为策略自算的"点·手"，乘 size 转元
                pl_pts = getattr(strategy, "realized_pl", 0) or 0
                long_pos = getattr(strategy, "long_pos", 0) or 0
                short_pos = getattr(strategy, "short_pos", 0) or 0
                pos_str = f"多{long_pos}/空{short_pos}"
                if pl_pts == 0 and long_pos == 0 and short_pos == 0:
                    continue
                contract = self.main_engine.get_contract(strategy.vt_symbol)
                size = contract.size if contract else 0
                pl_amount = pl_pts * size
            else:
                # 非锁仓 strategy_cumulative_pl 已在平仓累加时乘 size，直接是元
                pl_amount = self.strategy_cumulative_pl.get(name, 0) or 0
                pos_str = f"{strategy.pos:+d}"
                if pl_amount == 0 and strategy.pos == 0:
                    continue

            total += pl_amount
            lines.append(
                f"  {name}  {strategy.vt_symbol}\n"
                f"    已实现: {pl_amount:+.2f} 元 | 持仓: {pos_str}"
            )

        sep = "─" * 30
        if not lines:
            text = f"📊 当日盈亏汇总\n{sep}\n  今日无交易"
        else:
            text = (
                f"📊 当日盈亏汇总\n{sep}\n"
                + "\n\n".join(lines)
                + f"\n{sep}\n  合计已实现: {total:+.2f} 元"
            )
        self.notify.send_text(text)

    def _mark_stale_orders(self) -> None:
        """收盘退出前，把未成交挂单标记为已撤销（交易所收盘后挂单自动失效）"""
        monitor = getattr(self, "monitor", None)
        if not monitor:
            return
        try:
            with monitor.engine.begin() as conn:
                result = conn.exec_driver_sql(
                    "UPDATE orders SET status='CANCELLED' "
                    "WHERE status='SUBMITTING' AND traded=0"
                )
                n = result.rowcount
            if n:
                self.write_log(f"日盘收盘：{n} 个未成交挂单标记为已撤销")
        except Exception as e:
            self.write_log(f"标记挂单失效失败: {e}")

    def reset_daily_pl(self) -> None:
        """每日收盘归零累计盈亏：次日从 0 起统计当日盈亏（隔夜持仓按逐日盯市已结算，不影响）"""
        monitor = getattr(self, "monitor", None)
        if monitor:
            account = getattr(self, "account", "real")
            for name, strategy in self.strategies.items():
                is_locked = hasattr(strategy, "long_pos") and hasattr(
                    strategy, "short_pos"
                )
                if is_locked:
                    realized = getattr(strategy, "realized_pl", 0) or 0
                else:
                    realized = self.strategy_cumulative_pl.get(name, 0) or 0
                monitor.write(
                    MonitorDailyPnl(
                        trading_day=trading_day(datetime.now()),
                        strategy=name,
                        account=account,
                        realized_pl=realized,
                        cumulative_pl=self.strategy_cumulative_pl.get(name, 0) or 0,
                    )
                )
        for name, strategy in self.strategies.items():
            if hasattr(strategy, "realized_pl"):
                strategy.realized_pl = 0.0
            self.strategy_cumulative_pl[name] = 0.0

    def _write_contract(self, vt_symbol: str) -> None:
        """Upsert CTP contract specs (name/product/size/pricetick) into the
        monitor DB so the frontend shows real exchange data instead of a
        hardcoded table."""
        monitor = getattr(self, "monitor", None)
        if not monitor:
            return
        contract = self.main_engine.get_contract(vt_symbol)
        if not contract:
            return
        variety = "".join(ch for ch in vt_symbol.split(".")[0] if ch.isalpha())
        monitor.write_upsert(
            MonitorContract(
                symbol=vt_symbol,
                exchange=contract.exchange.value,
                name=contract.name or "",
                product=variety,
                size=contract.size or 0,
                pricetick=contract.pricetick or 0,
                update_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        )

    def _write_kline(self, bar) -> None:
        """BarGenerator 合成 1min bar 时写 K线"""
        monitor = getattr(self, "monitor", None)
        if monitor:
            monitor.write(
                MonitorKline(
                    symbol=bar.vt_symbol,
                    exchange=bar.exchange.value,
                    account=getattr(self, "account", "real"),
                    interval="1m",
                    datetime=str(bar.datetime),
                    open=bar.open_price,
                    high=bar.high_price,
                    low=bar.low_price,
                    close=bar.close_price,
                    volume=bar.volume,
                )
            )

    def _attach_kline_writer(self, strategy: CtaTemplate) -> None:
        """给策略的 BarGenerator 挂上 K线写库回调（幂等）"""
        bg = getattr(strategy, "bg", None)
        if bg is not None and hasattr(bg, "kline_writer") and bg.kline_writer is None:
            bg.kline_writer = self._write_kline

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
            data["lots"] = [
                [b[0], b[1], b[2], b[3].isoformat() if isinstance(b[3], datetime) else b[3]]
                if len(b) > 3
                else list(b)
                for b in lots
            ]
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
            self.write_log(
                f"策略 [{strategy_name}] 合约代码已规范化：{vt_symbol} -> {fixed}"
            )
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
            restored = []
            for b in lots:
                b = list(b)
                if len(b) > 3 and isinstance(b[3], str):
                    b[3] = datetime.fromisoformat(b[3])
                restored.append(b)
            self.strategy_lots[strategy_name] = restored
        cum_pl = data.get("cumulative_pl")
        if cum_pl:
            self.strategy_cumulative_pl[strategy_name] = cum_pl

    def _check_rollover_for_init(self, strategy: CtaTemplate) -> None:
        """初始化时的换月检查：只记录结果，不发送通知"""
        self.write_log(f"策略 [{strategy.strategy_name}] 开始换月检查（初始化）")
        name = strategy.strategy_name

        try:
            symbol, exchange = strategy.vt_symbol.split(".")
            variety = "".join(c for c in symbol if not c.isdigit())
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

        self.write_log(
            f"策略 [{name}] 检测到主力合约变化：{strategy.vt_symbol} -> {new_vt_symbol}"
        )

        if not self._has_position(strategy):
            old_vt_symbol = strategy.vt_symbol
            if self._execute_rollover(strategy, new_vt_symbol):
                self.write_log(
                    f"策略 [{name}] 初始化时已自动换月：{old_vt_symbol} -> {new_vt_symbol}"
                )
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
        """订单终结后清理缓存，防内存泄漏（全部成交/撤单/拒单时调用）。

        order_queue_delay / order_cancel_delay 保留不清理：ALLTRADED 订单的
        状态行写库可能晚于成交事件清理，入队延迟需跨状态读取（每单仅两个
        float，长期累积内存可忽略）。
        """
        self.order_send_times.pop(vt_orderid, None)
        self.order_cancel_times.pop(vt_orderid, None)
        self.orders.pop(vt_orderid, None)
        self._notified_nottraded.discard(vt_orderid)

    def get_daily_order_stats(self) -> dict:
        """返回每日订单状态统计，跨自然日自动清零（"总计"=唯一订单数）"""
        today = datetime.now().strftime("%Y%m%d")
        if self._order_stat_date != today:
            self._daily_order_status.clear()
            self._order_stat_date = today
        result: dict = {}
        for status in self._daily_order_status.values():
            result[status] = result.get(status, 0) + 1
        result["总计"] = len(self._daily_order_status)
        return result


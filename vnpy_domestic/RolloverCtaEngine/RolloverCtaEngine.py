# rollover_cta_engine.py
import copy
import json
import os
import statistics
import threading
import time
import traceback
import urllib.request
from collections import deque
from datetime import date, datetime
from pathlib import Path

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

from vnpy_domestic.rollover import RolloverTable
from vnpy_domestic.trader.notification_manager import NotificationManager
from vnpy_domestic.trader.position_lots import settle_close, trading_day

# 仓库根（vnpy_domestic/RolloverCtaEngine/RolloverCtaEngine.py → 上两级）
REPO_ROOT = Path(__file__).resolve().parents[2]
# 换月表超过这么多天没更新就拒绝拿它判主力（daily.py 每天会覆盖）
MAX_TABLE_STALE_DAYS = 10

# openctp 合约字典：含 VolumeMultiple / PriceTick / Long/ShortMarginRatioByMoney
OPENCTP_INSTRUMENTS_URL = os.getenv(
    "OPENCTP_INSTRUMENTS_URL",
    "http://dict.openctp.cn/instruments?types=futures",
)
# openctp 保证金率拉不到时的兜底（只影响账户卡估算，不写进 contracts 表）
DEFAULT_MARGIN_RATE = 0.08

from vnpy_domestic.monitor import (
    Account as MonitorAccount,
)
from vnpy_domestic.monitor import (
    AccountIntradayPnl as MonitorAccountIntradayPnl,
)
from vnpy_domestic.monitor import (
    AccountPosition as MonitorAccountPosition,
)
from vnpy_domestic.monitor import (
    Contract as MonitorContract,
)
from vnpy_domestic.monitor import (
    Kline as MonitorKline,
)
from vnpy_domestic.monitor import (
    Log as MonitorLog,
)
from vnpy_domestic.monitor import (
    MonitorWriter,
)
from vnpy_domestic.monitor import (
    Order as MonitorOrder,
)
from vnpy_domestic.monitor import (
    Position as MonitorPosition,
)
from vnpy_domestic.monitor import (
    Settlement as MonitorSettlement,
)
from vnpy_domestic.monitor import (
    StrategyIntraday as MonitorStrategyIntraday,
)
from vnpy_domestic.monitor import (
    StrategyStatus as MonitorStrategyStatus,
)
from vnpy_domestic.monitor import (
    SystemMetric as MonitorSystemMetric,
)
from vnpy_domestic.monitor import (
    Trade as MonitorTrade,
)
from vnpy_domestic.monitor import (
    TradeRound as MonitorTradeRound,
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
        # 通知重入保护：发送失败会写日志，日志又触发通知，递归下去一次失败会
        # 打出几十上百个 HTTP 请求（实测 250 次后才被 RecursionError 打断）。
        self._notify_local = threading.local()

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

        # 主力合约缓存 {(品种, 换月表版本): 合约代码}，进程级，子进程退出自动清空
        self._main_contract_cache: dict[tuple[str, float], str] = {}

        # 换月移仓：冻结中的策略（冻结期间 send_order 直接丢弃）
        self._migrating: set[str] = set()
        # 移仓成交后待执行的"切合约"动作队列（只由事件线程消费）。
        # 多个策略可能在同一轮换月检查里各自起线程，必须排队——单槽位会互相
        # 覆盖，导致除最后一个以外的策略永久冻结（send_order 一直被丢弃）。
        self._pending_switches: deque = deque()
        # 双尺子：策略指标用复权价（tick 乘因子），下单价格再除回真实价
        self.adjust_strategy_prices: bool = \
            os.getenv("ROLLOVER_ADJUST_PRICES", "0") == "1"
        # 每日换月表更新提醒（收盘后，仅交易日；每天只提醒一次）
        self.rollover_table_remind_at: tuple[int, int] = (18, 0)
        self._table_reminded_day: date | None = None
        # 换月检查失败/换月表过期的通知去重：{品种: 已提醒日期}
        self._rollover_fail_notified: dict[str, date] = {}
        # 移仓委托超价跳数（2~3 跳基本能吃透盘口）
        self.rollover_slippage_ticks: int = int(os.getenv("ROLLOVER_SLIPPAGE_TICKS", "3"))
        # 初始化判定出的"待移仓"：{策略名: 新合约}。判定只在启动做一次，
        # 真正下单要等盘中（有行情）；执行完即出队，失败不重试。
        self._pending_rollover: dict[str, str] = {}

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
        # 结算价缓存 {(vt_symbol, 查询日): price}
        self._settle_cache: dict[tuple[str, str], float | None] = {}
        # 保证金率缓存（openctp 合约字典）：vt_symbol -> (多头率, 空头率)
        self._margin_rates: dict[str, tuple[float, float]] = {}
        # 品种兜底：品种代码 -> [(多头率, 空头率), ...]（合约不在字典时取中位数）
        self._margin_product_rates: dict[str, list[tuple[float, float]]] = {}
        self._margin_loaded_at: float = 0.0
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
        notify = getattr(self, "notify", None)
        in_notify_worker = bool(
            notify is not None
            and getattr(notify, "is_worker_thread", None)
            and notify.is_worker_thread()
        )
        if (
            ("失败" in msg or "错误" in msg)
            and not getattr(self._notify_local, "active", False)
            and not in_notify_worker
        ):
            # 同线程里“发送中”产生的失败日志只落库，不再触发下一轮通知；
            # 异步之后真正发送在 worker 线程，那里也要挡（否则“飞书发送失败”
            # 会被自己的日志回调再次入队，形成回环）。
            self._notify_local.active = True
            try:
                self.notify.send_text(f"⚠️ CTA 异常\n  {msg.strip()}")
            finally:
                self._notify_local.active = False

    @staticmethod
    def _holding_seconds(open_dt, close_dt) -> float | None:
        """持仓秒数；两个时间戳的时区可能不同，统一成 naive 本地时间再减。"""
        try:
            od, cd = open_dt, close_dt
            if getattr(od, "tzinfo", None) is not None:
                od = od.astimezone().replace(tzinfo=None)
            if getattr(cd, "tzinfo", None) is not None:
                cd = cd.astimezone().replace(tzinfo=None)
            return (cd - od).total_seconds()
        except Exception:
            return None

    @staticmethod
    def _fmt_holding(secs: float) -> str:
        secs = max(0.0, float(secs))
        if secs < 60:
            return f"{secs:.0f} 秒"
        if secs < 3600:
            return f"{secs / 60:.0f} 分钟"
        if secs < 86400:
            return f"{secs / 3600:.1f} 小时"
        return f"{secs / 86400:.1f} 天"

    @staticmethod
    def _lots_avg(lots) -> float:
        """剩余批次加权均价，空则 0"""
        if not lots:
            return 0.0
        total_v = sum(b[0] for b in lots)
        if total_v == 0:
            return 0.0
        return sum(b[0] * b[1] for b in lots) / total_v

    def _floating_entry(self, strategy: CtaTemplate, avg_price: float) -> float:
        """浮动盈亏（开仓价口径）：持仓展示、策略累计盈亏用，和 trade_rounds 同基准。"""
        last = self.last_prices.get(strategy.vt_symbol)
        if last is None or not strategy.pos or not avg_price:
            return 0.0
        contract = self.main_engine.get_contract(strategy.vt_symbol)
        size = contract.size if contract else 0
        if not size:
            return 0.0
        return (last - avg_price) * strategy.pos * size

    def _floating_mtm(self, strategy: CtaTemplate, today: str) -> float:
        """浮动盈亏（逐日盯市）：隔夜批次按昨结算价，当日批次按开仓价。"""
        last = self.last_prices.get(strategy.vt_symbol)
        lots = self.strategy_lots.get(strategy.strategy_name) or []
        if last is None or not lots or not strategy.pos:
            return 0.0
        contract = self.main_engine.get_contract(strategy.vt_symbol)
        size = contract.size if contract else 0
        if not size:
            return 0.0
        settle = self._settle_of(strategy.vt_symbol, today)
        total = 0.0
        for lot in lots:
            vol, price, day = lot[0], lot[1], lot[2]
            cost = settle if (settle and day < today) else price
            total += (last - cost) * vol * size
        return total if strategy.pos > 0 else -total

    def _write_position_snapshot(
        self, strategy: CtaTemplate, strategy_name: str, force: bool = False
    ) -> None:
        """每次成交后写持仓快照（均价取引擎批次）。

        移仓期间不写：两腿都走正常记账，pos 会瞬时翻倍（旧 3 手 + 新 3 手），
        写进去就是脏行；切完合约由 `_reconcile_after_rollover` 用 force=True 补。
        """
        if not force and strategy_name in self._migrating:
            return
        avg_cost = self._lots_avg(self.strategy_lots.get(strategy_name))

        # ── 监控写库：持仓快照 ──
        monitor = getattr(self, "monitor", None)
        if monitor and strategy.pos != 0:
            direction = "long" if strategy.pos > 0 else "short"
            monitor.write(
                MonitorPosition(
                    strategy=strategy_name,
                    account=getattr(self, "account", "real"),
                    vt_symbol=strategy.vt_symbol,
                    direction=direction.upper(),
                    volume=abs(strategy.pos),
                    avg_price=round(avg_cost, 2),
                    pnl=round(self._floating_entry(strategy, avg_cost), 2),
                    trading_day=trading_day(datetime.now()),
                    snapshot_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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

    def _main_contract_key(self, variety: str) -> tuple[str, float]:
        """主力合约缓存的 key：品种 + 换月表版本（mtime）。

        表每天被回测仓的 scripts/daily.py 覆盖，缓存不带版本就等于"只在启动时
        判一次主力"。
        """
        return (variety.upper(), float(getattr(self, "_table_mtime", 0.0) or 0.0))

    def _fetch_main_contract(self, variety: str, retry: int = 0) -> str | None:
        """主力合约：从换月表读取（不再查新浪）。

        表由回测仓 `scripts/daily.py` 每日生成，回测与实盘同源。
        表缺失、过期超限、或没有该品种时返回 None —— 不猜、也不静默退回新浪，
        否则「回测一条线、实盘另一条线」的老问题会回来。
        """
        try:
            table = self._get_rollover_table()          # 内部按 mtime 热加载
        except Exception as exc:
            self._report_rollover_failure(
                variety, f"读取换月表异常（{variety}）：{exc}", urgent=True
            )
            return None

        # 缓存键必须带换月表版本：daily.py 每天覆盖表，只按品种缓存会让运行期
        # 永远返回启动时读到的合约，60 秒定时检查全部空转（等于不换月）。
        key = self._main_contract_key(variety)
        cached = self._main_contract_cache.get(key)
        if cached:
            return cached

        try:
            segment = table.latest(variety.upper())
        except Exception as exc:
            self._report_rollover_failure(
                variety, f"读取换月表异常（{variety}）：{exc}", urgent=True
            )
            return None

        stale_days = (datetime.now().date() - table.data_through).days
        if stale_days > MAX_TABLE_STALE_DAYS:
            msg = (f"换月表已过期 {stale_days} 天（data_through={table.data_through}），"
                   f"不用于判断 {variety} 主力；请先跑回测仓的 scripts/daily.py")
            self._report_rollover_failure(variety, msg, urgent=True)
            return None

        symbol = segment.contract.split(".")[0]
        self._main_contract_cache[key] = symbol
        self.write_log(
            f"{variety} 主力（读表）：{symbol}（该段起 {segment.day}，"
            f"因子 {segment.factor:.6f}，表更新到 {table.data_through}）"
        )
        return symbol

    def _report_rollover_failure(self, variety: str, msg: str,
                                 urgent: bool = False) -> None:
        """换月检查失败按「品种 + 自然日」去重。

        换月检查每 60 秒一轮、每个策略一遍；表过期/缺失时如果每次都写日志 +
        推飞书，会刷屏并触发飞书限流（而通知失败又会反过来刷日志）。这里每天
        每个品种只报一次；判断逻辑本身仍然每次返回失败（不换月），只是不再
        重复打扰。
        """
        key = variety.upper()
        today = datetime.now().date()
        if self._rollover_fail_notified.get(key) == today:
            return
        self._rollover_fail_notified[key] = today
        self.write_log(msg)
        if urgent:
            self.notify.send_text(f"🚨 {msg}")

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
        """(多头量, 空头量, 多头昨仓, 空头昨仓) —— 以 CTP 持仓为准。

        DCE 等净持仓交易所 CTP 可能返回 Direction.NET、volume 带符号；
        之前把所有非 LONG 都当空头，NET 多头会被算成空头，移仓方向直接反掉。
        """
        long_vol = short_vol = long_yd = short_yd = 0.0
        for pos in self.main_engine.get_all_positions():
            if pos.vt_symbol != vt_symbol:
                continue
            volume = pos.volume or 0
            yd_volume = pos.yd_volume or 0
            if pos.direction == Direction.LONG:
                long_vol += volume
                long_yd += yd_volume
            elif pos.direction == Direction.SHORT:
                short_vol += volume
                short_yd += yd_volume
            elif pos.direction == Direction.NET:
                if volume >= 0:
                    long_vol += volume
                    long_yd += yd_volume
                else:
                    short_vol += abs(volume)
                    short_yd += abs(yd_volume)
        return long_vol, short_vol, long_yd, short_yd

    def _strategy_position(self, name: str) -> tuple[float, float]:
        """本策略自己的 (手数, 昨仓手数) —— 策略级持仓归属的唯一来源。

        CTP 持仓**没有策略标签**：同一合约跑两个策略时，`_ctp_position` 返回的是
        账户总量，拿它决定移仓手数会把别的策略的仓一起搬走。所以移仓手数一律以
        `strategy_lots`（开仓时逐笔记的批次）为准；批次的 trading_day 还能精确
        区分昨仓/今仓，供上期/能源中心拆平昨平今用。
        """
        lots = self.strategy_lots.get(name) or []
        if not lots:
            return 0.0, 0.0
        today = trading_day(datetime.now())
        total = 0.0
        yest = 0.0
        for lot in lots:
            vol = float(lot[0])
            total += vol
            if len(lot) > 2 and str(lot[2]) < today:
                yest += vol
        return total, yest

    def _account_position(self, vt_symbol: str) -> float:
        """账户在该合约上的总手数（不分策略），只用于校验。"""
        long_vol, short_vol, _, _ = self._ctp_position(vt_symbol)
        return long_vol + short_vol

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

    def _send_manual(self, strategy: CtaTemplate, vt_symbol: str, direction: Direction,
                     offset: Offset, price: float, volume: float) -> str | None:
        """绕过 strategy.vt_symbol 直接下单，但**登记到策略映射**。

        移仓单登记之后，成交回报会进正常的 `process_trade_event`：持仓、批次、
        盈亏、trade_rounds、开平仓通知都按普通开平仓处理，不需要事后手工补账。
        """
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
        if vt_orderid:
            self.orderid_strategy_map[vt_orderid] = strategy
            self.strategy_orderid_map[strategy.strategy_name].add(vt_orderid)
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

    def _order_filled(self, vt_orderid: str | None) -> float:
        """委托的实际成交量（OMS 没有该单时按 0）。"""
        if not vt_orderid:
            return 0.0
        order = self.main_engine.get_order(vt_orderid)
        return float(order.traded or 0.0) if order else 0.0

    def _order_trades(self, vt_orderid: str | None, fallback_price: float,
                      fallback_volume: float) -> list[tuple[float, float]]:
        """该委托的实际成交 [(price, volume)]。

        OMS 的成交回报可能比委托终态晚到；拿不到明细时退回“委托价 × 已成交量”
        估算，保证记账不会因为缺明细而整段丢失。
        """
        if vt_orderid:
            getter = getattr(self.main_engine, "get_all_trades", None)
            if getter:
                fills = [
                    (t.price, t.volume) for t in getter()
                    if t.vt_orderid == vt_orderid and t.volume
                ]
                if fills:
                    return fills
        if fallback_volume:
            return [(fallback_price, fallback_volume)]
        return []

    def _finish_order(self, vt_orderid: str | None, expect: float,
                      timeout: float = 3.0) -> tuple[float, bool]:
        """等成交；不足 expect 手就撤掉剩余并等终态。

        返回 (实际成交量, 委托是否已到终态)。移仓必须按**实际成交量**推进：
        超时只代表“没完全成交”，不代表“没成交”，否则会漏回滚/多平仓。
        """
        if not vt_orderid:
            return 0.0, False
        if self._wait_traded([vt_orderid], expect, timeout):
            return self._order_filled(vt_orderid), True

        order = self.main_engine.get_order(vt_orderid)
        if order is not None and order.is_active():
            try:
                # 必须用 order.orderid（不带 "CTP." 前缀）：CTP 网关撤单会
                # int(req.orderid.split("_")[0])，传 vt_orderid 直接 ValueError，
                # 撤单根本发不出去；基类正确写法见 vnpy_ctastrategy/engine.py:441。
                req = order.create_cancel_request()
                self.main_engine.cancel_order(req, order.gateway_name)
                self.write_log(f"移仓委托超时，已撤剩余：{vt_orderid}")
            except Exception as exc:
                self.write_log(f"移仓委托撤单异常：{vt_orderid} {exc}")
            self._wait_traded([vt_orderid], 0, 2.0)

        order = self.main_engine.get_order(vt_orderid)
        filled = float(order.traded or 0.0) if order else 0.0
        settled = bool(
            order and order.status in (Status.ALLTRADED, Status.CANCELLED, Status.REJECTED)
        )
        return filled, settled

    def _ctp_avg_price(self, vt_symbol: str, direction: Direction) -> float:
        """CTP 某方向持仓的加权成本价；NET 持仓按符号拆多空。"""
        volume = 0.0
        cost = 0.0
        for pos in self.main_engine.get_all_positions():
            if pos.vt_symbol != vt_symbol:
                continue
            if pos.direction == Direction.NET:
                side = Direction.LONG if (pos.volume or 0) > 0 else Direction.SHORT
                vol = abs(pos.volume or 0)
            else:
                side = pos.direction
                vol = pos.volume or 0
            if side != direction or not vol:
                continue
            price = getattr(pos, "price", 0) or 0
            volume += vol
            cost += vol * price
        return cost / volume if volume else 0.0

    def _subscribe_and_wait_tick(self, vt_symbol: str, timeout: float = 10.0) -> bool:
        """移仓前订阅新合约并等首个 tick。

        新主力此前没有被任何策略订阅，OMS 里没有行情 → `_aggressive_price`
        返回 None → 移仓必然失败。这里补订阅，最多等 timeout 秒。
        """
        if self.main_engine.get_tick(vt_symbol) is not None:
            return True
        contract = self.main_engine.get_contract(vt_symbol)
        if contract is None:
            return False
        try:
            req = SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange)
            self.main_engine.subscribe(req, contract.gateway_name)
        except Exception as exc:
            self.write_log(f"移仓订阅 {vt_symbol} 异常：{exc}")
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.main_engine.get_tick(vt_symbol) is not None:
                self.write_log(f"移仓已订阅 {vt_symbol}，收到行情")
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

        # 手数以**本策略批次**为准（CTP 持仓没有策略标签，同合约多策略会吃错仓）
        lot_qty, lot_yd = self._strategy_position(name)
        use_lots = lot_qty > 0 and strategy.pos != 0
        if use_lots:
            qty = lot_qty
            yl = lot_yd
            is_long = strategy.pos > 0
        else:
            qty = long_vol or short_vol
            yl = long_yd if long_vol else short_yd
            is_long = bool(long_vol)
            if qty and lot_qty == 0:
                self.write_log(
                    f"{name}：移仓时没有批次记录（strategy_lots 空），"
                    f"退回按账户口径 {qty:g} 手移仓", strategy)

        # 账户总量必须 >= 本策略手数，否则说明归属已错乱，不能动
        acct_total = long_vol + short_vol
        if use_lots and acct_total + 1e-6 < qty:
            self.notify.send_text(
                f"🚨 {name}：账户在 {old_vt_symbol} 上只有 {acct_total:g} 手，"
                f"但本策略账内 {qty:g} 手，归属不一致，**中止移仓**\n"
                f"  请人工核对后重启策略")
            self._migrating.discard(name)
            return False

        if not qty:
            self.notify.send_text(
                f"ℹ️ {name} 在 {old_vt_symbol} 上没有 CTP 持仓，按空仓换月处理"
            )
            # 本方法跑在移仓线程里，stop/on_init/start 必须交给事件线程执行
            # （否则会和事件线程里的 on_tick/on_bar 打架）。
            self._pending_switches.append({
                "name": name, "new": new_vt_symbol, "old": old_vt_symbol,
                "qty": 0.0, "side": "", "was_trading": was_trading,
                "open_avg": 0.0, "close_fills": [],
                "flat": True,
            })
            return True

        if self.main_engine.get_contract(new_vt_symbol) is None:
            self.notify.send_text(
                f"⚠️ 换月移仓失败：CTP 找不到新合约 {new_vt_symbol}\n"
                f"  策略: {name}  旧合约: {old_vt_symbol}（保持不变）"
            )
            self._migrating.discard(name)
            return False

        # 先订阅新合约并等到行情，否则 _aggressive_price 拿不到盘口，一开仓就失败
        if not self._subscribe_and_wait_tick(new_vt_symbol):
            self.notify.send_text(
                f"⚠️ 换月移仓中止：{new_vt_symbol} 订阅后 10 秒没有行情\n"
                f"  策略: {name}  旧合约: {old_vt_symbol}（保持不变，未下任何单）"
            )
            self.write_log(f"移仓中止：{new_vt_symbol} 无行情，未下任何单", strategy)
            self._migrating.discard(name)
            return False

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

        yd = yl
        exchange = old_vt_symbol.rsplit(".", 1)[-1]
        opened: list[tuple[str, float]] = []      # (vt_orderid, 实际成交量)
        close_fills: list[tuple[Offset, float, float]] = []  # (offset, 成交价, 成交量)
        closed_old = 0.0                          # 老合约实际已平掉的手数
        open_avg = 0.0                            # 新合约开仓成交均价（CTP 成本缺失时兜底）

        try:
            # 1) 撤掉全部挂单，并复查是否真的撤干净（撤不掉的单会在移仓中继续成交）
            self.cancel_all(strategy)
            left_n = 0
            getter = getattr(self.main_engine, "get_all_active_orders", None)
            if callable(getter):
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    try:
                        left_n = sum(1 for o in getter()
                                     if getattr(o, "vt_symbol", "") == old_vt_symbol)
                    except Exception:
                        left_n = 0
                        break
                    if not left_n:
                        break
                    time.sleep(0.1)
            if left_n:
                self.notify.send_text(
                    f"⚠️ {name}：{old_vt_symbol} 撤单后仍有 {left_n} 笔活单，"
                    f"移仓继续（这些单可能继续成交，导致老仓多于预期）")
                self.write_log(f"撤单未清空：{left_n} 笔活单仍挂在 {old_vt_symbol}",
                               strategy)
            else:
                self.notify.send_text(f"🧹 {name}：已撤销 {old_vt_symbol} 上的全部挂单")

            # 2) 在新合约开仓（对手价 = 市价效果）
            price = self._aggressive_price(new_vt_symbol, open_dir)
            if not price:
                raise RuntimeError(f"{new_vt_symbol} 没有行情，无法移仓")
            vid = self._send_manual(
                strategy, new_vt_symbol, open_dir, Offset.OPEN, price, qty
            )
            if not vid:
                raise RuntimeError(f"新合约开仓委托失败（{new_vt_symbol} {qty:g} 手 @ {price}）")

            filled, settled = self._finish_order(vid, qty)
            if filled > 0:
                opened.append((vid, filled))
                fills = self._order_trades(vid, price, filled)
                open_avg = (
                    sum(p * v for p, v in fills) / sum(v for _, v in fills)
                    if fills else price
                )
            if filled < qty:
                if filled > 0:
                    self.notify.send_text(
                        f"⚠️ {name}：新合约只成交 {filled:g}/{qty:g} 手（剩余已撤）\n"
                        f"  合约: {new_vt_symbol}  委托价: {price:.3f}\n"
                        f"  将把已成交的 {filled:g} 手平掉回滚，老合约不动"
                        + ("" if settled else "\n  ⚠️ 委托未能撤销，请立即检查")
                    )
                raise RuntimeError(
                    f"新合约开仓未完全成交（{filled:g}/{qty:g} 手）"
                    + ("" if settled else "，委托未能撤销")
                )
            self.notify.send_text(
                f"🟢 {name}：新合约已开 {qty:g} 手 @ {open_avg:.3f}（{new_vt_symbol}）"
            )

            # 3) 平掉老合约（按交易所规则拆平昨/平今）
            for offset, vol in self._close_plan(exchange, qty, yd):
                old_price = self._aggressive_price(old_vt_symbol, close_dir)
                if not old_price:
                    raise RuntimeError(f"{old_vt_symbol} 没有行情，无法平旧仓")
                vid2 = self._send_manual(
                    strategy, old_vt_symbol, close_dir, offset, old_price, vol
                )
                if not vid2:
                    raise RuntimeError(
                        f"平旧仓委托失败（{old_vt_symbol} {offset.value} {vol:g} 手）"
                    )
                filled2, settled2 = self._finish_order(vid2, vol)
                for p, v in self._order_trades(vid2, old_price, filled2):
                    close_fills.append((offset, p, v))
                closed_old += filled2
                if filled2 < vol:
                    self.notify.send_text(
                        f"⚠️ {name}：旧合约 {offset.value} 只成交 {filled2:g}/{vol:g} 手（剩余已撤）\n"
                        f"  合约: {old_vt_symbol}  委托价: {old_price:.3f}\n"
                        f"  已平 {closed_old:g}/{qty:g} 手，继续补平剩余"
                        + ("" if settled2 else "\n  ⚠️ 委托未能撤销，请立即检查")
                    )
                    raise RuntimeError(
                        f"平旧仓未完全成交（{offset.value} {filled2:g}/{vol:g} 手）"
                    )
                self.notify.send_text(
                    f"🔵 {name}：旧合约已平 {filled2:g} 手（{offset.value}）@ {old_price:.3f}"
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
                cancel_failed = False
                for attempt in range(1, 4):
                    # 每轮按 CTP 实时持仓重算，避免“部分成交后还按原量再平”
                    # 每轮按**本策略剩余批次**重算（别的策略的仓不能平）；
                    # 没有批次记录时退回账户口径——不精确但比"当作没仓"安全。
                    lot_now, lot_yd_now = self._strategy_position(name)
                    if lot_now > 0:
                        yd_now = lot_yd_now
                        live = min(lot_now, remaining)
                    else:
                        ln, sn, lyd, syd = self._ctp_position(old_vt_symbol)
                        live = min(ln if is_long else sn, remaining)
                        yd_now = lyd if is_long else syd
                    if live <= 0:
                        closed_old = qty
                        remaining = 0.0
                        break
                    for offset, vol in self._close_plan(exchange, live, yd_now):
                        old_price = self._aggressive_price(old_vt_symbol, close_dir)
                        if not old_price:
                            break
                        vid = self._send_manual(
                            strategy, old_vt_symbol, close_dir, offset, old_price, vol
                        )
                        if not vid:
                            continue
                        filled, settled_now = self._finish_order(vid, vol)
                        for p, v in self._order_trades(vid, old_price, filled):
                            close_fills.append((offset, p, v))
                        closed_old += filled
                        if not settled_now:
                            # 撤不掉 = 还有活单会继续成交，再补平就会超平
                            self.notify.send_text(
                                f"🚨 {name}：{old_vt_symbol} 的委托无法撤销，停止补平\n"
                                f"  已平 {closed_old:g}/{qty:g} 手，"
                                f"请人工核对挂单与持仓"
                            )
                            cancel_failed = True
                            break
                    if cancel_failed:
                        break
                    remaining = qty - closed_old
                    if remaining <= 0:
                        break
                    self.write_log(
                        f"第 {attempt} 次补平未完成，剩 {remaining:g} 手", strategy
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
                    except Exception as exc:
                        # 告警里已经写了"已停止"，这里停不下来必须再喊一次，
                        # 否则人会以为策略已停而实际还在跑（还带着错乱的账）。
                        self.write_log(f"人工接管时停止策略失败（{name}）：{exc}", strategy)
                        self.notify.send_text(
                            f"🚨 {name} 停止失败，策略可能仍在运行！\n"
                            f"  {exc}\n"
                            f"  请立即手工停止该策略，再核对两边持仓")
                    self._migrating.discard(name)
                    return False
                # 补平完成 → 落到下面的成功收尾

            else:
                # 阶段 A：老仓一手没动，可以安全回滚新仓（按实际成交量）
                if not opened:
                    # 一单都没成交：回滚是空操作，但必须让人知道移仓失败了
                    self.notify.send_text(
                        f"⚠️ {name}：换月移仓中止，新合约一单未成交，老仓未动\n"
                        f"  {old_vt_symbol} → {new_vt_symbol}\n  {exc}")
                rollback_left = 0.0
                for _vid, vol in opened:
                    tick_price = self._aggressive_price(new_vt_symbol, close_dir)
                    if not tick_price:
                        rollback_left += vol
                        self.notify.send_text(
                            f"⚠️ 回滚失败：{new_vt_symbol} 没有行情，"
                            f"请手工平掉 {vol:g} 手（策略账外）"
                        )
                        continue
                    # 刚开的仓是"今仓"：SHFE/INE 必须平今，净持仓交易所用平仓
                    new_exchange = new_vt_symbol.rsplit(".", 1)[-1]
                    rb_offset = self._close_plan(new_exchange, vol, 0.0)[0][0]
                    rollback_id = self._send_manual(
                        strategy, new_vt_symbol, close_dir, rb_offset, tick_price, vol
                    )
                    rolled, settled = self._finish_order(rollback_id, vol)
                    if rolled < vol:
                        rollback_left += vol - rolled
                        self.notify.send_text(
                            f"🚨 回滚未完成，需人工接管\n"
                            f"  策略: {name}\n"
                            f"  {new_vt_symbol} 已回滚 {rolled:g}/{vol:g} 手，"
                            f"仍剩 {vol - rolled:g} 手（策略账外）\n"
                            f"  请立即检查账户持仓"
                            + ("" if settled else "；委托未能撤销")
                        )
                    elif settled:
                        self.notify.send_text(
                            f"↩️ 已回滚：{new_vt_symbol} 平掉 {rolled:g} 手，老合约保持不动"
                        )
                if rollback_left > 0:
                    self.notify.send_text(
                        f"🚨 换月回滚仍有 {rollback_left:g} 手未平（{new_vt_symbol}，"
                        f"策略账外），请手工处理"
                    )
                self._migrating.discard(name)
                return False

        # 4) 成功：只投递切换请求，由事件线程执行 stop/switch/start
        #    （策略对象只能被事件线程碰；这里继续冻结，直到切换完成）
        #    deque.append 在 CPython 下是原子操作，事件线程 popleft 消费。
        self._pending_switches.append({
            "name": name, "new": new_vt_symbol, "old": old_vt_symbol,
            "qty": qty, "side": side, "was_trading": was_trading,
            "open_avg": open_avg, "close_fills": close_fills,
        })
        self.write_log(
            f"移仓成交完成，等待切换合约：{old_vt_symbol} -> {new_vt_symbol}（{side} {qty:g} 手）",
            strategy,
        )
        return True

    # ----------------------------------------------------------------------
    # 盘中执行 / 切换合约
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
        一次只处理一个：`_switch_symbol` 里的 on_init/load_bar 可能耗时，
        多个一起做会把事件线程卡住；队列保证每个策略最终都会轮到。
        """
        if not self._pending_switches:
            return
        pending = self._pending_switches.popleft()

        name = pending["name"]
        new_vt_symbol = pending["new"]
        old_vt_symbol = pending["old"]
        strategy = self.strategies.get(name)

        try:
            if strategy is None:
                raise RuntimeError(f"策略 {name} 已不存在")
            self.stop_strategy(name)
            self._switch_symbol(strategy, new_vt_symbol, pending["was_trading"])

            # 空仓换月：新合约上本来就没有仓，不需要重建账目
            if not pending.get("flat"):
                # **按本策略移仓手数**设 pos，不能用账户总量（同合约可能还有别的策略）
                move_qty = float(pending.get("qty") or 0)
                if move_qty:
                    strategy.pos = move_qty if pending.get("side") == "多" else -move_qty

                # 移仓单不经过 orderid_strategy_map，on_trade 不会触发：
                # 必须在这里把旧腿盈亏入账，并用新合约成本重建批次。
                self._reconcile_after_rollover(strategy, pending)

            variety = _variety_of(new_vt_symbol)
            if variety:
                self._main_contract_cache[self._main_contract_key(variety)] = (
                    new_vt_symbol.rsplit(".", 1)[0]
                )

            if pending.get("flat"):
                detail = f"  {old_vt_symbol} → {new_vt_symbol}（空仓换月）\n"
                self.write_log(
                    f"空仓换月完成：{old_vt_symbol} -> {new_vt_symbol}", strategy
                )
            else:
                detail = (
                    f"  {old_vt_symbol} {pending['side']} {pending['qty']:g} 手 → "
                    f"{new_vt_symbol} {pending['side']} {pending['qty']:g} 手\n"
                )
                self.write_log(
                    f"换月移仓完成：{old_vt_symbol} -> {new_vt_symbol}"
                    f"（{pending['side']} {pending['qty']:g} 手）",
                    strategy,
                )
            self.notify.send_text(
                f"✅ 换月移仓完成\n"
                f"  策略: {name}\n"
                f"{detail}"
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

    def _reconcile_after_rollover(self, strategy: CtaTemplate, pending: dict) -> None:
        """移仓后的对账（**不重复结算盈亏**）。

        移仓单现在登记在 `orderid_strategy_map` 里，旧腿平仓 / 新腿开仓都是
        成交回报时按普通开平仓记的：`strategy.pos`、`strategy_lots`、
        `strategy_cumulative_pl`、`_today_realized_pl`、`trade_rounds`、通知。
        所以这里只做三件事：
          ① 用 CTP 持仓校验手数（CTP 约 4 秒刷一次，没刷到就以成交回报为准）；
          ② 批次缺账时按 CTP 成本兜底重建；
          ③ 补写旧合约 0 手快照 + 新合约真实持仓快照，并落盘策略数据。
        """
        name = strategy.strategy_name
        old_vt_symbol = pending["old"]
        new_vt_symbol = pending["new"]
        is_long = pending["side"] == "多"
        today = trading_day(datetime.now())
        expected = float(pending.get("qty") or 0)

        long_vol, short_vol, _, _ = self._ctp_position(new_vt_symbol)
        ctp_volume = long_vol or short_vol
        lot_qty, _ = self._strategy_position(name)

        # ① 手数校验：账户总量恰好等于本策略手数时才用 CTP 对齐；
        #    否则说明同合约还有别的策略，账户总量不能拿来覆盖本策略的 pos。
        if ctp_volume and abs(ctp_volume - expected) < 1e-6:
            strategy.pos = ctp_volume if long_vol >= short_vol else -ctp_volume
        elif expected and abs(lot_qty - expected) > 1e-6:
            self.write_log(
                f"{name}：账户 {new_vt_symbol} 共 {ctp_volume:g} 手、本策略账内 "
                f"{lot_qty:g} 手（应为 {expected:g}），按策略手数记账", strategy)
            strategy.pos = expected if is_long else -expected
        elif expected and not ctp_volume:
            self.write_log(
                f"{name}：移仓后 CTP 暂未显示 {new_vt_symbol} 持仓，"
                f"以成交回报为准（策略账内 {strategy.pos:+g} 手）",
                strategy,
            )

        # ② 批次缺账兜底（正常情况不该触发：成交回报会逐笔建批次）
        lots = self.strategy_lots.get(name) or []
        lots_volume = sum(b[0] for b in lots)
        if ctp_volume and abs(lots_volume - ctp_volume) > 1e-6:
            avg = (
                self._ctp_avg_price(new_vt_symbol, Direction.LONG)
                if long_vol
                else self._ctp_avg_price(new_vt_symbol, Direction.SHORT)
            ) or float(pending.get("open_avg") or 0.0)
            self.strategy_lots[name] = [[ctp_volume, avg, today, datetime.now()]]
            self.notify.send_text(
                f"⚠️ {name}：移仓后批次 {lots_volume:g} 手 ≠ CTP {ctp_volume:g} 手，"
                f"已按 CTP 成本 {avg:.3f} 重建；请核对盈亏"
            )
        elif not ctp_volume and not lots_volume:
            self.strategy_lots.pop(name, None)

        filled_old = sum(v for _, _, v in (pending.get("close_fills") or []))
        if filled_old and abs(filled_old - expected) > 1e-6:
            self.write_log(
                f"{name}：移仓平旧实际成交 {filled_old:g}/{expected:g} 手，请核对",
                strategy,
            )

        # ③ 旧合约补 0 手快照：positions 表按 (strategy, vt_symbol) 取最新，
        #    不写 0 行的话换月后前端会一直显示旧合约的持仓。
        monitor = getattr(self, "monitor", None)
        if monitor:
            monitor.write(
                MonitorPosition(
                    strategy=name,
                    account=getattr(self, "account", "real"),
                    vt_symbol=old_vt_symbol,
                    direction="LONG" if is_long else "SHORT",
                    volume=0,
                    avg_price=0.0,
                    pnl=0.0,
                    trading_day=today,
                    snapshot_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
            )
        # 新合约立刻写一条真实持仓，否则前端持仓卡要等到下一笔成交
        self._write_position_snapshot(strategy, name, force=True)

        self.sync_strategy_data(strategy)

    def _run_pending_rollovers(self) -> None:
        """执行初始化时判定出的待移仓（**不再重新判定主力**）。

        08:45 / 20:45 启动时市场还没开，拿不到盘口价，所以启动只判定不下单；
        等进入盘中窗口、且新合约已经有行情，才真正执行，每个策略只做一次。
        失败不重试（避免坏行情里反复下单），直接告警交人工。
        """
        if not self._pending_rollover or not self._in_rollover_window():
            return
        for name, new_vt_symbol in list(self._pending_rollover.items()):
            strategy = self.strategies.get(name)
            if strategy is None:
                self._pending_rollover.pop(name, None)
                continue
            if name in self._migrating:
                continue
            if not strategy.inited or not strategy.trading:
                continue
            if self.main_engine.get_tick(new_vt_symbol) is None:
                continue                       # 还没行情，下一秒再看
            self._pending_rollover.pop(name, None)
            self.write_log(
                f"盘中执行换月移仓：{strategy.vt_symbol} -> {new_vt_symbol}", strategy
            )
            try:
                self._migrate_position(strategy, new_vt_symbol)
            except Exception as exc:
                self.write_log(f"换月移仓启动异常（{name}）：{exc}", strategy)
                self.notify.send_text(f"⚠️ 换月移仓启动异常\n  {name}\n  {exc}")

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

        # 只有落后超过 3 个自然日才提醒：data_through 永远是"上一个交易日"，
        # 用 `>= today` 判断会让每个交易日 15:10、每个周一、每个节后首日都误报。
        if (now.date() - table.data_through).days <= 3:
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
            f"  请检查 scripts\\rollover_daily.py 是否正常运行（计划任务/守护线程）\n"
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
        today = trading_day(datetime.now())
        for name, strategy in self.strategies.items():
            # 累计盈亏（/strategies）= Σ trade_rounds(开仓价) + 浮动，所以这里用开仓价口径；
            # 日内权益曲线要跟账户权益对得上，所以用盯市口径
            avg_price = self._lots_avg(self.strategy_lots.get(name))
            pnl_entry = round(self._floating_entry(strategy, avg_price), 2)
            pnl_mtm = round(self._floating_mtm(strategy, today), 2)
            monitor.write(
                MonitorStrategyStatus(
                    strategy=name,
                    account=account,
                    symbol=strategy.vt_symbol,
                    status="running" if strategy.trading else "stopped",
                    pos=strategy.pos,
                    pnl=pnl_entry,
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
                        floating_pl=pnl_mtm,
                        equity=round(realized + pnl_mtm, 2),
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

    def _restore_today_realized(self, day: str) -> float:
        """进程重启后把"当日已实现"从监控库续上（内存变量会丢）。

        CTP 没有"当日平仓盈亏"字段，只能读自己上一次写的
        `account_intraday_pnl.realized_pl`；没写过就从 0 开始。
        """
        monitor = getattr(self, "monitor", None)
        if not monitor:
            return 0.0
        try:
            with monitor.engine.connect() as conn:
                row = conn.exec_driver_sql(
                    "SELECT realized_pl FROM account_intraday_pnl "
                    "WHERE account = ? AND trading_day = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (getattr(self, "account", "real"), day),
                ).fetchone()
        except Exception as exc:
            self.write_log(f"恢复当日已实现盈亏失败（按 0 计）：{exc}")
            return 0.0
        return float(row[0] or 0.0) if row else 0.0

    # ----------------------------------------------------------------------
    # 结算价（逐日盯市基准）
    # ----------------------------------------------------------------------
    def _settle_of(self, vt_symbol: str, before_day: str) -> float | None:
        """取 before_day 之前最近一个交易日的结算价。

        隔夜仓的逐日盯市成本 = 前一交易日结算价；不依赖 CTP 的聚合持仓成本，
        所以多策略同品种、手工单都不会算错。
        """
        key = (vt_symbol, before_day)
        if key in self._settle_cache:
            return self._settle_cache[key]

        value = None
        monitor = getattr(self, "monitor", None)
        if monitor:
            try:
                with monitor.engine.connect() as conn:
                    row = conn.exec_driver_sql(
                        "SELECT price FROM settlements "
                        "WHERE account = ? AND symbol = ? AND trading_day < ? "
                        "ORDER BY trading_day DESC LIMIT 1",
                        (
                            getattr(self, "account", "real"),
                            vt_symbol,
                            before_day,
                        ),
                    ).fetchone()
                if row and row[0]:
                    value = float(row[0])
            except Exception as exc:
                self.write_log(f"读结算价失败（{vt_symbol}）：{exc}")
        self._settle_cache[key] = value
        return value

    def _save_settlement(
        self, day: str, vt_symbol: str, price: float, update_time: str
    ) -> None:
        """幂等写一条结算价：先删同 (交易日, 合约)，再异步插入。"""
        monitor = getattr(self, "monitor", None)
        if not monitor:
            return
        account = getattr(self, "account", "real")
        try:
            with monitor.engine.begin() as conn:
                conn.exec_driver_sql(
                    "DELETE FROM settlements "
                    "WHERE account = ? AND trading_day = ? AND symbol = ?",
                    (account, day, vt_symbol),
                )
        except Exception as exc:
            self.write_log(f"清理旧结算价失败（{vt_symbol}）：{exc}")
        monitor.write(
            MonitorSettlement(
                account=account,
                symbol=vt_symbol,
                trading_day=day,
                price=price,
                update_time=update_time,
            )
        )
        self._settle_cache[(vt_symbol, day)] = price

    def _fetch_settlement_akshare(self, market: str, day: str) -> list:
        """拉交易所日线（akshare），返回 [(symbol, settle), ...]。"""
        import akshare as ak

        df = ak.get_futures_daily(
            start_date=day.replace("-", ""),
            end_date=day.replace("-", ""),
            market=market,
        )
        if df is None or df.empty:
            return []
        out = []
        for symbol, settle in zip(df["symbol"], df["settle"]):
            if settle is not None and float(settle) > 0:
                out.append((str(symbol), float(settle)))
        return out

    def _settlement_symbols(self, day: str) -> set:
        """某交易日已经写过的合约集合。"""
        monitor = getattr(self, "monitor", None)
        if not monitor:
            return set()
        try:
            with monitor.engine.connect() as conn:
                rows = conn.exec_driver_sql(
                    "SELECT symbol FROM settlements "
                    "WHERE account = ? AND trading_day = ?",
                    (getattr(self, "account", "real"), day),
                ).fetchall()
            return {str(r[0]) for r in rows}
        except Exception as exc:
            self.write_log(f"读结算价失败：{exc}")
            return set()

    def _previous_weekday(self, day: str) -> str:
        """前一个工作日（无节假日日历；拉不到数据会再往前退）。"""
        d = date.fromordinal(date.fromisoformat(day).toordinal() - 1)
        while d.weekday() >= 5:
            d = date.fromordinal(d.toordinal() - 1)
        return d.isoformat()

    def _fetch_markets(self, day: str, targets: set, fetcher=None) -> dict:
        """按交易所分批取结算价，返回 {vt_symbol: settle}。"""
        official: dict[str, float] = {}
        for market in ("DCE", "CZCE", "SHFE", "INE", "GFEX", "CFFEX"):
            symbols = {s for s in targets if s.endswith(f".{market}")}
            if not symbols:
                continue
            try:
                pairs = (
                    fetcher(market, day)
                    if fetcher
                    else self._fetch_settlement_akshare(market, day)
                )
            except Exception as exc:
                self.write_log(f"拉取 {market} 结算价失败：{exc}")
                continue
            for symbol, price in pairs:
                vt = normalize_vt_symbol(f"{symbol}.{market}")
                if vt in symbols:
                    official[vt] = price
        return official

    def sync_settlements_from_api(self, fetcher=None) -> dict:
        """启动时取上一交易日的结算价（交易所日线），写库并推送手机。

        已有数据就跳过（一天只拉一次、只推一次）。fetcher 仅供测试注入。
        """
        result = {"day": "", "saved": [], "skipped": False}
        monitor = getattr(self, "monitor", None)
        if not monitor:
            return result

        today = trading_day(datetime.now())
        targets = {s.vt_symbol for s in self.strategies.values()}
        if not targets:
            return result

        day = self._previous_weekday(today)
        for _ in range(3):
            missing = targets - self._settlement_symbols(day)
            if not missing:
                result["day"] = day
                result["skipped"] = True
                self.write_log(f"结算价已就绪（{day}），无需拉取")
                return result

            official = self._fetch_markets(day, missing, fetcher)
            if not official:
                day = self._previous_weekday(day)
                continue

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for vt, price in official.items():
                self._save_settlement(day, vt, price, now)
                # 启动瞬间可能已经缓存过 None，这里覆盖掉
                self._settle_cache[(vt, today)] = price
                result["saved"].append((vt, price))
            result["day"] = day

            self.write_log(
                f"结算价已更新（{day}）："
                + "、".join(f"{vt} {price:.2f}" for vt, price in result["saved"])
            )
            # 一个合约可能对应多个策略（一对多），打印时带上
            owners: dict[str, list] = {}
            for name, strategy in self.strategies.items():
                owners.setdefault(strategy.vt_symbol, []).append(name)

            label = (
                "模拟盘" if getattr(self, "account", "real") == "simnow" else "实盘"
            )
            lines = [f"📐 结算价（{label}｜交易日 {day}）", f"⏱ 拉取于 {now}"]
            for vt, price in result["saved"]:
                names = owners.get(vt) or []
                tag = "、".join(names) if names else "无策略（历史/残仓）"
                lines.append(f"  {vt}  {price:.2f}  [{tag}]")
            self.notify.send_text("\n".join(lines))
            return result

        self.write_log(f"结算价拉取失败（{today} 之前 3 个工作日都没有数据）")
        return result

    # ----------------------------------------------------------------------
    # 保证金率（openctp 合约字典）
    # ----------------------------------------------------------------------
    @staticmethod
    def _fetch_openctp_instruments(timeout: float = 15.0) -> list:
        """拉 openctp 全市场期货合约（含保证金率 / 合约乘数 / 最小变动价位）。

        用空 ProxyHandler 直连，避免本机 Clash 等代理劫持 dict.openctp.cn
        （与回测仓 fetch_specs.py 的做法一致）。
        """
        req = urllib.request.Request(
            OPENCTP_INSTRUMENTS_URL,
            headers={"User-Agent": "vnpy-domestic/0.2.2"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        if payload.get("rsp_code") not in (0, None):
            raise RuntimeError(
                f"openctp 返回错误 rsp_code={payload.get('rsp_code')} "
                f"{payload.get('rsp_message', '')}"
            )
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise RuntimeError("openctp 返回空合约列表")
        return data

    def sync_margin_rates(self, fetcher=None) -> dict:
        """启动时加载 openctp 保证金率（按合约，缺失时用品种中位数）。

        只影响账户卡的保证金估算和 contracts.margin_rate，不参与交易/风控。
        失败不阻塞启动：保留已有缓存；从未加载成功时按 DEFAULT_MARGIN_RATE 估算。
        fetcher 仅供测试注入，返回 openctp 的 data 列表。
        """
        try:
            data = fetcher() if fetcher else self._fetch_openctp_instruments()
        except Exception as exc:
            self.write_log(
                f"openctp 保证金率加载失败（账户保证金按 "
                f"{DEFAULT_MARGIN_RATE:.0%} 兜底）：{exc}"
            )
            return {"contracts": 0, "products": 0, "error": str(exc)}

        rates: dict[str, tuple[float, float]] = {}
        products: dict[str, list[tuple[float, float]]] = {}
        for item in data:
            try:
                instrument = str(item.get("InstrumentID") or "").strip()
                exchange = str(item.get("ExchangeID") or "").strip().upper()
                product = str(item.get("ProductID") or "").strip().upper()
                long_rate = float(item.get("LongMarginRatioByMoney") or 0.0)
                short_rate = float(item.get("ShortMarginRatioByMoney") or 0.0)
            except (AttributeError, TypeError, ValueError):
                continue

            if not instrument or not exchange:
                continue
            if long_rate <= 0 and short_rate <= 0:
                continue
            # 部分合约只配了一边，另一边兜底
            if long_rate <= 0:
                long_rate = short_rate
            if short_rate <= 0:
                short_rate = long_rate

            vt_symbol = normalize_vt_symbol(f"{instrument}.{exchange}")
            rates[vt_symbol] = (long_rate, short_rate)
            if product:
                products.setdefault(product, []).append((long_rate, short_rate))

        if not rates:
            self.write_log("openctp 保证金率解析为空，沿用兜底费率")
            return {"contracts": 0, "products": 0, "error": "empty"}

        self._margin_rates = rates
        self._margin_product_rates = products
        self._margin_loaded_at = time.time()
        self.write_log(
            f"openctp 保证金率已加载：{len(rates)} 个合约 / {len(products)} 个品种"
        )

        # 让前端合约卡立刻有值，不用等下一轮 5 分钟监控
        monitor = getattr(self, "monitor", None)
        if monitor:
            for strategy in list(getattr(self, "strategies", {}).values()):
                self._write_contract(strategy.vt_symbol)
        return {"contracts": len(rates), "products": len(products)}

    def _margin_rate_of(self, vt_symbol: str, direction=None) -> float | None:
        """取某合约的保证金率（小数，0.06 = 6%）。

        优先精确到合约；合约不在 openctp 字典（新上市/临近交割）时按品种取
        中位数；都没有返回 None，由调用方决定是否兜底。
        """
        rates = self._margin_rates.get(normalize_vt_symbol(vt_symbol))
        if rates is None:
            variety = _variety_of(vt_symbol).upper()
            values = self._margin_product_rates.get(variety) or []
            if values:
                idx = 1 if direction == Direction.SHORT else 0
                candidates = sorted(v[idx] for v in values if v[idx] > 0)
                if candidates:
                    return float(statistics.median(candidates))
            return None

        long_rate, short_rate = rates
        if direction == Direction.SHORT:
            return short_rate or long_rate
        return long_rate or short_rate

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
            # 保证金率按 openctp 合约字典（多头/空头分别取），拿不到才用兜底
            rate_direction = (
                Direction.LONG if direction == "LONG" else Direction.SHORT
            )
            margin_rate = self._margin_rate_of(pos.vt_symbol, rate_direction)
            if margin_rate is None:
                margin_rate = DEFAULT_MARGIN_RATE
            total_margin += volume * pos.price * size * margin_rate
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
            self._today_realized_pl = self._restore_today_realized(today)
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
        # 正在换月移仓：策略自己的下单全部丢弃（迁移单走 _send_manual 旁路）
        if getattr(strategy, "strategy_name", "") in self._migrating:
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
        """兜底：单个事件处理出错也不能让事件线程死掉。"""
        try:
            self._handle_order_event(event)
        except Exception:
            self.write_log(
                f"委托事件处理错误（已捕获，事件线程继续）：{traceback.format_exc()}"
            )

    def _handle_order_event(self, event: Event) -> None:
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
        """兜底：单个事件处理出错也不能让事件线程死掉。"""
        try:
            self._handle_trade_event(event)
        except Exception:
            self.write_log(
                f"成交事件处理错误（已捕获，事件线程继续）：{traceback.format_exc()}"
            )

    def _handle_trade_event(self, event: Event) -> None:
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

        # ── P&L 追踪（批次队列按交易所规则匹配）──
        pl = 0.0
        pl_amount = 0.0
        size = 0.0
        batch_details: list = []      # 平仓时 settle_close 返回的逐批配对明细
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
                # 逐日盯市：隔日仓成本基准 = 前一交易日结算价（settlements 表）
                prev_settle = self._settle_of(trade.vt_symbol, today)
                if prev_settle is None:
                    # 还没写过结算价（首次上线/漏写）：整仓都是昨仓时退用 CTP 成本价
                    hold_direction = (
                        Direction.LONG if direction_mult == 1 else Direction.SHORT
                    )
                    for pos in self.main_engine.get_all_positions():
                        if pos.vt_symbol != trade.vt_symbol:
                            continue
                        if (
                            pos.direction in (Direction.NET, hold_direction)
                            and pos.yd_volume > 0
                            and pos.yd_volume == pos.volume
                        ):
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
                batch_details = details or []
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

        # ── 开平仓通知（on_trade 之后，pos 已更新） ──
        # 移仓单（reference="rollover"）走同一套记账，只是文案上区分一下
        is_rollover = order is not None and order.reference == "rollover"
        if trade.offset == Offset.OPEN:
            pos_str = f"{strategy.pos:+g}"
            self.notify.send_text(
                f"{'🔁 移仓开仓' if is_rollover else '🟢 开仓'}\n"
                f"  策略: {strategy_name}\n"
                f"  合约: {trade.symbol}\n"
                f"  方向: {trade.direction.value}\n"
                f"  价格: {trade.price:.2f}  数量: {trade.volume}\n"
                f"  持仓: {pos_str}"
            )
        elif trade.offset in (Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY):
            notify_text = (
                f"{'🔁 移仓平仓' if is_rollover else '🔴 平仓'}\n"
                f"  策略: {strategy_name}\n"
                f"  合约: {trade.symbol}\n"
                f"  平仓价: {trade.price:.2f}  数量: {trade.volume}"
            )
            # 把配对的**开仓批次**一起贴出来：手数 / 开仓价 / 开仓时间 / 持有时间 / 该批盈亏
            if batch_details:
                notify_text += "\n  对应开仓:"
                for vol, entry_price, _day, pair_pl, open_dt in batch_details:
                    seg = f"\n    · {vol:g} 手 @ {entry_price:.2f}"
                    if open_dt is not None:
                        try:
                            seg += f"  开于 {open_dt:%m-%d %H:%M}"
                        except (TypeError, ValueError):
                            pass
                        secs = self._holding_seconds(open_dt, trade.datetime)
                        if secs is not None:
                            seg += f"  持有 {self._fmt_holding(secs)}"
                    if size:
                        seg += f"  盈亏 {pair_pl * size:+.2f}"
                    notify_text += seg
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
        """兜底：单个事件处理出错也不能让事件线程死掉。"""
        try:
            self._handle_timer_event(event)
        except Exception:
            self.write_log(
                f"定时器处理错误（已捕获，事件线程继续）：{traceback.format_exc()}"
            )

    def _handle_timer_event(self, event: Event) -> None:
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

        # ── 盘中执行启动时判定出的待移仓（每策略只执行一次）──
        self._run_pending_rollovers()

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
                f"  合约: {strategy.vt_symbol}  |  持仓: {strategy.pos:+g}",
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
            # strategy_cumulative_pl 在平仓累加时已乘 size，直接是元
            pl_amount = self.strategy_cumulative_pl.get(name, 0) or 0
            pos_str = f"{strategy.pos:+g}"
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
        """把当日累计盈亏落库并归零（每次退出都会调用，不保证只调一次）。

        同一个交易日可能被调多次：夜盘 23:05 退出（交易日=次日）与次日 15:01
        退出（同一交易日）。所以写入按 (trading_day, strategy, account) 做
        **累加 upsert**，否则同一交易日会被拆成两行。

        盘中手动停止（14:00 之类）也会走到这里：写当天那一行并归零，累计盈亏
        不会串到下一个交易日。
        """
        monitor = getattr(self, "monitor", None)
        day = trading_day(datetime.now())
        if monitor:
            account = getattr(self, "account", "real")
            for name in self.strategies:
                realized = self.strategy_cumulative_pl.get(name, 0) or 0
                self._add_daily_pnl(monitor, day, name, account, realized)
        for name in self.strategies:
            self.strategy_cumulative_pl[name] = 0.0

    def _add_daily_pnl(self, monitor, day: str, name: str, account: str,
                       realized: float) -> None:
        """daily_pnl 累加写：同一天同一策略只保留一行，值相加。"""
        try:
            with monitor.engine.begin() as conn:
                row = conn.exec_driver_sql(
                    "SELECT id, realized_pl FROM daily_pnl "
                    "WHERE trading_day = ? AND strategy = ? AND account = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (day, name, account),
                ).fetchone()
                if row:
                    total = round(float(row[1] or 0.0) + realized, 2)
                    conn.exec_driver_sql(
                        "UPDATE daily_pnl SET realized_pl = ?, cumulative_pl = ? "
                        "WHERE id = ?",
                        (total, total, row[0]),
                    )
                else:
                    conn.exec_driver_sql(
                        "INSERT INTO daily_pnl "
                        "(trading_day, strategy, account, realized_pl, cumulative_pl) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (day, name, account, round(realized, 2), round(realized, 2)),
                    )
        except Exception as exc:
            self.write_log(f"写 daily_pnl 失败（{name} {day}）：{exc}")

    def _write_contract(self, vt_symbol: str) -> None:
        """Upsert CTP contract specs (name/product/size/pricetick/margin_rate)
        into the monitor DB so the frontend shows real exchange data instead of
        a hardcoded table."""
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
                # 小数存储（0.06）；没有 openctp 数据时写 0，前端显示“动态”
                margin_rate=round(
                    self._margin_rate_of(vt_symbol, Direction.LONG) or 0.0, 6
                ),
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

    def _check_duplicate_products(self) -> None:
        """同一合约跑多个策略 → 换月移仓会互相吃仓（CTP 持仓没有策略标签）。

        启动时检查一次，命中就写日志 + 发飞书；不阻止启动（有时是临时对比，
        手工移仓也能用），但必须让人知道。
        """
        if getattr(self, "_dup_checked", False):
            return
        self._dup_checked = True
        seen: dict[str, str] = {}
        dups: list[str] = []
        for name, setting in list(self.strategy_setting.items()):
            vs = normalize_vt_symbol(setting.get("vt_symbol", "") or "")
            if not vs:
                continue
            if vs in seen:
                dups.append(f"{seen[vs]} + {name} → {vs}")
            else:
                seen[vs] = name
        if dups:
            msg = ("🚨 检测到同一合约跑多个策略，换月移仓会互相吃仓：\n  "
                   + "\n  ".join(dups)
                   + "\n  移仓已改为按策略批次手数执行；仍建议改成不同品种，"
                   "或交易前手工移仓")
            self.write_log(msg)
            self.notify.send_text(msg)

    def _init_strategy(self, strategy_name: str) -> None:
        """翻写：初始化前检查是否需要换月，仅记录结果不发通知"""
        self._check_duplicate_products()
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

    def _saved_position(self, strategy_name: str) -> float:
        """持久化里的净持仓。

        `_init_strategy` 跑在 vnpy 恢复 strategy_data 之前，此时 strategy.pos
        一定是构造时的 0；判"有没有仓"只能读持久化 + CTP，不能读 strategy.pos。
        """
        data = self.strategy_data.get(strategy_name) or {}
        return float(data.get("pos", 0) or 0)

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
            self._report_rollover_failure(
                variety, f"换月检查未通过：无法获取 {variety} 主力合约信息"
            )
            self._rollover_init_results[name] = "failed"
            return

        new_vt_symbol = normalize_vt_symbol(f"{main_contract}.{exchange}")
        if new_vt_symbol == normalize_vt_symbol(strategy.vt_symbol):
            self._rollover_init_results[name] = "no_change"
            return

        self.write_log(
            f"策略 [{name}] 检测到主力合约变化：{strategy.vt_symbol} -> {new_vt_symbol}"
        )

        # vnpy 的 _init_strategy 是 on_init 之后才把 strategy_data 里的 pos
        # 写回策略对象（vnpy_ctastrategy/engine.py:684-692），本方法跑在那之前，
        # strategy.pos 必然是 0。用 0 判"空仓"会把真实持仓当成空仓直接切合约，
        # 老仓留在旧合约上无人接管。
        saved_pos = self._saved_position(name)
        ctp_long, ctp_short, _, _ = self._ctp_position(strategy.vt_symbol)
        if not (saved_pos or ctp_long or ctp_short):
            # 空仓：直接切合约，不用等行情
            old_vt_symbol = strategy.vt_symbol
            if self._execute_rollover(strategy, new_vt_symbol):
                self.write_log(
                    f"策略 [{name}] 初始化时已自动换月：{old_vt_symbol} -> {new_vt_symbol}"
                )
                self._rollover_init_results[name] = "rolled"
            else:
                self._rollover_init_results[name] = "failed"
        else:
            # 有持仓：启动时市场没开，只登记，盘中再移仓
            self._pending_rollover[name] = new_vt_symbol
            self.write_log(
                f"策略 [{name}] 有持仓（持久化 pos={saved_pos:g}，"
                f"CTP 多 {ctp_long:g}/空 {ctp_short:g}），"
                f"换月移仓推迟到盘中执行：{strategy.vt_symbol} -> {new_vt_symbol}",
                strategy,
            )
            self._rollover_init_results[name] = "pending"

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


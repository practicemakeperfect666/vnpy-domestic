"""
vnpy-domestic trader.utility
============================
vnpy 增强版工具模块，包含：
  - 交易时段加载/运行时判断（竞价位移、非交易时段过滤）
  - MyBarGenerator  —— 增强版 BarGenerator
"""
from __future__ import annotations

import csv
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional

from vnpy.trader.constant import Interval
from vnpy.trader.object import BarData, TickData
from vnpy.trader.utility import BarGenerator as _BarGenerator


# ═══════════════════════════════════════════════════════════
# 第一部分：交易时段管理
# ═══════════════════════════════════════════════════════════

_TRADING_SESSIONS: dict[str, list[tuple[time, time]]] = {}
_SORTED_PIDS: list[str] = []


def _parse_time_ranges(ranges_str: str) -> list[tuple[time, time]]:
    """'09:00-10:15 | 10:30-11:30' → [(time(9,0), time(10,15)), ...]"""
    if not ranges_str or not ranges_str.strip():
        return []
    result: list[tuple[time, time]] = []
    for part in ranges_str.split("|"):
        part = part.strip()
        if not part:
            continue
        start_str, end_str = part.split("-")
        h1, m1 = (int(x) for x in start_str.strip().split(":"))
        h2, m2 = (int(x) for x in end_str.strip().split(":"))
        result.append((time(h1, m1), time(h2, m2)))
    return result


def _load_trading_times(csv_path: Path) -> dict[str, list[tuple[time, time]]]:
    """加载 trading_times.csv，返回 {ProductID: [(start, end), ...]}"""
    result: dict[str, list[tuple[time, time]]] = {}
    if not csv_path.exists():
        return result
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pid: str = row["ProductID"].strip()
            ranges: list[tuple[time, time]] = []
            ranges.extend(_parse_time_ranges(row.get("day_trading_hours", "")))
            ranges.extend(_parse_time_ranges(row.get("night_trading_hours", "")))
            result[pid] = ranges
    return result


# ── 默认 CSV 搜索路径 ──
_DEFAULT_CSV_PATHS: list[Path] = [
    Path.cwd() / ".vntrader" / "trading_times.csv",
    Path.home() / ".vntrader" / "trading_times.csv",
]
# 同时支持从 vnpy-domestic 包目录查找
_pkg_root = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CSV_PATHS.insert(0, _pkg_root / ".vntrader" / "trading_times.csv")


def reload_trading_times(csv_path: Optional[Path] = None) -> bool:
    """重新加载交易时段数据，返回是否加载成功"""
    global _TRADING_SESSIONS, _SORTED_PIDS

    if csv_path:
        paths = [csv_path]
    else:
        paths = _DEFAULT_CSV_PATHS

    for p in paths:
        data = _load_trading_times(p)
        if data:
            _TRADING_SESSIONS = data
            _SORTED_PIDS = sorted(_TRADING_SESSIONS.keys(), key=len, reverse=True)
            return True

    _TRADING_SESSIONS = {}
    _SORTED_PIDS = []
    return False


# 模块加载时自动尝试加载
reload_trading_times()


def _extract_product_id(symbol: str) -> str:
    """从 tick symbol 提取产品代码，如 'AP510'→'AP', 'l_f2501'→'l_f'"""
    upper: str = symbol.upper()
    for pid in _SORTED_PIDS:
        if upper.startswith(pid.upper()):
            return pid
    return ""


def _time_subtract_minute(t: time) -> time:
    """time - 1 minute，处理跨小时"""
    dummy = datetime(2000, 1, 1, t.hour, t.minute) - timedelta(minutes=1)
    return dummy.time()


def _is_session_start(idx: int, ranges: list[tuple[time, time]]) -> bool:
    """第 idx 段是否为新的交易会话（开盘有集合竞价）
    两段间隔 > 3h = 新会话（日盘/夜盘交界）；间隔 ≤ 3h = 盘中间歇，无竞价
    """
    if idx == 0:
        return True
    prev_end = ranges[idx - 1][1]
    curr_start = ranges[idx][0]
    prev_m = prev_end.hour * 60 + prev_end.minute
    curr_m = curr_start.hour * 60 + curr_start.minute
    gap = (24 * 60 - prev_m) + curr_m if curr_m <= prev_m else curr_m - prev_m
    return gap > 180


def is_trading_time(symbol: str, dt: datetime) -> bool:
    """判断 tick 时间是否在交易时段内"""
    pid = _extract_product_id(symbol)
    if not pid or pid not in _TRADING_SESSIONS:
        return True
    ranges = _TRADING_SESSIONS[pid]
    if not ranges:
        return True
    t = dt.time()
    for start, end in ranges:
        end_inc = time(end.hour, end.minute, 59, 999999)
        if start <= end:
            if start <= t <= end_inc:
                return True
        else:
            if t >= start or t <= end_inc:
                return True
    return False


def is_auction_time(symbol: str, dt: datetime) -> bool:
    """判断 tick 是否在开盘前 1 分钟的集合竞价时段内
    只对每个交易会话的第一个段判断（10:30 小节恢复、13:30 午盘均无竞价）
    """
    pid = _extract_product_id(symbol)
    if not pid or pid not in _TRADING_SESSIONS:
        return False
    ranges = _TRADING_SESSIONS[pid]
    if not ranges:
        return False
    t = dt.time()
    for i, (start, _) in enumerate(ranges):
        if not _is_session_start(i, ranges):
            continue
        auction_start = _time_subtract_minute(start)
        if auction_start <= start:
            if auction_start <= t < start:
                return True
        else:
            if t >= auction_start or t < start:
                return True
    return False


def get_auction_session_start(symbol: str, dt: datetime) -> Optional[datetime]:
    """返回竞价 tick 对应的开盘时间（秒=0，微秒=0）"""
    pid = _extract_product_id(symbol)
    if not pid or pid not in _TRADING_SESSIONS:
        return None
    ranges = _TRADING_SESSIONS[pid]
    if not ranges:
        return None
    t = dt.time()
    for i, (start, _) in enumerate(ranges):
        if not _is_session_start(i, ranges):
            continue
        auction_start = _time_subtract_minute(start)
        if auction_start <= start:
            if auction_start <= t < start:
                return dt.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        else:
            if t >= auction_start or t < start:
                return dt.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    return None


# ═══════════════════════════════════════════════════════════
# 第二部分：增强版 BarGenerator
# ═══════════════════════════════════════════════════════════

class MyBarGenerator(_BarGenerator):
    """增强版 BarGenerator：竞价处理 + 交易时段过滤 + 跨段 volume 正确计算"""

    def __init__(
        self,
        on_bar,
        window: int = 0,
        on_window_bar=None,
        interval: Interval = Interval.MINUTE,
        daily_end=None,
        enable_trading_filter: bool = True,
    ):
        super().__init__(on_bar, window, on_window_bar, interval, daily_end)
        self.enable_trading_filter: bool = enable_trading_filter

    def update_tick(self, tick: TickData) -> None:
        """重写 update_tick"""
        new_minute: bool = False

        if not tick.last_price:
            return

        if self.enable_trading_filter and is_auction_time(tick.symbol, tick.datetime):
            new_dt = get_auction_session_start(tick.symbol, tick.datetime)
            if new_dt:
                tick.datetime = new_dt

        elif self.enable_trading_filter and not is_trading_time(tick.symbol, tick.datetime):
            return

        if not self.bar:
            new_minute = True
        elif (
            self.bar.datetime.minute != tick.datetime.minute
            or self.bar.datetime.hour != tick.datetime.hour
        ):
            self.bar.datetime = self.bar.datetime.replace(second=0, microsecond=0)
            self.on_bar(self.bar)
            new_minute = True

        if new_minute:
            self.bar = BarData(
                symbol=tick.symbol,
                exchange=tick.exchange,
                interval=Interval.MINUTE,
                datetime=tick.datetime,
                gateway_name=tick.gateway_name,
                open_price=tick.last_price,
                high_price=tick.last_price,
                low_price=tick.last_price,
                close_price=tick.last_price,
                open_interest=tick.open_interest,
            )
        elif self.bar:
            self.bar.high_price = max(self.bar.high_price, tick.last_price)
            if self.last_tick and tick.high_price > self.last_tick.high_price:
                self.bar.high_price = max(self.bar.high_price, tick.high_price)
            self.bar.low_price = min(self.bar.low_price, tick.last_price)
            if self.last_tick and tick.low_price < self.last_tick.low_price:
                self.bar.low_price = min(self.bar.low_price, tick.low_price)
            self.bar.close_price = tick.last_price
            self.bar.open_interest = tick.open_interest
            self.bar.datetime = tick.datetime

        if self.last_tick and self.bar:
            gap = (tick.datetime - self.last_tick.datetime).total_seconds()
            if gap > 7200:
                self.last_tick = None

        if self.bar:
            if self.last_tick:
                self.bar.volume += max(tick.volume - self.last_tick.volume, 0)
                self.bar.turnover += max(tick.turnover - self.last_tick.turnover, 0)
            else:
                self.bar.volume += tick.volume
                self.bar.turnover += tick.turnover

        self.last_tick = tick

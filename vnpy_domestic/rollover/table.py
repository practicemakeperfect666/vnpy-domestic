"""换月复权表：读取 + 合约代码归一化 + 交易日归口（M3）。

**零依赖**：只用标准库。`vnpy_domestic/__init__.py` 会 import vnpy，所以这个
模块必须放在仓库根目录，不能放进 `vnpy_domestic/` 包里，否则 publish/diagnose
（以及任何不想装 vnpy 的场景）都会被拖进 vnpy 的依赖。

回测仓库 `vnpy_backtester/rollover_table.py` 是它的**逐字节副本**，
由 `scripts/check_contract.py` 校验 sha256。
"""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 1

# 库内后缀 / vnpy 标准后缀
EXCHANGE_DB_SUFFIX = {
    "SHFE": "SHF", "DCE": "DCE", "CZCE": "ZCE",
    "CFFEX": "CFX", "INE": "INE", "GFEX": "GFE",
}
_TO_VNPY = {"SHF": "SHFE", "ZCE": "CZCE", "CFX": "CFFEX", "GFE": "GFEX"}
LOWER_CASE_EXCHANGES = ("DCE", "SHFE", "INE", "GFEX")

# bar 归属：日盘 / 夜盘 / 凌晨
SESSION_DAY = "day"
SESSION_NIGHT = "night"
SESSION_EARLY = "early"


class RolloverTableError(ValueError):
    """表的格式/覆盖范围问题。不要吞掉这个异常。"""


# 品种改名：现行代码 -> 历史上用过的旧代码（库里的合约前缀）
# 依据库里的实际区间（旧代码停用日与新代码启用日有重叠，但没有重复月份）
PRODUCT_ALIASES: dict[str, tuple[str, ...]] = {
    "MA": ("ME",),   # 甲醇   ME -> MA  (2015-05)
    "OI": ("RO",),   # 菜籽油 RO -> OI  (2013-05)
    "ZC": ("TC",),   # 动力煤 TC -> ZC  (2016-04)
    "RI": ("ER",),   # 早籼稻 ER -> RI  (2013-05)
    "PM": ("WT",),   # 硬麦   WT -> PM  (2012-11)
}


def product_prefixes(product: str) -> tuple[str, ...]:
    """取数时要合并的合约前缀：现行代码 + 历史旧代码。"""
    key = product.upper()
    return (key, *PRODUCT_ALIASES.get(key, ()))


# ---------------------------------------------------------------------------
# 合约代码
# ---------------------------------------------------------------------------
def _split(code: str, exchange: str | None) -> tuple[str, str]:
    if "." in code:
        symbol, suffix = code.rsplit(".", 1)
        exchange = exchange or suffix
    else:
        symbol = code
    ex = (exchange or "").upper()
    return symbol, _TO_VNPY.get(ex, ex)


def _expand_two_digit_year(digits: str, today: date) -> str:
    """3 位月份 → 4 位：取当前十年，必要时进一位。"""
    decade = today.year // 10 * 10
    year = decade + int(digits[0])
    if year < today.year - 1:
        year += 10
    return f"{year % 100:02d}{digits[1:]}"


def normalize_contract_code(code: str, exchange: str | None = None, *,
                            target: str = "db", today: date | None = None) -> str:
    """合约代码在「库内格式」和「CTP 格式」之间转换。

    库内：SHFE/DCE/INE/GFEX 小写 4 位；CZCE 大写 **4 位**（实测 SR1001.ZCE）；CFFEX 大写 4 位。
    CTP ：同库内，但 **CZCE 是 3 位**（SR701）。
    """
    symbol, ex = _split(code, exchange)
    variety = "".join(c for c in symbol if not c.isdigit())
    digits = symbol[len(variety):]
    if not variety or not digits.isdigit():
        raise RolloverTableError(f"合约代码无法解析：{code!r}")

    today = today or date.today()
    if target == "db":
        if len(digits) == 3:
            digits = _expand_two_digit_year(digits, today)
        suffix = EXCHANGE_DB_SUFFIX.get(ex, ex)
    elif target == "ctp":
        if ex == "CZCE" and len(digits) == 4:
            digits = digits[1:]
        suffix = ex
    else:
        raise RolloverTableError(f"未知 target={target!r}，只支持 db/ctp")

    variety = variety.lower() if ex in LOWER_CASE_EXCHANGES else variety.upper()
    return f"{variety}{digits}.{suffix}"


# ---------------------------------------------------------------------------
# 交易日归口（全项目唯一实现）
# ---------------------------------------------------------------------------
def next_weekday(day: date) -> date:
    """下一个工作日（跳过周六周日）。"""
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def trading_day_of_session(cal_day: date, session: str) -> date:
    """bar 所属交易日。

    - 日盘（09:00–15:00）→ 当天
    - 夜盘（21:00–23:59）→ 下一个工作日
    - 凌晨（00:00–02:30）→ 前一晚所属交易日的下一个工作日

    周五夜盘属于下周一；节前交易所不发夜盘，所以节假日不用特殊处理。
    """
    if session == SESSION_DAY:
        return cal_day
    if session == SESSION_NIGHT:
        return next_weekday(cal_day)
    if session == SESSION_EARLY:
        return next_weekday(cal_day - timedelta(days=1))
    raise RolloverTableError(f"未知 session={session!r}")


# ---------------------------------------------------------------------------
# 表
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Segment:
    """一段连续行情：从 day 起用 contract，价格乘 factor。"""

    day: date
    contract: str
    factor: float


class RolloverTable:
    """查表：给定品种 + 交易日 → 用哪个合约、乘多少。"""

    def __init__(self, payload: dict) -> None:
        self._validate_header(payload)
        self.schema: int = payload["schema"]
        self.anchor: date = date.fromisoformat(payload["anchor"])
        self.data_through: date = date.fromisoformat(payload["data_through"])
        self.generated_at: str = payload.get("generated_at", "")
        self._segments: dict[str, list[Segment]] = {}
        self._days: dict[str, list[date]] = {}

        for product, item in payload["products"].items():
            segments = [
                Segment(date.fromisoformat(day), contract, float(factor))
                for day, contract, factor in item["segments"]
            ]
            self._validate_segments(product, segments)
            self._segments[product.upper()] = segments
            self._days[product.upper()] = [seg.day for seg in segments]

    # ---------- 加载与校验 ----------
    @classmethod
    def load(cls, path: str | Path) -> "RolloverTable":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    @staticmethod
    def _validate_header(payload: dict) -> None:
        if payload.get("schema") != SCHEMA_VERSION:
            raise RolloverTableError(
                f"schema 不支持：{payload.get('schema')!r}（本代码只认 {SCHEMA_VERSION}）"
            )
        for key in ("anchor", "data_through", "products"):
            if key not in payload:
                raise RolloverTableError(f"表缺少字段 {key}")
        if not payload["products"]:
            raise RolloverTableError("表里没有任何品种")

    def _validate_segments(self, product: str, segments: list[Segment]) -> None:
        if not segments:
            raise RolloverTableError(f"{product}: segments 为空")
        previous = None
        for seg in segments:
            if previous is not None and seg.day <= previous:
                raise RolloverTableError(f"{product}: segments 的日期必须严格递增（{seg.day}）")
            if seg.factor <= 0:
                raise RolloverTableError(f"{product}: factor 必须 > 0（{seg.day} {seg.factor}）")
            if not seg.contract:
                raise RolloverTableError(f"{product}: contract 不能为空（{seg.day}）")
            previous = seg.day
        if segments[-1].day > self.data_through:
            raise RolloverTableError(
                f"{product}: 最后一段 {segments[-1].day} 晚于 data_through {self.data_through}"
            )

    # ---------- 查询 ----------
    def products(self) -> list[str]:
        return sorted(self._segments)

    def segment_of(self, product: str, day: date) -> Segment:
        """给定交易日，返回最后一段 day <= 入参 的段。

        早于第一段 → 抛错（表没覆盖到，回测区间要检查）。
        晚于 data_through → 用最后一段（实盘就是这种情况）。
        """
        key = product.upper()
        if key not in self._segments:
            raise RolloverTableError(f"表里没有品种 {product}")
        index = bisect_right(self._days[key], day) - 1
        if index < 0:
            raise RolloverTableError(
                f"{product}: {day} 早于表的第一段 {self._days[key][0]}"
            )
        return self._segments[key][index]

    def latest(self, product: str) -> Segment:
        key = product.upper()
        if key not in self._segments:
            raise RolloverTableError(f"表里没有品种 {product}")
        return self._segments[key][-1]

    def first_day(self, product: str) -> date:
        key = product.upper()
        if key not in self._segments:
            raise RolloverTableError(f"表里没有品种 {product}")
        return self._days[key][0]

"""换月表的纯函数核心（M2 的算法部分，不碰数据库、不碰 vnpy）。

输入：每个合约的「日盘收盘」序列（day / close / oi）
输出：segments（生效交易日 / 合约 / 因子）

口径（与方案一致）：
- 判主力用**上一交易日日盘收盘 OI**，无未来函数
- 防抖：新合约连续 lead 日领先才切，切换后冷却 cooldown 日
- 交割过滤优先级高于防抖：实物交割合约在「交割月起始日 - 5 天」失去资格
- ratio = close[旧, T-1] / close[新, T-1]；缺任一 → 抛错，绝不写 1.0
- anchor 当日 factor = 1.0（固定不变，重算不会平移历史因子）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# 现金交割：可以持有到到期，不做交割月过滤
CASH_SETTLED = {"IF", "IH", "IC", "IM", "EC"}

DELIVERY_GUARD_DAYS = 5

# 换月点两侧最多容忍多少天的数据空洞：超过就不算因子（跨半年算出来是假数）
MAX_ROLL_GAP_DAYS = 60


class RolloverCoreError(ValueError):
    """数据不足以生成表（缺收盘价、合约无法解析等）。"""


@dataclass(frozen=True)
class DayClose:
    day: date
    close: float
    oi: float


def _split_code(contract: str) -> tuple[str, str]:
    """'m2609.DCE' -> ('m', '2609')；'SR701' -> ('SR', '701')；'M2609' -> ('M','2609')。

    必须先去掉交易所后缀：否则 'M2609.DCE' 的品种名会被算成 'M.DCE'，
    月份数字变成 '09.DCE'，交割过滤整条链路静默失效。
    """
    body = contract.split(".")[0]
    variety = "".join(c for c in body if not c.isdigit())
    return variety, body[len(variety):]


def delivery_month_start(contract: str, today: date | None = None) -> date | None:
    """从合约代码推交割月 1 日：m2609 -> 2026-09-01，SR701 -> 2027-01-01。

    三位码（CZCE 的 CTP 写法，如 SR701）只有年份末一位：用参考日推十年，
    不能硬编码前缀，否则 2030 年的合约会被算成 2020 年（已过期）→ 交割过滤永远拒。
    """
    _, digits = _split_code(contract)
    if len(digits) not in (3, 4) or not digits.isdigit():
        return None
    if len(digits) == 3:
        reference = (today or date.today()).year
        decade = reference // 10 * 10
        year = decade + int(digits[0])
        if year < reference - 1:
            year += 10
        digits = f"{year % 100:02d}{digits[1:]}"
    year, month = 2000 + int(digits[:2]), int(digits[2:])
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1)


def is_eligible(contract: str, day: date, product: str) -> bool:
    """交割月过滤：实物交割合约在交割月起始日前 5 天失去资格。

    现金交割不过滤。**代码解析不了就抛错**——不能静默放行，否则交割过滤
    会在不知不觉中失效（这正是之前踩过的坑）。
    """
    if product.upper() in CASH_SETTLED:
        return True
    start = delivery_month_start(contract)
    if start is None:
        raise RolloverCoreError(f"合约代码无法解析交割月：{contract!r}")
    return day < start - timedelta(days=DELIVERY_GUARD_DAYS)


def build_daily(rows) -> dict[str, dict[date, DayClose]]:
    """rows: [(contract, day, close, oi)]，入口已保证是「日盘最后一根」。"""
    daily: dict[str, dict[date, DayClose]] = {}
    for contract, day, close, oi in rows:
        daily.setdefault(contract, {})[day] = DayClose(day, float(close), float(oi))
    return daily


def calendar_of(daily: dict[str, dict[date, DayClose]]) -> list[date]:
    """交易日历由数据自身推导：任一合约日盘有 bar 的日期。"""
    days: set[date] = set()
    for series in daily.values():
        days.update(series)
    return sorted(days)


def pick_raw_main(daily: dict[str, dict[date, DayClose]], days: list[date],
                  product: str, ignored: set[str] | None = None) -> dict[date, str]:
    """每个交易日的"原始主力"：用上一交易日日盘收盘 OI 排名。

    候选必须**当天也有数据**——已经停牌/退市的合约不能再当主力，
    否则后面算换月因子时取不到它的收盘价。
    解析不出交割月的合约跳过并记进 ignored（不让一个坏码毁掉整个品种）。
    """
    raw: dict[date, str] = {}
    for index, day in enumerate(days):
        source = days[index - 1] if index else day
        pool: list[str] = []
        for contract, series in daily.items():
            if day not in series:
                continue
            try:
                if not is_eligible(contract, day, product):
                    continue
            except RolloverCoreError:
                if ignored is not None:
                    ignored.add(contract)
                continue
            pool.append(contract)
        if not pool:
            continue
        # 优先用上一日收盘 OI；数据有断层时退回当日 OI
        ranked = [(c, (daily[c].get(source) or daily[c][day]).oi) for c in pool]
        raw[day] = max(ranked, key=lambda item: item[1])[0]
    return raw


def apply_debounce(raw: dict[date, str], days: list[date], product: str,
                   daily: dict[str, dict[date, DayClose]] | None = None,
                   lead_days: int = 4, cooldown_days: int = 4) -> dict[date, str]:
    """防抖 + 交割兜底：当前主力一旦过期或当天没数据，当天立即切走。"""
    out: dict[date, str] = {}
    current: str | None = None
    candidate: str | None = None
    count = 0
    cooldown = 0

    for day in days:
        target = raw.get(day)

        # 当前主力当天没有 bar（停牌/退市）→ 立即切，不能继续扛
        if (current is not None and daily is not None
                and day not in daily.get(current, {})):
            if target is not None:
                current = target
                candidate, count, cooldown = None, 0, cooldown_days
                out[day] = current
                continue

        if target is None:
            if current is not None:
                out[day] = current
            continue

        if current is None:
            current = target
            out[day] = current
            continue

        if not is_eligible(current, day, product):
            current = target                      # 过期：立即切，跳过防抖
            candidate, count, cooldown = None, 0, cooldown_days
            out[day] = current
            continue

        if cooldown > 0:
            cooldown -= 1
            out[day] = current
            continue

        if target == current:
            candidate, count = None, 0
        else:
            if target == candidate:
                count += 1
            else:
                candidate, count = target, 1
            if count >= lead_days:
                current = candidate
                candidate, count, cooldown = None, 0, cooldown_days

        out[day] = current

    return out


def _close_before(series: dict[date, DayClose], day: date) -> DayClose | None:
    """该合约在 day（含）之前最近一个有收盘价的交易日。"""
    best = None
    for d, row in series.items():
        if d <= day and (best is None or d > best.day):
            best = row
    return best


def _close_after(series: dict[date, DayClose], day: date) -> DayClose | None:
    """该合约在 day（含）之后最近一个有收盘价的交易日。"""
    best = None
    for d, row in series.items():
        if d >= day and (best is None or d < best.day):
            best = row
    return best


def compute_segments(daily: dict[str, dict[date, DayClose]], main: dict[date, str],
                     days: list[date], anchor: date, product: str = "") -> list[list]:
    """按换月点累积 factor，并把 anchor 当日归一为 1.0。"""
    segments: list[list] = []
    factor = 1.0
    previous_contract: str | None = None
    previous_day: date | None = None

    for day in days:
        contract = main.get(day)
        if contract is None:
            continue

        if previous_contract is None:
            segments.append([day, contract, factor])
        elif contract != previous_contract:
            old_series = daily.get(previous_contract, {})
            new_series = daily.get(contract, {})

            old_row = old_series.get(previous_day)
            new_row = new_series.get(previous_day)

            if old_row is None or new_row is None:
                # 换月点两侧有洞（旧合约先停、新合约后开）：用两侧最近的收盘价，
                # 但洞太大就不算——跨半年算出来的因子是假的。
                old_row = old_row or _close_before(old_series, previous_day)
                new_row = new_row or _close_after(new_series, previous_day)
                if old_row is None or new_row is None:
                    raise RolloverCoreError(
                        f"{day} 换月 {previous_contract}->{contract} 取不到收盘价"
                    )
                gap = max((previous_day - old_row.day).days,
                          (new_row.day - previous_day).days)
                if gap > MAX_ROLL_GAP_DAYS:
                    raise RolloverCoreError(
                        f"{product}: {previous_day} 换月 {previous_contract}->{contract} "
                        f"两侧数据断层 {gap} 天（旧 {old_row.day} / 新 {new_row.day}）"
                    )

            factor *= old_row.close / new_row.close
            segments.append([day, contract, factor])

        previous_contract, previous_day = contract, day

    # anchor 归一：anchor 当日所在的段因子置为 1.0
    anchor_factor = None
    for segment in segments:
        if segment[0] <= anchor:
            anchor_factor = segment[2]
        else:
            break
    if anchor_factor:
        # 保留 10 位：4 位会让复权序列差 0.01 点量级，6 位差 0.003 点，
        # 而 round_to(pricetick) 对 0.003 的差异是敏感的（换月前后成交价会跳一个 tick）
        segments = [[d, c, round(f / anchor_factor, 10)] for d, c, f in segments]
    return segments


def build_product(daily: dict[str, dict[date, DayClose]], product: str, *,
                  anchor: date, lead_days: int = 4,
                  cooldown_days: int = 4) -> dict:
    """一个品种：daily → {"segments": [...], "data_through": date}。"""
    days = sorted({day for series in daily.values() for day in series})
    if not days:
        raise RolloverCoreError(f"{product}: 没有任何日盘数据")

    ignored: set[str] = set()
    raw = pick_raw_main(daily, days, product, ignored)
    if not raw:
        raise RolloverCoreError(f"{product}: 没有任何合约有可用 OI")

    main = apply_debounce(raw, days, product, daily=daily,
                          lead_days=lead_days, cooldown_days=cooldown_days)
    segments = compute_segments(daily, main, days, anchor, product)
    if not segments:
        raise RolloverCoreError(f"{product}: 生成不出 segments")
    return {"segments": segments, "data_through": days[-1],
            "ignored": sorted(ignored)}

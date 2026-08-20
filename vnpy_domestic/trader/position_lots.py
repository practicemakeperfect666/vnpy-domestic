"""持仓批次队列 + 平仓匹配规则（覆盖全部 6 个交易所）。

平仓顺序规则：
- DCE/CZCE/GFEX：先开先平（FIFO）
- CFFEX：先平今仓，再平昨仓（2015 股灾后平今手续费高，默认先平今）
- SHFE/INE：平今(CLOSETODAY)/平昨(CLOSE/CLOSEYESTERDAY)指令可选；涨跌停强制先平昨仓
"""
from datetime import datetime, timedelta

from vnpy.trader.constant import Offset


def trading_day(dt: datetime) -> str:
    """成交时间 → 交易日期字符串（ISO）。夜盘 21:00 后归属下一交易日，周末跳到周一。"""
    d = dt.date()
    if dt.hour >= 20:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def settle_close(lots: list, exchange, offset, qty: int,
                 today: str, close_price: float, direction_mult: float,
                 is_limit: bool = False) -> float:
    """按交易所规则从 lots 平掉 qty 手，原地修改 lots，返回已实现盈亏（点差）。

    lots: list of [volume, price, trading_day]（可变批次，开仓时间序）
    direction_mult: 卖平多=1，买平空=-1
    """
    ex = (exchange.value if hasattr(exchange, "value") else str(exchange)).upper()
    n = len(lots)

    if ex in ("DCE", "CZCE", "GFEX"):
        seq = list(range(n))                                    # 先开先平
    elif ex == "CFFEX":
        seq = sorted(range(n), key=lambda i: (lots[i][2] != today, i))   # 先今后昨
    elif ex in ("SHFE", "INE"):
        if is_limit:
            seq = sorted(range(n), key=lambda i: (lots[i][2] == today, i))  # 涨停强制先昨
        elif offset == Offset.CLOSETODAY:
            seq = [i for i in range(n) if lots[i][2] == today]   # 只平今
        else:
            seq = [i for i in range(n) if lots[i][2] < today]    # 平昨（CLOSE 默认）
    else:
        seq = list(range(n))

    pl = 0.0
    remaining = qty
    for i in seq:
        if remaining <= 0:
            break
        vol, price, _day = lots[i]
        take = min(vol, remaining)
        pl += (close_price - price) * take * direction_mult
        remaining -= take
        lots[i][0] = vol - take
    lots[:] = [b for b in lots if b[0] > 0]
    return pl

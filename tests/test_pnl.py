"""盈亏口径测试：结算价（逐日盯市）、浮动盈亏、VWAP 落库。"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine

from vnpy_domestic.monitor.models import Base, Settlement
from vnpy_domestic.RolloverCtaEngine.RolloverCtaEngine import (
    RolloverCtaEngine,
)
from vnpy_domestic.trader.position_lots import trading_day


class FakeContract:
    size = 10.0
    pricetick = 1.0


class FakeMainEngine:
    def get_contract(self, vt_symbol):
        return FakeContract()


class FakeMonitor:
    """只提供 engine（结算价读写）+ write 队列。"""

    def __init__(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.rows = []

    def write(self, row):
        self.rows.append(row)


class FakeNotify:
    def __init__(self):
        self.sent = []

    def send_text(self, text, *a, **k):
        self.sent.append(text)


class FakeStrategy:
    strategy_name = "S"
    vt_symbol = "m2609.DCE"

    def __init__(self, pos=2.0):
        self.pos = pos


def make_engine(pos=2.0):
    e = object.__new__(RolloverCtaEngine)
    e.main_engine = FakeMainEngine()
    e.monitor = FakeMonitor()
    e.last_prices = {"m2609.DCE": 3100.0}
    e.strategy_lots = {
        "S": [[2, 3000.0, "2026-11-08", datetime(2026, 11, 8, 9, 0)]]
    }
    e._settle_cache = {}
    e.write_log = lambda *a, **k: None
    e.account = "real"
    e.strategies = {"S": FakeStrategy(pos)}
    e.symbol_strategy_map = {"m2609.DCE": [e.strategies["S"]]}
    e.notify = FakeNotify()
    return e


def _add_settle(engine, day, price):
    with engine.monitor.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO settlements "
            "(account, symbol, trading_day, price, update_time) "
            "VALUES (?, ?, ?, ?, ?)",
            ("real", "m2609.DCE", day, price, ""),
        )


def test_settle_of_picks_latest_before_day():
    e = make_engine()
    _add_settle(e, "2026-11-06", 2980.0)
    _add_settle(e, "2026-11-08", 3050.0)

    assert e._settle_of("m2609.DCE", "2026-11-09") == 3050.0
    assert e._settle_of("m2609.DCE", "2026-11-08") == 2980.0
    assert e._settle_of("m2609.DCE", "2026-11-06") is None


def test_floating_mtm_uses_settlement_for_overnight_lots():
    e = make_engine()
    _add_settle(e, "2026-11-08", 3050.0)

    # 昨仓 2 手 @3000，昨结算 3050，最新 3100 → (3100-3050)*2*10
    assert e._floating_mtm(e.strategies["S"], "2026-11-09") == 1000.0

    # 今日开的仓按开仓价
    e.strategy_lots["S"] = [[2, 3000.0, "2026-11-09", None]]
    assert e._floating_mtm(e.strategies["S"], "2026-11-09") == 2000.0

    # 空头取反
    e.strategies["S"].pos = -2
    assert e._floating_mtm(e.strategies["S"], "2026-11-09") == -2000.0


def test_sync_settlements_fetches_and_notifies():
    """启动时从交易所取上一交易日结算价：写库 + 推手机。"""
    e = make_engine()
    today = trading_day(datetime.now())
    prev = _prev_weekday(today)

    calls = []

    def fetcher(market, day):
        calls.append((market, day))
        return [("m2609", 3090.0)]

    result = e.sync_settlements_from_api(fetcher=fetcher)

    assert result["day"] == prev
    assert result["saved"] == [("m2609.DCE", 3090.0)]
    assert calls == [("DCE", prev)]

    rows = [r for r in e.monitor.rows if isinstance(r, Settlement)]
    assert [(r.symbol, r.price) for r in rows] == [("m2609.DCE", 3090.0)]
    assert e.notify.sent and "3090" in e.notify.sent[0]


def test_sync_settlements_skips_when_present():
    """上一交易日已有数据：不再拉取，也不再推消息。"""
    e = make_engine()
    today = trading_day(datetime.now())
    _add_settle(e, _prev_weekday(today), 3090.0)

    def fetcher(market, day):
        raise AssertionError("不该再拉取")

    result = e.sync_settlements_from_api(fetcher=fetcher)

    assert result["skipped"] is True
    assert not e.notify.sent


def _prev_weekday(day):
    d = date.fromordinal(date.fromisoformat(day).toordinal() - 1)
    while d.weekday() >= 5:
        d = date.fromordinal(d.toordinal() - 1)
    return d.isoformat()

"""openctp 保证金率：解析、合约/品种回退、账户保证金估算。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vnpy.trader.constant import Direction

from vnpy_domestic.RolloverCtaEngine.RolloverCtaEngine import (
    DEFAULT_MARGIN_RATE,
    RolloverCtaEngine,
)
from vnpy_domestic.trader.position_lots import trading_day

# 模拟 http://dict.openctp.cn/instruments?types=futures 的 data 列表：
# TA 只配了空头，用来验证单边兜底；SR 多空不同，用来验证方向取率。
DATA = [
    {
        "InstrumentID": "m2701",
        "ExchangeID": "DCE",
        "ProductID": "m",
        "LongMarginRatioByMoney": 0.08,
        "ShortMarginRatioByMoney": 0.08,
    },
    {
        "InstrumentID": "SR701",
        "ExchangeID": "CZCE",
        "ProductID": "SR",
        "LongMarginRatioByMoney": 0.06,
        "ShortMarginRatioByMoney": 0.07,
    },
    {
        "InstrumentID": "TA701",
        "ExchangeID": "CZCE",
        "ProductID": "TA",
        "LongMarginRatioByMoney": 0.0,
        "ShortMarginRatioByMoney": 0.09,
    },
]


class FakeContract:
    size = 10.0


class FakePosition:
    vt_symbol = "m2701.DCE"
    direction = Direction.LONG
    volume = 2.0
    price = 3000.0
    pnl = 100.0


class FakeMainEngine:
    def get_all_positions(self):
        return [FakePosition()]

    def get_contract(self, vt_symbol):
        return FakeContract()


class FakeMonitor:
    def __init__(self):
        self.rows = []

    def write(self, row):
        self.rows.append(row)


def make_engine() -> RolloverCtaEngine:
    engine = object.__new__(RolloverCtaEngine)  # 跳过 __init__，不连 CTP
    engine._margin_rates = {}
    engine._margin_product_rates = {}
    engine._margin_loaded_at = 0.0
    engine.strategies = {}
    engine.monitor = None
    engine.logs = []
    engine.write_log = lambda msg, strategy=None: engine.logs.append(msg)
    return engine


def test_sync_margin_rates_parses_contract_and_product_fallback():
    engine = make_engine()
    result = engine.sync_margin_rates(fetcher=lambda: DATA)

    assert result == {"contracts": 3, "products": 3}
    assert engine._margin_rate_of("m2701.DCE", Direction.LONG) == 0.08
    assert engine._margin_rate_of("SR701.CZCE", Direction.LONG) == 0.06
    assert engine._margin_rate_of("SR701.CZCE", Direction.SHORT) == 0.07
    # 只配了空头 → 多头用空头兜底
    assert engine._margin_rate_of("TA701.CZCE", Direction.LONG) == 0.09
    # 合约不在字典 → 品种中位数
    assert engine._margin_rate_of("m2799.DCE", Direction.LONG) == 0.08
    # 品种也没有 → None（调用方决定兜底）
    assert engine._margin_rate_of("zz9999.DCE", Direction.LONG) is None


def test_sync_margin_rates_failure_keeps_previous_cache():
    engine = make_engine()
    engine.sync_margin_rates(fetcher=lambda: DATA)

    def boom():
        raise RuntimeError("openctp down")

    result = engine.sync_margin_rates(fetcher=boom)

    assert result["contracts"] == 0
    assert "openctp down" in result["error"]
    # 旧缓存不能被失败请求清空
    assert engine._margin_rate_of("m2701.DCE", Direction.LONG) == 0.08
    assert any("加载失败" in msg for msg in engine.logs)


def test_write_account_positions_uses_openctp_margin_rate():
    engine = make_engine()
    engine.main_engine = FakeMainEngine()
    engine.monitor = FakeMonitor()
    engine.account = "real"
    engine._accountid = "test"
    engine._last_balance = 100000.0
    engine._last_available = 90000.0
    engine._last_frozen = 10000.0
    engine._today_trading_day = trading_day(datetime.now())
    engine._today_realized_pl = 0.0
    engine.sync_margin_rates(fetcher=lambda: DATA)

    engine._write_account_positions()

    account_rows = [r for r in engine.monitor.rows if type(r).__name__ == "Account"]
    assert account_rows
    # 2 手 × 3000 × 乘数 10 × 8% = 4800
    assert account_rows[-1].margin == 4800.0


def test_account_margin_falls_back_when_rates_not_loaded():
    engine = make_engine()
    engine.main_engine = FakeMainEngine()
    engine.monitor = FakeMonitor()
    engine.account = "real"
    engine._accountid = "test"
    engine._last_balance = 100000.0
    engine._last_available = 90000.0
    engine._last_frozen = 10000.0
    engine._today_trading_day = trading_day(datetime.now())
    engine._today_realized_pl = 0.0

    engine._write_account_positions()

    account_rows = [r for r in engine.monitor.rows if type(r).__name__ == "Account"]
    assert account_rows
    # 没有 openctp 数据时按兜底 8% 估算
    assert account_rows[-1].margin == 2 * 3000 * 10 * DEFAULT_MARGIN_RATE

"""同一合约跑多个策略时的移仓归属测试（策略级持仓归属表）。

背景：CTP 持仓没有策略标签，`_ctp_position(vt_symbol)` 返回的是账户总量。
同合约两个策略时，如果用账户总量决定移仓手数，会把别的策略的仓一起搬走。
所以移仓手数必须以 `strategy_lots`（本策略批次）为准。
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_rollover_migrate import FakePosition, make_engine, run
from vnpy.trader.constant import Direction, Offset

LOT_DAY = "2026-11-14"          # 早于 today → 算昨仓


class MultiStrategySameProductTest(unittest.TestCase):
    def _setup(self, account_volume: float, lot_volume: float, pos: float):
        engine, strategy = make_engine(
            fills=[lot_volume, lot_volume],
            positions=[FakePosition("m2609.DCE", Direction.LONG, account_volume,
                                    yd_volume=account_volume, price=3000.0)],
            pos=pos,
        )
        engine.strategy_lots[strategy.strategy_name] = [
            [float(lot_volume), 3000.0, LOT_DAY, datetime(2026, 11, 14, 9, 0)]
        ]
        return engine, strategy

    # ── 核心：开仓/平仓只用本策略手数 ──
    def test_opens_and_closes_only_own_lots(self):
        # 账户 5 手 = 本策略 2 手 + 另一个策略 3 手
        engine, strategy = self._setup(account_volume=5, lot_volume=2, pos=2)
        result = run(engine, strategy, new="m2701.DCE")

        self.assertTrue(result)
        opens = [o for o in engine.main_engine.sent if o["offset"] == Offset.OPEN]
        closes = [o for o in engine.main_engine.sent if o["offset"] != Offset.OPEN]
        self.assertEqual(sum(o["volume"] for o in opens), 2, "新合约只该开 2 手")
        self.assertEqual(sum(o["volume"] for o in closes), 2, "旧合约只该平 2 手")

        old = [p for p in engine.main_engine.positions if p.vt_symbol == "m2609.DCE"]
        self.assertEqual(sum(p.volume for p in old), 3, "另一个策略的 3 手不能动")

        new = [p for p in engine.main_engine.positions if p.vt_symbol == "m2701.DCE"]
        self.assertEqual(sum(p.volume for p in new), 2)

    # ── 归属错乱（账户少于本策略）+ 必须中止，不能乱下单 ──
    def test_aborts_when_account_less_than_own_lots(self):
        engine, strategy = self._setup(account_volume=1, lot_volume=2, pos=2)
        result = run(engine, strategy, new="m2701.DCE")

        self.assertFalse(result)
        self.assertEqual(engine.main_engine.sent, [], "归属错乱时一单都不能下")
        self.assertIn("归属不一致", engine.notify.joined())

    # ── 没有批次记录时退回账户口径（单策略场景，保持原行为）──
    def test_falls_back_to_account_when_no_lots(self):
        engine, strategy = make_engine(
            fills=[3, 3],
            positions=[FakePosition("m2609.DCE", Direction.LONG, 3, 3, 3000.0)],
            pos=3,
        )
        engine.strategy_lots = {}
        result = run(engine, strategy, new="m2701.DCE")

        self.assertTrue(result)
        opens = [o for o in engine.main_engine.sent if o["offset"] == Offset.OPEN]
        self.assertEqual(sum(o["volume"] for o in opens), 3)

    # ── 归属表本身：手数 / 昨仓 ──
    def test_strategy_position_counts_volume_and_yesterday(self):
        engine, strategy = make_engine(fills=[], positions=[], pos=3)
        # 用极远/极近的日期，避开"当前是否夜盘"对 trading_day 的影响
        engine.strategy_lots[strategy.strategy_name] = [
            [1.0, 3000.0, "2020-01-01", datetime(2020, 1, 1, 9, 0)],   # 一定算昨仓
            [2.0, 3010.0, "2099-01-01", datetime(2099, 1, 1, 9, 0)],   # 一定算今仓
        ]
        total, yest = engine._strategy_position(strategy.strategy_name)
        self.assertEqual(total, 3.0)
        self.assertEqual(yest, 1.0)


if __name__ == "__main__":
    unittest.main()

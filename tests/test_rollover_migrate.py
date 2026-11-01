"""换月移仓的状态机测试（不连 CTP，不需要 vnpy 的 MainEngine）。

用 `object.__new__` 造一个"裸"引擎对象，把 CTP 相关的东西全换成假对象，
只跑 `_migrate_position_worker` 的真实逻辑，覆盖六种收场：

    1. CTP 无持仓          → 走空仓换月
    2. 全成                → 切合约、解冻、strategy.pos 对齐
    3. 开新仓未成交        → 回滚（其实没仓可回），冷却 5 分钟
    4. 开新成了、平旧没成  → 阶段 A：平掉新仓回滚
    5. 平旧只成一半        → 阶段 B：继续补平，补完算成功
    6. 平旧补不平          → 停策略 + 报"策略外账"，冷却 1 小时

    cd vnpy-domestic && python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vnpy.trader.constant import Direction, Exchange, Offset, Status  # noqa: E402

from vnpy_domestic.RolloverCtaEngine.RolloverCtaEngine import RolloverCtaEngine  # noqa: E402


class FakeTick:
    def __init__(self, bid=3089, ask=3090, last=3089.5, up=0.0, down=0.0):
        self.bid_price_1 = bid
        self.ask_price_1 = ask
        self.last_price = last
        self.limit_up = up
        self.limit_down = down


class FakeContract:
    pricetick = 1.0
    gateway_name = "CTP"

    def __init__(self, symbol="m2609", exchange=Exchange.DCE):
        self.symbol = symbol
        self.exchange = exchange


class FakePosition:
    def __init__(self, vt_symbol, direction, volume, yd_volume=0.0):
        self.vt_symbol = vt_symbol
        self.direction = direction
        self.volume = volume
        self.yd_volume = yd_volume


class FakeOrder:
    def __init__(self, traded, status):
        self.traded = traded
        self.status = status


class FakeMainEngine:
    """按剧本回放：fills = 每次 send_order 的成交手数（0 表示不成交）。"""

    def __init__(self, positions=None, fills=(), tick=None):
        self.positions = list(positions or [])
        self.fills = list(fills)
        self.tick = tick or FakeTick()
        self.orders: dict[str, FakeOrder] = {}
        self.sent: list[dict] = []
        self._seq = 0

    def get_all_positions(self):
        return self.positions

    def get_tick(self, vt_symbol):
        return self.tick

    def get_contract(self, vt_symbol):
        if "." not in vt_symbol:
            return None
        symbol, exchange = vt_symbol.split(".")
        return FakeContract(symbol, Exchange(exchange))

    def send_order(self, req, gateway_name):
        self._seq += 1
        vt_orderid = f"ord{self._seq}"
        filled = self.fills.pop(0) if self.fills else 0
        self.orders[vt_orderid] = FakeOrder(
            filled, Status.ALLTRADED if filled else Status.NOTTRADED)
        self.sent.append({"id": vt_orderid, "symbol": req.symbol, "direction": req.direction,
                          "offset": req.offset, "volume": req.volume, "price": req.price})
        return vt_orderid

    def get_order(self, vt_orderid):
        return self.orders.get(vt_orderid)


class FakeNotify:
    def __init__(self):
        self.messages: list[str] = []

    def send_text(self, msg, *args, **kwargs):
        self.messages.append(msg)

    def joined(self) -> str:
        return "\n".join(self.messages)


class FakeStrategy:
    def __init__(self, name="dual_ma", vt_symbol="m2609.DCE", pos=3, trading=True):
        self.strategy_name = name
        self.vt_symbol = vt_symbol
        self.pos = pos
        self.trading = trading
        self.inited = True


def make_engine(fills, positions=None, pos=3, tick=None) -> tuple:
    engine = object.__new__(RolloverCtaEngine)      # 跳过 __init__（不需要真引擎）
    engine.main_engine = FakeMainEngine(positions or [], fills, tick)
    engine.notify = FakeNotify()
    engine._migrating = set()
    engine._rollover_failed_until = {}
    engine._last_rollover_check = 0.0
    engine._main_contract_cache = {}
    engine.rollover_slippage_ticks = 3
    engine.order_send_times = {}
    engine.strategies = {}
    engine._pending_switch = None
    engine.order_cancel_times = {}
    engine.logs: list[str] = []
    engine.actions: list[tuple] = []

    engine.write_log = lambda msg, strategy=None: engine.logs.append(msg)
    engine.cancel_all = lambda name: engine.actions.append(("cancel_all", name))
    engine.stop_strategy = lambda name: engine.actions.append(("stop", name))
    engine.start_strategy = lambda name: engine.actions.append(("start", name))

    def _switch_symbol(strategy, new_vt_symbol, was_trading):
        engine.actions.append(("switch", new_vt_symbol))
        strategy.vt_symbol = new_vt_symbol
    engine._switch_symbol = _switch_symbol

    # 真实 _wait_traded 会 sleep 轮询；测试里直接按假订单的成交量判定
    def _wait_traded(order_ids, expect, timeout=3.0):
        return sum(engine.main_engine.get_order(vid).traded for vid in order_ids) >= expect
    engine._wait_traded = _wait_traded

    strategy = FakeStrategy(pos=pos)
    return engine, strategy


def long_positions(volume=3, yd=0.0, vt="m2609.DCE"):
    return [FakePosition(vt, Direction.LONG, volume, yd)]


def run(engine, strategy, new="m2701.DCE", qty=3):
    engine._migrating.add(strategy.strategy_name)
    engine.strategies[strategy.strategy_name] = strategy
    result = engine._migrate_position_worker(strategy, new)
    # 切换由事件线程执行（定时器里），测试里同步跑一次等价于下一个 tick
    if getattr(engine, "_pending_switch", None):
        engine._apply_pending_switch()
    return result


class NoPositionTest(unittest.TestCase):
    def test_flat_goes_to_plain_rollover(self):
        engine, strategy = make_engine(fills=[], positions=[], pos=0)
        result = run(engine, strategy)
        self.assertTrue(result)
        self.assertIn(("switch", "m2701.DCE"), engine.actions)
        self.assertEqual(engine._migrating, set())
        self.assertIn("没有 CTP 持仓", engine.notify.joined())


class SuccessTest(unittest.TestCase):
    def test_full_migration(self):
        # 1 次开新（3 手）+ 1 次平旧（3 手）
        engine, strategy = make_engine(fills=[3, 3], positions=long_positions(3))
        result = run(engine, strategy)

        self.assertTrue(result)
        self.assertIn("✅ 换月移仓完成", engine.notify.joined())
        self.assertIn(("switch", "m2701.DCE"), engine.actions)
        self.assertEqual(strategy.vt_symbol, "m2701.DCE")
        self.assertEqual(strategy.pos, 3)
        self.assertEqual(engine._migrating, set())

        open_order, close_order = engine.main_engine.sent[0], engine.main_engine.sent[1]
        self.assertEqual(open_order["symbol"], "m2701")
        self.assertEqual(open_order["offset"], Offset.OPEN)
        self.assertEqual(close_order["symbol"], "m2609")
        self.assertEqual(close_order["offset"], Offset.CLOSE)
        # 超价 3 跳：买 3090+3、卖 3089-3
        self.assertEqual(open_order["price"], 3093)
        self.assertEqual(close_order["price"], 3086)


class OpenFailsTest(unittest.TestCase):
    def test_no_fill_no_rollback(self):
        engine, strategy = make_engine(fills=[0], positions=long_positions(3))
        result = run(engine, strategy)

        self.assertFalse(result)
        self.assertEqual(engine._migrating, set())
        self.assertEqual(len(engine.main_engine.sent), 1)      # 只有开仓那一单
        self.assertEqual(strategy.vt_symbol, "m2609.DCE")      # 合约没动
        self.assertGreater(engine._rollover_failed_until["dual_ma"], 0)


class PhaseARollbackTest(unittest.TestCase):
    def test_open_ok_close_fails_rollback_new(self):
        # 开新成交 3 手，平旧 0 手，回滚平新 3 手
        engine, strategy = make_engine(fills=[3, 0, 3], positions=long_positions(3))
        result = run(engine, strategy)

        self.assertFalse(result)
        self.assertIn("↩️ 已回滚", engine.notify.joined())
        self.assertEqual(strategy.vt_symbol, "m2609.DCE")
        self.assertEqual(engine._migrating, set())
        rollback = engine.main_engine.sent[2]
        self.assertEqual(rollback["symbol"], "m2701")
        self.assertEqual(rollback["direction"], Direction.SHORT)
        self.assertEqual(rollback["volume"], 3)


class PhaseBContinueTest(unittest.TestCase):
    def test_partial_close_then_retry_succeeds(self):
        # 上期所 3 手：平昨 2 手成功、平今 1 手失败 → 补平 1 手成功
        positions = [FakePosition("m2609.SHFE", Direction.LONG, 3, yd_volume=2)]
        engine, strategy = make_engine(fills=[3, 2, 0, 1], positions=positions)
        strategy.vt_symbol = "m2609.SHFE"
        result = run(engine, strategy, new="m2701.SHFE")

        self.assertTrue(result)
        self.assertIn("继续补平", engine.notify.joined())
        self.assertIn("✅ 换月移仓完成", engine.notify.joined())
        self.assertEqual(strategy.vt_symbol, "m2701.SHFE")
        old_offsets = [o["offset"] for o in engine.main_engine.sent if o["symbol"] == "m2609"]
        self.assertEqual(old_offsets, [Offset.CLOSEYESTERDAY, Offset.CLOSETODAY, Offset.CLOSE])


class PhaseBGiveUpTest(unittest.TestCase):
    def test_partial_close_cannot_finish_stops_strategy(self):
        # 平昨 2 手成功，平今 1 手失败，补平 3 次也失败 → 人工接管
        positions = [FakePosition("m2609.SHFE", Direction.LONG, 3, yd_volume=2)]
        engine, strategy = make_engine(fills=[3, 2, 0, 0, 0, 0], positions=positions)
        strategy.vt_symbol = "m2609.SHFE"
        result = run(engine, strategy, new="m2701.SHFE")

        self.assertFalse(result)
        joined = engine.notify.joined()
        self.assertIn("需要人工接管", joined)
        self.assertIn(("stop", "dual_ma"), engine.actions)
        self.assertEqual(strategy.vt_symbol, "m2609.SHFE")     # 没切合约
        self.assertEqual(engine._migrating, set())
        self.assertGreater(engine._rollover_failed_until["dual_ma"], 0)
        # 关键：绝不回滚新仓（否则会制造反向敞口）
        new_side = [o for o in engine.main_engine.sent
                    if o["symbol"] == "m2701" and o["offset"] != Offset.OPEN]
        self.assertEqual(new_side, [])


class LockedPositionTest(unittest.TestCase):
    def test_both_sides_skipped(self):
        positions = [FakePosition("m2609.DCE", Direction.LONG, 1),
                     FakePosition("m2609.DCE", Direction.SHORT, 1)]
        engine, strategy = make_engine(fills=[], positions=positions)
        result = run(engine, strategy)

        self.assertFalse(result)
        self.assertIn("多空双向持仓", engine.notify.joined())
        self.assertEqual(engine.main_engine.sent, [])
        self.assertEqual(engine._migrating, set())


if __name__ == "__main__":
    unittest.main()




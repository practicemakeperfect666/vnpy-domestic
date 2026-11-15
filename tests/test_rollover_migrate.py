"""换月移仓的状态机测试（不连 CTP，不需要 vnpy 的 MainEngine）。

用 `object.__new__` 造一个"裸"引擎对象，把 CTP 相关的东西全换成假对象，
只跑 `_migrate_position_worker` 的真实逻辑，覆盖这些收场：

    1. CTP 无持仓          → 走空仓换月
    2. 全成                → 切合约、解冻、strategy.pos 对齐、批次按新合约重建
    3. 开新仓未成交        → 回滚（其实没仓可回），冷却 5 分钟
    4. 开新仓只成一半      → 只回滚实际成交的那部分，发“部分成交”通知
    5. 开新成了、平旧没成  → 阶段 A：平掉新仓回滚
    6. 平旧只成一半        → 阶段 B：按实际成交量继续补平，补完算成功
    7. 平旧补不平          → 停策略 + 报"策略外账"，冷却 1 小时
    8. 两个策略同时移仓    → 切换队列两个都执行，不能互相覆盖
    9. 换月表过期          → 按品种按天只通知一次

    cd vnpy-domestic && python -m pytest tests -v
"""

from __future__ import annotations

import sys
import threading
import unittest
from collections import defaultdict, deque
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vnpy.trader.constant import Direction, Exchange, Offset, Status
from vnpy.trader.object import CancelRequest

from vnpy_domestic.rollover import Segment
from vnpy_domestic.RolloverCtaEngine.RolloverCtaEngine import RolloverCtaEngine


class FakeTick:
    def __init__(self, bid=3089, ask=3090, last=3089.5, up=0.0, down=0.0):
        self.bid_price_1 = bid
        self.ask_price_1 = ask
        self.last_price = last
        self.limit_up = up
        self.limit_down = down


class FakeContract:
    pricetick = 1.0
    size = 10.0
    gateway_name = "CTP"

    def __init__(self, symbol="m2609", exchange=Exchange.DCE):
        self.symbol = symbol
        self.exchange = exchange


class FakePosition:
    def __init__(self, vt_symbol, direction, volume, yd_volume=0.0, price=3000.0):
        self.vt_symbol = vt_symbol
        self.direction = direction
        self.volume = volume
        self.yd_volume = yd_volume
        self.price = price


class FakeOrder:
    def __init__(self, traded, status, price=0.0, symbol="", exchange=None,
                 gateway_name="CTP", orderid=""):
        self.traded = traded
        self.status = status
        self.price = price
        self.symbol = symbol
        self.exchange = exchange
        self.gateway_name = gateway_name
        self.datetime = None
        # 与 vnpy 的 OrderData 一致：orderid 是柜台格式（1_2_3），
        # vt_orderid 才带网关前缀（CTP.1_2_3）；撤单必须传 orderid。
        self.orderid = orderid
        self.vt_orderid = f"{gateway_name}.{orderid}" if orderid else ""

    def is_active(self):
        return self.status not in (Status.ALLTRADED, Status.CANCELLED, Status.REJECTED)

    def create_cancel_request(self):
        return CancelRequest(
            orderid=self.orderid, symbol=self.symbol, exchange=self.exchange
        )


class FakeTrade:
    def __init__(self, vt_orderid, price, volume):
        self.vt_orderid = vt_orderid
        self.price = price
        self.volume = volume


class FakeMainEngine:
    """按剧本回放：fills = 每次 send_order 的成交手数（0 表示不成交）。

    send_order 会同步更新假持仓，这样阶段 B 才能像真实环境一样按
    “当前 CTP 持仓”重算，而不是拿旧数量重复平仓。
    """

    def __init__(self, positions=None, fills=(), tick=None):
        self.positions = list(positions or [])
        self.fills = list(fills)
        self.tick = tick or FakeTick()
        self.orders: dict[str, FakeOrder] = {}
        self.orders_by_orderid: dict[str, FakeOrder] = {}
        self.trades: list[FakeTrade] = []
        self.sent: list[dict] = []
        self._seq = 0

    def get_all_positions(self):
        return self.positions

    def get_all_trades(self):
        return self.trades

    def get_tick(self, vt_symbol):
        return self.tick

    def get_contract(self, vt_symbol):
        if "." not in vt_symbol:
            return None
        symbol, exchange = vt_symbol.split(".")
        return FakeContract(symbol, Exchange(exchange))

    def send_order(self, req, gateway_name):
        self._seq += 1
        orderid = f"1_2_{self._seq}"
        vt_orderid = f"CTP.{orderid}"
        filled = self.fills.pop(0) if self.fills else 0
        status = Status.ALLTRADED if filled else Status.NOTTRADED
        order = FakeOrder(
            filled, status, req.price, req.symbol, req.exchange, orderid=orderid
        )
        self.orders[vt_orderid] = order
        self.orders_by_orderid[orderid] = order
        if filled:
            self.trades.append(FakeTrade(vt_orderid, req.price, filled))
            self._apply_fill(req, filled)
        self.sent.append({"id": vt_orderid, "symbol": req.symbol,
                          "direction": req.direction, "offset": req.offset,
                          "volume": req.volume, "price": req.price})
        return vt_orderid

    def cancel_order(self, req, gateway_name):
        # CTP 网关按 orderid 撤单（int(frontid)）；传 vt_orderid 会抛 ValueError。
        # 这里同样按 orderid 索引，才能在单测里复现"撤单传错 id"的缺陷。
        order = self.orders_by_orderid.get(req.orderid)
        if order is not None and order.is_active():
            order.status = Status.CANCELLED

    def get_order(self, vt_orderid):
        return self.orders.get(vt_orderid)

    def _apply_fill(self, req, filled):
        vt_symbol = f"{req.symbol}.{req.exchange.value}"
        if req.offset == Offset.OPEN:
            self._add_position(vt_symbol, req.direction, filled, req.price)
        else:
            side = Direction.LONG if req.direction == Direction.SHORT else Direction.SHORT
            self._reduce_position(vt_symbol, side, filled)

    def _add_position(self, vt_symbol, direction, volume, price):
        for pos in self.positions:
            if pos.vt_symbol == vt_symbol and pos.direction == direction:
                total = pos.volume + volume
                pos.price = (
                    (pos.price * pos.volume + price * volume) / total if total else price
                )
                pos.volume = total
                return
        self.positions.append(FakePosition(vt_symbol, direction, volume, 0.0, price))

    def _reduce_position(self, vt_symbol, direction, volume):
        for pos in list(self.positions):
            if pos.vt_symbol != vt_symbol or pos.direction != direction:
                continue
            take = min(pos.volume, volume)
            pos.volume -= take
            pos.yd_volume = max(0.0, pos.yd_volume - take)
            if pos.volume <= 0:
                self.positions.remove(pos)
            return


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
    engine._pending_rollover = {}
    engine.orderid_strategy_map = {}
    engine.strategy_orderid_map = defaultdict(set)
    engine._main_contract_cache = {}
    engine.rollover_slippage_ticks = 3
    engine.order_send_times = {}
    engine.strategies = {}
    engine._pending_switches = deque()
    engine._rollover_fail_notified = {}
    engine.strategy_lots = {}
    engine.strategy_cumulative_pl = {}
    engine._today_realized_pl = 0.0
    engine.last_prices = {}
    engine.account = "real"
    engine.order_cancel_times = {}
    engine.logs: list[str] = []
    engine.actions: list[tuple] = []

    engine.write_log = lambda msg, strategy=None: engine.logs.append(msg)
    engine.cancel_all = lambda strategy: engine.actions.append(
        ("cancel_all", strategy.strategy_name)
    )
    engine.stop_strategy = lambda name: engine.actions.append(("stop", name))
    engine.start_strategy = lambda name: engine.actions.append(("start", name))
    engine.sync_strategy_data = lambda strategy: None

    def _switch_symbol(strategy, new_vt_symbol, was_trading):
        engine.actions.append(("switch", new_vt_symbol))
        strategy.vt_symbol = new_vt_symbol
    engine._switch_symbol = _switch_symbol

    # 真实 _wait_traded 会 sleep 轮询；测试里直接按假订单的成交量判定
    def _wait_traded(order_ids, expect, timeout=3.0):
        traded = 0.0
        finished = True
        for vid in order_ids:
            order = engine.main_engine.get_order(vid)
            if order is None:
                finished = False
                continue
            traded += order.traded
            if order.is_active():
                finished = False
        return traded >= expect and finished
    engine._wait_traded = _wait_traded

    strategy = FakeStrategy(pos=pos)
    return engine, strategy


def long_positions(volume=3, yd=0.0, vt="m2609.DCE", price=3000.0):
    return [FakePosition(vt, Direction.LONG, volume, yd, price)]


def run(engine, strategy, new="m2701.DCE", qty=3):
    engine._migrating.add(strategy.strategy_name)
    engine.strategies[strategy.strategy_name] = strategy
    result = engine._migrate_position_worker(strategy, new)
    # 切换由事件线程执行（定时器里），测试里同步把队列消费完
    while engine._pending_switches:
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
        self.assertIn("换月移仓失败", "\n".join(engine.logs))


class PartialOpenRollbackTest(unittest.TestCase):
    def test_partial_open_rolls_back_only_filled_volume(self):
        # 开新只成 1 手（剩下已撤）→ 只回滚这 1 手，老合约 3 手不动
        engine, strategy = make_engine(fills=[1, 1], positions=long_positions(3))
        result = run(engine, strategy)

        self.assertFalse(result)
        joined = engine.notify.joined()
        self.assertIn("只成交 1/3", joined)
        self.assertIn("已回滚", joined)
        rollback = engine.main_engine.sent[1]
        self.assertEqual(rollback["symbol"], "m2701")
        self.assertEqual(rollback["direction"], Direction.SHORT)
        self.assertEqual(rollback["volume"], 1)
        # 老仓一手没动
        self.assertEqual(engine._ctp_position("m2609.DCE")[0], 3)
        self.assertEqual(strategy.vt_symbol, "m2609.DCE")


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
        # 上期所 3 手：平昨 2 手成功、平今 1 手失败 → 按实时持仓补平 1 手
        positions = [FakePosition("m2609.SHFE", Direction.LONG, 3, yd_volume=2)]
        engine, strategy = make_engine(fills=[3, 2, 0, 1], positions=positions)
        strategy.vt_symbol = "m2609.SHFE"
        result = run(engine, strategy, new="m2701.SHFE")

        self.assertTrue(result)
        joined = engine.notify.joined()
        self.assertIn("只成交 0/1 手", joined)
        self.assertIn("继续补平", joined)
        self.assertIn("✅ 换月移仓完成", joined)
        self.assertEqual(strategy.vt_symbol, "m2701.SHFE")
        old_orders = [o for o in engine.main_engine.sent if o["symbol"] == "m2609"]
        self.assertEqual(
            [o["offset"] for o in old_orders],
            [Offset.CLOSEYESTERDAY, Offset.CLOSETODAY, Offset.CLOSETODAY],
        )
        # 补平按实时持仓只补 1 手，不能拿原来的 3 手再平一次
        self.assertEqual(old_orders[-1]["volume"], 1)
        # 实际成交总量必须正好等于 3（委托里有一张 1 手被撤，未成交）
        old_ids = {o["id"] for o in old_orders}
        filled = sum(
            t.volume for t in engine.main_engine.trades if t.vt_orderid in old_ids
        )
        self.assertEqual(filled, 3)
        self.assertEqual(engine._ctp_position("m2609.SHFE")[0], 0)


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
        self.assertIn("换月移仓失败", "\n".join(engine.logs))
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


class ReconcileNoDoubleCountTest(unittest.TestCase):
    def test_reconcile_does_not_rebook_pnl(self):
        # 移仓成交已经走 process_trade_event 记过账；_reconcile 只能对账，
        # 再 settle_close 一次就是双重计账。
        engine, strategy = make_engine(
            fills=[3, 3], positions=long_positions(3, price=3000)
        )
        engine.strategy_lots["dual_ma"] = [[3, 3093.0, "2026-11-08"]]
        result = run(engine, strategy)

        self.assertTrue(result)
        self.assertEqual(engine.strategy_cumulative_pl, {})
        self.assertEqual(engine._today_realized_pl, 0.0)
        # 批次保持成交回报建的价格，不被 CTP 均价覆盖
        lots = engine.strategy_lots["dual_ma"]
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0][1], 3093.0)


class MultiPendingSwitchTest(unittest.TestCase):
    def test_two_migrations_both_switch(self):
        positions = [
            FakePosition("m2609.DCE", Direction.LONG, 3, price=3000),
            FakePosition("v2609.DCE", Direction.LONG, 2, price=5000),
        ]
        engine, _ = make_engine(fills=[3, 3, 2, 2], positions=positions)
        s1 = FakeStrategy("s1", "m2609.DCE", pos=3)
        s2 = FakeStrategy("s2", "v2609.DCE", pos=2)
        engine.strategies = {"s1": s1, "s2": s2}
        engine._migrating.update({"s1", "s2"})

        self.assertTrue(engine._migrate_position_worker(s1, "m2701.DCE"))
        self.assertTrue(engine._migrate_position_worker(s2, "v2701.DCE"))
        # 两个都在队列里，不能只留最后一个
        self.assertEqual(len(engine._pending_switches), 2)

        while engine._pending_switches:
            engine._apply_pending_switch()

        self.assertEqual(s1.vt_symbol, "m2701.DCE")
        self.assertEqual(s2.vt_symbol, "v2701.DCE")
        self.assertEqual(engine._migrating, set())


class NotifyReentrancyTest(unittest.TestCase):
    def test_failed_notification_does_not_recurse(self):
        engine = object.__new__(RolloverCtaEngine)
        engine.log_engine = None
        engine.event_engine = type(
            "FakeEventEngine", (), {"put": lambda self, event: None}
        )()
        engine.monitor = None
        engine.account = "real"
        engine._notify_local = threading.local()
        calls: list[str] = []

        class FailingNotify:
            def send_text(self, text, *args, **kwargs):
                calls.append(text)
                # 模拟 NotificationManager：发送失败时通过日志回调再写一条日志
                engine.write_log("飞书发送失败：too many request")

        engine.notify = FailingNotify()
        engine.write_log("订单失败")

        # 只发一次；失败日志不能再次触发通知
        self.assertEqual(len(calls), 1)


class NetPositionTest(unittest.TestCase):
    def test_net_long_is_not_mistaken_for_short(self):
        # DCE 净持仓：Direction.NET + 正数 volume 必须当多头
        positions = [FakePosition("m2609.DCE", Direction.NET, 3, price=3000)]
        engine, strategy = make_engine(fills=[3, 3], positions=positions)
        result = run(engine, strategy)

        self.assertTrue(result)
        self.assertEqual(engine.main_engine.sent[0]["direction"], Direction.LONG)
        self.assertIn("多 3 手", engine.notify.joined())
        self.assertEqual(strategy.pos, 3)


class StaleTableDedupeTest(unittest.TestCase):
    def test_stale_table_notifies_once_per_day(self):
        engine, _ = make_engine(fills=[], positions=[])

        class FakeTable:
            data_through = date(2020, 1, 1)

            def latest(self, product):
                return Segment(date(2020, 1, 1), "m2701", 1.0)

        engine._get_rollover_table = lambda: FakeTable()
        for _ in range(3):
            self.assertIsNone(engine._fetch_main_contract("M"))

        messages = [m for m in engine.notify.messages if "过期" in m]
        self.assertEqual(len(messages), 1)


class PendingRolloverTest(unittest.TestCase):
    def test_pending_executes_once_in_trading_window(self):
        engine, strategy = make_engine(fills=[], positions=[], pos=0)
        engine.strategies = {"dual_ma": strategy}
        engine._pending_rollover = {"dual_ma": "m2701.DCE"}
        engine._migrate_position = lambda s, n: engine.actions.append(("migrate", n))

        # 不在盘中窗口：不执行，继续挂着
        engine._in_rollover_window = lambda now=None: False
        engine._run_pending_rollovers()
        self.assertIn("dual_ma", engine._pending_rollover)
        self.assertNotIn(("migrate", "m2701.DCE"), engine.actions)

        # 盘中且新合约有行情：执行一次并出队
        engine._in_rollover_window = lambda now=None: True
        engine._run_pending_rollovers()
        self.assertEqual(engine._pending_rollover, {})
        self.assertIn(("migrate", "m2701.DCE"), engine.actions)

        # 不会重复执行
        engine._run_pending_rollovers()
        self.assertEqual(engine.actions.count(("migrate", "m2701.DCE")), 1)


if __name__ == "__main__":
    unittest.main()

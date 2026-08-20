"""多层补仓轮动策略 V3 — 双向网格
多头: RSI<entry 做多, 跌补仓, RSI>exit 平
空头: RSI>exit 做空, 涨补仓, RSI<entry 平
"""
import time
import json
from vnpy_ctastrategy import (
    CtaTemplate, TickData, BarData, TradeData, OrderData,
)
from vnpy_ctastrategy import ArrayManager
from vnpy.trader.constant import Status
from vnpy_domestic import MyBarGenerator

CMP_NONE = 0; CMP_LESS = 1; CMP_GREATER = 2
CMP_CROSSOVER = 3; CMP_CROSSUNDER = 4


class MultiLayerStrategy(CtaTemplate):
    author = "luning"

    layers = 3
    size_1 = 1; size_2 = 2; size_3 = 3
    down_2 = 0.005; down_3 = 0.008
    stoploss = 0.01
    drop_threshold = 0.05; drop_lookback = 5
    tp_mode = "fixed"; tp_pct = 0.02
    rsi_period = 14; rsi_entry = 30; rsi_exit = 70
    entry_cmp = "less"; exit_cmp = "greater"

    parameters = [
        "layers", "stoploss", "drop_threshold", "drop_lookback",
        "size_1", "size_2", "size_3",
        "down_2", "down_3",
        "tp_pct",
        "rsi_period", "rsi_entry", "rsi_exit",
        "entry_cmp", "exit_cmp",
    ]
    variables = ["pos", "layer_state", "pending_entry", "current_rsi"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.sizes = [0]
        self.step = [0, 0]
        for i in range(1, self.layers + 1):
            self.sizes.append(getattr(self, f"size_{i}", 1))
        for i in range(2, self.layers + 1):
            self.step.append(getattr(self, f"down_{i}", 0.05))

        _m = {"none": 0, "less": 1, "greater": 2, "crossover": 3, "crossunder": 4}
        self._entry_mode = _m.get(self.entry_cmp, 3)
        self._exit_mode = _m.get(self.exit_cmp, 2)

        self._opened = [False] * (self.layers + 1)
        self._entry_prices = [0.0] * (self.layers + 1)
        self._need_box = True
        self._direction = 0   # 1=long, -1=short

        # 平仓单追踪：vt_orderid → (layer, reason, timestamp)
        self._close_orders: dict[str, tuple[int, str, float]] = {}
        # 入场单追踪：vt_orderid → (layer, "long"/"short")
        self._entry_orders: dict[str, tuple[int, str]] = {}
        self._entry_bar_count = 0   # 入场单待成交 bar 数

        self._prev_close = 0.0
        self._prev_entry_val = 0.0
        self._prev_exit_val = 0.0; self._prev_exit_val2 = 0.0
        self._current_rsi: float = 50.0

    @property
    def layer_state(self):
        return json.dumps([self._opened, self._entry_prices, self._direction, self._need_box])

    @layer_state.setter
    def layer_state(self, val):
        if val:
            self._opened, self._entry_prices, self._direction, self._need_box = json.loads(val)

    @property
    def pending_entry(self):
        return len(self._entry_orders)

    @pending_entry.setter
    def pending_entry(self, val):
        pass  # derived from _entry_orders, restored via layer_state

    @property
    def current_rsi(self):
        return round(self._current_rsi, 1)

    @current_rsi.setter
    def current_rsi(self, val):
        self._current_rsi = float(val)

    def on_init(self):
        self.write_log("init")
        self.bg = MyBarGenerator(self.on_bar, enable_trading_filter=True)
        self.am = ArrayManager(150)
        self.load_bar(10)

    def on_start(self): self.write_log("start")
    def on_stop(self): self.write_log("stop")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    # ── helpers ──

    def _cmp(self, val, trig, prev, mode):
        if mode == CMP_NONE: return False
        if mode == CMP_LESS: return val < trig
        if mode == CMP_GREATER: return val > trig
        if mode == CMP_CROSSOVER: return prev <= trig and val > trig
        if mode == CMP_CROSSUNDER: return prev >= trig and val < trig
        return False

    def _entry_long(self, prev):
        return self._cmp(self._entry_val, self._entry_long_trig, prev, self._entry_mode)

    def _entry_short(self, prev):
        return self._cmp(self._entry_val, self._entry_short_trig, prev, self._exit_mode)

    def _exit_long(self, prev):
        return self._cmp(self._exit_val, self._exit_trig, prev, self._exit_mode)

    def _exit_short(self, prev):
        return self._cmp(self._exit_val, self._exit_trig, prev, self._entry_mode)

    def _exit2_long(self, prev):
        return self._cmp(self._exit2_val, self._exit2_trig, prev, CMP_CROSSUNDER)

    def _exit2_short(self, prev):
        return self._cmp(self._exit2_val, self._exit2_trig, prev, CMP_CROSSOVER)

    # ── liquidation + feishu ──

    def _notify(self, msg):
        notify = getattr(self.cta_engine, 'notify', None)
        if notify:
            notify.send_text(msg)

    def _liquidate_all(self, reason=""):
        if self.pos == 0: return
        if not self.trading: return
        # 撤掉未成交的 S2/S3 入场单
        if self._entry_orders:
            self.cancel_all()
            self._entry_orders.clear()
            self._entry_bar_count = 0
        contract = self.cta_engine.main_engine.get_contract(self.vt_symbol)
        tick = (contract.pricetick if contract else 1) * 3
        price = self._prev_close - tick if self.pos > 0 else self._prev_close + tick
        self.write_log(f"[{reason}] 全平 {self.pos}手")
        vids = self.sell(price, abs(self.pos)) if self.pos > 0 \
            else self.cover(price, abs(self.pos))
        for vid in vids:
            self._close_orders[vid] = (0, reason, time.time())
        self._notify(f"📤 平仓单已挂出\n  策略: {self.strategy_name}\n  全平 {abs(self.pos)}手 @{price}\n  原因: {reason}")

    def _liquidate_layer(self, n):
        sz = self.sizes[n]
        if not self.trading: return
        contract = self.cta_engine.main_engine.get_contract(self.vt_symbol)
        tick = (contract.pricetick if contract else 1) * 3
        price = self._prev_close - tick if self.pos > 0 else self._prev_close + tick
        tag = f"{'B' if self._direction>0 else 'S'}{n}"
        self.write_log(f"[止盈] {tag} {sz}手 @{price}")
        vids = self.sell(price, sz) if self.pos > 0 \
            else self.cover(price, sz)
        for vid in vids:
            self._close_orders[vid] = (n, "止盈", time.time())
        self._notify(f"📤 平仓单已挂出\n  策略: {self.strategy_name}\n  {tag} {sz}手 @{price}")

    # ── risk checks ──

    def _check_layer_stoploss(self, close):
        for n in range(1, self.layers + 1):
            if not self._opened[n]: continue
            ep = self._entry_prices[n]
            if self._direction > 0:
                if close < ep * (1 - self.stoploss):
                    self._liquidate_layer(n)
            else:
                if close > ep * (1 + self.stoploss):
                    self._liquidate_layer(n)

    def _check_flash(self, close):
        sz = self.am.close.size
        if sz < self.drop_lookback + 1: return False
        prev = self.am.close[-self.drop_lookback - 1]
        if prev <= 0: return False
        chg = (close - prev) / prev
        if self._direction > 0: return chg < -self.drop_threshold
        if self._direction < 0: return chg > self.drop_threshold
        return False

    def _check_take_profit(self, close):
        for n in range(1, self.layers + 1):
            if not self._opened[n]: continue
            ep = self._entry_prices[n]
            if self._direction > 0:
                if close >= ep * (1 + self.tp_pct): self._liquidate_layer(n); return
            else:
                if close <= ep * (1 - self.tp_pct): self._liquidate_layer(n); return

    # ── entry ──

    def _try_open_s1(self, close, label, entry_check):
        if self._opened[1]: return False
        if not self._need_box: return False
        if self._entry_orders: return False
        if not entry_check(self._prev_entry_val): return False

        if self.trading:
            vids = getattr(self, label)(close, self.sizes[1])
            for vid in vids:
                self._entry_orders[vid] = (1, label)
            self.write_log(f"[{label.upper()}1] 挂单 {self.sizes[1]}手 @{close}")
            self._notify(f"📥 入场挂单\n  策略: {self.strategy_name}\n  {label.upper()}1 {self.sizes[1]}手 @{close}")
        return True

    def _try_open_sn(self, n, close, label, is_long):
        if self._opened[n]: return False
        if self._entry_orders: return False
        ref = self._entry_prices[n - 1]
        if ref <= 0: return False

        if is_long:
            if close > ref: return False
            chg = (ref - close) / ref
        else:
            if close < ref: return False
            chg = (close - ref) / ref

        if chg <= self.step[n]: return False

        if self.trading:
            vids = getattr(self, label)(close, self.sizes[n])
            for vid in vids:
                self._entry_orders[vid] = (n, label)
            self.write_log(f"[{label.upper()}{n}] 挂单 {self.sizes[n]}手 @{close} {'跌' if is_long else '涨'}{chg:.2%}")
        return True

    # ── main ──

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited: return

        close = bar.close_price
        rsi = self.am.rsi(self.rsi_period)
        rsi = rsi if rsi is not None else 50
        ma20 = self.am.sma(20) or close

        self._entry_val = rsi
        self._entry_long_trig = self.rsi_entry
        self._entry_short_trig = self.rsi_exit
        self._exit_val = rsi
        self._exit_trig = self.rsi_exit if self._direction >= 0 else self.rsi_entry
        self._exit2_val = close
        self._exit2_trig = ma20

        # ── exit checks (skip if close orders pending) ──
        self._prev_close = close   # 平仓单用当前价，不用等 bar 结束
        closing = bool(self._close_orders)
        if not closing:
            if self.pos != 0:
                self._check_take_profit(close)

            if self.pos > 0 and (self._exit_long(self._prev_exit_val) or self._exit2_long(self._prev_exit_val2)):
                self._liquidate_all("箱体退出(B)")
            if self.pos < 0 and (self._exit_short(self._prev_exit_val) or self._exit2_short(self._prev_exit_val2)):
                self._liquidate_all("箱体退出(S)")

            self._check_layer_stoploss(close)
            if self._check_flash(close):
                self._liquidate_all("急变")

        # ── entry ──
        if self._entry_orders:
            self._entry_bar_count += 1
            if self._need_box and self._entry_bar_count > 120:
                self.cancel_all()
                self._entry_orders.clear()
                self._entry_bar_count = 0
                self._direction = 0
                self.write_log("[入场超时] S1撤单重置")
        elif self._need_box and self.pos == 0:
            if self._entry_long(self._prev_entry_val):
                self._direction = 1
                self._try_open_s1(close, "buy", self._entry_long)
            elif self._entry_short(self._prev_entry_val):
                self._direction = -1
                self._try_open_s1(close, "short", self._entry_short)

        elif not self._need_box and self.pos != 0:
            is_long = self._direction > 0
            label = "buy" if is_long else "short"
            for n in range(2, self.layers + 1):
                if self._try_open_sn(n, close, label, is_long):
                    break

        self._prev_entry_val = self._entry_val
        self._prev_exit_val = self._exit_val
        self._prev_exit_val2 = self._exit2_val
        self._current_rsi = rsi

    def on_order(self, order: OrderData):
        # 入场单: 仅撤单/拒单时清理，成交由 on_trade 处理
        if order.vt_orderid in self._entry_orders:
            if order.status in (Status.CANCELLED, Status.REJECTED):
                layer, label = self._entry_orders.pop(order.vt_orderid)
                self.write_log(f"[{label.upper()}{layer}] 入场单失效 status={order.status.value}")
                if layer == 1:
                    self._direction = 0
            return

        if order.vt_orderid not in self._close_orders:
            return
        layer, reason, _ = self._close_orders[order.vt_orderid]
        tag = "全平" if layer == 0 else f"{'B' if self._direction>0 else 'S'}{layer}"

        if order.status in (Status.CANCELLED, Status.REJECTED):
            self._close_orders.pop(order.vt_orderid, None)
            self.write_log(f"[{tag}] {reason} 订单失效 status={order.status.value}")

    def on_trade(self, trade: TradeData):
        # 入场单成交 → 更新层状态
        if trade.vt_orderid in self._entry_orders:
            layer, label = self._entry_orders.pop(trade.vt_orderid)
            self._opened[layer] = True
            self._entry_prices[layer] = trade.price
            self._entry_bar_count = 0
            if layer == 1:
                self._need_box = False
            self.write_log(f"[{label.upper()}{layer}] 成交 {self.sizes[layer]}手 @{trade.price}")
            return

        if trade.vt_orderid not in self._close_orders:
            return
        layer, reason, _ = self._close_orders.pop(trade.vt_orderid)
        if layer == 0:
            self._opened = [False] * (self.layers + 1)
            self._entry_prices = [0.0] * (self.layers + 1)
            self._need_box = True
            self._direction = 0
            if self._entry_orders:
                self.cancel_all()
                self._entry_orders.clear()
                self._entry_bar_count = 0
        else:
            self._opened[layer] = False
            if not any(self._opened[1:]):
                self._entry_prices = [0.0] * (self.layers + 1)
                self._need_box = True
                self._direction = 0
                if self._entry_orders:
                    self.cancel_all()
                    self._entry_orders.clear()
                    self._entry_bar_count = 0

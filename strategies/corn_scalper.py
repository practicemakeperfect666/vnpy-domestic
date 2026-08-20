"""
玉米刷盘口策略 (Corn Scalper)
MA模式: 价>MA→买一开多,填后卖一平; 价<MA→卖一开空,填后买一平
被套: 平仓价偏离盘口→撤单→开反向锁仓→锁仓单平掉→继续刷反向
      直到盘口回到成本价才挂平仓单解套(随缘)
收盘: 14:57/22:57 撤单 + 强平
多空分别记录 long_pos / short_pos，锁仓(净持仓=0)不再误判为无仓
开仓单(入场/锁仓): 盘口变薄(<min_depth)撤单等厚重挂; 平仓单: 价格反向N跳撤单追价
"""
from vnpy_ctastrategy import (
    CtaTemplate, TickData, BarData, TradeData, OrderData,
)
from vnpy_ctastrategy import ArrayManager
from vnpy.trader.constant import Direction, Offset, Status
from vnpy_domestic import MyBarGenerator
from vnpy_domestic.trader.position_lots import trading_day, settle_close


class CornScalperStrategy(CtaTemplate):
    author = "vnpy-domestic"

    ma_window = 5
    min_depth = 500
    fixed_size = 1
    deviation_ticks = 2   # 平仓单价格往不利方向偏离 N 跳才撤

    parameters = ["ma_window", "min_depth", "fixed_size", "deviation_ticks"]
    variables = ["pos", "ma_value", "mode", "long_pos", "short_pos", "_cost_price", "long_lots", "short_lots", "realized_pl"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.bg = MyBarGenerator(self.on_bar, 5, self.on_5min_bar)
        self.am = ArrayManager(max(self.ma_window + 10, 15))
        self.ma_value = 0.0
        self.mode = "ma"            # "ma" | "trapped_long" | "trapped_short"
        self.long_pos = 0           # 多头手数
        self.short_pos = 0          # 空头手数
        self.long_lots = []   # 多头批次 [[vol, price, day], ...]（FIFO）
        self.short_lots = []  # 空头批次
        self.realized_pl = 0.0      # 累计已实现盈亏
        self.last_close_pl = 0.0    # 最近一笔平仓盈亏（瞬态，供通知）
        self._cost_price = 0.0      # 入场成本价（判断"盘口回来"基准）
        self._pt = 0.0              # pricetick 缓存（on_init 时赋值，避免每 tick 查合约）
        self._entry_id: str = ""
        self._entry_cancelling: bool = False
        self._exit_id: str = ""
        self._exit_cancelling: bool = False
        self._exit_price: float = 0.0
        self._exit_watch: str = ""  # "ask" 平多看卖一 / "bid" 平空看买一
        self._lock_id: str = ""
        self._lock_cancelling: bool = False
        self._closed: bool = False

    def on_init(self):
        self.write_log("CornScalper init")
        self._pt = self._pricetick()
        self.load_bar(10, callback=self.bg.update_bar)

    def on_start(self):
        self.write_log("CornScalper start")

    def on_stop(self):
        self.write_log("CornScalper stop")

    # ── Bar ──
    def on_5min_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if self.am.inited:
            self.ma_value = float(self.am.close[-self.ma_window:].mean())

    def on_bar(self, bar: BarData):
        pass

    # ── Tick ──
    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

        if self._should_close(tick.datetime):
            if not self._closed:
                self.cancel_all()
                self._entry_id = ""
                self._exit_id = ""
                self._exit_price = 0.0
                self._exit_watch = ""
                self._lock_id = ""
                self._closed = True
                self._force_close(tick)
            return

        # 入场单：盘口变薄 → 撤单，等盘口厚重挂
        # 撤单后不清 _entry_id：若撤单撞上成交，on_trade 仍需匹配 _entry_id 记录成本价
        if self._entry_id:
            if tick.bid_volume_1 < self.min_depth or tick.ask_volume_1 < self.min_depth:
                if not self._entry_cancelling:
                    self.cancel_order(self._entry_id)
                    self._entry_cancelling = True
            return

        # 平仓单：价格反向偏离 → 撤单（不清 _exit_id，防撤单撞成交丢 mode）
        if self._exit_id:
            if self._price_deviated(self._exit_watch, self._exit_price, tick):
                if not self._exit_cancelling:
                    self.cancel_order(self._exit_id)
                    self._exit_cancelling = True
            return

        # 锁仓单：盘口变薄 → 撤单（不清 _lock_id，防撤单撞成交）
        if self._lock_id:
            if tick.bid_volume_1 < self.min_depth or tick.ask_volume_1 < self.min_depth:
                if not self._lock_cancelling:
                    self.cancel_order(self._lock_id)
                    self._lock_cancelling = True
            return

        if tick.bid_volume_1 < self.min_depth or tick.ask_volume_1 < self.min_depth:
            return

        # ── 状态机（用 long_pos/short_pos，不用 net pos）──
        if self.long_pos > 0 and self.short_pos > 0:
            self._close_lock(tick)      # 锁仓中 → 平掉锁仓单
        elif self.long_pos > 0:
            if self.mode == "trapped_long":
                if self._price_recovered(tick):
                    self._place_exit(tick)   # 盘口回来 → 挂平多解套
                else:
                    self._open_lock(tick)    # 没回来 → 继续开空锁仓
            else:
                self._place_exit(tick)  # 正常平多
        elif self.short_pos > 0:
            if self.mode == "trapped_short":
                if self._price_recovered(tick):
                    self._place_exit(tick)   # 盘口回来 → 挂平空解套
                else:
                    self._open_lock(tick)    # 没回来 → 继续开多锁仓
            else:
                self._place_exit(tick)  # 正常平空
        else:
            self.mode = "ma"
            self._try_enter_ma(tick)    # 空仓 → 按 MA 入场

    # ── Trade ──
    def on_trade(self, trade: TradeData):
        self._update_position(trade)

        if trade.vt_orderid == self._entry_id:
            self._entry_id = ""
            self._entry_cancelling = False
            self._cost_price = trade.price   # 记录入场成本价
        elif trade.vt_orderid == self._exit_id:
            self._exit_id = ""
            self._exit_cancelling = False
            self._exit_price = 0.0
            self._exit_watch = ""
            # 平锁仓单→剩单边被困; 平单边→全平重新入场
            if self.long_pos > 0:
                self.mode = "trapped_long"
            elif self.short_pos > 0:
                self.mode = "trapped_short"
            else:
                self.mode = "ma"
        elif trade.vt_orderid == self._lock_id:
            self._lock_id = ""
            self._lock_cancelling = False

    # ── Order ──
    def on_order(self, order: OrderData):
        if order.status in (Status.CANCELLED, Status.REJECTED):
            if order.vt_orderid == self._entry_id:
                self._entry_id = ""
                self._entry_cancelling = False
            elif order.vt_orderid == self._exit_id:
                self._exit_id = ""
                self._exit_cancelling = False
                self._exit_price = 0.0
                self._exit_watch = ""
                if self.long_pos > 0 or self.short_pos > 0:
                    self._mark_trapped()
            elif order.vt_orderid == self._lock_id:
                self._lock_id = ""
                self._lock_cancelling = False

    # ── Position tracking ──
    def _update_position(self, trade: TradeData):
        """按 direction + offset 分记多空手数，多空各自 FIFO 批次 + 已实现盈亏"""
        day = trading_day(trade.datetime)
        if trade.offset == Offset.OPEN:
            if trade.direction == Direction.LONG:
                self.long_lots.append([trade.volume, trade.price, day])
                self.long_pos += trade.volume
            else:
                self.short_lots.append([trade.volume, trade.price, day])
                self.short_pos += trade.volume
        else:  # CLOSE / CLOSETODAY / CLOSEYESTERDAY
            if trade.direction == Direction.SHORT:
                # 卖平多
                pl = settle_close(self.long_lots, trade.exchange, trade.offset,
                                  trade.volume, day, trade.price, 1)
                self.realized_pl += pl
                self.last_close_pl = pl
                self.long_pos -= trade.volume
            else:
                # 买平空
                pl = settle_close(self.short_lots, trade.exchange, trade.offset,
                                  trade.volume, day, trade.price, -1)
                self.realized_pl += pl
                self.last_close_pl = pl
                self.short_pos -= trade.volume
        self.long_pos = max(self.long_pos, 0)
        self.short_pos = max(self.short_pos, 0)

    # ── Enter ──
    def _try_enter_ma(self, tick: TickData):
        if not self.am.inited:
            return
        if tick.last_price > self.ma_value:
            ids = self.buy(tick.bid_price_1, self.fixed_size)
        elif tick.last_price < self.ma_value:
            ids = self.short(tick.ask_price_1, self.fixed_size)
        else:
            return
        if ids:
            self._entry_id = ids[0]

    def _open_lock(self, tick: TickData):
        """被套 → 开反向锁仓单（量 = 被困仓位）"""
        if self.mode == "trapped_long":
            ids = self.short(tick.ask_price_1, self.long_pos)
        else:
            ids = self.buy(tick.bid_price_1, self.short_pos)
        if ids:
            self._lock_id = ids[0]

    # ── Exit ──
    def _place_exit(self, tick: TickData):
        """平掉单边持仓：挂开仓价 ± 1 跳，赚价差"""
        pt = self._pricetick()
        if self.long_pos > 0:
            price = self._cost_price + pt
            ids = self.sell(price, self.long_pos)
            self._exit_price = price
            self._exit_watch = "ask"
        elif self.short_pos > 0:
            price = self._cost_price - pt
            ids = self.cover(price, self.short_pos)
            self._exit_price = price
            self._exit_watch = "bid"
        else:
            return
        if ids:
            self._exit_id = ids[0]

    def _close_lock(self, tick: TickData):
        """平掉锁仓单"""
        if self.mode == "trapped_long":
            ids = self.cover(tick.bid_price_1, self.short_pos)
            self._exit_price = tick.bid_price_1
            self._exit_watch = "bid"
        else:
            ids = self.sell(tick.ask_price_1, self.long_pos)
            self._exit_price = tick.ask_price_1
            self._exit_watch = "ask"
        if ids:
            self._exit_id = ids[0]

    # ── Price deviation（仅平仓单）──
    def _price_deviated(self, watch: str, price: float, tick: TickData) -> bool:
        """平仓挂单价往不利方向偏离 N 跳才撤（有利方向挂着等成交）"""
        if price <= 0:
            return True
        pt = self._pricetick()
        if watch == "ask":
            # 平多卖单：卖一价跌到挂单价以下 N 跳 = 价格反向
            return tick.ask_price_1 <= price - self.deviation_ticks * pt
        elif watch == "bid":
            # 平空买单：买一价涨到挂单价以上 N 跳 = 价格反向
            return tick.bid_price_1 >= price + self.deviation_ticks * pt
        return True

    def _pricetick(self) -> float:
        if self._pt > 0:
            return self._pt
        try:
            contract = self.cta_engine.main_engine.get_contract(self.vt_symbol)
            if contract and contract.pricetick:
                self._pt = contract.pricetick
                return self._pt
        except Exception:
            pass
        return 1.0

    # ── Price recovered ──
    def _price_recovered(self, tick: TickData) -> bool:
        """盘口是否回到被困成本价（可解套）"""
        if self._cost_price <= 0:
            return True
        if self.mode == "trapped_long":
            return tick.ask_price_1 >= self._cost_price
        elif self.mode == "trapped_short":
            return tick.bid_price_1 <= self._cost_price
        return True

    # ── Trapped ──
    def _mark_trapped(self):
        if self.long_pos > 0:
            self.mode = "trapped_long"
        elif self.short_pos > 0:
            self.mode = "trapped_short"

    # ── Close ──
    def _should_close(self, dt) -> bool:
        t = dt.time()
        return (t.hour == 14 and t.minute >= 57) or (t.hour == 22 and t.minute >= 57)

    def _force_close(self, tick: TickData):
        """收盘前强平，多空都平"""
        if self.long_pos > 0:
            self.sell(tick.bid_price_1, self.long_pos)
        if self.short_pos > 0:
            self.cover(tick.ask_price_1, self.short_pos)

    def get_monitor_message(self) -> str:
        lines = [
            f"\u25b8 CornScalper  ({self.strategy_name})",
            f"  合约: {self.vt_symbol}  |  净持仓: {self.pos:+d}",
            f"  多单: {int(self.long_pos)}  |  空单: {int(self.short_pos)}",
            f"  MA({self.ma_window}): {self.ma_value:.1f}  |  模式: {self.mode}",
        ]
        if hasattr(self, "am") and self.am is not None:
            lines.append(f"  缓存: {self.am.count}/{self.am.size}  |  ready={self.am.inited}")
        if hasattr(self, "bg") and self.bg is not None:
            bg = self.bg
            lines.append(f"  K线: {bg.__class__.__name__}  |  window={getattr(bg, 'window', 0)}")
        return "\n".join(lines)

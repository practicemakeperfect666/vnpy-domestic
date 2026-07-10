"""双均线策略——基于 MyBarGenerator"""

from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
)
from vnpy_ctastrategy import ArrayManager

from vnpy_domestic import MyBarGenerator


class DualMaStrategy(CtaTemplate):
    """双均线策略：快线上穿慢线开多，下穿平多"""

    author = "vnpy-domestic"

    fast_period: int = 10
    slow_period: int = 30
    fixed_size: int = 1

    parameters = ["fast_period", "slow_period", "fixed_size"]

    fast_ma: float = 0.0
    slow_ma: float = 0.0

    variables = ["fast_ma", "slow_ma"]

    def on_init(self) -> None:
        self.write_log("策略初始化")
        self.bg = MyBarGenerator(self.on_bar, enable_trading_filter=True)
        self.am = ArrayManager(size=self.slow_period + 1)
        self.load_bar(4)

    def on_start(self) -> None:
        self.write_log("策略启动")

    def on_stop(self) -> None:
        self.write_log("策略停止")

    def on_tick(self, tick: TickData) -> None:
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        fast_arr = self.am.sma(self.fast_period, array=True)
        slow_arr = self.am.sma(self.slow_period, array=True)

        self.fast_ma = fast_arr[-1]
        self.slow_ma = slow_arr[-1]

        cross_over = fast_arr[-2] <= slow_arr[-2] and fast_arr[-1] > slow_arr[-1]
        cross_below = fast_arr[-2] >= slow_arr[-2] and fast_arr[-1] < slow_arr[-1]

        if cross_over and self.pos <= 0:
            if self.trading:
                self.buy(bar.close_price + 5, self.fixed_size)
                self.write_log(f"🟢 开多信号 close={bar.close_price:.2f}")

        elif cross_below and self.pos > 0:
            if self.trading:
                self.sell(bar.close_price - 5, abs(self.pos))
                self.write_log(f"🔴 平多信号 close={bar.close_price:.2f}")

    def on_order(self, order: OrderData) -> None:
        pass

    def on_trade(self, trade: TradeData) -> None:
        self.write_log(
            f"成交 {trade.direction.value} {trade.volume}手 @{trade.price:.2f}"
        )

    def on_stop_order(self, stop_order: StopOrder) -> None:
        pass

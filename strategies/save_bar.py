from pathlib import Path
from datetime import datetime
import time

import pandas as pd

from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
)
from vnpy_domestic import MyBarGenerator


class SaveStrategy(CtaTemplate):
    """保存 K 线数据的示例策略"""

    author = "vnpy-domestic"

    # 参数
    save_bar: bool = True
    save_path: str = ".vntrader/bar_data"
    cache_size: int = 100

    parameters = ["save_bar", "save_path", "cache_size"]
    variables = []

    def on_init(self) -> None:
        self.write_log("策略初始化")
        self.bg: MyBarGenerator = MyBarGenerator(self.on_bar)
        self.bar_cache = []
        self.csv_path = None
        self.write_header = True
        self.bar_count = 0
        if self.save_bar:
            self.init_csv_file()

    def init_csv_file(self):
        try:
            save_dir = Path(self.save_path)
            if not save_dir.is_absolute():
                save_dir = Path.cwd() / save_dir
            save_dir.mkdir(parents=True, exist_ok=True)
            symbol_name = self.vt_symbol.replace(".", "_")
            date_str = datetime.now().strftime("%Y%m%d")
            file_name = f"{symbol_name}_{date_str}.csv"
            self.csv_path = save_dir / file_name
            if self.csv_path.exists():
                self.write_header = False
                self.write_log(f"📁 数据文件已存在: {self.csv_path}")
                try:
                    df = pd.read_csv(self.csv_path)
                    self.bar_count = len(df)
                except Exception:
                    pass
            else:
                self.write_header = True
                self.write_log(f"📁 创建新数据文件: {self.csv_path}")
        except Exception as e:
            self.write_log(f"❌ 初始化CSV文件失败: {e}")
            self.save_bar = False

    def on_start(self) -> None:
        self.write_log("策略启动")

    def on_stop(self) -> None:
        self.write_log("策略停止")
        if self.save_bar:
            self.flush_cache()

    def on_tick(self, tick: TickData) -> None:
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        if self.save_bar:
            self.save_bar_to_csv(bar)

    def save_bar_to_csv(self, bar: BarData):
        local_time = datetime.now()
        local_timestamp = time.time()
        row = {
            "datetime": bar.datetime.strftime("%Y-%m-%d %H:%M:%S"),
            "local_time": local_time.strftime("%Y-%m-%d %H:%M:%S"),
            "local_timestamp": int(local_timestamp * 1000),
            "open": round(bar.open_price, 2),
            "high": round(bar.high_price, 2),
            "low": round(bar.low_price, 2),
            "close": round(bar.close_price, 2),
            "volume": bar.volume,
            "turnover": bar.turnover,
            "open_interest": bar.open_interest,
        }
        self.bar_cache.append(row)
        self.bar_count += 1
        if self.bar_count % 100 == 0:
            self.write_log(
                f"💾 已保存 {self.bar_count} 条Bar, "
                f"最新行情时间: {bar.datetime.strftime('%H:%M:%S')}"
            )
        if len(self.bar_cache) >= self.cache_size:
            self.flush_cache()

    def flush_cache(self):
        if not self.bar_cache or not self.csv_path:
            return
        try:
            df = pd.DataFrame(self.bar_cache)
            mode = "a" if not self.write_header else "w"
            df.to_csv(self.csv_path, mode=mode, header=self.write_header, index=False, encoding="utf-8")
            self.bar_cache = []
            self.write_header = False
            self.write_log(f"✅ 已写入 {len(df)} 条数据到文件")
        except Exception as e:
            self.write_log(f"❌ 写入CSV失败: {e}")

    def on_order(self, order: OrderData) -> None:
        pass

    def on_trade(self, trade: TradeData) -> None:
        pass

    def on_stop_order(self, stop_order: StopOrder) -> None:
        pass

"""
使用 Akshare 作为 vnpy 数据服务（仅支持1分钟K线）
放置于 examples/no_ui/vnpy_akshare.py
配置方法：在 vt_setting.json 中设置 "datafeed.name": "akshare"
"""

from datetime import datetime
from typing import Callable
import pandas as pd
import akshare as ak

from vnpy.trader.object import HistoryRequest, BarData, Interval
from vnpy.trader.constant import Exchange
from vnpy.trader.datafeed import BaseDatafeed


class Datafeed(BaseDatafeed):
    """基于 Akshare 的 vnpy 数据服务（仅1分钟K线）"""

    def __init__(self):
        super().__init__()
        self.name = "akshare"

    def init(self, output: Callable = print) -> bool:
        """初始化连接（无需鉴权）"""
        output("Akshare 数据服务初始化成功（1分钟K线）")
        return True

    def query_bar_history(
        self, req: HistoryRequest, output: Callable = print
    ) -> list[BarData]:
        """查询历史K线数据，仅支持1分钟周期"""
        symbol: str = req.symbol
        start: datetime = req.start
        end: datetime = req.end

        # 去除时区信息（因为 akshare 返回的是本地时间，无时区）
        if start.tzinfo is not None:
            start = start.replace(tzinfo=None)
        if end.tzinfo is not None:
            end = end.replace(tzinfo=None)

        # 只处理1分钟K线，其他周期直接返回空
        if req.interval != Interval.MINUTE:
            output(f"本数据服务仅支持1分钟K线，当前请求周期: {req.interval}")
            return []

        # CZCE: 1-digit year → 2-digit for Sina (MA610 → MA2610)
        if req.exchange == Exchange.CZCE:
            variety = ''.join(c for c in symbol if not c.isdigit())
            digits = symbol[len(variety):]
            if len(digits) == 3:
                symbol = variety + str(datetime.now().year)[2] + digits

        # 调用 akshare 获取1分钟数据
        try:
            df = ak.futures_zh_minute_sina(symbol=symbol.lower(), period="1m")
            if df is None or df.empty:
                output(f"未获取到 {symbol} 的1分钟K线数据")
                return []
        except Exception as e:
            output(f"获取数据异常: {e}")
            return []

        # 转换 datetime 列
        df["datetime"] = pd.to_datetime(df["datetime"])

        # 生成 BarData 列表
        bars = []
        for _, row in df.iterrows():
            dt = row["datetime"].to_pydatetime()
            # 只保留在时间窗口内的数据
            if dt < start or dt > end:
                continue
            bar = BarData(
                symbol=symbol,
                exchange=req.exchange,
                datetime=dt,
                interval=Interval.MINUTE,
                open_price=float(row["open"]),
                high_price=float(row["high"]),
                low_price=float(row["low"]),
                close_price=float(row["close"]),
                volume=float(row.get("volume", 0)),
                open_interest=float(row.get("hold", 0)),
                gateway_name="AKSHARE",
            )
            bars.append(bar)

        return bars


# 测试入口
if __name__ == "__main__":
    from vnpy.trader.constant import Exchange

    req = HistoryRequest(
        symbol="rb2610",
        exchange=Exchange.SHFE,
        interval=Interval.MINUTE,
        start=datetime.now() - pd.Timedelta(days=2),
        end=datetime.now(),
    )

    feed = Datafeed()
    bars = feed.query_bar_history(req)
    print(f"获取到 {len(bars)} 条1分钟K线")
    for bar in bars[:5]:
        print(bar.datetime, bar)
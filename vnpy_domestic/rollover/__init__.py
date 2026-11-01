"""换月复权表（读取侧）。

    from vnpy_domestic.rollover import RolloverTable

回测仓 `vnpy_backtester/rollover_table.py` 是 `table.py` 的逐字节副本，
两边必须保持一致（跨仓库不能互相 import）。
"""

from vnpy_domestic.rollover.table import (
    PRODUCT_ALIASES,
    SCHEMA_VERSION,
    RolloverTable,
    RolloverTableError,
    Segment,
    normalize_contract_code,
    product_prefixes,
    trading_day_of_session,
)

__all__ = [
    "PRODUCT_ALIASES", "SCHEMA_VERSION", "RolloverTable", "RolloverTableError",
    "Segment", "normalize_contract_code", "product_prefixes", "trading_day_of_session",
]

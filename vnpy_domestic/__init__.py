"""vnpy-domestic: 面向国内期货的 vnpy 增强扩展包"""

__version__ = "0.3.0"

import sys

from vnpy_domestic.trader.newbargenerator import MyBarGenerator

# 注册 vnpy_akshare 别名，让 vnpy 数据源系统能自动发现
from vnpy_domestic.trader import vnpy_akshare
sys.modules["vnpy_akshare"] = vnpy_akshare

__all__ = ["MyBarGenerator"]

"""离线自测：用真实 vnpy + 真实 vnpy_domestic.MyBarGenerator 跑一遍 on_init。

**不需要 CTP、不需要行情**，只验证"高周期策略能不能初始化成功"——高周期策略最常见的
死法是 `load_bar` 拿不到历史 → `ArrayManager` 永远 `inited=False` → 策略"初始化成功"
却一笔不交易，还不报错。改完策略先跑这个，别等开盘才发现。

    python scripts/strategy_selftest.py
    python scripts/strategy_selftest.py --strategy-file D:\\path\\my_strategy.py
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

# 本脚本在 <仓库>/scripts/ 下，仓库根就是 parents[1]
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_NAMES = ("dynamic_donchian_strategy.py", "dual_ma.py", "short_dual_ma.py")

SEARCH_DIRS = (
    REPO_ROOT / "strategies",
    REPO_ROOT.parent / "模拟盘simnow" / "strategies",
    REPO_ROOT.parent / "实盘" / "strategies",
)


class FakeCtaEngine:
    """只实现 CtaTemplate 会调用到的方法，用来离线跑 on_init。"""

    def __init__(self) -> None:
        self.orders: list = []
        self.logs: list[str] = []
        self.size = 10.0
        self.pricetick = 1.0

    # 下单相关：初始化阶段 trading=False，框架不会调到这里
    def send_order(self, strategy, direction, offset, price, volume, stop, lock, net):
        self.orders.append((strategy.strategy_name, direction, offset, price, volume))
        return [f"order{len(self.orders)}"]

    def cancel_order(self, strategy, vt_orderid):
        pass

    def cancel_all(self, strategy):
        pass

    def load_bar(self, vt_symbol, days, interval, callback, use_database):
        self.logs.append(f"[engine] load_bar({days} 天) 返回空（模拟库里没数据）")
        return []

    def load_tick(self, vt_symbol, days, callback):
        return []

    def get_pricetick(self, strategy):
        return self.pricetick

    def get_size(self, strategy):
        return self.size

    def write_log(self, msg, strategy=None):
        self.logs.append(str(msg))

    def send_notification(self, msg, strategy=None):
        pass

    def put_strategy_event(self, strategy):
        pass

    def sync_strategy_data(self, strategy):
        pass


def find_strategy_file(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        if not p.exists():
            raise SystemExit(f"找不到策略文件：{p}")
        return p
    for d in SEARCH_DIRS:
        for name in DEFAULT_NAMES:
            cand = d / name
            if cand.exists():
                return cand
    raise SystemExit(
        "没找到策略文件。用 --strategy-file 指定，例如：\n"
        "  python scripts/strategy_selftest.py --strategy-file "
        r"C:\Users\29523\Desktop\vnpy\模拟盘simnow\strategies\dynamic_donchian_strategy.py"
    )


def load_class(path: Path):
    spec = importlib.util.spec_from_file_location("_selftest_target", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_selftest_target"] = mod
    spec.loader.exec_module(mod)
    from vnpy_ctastrategy import CtaTemplate

    for obj in vars(mod).values():
        if isinstance(obj, type) and issubclass(obj, CtaTemplate) and obj is not CtaTemplate:
            return obj
    raise SystemExit(f"{path.name} 里没有 CtaTemplate 子类")


# (vt_symbol, bg_window) —— 只用于验证"合约代码能否解析 + 预热是否成功"
CASES = [
    ("SM2701.CZCE", 15),
    ("rb2701.SHFE", 15),
    ("IC2612.CFFEX", 15),
    ("SR2701.CZCE", 60),
    ("m2701.DCE", 60),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy-file", default=None, help="不给就自动在 strategies/ 里找")
    args = ap.parse_args()

    path = find_strategy_file(args.strategy_file)
    cls = load_class(path)
    print(f"策略文件: {path}")
    print(f"策略类  : {cls.__name__}")
    print(f"参数    : {getattr(cls, 'parameters', [])}")

    fails = 0
    for vt_symbol, window in CASES:
        engine = FakeCtaEngine()
        setting = {
            "bg_window": window, "n1": 15, "n2": 180, "s1": 1.0, "fixed_size": 1,
            "warmup_source": "akshare", "warmup_bars": 300,
        }
        try:
            st = cls(engine, f"test_{vt_symbol}", vt_symbol, setting)
        except TypeError as exc:
            print(f"\n{'=' * 78}\n{vt_symbol}  构造失败（参数名不匹配？）：{exc}")
            fails += 1
            continue

        print(f"\n{'=' * 78}")
        print(f"{vt_symbol}  bg_window={window}")
        try:
            st.on_init()
        except Exception as exc:                       # noqa: BLE001
            print(f"    on_init 抛异常：{type(exc).__name__}: {exc}")
            fails += 1
            continue
        for line in engine.logs:
            print("   ", line)
        am = getattr(st, "am", None)
        ok = bool(am and am.inited)
        print(f"    -> am {getattr(am, 'count', '?')}/{getattr(am, 'size', '?')} "
              f"inited={ok} 订单数={len(engine.orders)}")
        if not ok:
            fails += 1

    print(f"\n{'=' * 78}")
    print(f"失败用例：{fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

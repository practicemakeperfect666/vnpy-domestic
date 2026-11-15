"""每日收盘后的「主力有没有变化」快速检查（不写表，只判断）。

为什么不直接跑全量更新：全量要拉 2~4 个月、按交易所全合约，约 8 分钟。
而 99% 的日子没有任何品种换月，只需要看「今天的 OI 排名第一名还是不是表里那个」。

判定口径与 update_rollover_akshare.py 完全一致（同一个 core.pick_raw_main，
同样的 lead=4 防抖、同样的交割月过滤），只是窗口缩到最近 N 个交易日。

退出码
------
    0   没有品种需要换月
    10  有品种已满足换月条件（需要跑全量更新）
    1   取数失败

用法
----
    python scripts\\rollover_check.py                # 检查表里全部品种
    python scripts\\rollover_check.py --products IC,SM,AG
    python scripts\\rollover_check.py --days 8       # 只看最近 8 个交易日
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import update_rollover_akshare as upd

LEAD = upd.LEAD
TABLE = upd.default_table()
ALL_EXCHANGES = ("SHFE", "CZCE", "CFFEX", "INE", "GFEX")


def _notify(text: str) -> bool:
    """尽力发通知（读 .vntrader/secrets.yaml），失败只打印，不影响退出码。"""
    try:
        sys.path.insert(0, str(ROOT))
        from vnpy_domestic.trader.notification_manager import NotificationManager

        secrets = ROOT / ".vntrader" / "secrets.yaml"
        nm = NotificationManager(config_path=secrets if secrets.exists() else None)
        ok = nm.send_text(text)
        nm.close(timeout=6.0)
        return ok
    except Exception as exc:
        print(f"[notify] ⚠ 发送失败：{type(exc).__name__}: {exc}")
        return False


def current_contract(segments: list, day: date) -> str | None:
    """表里 day 当天应该用哪个合约（取最后一个起始日 <= day 的段）。"""
    cur = None
    for seg in segments:
        if date.fromisoformat(seg[0]) <= day:
            cur = seg[1]
        else:
            break
    return cur


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default=str(TABLE))
    ap.add_argument("--products", default="", help="逗号分隔，默认表里全部")
    ap.add_argument("--days", type=int, default=12, help="回看多少个自然日（要 >= LEAD+3）")
    args = ap.parse_args()

    payload = json.loads(Path(args.table).read_text(encoding="utf-8"))
    anchor = payload["anchor"]
    print(f"表: {args.table}")
    print(f"anchor={anchor}  data_through={payload['data_through']}\n")

    targets = sorted({p.strip().upper() for p in args.products.split(",") if p.strip()}
                     or upd.trading_products(upd.LIVE_TABLE.parents[1])
                     or list(payload["products"]))
    start = date.today() - timedelta(days=args.days)
    end = date.today()

    cache: dict[str, dict] = {}
    pending: list[str] = []
    failed: list[str] = []

    print(f"{'品种':<5}{'表里当前主力':<13}{'最近 OI 第一':<13}{'连续领先':>8}  判定")
    print("-" * 66)

    for product in targets:
        item = payload["products"].get(product)
        if not item:
            continue
        exchange = item["exchange"]
        try:
            if exchange == "DCE":
                daily = upd.fetch_dce_product(product, upd.listed_months(item["segments"]), start)
            else:
                if exchange not in cache:
                    cache[exchange] = upd.fetch_exchange(exchange, start, end)
                daily = {c: s for c, s in cache[exchange].items()
                         if c.startswith(product.lower()) or c.startswith(product.upper())}
        except Exception as exc:
            failed.append(f"{product}({type(exc).__name__})")
            continue
        if not daily:
            failed.append(f"{product}(无数据)")
            continue

        days = sorted({d for series in daily.values() for d in series})
        if not days:
            continue
        raw = upd.core.pick_raw_main(daily, days, product, set())

        cur = current_contract(item["segments"], days[-1])
        top = raw.get(days[-1])
        if not cur or not top:
            continue

        # 候选合约连续领先了几天（从后往前数）
        streak = 0
        for day in reversed(days):
            t = raw.get(day)
            if t and t != cur:
                streak += 1
            else:
                break

        if cur == top:
            verdict = "✅ 无变化"
        elif streak >= LEAD:
            verdict = f"⚠️ 应换月 → {top}"
            pending.append(f"{product}: {cur} -> {top}（连续 {streak} 天领先）")
        else:
            verdict = f"候选 {top}（还差 {LEAD - streak} 天确认）"
        print(f"{product:<5}{cur:<13}{top!s:<13}{streak:>8}  {verdict}")

    if failed:
        print(f"\n取数失败 {len(failed)} 个：{'、'.join(failed[:12])}")

    if pending:
        print(f"\n需要换月的品种 {len(pending)} 个：")
        for line in pending:
            print("  ·", line)
        print("\n→ 请跑全量更新：python scripts\\rollover_daily.py")
        return 10

    # 取数失败也要通知：否则"检查通过"是假的
    if failed:
        _notify(f"⚠️ 换月表检查有品种取数失败（{len(failed)}/{len(targets)}）\n"
                f"  {'、'.join(failed[:12])}\n"
                f"  表 data_through={payload['data_through']}｜请检查 akshare 接口或网络")
        print("\n⚠ 有品种取数失败，请人工确认")
        return 1

    print("\n✅ 全表主力都没有变化，无需更新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

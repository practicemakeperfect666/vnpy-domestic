"""用 akshare 日线增量更新换月复权表（交易机部署版）—— 不依赖 kline_1min / 供应商 7z。

两种更新方式共用**这一个脚本、同一套算法**
----------------------------------------
① 回测机：跑完后 commit + push，实盘机 `git pull` 拿到（人工或 git 流程）
② 实盘机：计划任务每天 17:30 自己跑一遍，本地表即时生效（引擎按 mtime 热加载）

两者都是**增量追加**：只在 `data_through` 之后追加新换月点，历史 segments
一个字节都不改，anchor 保持 2026-09-01。所以无论哪边先跑、跑几次，
得到的表都指向同一条主力/复权序列。

多进程并发安全
--------------
· 文件锁：同一时刻只允许一个更新进程（实盘机 + 模拟盘机都有计划任务时也只跑一个）
· 原子替换 `os.replace`：两个交易进程正在读也不会读到半个文件
· 只追加 + 每次重新读盘：重复跑、交叉跑都不会丢数据
· 写盘前 `RolloverTable(payload)` 自校验；anchor 变了直接拒绝

为什么可行
----------
`rollover_core.build_product()` 是纯函数，输入只有「每个合约的日收盘 + 持仓量」，
数据从 PG 来还是从 akshare 来都无所谓。akshare 正好能提供：

    ak.get_futures_daily(start, end, market)   SHFE/CZCE/CFFEX/INE/GFEX，一次拿全部合约
    ak.futures_zh_daily_sina(symbol)           DCE（get_futures_daily 的 DCE 接口当前挂），
                                               单合约全生命期日线，带 close/hold

为什么是"增量追加"而不是"全量重算"
--------------------------------
表的 factor 是相对 anchor（2026-09-01）归一化的，**历史段一旦重算就会漂移**，
而实盘持仓浮盈、回测净值都基于它。所以：
  · 历史 segments 一律原样保留
  · 只从 data_through 之后追加新换月点
  · factor = 上一段 factor × (旧合约收盘 / 新合约收盘)，取换月前一个交易日

用法
----
    python update_rollover_akshare.py --dry-run                 # 只看会不会动
    python update_rollover_akshare.py                           # 写回原表
    python update_rollover_akshare.py --products IC,SM,au       # 只更新指定品种
    python update_rollover_akshare.py --table path\to\rollover.json
"""
from __future__ import annotations

import argparse

# ────────────────────────── 依赖（零 vnpy 依赖）
import importlib.util
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import akshare as ak


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ROOT = Path(__file__).resolve().parents[1]          # vnpy-domestic 根
_CORE = ROOT / "vnpy_domestic" / "rollover" / "core.py"
_TBL = ROOT / "vnpy_domestic" / "rollover" / "table.py"
if not _CORE.exists():                              # 回测仓兜底
    _CORE = ROOT / "rollover_core.py"
    _TBL = ROOT / "rollover_table.py"

core = _load_module("_rollover_core", _CORE)
_table = _load_module("_rollover_table", _TBL)
RolloverTable = _table.RolloverTable
RolloverTableError = _table.RolloverTableError
normalize_contract_code = _table.normalize_contract_code
def default_table() -> Path:
    """默认更新的表：交易机 = vnpy-domestic/data/rollover.json。"""
    env = os.getenv("ROLLOVER_TABLE")
    if env:
        return Path(env)
    return ROOT / "data" / "rollover.json"


LIVE_TABLE = default_table()
EXPECTED_ANCHOR = ""          # 空字符串 = 默认不校验 anchor，原样继承表里的值
EXCHANGES = ("SHFE", "CZCE", "CFFEX", "INE", "GFEX")
LEAD = 4
COOLDOWN = 4
LOCK_FILE = Path(tempfile.gettempdir()) / "vnpy_rollover_update.lock"


# ─────────────────────────── 单实例文件锁 ───────────────────────────
def acquire_lock():
    """同一时刻只允许一个更新进程（实盘机/模拟盘机的计划任务撞车时用）。

    返回文件句柄（拿着就代表持锁），抢不到返回 None。
    """
    try:
        fh = open(LOCK_FILE, "w")
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


# ────────────────────────────── 取数 ──────────────────────────────
def _norm(code: str, exchange: str) -> str:
    """统一成表里的库内格式（CZCE 4 位大写、其余小写、无后缀）。"""
    return normalize_contract_code(code, exchange, target="db").split(".")[0]


def fetch_exchange(market: str, start: date, end: date) -> dict[str, dict[date, core.DayClose]]:
    """一个交易所的全部合约日线。返回 {contract: {day: DayClose}}。"""
    df = ak.get_futures_daily(start_date=start.strftime("%Y%m%d"),
                              end_date=end.strftime("%Y%m%d"), market=market)
    out: dict[str, dict[date, core.DayClose]] = {}
    if df is None or len(df) == 0:
        return out
    for r in df.to_dict("records"):
        sym = str(r.get("symbol") or "").upper()
        if not sym or "_" in sym:          # 过滤 SC_TAS2403 这种组合合约
            continue
        try:
            close = float(r["close"])
            oi = float(r.get("open_interest") or 0)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        raw_day = str(r["date"])
        try:
            day = (datetime.strptime(raw_day, "%Y%m%d").date() if len(raw_day) == 8
                   else datetime.fromisoformat(raw_day[:10]).date())
        except ValueError:
            continue
        code = _norm(sym, market)
        out.setdefault(code, {})[day] = core.DayClose(day, close, oi)
    return out


def fetch_dce_product(product: str, months: list[str], start: date) -> dict[str, dict[date, core.DayClose]]:
    """DCE 走 sina 单合约日线。months 是该品种挂牌月份（从历史段推出），如 ['01','05','09']。"""
    out: dict[str, dict[date, core.DayClose]] = {}
    today = date.today()
    codes = []
    for year in (today.year, today.year + 1, today.year + 2):
        for mm in months:
            # 已经不交易的老合约不用再拉，省掉一半以上请求
            if date(year, int(mm), 1) < date(today.year, today.month, 1) - timedelta(days=200):
                continue
            codes.append(f"{product.upper()}{str(year)[2:]}{mm}")
    for code in codes:
        try:
            df = ak.futures_zh_daily_sina(symbol=code.lower())
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        norm = _norm(code, "DCE")
        for r in df.to_dict("records"):
            try:
                day = datetime.fromisoformat(str(r["date"])[:10]).date()
                close = float(r["close"])
                oi = float(r.get("hold") or 0)
            except (TypeError, ValueError):
                continue
            if day < start or close <= 0:
                continue
            out.setdefault(norm, {})[day] = core.DayClose(day, close, oi)
    return out


def listed_months(segments: list) -> list[str]:
    """从历史段推出该品种的挂牌月份，省得对 DCE 盲猜 12 个月。"""
    months = set()
    for _day, contract, _f in segments:
        digits = "".join(c for c in contract if c.isdigit())
        if len(digits) >= 4:
            months.add(digits[-2:])
    return sorted(months) or [f"{m:02d}" for m in range(1, 13)]


def latest_trading_day() -> date | None:
    """用 akshare 探一下「最新交易日」：拿一个必然活跃的合约的最后一根日线。

    只调 1 次网络，用来判断 --skip-fresh 要不要跳过。
    不能用 date.today()：周末/节假日今天没有行情，表永远追不上"今天"。
    """
    for sym in ("rb2701", "rb2705", "rb0"):
        try:
            df = ak.futures_zh_daily_sina(symbol=sym)
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        try:
            return datetime.fromisoformat(str(df.iloc[-1]["date"])[:10]).date()
        except (TypeError, ValueError):
            continue
    return None


def trading_products(repo: Path) -> list[str]:
    """从 cta_strategy_setting.json 里读出「实际在交易的品种」。

    vt_symbol 形如 `IC2612.CFFEX` / `SM2701.CZCE`，去掉交易所后缀再取字母前缀。
    找不到配置就返回空列表（调用方回退到全表品种）。
    """
    for cand in (repo / ".vntrader" / "cta_strategy_setting.json",
                 Path.cwd() / ".vntrader" / "cta_strategy_setting.json"):
        if not cand.exists():
            continue
        try:
            cfg = json.loads(cand.read_text(encoding="utf-8"))
        except Exception:
            continue
        products: set[str] = set()
        for inst in (cfg.values() if isinstance(cfg, dict) else []):
            if not isinstance(inst, dict):
                continue
            symbol = str(inst.get("vt_symbol") or "").split(".")[0]
            head = ""
            for ch in symbol:
                if ch.isalpha():
                    head += ch
                else:
                    break
            if head:
                products.add(head.upper())
        if products:
            print(f"交易品种（来自 {cand.name}）：{'、'.join(sorted(products))}")
            return sorted(products)
    return []


# ────────────────────────── 主力判定（带种子）──────────────────────────
def extend_main(daily, days, product, seed_contract, seed_day):
    """从 seed_day 往后跑防抖，seed_day 及之前一律以换月表为准。

    注意：`pick_raw_main` 要用整个窗口（它取「上一交易日」的 OI），
    但防抖循环必须**从 seed_day 开始**。如果从窗口第一天就开始跑，
    种子日之前的历史会重新判一遍，判出来的主力可能和表里的不一致，
    然后一路带到 seed_day，导致「起点对不上」而误跳过整品种。
    """
    raw = core.pick_raw_main(daily, days, product, set())
    out: dict[date, str] = {}
    current = seed_contract
    candidate, count, cool = None, 0, 0

    for day in days:
        if day < seed_day:           # 种子日之前不参与判定
            continue
        target = raw.get(day)
        if current and day not in daily.get(current, {}):
            if target:
                current = target
                candidate, count, cool = None, 0, COOLDOWN
                out[day] = current
                continue
        if target is None:
            if current:
                out[day] = current
            continue
        if not core.is_eligible(current, day, product):
            current = target
            candidate, count, cool = None, 0, COOLDOWN
            out[day] = current
            continue
        if cool > 0:
            cool -= 1
            out[day] = current
            continue
        if target == current:
            candidate, count = None, 0
        else:
            if target == candidate:
                count += 1
            else:
                candidate, count = target, 1
            if count >= LEAD:
                current = candidate
                candidate, count, cool = None, 0, COOLDOWN
        out[day] = current
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default=str(LIVE_TABLE), help="要读的现有表（默认实盘那份）")
    ap.add_argument("--out", action="append", default=None,
                    help="写到哪些路径，可给多次；默认实盘 + 回测两份")
    ap.add_argument("--products", default="", help="逗号分隔，默认全部")
    ap.add_argument("--lookback", type=int, default=12, help="最后一段换月日之前再看几天（防抖用）")
    ap.add_argument("--skip-fresh", action="store_true",
                    help="表已经更新到今天就直接退出（每日定时任务用）")
    ap.add_argument("--no-lock", action="store_true", help="跳过单实例锁（手动调试用）")
    ap.add_argument("--expected-anchor", default="",
                    help="留空=不校验 anchor（默认）。填日期则要求表里 anchor 必须等于它")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    lock = None
    if not args.no_lock:
        lock = acquire_lock()
        if lock is None:
            print(f"另一个更新进程正在运行（锁 {LOCK_FILE}），本次退出")
            return 0

    path = Path(args.table)
    payload = json.loads(path.read_text(encoding="utf-8"))
    anchor = payload["anchor"]
    # 默认**不校验也不改** anchor：原样继承表里的值。
    # 只在显式给了 --expected-anchor 时才拦。
    if args.expected_anchor and anchor != args.expected_anchor:
        print(f"❌ anchor={anchor}，期望 {args.expected_anchor}；拒绝更新", file=sys.stderr)
        return 4
    old_through = date.fromisoformat(payload["data_through"])
    old_anchor = payload["anchor"]
    # 写盘前的「全量保护」基准：品种集合 + 每个品种的原始段（浅拷贝足够，段是 list）
    old_snapshot = {p: [list(s) for s in item["segments"]]
                    for p, item in payload["products"].items()}
    print(f"表: {path}")
    print(f"anchor={anchor}  data_through={old_through}  generated_at={payload.get('generated_at')}")

    # 每日定时任务的快速短路：表已经到最新交易日就不用再拉一遍网络。
    # 不能用 date.today() —— 周末/节假日今天没有行情，表永远追不上"今天"。
    if args.skip_fresh:
        latest = latest_trading_day()
        if latest and old_through >= latest:
            print(f"表已更新到 {old_through}（akshare 最新交易日 {latest}），跳过")
            return 0
        if latest:
            print(f"表 {old_through} 落后于最新交易日 {latest}，开始更新")

    # 品种：显式 --products 优先；否则默认只更新「实际在交易的品种」
    # （从 .vntrader/cta_strategy_setting.json 读，10 个品种 90 秒→10 秒）
    explicit = {p.strip().upper() for p in args.products.split(",") if p.strip()}
    if explicit:
        targets = sorted(explicit)
    else:
        targets = trading_products(LIVE_TABLE.parents[1]) or sorted(payload["products"])
    print(f"本次更新 {len(targets)} 个品种\n")

    # 窗口：只取「过去 lookback 天」（默认 12 天，够 lead=4 防抖 + cooldown=4 + OI 排名）。
    # 例外：表落后超过 lookback 天时自动往前延，保证窗口覆盖到 data_through，
    # 否则中间那些天的换月会被漏掉、因子也会算错。
    start = date.today() - timedelta(days=args.lookback)
    if old_through < start:
        start = old_through - timedelta(days=args.lookback)
        print(f"⚠ 表落后到 {old_through}，窗口自动延长到 {start}")
    end = date.today()
    print(f"取数窗口: {start} ~ {end}（过去 {args.lookback} 天）\n")

    cache: dict[str, dict] = {}
    data_days: list[date] = []          # 每个品种「数据覆盖到的最后一天」
    changed, skipped, failed = [], [], []

    for product in targets:
        item = payload["products"].get(product)
        if not item:
            skipped.append(f"{product}(表里没有)")
            continue
        exchange = item["exchange"]
        segments = item["segments"]
        last_day, last_contract, last_factor = segments[-1]

        try:
            if exchange == "DCE":
                daily = fetch_dce_product(product, listed_months(segments), start)
            else:
                if exchange not in cache:
                    cache[exchange] = fetch_exchange(exchange, start, end)
                daily = {c: s for c, s in cache[exchange].items()
                         if c.startswith(product.lower()) or c.startswith(product.upper())}
        except Exception as exc:
            failed.append(f"{product}({type(exc).__name__}: {exc})")
            continue

        if not daily:
            skipped.append(f"{product}(akshare 无数据)")
            continue

        days = sorted({d for series in daily.values() for d in series})
        # 窗口只有 12 天，通常不含「最后一次换月日」，所以种子直接取窗口第一天 +
        # 表里的当前合约；extend_main 从第一天起重新累积 4 天防抖，结果与全窗口一致
        # （每天跑一次，切换当天窗口刚好包含完整 4 天领先）。
        seed_day = days[0]
        main_by_day = extend_main(daily, days, product, last_contract, seed_day)
        if main_by_day.get(seed_day) != last_contract:
            skipped.append(f"{product}(起点对不上：窗口首日 {seed_day} 表说 {last_contract}，"
                           f"OI 排名说 {main_by_day.get(seed_day)}；"
                           f"表可能落后太多，用 --lookback 60 补一次)")
            continue

        new_segments = []
        factor = last_factor
        prev_c, prev_d = last_contract, date.fromisoformat(last_day)
        for day in days:
            if day <= prev_d:
                continue
            contract = main_by_day.get(day)
            if not contract:
                continue
            if contract != prev_c:
                old_row = daily.get(prev_c, {}).get(prev_d)
                new_row = daily.get(contract, {}).get(prev_d)
                if old_row is None or new_row is None:
                    skipped.append(f"{product}({day} 换月 {prev_c}->{contract} 缺收盘价)")
                    new_segments = []
                    break
                factor *= old_row.close / new_row.close
                new_segments.append([day.isoformat(), contract, round(factor, 10)])
            prev_c, prev_d = contract, day

        newest = max(days)
        data_days.append(newest)                 # 数据实际覆盖到哪天
        if new_segments:
            item["segments"].extend(new_segments)
            changed.append(f"{product}: {len(new_segments)} 个换月 -> "
                           + ", ".join(f"{d} {c}" for d, c, _ in new_segments))
        print(f"{product:<4} {exchange:<6} 段 {len(segments)}->{len(item['segments']):<4} "
              f"末段 {item['segments'][-1][1]:<10} 数据到 {newest}"
              f"{'  ← 追加 ' + str(len(new_segments)) if new_segments else ''}")

    if failed:
        print("\n失败：", "；".join(failed))
    if skipped:
        print("跳过：", "；".join(skipped))
    if changed:
        print("\n新增换月点：")
        for line in changed:
            print("  ", line)

    # data_through 语义 = 数据覆盖到的最后一天（不是最后一个换月日）：
    # 否则引擎的 _check_table_freshness 每天都会误报"表没更新"。
    # 推进条件：**默认模式（自动取交易品种）且无失败**。
    # 显式 --products 是手动/调试，只动了几个品种，不能让全局日期虚高。
    if failed:
        payload["data_through"] = old_through.isoformat()
        print(f"\n{len(failed)} 个品种取数失败，data_through 保持 {old_through} 不变")
    elif explicit:
        payload["data_through"] = old_through.isoformat()
        print(f"\n手动指定品种（--products），data_through 保持 {old_through} 不变")
    else:
        payload["data_through"] = max([old_through, *data_days]).isoformat()
    payload["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")

    try:
        RolloverTable(payload)                     # 消费者用的同一套校验
    except RolloverTableError as exc:
        print(f"\n自校验失败，未写文件：{exc}", file=sys.stderr)
        return 2

    # ── 全量保护：绝不写出一张比原来「少」的表 ──────────────────
    problems: list[str] = []
    if payload["anchor"] != old_anchor:
        problems.append(f"anchor 被改了：{old_anchor} -> {payload['anchor']}")
    lost = sorted(set(old_snapshot) - set(payload["products"]))
    if lost:
        problems.append(f"品种丢失 {len(lost)} 个：{lost[:8]}")
    for product, osegs in old_snapshot.items():
        new_item = payload["products"].get(product)
        if not new_item:
            continue
        nsegs = new_item["segments"]
        if len(nsegs) < len(osegs):
            problems.append(f"{product}: 段数变少 {len(osegs)} -> {len(nsegs)}")
        elif nsegs[:len(osegs)] != osegs:
            problems.append(f"{product}: 历史段被改写")
    if problems:
        print("\n❌ 全量保护触发，拒绝写文件：", file=sys.stderr)
        for p in problems[:12]:
            print(f"   · {p}", file=sys.stderr)
        return 5
    print(f"全量保护：品种 {len(old_snapshot)} 个全部保留，"
          f"历史段逐字节一致，anchor 未变（{old_anchor}）")

    print(f"\ndata_through: {old_through} -> {payload['data_through']}")
    if args.dry_run:
        print("dry-run，未写文件")
        return 0

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    # 交易机只写自己那份（vnpy-domestic/data/rollover.json）；
    # 要同时写回测那份，用 --out 显式给两次
    outs = list(args.out) if args.out else [str(LIVE_TABLE)]
    for out in outs:
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)                    # 原子替换，引擎按 mtime 热加载
        print(f"已写入 {target}（{len(text)/1024:.1f} KB）")

    if len(outs) > 1:                              # 校验两份完全一致
        import hashlib
        hashes = {hashlib.md5(Path(o).read_bytes()).hexdigest().upper() for o in outs}
        if len(hashes) != 1:
            print("⚠️ 两份表不一致！", file=sys.stderr)
            return 3
        print(f"两份一致 md5={hashes.pop()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

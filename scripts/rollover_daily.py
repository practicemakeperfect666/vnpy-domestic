"""收盘后自动更新换月复权表 + 发通知 + 记日志（交易机计划任务入口）。

做的事
------
1. 把 stdout/stderr 同时写到 `scripts/logs/rollover_update_YYYYMM.log`
2. 调用同目录的 `update_rollover_akshare.py`（增量追加，不动历史段）
3. 对比更新前后的表，组装一条摘要
4. 通过 `NotificationManager`（读 `.vntrader/secrets.yaml` 的飞书/钉钉配置）发出去
5. 透传退出码给计划任务

用法
----
    python scripts\\rollover_daily.py                  # 默认带 --skip-fresh
    python scripts\\rollover_daily.py --dry-run        # 只看不动
    python scripts\\rollover_daily.py --products IC,SM
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

TABLE = ROOT / "data" / "rollover.json"

LOG_FILE: Path | None = None


def _open_log() -> tuple[object | None, Path | None]:
    """日志文件放在哪：优先 scripts/logs，不可写就退到仓库根，再退到 %TEMP%。

    绝对不能因为「日志目录建不出来」就把整个更新搞崩 —— 表还是要更新。
    """
    name = f"rollover_update_{datetime.now():%Y%m}.log"
    for base in (HERE / "logs", ROOT / "logs",
                 Path(os.getenv("TEMP") or tempfile.gettempdir()) / "vnpy_rollover_logs"):
        try:
            base.mkdir(parents=True, exist_ok=True)
            fh = open(base / name, "a", encoding="utf-8")
            return fh, base / name
        except Exception:
            continue
    return None, None


class _Tee:
    """把输出同时写到原 stdout 和日志文件。"""

    def __init__(self, stream, fh):
        self.stream = stream
        self.fh = fh

    def write(self, s):
        self.stream.write(s)
        try:
            self.fh.write(s)
            self.fh.flush()
        except Exception:
            pass
        return len(s)

    def flush(self):
        self.stream.flush()
        try:
            self.fh.flush()
        except Exception:
            pass


def _snapshot() -> dict:
    """取表的关键状态：data_through + 每品种段数（用于对比新增了几段）。"""
    import json

    if not TABLE.exists():
        return {}
    try:
        d = json.loads(TABLE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {
        "anchor": d.get("anchor"),
        "through": d.get("data_through"),
        "segs": {p: len(i.get("segments", [])) for p, i in d.get("products", {}).items()},
        "items": {p: i.get("segments", []) for p, i in d.get("products", {}).items()},
    }


def _diff(before: dict, after: dict) -> tuple[str, list[str]]:
    """返回 (状态, 新增换月点的可读列表)。"""
    if not before or not after:
        return "unknown", []
    if before.get("through") == after.get("through") and before["segs"] == after["segs"]:
        return "nochange", []

    added: list[str] = []
    for product, segs in after["items"].items():
        old_n = before["segs"].get(product, 0)
        for seg in segs[old_n:]:
            if len(seg) >= 3:
                added.append(f"{product} {seg[0]} {seg[1]}  factor {seg[2]:.4f}")
    return "updated", added


def _notify(text: str) -> bool:
    """尽力发一条通知；没配 webhook 就只写日志，绝不影响更新结果。"""
    try:
        from vnpy_domestic.trader.notification_manager import NotificationManager

        secrets = ROOT / ".vntrader" / "secrets.yaml"
        nm = NotificationManager(config_path=secrets if secrets.exists() else None)
        ok = nm.send_text(text)
        nm.close(timeout=6.0)          # 把队列里的消息发完再退出
        if not ok:
            print("[notify] ⚠ 消息未能入队（队列满或已关闭）—— 通知可能没送达", flush=True)
        return ok
    except Exception as exc:
        print(f"[notify] ⚠ 发送失败（不影响表更新）：{type(exc).__name__}: {exc}", flush=True)
        return False


RC_REASON = {
    1: "脚本异常（看日志 traceback）",
    2: "表自校验失败（RolloverTable 拒绝）",
    3: "两份表 md5 不一致",
    4: "anchor 与期望值不符",
    5: "全量保护触发（品种丢失 / 段数变少 / 历史段被改写）",
}


def _log_hint() -> str:
    return str(LOG_FILE) if LOG_FILE else "（日志目录不可写，仅控制台输出）"


def main() -> int:
    args = sys.argv[1:]
    if not args:
        args = ["--skip-fresh"]

    before = _snapshot()
    print(f"\n{'=' * 70}")
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 换月复权表自动更新开始")
    print(f"表: {TABLE}")

    rc = 0
    try:
        import update_rollover_akshare as upd

        sys.argv = ["update_rollover_akshare.py", *args]
        rc = upd.main()
    except SystemExit as exc:
        rc = int(exc.code or 0)
    except Exception:
        rc = 1
        print("更新脚本异常：")
        traceback.print_exc()

    after = _snapshot()
    state, added = _diff(before, after)
    if "--dry-run" in args:                     # dry-run 不落盘，不算一次更新
        state = "dryrun"
    old_t, new_t = before.get("through"), after.get("through")
    anchor = after.get("anchor")
    n_products = len(after.get("segs", {}))

    print(f"结果: rc={rc} state={state} data_through {old_t} -> {new_t} "
          f"anchor={anchor} 品种={n_products} 新增={len(added)}")
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 结束\n")

    if rc == 0 and state == "updated" and added:
        body = "\n".join(f"  · {a}" for a in added[:12])
        more = f"\n  · …共 {len(added)} 个" if len(added) > 12 else ""
        _notify(f"📅 换月复权表已更新\n"
                f"  {old_t} → {new_t}｜品种 {n_products}｜anchor {anchor}\n"
                f"  新增 {len(added)} 个换月点：\n{body}{more}")
    elif rc == 0 and state == "updated":
        _notify(f"📅 换月复权表已推进\n"
                f"  {old_t} → {new_t}｜品种 {n_products}｜anchor {anchor}\n"
                f"  本次无新增换月点（仅数据日期前移）")
    elif rc == 0 and state == "nochange":
        _notify(f"✅ 换月复权表已是最新（{new_t}），本次无变化｜品种 {n_products}")
    elif rc == 0 and state == "unknown":
        pass                                    # skip-fresh 短路或表不存在，不打扰
    elif rc == 0:
        pass
    else:
        # 失败一定要通知：带退出码原因 + 日志路径，方便直接定位
        reason = RC_REASON.get(rc, "未知原因")
        _notify(f"🚨 换月复权表更新失败\n"
                f"  退出码 {rc}：{reason}\n"
                f"  data_through 仍为 {new_t}｜anchor {anchor}｜品种 {n_products}\n"
                f"  日志：{_log_hint()}")

    # 成功但表仍然落后（akshare 没数据 / 全部被跳过）：也要提醒
    if rc == 0 and new_t:
        try:
            lag = (datetime.now().date() - datetime.fromisoformat(new_t).date()).days
        except ValueError:
            lag = 0
        if lag > 7:
            _notify(f"⚠️ 换月复权表更新成功，但仍落后 {lag} 天\n"
                    f"  data_through={new_t}（今天 {datetime.now():%Y-%m-%d}）\n"
                    f"  可能 akshare 取数异常，请人工检查｜日志：{_log_hint()}")

    return rc


if __name__ == "__main__":
    fh, LOG_FILE = _open_log()
    old_out, old_err = sys.stdout, sys.stderr
    if fh is not None:
        sys.stdout = _Tee(old_out, fh)
        sys.stderr = _Tee(old_err, fh)
    else:
        print("[log] ⚠ 所有日志目录都不可写，本次只输出到控制台", file=sys.stderr)
    try:
        code = main()
    finally:
        try:
            if fh is not None:
                fh.close()
        finally:
            sys.stdout, sys.stderr = old_out, old_err
    raise SystemExit(code)

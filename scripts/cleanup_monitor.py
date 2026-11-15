"""Prune old snapshot rows from monitor.db.

Deletes snapshot rows older than KEEP_DAYS, always keeping the latest row
per key so the realtime panel never loses its current state. Also prunes
settlements by trading_day (keeping the latest row per account+symbol, which
is all the mark-to-market logic ever reads). Logs an INFO record of the prune
to the logs table (kept forever). K-line, trades, orders, positions, daily_pnl
and logs tables are not touched.

Usage:
    python cleanup_monitor.py [db_path] [keep_days] [--vacuum]
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta

DEFAULT_DB = os.getenv(
    "MONITOR_DB_PATH", os.path.join(os.path.expanduser("~"), "vnpy-data", "monitor.db")
)

def _has_table(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


SNAPSHOT_TABLES = [
    ("accounts", "snapshot_time", ["account"]),
    ("strategy_status", "update_time", ["account", "strategy"]),
    ("account_positions", "snapshot_time", ["account", "vt_symbol", "direction"]),
    ("account_intraday_pnl", "snapshot_time", ["account"]),
    ("system_metrics", "ts", []),  # 无 account 字段，只保留全局最新一条
]


def prune(db_path: str, keep_days: int, vacuum: bool) -> None:
    boundary = (datetime.now() - timedelta(days=keep_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = 0
        for table, time_col, keys in SNAPSHOT_TABLES:
            if keys:
                key_sql = ", ".join(keys)
                keep = f"SELECT MAX(id) FROM {table} GROUP BY {key_sql}"
            else:
                keep = f"SELECT MAX(id) FROM {table}"
            deleted = conn.execute(
                f"DELETE FROM {table} WHERE {time_col} < ? AND id NOT IN ({keep})",
                (boundary,),
            ).rowcount
            total += deleted
            print(f"{table}: deleted {deleted}")

        # settlements：trading_day 是日期字符串，按日期比；保留每个
        # (account, symbol) 的最新一条（盯市只需要最近一条，删了也能重拉）
        if _has_table(conn, "settlements"):
            date_boundary = (datetime.now() - timedelta(days=keep_days)).strftime(
                "%Y-%m-%d"
            )
            deleted = conn.execute(
                "DELETE FROM settlements WHERE trading_day < ? AND id NOT IN ("
                "SELECT MAX(id) FROM settlements GROUP BY account, symbol)",
                (date_boundary,),
            ).rowcount
            total += deleted
            print(f"settlements: deleted {deleted}")

        # 记录清理动作到 logs 表（logs 永久保留）
        conn.execute(
            "INSERT INTO logs (ts, level, strategy, account, message) "
            "VALUES (?, 'INFO', '', '', ?)",
            (now, f"cleanup: pruned {total} rows older than {keep_days} days"),
        )
        conn.commit()
        if vacuum:
            try:
                conn.execute("VACUUM")
                print("vacuum: done")
            except sqlite3.OperationalError as exc:
                print(f"vacuum: skipped ({exc})")
    finally:
        conn.close()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    keep_days = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    vacuum = "--vacuum" in sys.argv
    prune(db_path, keep_days, vacuum)

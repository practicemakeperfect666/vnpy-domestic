"""MonitorWriter: non-blocking DB writer for the trading process.

Design: a queue + a dedicated writer thread. The trading thread only
enqueues a model instance and returns immediately; the writer thread
batches and commits. A write failure is logged and never raised back
into the trading loop.
"""

import logging
import os
import queue
import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models import Base

logger = logging.getLogger("monitor_writer")


class MonitorWriter:
    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        with self.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA busy_timeout=5000")
        Base.metadata.create_all(self.engine)
        self._migrate()

        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _migrate(self) -> None:
        """Migrate existing tables (create_all does not alter existing): add
        queue/cancel delay cols, drop legacy delay_ms. The legacy delay_ms is
        NOT NULL in old schemas; once the model stops writing it, every INSERT
        fails the NOT NULL constraint, so it must be dropped."""
        with self.engine.begin() as conn:
            cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(orders)")}
            for col in ("queue_delay_ms", "cancel_delay_ms"):
                if col not in cols:
                    conn.exec_driver_sql(
                        f"ALTER TABLE orders ADD COLUMN {col} FLOAT DEFAULT 0"
                    )
            if "delay_ms" in cols:
                conn.exec_driver_sql("ALTER TABLE orders DROP COLUMN delay_ms")

            tcols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(trades)")}
            if "delay_ms" in tcols:
                conn.exec_driver_sql("ALTER TABLE trades DROP COLUMN delay_ms")

    def write(self, obj) -> None:
        """Enqueue a model instance; never blocks the caller."""
        self._queue.put((obj, False))

    def write_upsert(self, obj) -> None:
        """Enqueue a one-row-per-key model (e.g. Contract) as an upsert.

        The writer thread uses session.merge() keyed on the primary key, so a
        later write for the same key updates the existing row instead of
        inserting a duplicate.
        """
        self._queue.put((obj, True))

    def _loop(self) -> None:
        session_factory = sessionmaker(bind=self.engine)
        with session_factory() as session:
            while True:
                try:
                    first = self._queue.get(timeout=0.5)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                batch = [first]
                while True:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                try:
                    for obj, upsert in batch:
                        if upsert:
                            session.merge(obj)
                        else:
                            session.add(obj)
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    logger.error("monitor write failed: %s", exc)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.engine.dispose()

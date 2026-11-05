"""Run history storage in the plugin's own data directory (one SQLite file).

Holds runs recorded with eta_record_run (CI, scripts, anything else) and, automatically, each
delegated subagent run Hermes reports through the subagent_stop hook (lane "subagent:<role>",
start time, duration, success flag; never the child's summary or tool calls). Kanban and cron
history are not stored here; they are read live. Nothing here leaves the machine.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Callable, Optional

from .estimator import Run

PLUGIN_NAME = "eta"
MAX_ROWS_PER_LANE = 2000  # keep the file small; old runs carry little signal
_BUSY_TIMEOUT_MS = 5000  # Hermes's open_db default, restored after a bounded hook write

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lane TEXT NOT NULL,
    started_at REAL NOT NULL,
    duration_s REAL NOT NULL,
    ok INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS runs_lane ON runs(lane, started_at);
"""


def _default_connect() -> sqlite3.Connection:
    from plugins.plugin_storage import plugin_db  # public plugin storage API
    return plugin_db(PLUGIN_NAME)


class RunStore:
    def __init__(self, connect: Optional[Callable[[], sqlite3.Connection]] = None):
        self._connect = connect or _default_connect
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = self._connect()
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
        return self._conn

    def record(self, lane: str, duration_s: float, ok: bool,
               started_at: Optional[float] = None, source: str = "manual",
               wait_s: Optional[float] = None) -> bool:
        """Insert one run. With `wait_s`, give up (return False, nothing written) rather than
        wait longer than that for this store's lock or for another process holding the file."""
        lane = _clean_lane(lane)
        started = started_at if started_at is not None else time.time() - max(duration_s, 0)
        if not self._lock.acquire(timeout=wait_s if wait_s is not None else -1):
            return False
        try:
            db = self._db()
            if wait_s is not None:
                db.execute(f"PRAGMA busy_timeout={max(int(wait_s * 1000), 1)}")
            try:
                db.execute("INSERT INTO runs(lane, started_at, duration_s, ok, source) VALUES (?,?,?,?,?)",
                           (lane, float(started), float(max(duration_s, 0)), 1 if ok else 0, source))
                db.execute(
                    "DELETE FROM runs WHERE lane = ? AND id NOT IN "
                    "(SELECT id FROM runs WHERE lane = ? ORDER BY started_at DESC LIMIT ?)",
                    (lane, lane, MAX_ROWS_PER_LANE))
                db.commit()
            except sqlite3.OperationalError:
                db.rollback()
                if wait_s is None:
                    raise
                return False
            finally:
                if wait_s is not None:
                    db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            return True
        finally:
            self._lock.release()

    def runs(self, lane: str) -> list[Run]:
        with self._lock:
            rows = self._db().execute(
                "SELECT started_at, duration_s, ok FROM runs WHERE lane = ? ORDER BY started_at",
                (_clean_lane(lane),)).fetchall()
        return [Run(started_at=r[0], duration_s=r[1], ok=bool(r[2])) for r in rows]

    def runs_like(self, prefix: str) -> list[Run]:
        """Runs of every lane whose name starts with `prefix` (e.g. all "subagent:" lanes)."""
        with self._lock:
            rows = self._db().execute(
                "SELECT started_at, duration_s, ok FROM runs WHERE substr(lane, 1, ?) = ? "
                "ORDER BY started_at", (len(prefix), prefix)).fetchall()
        return [Run(started_at=r[0], duration_s=r[1], ok=bool(r[2])) for r in rows]

    def all_runs(self, exclude_lane: Optional[str] = None) -> list[Run]:
        with self._lock:
            rows = self._db().execute(
                "SELECT started_at, duration_s, ok FROM runs WHERE lane != ? ORDER BY started_at",
                (_clean_lane(exclude_lane) if exclude_lane else "",)).fetchall()
        return [Run(started_at=r[0], duration_s=r[1], ok=bool(r[2])) for r in rows]

    def lanes(self) -> list[str]:
        with self._lock:
            rows = self._db().execute("SELECT DISTINCT lane FROM runs ORDER BY lane").fetchall()
        return [r[0] for r in rows]


def _clean_lane(lane: Optional[str]) -> str:
    lane = (lane or "default").strip()[:120]
    return lane or "default"

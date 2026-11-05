"""Run history storage in the plugin's own data directory (one SQLite file).

Kanban dispatchers and workers run in different processes, so history lives in SQLite
(WAL) rather than in memory. Nothing here leaves the machine.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Callable, Optional

from .estimator import Run

PLUGIN_NAME = "eta"
MAX_ROWS_PER_LANE = 2000  # keep the file small; old runs carry little signal

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
CREATE TABLE IF NOT EXISTS open_claims (
    claim_key TEXT PRIMARY KEY,
    lane TEXT NOT NULL,
    claimed_at REAL NOT NULL
);
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
               started_at: Optional[float] = None, source: str = "manual") -> None:
        lane = _clean_lane(lane)
        started = started_at if started_at is not None else time.time() - max(duration_s, 0)
        with self._lock:
            db = self._db()
            db.execute("INSERT INTO runs(lane, started_at, duration_s, ok, source) VALUES (?,?,?,?,?)",
                       (lane, float(started), float(max(duration_s, 0)), 1 if ok else 0, source))
            db.execute(
                "DELETE FROM runs WHERE lane = ? AND id NOT IN "
                "(SELECT id FROM runs WHERE lane = ? ORDER BY started_at DESC LIMIT ?)",
                (lane, lane, MAX_ROWS_PER_LANE))
            db.commit()

    def open_claim(self, claim_key: str, lane: str, at: Optional[float] = None) -> None:
        with self._lock:
            db = self._db()
            db.execute("INSERT OR REPLACE INTO open_claims(claim_key, lane, claimed_at) VALUES (?,?,?)",
                       (claim_key, _clean_lane(lane), at if at is not None else time.time()))
            # Claims older than a week were abandoned; drop them.
            db.execute("DELETE FROM open_claims WHERE claimed_at < ?", (time.time() - 7 * 86400,))
            db.commit()

    def close_claim(self, claim_key: str, lane: str, ok: bool, at: Optional[float] = None) -> None:
        end = at if at is not None else time.time()
        with self._lock:
            db = self._db()
            row = db.execute("SELECT lane, claimed_at FROM open_claims WHERE claim_key = ?",
                             (claim_key,)).fetchone()
            db.execute("DELETE FROM open_claims WHERE claim_key = ?", (claim_key,))
            db.commit()
        if row:
            self.record(row[0], end - row[1], ok, started_at=row[1], source="kanban")
        else:
            # Claim happened before the plugin was enabled: count the outcome, duration unknown.
            self.record(lane, 0.0, ok, started_at=end, source="kanban")

    def runs(self, lane: str) -> list[Run]:
        with self._lock:
            rows = self._db().execute(
                "SELECT started_at, duration_s, ok FROM runs WHERE lane = ? ORDER BY started_at",
                (_clean_lane(lane),)).fetchall()
        return [Run(started_at=r[0], duration_s=r[1], ok=bool(r[2])) for r in rows]

    def lanes(self) -> list[str]:
        with self._lock:
            rows = self._db().execute("SELECT DISTINCT lane FROM runs ORDER BY lane").fetchall()
        return [r[0] for r in rows]


def _clean_lane(lane: Optional[str]) -> str:
    lane = (lane or "default").strip()[:120]
    return lane or "default"

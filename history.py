"""Read past delegated subagent runs from Hermes's own session store, once, read-only.

The subagent_stop hook only sees runs from the day the plugin is installed. A Hermes that has
been in use for months already holds that history in its session database (state.db):

- each synchronous delegate_task call leaves a role='tool' message whose content is the joined
  result, {"results": [{"status", "duration_seconds", ...}, ...]}, the same status and duration
  the hook receives (tools/delegate_tool_dispatch.py);
- the assistant message that made the call holds its arguments, so each task's requested role
  (leaf or orchestrator);
- each background delegation leaves a row in async_delegations (task_json, result_json).

Hermes has no public plugin API for reading that history, so this opens state.db with SQLite's
read-only mode (file:...?mode=ro, PRAGMA query_only), behind a hard row cap and a time budget,
and never writes to it. Only role, start, duration and success are taken: never a goal, summary,
tool trace, message text or anything else. A store whose layout differs (older or newer Hermes),
a locked or missing file, or any error gives an empty or partial result, never an exception.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

# Bounds. A two-year history with ~12k delegated runs reads in well under a second.
MAX_RUNS = 6000            # newest runs kept (the run store keeps 2000 per lane)
MAX_PARENT_SESSIONS = 4000 # sessions that delegated, newest first
MAX_CONTENT_BYTES = 2_000_000  # a result message larger than this is skipped, never parsed
TIME_BUDGET_S = 1.5        # wall time for the whole read; SQLite is interrupted past it
BUSY_TIMEOUT_MS = 200      # never wait long for a writer
_CHUNK = 300               # session ids per IN (...) query

_STATUSES = {"completed": True, "failed": False, "error": False, "timeout": False}
_ROLES = {"leaf", "orchestrator"}
_NEEDS = {"sessions": {"source", "parent_session_id", "started_at", "ended_at"},
          "messages": {"session_id", "role", "content", "tool_call_id", "tool_calls", "tool_name",
                       "timestamp"}}


def _role(value) -> str:
    """Hermes's own rule (tools/delegate_tool._normalize_role): unknown or empty is leaf."""
    v = str(value).strip().lower() if value else "leaf"
    return v if v in _ROLES else "leaf"


def _json(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value or value[0] not in "{[":
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def _call_roles(tool_calls) -> dict:
    """tool_call_id -> list of per-task roles, for each delegate_task call in one message."""
    out = {}
    calls = _json(tool_calls)
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        if fn.get("name") != "delegate_task" or not call.get("id"):
            continue
        args = _json(fn.get("arguments")) or {}
        if not isinstance(args, dict):
            continue
        top = args.get("role")
        tasks = args.get("tasks")
        tasks = _json(tasks) if isinstance(tasks, str) else tasks
        if isinstance(tasks, list) and tasks:
            out[str(call["id"])] = [_role((t.get("role") if isinstance(t, dict) else None) or top)
                                    for t in tasks]
        else:
            out[str(call["id"])] = [_role(top)]
    return out


def _entries(payload) -> list:
    if not isinstance(payload, dict):
        return []
    found = []
    for key in ("results", "inline_results"):
        if isinstance(payload.get(key), list):
            found += [e for e in payload[key] if isinstance(e, dict)]
    if not found and "status" in payload and "duration_seconds" in payload:
        found = [payload]  # a single background result
    return found


def _end(run) -> float:
    return run[1] + run[2]


def _run(entry: dict, end_ts, role: str):
    """(lane, started_at, duration_s, ok) or None, by the same rule the subagent_stop hook uses."""
    ok = _STATUSES.get(entry.get("status") if isinstance(entry.get("status"), str) else "")
    try:
        seconds = float(entry.get("duration_seconds") or 0)
        end = float(end_ts)
    except (TypeError, ValueError):
        return None
    if ok is None or not math.isfinite(seconds) or seconds <= 0 or not math.isfinite(end):
        return None
    return (f"subagent:{role}", end - seconds, seconds, ok)


class _OutOfTime(Exception):
    pass


def _columns(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def read_delegated_runs(path, *, before: Optional[float] = None, max_runs: int = MAX_RUNS,
                        budget_s: float = TIME_BUDGET_S,
                        clock: Callable[[], float] = time.monotonic) -> dict:
    """Past delegated runs from Hermes's state.db, newest (by end) first, ending at or before `before`.

    Returns {"runs": [(lane, started_at, duration_s, ok)], "partial": bool, "reason": str|None,
    "scanned": {...}}. Never raises and never writes."""
    out = {"runs": [], "partial": False, "reason": None,
           "scanned": {"sessions": 0, "result_messages": 0, "background": 0, "skipped": 0}}
    deadline = clock() + max(budget_s, 0.05)
    p = Path(path) if path else None
    if p is None or not p.is_file():
        out["reason"] = "no session history file"
        return out
    conn = None
    runs: list = []
    try:
        conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True,
                               timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # SQLite checks this every 2000 VM steps; returning non-zero aborts the statement.
        conn.set_progress_handler(lambda: 1 if clock() > deadline else 0, 2000)
        for table, need in _NEEDS.items():
            if not need <= _columns(conn, table):
                out["reason"] = f"session store layout not recognised ({table})"
                return out
        cutoff = float(before) if before is not None else float("inf")
        seen: set = set()

        parents = [r[0] for r in conn.execute(
            "SELECT parent_session_id, MAX(COALESCE(ended_at, started_at)) AS last FROM sessions "
            "WHERE source = 'subagent' AND parent_session_id IS NOT NULL "
            "GROUP BY parent_session_id ORDER BY last DESC LIMIT ?", (MAX_PARENT_SESSIONS,))]
        out["scanned"]["sessions"] = len(parents)
        for i in range(0, len(parents), _CHUNK):
            if len(runs) >= max_runs:
                break
            chunk = parents[i:i + _CHUNK]
            marks = ",".join("?" * len(chunk))
            roles: dict = {}
            for (calls,) in conn.execute(
                    f"SELECT tool_calls FROM messages WHERE session_id IN ({marks}) AND role = 'assistant' "
                    "AND tool_calls LIKE '%delegate_task%' AND length(tool_calls) <= ?",
                    (*chunk, MAX_CONTENT_BYTES)):
                roles.update(_call_roles(calls))
            has_uid = "message_uid" in _columns(conn, "messages")
            rows = conn.execute(
                f"SELECT timestamp, tool_call_id, {'message_uid' if has_uid else 'NULL'}, content "
                f"FROM messages WHERE session_id IN ({marks}) AND role = 'tool' "
                "AND tool_name = 'delegate_task' AND length(content) <= ? ORDER BY timestamp DESC",
                (*chunk, MAX_CONTENT_BYTES))
            for ts, call_id, uid, content in rows:
                if clock() > deadline:
                    raise _OutOfTime
                key = uid or call_id or None
                if key is not None and key in seen:
                    continue  # a compaction clone of a message already read
                if key is not None:
                    seen.add(key)
                out["scanned"]["result_messages"] += 1
                entries = _entries(_json(content))
                if not entries:
                    out["scanned"]["skipped"] += 1  # a dispatch handle, a spilled preview, an error
                    continue
                task_roles = roles.get(str(call_id), [])
                for entry in entries:
                    idx = entry.get("task_index")
                    role = task_roles[idx] if isinstance(idx, int) and 0 <= idx < len(task_roles) else (
                        task_roles[0] if len(set(task_roles)) == 1 else "leaf")
                    run = _run(entry, ts, role)
                    if run is not None and run[1] + run[2] <= cutoff:
                        runs.append(run)

        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "async_delegations" in tables and {
                "state", "completed_at", "task_json", "result_json"} <= _columns(conn, "async_delegations"):
            for state, completed, task_json, result_json in conn.execute(
                    "SELECT state, completed_at, task_json, result_json FROM async_delegations "
                    "WHERE state NOT IN ('running', 'stalling', 'finalizing') AND completed_at IS NOT NULL "
                    "AND length(result_json) <= ? ORDER BY completed_at DESC LIMIT ?",
                    (MAX_CONTENT_BYTES, max_runs)):
                if clock() > deadline:
                    raise _OutOfTime
                out["scanned"]["background"] += 1
                task = _json(task_json) if isinstance(_json(task_json), dict) else {}
                role = _role(task.get("role"))
                for entry in _entries(_json(result_json)):
                    run = _run(entry, completed, role)
                    if run is not None and run[1] + run[2] <= cutoff:
                        runs.append(run)

        runs.sort(key=_end, reverse=True)
        out["runs"] = runs[:max_runs]
        out["partial"] = len(runs) > max_runs or len(parents) >= MAX_PARENT_SESSIONS
    except (sqlite3.OperationalError, _OutOfTime) as exc:
        interrupted = isinstance(exc, _OutOfTime) or "interrupt" in str(exc).lower()
        out["partial"] = True
        out["reason"] = "time budget reached" if interrupted else "session store busy or unreadable"
        out["runs"] = sorted(runs, key=_end, reverse=True)[:max_runs]
    except Exception:
        out["runs"] = []
        out["reason"] = "session store unreadable"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out

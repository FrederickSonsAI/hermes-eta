"""Tool handlers. Each returns a JSON string and never raises."""

from __future__ import annotations

import json
import math
import shlex
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .estimator import Run, estimate, lane_stats
from .store import RunStore

# Kanban statuses that still need work, and run outcomes that count as failed attempts.
# "blocked" is neither: it usually means the task waits on a person.
_OPEN = {"triage", "todo", "scheduled", "ready", "running", "review"}
_FAILED = {"crashed", "timed_out", "spawn_failed", "gave_up"}
_KANBAN_HISTORY_TASKS = 15


class BadInput(ValueError):
    pass


def _as_dict(result) -> dict:
    if isinstance(result, dict):
        return result
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return {"error": "unreadable tool result"}
    return data if isinstance(data, dict) else {"error": "unexpected tool result"}


def _tz(name: Optional[str]):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _num(args: dict, key: str, cast=float, lo=None, hi=None, strict_lo=False):
    value = args.get(key)
    if value is None or value == "":
        return None
    try:
        num = cast(value)
    except (TypeError, ValueError):
        raise BadInput(f"{key} must be a number")
    if isinstance(num, float) and math.isnan(num):
        raise BadInput(f"{key} must be a number")
    if lo is not None and (num <= lo if strict_lo else num < lo):
        raise BadInput(f"{key} must be {'more than' if strict_lo else 'at least'} {lo}")
    if hi is not None and num > hi:
        raise BadInput(f"{key} must be at most {hi}")
    return num


def _ts(value) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _stats_dict(stats) -> dict:
    return {
        "runs_total": stats.runs_total,
        "runs_3h": stats.runs_3h,
        "runs_24h": stats.runs_24h,
        "success_rate": None if stats.success_rate is None else round(stats.success_rate, 3),
        "median_run_minutes": None if stats.median_duration_s is None else round(stats.median_duration_s / 60, 1),
        "slow_run_minutes": None if stats.p90_duration_s is None else round(stats.p90_duration_s / 60, 1),
        "fail_streak": stats.fail_streak,
    }


class Handlers:
    def __init__(self, store: RunStore, default_tz: Callable[[], Optional[str]] = lambda: None,
                 clock: Callable[[], float] = time.time,
                 dispatch: Optional[Callable[..., str]] = None):
        self.store = store
        self.default_tz = default_tz
        self.clock = clock
        self.dispatch = dispatch

    def _kanban(self, assignee: str, board: Optional[str], **kwargs) -> dict:
        """Read open work and recent run history for one assignee through Hermes's own
        kanban tools (same approvals and scoping as if the model had called them)."""
        if self.dispatch is None:
            raise BadInput("kanban tools are not available in this session")
        list_args = {"assignee": assignee, "limit": 200}
        if board:
            list_args["board"] = board
        listing = _as_dict(self.dispatch("kanban_list", list_args, **kwargs))
        if "error" in listing or "tasks" not in listing:
            raise BadInput(f"kanban_list failed: {listing.get('error', 'no tasks field')}"
                           " (is the kanban toolset enabled for this profile?)")
        tasks = listing["tasks"]
        open_tasks = [t for t in tasks if t.get("status") in _OPEN]
        blocked = [t for t in tasks if t.get("status") == "blocked"]
        running = [t for t in tasks if t.get("status") == "running"]
        done = sorted((t for t in tasks if t.get("status") == "done"),
                      key=lambda t: _ts(t.get("completed_at")) or 0, reverse=True)
        runs: list[Run] = []
        for task in done[:_KANBAN_HISTORY_TASKS]:
            try:
                show_args = {"task_id": task["id"], **({"board": board} if board else {})}
                shown = _as_dict(self.dispatch("kanban_show", show_args, **kwargs))
            except Exception:
                shown = {}
            task_runs = shown.get("runs") if isinstance(shown, dict) else None
            if task_runs:
                for r in task_runs:
                    start, end = _ts(r.get("started_at")), _ts(r.get("ended_at"))
                    outcome = r.get("outcome")
                    if start is None or end is None or outcome not in _FAILED | {"completed"}:
                        continue
                    runs.append(Run(started_at=start, duration_s=max(end - start, 0),
                                    ok=outcome == "completed"))
            else:  # fall back to the task's own timestamps: one successful run
                start, end = _ts(task.get("started_at")), _ts(task.get("completed_at"))
                if start is not None and end is not None:
                    runs.append(Run(started_at=start, duration_s=max(end - start, 0), ok=True))
        return {"open": len(open_tasks), "blocked": len(blocked), "running": len(running),
                "runs": runs, "truncated": bool(listing.get("truncated"))}

    def eta_estimate(self, args: dict, **kwargs) -> str:
        try:
            remaining = _num(args, "remaining_items", int, lo=0)
            tz_name = args.get("timezone") or self.default_tz()
            tz = _tz(tz_name)
            if tz_name and tz is None:
                raise BadInput(f"unknown timezone: {tz_name} (use an IANA name like America/New_York)")
            avg = _num(args, "avg_run_minutes", float, lo=0, strict_lo=True)
            success = _num(args, "success_rate", float, lo=0, hi=1)
            queue_ahead = _num(args, "queue_ahead", int, lo=0) or 0
            lanes = _num(args, "lanes", int, lo=1) or 1
            cap = _num(args, "daily_run_cap", int, lo=0)
            left = _num(args, "runs_left_today", int, lo=0)
            reset_hour = _num(args, "reset_hour", int, lo=0, hi=23) or 0
            review = _num(args, "review_minutes", float, lo=0) or 0.0
            waiting = (args.get("waiting_on") or "").strip() or None

            now_s = self.clock()
            lane = (args.get("lane") or "").strip()
            assignee = (args.get("kanban_assignee") or "").strip()
            notes = []
            runs = self.store.runs(lane) if lane else []
            if assignee:
                kb = self._kanban(assignee, (args.get("board") or "").strip() or None, **kwargs)
                runs = runs + kb["runs"]
                if remaining is None:
                    remaining = kb["open"]
                if kb["blocked"]:
                    notes.append(f"{kb['blocked']} task(s) are blocked and not counted.")
                    if not waiting and remaining == 0:
                        waiting = f"someone unblocks {kb['blocked']} blocked task(s)"
                if kb["truncated"]:
                    notes.append("Board has more than 200 tasks for this assignee; counts are partial.")
            if remaining is None:
                raise BadInput("remaining_items is required unless kanban_assignee is given")
            stats = lane_stats(runs, now_s) if runs else None
            est = estimate(
                remaining_items=remaining, stats=stats,
                avg_run_s=avg * 60 if avg else None, success_rate=success,
                queue_ahead=queue_ahead, lanes=lanes, daily_run_cap=cap, runs_left_today=left,
                reset_hour=reset_hour, review_s=review * 60, waiting_on_person=waiting,
                now=datetime.fromtimestamp(now_s, timezone.utc), tz=tz)
            return json.dumps({
                "status": est.status,
                "answer": est.headline,
                "likely_at": est.likely_at.isoformat() if est.likely_at else None,
                "worst_at": est.worst_at.isoformat() if est.worst_at else None,
                "key_numbers": est.key_numbers,
                "biggest_risk": est.biggest_risk,
                "clock_starts_when": est.clock_starts_when,
                "confidence": est.confidence,
                "notes": notes,
                "lane": lane or assignee or None,
                "history_used": stats is not None,
                "as_of": datetime.fromtimestamp(now_s, tz or timezone.utc).isoformat(),
            })
        except BadInput as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:  # handlers must never raise
            return json.dumps({"error": f"eta_estimate failed: {exc}"})

    def eta_record_run(self, args: dict, **kwargs) -> str:
        try:
            lane = (args.get("lane") or "").strip()
            minutes = _num(args, "duration_minutes", float, lo=0)
            if not lane or minutes is None or "ok" not in args:
                return json.dumps({"error": "need lane, duration_minutes (0 or more) and ok"})
            ok = args["ok"] if isinstance(args["ok"], bool) else str(args["ok"]).lower() in {"true", "1", "yes"}
            self.store.record(lane, minutes * 60, ok, started_at=self.clock() - minutes * 60)
            stats = lane_stats(self.store.runs(lane), self.clock())
            return json.dumps({"recorded": True, "lane": lane, "stats": _stats_dict(stats)})
        except BadInput as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return json.dumps({"error": f"eta_record_run failed: {exc}"})

    def eta_history(self, args: dict, **kwargs) -> str:
        try:
            lane = (args.get("lane") or "").strip()
            lanes = [lane] if lane else self.store.lanes()
            now_s = self.clock()
            return json.dumps({"lanes": {name: _stats_dict(lane_stats(self.store.runs(name), now_s))
                                         for name in lanes}})
        except Exception as exc:
            return json.dumps({"error": f"eta_history failed: {exc}"})

    def slash_eta(self, raw_args: str = "", **kwargs) -> str:
        """/eta                         history for every recorded lane
        /eta <lane> <remaining> [ahead] estimate from recorded runs
        /eta @<assignee> [board]        estimate a kanban assignee's open work"""
        usage = "Usage: /eta <lane> <remaining items> [queued ahead], or /eta @<kanban assignee> [board]"
        try:
            parts = shlex.split(raw_args or "")
        except ValueError:
            parts = (raw_args or "").split()
        if not parts:
            data = json.loads(self.eta_history({}))
            if "error" in data:
                return data["error"]
            if not data.get("lanes"):
                return "No runs recorded yet. " + usage
            lines = []
            for name, st in data["lanes"].items():
                ok = "n/a" if st["success_rate"] is None else f"{round(st['success_rate'] * 100)}%"
                typical = (f"typical {st['median_run_minutes']} min"
                           if st["median_run_minutes"] is not None else "no durations yet")
                lines.append(f"{name}: {st['runs_24h']} runs in 24h, {ok} ok, {typical}")
            return "\n".join(lines)
        if parts[0].startswith("@") and len(parts[0]) > 1:
            args = {"kanban_assignee": parts[0][1:]}
            if len(parts) > 1:
                args["board"] = parts[1]
        elif len(parts) >= 2 and parts[1].isdigit():
            args = {"lane": parts[0], "remaining_items": int(parts[1])}
            if len(parts) > 2 and parts[2].isdigit():
                args["queue_ahead"] = int(parts[2])
        else:
            return usage
        data = json.loads(self.eta_estimate(args, **kwargs))
        if "error" in data:
            return data["error"]
        text = data["answer"]
        if data.get("biggest_risk") and data["status"] == "ok":
            text += f" Biggest risk: {data['biggest_risk']}"
        for note in data.get("notes") or []:
            text += f" {note}"
        return text

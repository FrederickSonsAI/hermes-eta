"""Tool handlers. Each returns a JSON string and never raises."""

from __future__ import annotations

import json
import shlex
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .estimator import estimate, lane_stats
from .store import RunStore


def _tz(name: Optional[str]):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _num(args: dict, key: str, cast=float):
    value = args.get(key)
    if value is None or value == "":
        return None
    return cast(value)


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
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.default_tz = default_tz
        self.clock = clock

    def eta_estimate(self, args: dict, **kwargs) -> str:
        try:
            remaining = _num(args, "remaining_items", int)
            if remaining is None or remaining < 0:
                return json.dumps({"error": "remaining_items must be a whole number of 0 or more"})
            tz_name = args.get("timezone") or self.default_tz()
            tz = _tz(tz_name)
            if tz_name and tz is None:
                return json.dumps({"error": f"unknown timezone: {tz_name}"})
            now_s = self.clock()
            lane = (args.get("lane") or "").strip()
            stats = lane_stats(self.store.runs(lane), now_s) if lane else None
            if stats is not None and stats.runs_total == 0:
                stats = None
            avg = _num(args, "avg_run_minutes")
            review = _num(args, "review_minutes") or 0.0
            est = estimate(
                remaining_items=remaining,
                stats=stats,
                avg_run_s=avg * 60 if avg else None,
                success_rate=_num(args, "success_rate"),
                queue_ahead=_num(args, "queue_ahead", int) or 0,
                lanes=_num(args, "lanes", int) or 1,
                daily_run_cap=_num(args, "daily_run_cap", int),
                runs_left_today=_num(args, "runs_left_today", int),
                reset_hour=_num(args, "reset_hour", int) or 0,
                review_s=review * 60,
                waiting_on_person=(args.get("waiting_on") or "").strip() or None,
                now=datetime.fromtimestamp(now_s, timezone.utc),
                tz=tz,
            )
            out = {
                "status": est.status,
                "answer": est.headline,
                "likely_at": est.likely_at.isoformat() if est.likely_at else None,
                "worst_at": est.worst_at.isoformat() if est.worst_at else None,
                "key_numbers": est.key_numbers,
                "biggest_risk": est.biggest_risk,
                "clock_starts_when": est.clock_starts_when,
                "confidence": est.confidence,
                "lane": lane or None,
                "history_used": stats is not None,
                "as_of": datetime.fromtimestamp(now_s, tz or timezone.utc).isoformat(),
            }
            return json.dumps(out)
        except Exception as exc:  # handlers must never raise
            return json.dumps({"error": f"eta_estimate failed: {exc}"})

    def eta_record_run(self, args: dict, **kwargs) -> str:
        try:
            lane = (args.get("lane") or "").strip()
            minutes = _num(args, "duration_minutes")
            if not lane or minutes is None or minutes < 0 or "ok" not in args:
                return json.dumps({"error": "need lane, duration_minutes (0 or more) and ok"})
            ok = args["ok"] if isinstance(args["ok"], bool) else str(args["ok"]).lower() in {"true", "1", "yes"}
            self.store.record(lane, minutes * 60, ok, started_at=self.clock() - minutes * 60)
            stats = lane_stats(self.store.runs(lane), self.clock())
            return json.dumps({"recorded": True, "lane": lane, "stats": _stats_dict(stats)})
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
        """/eta <lane> <remaining_items> [queue_ahead]  or  /eta  (history for all lanes)."""
        try:
            parts = shlex.split(raw_args or "")
        except ValueError:
            parts = (raw_args or "").split()
        if not parts:
            data = json.loads(self.eta_history({}))
            if not data.get("lanes"):
                return "No runs recorded yet. Usage: /eta <lane> <remaining items> [queued ahead]"
            lines = [f"{n}: {s['runs_24h']} runs in 24h, "
                     f"{'n/a' if s['success_rate'] is None else str(round(s['success_rate'] * 100)) + '%'} ok, "
                     f"typical {s['median_run_minutes']} min"
                     for n, s in data["lanes"].items()]
            return "\n".join(lines)
        if len(parts) < 2 or not parts[1].isdigit():
            return "Usage: /eta <lane> <remaining items> [queued ahead]"
        args = {"lane": parts[0], "remaining_items": int(parts[1])}
        if len(parts) > 2 and parts[2].isdigit():
            args["queue_ahead"] = int(parts[2])
        data = json.loads(self.eta_estimate(args))
        if "error" in data:
            return data["error"]
        text = data["answer"]
        if data.get("biggest_risk") and data["status"] == "ok":
            text += f" Biggest risk: {data['biggest_risk']}"
        return text

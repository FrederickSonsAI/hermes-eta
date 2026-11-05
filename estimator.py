"""Pure ETA math. No Hermes imports, no I/O, so it is easy to test and reason about.

The model: work is a number of runs that each take some time and succeed with some
probability. Runs needed = remaining items / success rate. Wall time = (runs ahead in the
queue + runs needed) * average run time / parallel lanes, stretched past a daily reset when
a daily run cap cannot cover the work. The answer is a range (likely / worst case), never a
single precise time, plus the numbers it rests on and the biggest risk.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from statistics import median
from typing import Optional

# A lane that failed this many runs in a row at the end of its history is "stuck".
STUCK_STREAK = 3
# Below this success rate the estimate is too unstable to be honest.
MIN_SUCCESS_RATE = 0.15
# With fewer samples than this, say the estimate is low-confidence.
LOW_SAMPLES = 5
# Past this many days of capped work there is no useful ETA.
MAX_DAYS = 366


@dataclass
class Run:
    started_at: float  # unix seconds
    duration_s: float
    ok: bool


@dataclass
class LaneStats:
    runs_total: int
    runs_3h: int
    runs_24h: int
    success_rate: Optional[float]  # None when there is no history
    median_duration_s: Optional[float]
    p90_duration_s: Optional[float]
    fail_streak: int  # consecutive failures at the end of the history


@dataclass
class Estimate:
    status: str  # "ok" | "blocked_on_person" | "unbounded" | "no_data" | "done"
    headline: str
    likely_at: Optional[datetime] = None
    worst_at: Optional[datetime] = None
    runs_needed: Optional[int] = None
    key_numbers: dict = field(default_factory=dict)
    biggest_risk: Optional[str] = None
    clock_starts_when: Optional[str] = None
    confidence: str = "normal"


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values")
    k = (len(ordered) - 1) * pct
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def lane_stats(runs: list[Run], now: float, window_s: float = 24 * 3600) -> LaneStats:
    """Summarise a lane's history. Success rate and durations use the last `window_s`,
    falling back to all history when the window is empty."""
    runs = sorted(runs, key=lambda r: r.started_at)
    recent = [r for r in runs if now - r.started_at <= window_s] or runs
    streak = 0
    for r in reversed(runs):
        if r.ok:
            break
        streak += 1
    durations = [r.duration_s for r in recent if r.duration_s > 0]
    return LaneStats(
        runs_total=len(runs),
        runs_3h=sum(1 for r in runs if now - r.started_at <= 3 * 3600),
        runs_24h=sum(1 for r in runs if now - r.started_at <= 24 * 3600),
        success_rate=(sum(1 for r in recent if r.ok) / len(recent)) if recent else None,
        median_duration_s=median(durations) if durations else None,
        p90_duration_s=_percentile(durations, 0.9) if durations else None,
        fail_streak=streak,
    )


def _next_reset(after_utc: datetime, reset_hour: int, tz: tzinfo) -> datetime:
    """Next local `reset_hour` strictly after `after_utc`, returned in UTC (DST-safe)."""
    local = after_utc.astimezone(tz)
    candidate = datetime(local.year, local.month, local.day, reset_hour, tzinfo=tz)
    if candidate.astimezone(timezone.utc) <= after_utc:
        nxt = local.date() + timedelta(days=1)
        candidate = datetime(nxt.year, nxt.month, nxt.day, reset_hour, tzinfo=tz)
    return candidate.astimezone(timezone.utc)


def _finish_time(start_utc: datetime, runs: int, run_s: float, lanes: int,
                 runs_left_today: Optional[int], daily_cap: Optional[int],
                 reset_hour: int, tz: tzinfo) -> datetime:
    """Runs go `lanes` at a time. A daily cap pushes the rest past each reset.
    All arithmetic is in UTC so DST changes never shift the answer."""
    t = start_utc
    remaining = runs
    budget = runs_left_today if runs_left_today is not None else daily_cap
    if budget is not None and budget > 0:
        batch = min(remaining, budget)
        t += timedelta(seconds=math.ceil(batch / lanes) * run_s)
        remaining -= batch
    if remaining <= 0:
        return t
    if daily_cap is None:
        if runs_left_today is not None:  # today's allowance is spent; the rest waits for the reset
            t = _next_reset(t, reset_hour, tz)
        return t + timedelta(seconds=math.ceil(remaining / lanes) * run_s)
    # daily_cap > 0 here (estimate() rejects 0). Whole days of capped work, then the tail.
    full_days, tail = divmod(remaining, daily_cap)
    if tail == 0:
        full_days, tail = full_days - 1, daily_cap
    if full_days > MAX_DAYS:
        raise OverflowError("more than a year of capped work")
    reset = _next_reset(t, reset_hour, tz)
    for _ in range(full_days):
        reset = _next_reset(reset, reset_hour, tz)
    return reset + timedelta(seconds=math.ceil(tail / lanes) * run_s)


def _range(now_utc, now, tz, queue_ahead, runs_needed, runs_worst, med, slow, lanes,
           runs_left_today, daily_run_cap, reset_hour, review_s):
    likely = (_finish_time(now_utc, queue_ahead + runs_needed, med, lanes, runs_left_today,
                           daily_run_cap, reset_hour, tz) + timedelta(seconds=review_s)).astimezone(tz)
    worst = (_finish_time(now_utc, queue_ahead + runs_worst, slow, lanes, runs_left_today,
                          daily_run_cap, reset_hour, tz) + timedelta(seconds=review_s * 2)).astimezone(tz)
    if worst <= likely:
        worst = likely + timedelta(minutes=max(5, (likely - now).total_seconds() / 600))
    return likely, worst


def estimate(*, remaining_items: int, stats: Optional[LaneStats] = None,
             avg_run_s: Optional[float] = None, success_rate: Optional[float] = None,
             queue_ahead: int = 0, lanes: int = 1, daily_run_cap: Optional[int] = None,
             runs_left_today: Optional[int] = None, reset_hour: int = 0,
             review_s: float = 0.0, waiting_on_person: Optional[str] = None,
             now: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> Estimate:
    """Compute an ETA. Explicit arguments override what `stats` says."""
    tz = tz or timezone.utc
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now = now_utc.astimezone(tz)
    lanes = max(int(lanes or 1), 1)
    queue_ahead = max(int(queue_ahead or 0), 0)

    if remaining_items <= 0 and queue_ahead == 0:
        return Estimate(status="done", headline="Nothing left to run.")

    sr = success_rate if success_rate is not None else (stats.success_rate if stats else None)
    med = avg_run_s if avg_run_s is not None else (stats.median_duration_s if stats else None)
    p90 = avg_run_s * 1.5 if avg_run_s is not None else (stats.p90_duration_s if stats else None)
    samples = stats.runs_total if stats else 0
    streak = stats.fail_streak if stats else 0

    numbers = {"remaining_items": remaining_items, "queue_ahead": queue_ahead, "lanes": lanes}
    if sr is not None:
        numbers["success_rate"] = round(sr, 3)
    if med is not None:
        numbers["median_run_minutes"] = round(med / 60, 1)
    if stats:
        numbers.update(runs_3h=stats.runs_3h, runs_24h=stats.runs_24h, fail_streak=streak)
    if daily_run_cap is not None:
        numbers["daily_run_cap"] = daily_run_cap
    if runs_left_today is not None:
        numbers["runs_left_today"] = runs_left_today

    if daily_run_cap == 0:
        return Estimate(
            status="unbounded",
            headline="No honest ETA: the daily run cap is 0, so nothing can run.",
            key_numbers=numbers, biggest_risk="The run cap.")

    if streak >= STUCK_STREAK or (sr is not None and sr < MIN_SUCCESS_RATE):
        why = (f"the last {streak} runs failed in a row" if streak >= STUCK_STREAK
               else f"only {round((sr or 0) * 100)}% of recent runs succeed")
        return Estimate(
            status="unbounded",
            headline=f"No honest ETA: {why}. Fix the failing step first, then re-estimate.",
            key_numbers=numbers, biggest_risk="The same failure repeating.")

    if med is None:
        return Estimate(
            status="no_data",
            headline="No run history yet. Record a few runs or pass avg_run_minutes.",
            key_numbers=numbers)

    sr_eff = sr if sr is not None else 1.0
    runs_needed = math.ceil(remaining_items / sr_eff) if remaining_items > 0 else 0
    # Worst case: success one notch worse, slow (p90) runs.
    sr_worst = max(sr_eff - 0.15, MIN_SUCCESS_RATE) if sr is not None else 0.85
    runs_worst = math.ceil(remaining_items / sr_worst) if remaining_items > 0 else 0
    numbers["runs_needed"] = runs_needed

    clock = waiting_on_person or None
    slow = max(p90 or med * 1.5, med)
    try:
        likely, worst = _range(now_utc, now, tz, queue_ahead, runs_needed, runs_worst, med, slow,
                               lanes, runs_left_today, daily_run_cap, reset_hour, review_s)
    except OverflowError:
        return Estimate(status="unbounded",
                        headline="No honest ETA: at this run cap the work takes more than a year.",
                        key_numbers=numbers, biggest_risk="The daily run cap.")

    risk = None
    if daily_run_cap is not None or runs_left_today is not None:
        cap_today = runs_left_today if runs_left_today is not None else daily_run_cap
        if cap_today is not None and queue_ahead + runs_needed > cap_today:
            risk = "The daily run cap runs out first, so the rest waits for the reset."
    if risk is None and queue_ahead > runs_needed:
        risk = f"{queue_ahead} runs are queued ahead of this work."
    if risk is None and sr is not None and sr < 0.7:
        risk = f"About {round((1 - sr) * 100)}% of runs fail and have to be retried."
    if risk is None:
        risk = "Runs taking longer than usual."

    confidence = "low" if samples < LOW_SAMPLES and avg_run_s is None else "normal"
    status = "blocked_on_person" if clock else "ok"
    headline = describe_range(likely, worst, now)
    if clock:
        headline = f"Clock starts when {clock}. After that: {duration_words(likely - now)} to {duration_words(worst - now)}."
    return Estimate(status=status, headline=headline, likely_at=likely, worst_at=worst,
                    runs_needed=runs_needed, key_numbers=numbers, biggest_risk=risk,
                    clock_starts_when=clock, confidence=confidence)


def duration_words(delta: timedelta) -> str:
    minutes = max(int(round(delta.total_seconds() / 60)), 1)
    if minutes < 90:
        return f"{minutes} min"
    hours = minutes / 60
    if hours < 48:
        return f"{round(hours, 1):g} h"
    return f"{round(hours / 24, 1):g} days"


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _day(dt: datetime, now: datetime) -> str:
    days = (dt.date() - now.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days < 7:
        return dt.strftime("%a %b %d").replace(" 0", " ")
    return dt.strftime("%b %d, %Y").replace(" 0", " ")


def describe_range(likely: datetime, worst: datetime, now: datetime) -> str:
    if _clock(likely) == _clock(worst) and _day(likely, now) == _day(worst, now):
        return f"Likely around {_clock(likely)} {_day(likely, now)}."
    if _day(likely, now) == _day(worst, now):
        return f"Likely between {_clock(likely)} and {_clock(worst)} {_day(likely, now)}."
    return f"Likely {_clock(likely)} {_day(likely, now)}, {_clock(worst)} {_day(worst, now)} at worst."

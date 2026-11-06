"""Pure ETA math. No Hermes imports, no I/O, so it is easy to test and reason about.

The model: work is a number of runs that each take some time and succeed with some
probability. The likely case is the typical one: when most runs succeed (rate 0.5 or more) the
typical item takes one run, otherwise remaining items / success rate runs on average (not rounded
up). Retries for failures go in the worst case, which rounds up at a lower success rate. Wall time = (runs ahead in the
queue + runs needed) * average run time / parallel lanes, stretched past a daily reset when
a daily run cap cannot cover the work. Runs already going are credited with the time they have
run. With few runs the worst case widens; with none, a rough answer is labelled as rough. The
answer is a range (likely / worst case), never a single precise time, plus the numbers it rests
on and the biggest risk.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from statistics import median
from typing import Optional, Sequence

# A lane that failed this many runs in a row at the end of its history is "stuck".
STUCK_STREAK = 3
# Below this success rate the estimate is too unstable to be honest.
MIN_SUCCESS_RATE = 0.15
# With fewer samples than this, say the estimate is low-confidence.
LOW_SAMPLES = 5
# Past this many days of capped work there is no useful ETA.
MAX_DAYS = 366
# With few samples the observed success rate is pulled toward this typical rate (weight PRIOR_RUNS
# runs), so one lucky or unlucky run does not decide the answer.
PRIOR_SUCCESS_RATE = 0.8
PRIOR_RUNS = 2
# A run must have been going at least this long before its elapsed time alone says anything.
MIN_ELAPSED_FOR_GUESS_S = 60


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
    samples: int = 0  # runs the success rate and durations were taken from
    successes: int = 0
    max_duration_s: Optional[float] = None


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
    confidence: str = "normal"  # "normal" | "low" (under 5 runs) | "rough" (no runs for this work)


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
    successes = sum(1 for r in recent if r.ok)
    return LaneStats(
        runs_total=len(runs),
        runs_3h=sum(1 for r in runs if now - r.started_at <= 3 * 3600),
        runs_24h=sum(1 for r in runs if now - r.started_at <= 24 * 3600),
        success_rate=(successes / len(recent)) if recent else None,
        median_duration_s=median(durations) if durations else None,
        p90_duration_s=_percentile(durations, 0.9) if durations else None,
        fail_streak=streak,
        samples=len(recent),
        successes=successes,
        max_duration_s=max(durations) if durations else None,
    )


def _next_reset(after_utc: datetime, reset_hour: int, tz: tzinfo) -> datetime:
    """Next local `reset_hour` strictly after `after_utc`, returned in UTC (DST-safe)."""
    local = after_utc.astimezone(tz)
    candidate = datetime(local.year, local.month, local.day, reset_hour, tzinfo=tz)
    if candidate.astimezone(timezone.utc) <= after_utc:
        nxt = local.date() + timedelta(days=1)
        candidate = datetime(nxt.year, nxt.month, nxt.day, reset_hour, tzinfo=tz)
    return candidate.astimezone(timezone.utc)


def _rounds(n: float, lanes: int) -> float:
    """Run lengths `n` runs take `lanes` at a time. Whole runs fill rounds; an expected fraction
    of a run (the likely case at a low success rate) adds that fraction of a round only when no
    lane is free in the last round."""
    if n <= 0:
        return 0.0
    whole = math.floor(n + 1e-9)
    frac = n - whole if n - whole > 1e-9 else 0.0
    rounds = float(math.ceil(whole / lanes)) if whole else 0.0
    if frac and whole % lanes == 0:
        rounds += frac
    return rounds


def likely_runs(remaining_items: int, success_rate: Optional[float]) -> float:
    """Runs the typical case takes. At a success rate of 0.5 or more the typical (median) item
    succeeds on its first run, so one run per item. Below that, the expected number of runs,
    remaining / rate, not rounded up: rounding up is a retry, and retries belong in the worst
    case."""
    if remaining_items <= 0:
        return 0.0
    if success_rate is None or success_rate >= 0.5:
        return float(remaining_items)
    return remaining_items / success_rate


def _finish_time(start_utc: datetime, runs: float, run_s: float, lanes: int,
                 runs_left_today: Optional[int], daily_cap: Optional[int],
                 reset_hour: int, tz: tzinfo) -> datetime:
    """Runs go `lanes` at a time. A daily cap pushes the rest past each reset.
    All arithmetic is in UTC so DST changes never shift the answer."""
    def span(n: float) -> timedelta:
        return timedelta(seconds=_rounds(n, lanes) * run_s)

    t = start_utc
    remaining = runs
    budget = runs_left_today if runs_left_today is not None else daily_cap
    if budget is None:
        return t + span(remaining)
    if budget > 0:
        batch = min(remaining, budget)
        t += span(batch)
        remaining -= batch
    if remaining <= 0:
        return t
    window = start_utc
    for _ in range(MAX_DAYS):
        # The next window opens at the next reset, or when the last batch finishes if that
        # batch ran past the reset (a big cap with slow runs can take longer than a day).
        window = max(t, _next_reset(window, reset_hour, tz))
        if daily_cap is None:  # only today's allowance was limited
            return window + span(remaining)
        batch = min(remaining, daily_cap)
        t = window + span(batch)
        remaining -= batch
        if remaining <= 0:
            return t
    raise OverflowError("more than a year of capped work")


def _range(now_utc, now, tz, queue_ahead, runs_needed, runs_worst, med, slow, lanes,
           runs_left_today, daily_run_cap, reset_hour, review_s, running=()):
    """Likely and worst finish. Runs already going (`running`: seconds elapsed each) are part of
    the work, so the time they have already run is taken off, never past the moment the
    slowest of them can finish. A run already past the slow mark gets no credit in the worst
    case: it may have to start over."""
    def finish(n, run_s, review, worst):
        end = _finish_time(now_utc, n, run_s, lanes, runs_left_today, daily_run_cap, reset_hour, tz)
        if running:
            credit = sum(0.0 if (worst and e >= run_s) else min(e, run_s) for e in running) / lanes
            floor = now_utc + timedelta(seconds=max(
                (run_s if (worst and e >= run_s) else max(run_s - e, 0.0)) for e in running))
            end = max(end - timedelta(seconds=credit), floor)
        return (end + timedelta(seconds=review)).astimezone(tz)

    likely = finish(queue_ahead + runs_needed, med, review_s, False)
    worst = finish(queue_ahead + runs_worst, slow, review_s * 2, True)
    if worst <= likely:
        worst = likely + timedelta(minutes=max(5, (likely - now).total_seconds() / 600))
    return likely, worst


def estimate(*, remaining_items: int, stats: Optional[LaneStats] = None,
             avg_run_s: Optional[float] = None, success_rate: Optional[float] = None,
             queue_ahead: int = 0, lanes: int = 1, daily_run_cap: Optional[int] = None,
             runs_left_today: Optional[int] = None, reset_hour: int = 0,
             review_s: float = 0.0, waiting_on_person: Optional[str] = None,
             running_elapsed_s: Sequence[float] = (), quiet_running: int = 0,
             rough_basis: Optional[str] = None,
             now: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> Estimate:
    """Compute an ETA. Explicit arguments override what `stats` says.

    `running_elapsed_s`: seconds each run that is already going has been running (those runs
    are part of `remaining_items`). `quiet_running`: how many of them have gone quiet.
    `rough_basis`: set when `stats` is not this work's own history (e.g. other lanes), in words.
    """
    tz = tz or timezone.utc
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now = now_utc.astimezone(tz)
    lanes = max(int(lanes or 1), 1)
    queue_ahead = max(int(queue_ahead or 0), 0)
    running = [max(float(e), 0.0) for e in (running_elapsed_s or ())][:max(remaining_items, 0)]

    if remaining_items <= 0 and queue_ahead == 0:
        return Estimate(status="done", headline="Nothing left to run.")

    samples = stats.samples if stats else 0
    streak = stats.fail_streak if stats else 0
    observed_sr = stats.success_rate if stats else None
    few = success_rate is None and observed_sr is not None and samples < LOW_SAMPLES
    if success_rate is not None:
        sr = success_rate
    elif few:  # shrink toward a typical rate so 1-4 runs cannot swing it to 0% or 100%
        sr = (stats.successes + PRIOR_SUCCESS_RATE * PRIOR_RUNS) / (samples + PRIOR_RUNS)
    else:
        sr = observed_sr
    med = avg_run_s if avg_run_s is not None else (stats.median_duration_s if stats else None)
    p90 = avg_run_s * 1.5 if avg_run_s is not None else (stats.p90_duration_s if stats else None)
    longest = None if avg_run_s is not None else (stats.max_duration_s if stats else None)

    numbers = {"remaining_items": remaining_items, "queue_ahead": queue_ahead, "lanes": lanes,
               "runs_used": samples}
    if observed_sr is not None or success_rate is not None:
        numbers["success_rate"] = round(success_rate if success_rate is not None else observed_sr, 3)
    if few:
        numbers["success_rate_used"] = round(sr, 3)
    if running:
        numbers["running_now"] = len(running)
        numbers["running_elapsed_minutes"] = [round(e / 60, 1) for e in running]
    if stats:
        numbers.update(runs_3h=stats.runs_3h, runs_24h=stats.runs_24h, fail_streak=streak)
    if daily_run_cap is not None:
        numbers["daily_run_cap"] = daily_run_cap
    if runs_left_today is not None:
        numbers["runs_left_today"] = runs_left_today

    clock = waiting_on_person or None

    def no_range(status: str, headline: str, **kw) -> Estimate:
        # No range to give, but a step that waits on a person still says when the clock starts.
        if clock:
            headline = f"Clock starts when {clock}. {headline}"
        return Estimate(status=status, headline=headline, key_numbers=numbers,
                        clock_starts_when=clock, **kw)

    if daily_run_cap == 0:
        return no_range("unbounded", "No honest ETA: the daily run cap is 0, so nothing can run.",
                        biggest_risk="The run cap.")

    if streak >= STUCK_STREAK or (sr is not None and sr < MIN_SUCCESS_RATE):
        why = (f"the last {streak} runs failed in a row" if streak >= STUCK_STREAK
               else f"only {round((sr or 0) * 100)}% of recent runs succeed")
        return no_range("unbounded",
                        f"No honest ETA: {why}. Fix the failing step first, then re-estimate.",
                        biggest_risk="The same failure repeating.")

    rough = rough_basis if (rough_basis and avg_run_s is None) else None
    if med is None and running and max(running) >= MIN_ELAPSED_FOR_GUESS_S:
        # Nothing has finished yet, only a run in progress. With no other knowledge, a run seen
        # e minutes in is as likely to be early as late in its life: typical total 2e, and 3e
        # more at the slow end (the usual 50% "delta t" interval).
        e = max(running)
        med, p90, longest = 2 * e, 4 * e, None
        rough = f"how long the current run has been going ({duration_words(timedelta(seconds=e))})"
    if med is None:
        return no_range("no_data",
                        "No honest ETA yet: there is no finished run to go on. Give a typical run "
                        "length (avg_run_minutes) or how long the current run has been going "
                        "(running_minutes), or let one run finish. Delegated subagent runs are "
                        "recorded automatically.")

    wide = rough is not None or (avg_run_s is None and samples < LOW_SAMPLES)
    sr_eff = sr if sr is not None else 1.0
    runs_likely = likely_runs(remaining_items, sr_eff)
    runs_needed = math.ceil(runs_likely - 1e-9)  # whole runs to plan for in the typical case
    # Worst case: success a notch worse, slow (p90) runs. With little data, both notches widen.
    drop = 0.3 if wide else 0.15
    sr_worst = max(sr_eff - drop, MIN_SUCCESS_RATE) if sr is not None else (0.7 if wide else 0.85)
    runs_worst = math.ceil(remaining_items / sr_worst) if remaining_items > 0 else 0
    numbers["runs_needed"] = runs_needed
    if runs_likely != runs_needed:
        numbers["runs_expected"] = round(runs_likely, 2)

    slow = max(p90 or med * 1.5, med)
    if wide:  # the slowest of a handful of runs says little about the slow tail
        slow = max(slow, longest or 0.0, med * 2)
    numbers["slow_run_minutes"] = round(slow / 60, 1)
    try:
        likely, worst = _range(now_utc, now, tz, queue_ahead, runs_likely, runs_worst, med, slow,
                               lanes, runs_left_today, daily_run_cap, reset_hour, review_s, running)
    except OverflowError:
        return no_range("unbounded", "No honest ETA: at this run cap the work takes more than a year.",
                        biggest_risk="The daily run cap.")

    risk = None
    overdue = sum(1 for e in running if e > slow)
    if overdue:
        numbers["running_past_slow_mark"] = overdue
        risk = (f"{overdue} run(s) already going longer than the slow mark "
                f"({duration_words(timedelta(seconds=slow))}); may be stuck.")
    if risk is None and quiet_running:
        risk = f"{quiet_running} running run(s) have gone quiet; may be stuck."
    if risk is None and (daily_run_cap is not None or runs_left_today is not None):
        cap_today = runs_left_today if runs_left_today is not None else daily_run_cap
        if cap_today is not None and queue_ahead + runs_needed > cap_today:
            risk = "The daily run cap runs out first, so the rest waits for the reset."
    if risk is None and queue_ahead > runs_needed:
        risk = f"{queue_ahead} runs are queued ahead of this work."
    if risk is None and sr is not None and sr < 0.7:
        risk = f"About {round((1 - sr) * 100)}% of runs fail and have to be retried."
    if risk is None and wide:
        risk = "Too few runs to know the slow end well."
    if risk is None:
        risk = "Runs taking longer than usual."

    confidence = "rough" if rough else ("low" if wide else "normal")
    status = "blocked_on_person" if clock else "ok"
    headline = describe_range(likely, worst, now)
    if clock:
        headline = f"Clock starts when {clock}. After that: {duration_words(likely - now)} to {duration_words(worst - now)}."
    if rough:
        headline = f"Rough guess: {headline} No finished runs for this work yet, so this rests only on {rough}."
    elif avg_run_s is None and samples:
        headline += f" Based on {samples} run{'' if samples == 1 else 's'}"
        headline += ", so the worst case is wide." if samples < LOW_SAMPLES else "."
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

"""Tool handlers. Each returns a JSON string and never raises."""

from __future__ import annotations

import json
import math
import shlex
import threading
import time
from datetime import datetime, timezone, tzinfo
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .estimator import MIN_ELAPSED_FOR_GUESS_S, Run, estimate, lane_stats
from .store import RunStore

# Kanban statuses that still need work, and run outcomes that count as failed attempts.
# "blocked" is neither: it usually means the task waits on a person. A reclaimed or stale run
# (worker died, claim expired) is an attempt that did not land, as Hermes's own failure breaker
# counts it; an operator's reclaim (manual reclaim, ancestor reopened, task archived) is not.
_OPEN = {"triage", "todo", "scheduled", "ready", "running", "review"}
_FAILED = {"crashed", "timed_out", "spawn_failed", "gave_up", "reclaimed", "stale"}
# A run that hands work to review finished its attempt, unless the review that follows sends it
# back. changes_requested ends the reviewer's own run: the review was done, so it is a success
# for the reviewer, and it marks the hand-off it rejected as a failed attempt.
_SUCCEEDED = {"completed", "review_requested", "changes_requested"}
_REVIEW_VERDICTS = {"completed", "changes_requested"}


def _operator_reclaim(run: dict) -> bool:
    """A reclaim an operator caused carries no error (ancestor reopened, archived mid-run) or a
    manual_reclaim one; Hermes resets the task's failure count for those."""
    error = run.get("error")
    return not error or str(error).startswith("manual_reclaim")
# One kanban_list call reads the whole board for the assignee; kanban_show (the only tool that
# returns per-run outcomes) is called for at most this many tasks: running ones first, then, in
# turn, the most recently finished and the most recently first-claimed of the tasks that were
# attempted but are neither running nor done (sent back to ready after a failed attempt, blocked
# after a give-up, in review). Hermes sets a task's started_at at its first claim and never
# clears it, so kanban_list already shows which tasks have attempts worth reading.
_KANBAN_SHOW_MAX = 6
_KANBAN_LIST_LIMIT = 200  # kanban_list's own maximum
# A running cron run with no progress for this long has gone quiet. Same bound Hermes uses for a
# kanban worker's heartbeat (DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS).
_QUIET_S = 3600
# Delegated subagent runs are recorded under "subagent:<role>"; lane "subagent" pools them.
SUBAGENT_LANE = "subagent"
_SUBAGENT_STATUSES = {"completed": True, "failed": False, "error": False, "timeout": False}
# The hook never holds the parent's delegation up longer than this waiting for the SQLite file.
HOOK_WAIT_S = 0.25
# Marks the one-time import of past delegated runs from Hermes's session history (history.py).
HISTORY_KEY = "history_backfill"
_TRANSIENT = {"session store busy or unreadable", "time budget reached"}


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


def _tz_name(tz: tzinfo, at: datetime) -> str:
    return getattr(tz, "key", None) or at.astimezone(tz).tzname() or "UTC"


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
                 dispatch: Optional[Callable[..., str]] = None,
                 host_tz: Callable[[], Optional[tzinfo]] = lambda: None,
                 dispatch_cap: Callable[[], Optional[int]] = lambda: None,
                 cron_reader: Optional[Callable[[str], Optional[list]]] = None,
                 tool_visible: Callable[[str], bool] = lambda name: True,
                 history_reader: Optional[Callable[[Optional[float]], dict]] = None,
                 history_enabled: Callable[[], bool] = lambda: True):
        self.store = store
        self.history_reader = history_reader
        self.history_enabled = history_enabled
        self._history_done = False
        self._history_lock = threading.Lock()
        self.default_tz = default_tz
        self.clock = clock
        self.dispatch = dispatch
        self.host_tz = host_tz
        self.dispatch_cap = dispatch_cap
        self.cron_reader = cron_reader
        self.tool_visible = tool_visible

    def ensure_history(self) -> Optional[str]:
        """First use only: import past delegated runs from Hermes's session history, read-only,
        so a long-used Hermes has estimates from day one. Runs that ended after the first run the
        hook recorded are left out (the hook has them). Done once per profile (marked in the
        plugin's own store); a busy or locked session store is retried in the next process.
        Returns a one-line note for the answer when this call did the import. Never raises."""
        if self._history_done or self.history_reader is None:
            return None
        if not self._history_lock.acquire(blocking=False):
            return None  # another thread is importing right now
        try:
            if self._history_done:
                return None
            try:
                if not self.history_enabled():
                    return None
            except Exception:
                pass
            if self.store.get_meta(HISTORY_KEY) is not None:
                self._history_done = True
                return None
            self._history_done = True  # one attempt per process, whatever happens next
            before = self.store.first_end("subagent_stop") or self.clock()
            result = self.history_reader(before) or {}
            runs = result.get("runs") or []
            reason = result.get("reason")
            if reason in _TRANSIENT and not runs:
                return None  # not marked done: the next process tries again
            n = self.store.import_history(runs, HISTORY_KEY, json.dumps({
                "at": self.clock(), "imported": len(runs), "partial": bool(result.get("partial")),
                "reason": reason}))
            if not n:
                return None
            note = (f"Read {n} past delegated run{'' if n == 1 else 's'} from this Hermes's session "
                    "history (read-only, once).")
            if result.get("partial"):
                note += " Only the newest were read" + (f" ({reason})." if reason else ".")
            return note
        except Exception:
            return None
        finally:
            self._history_lock.release()

    def _visible(self, name: str) -> bool:
        """Whether Hermes would offer tool `name` here (its own availability check). The kanban
        tools are hidden from delegated children and from profiles without the kanban toolset;
        the estimate does not reach them where Hermes hides them."""
        try:
            return bool(self.tool_visible(name))
        except Exception:
            return False

    def _hermes_zone(self) -> Optional[tzinfo]:
        try:
            tz = self.host_tz()
        except Exception:
            return None
        return tz if isinstance(tz, tzinfo) else None

    def _zone(self, args: dict, now: datetime) -> tuple[tzinfo, str, str, Optional[str]]:
        """Time zone for the answer: the call's, else the plugin setting, else Hermes's own
        (hermes_time.get_timezone), else this machine's local zone. A named zone that cannot be
        loaded (a typo, or no tz database, as on Termux without tzdata) is never an error: the
        answer falls back to Hermes's zone, then UTC, and says so."""
        for name, source in ((args.get("timezone"), "argument"), (self.default_tz(), "plugin setting")):
            if not name:
                continue
            tz = _tz(str(name))
            if tz is not None:
                return tz, str(name), source, None
            fallback = self._hermes_zone()
            fb_source = "hermes" if fallback is not None else "utc fallback"
            fallback = fallback or timezone.utc
            fb_name = _tz_name(fallback, now)
            note = (f"Could not load time zone {name} (unknown name, or no time zone data on this "
                    f"device; on Termux run pkg install tzdata), so times are in {fb_name}.")
            return fallback, fb_name, fb_source, note
        tz = self._hermes_zone()
        if tz is not None:
            return tz, _tz_name(tz, now), "hermes", None
        local = now.astimezone().tzinfo or timezone.utc
        return local, _tz_name(local, now), "system", None

    def _kanban(self, assignee: str, board: Optional[str], now_s: float, **kwargs) -> dict:
        """Open work, running tasks and recent run history for one assignee, through Hermes's own
        kanban tools: one kanban_list call for the board, kanban_show for a few tasks' runs.
        Note: Hermes's kanban_list first promotes tasks whose dependencies have cleared to
        ready (the same recompute the CLI's list does); nothing else is written."""
        if self.dispatch is None or not self._visible("kanban_list"):
            raise BadInput("kanban tools are not available in this session")
        list_args = {"assignee": assignee, "limit": _KANBAN_LIST_LIMIT}
        if board:
            list_args["board"] = board
        data = _as_dict(self.dispatch("kanban_list", list_args, **kwargs))
        if "error" in data or not isinstance(data.get("tasks"), list):
            raise BadInput(f"kanban_list failed: {data.get('error', 'no tasks field')}"
                           " (is the kanban toolset enabled for this profile?)")
        tasks = [t for t in data["tasks"] if isinstance(t, dict)]
        by_status: dict[str, list] = {}
        for t in tasks:
            by_status.setdefault(t.get("status"), []).append(t)
        open_tasks = [t for t in tasks if t.get("status") in _OPEN]
        running = by_status.get("running", [])
        done = sorted(by_status.get("done", []), key=lambda t: _ts(t.get("completed_at")) or 0,
                      reverse=True)
        attempted = sorted((t for t in tasks if t.get("status") not in ("running", "done")
                            and _ts(t.get("started_at"))),
                           key=lambda t: _ts(t.get("started_at")), reverse=True)

        runs: list[Run] = []
        elapsed: list[float] = []

        def show(task) -> list:
            try:
                args = {"task_id": task["id"], **({"board": board} if board else {})}
                shown = _as_dict(self.dispatch("kanban_show", args, **kwargs))
            except Exception:
                return []
            task_runs = shown.get("runs") if isinstance(shown, dict) else None
            return [r for r in task_runs if isinstance(r, dict)] if isinstance(task_runs, list) else []

        who = assignee.strip().lower()

        def closed(task_runs) -> None:
            # Only this assignee's own attempts: a task's run list also holds the runs of other
            # profiles (a reviewer's, or an earlier assignee's).
            ordered = sorted((r for r in task_runs if _ts(r.get("started_at"))),
                             key=lambda r: _ts(r.get("started_at")))
            for i, r in enumerate(ordered):
                profile = r.get("profile")
                if isinstance(profile, str) and profile.strip() and profile.strip().lower() != who:
                    continue
                start, end = _ts(r.get("started_at")), _ts(r.get("ended_at"))
                outcome = r.get("outcome")
                if not start or not end or end < start or outcome not in _FAILED | _SUCCEEDED:
                    continue
                if outcome == "reclaimed" and _operator_reclaim(r):
                    continue
                ok = outcome in _SUCCEEDED
                if outcome == "review_requested":
                    verdict = next((later.get("outcome") for later in ordered[i + 1:]
                                    if later.get("outcome") in _REVIEW_VERDICTS), None)
                    ok = verdict != "changes_requested"
                runs.append(Run(started_at=start, duration_s=end - start, ok=ok))

        budget = _KANBAN_SHOW_MAX
        for task in running:
            task_runs = show(task) if budget > 0 else []
            budget -= 1
            closed(task_runs)
            active = [_ts(r.get("started_at")) for r in task_runs if r.get("ended_at") in (None, "")]
            start = (max(a for a in active if a) if any(active) else None) or _ts(task.get("started_at"))
            if start and start <= now_s:
                elapsed.append(now_s - start)
        # Finished and attempted-but-open tasks take turns, so neither the successes nor the
        # failed attempts crowd the other out of the budget.
        picks: list[tuple[dict, bool]] = []
        for i in range(max(len(done), len(attempted))):
            picks += [(t[i], is_done) for t, is_done in ((attempted, False), (done, True))
                      if i < len(t)]
        for task, is_done in picks[:max(budget, 0)]:
            task_runs = show(task)
            if task_runs:
                closed(task_runs)
            elif is_done:  # fall back to the task's own timestamps: one successful run
                start, end = _ts(task.get("started_at")), _ts(task.get("completed_at"))
                if start and end and end >= start:
                    runs.append(Run(started_at=start, duration_s=end - start, ok=True))
        return {"open": len(open_tasks), "blocked": len(by_status.get("blocked", [])),
                "running": len(running), "ready": len(by_status.get("ready", [])),
                "elapsed": elapsed, "runs": runs, "truncated": bool(data.get("truncated"))}

    def _cron(self, job_id: str, now_s: float) -> Optional[dict]:
        """A cron job's past runs and any run in progress, from Hermes's cron run ledger.
        None when there is no ledger or no reader."""
        if self.cron_reader is None:
            return None
        rows = self.cron_reader(job_id)
        if rows is None:
            return None
        runs, elapsed, quiet = [], [], 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = row.get("status")
            start = _ts(row.get("started_at")) or _ts(row.get("claimed_at"))
            if not start:
                continue
            if status in ("claimed", "running"):
                if start <= now_s:
                    elapsed.append(now_s - start)
                    last = _ts(row.get("progress_at")) or start
                    if now_s - last > _QUIET_S:
                        quiet += 1
                continue
            end = _ts(row.get("finished_at"))
            if status in ("completed", "failed", "unknown") and end and end >= start:
                runs.append(Run(started_at=start, duration_s=end - start, ok=status == "completed"))
        return {"runs": runs, "elapsed": elapsed, "quiet": quiet}

    def eta_estimate(self, args: dict, **kwargs) -> str:
        try:
            remaining = _num(args, "remaining_items", int, lo=0)
            avg = _num(args, "avg_run_minutes", float, lo=0, strict_lo=True)
            success = _num(args, "success_rate", float, lo=0, hi=1)
            queue_ahead = _num(args, "queue_ahead", int, lo=0) or 0
            lanes = _num(args, "lanes", int, lo=1)
            cap = _num(args, "daily_run_cap", int, lo=0)
            left = _num(args, "runs_left_today", int, lo=0)
            reset_hour = _num(args, "reset_hour", int, lo=0, hi=23) or 0
            review = _num(args, "review_minutes", float, lo=0) or 0.0
            running_min = _num(args, "running_minutes", float, lo=0)
            waiting = (args.get("waiting_on") or "").strip() or None

            history_note = self.ensure_history()
            now_s = self.clock()
            now_dt = datetime.fromtimestamp(now_s, timezone.utc)
            tz, tz_name, tz_source, tz_note = self._zone(args, now_dt)
            lane = (args.get("lane") or "").strip()
            assignee = (args.get("kanban_assignee") or "").strip()
            cron_job = (args.get("cron_job") or "").strip()
            notes, sources = ([history_note] if history_note else []), []
            runs: list[Run] = []
            elapsed: list[float] = []
            quiet = 0
            cron_idle = None
            if lane:
                runs = (self.store.runs_like(SUBAGENT_LANE + ":") if lane.lower() == SUBAGENT_LANE
                        else self.store.runs(lane))
                sources.append("recorded runs")
            if assignee:
                kb = self._kanban(assignee, (args.get("board") or "").strip() or None, now_s, **kwargs)
                sources.append("kanban")
                runs = runs + kb["runs"]
                elapsed += kb["elapsed"]
                from_board = remaining is None
                if from_board:
                    remaining = kb["open"]
                # Only blocked work is left: it is the remaining work, and its clock starts when
                # someone unblocks it. The range after that is for those tasks once they are ready.
                only_blocked = bool(kb["blocked"]) and from_board and remaining == 0
                ready = kb["ready"] + (kb["blocked"] if only_blocked else 0)
                if lanes is None and (kb["running"] or ready):
                    # Ready tasks are dispatched in parallel, up to Hermes's concurrency caps.
                    want = kb["running"] + ready
                    limit = self.dispatch_cap()
                    lanes = max(1, min(want, max(limit, kb["running"])) if limit else want)
                    if not kb["running"] and not only_blocked:
                        notes.append(f"Nothing is running for {assignee} right now; this assumes "
                                     "the kanban dispatcher picks up the ready tasks.")
                if only_blocked:
                    remaining = kb["blocked"]
                    waiting = waiting or f"someone unblocks {kb['blocked']} blocked task(s)"
                elif kb["blocked"]:
                    notes.append(f"{kb['blocked']} task(s) are blocked and not counted.")
                if kb["truncated"]:
                    notes.append(f"Board has more than {_KANBAN_LIST_LIMIT} tasks for this "
                                 "assignee; counts are partial.")
            if cron_job:
                cr = self._cron(cron_job, now_s)
                sources.append("cron")
                if cr is None:
                    notes.append("No cron run history is available on this Hermes.")
                else:
                    runs = runs + cr["runs"]
                    elapsed += cr["elapsed"]
                    quiet += cr["quiet"]
                    if cr["quiet"]:
                        notes.append(f"{cr['quiet']} run(s) of cron job {cron_job} have shown no "
                                     f"progress for over {_QUIET_S // 60} min.")
                if remaining is None:
                    remaining = max(len(cr["elapsed"]) if cr else 0, 1)
                    if cr is not None and not cr["elapsed"]:  # no ledger: running is unknown
                        cron_idle = (len(notes), cron_job)  # worded once the answer is known
            if running_min is not None:
                elapsed.append(running_min * 60)
                if remaining is None:
                    remaining = 1
            if remaining is None:
                raise BadInput("remaining_items is required unless kanban_assignee, cron_job or "
                               "running_minutes is given")
            stats = lane_stats(runs, now_s) if runs else None

            rough_basis = None
            have_elapsed = bool(elapsed) and max(elapsed) >= MIN_ELAPSED_FOR_GUESS_S
            if avg is None and not have_elapsed and (stats is None or stats.median_duration_s is None):
                pool = [r for r in self.store.all_runs(exclude_lane=lane or None) if r.duration_s > 0]
                if pool:  # other work on this Hermes: a labelled rough guess beats no answer
                    stats = lane_stats(pool, now_s)
                    stats.fail_streak = 0  # another lane's failures say nothing about this one
                    rough_basis = (f"{stats.samples} run{'' if stats.samples == 1 else 's'} of "
                                   "other work recorded on this Hermes")
                    sources.append("other lanes")
            est = estimate(
                remaining_items=remaining, stats=stats,
                avg_run_s=avg * 60 if avg else None, success_rate=success,
                queue_ahead=queue_ahead, lanes=lanes or 1, daily_run_cap=cap, runs_left_today=left,
                reset_hour=reset_hour, review_s=review * 60, waiting_on_person=waiting,
                running_elapsed_s=elapsed, quiet_running=quiet, rough_basis=rough_basis,
                now=now_dt, tz=tz)
            answer = est.headline
            if cron_idle:
                at, job = cron_idle
                # Only a range is "how long one run takes"; without one, say the plain fact.
                notes.insert(at, f"Cron job {job} is not running now: this is how long one run "
                                 "takes once it starts, not when its schedule fires."
                             if est.likely_at else f"Cron job {job} is not running now.")
            if tz_note:
                notes.append(tz_note)
                answer += f" ({tz_note})"
            return json.dumps({
                "status": est.status,
                "answer": answer,
                "likely_at": est.likely_at.isoformat() if est.likely_at else None,
                "worst_at": est.worst_at.isoformat() if est.worst_at else None,
                "key_numbers": est.key_numbers,
                "biggest_risk": est.biggest_risk,
                "clock_starts_when": est.clock_starts_when,
                "confidence": est.confidence,
                "notes": notes,
                "lane": lane or assignee or (f"cron:{cron_job}" if cron_job else None),
                "history_used": stats is not None,
                "based_on_runs": stats.samples if stats else 0,
                "sources": sources,
                "timezone": tz_name,
                "timezone_source": tz_source,
                "as_of": now_dt.astimezone(tz).isoformat(),
            })
        except BadInput as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:  # handlers must never raise
            return json.dumps({"error": f"eta_estimate failed: {exc}"})

    def on_subagent_stop(self, child_role=None, child_status=None, duration_ms=None, **kwargs):
        """subagent_stop observer: record the delegated run's role, outcome and duration.
        Interrupted children (stopped by the user or parent) and zero-length entries (a route
        refused before anything ran) are skipped. Fails open: never raises, returns None."""
        try:
            ok = _SUBAGENT_STATUSES.get(child_status if isinstance(child_status, str) else "")
            seconds = float(duration_ms or 0) / 1000.0
            if ok is None or not math.isfinite(seconds) or seconds <= 0:
                return None
            role = child_role.strip()[:40] if isinstance(child_role, str) and child_role.strip() else "leaf"
            # Runs on the parent's thread: wait at most a fraction of a second for the file.
            self.store.record(f"{SUBAGENT_LANE}:{role}", seconds, ok,
                              started_at=self.clock() - seconds, source="subagent_stop",
                              wait_s=HOOK_WAIT_S)
        except Exception:
            pass
        return None

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
            note = self.ensure_history()
            lane = (args.get("lane") or "").strip()
            lanes = [lane] if lane else self.store.lanes()
            now_s = self.clock()
            out = {"lanes": {name: _stats_dict(lane_stats(self.store.runs(name), now_s)) for name in lanes}}
            imported = self.store.get_meta(HISTORY_KEY)
            if imported:
                try:
                    out["history_import"] = json.loads(imported)
                except ValueError:
                    pass
            if note:
                out["notes"] = [note]
            return json.dumps(out)
        except Exception as exc:
            return json.dumps({"error": f"eta_history failed: {exc}"})

    def slash_eta(self, raw_args: str = "", **kwargs) -> str:
        """/eta                         history for every recorded lane
        /eta <lane> <remaining> [ahead] estimate from recorded runs
        /eta @<assignee> [board]        estimate a kanban assignee's open work
        /eta cron <job id> [remaining]  estimate a cron job's run from its run ledger"""
        usage = ("Usage: /eta <lane> <remaining items> [queued ahead], /eta @<kanban assignee> [board], "
                 "or /eta cron <job id> [remaining runs]")
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
            lines = list(data.get("notes") or [])
            for name, st in data["lanes"].items():
                ok = "n/a" if st["success_rate"] is None else f"{round(st['success_rate'] * 100)}%"
                typical = (f"typical {st['median_run_minutes']} min"
                           if st["median_run_minutes"] is not None else "no durations yet")
                line = f"{name}: {st['runs_24h']} run{'' if st['runs_24h'] == 1 else 's'} in 24h, {ok} ok, {typical}"
                if st["runs_total"] < 5:
                    line += f" (only {st['runs_total']} run{'' if st['runs_total'] == 1 else 's'} so far)"
                lines.append(line)
            return "\n".join(lines)
        if parts[0].lower() == "cron" and len(parts) >= 2:
            args = {"cron_job": parts[1]}
            if len(parts) > 2 and parts[2].isdigit():
                args["remaining_items"] = int(parts[2])
        elif parts[0].startswith("@") and len(parts[0]) > 1:
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
        if data.get("biggest_risk") and data["status"] in ("ok", "blocked_on_person"):
            text += f" Biggest risk: {data['biggest_risk']}"
        for note in data.get("notes") or []:
            if note not in text:
                text += f" {note}"
        return text

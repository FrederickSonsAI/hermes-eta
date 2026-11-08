import json
from datetime import datetime, timedelta, timezone
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _manifest_list(key):
    text = (ROOT / "plugin.yaml").read_text()
    block = re.search(rf"^{key}:\n((?:  - .+\n)+)", text, re.M).group(1)
    return sorted(line.strip()[2:] for line in block.splitlines())


def _register(pkg, fake_ctx, monkeypatch):
    from eta_plugin import store as store_mod
    monkeypatch.setattr(store_mod, "_default_connect",
                        lambda: sqlite3.connect(":memory:", check_same_thread=False))
    pkg.register(fake_ctx)
    return fake_ctx


def test_register_matches_manifest(pkg, fake_ctx, monkeypatch):
    ctx = _register(pkg, fake_ctx, monkeypatch)
    assert sorted(ctx.tools) == _manifest_list("provides_tools")
    assert sorted(ctx.hooks) == _manifest_list("provides_hooks") == ["subagent_stop"]
    assert "eta" in ctx.commands and "eta" in ctx.skills


class FakeKanban:
    """Stands in for Hermes's kanban_list / kanban_show tools."""

    def __init__(self, now):
        self.calls = []
        day = 86400
        self.tasks = [
            {"id": "d1", "status": "done", "started_at": now - day, "completed_at": now - day + 600},
            {"id": "d2", "status": "done", "started_at": now - 7200, "completed_at": now - 6600},
            {"id": "o1", "status": "ready"}, {"id": "o2", "status": "todo"},
            {"id": "o3", "status": "running", "started_at": now - 3 * 3600},
            {"id": "b1", "status": "blocked"},
        ]
        self.runs = {
            "d1": [{"started_at": now - day, "ended_at": now - day + 600, "outcome": "completed"}],
            "d2": [{"started_at": now - 9000, "ended_at": now - 8400, "outcome": "crashed"},
                   {"started_at": 0, "ended_at": now - 8000, "outcome": "crashed"},
                   {"started_at": now - 7200, "ended_at": now - 6600, "outcome": "completed"},
                   {"started_at": now - 6000, "ended_at": None, "outcome": None}],
            # o3: an earlier attempt was reclaimed; the active attempt started 4 min ago
            "o3": [{"started_at": now - 3 * 3600, "ended_at": now - 3 * 3600 + 300, "outcome": "reclaimed",
                    "error": "claim expired"},
                   {"started_at": now - 240, "ended_at": None, "outcome": None}],
        }

    def __call__(self, name, args, **kw):
        self.calls.append((name, args))
        if name == "kanban_list":  # one call, no status filter: the whole board for the assignee
            assert args["assignee"] == "writer" and "status" not in args and args["limit"] == 200
            return json.dumps({"tasks": list(self.tasks), "count": len(self.tasks),
                               "truncated": False, "promoted": 0})
        if name == "kanban_show":
            return {"runs": self.runs.get(args["task_id"], [])}  # the registry may return a dict
        return json.dumps({"error": "Unknown tool"})


NOW = 1_793_900_000.0


def _handlers(store, dispatch=None, now=NOW, tz=None, **kw):
    from eta_plugin.tools import Handlers
    return Handlers(store, default_tz=lambda: tz, clock=lambda: now, dispatch=dispatch, **kw)


def test_kanban_live_read(store):
    fk = FakeKanban(NOW)
    out = json.loads(_handlers(store, fk, tz="America/New_York").eta_estimate(
        {"kanban_assignee": "writer", "lanes": 2}))
    assert out["status"] == "ok", out
    assert out["key_numbers"]["remaining_items"] == 3  # ready, todo, running; blocked excluded
    # d1 ok, d2 crash + ok, o3 reclaimed: crash and reclaim count, open runs ignored
    assert out["key_numbers"]["success_rate"] == 0.5
    assert out["key_numbers"]["running_elapsed_minutes"] == [4.0]  # active attempt, not first claim
    assert any("blocked" in n for n in out["notes"])
    names = [c[0] for c in fk.calls]
    assert names.count("kanban_list") == 1 and names.count("kanban_show") == 3


def test_kanban_unavailable_is_clean_error(store):
    h = _handlers(store, lambda *a, **k: json.dumps({"error": "Unknown tool: kanban_list"}))
    assert "kanban toolset" in json.loads(h.eta_estimate({"kanban_assignee": "writer"}))["error"]
    assert "not available" in json.loads(_handlers(store).eta_estimate({"kanban_assignee": "x"}))["error"]


def test_only_blocked_work_waits_on_person(store):
    fk = FakeKanban(NOW)
    fk.tasks = [t for t in fk.tasks if t["status"] in {"done", "blocked"}]
    h = _handlers(store, fk)
    out = json.loads(h.eta_estimate({"kanban_assignee": "writer"}))
    assert out["status"] == "blocked_on_person", out  # not "Nothing left to run."
    assert out["clock_starts_when"] == "someone unblocks 1 blocked task(s)"
    assert out["answer"].startswith("Clock starts when someone unblocks 1 blocked task(s). After that:")
    assert out["key_numbers"]["remaining_items"] == 1  # the blocked task is the work left
    assert not any("not counted" in n for n in out["notes"])
    assert h.slash_eta("@writer").startswith("Clock starts when someone unblocks 1 blocked task(s).")
    # an explicit "nothing left" from the caller still stands
    assert json.loads(h.eta_estimate({"kanban_assignee": "writer", "remaining_items": 0}))["status"] == "done"


def test_only_blocked_work_after_a_give_up_still_names_the_clock(store):
    fk = FakeKanban(NOW)
    fk.tasks = [{"id": "g1", "status": "blocked", "started_at": NOW - 4000}]
    fk.runs = {"g1": [
        {"started_at": NOW - 4000, "ended_at": NOW - 3900, "outcome": "crashed"},
        {"started_at": NOW - 3800, "ended_at": NOW - 3700, "outcome": "crashed"},
        {"started_at": NOW - 3600, "ended_at": NOW - 3500, "outcome": "timed_out"}]}
    out = json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer"}))
    assert out["status"] == "unbounded", out
    assert out["answer"].startswith("Clock starts when someone unblocks 1 blocked task(s). No honest ETA:")
    assert out["clock_starts_when"] == "someone unblocks 1 blocked task(s)"


def test_only_blocked_work_runs_in_parallel_once_unblocked(store):
    """Unblocked tasks are ready, so the range after the clock starts uses the same lanes as the
    same tasks waiting in ready would: not one at a time."""
    done = [{"id": f"d{i}", "status": "done", "started_at": NOW - 9000 + i * 1000,
             "completed_at": NOW - 9000 + i * 1000 + 600} for i in range(6)]

    def board(status):
        fk = FakeKanban(NOW)
        fk.tasks = done + [{"id": f"x{i}", "status": status} for i in range(4)]
        fk.runs = {t["id"]: [{"started_at": t["started_at"], "ended_at": t["completed_at"],
                              "outcome": "completed"}] for t in done}
        return fk

    blocked = json.loads(_handlers(store, board("blocked"), dispatch_cap=lambda: 3).eta_estimate(
        {"kanban_assignee": "writer"}))
    ready = json.loads(_handlers(store, board("ready"), dispatch_cap=lambda: 3).eta_estimate(
        {"kanban_assignee": "writer"}))
    assert blocked["status"] == "blocked_on_person", blocked
    assert blocked["key_numbers"]["lanes"] == ready["key_numbers"]["lanes"] == 3
    assert blocked["answer"].startswith(
        "Clock starts when someone unblocks 4 blocked task(s). After that: 20 min to"), blocked
    assert not any("Nothing is running" in n for n in blocked["notes"])


def test_db_failure_never_raises(pkg, fake_ctx, monkeypatch):
    from eta_plugin import store as store_mod

    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(store_mod, "_default_connect", boom)
    pkg.register(fake_ctx)
    assert "error" in json.loads(fake_ctx.tools["eta_history"]({}))
    assert "error" in json.loads(fake_ctx.tools["eta_estimate"]({"lane": "x", "remaining_items": 1}))


def test_estimate_tool_end_to_end(pkg, fake_ctx, monkeypatch):
    ctx = _register(pkg, fake_ctx, monkeypatch)
    for ok in [True, True, True, False, True]:
        assert json.loads(ctx.tools["eta_record_run"]({"lane": "ci", "duration_minutes": 8, "ok": ok}))["recorded"]
    ctx.config["timezone"] = "America/New_York"
    out = json.loads(ctx.tools["eta_estimate"]({"lane": "ci", "remaining_items": 4, "queue_ahead": 2}))
    assert out["status"] == "ok" and out["history_used"] is True
    assert out["key_numbers"]["success_rate"] == 0.8
    assert out["likely_at"].endswith(("-05:00", "-04:00"))
    assert out["answer"].startswith("Likely")


def test_estimate_tool_bad_input(pkg, fake_ctx, monkeypatch):
    ctx = _register(pkg, fake_ctx, monkeypatch)
    assert "error" in json.loads(ctx.tools["eta_estimate"]({}))
    assert "error" in json.loads(ctx.tools["eta_record_run"]({"lane": "x"}))
    assert "error" in json.loads(ctx.tools["eta_record_run"]({"lane": "x", "duration_minutes": -3, "ok": True}))
    assert "error" in json.loads(ctx.tools["eta_estimate"]({"remaining_items": "lots"}))
    bad = [{"avg_run_minutes": -5}, {"avg_run_minutes": 0}, {"success_rate": 1.5},
           {"review_minutes": -1}, {"lanes": 0}, {"reset_hour": 24}, {"remaining_items": -1},
           {"avg_run_minutes": "nan"}, {"daily_run_cap": -1}]
    for extra in bad:
        args = {"remaining_items": 2, "avg_run_minutes": 5, **extra}
        assert "error" in json.loads(ctx.tools["eta_estimate"](args)), extra


def test_slash_command(pkg, fake_ctx, monkeypatch):
    ctx = _register(pkg, fake_ctx, monkeypatch)
    assert "No runs recorded" in ctx.commands["eta"]("")
    ctx.tools["eta_record_run"]({"lane": "docs", "duration_minutes": 5, "ok": True})
    ctx.tools["eta_record_run"]({"lane": "nodur", "duration_minutes": 0, "ok": True})
    listing = ctx.commands["eta"]("")
    assert "docs:" in listing and "no durations yet" in listing and "None" not in listing
    assert ctx.commands["eta"]("docs 3 1").startswith("Likely")
    assert ctx.commands["eta"]("docs").startswith("Usage")
    assert "kanban" in ctx.commands["eta"]("@writer")  # fake ctx has no kanban tools


def test_review_requested_counts_as_success(store):
    fk = FakeKanban(NOW)
    fk.runs["d1"][0]["outcome"] = "review_requested"
    out = json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer"}))
    assert out["key_numbers"]["success_rate"] == 0.5  # unchanged: review_requested is a success


# --- kanban: one list call, failed attempts, parallel lanes, running work ----------------------

def test_kanban_call_budget_with_a_big_board(store):
    fk = FakeKanban(NOW)
    for i in range(30):
        fk.tasks.append({"id": f"x{i}", "status": "done", "started_at": NOW - 9000 - i,
                         "completed_at": NOW - 8400 - i})
    out = json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer"}))
    assert out["status"] == "ok", out
    names = [c[0] for c in fk.calls]
    assert names.count("kanban_list") == 1 and names.count("kanban_show") <= 6


def test_reclaimed_stale_and_rejected_handoffs_are_failed_attempts(store):
    fk = FakeKanban(NOW)
    fk.runs["d1"] = [
        {"started_at": NOW - 5000, "ended_at": NOW - 4900, "outcome": "reclaimed",
         "error": "worker pid gone", "profile": "writer"},
        {"started_at": NOW - 4800, "ended_at": NOW - 4200, "outcome": "review_requested",
         "profile": "writer"},
        # the reviewer's own run: another profile, not this assignee's history
        {"started_at": NOW - 4100, "ended_at": NOW - 4000, "outcome": "changes_requested",
         "profile": "reviewer"},
        {"started_at": NOW - 3900, "ended_at": NOW - 3300, "outcome": "completed", "profile": "Writer"}]
    fk.runs["d2"] = [{"started_at": NOW - 3000, "ended_at": NOW - 2400, "outcome": "stale"},
                     {"started_at": NOW - 2300, "ended_at": NOW - 1700, "outcome": "completed"}]
    out = json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer"}))
    # failed: reclaim with an error, the hand-off review sent back, stale, o3's reclaim = 4 of 6
    assert out["key_numbers"]["success_rate"] == round(2 / 6, 3)
    assert out["based_on_runs"] == 6


def test_failed_attempts_on_requeued_and_given_up_tasks_count(store):
    """A lane that keeps crashing: its failed attempts sit on tasks that went back to ready or
    were blocked after a give-up, not on running or done tasks."""
    fk = FakeKanban(NOW)
    fk.tasks = [
        {"id": "d1", "status": "done", "started_at": NOW - 30000, "completed_at": NOW - 29400},
        {"id": "d2", "status": "done", "started_at": NOW - 29000, "completed_at": NOW - 28400},
        {"id": "r1", "status": "ready", "started_at": NOW - 9000},
        {"id": "r2", "status": "ready", "started_at": NOW - 8000},
        {"id": "g1", "status": "blocked", "started_at": NOW - 4000},
        {"id": "n1", "status": "ready"},  # never attempted: nothing to show
    ]
    fk.runs = {
        "d1": [{"started_at": NOW - 30000, "ended_at": NOW - 29400, "outcome": "completed"}],
        "d2": [{"started_at": NOW - 29000, "ended_at": NOW - 28400, "outcome": "completed"}],
        "r1": [{"started_at": NOW - 9000, "ended_at": NOW - 8900, "outcome": "crashed"},
               {"started_at": NOW - 8800, "ended_at": NOW - 8700, "outcome": "reclaimed",
                "error": "manual_reclaim: operator"},  # neutral
               {"started_at": NOW - 8600, "ended_at": NOW - 8500, "outcome": "crashed",
                "profile": "someone-else"},  # not this assignee's attempt
               {"started_at": NOW - 6000, "ended_at": NOW - 5000, "outcome": "timed_out"}],
        "r2": [{"started_at": NOW - 8000, "ended_at": NOW - 7400, "outcome": "review_requested",
                "profile": "writer"},
               {"started_at": NOW - 7300, "ended_at": NOW - 7200, "outcome": "changes_requested",
                "profile": "reviewer"}],  # sent back: the hand-off failed
        "g1": [{"started_at": NOW - 4000, "ended_at": NOW - 3900, "outcome": "stale"},
               {"started_at": NOW - 3000, "ended_at": NOW - 2990, "outcome": "gave_up"}],
    }
    out = json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer"}))
    assert out["status"] == "unbounded", out  # not a confident range
    assert out["based_on_runs"] == 7 and out["key_numbers"]["success_rate"] == round(2 / 7, 3)
    names = [c[0] for c in fk.calls]
    assert names.count("kanban_list") == 1 and names.count("kanban_show") == 5
    assert "n1" not in {a.get("task_id") for _, a in fk.calls}


def test_show_budget_keeps_both_requeued_and_finished_tasks(store):
    fk = FakeKanban(NOW)
    fk.tasks = [{"id": f"r{i}", "status": "ready", "started_at": NOW - 9000 + i} for i in range(10)]
    fk.tasks += [{"id": f"d{i}", "status": "done", "started_at": NOW - 20000 + i,
                  "completed_at": NOW - 19000 + i} for i in range(10)]
    fk.runs = {}
    json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer"}))
    shown = [a["task_id"] for n, a in fk.calls if n == "kanban_show"]
    assert len(shown) == 6 and sum(t.startswith("r") for t in shown) == 3
    assert shown[:2] == ["r9", "d9"]  # newest of each, in turn


def test_operator_reclaims_are_not_failures(store):
    fk = FakeKanban(NOW)
    fk.runs["d1"] = [
        {"started_at": NOW - 5000, "ended_at": NOW - 4900, "outcome": "reclaimed"},  # archived
        {"started_at": NOW - 4800, "ended_at": NOW - 4700, "outcome": "reclaimed",
         "error": "manual_reclaim: wrong branch"},
        {"started_at": NOW - 4600, "ended_at": NOW - 4000, "outcome": "completed"}]
    fk.runs["d2"] = [{"started_at": NOW - 3000, "ended_at": NOW - 2400, "outcome": "completed"}]
    fk.runs["o3"] = [{"started_at": NOW - 240, "ended_at": None, "outcome": None}]
    out = json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer"}))
    assert out["status"] == "ok" and out["key_numbers"]["success_rate"] == 1.0
    assert out["based_on_runs"] == 2


def test_reviewer_lane_counts_changes_requested_as_a_finished_review(store):
    fk = FakeKanban(NOW)
    fk.tasks = [{"id": "v1", "status": "done"}, {"id": "v2", "status": "review"}]
    fk.runs = {"v1": [
        {"started_at": NOW - 9000, "ended_at": NOW - 8000, "outcome": "review_requested",
         "profile": "builder"},
        {"started_at": NOW - 7900, "ended_at": NOW - 7600, "outcome": "changes_requested",
         "profile": "writer"},
        {"started_at": NOW - 7000, "ended_at": NOW - 6000, "outcome": "review_requested",
         "profile": "builder"},
        {"started_at": NOW - 5900, "ended_at": NOW - 5600, "outcome": "completed", "profile": "writer"}]}
    out = json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer"}))
    assert out["key_numbers"]["success_rate"] == 1.0 and out["based_on_runs"] == 2
    assert out["key_numbers"]["remaining_items"] == 1


def test_kanban_not_read_where_hermes_hides_it(store):
    fk = FakeKanban(NOW)
    h = _handlers(store, fk, tool_visible=lambda name: False)
    assert "not available" in json.loads(h.eta_estimate({"kanban_assignee": "writer"}))["error"]
    assert fk.calls == []  # nothing dispatched: no board read, no ready promotion

    def broken(name):
        raise RuntimeError("registry unavailable")
    h = _handlers(store, fk, tool_visible=broken)
    assert "not available" in json.loads(h.eta_estimate({"kanban_assignee": "writer"}))["error"]
    assert fk.calls == []


def test_register_gates_kanban_on_hermes_tool_availability(pkg, fake_ctx, monkeypatch):
    import sys
    import types
    fk = FakeKanban(NOW)
    fake_ctx.dispatch_tool = fk
    shown = set()
    registry = types.SimpleNamespace(
        get_definitions=lambda names, quiet=False: [{"name": n} for n in names if n in shown])
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.registry", types.SimpleNamespace(registry=registry))
    ctx = _register(pkg, fake_ctx, monkeypatch)
    hidden = json.loads(ctx.tools["eta_estimate"]({"kanban_assignee": "writer"}))
    assert "not available" in hidden["error"] and fk.calls == []
    shown.add("kanban_list")
    assert json.loads(ctx.tools["eta_estimate"]({"kanban_assignee": "writer"}))["status"] == "ok"


def test_lanes_follow_running_and_ready_within_dispatch_cap(store):
    fk = FakeKanban(NOW)
    fk.tasks += [{"id": f"r{i}", "status": "ready"} for i in range(4)]  # 5 ready + 1 running
    free = json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer"}))
    assert free["key_numbers"]["lanes"] == 6
    capped = json.loads(_handlers(store, fk,
                                  dispatch_cap=lambda: 2).eta_estimate({"kanban_assignee": "writer"}))
    assert capped["key_numbers"]["lanes"] == 2
    told = json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer", "lanes": 1}))
    assert told["key_numbers"]["lanes"] == 1


def test_derived_dispatch_cap_is_named_only_when_it_limits_lanes(store):
    fk = FakeKanban(NOW)
    fk.tasks += [{"id": f"r{i}", "status": "ready"} for i in range(4)]  # 5 ready + 1 running
    note = "At most 2 kanban task(s) run at once here"
    capped = json.loads(_handlers(store, fk, dispatch_cap=lambda: (2, note)).eta_estimate(
        {"kanban_assignee": "writer"}))
    assert capped["key_numbers"]["lanes"] == 2 and note in capped["notes"]
    roomy = json.loads(_handlers(store, fk, dispatch_cap=lambda: (8, note)).eta_estimate(
        {"kanban_assignee": "writer"}))
    assert roomy["key_numbers"]["lanes"] == 6 and note not in roomy["notes"]


def test_running_task_elapsed_is_subtracted(store):
    for _ in range(6):
        store.record("ci", 1200, True, started_at=NOW - 7200)
    h = _handlers(store)
    fresh = json.loads(h.eta_estimate({"lane": "ci", "remaining_items": 1}))
    going = json.loads(h.eta_estimate({"lane": "ci", "remaining_items": 1, "running_minutes": 15}))
    t = lambda o: datetime.fromisoformat(o["likely_at"]).timestamp() - NOW
    assert round(t(fresh)) == 1200 and round(t(going)) == 300


def test_running_task_past_slow_mark_is_flagged(store):
    for _ in range(6):
        store.record("ci", 600, True, started_at=NOW - 7200)
    out = json.loads(_handlers(store).eta_estimate({"lane": "ci", "running_minutes": 90}))
    assert out["status"] == "ok" and "slow mark" in out["biggest_risk"]
    assert out["key_numbers"]["running_past_slow_mark"] == 1


# --- subagent_stop hook -------------------------------------------------------------------------

def _hook(pkg, fake_ctx, monkeypatch):
    ctx = _register(pkg, fake_ctx, monkeypatch)
    return ctx, ctx.hooks["subagent_stop"]


def test_subagent_stop_records_runs(pkg, fake_ctx, monkeypatch):
    ctx, hook = _hook(pkg, fake_ctx, monkeypatch)
    base = dict(parent_session_id="p", parent_turn_id="t", child_session_id="c",
                child_summary="secret summary", tool_call_history=[{"tool_name": "x"}])
    assert hook(child_role="leaf", child_status="completed", duration_ms=120_000, **base) is None
    hook(child_role=None, child_status="failed", duration_ms=60_000, **base)
    hook(child_role="orchestrator", child_status="timeout", duration_ms=300_000, **base)
    hook(child_role="leaf", child_status="interrupted", duration_ms=50_000, **base)  # skipped
    hook(child_role="leaf", child_status="error", duration_ms=0, **base)  # refused route: skipped
    hist = json.loads(ctx.tools["eta_history"]({}))["lanes"]
    assert hist["subagent:leaf"]["runs_total"] == 2 and hist["subagent:leaf"]["success_rate"] == 0.5
    assert hist["subagent:orchestrator"]["runs_total"] == 1
    pooled = json.loads(ctx.tools["eta_estimate"]({"lane": "subagent", "remaining_items": 1}))
    assert pooled["based_on_runs"] == 3 and pooled["history_used"]


def test_subagent_stop_gives_up_fast_when_the_store_is_busy(store, pkg):
    import time as _time
    from eta_plugin.tools import HOOK_WAIT_S
    h = _handlers(store)
    store._lock.acquire()  # another thread is mid-write
    try:
        t0 = _time.monotonic()
        assert h.on_subagent_stop(child_role="leaf", child_status="completed", duration_ms=5000) is None
        assert _time.monotonic() - t0 < HOOK_WAIT_S + 0.5
    finally:
        store._lock.release()
    assert store.runs("subagent:leaf") == []  # dropped, never blocked
    h.on_subagent_stop(child_role="leaf", child_status="completed", duration_ms=5000)
    assert len(store.runs("subagent:leaf")) == 1


def test_subagent_stop_gives_up_when_another_process_locks_the_file(tmp_path, pkg):
    from eta_plugin.store import RunStore
    path = tmp_path / "data.db"
    store = RunStore(connect=lambda: sqlite3.connect(path, timeout=5, check_same_thread=False))
    store.runs("x")  # create the schema
    other = sqlite3.connect(path, timeout=0)
    other.execute("BEGIN EXCLUSIVE")
    try:
        import time as _time
        t0 = _time.monotonic()
        assert store.record("subagent:leaf", 5, True, wait_s=0.1) is False
        assert _time.monotonic() - t0 < 2
    finally:
        other.rollback()
        other.close()
    assert store.record("subagent:leaf", 5, True, wait_s=0.1) is True


def test_subagent_stop_never_raises(pkg, fake_ctx, monkeypatch):
    from eta_plugin import store as store_mod

    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(store_mod, "_default_connect", boom)
    pkg.register(fake_ctx)
    hook = fake_ctx.hooks["subagent_stop"]
    assert hook(child_role="leaf", child_status="completed", duration_ms=1000) is None
    assert hook(child_status=object(), duration_ms="nan") is None
    assert hook() is None


# --- little or no history -----------------------------------------------------------------------

def test_cold_start_says_what_it_needs(store):
    out = json.loads(_handlers(store).eta_estimate({"lane": "new", "remaining_items": 2}))
    assert out["status"] == "no_data"
    assert "avg_run_minutes" in out["answer"] and "running_minutes" in out["answer"]


def test_cold_start_rough_guess_from_running_elapsed(store):
    out = json.loads(_handlers(store).eta_estimate({"lane": "new", "running_minutes": 20}))
    assert out["status"] == "ok" and out["confidence"] == "rough"
    assert out["answer"].startswith("Rough guess:") and "20 min" in out["answer"]
    left = lambda k: round((datetime.fromisoformat(out[k]).timestamp() - NOW) / 60)
    assert left("likely_at") == 20 and left("worst_at") == 140  # slow end plus one retry


def test_cold_start_rough_guess_from_other_lanes(store):
    for ok in (True, True, False):
        store.record("subagent:leaf", 600, ok, started_at=NOW - 3600)
    out = json.loads(_handlers(store).eta_estimate({"lane": "brand-new", "remaining_items": 1}))
    assert out["status"] == "ok" and out["confidence"] == "rough"
    assert "3 runs of other work" in out["answer"]


def test_one_and_two_runs_say_so_and_widen(store):
    store.record("one", 600, True, started_at=NOW - 3600)
    for _ in range(10):
        store.record("many", 600, True, started_at=NOW - 3600)
    h = _handlers(store)
    one = json.loads(h.eta_estimate({"lane": "one", "remaining_items": 3}))
    many = json.loads(h.eta_estimate({"lane": "many", "remaining_items": 3}))
    assert "Based on 1 run, so the worst case is wide." in one["answer"]
    assert one["confidence"] == "low" and many["confidence"] == "normal"
    assert one["worst_at"] > many["worst_at"]
    store.record("two", 600, False, started_at=NOW - 3000)
    two = json.loads(h.eta_estimate({"lane": "two", "remaining_items": 1}))
    assert two["status"] == "ok", two  # one failure out of one is not "no honest ETA"
    store.record("two", 600, True, started_at=NOW - 2000)
    assert "Based on 2 runs" in h.slash_eta("two 1")


def test_slash_listing_flags_thin_history(pkg, fake_ctx, monkeypatch):
    ctx = _register(pkg, fake_ctx, monkeypatch)
    ctx.tools["eta_record_run"]({"lane": "docs", "duration_minutes": 5, "ok": True})
    assert "(only 1 run so far)" in ctx.commands["eta"]("")


# --- time zone ----------------------------------------------------------------------------------

def test_timezone_defaults_to_hermes(store):
    from zoneinfo import ZoneInfo
    store.record("ci", 600, True, started_at=NOW - 3600)
    h = _handlers(store, host_tz=lambda: ZoneInfo("America/Chicago"))
    out = json.loads(h.eta_estimate({"lane": "ci", "remaining_items": 1}))
    assert out["timezone"] == "America/Chicago" and out["timezone_source"] == "hermes"
    assert out["likely_at"].endswith(("-06:00", "-05:00"))
    system = json.loads(_handlers(store).eta_estimate({"lane": "ci", "remaining_items": 1}))
    assert system["timezone_source"] == "system"  # Hermes has none set: server-local, like Hermes


def test_timezone_that_cannot_load_falls_back_never_errors(store):
    from zoneinfo import ZoneInfo
    store.record("ci", 600, True, started_at=NOW - 3600)
    h = _handlers(store, tz="Mars/Base", host_tz=lambda: ZoneInfo("America/Chicago"))
    out = json.loads(h.eta_estimate({"lane": "ci", "remaining_items": 1}))
    assert "error" not in out and out["timezone"] == "America/Chicago"
    assert "Could not load time zone Mars/Base" in out["answer"]
    utc = json.loads(_handlers(store).eta_estimate({"lane": "ci", "remaining_items": 1,
                                                     "timezone": "Not/AZone"}))
    assert utc["timezone"] == "UTC" and utc["timezone_source"] == "utc fallback"
    assert "pkg install tzdata" in utc["answer"]
    assert h.slash_eta("ci 1").count("Could not load") == 1


def test_zoneinfo_missing_tzdata_falls_back(store, monkeypatch):
    from eta_plugin import tools as tools_mod
    from zoneinfo import ZoneInfoNotFoundError

    def no_tzdata(name):
        raise ZoneInfoNotFoundError(name)
    monkeypatch.setattr(tools_mod, "ZoneInfo", no_tzdata)  # as on Termux without tzdata
    store.record("ci", 600, True, started_at=NOW - 3600)
    out = json.loads(_handlers(store, tz="America/New_York").eta_estimate({"lane": "ci", "remaining_items": 1}))
    assert out["status"] == "ok" and out["timezone"] == "UTC"


# --- cron ---------------------------------------------------------------------------------------

def _iso(ts):
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=-5))).isoformat()


def test_cron_history_and_running_run(store):
    rows = [  # newest first, as list_executions returns them
        {"status": "running", "claimed_at": _iso(NOW - 600), "started_at": _iso(NOW - 600),
         "progress_at": _iso(NOW - 60)},
        {"status": "completed", "started_at": _iso(NOW - 9000), "finished_at": _iso(NOW - 7800)},
        {"status": "failed", "started_at": _iso(NOW - 20000), "finished_at": _iso(NOW - 19000)},
        {"status": "completed", "started_at": _iso(NOW - 80000), "finished_at": _iso(NOW - 78800)},
        {"status": "cancelled", "started_at": _iso(NOW - 99000), "finished_at": _iso(NOW - 98000)},
        {"status": "missed", "claimed_at": _iso(NOW - 99500)},
    ]
    asked = []
    h = _handlers(store, cron_reader=lambda job: asked.append(job) or rows)
    out = json.loads(h.eta_estimate({"cron_job": "nightly"}))
    assert asked == ["nightly"] and out["status"] == "ok", out
    assert out["key_numbers"]["remaining_items"] == 1 and out["key_numbers"]["running_now"] == 1
    assert out["based_on_runs"] == 3 and out["key_numbers"]["success_rate"] == round(2 / 3, 3)
    assert out["lane"] == "cron:nightly"
    assert h.slash_eta("cron nightly").startswith("Likely")


def test_cron_no_honest_eta_drops_the_one_run_line(store):
    rows = [{"status": "failed", "started_at": _iso(NOW - d * 86400), "finished_at": _iso(NOW - d * 86400 + 120)}
            for d in (1, 2, 3)]
    h = _handlers(store, cron_reader=lambda job: rows)
    out = json.loads(h.eta_estimate({"cron_job": "nightly"}))
    assert out["status"] == "unbounded", out
    text = h.slash_eta("cron nightly")
    assert text.startswith("No honest ETA: the last 3 runs failed in a row.")
    for said in [text, *out["notes"]]:
        assert "how long one run takes" not in said, said
    assert "Cron job nightly is not running now." in out["notes"]
    good = [{"status": "completed", "started_at": _iso(NOW - d * 86400),
             "finished_at": _iso(NOW - d * 86400 + 600)} for d in (1, 2, 3)]
    ok = json.loads(_handlers(store, cron_reader=lambda job: good).eta_estimate({"cron_job": "nightly"}))
    assert ok["status"] == "ok" and any("how long one run takes" in n for n in ok["notes"])


def test_cron_quiet_run_and_missing_ledger(store):
    rows = [{"status": "running", "started_at": _iso(NOW - 7200), "progress_at": _iso(NOW - 5400)}]
    out = json.loads(_handlers(store, cron_reader=lambda job: rows).eta_estimate({"cron_job": "j"}))
    assert out["confidence"] == "rough" and "gone quiet" in out["biggest_risk"]
    assert any("no progress" in n for n in out["notes"])
    none = json.loads(_handlers(store, cron_reader=lambda job: None).eta_estimate({"cron_job": "j"}))
    assert any("No cron run history" in n for n in none["notes"]) and none["status"] == "no_data"
    assert not any("not running now" in n for n in none["notes"])  # no ledger: not known


def _history_board():
    fk = FakeKanban(NOW)
    done = [{"id": f"d{i}", "status": "done", "started_at": NOW - 9000 + i * 1000,
             "completed_at": NOW - 9000 + i * 1000 + 600} for i in range(6)]
    fk.tasks = list(done)
    fk.runs = {t["id"]: [{"started_at": t["started_at"], "ended_at": t["completed_at"],
                          "outcome": "completed"}] for t in done}
    return fk


def test_todo_children_of_a_blocked_task_wait_on_the_same_person(store):
    """kanban_list's parents field: a todo task whose parent (or grandparent) on the board is
    blocked cannot start until that task is unblocked, so it gets no range of its own."""
    fk = _history_board()
    fk.tasks += [{"id": "b0", "status": "blocked", "parents": [], "children": ["t1"]},
                 {"id": "t1", "status": "todo", "parents": ["b0"], "children": ["t2"]},
                 {"id": "t2", "status": "todo", "parents": ["t1"], "children": []}]
    h = _handlers(store, fk)
    out = json.loads(h.eta_estimate({"kanban_assignee": "writer"}))
    assert out["status"] == "blocked_on_person", out
    assert out["answer"].startswith("Clock starts when someone unblocks 1 blocked task(s). After that:")
    assert out["key_numbers"]["remaining_items"] == 3  # the blocked task and both children
    assert h.slash_eta("@writer").startswith("Clock starts when someone unblocks 1 blocked task(s).")
    names = [c[0] for c in fk.calls]
    assert names.count("kanban_list") == 2 and names.count("kanban_show") <= 12  # two calls above


def test_runnable_work_beside_blocked_children_is_still_estimated(store):
    fk = _history_board()
    fk.tasks += [{"id": "r1", "status": "ready", "parents": []},
                 {"id": "b0", "status": "blocked", "parents": []},
                 {"id": "t1", "status": "todo", "parents": ["b0"]},
                 {"id": "t2", "status": "todo", "parents": ["elsewhere"]}]  # parent not on board
    out = json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer"}))
    assert out["status"] == "ok", out
    assert out["key_numbers"]["remaining_items"] == 2  # r1 and t2
    assert "1 task(s) are blocked and not counted." in out["notes"]
    assert "1 todo task(s) wait on a blocked task and are not counted." in out["notes"]
    names = [c[0] for c in fk.calls]
    assert names.count("kanban_list") == 1 and names.count("kanban_show") <= 6


def test_blocked_chain_unblocks_one_at_a_time(store):
    """Hermes's unblock_task re-gates on parents: a blocked task whose parent on the board is
    blocked lands in todo, not ready, so it is no extra lane once the clock starts."""
    def board(chained):
        fk = _history_board()
        fk.tasks += [{"id": f"b{i}", "status": "blocked",
                      "parents": [f"b{i - 1}"] if chained and i else []} for i in range(4)]
        return fk

    chain = json.loads(_handlers(store, board(True), dispatch_cap=lambda: 3).eta_estimate(
        {"kanban_assignee": "writer"}))
    loose = json.loads(_handlers(store, board(False), dispatch_cap=lambda: 3).eta_estimate(
        {"kanban_assignee": "writer"}))
    assert chain["status"] == loose["status"] == "blocked_on_person", chain
    assert chain["answer"].startswith("Clock starts when someone unblocks 4 blocked task(s).")
    assert loose["key_numbers"]["lanes"] == 3
    assert chain["key_numbers"]["lanes"] == 1, chain

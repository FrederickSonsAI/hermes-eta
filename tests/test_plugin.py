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
            "o3": [{"started_at": now - 3 * 3600, "ended_at": now - 3 * 3600 + 300, "outcome": "reclaimed"},
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
    out = json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer"}))
    assert out["status"] == "done"
    assert any("blocked" in n for n in out["notes"])


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


def test_reclaimed_and_changes_requested_are_failed_attempts(store):
    fk = FakeKanban(NOW)
    fk.runs["d1"] = [
        {"started_at": NOW - 5000, "ended_at": NOW - 4900, "outcome": "reclaimed"},
        {"started_at": NOW - 4800, "ended_at": NOW - 4200, "outcome": "review_requested"},
        {"started_at": NOW - 4100, "ended_at": NOW - 4000, "outcome": "changes_requested"},
        {"started_at": NOW - 3900, "ended_at": NOW - 3300, "outcome": "completed"}]
    fk.runs["d2"] = [{"started_at": NOW - 3000, "ended_at": NOW - 2400, "outcome": "stale"},
                     {"started_at": NOW - 2300, "ended_at": NOW - 1700, "outcome": "completed"}]
    out = json.loads(_handlers(store, fk).eta_estimate({"kanban_assignee": "writer"}))
    # failed: reclaimed, changes_requested, stale, o3's reclaim = 4 of 7
    assert out["key_numbers"]["success_rate"] == round(3 / 7, 3)


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


def test_cron_quiet_run_and_missing_ledger(store):
    rows = [{"status": "running", "started_at": _iso(NOW - 7200), "progress_at": _iso(NOW - 5400)}]
    out = json.loads(_handlers(store, cron_reader=lambda job: rows).eta_estimate({"cron_job": "j"}))
    assert out["confidence"] == "rough" and "gone quiet" in out["biggest_risk"]
    assert any("no progress" in n for n in out["notes"])
    none = json.loads(_handlers(store, cron_reader=lambda job: None).eta_estimate({"cron_job": "j"}))
    assert any("No cron run history" in n for n in none["notes"]) and none["status"] == "no_data"

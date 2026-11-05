import json
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
    assert ctx.hooks == {}
    assert "provides_hooks" not in (ROOT / "plugin.yaml").read_text()
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
            {"id": "o3", "status": "running"}, {"id": "b1", "status": "blocked"},
        ]
        self.runs = {
            "d1": [{"started_at": now - day, "ended_at": now - day + 600, "outcome": "completed"}],
            "d2": [{"started_at": now - 9000, "ended_at": now - 8400, "outcome": "crashed"},
                   {"started_at": 0, "ended_at": now - 8000, "outcome": "crashed"},
                   {"started_at": now - 7200, "ended_at": now - 6600, "outcome": "completed"},
                   {"started_at": now - 6000, "ended_at": None, "outcome": None}],
        }

    def __call__(self, name, args, **kw):
        self.calls.append((name, args))
        if name == "kanban_list":
            assert args["assignee"] == "writer" and args.get("status")
            rows = [t for t in self.tasks if t["status"] == args["status"]]
            return json.dumps({"tasks": rows, "count": len(rows), "truncated": False})
        if name == "kanban_show":
            return {"runs": self.runs.get(args["task_id"], [])}  # the registry may return a dict
        return json.dumps({"error": "Unknown tool"})


NOW = 1_793_900_000.0


def _handlers(store, dispatch=None, now=NOW, tz=None):
    from eta_plugin.tools import Handlers
    return Handlers(store, default_tz=lambda: tz, clock=lambda: now, dispatch=dispatch)


def test_kanban_live_read(store):
    fk = FakeKanban(NOW)
    out = json.loads(_handlers(store, fk, tz="America/New_York").eta_estimate(
        {"kanban_assignee": "writer", "lanes": 2}))
    assert out["status"] == "ok", out
    assert out["key_numbers"]["remaining_items"] == 3  # ready, todo, running; blocked excluded
    assert out["key_numbers"]["success_rate"] == round(2 / 3, 3)  # crash counts, open run ignored
    assert any("blocked" in n for n in out["notes"])
    assert [c[0] for c in fk.calls].count("kanban_show") == 2


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
    assert "error" in json.loads(ctx.tools["eta_estimate"]({"remaining_items": 2, "timezone": "Mars/Base"}))
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
    assert out["key_numbers"]["success_rate"] == round(2 / 3, 3)

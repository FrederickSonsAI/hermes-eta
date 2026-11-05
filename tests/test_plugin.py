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
    assert sorted(ctx.hooks) == _manifest_list("provides_hooks")
    assert "eta" in ctx.commands and "eta" in ctx.skills


def test_kanban_hooks_record_history(pkg, fake_ctx, monkeypatch):
    ctx = _register(pkg, fake_ctx, monkeypatch)
    for i in range(4):
        ctx.hooks["kanban_task_claimed"](task_id=f"t{i}", board="b", assignee="writer", run_id=1,
                                         profile_name="default")
        name = "kanban_task_completed" if i < 3 else "kanban_task_blocked"
        ctx.hooks[name](task_id=f"t{i}", board="b", assignee="writer", run_id=1,
                        profile_name="default", summary=None, reason="x")
    hist = json.loads(ctx.tools["eta_history"]({"lane": "writer"}))
    s = hist["lanes"]["writer"]
    assert s["runs_total"] == 4 and s["success_rate"] == 0.75 and s["fail_streak"] == 1


def test_hook_without_claim_still_counts(pkg, fake_ctx, monkeypatch):
    ctx = _register(pkg, fake_ctx, monkeypatch)
    ctx.hooks["kanban_task_completed"](task_id="z", board=None, assignee=None, run_id=None)
    hist = json.loads(ctx.tools["eta_history"]({}))
    assert hist["lanes"]["kanban"]["runs_total"] == 1


def test_hooks_never_raise(pkg, fake_ctx, monkeypatch):
    ctx = _register(pkg, fake_ctx, monkeypatch)
    from eta_plugin import store as store_mod

    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(store_mod, "_default_connect", boom)
    fresh = type(fake_ctx)()
    pkg.register(fresh)
    fresh.hooks["kanban_task_claimed"](task_id="a")
    assert "error" in json.loads(fresh.tools["eta_history"]({}))


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
    assert "error" in json.loads(ctx.tools["eta_estimate"]({"remaining_items": "lots"}))


def test_slash_command(pkg, fake_ctx, monkeypatch):
    ctx = _register(pkg, fake_ctx, monkeypatch)
    assert "No runs recorded" in ctx.commands["eta"]("")
    ctx.tools["eta_record_run"]({"lane": "docs", "duration_minutes": 5, "ok": True})
    assert "docs:" in ctx.commands["eta"]("")
    assert ctx.commands["eta"]("docs 3 1").startswith("Likely")
    assert ctx.commands["eta"]("docs").startswith("Usage")

"""The one-time, read-only import of past delegated runs from Hermes's session store."""

import hashlib
import json
import sqlite3

import pytest

NOW = 1_793_900_000.0

# The columns history.py reads, laid out as Hermes's SCHEMA_SQL has them (hermes_state_common.py).
SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, parent_session_id TEXT,
    started_at REAL NOT NULL, ended_at REAL, end_reason TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL,
    message_uid TEXT, compacted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE async_delegations (delegation_id TEXT PRIMARY KEY, origin_session TEXT NOT NULL,
    parent_session_id TEXT, state TEXT NOT NULL, dispatched_at REAL NOT NULL, completed_at REAL,
    updated_at REAL NOT NULL, result_json TEXT, task_json TEXT);
"""


def _entry(i, status, seconds):
    return {"task_index": i, "status": status, "summary": "PRIVATE SUMMARY", "duration_seconds": seconds,
            "tool_trace": [{"tool": "terminal", "args": "PRIVATE"}]}


def _call(call_id, args):
    return json.dumps([{"id": call_id, "type": "function",
                        "function": {"name": "delegate_task", "arguments": json.dumps(args)}}])


def make_db(path, *, extra=None):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    rows = []
    # Parent p1: a batch of two (one orchestrator by task, one leaf by default) plus a top-level role call.
    db.execute("INSERT INTO sessions VALUES ('p1','cli',NULL,?,?,'agent_close')", (NOW - 9000, NOW - 1000))
    for c in ("c1", "c2", "c3", "c4"):
        db.execute("INSERT INTO sessions VALUES (?, 'subagent', 'p1', ?, ?, 'agent_close')", (c, NOW - 8000, NOW - 7000))
    rows += [
        ("p1", "assistant", "", None, _call("call_a", {"tasks": [{"goal": "g1", "role": "orchestrator"},
                                                                 {"goal": "g2"}]}), None, NOW - 8000, "u1"),
        ("p1", "tool", json.dumps({"results": [_entry(0, "completed", 400.0), _entry(1, "failed", 30.0)],
                                   "total_duration_seconds": 400}), "call_a", None, "delegate_task", NOW - 7600, "u2"),
        ("p1", "assistant", "", None, _call("call_b", {"goal": "g3", "role": "orchestrator"}), None, NOW - 7000, "u3"),
        ("p1", "tool", json.dumps({"results": [_entry(0, "timeout", 600.0)]}), "call_b", None, "delegate_task",
         NOW - 6400, "u4"),
        # skipped: interrupted, zero length, a spilled preview, a background dispatch handle
        ("p1", "assistant", "", None, _call("call_c", {"goal": "g4"}), None, NOW - 6000, "u5"),
        ("p1", "tool", json.dumps({"results": [_entry(0, "interrupted", 50.0), _entry(1, "error", 0)]}),
         "call_c", None, "delegate_task", NOW - 5900, "u6"),
        ("p1", "tool", "<persisted-output>\n{\"results\": [{\"task_in...\n</persisted-output>", "call_d", None,
         "delegate_task", NOW - 5800, "u7"),
        ("p1", "tool", json.dumps({"status": "dispatched", "delegation_id": "d1"}), "call_e", None,
         "delegate_task", NOW - 5700, "u8"),
        # not a delegation at all
        ("p1", "tool", json.dumps({"results": [_entry(0, "completed", 5.0)]}), "call_f", None, "terminal",
         NOW - 5600, "u9"),
        # a compaction clone of u2 in a continuation session that also delegated
        ("p2", "tool", json.dumps({"results": [_entry(0, "completed", 400.0), _entry(1, "failed", 30.0)]}),
         "call_a", None, "delegate_task", NOW - 7600, "u2"),
    ]
    db.execute("INSERT INTO sessions VALUES ('p2','cli','p1',?,?,'compression')", (NOW - 5000, NOW - 100))
    db.execute("INSERT INTO sessions VALUES ('c9','subagent','p2',?,?,'agent_close')", (NOW - 4000, NOW - 3000))
    db.executemany("INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, tool_name, "
                   "timestamp, message_uid) VALUES (?,?,?,?,?,?,?,?)", rows + (extra or []))
    db.execute("INSERT INTO async_delegations VALUES ('d1','p1','p1','completed',?,?,?,?,?)",
               (NOW - 5700, NOW - 5000, NOW - 5000, json.dumps(_entry(0, "completed", 690.0)),
                json.dumps({"goal": "g5", "role": "leaf"})))
    db.execute("INSERT INTO async_delegations VALUES ('d2','p1','p1','error',?,?,?,?,?)",
               (NOW - 4000, NOW - 3000, NOW - 3000,
                json.dumps({"results": [_entry(0, "failed", 200.0), _entry(1, "completed", 900.0)]}),
                json.dumps({"goals": ["a", "b"], "is_batch": True, "role": "orchestrator"})))
    db.execute("INSERT INTO async_delegations VALUES ('d3','p1','p1','running',?,NULL,?,NULL,NULL)",
               (NOW - 100, NOW - 100))
    db.commit()
    db.close()
    return path


@pytest.fixture
def state_db(tmp_path):
    return make_db(tmp_path / "state.db")


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reads_roles_outcomes_and_durations(pkg, state_db):
    from eta_plugin.history import read_delegated_runs
    before = _digest(state_db)
    out = read_delegated_runs(state_db)
    got = sorted((lane, round(dur), ok) for lane, _start, dur, ok in out["runs"])
    assert got == sorted([
        ("subagent:orchestrator", 400, True), ("subagent:leaf", 30, False),   # per-task role, default leaf
        ("subagent:orchestrator", 600, False),                                 # top-level role, timeout
        ("subagent:leaf", 690, True),                                          # background, single
        ("subagent:orchestrator", 200, False), ("subagent:orchestrator", 900, True),  # background batch
    ])
    assert out["reason"] is None and not out["partial"]
    assert out["scanned"]["skipped"] == 2  # the spilled preview and the dispatch handle
    # start = end - duration; the end is the result message's (or row's) time
    starts = {round(dur): start for _l, start, dur, _ok in out["runs"]}
    assert starts[400] == NOW - 7600 - 400 and starts[690] == NOW - 5000 - 690
    assert _digest(state_db) == before                       # never writes
    assert not (state_db.parent / "state.db-wal").exists()   # no journal of its own
    assert "PRIVATE" not in json.dumps(out)                  # never the summary or tool trace


def test_newest_first_cap_and_cutoff(pkg, state_db):
    from eta_plugin.history import read_delegated_runs
    out = read_delegated_runs(state_db, max_runs=2)
    assert len(out["runs"]) == 2 and out["partial"]
    assert sorted(round(r[2]) for r in out["runs"]) == [200, 900]  # the two that ended last
    out = read_delegated_runs(state_db, before=NOW - 6000)
    assert sorted(round(r[2]) for r in out["runs"]) == [30, 400, 600]


def test_missing_odd_or_locked_store_is_empty_never_raises(pkg, tmp_path, state_db):
    from eta_plugin.history import read_delegated_runs
    assert read_delegated_runs(tmp_path / "nope.db")["reason"] == "no session history file"
    assert read_delegated_runs(None)["runs"] == []
    odd = tmp_path / "odd.db"
    sqlite3.connect(odd).executescript("CREATE TABLE sessions (id TEXT); CREATE TABLE messages (id INT);")
    assert "not recognised" in read_delegated_runs(odd)["reason"]
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite" * 100)
    out = read_delegated_runs(junk)
    assert out["runs"] == [] and out["reason"]
    locker = sqlite3.connect(state_db, timeout=0)
    locker.execute("PRAGMA locking_mode=EXCLUSIVE")
    locker.execute("BEGIN EXCLUSIVE")
    try:
        out = read_delegated_runs(state_db)
        assert out["runs"] == [] and out["reason"] == "session store busy or unreadable"
    finally:
        locker.rollback()
        locker.close()


def test_time_budget_interrupts_and_keeps_what_was_read(pkg, tmp_path):
    from eta_plugin.history import read_delegated_runs
    db = make_db(tmp_path / "s.db")
    ticks = iter([0.0] + [0.0] * 3 + [10.0] * 10_000)
    out = read_delegated_runs(db, budget_s=1.0, clock=lambda: next(ticks))
    assert out["partial"] and out["reason"] == "time budget reached"


def _handlers(store, reader, enabled=lambda: True, now=NOW):
    from eta_plugin.tools import Handlers
    return Handlers(store, clock=lambda: now, history_reader=reader, history_enabled=enabled)


def test_first_use_imports_once_and_says_so(pkg, store, state_db):
    from eta_plugin.history import read_delegated_runs
    calls = []

    def reader(before):
        calls.append(before)
        return read_delegated_runs(state_db, before=before)

    h = _handlers(store, reader)
    first = json.loads(h.eta_estimate({"lane": "subagent", "remaining_items": 2}))
    assert any("Read 6 past delegated runs" in n for n in first["notes"])
    assert first["history_used"] and first["based_on_runs"] == 6
    again = json.loads(h.eta_estimate({"lane": "subagent:orchestrator", "remaining_items": 1}))
    assert not any("past delegated" in n for n in again["notes"]) and again["based_on_runs"] == 4
    assert calls == [NOW]
    # a new process (new Handlers, same store) does not import again
    h2 = _handlers(store, reader)
    h2.eta_estimate({"lane": "subagent", "remaining_items": 1})
    assert calls == [NOW]
    hist = json.loads(h2.eta_history({}))
    assert hist["history_import"]["imported"] == 6 and "notes" not in hist


def test_import_stops_where_the_hook_started(pkg, store):
    seen = []
    store.record("subagent:leaf", 100, True, started_at=NOW - 5000, source="subagent_stop")
    h = _handlers(store, lambda before: seen.append(before) or {"runs": []})
    h.eta_history({})
    assert seen == [NOW - 4900]


def test_hook_never_imports(pkg, store):
    h = _handlers(store, lambda before: pytest.fail("the hook must not read history"))
    assert h.on_subagent_stop(child_role="leaf", child_status="completed", duration_ms=1000) is None


def test_disabled_busy_and_failing_reader(pkg, store, state_db):
    from eta_plugin.history import read_delegated_runs
    from eta_plugin.tools import HISTORY_KEY
    off = _handlers(store, lambda b: pytest.fail("disabled"), enabled=lambda: False)
    off.eta_history({})
    assert store.get_meta(HISTORY_KEY) is None
    busy = _handlers(store, lambda b: {"runs": [], "reason": "session store busy or unreadable"})
    busy.eta_history({})
    assert store.get_meta(HISTORY_KEY) is None  # retried by the next process
    boom = _handlers(store, lambda b: 1 / 0)
    assert "lanes" in json.loads(boom.eta_history({}))
    blank = _handlers(store, lambda b: read_delegated_runs(state_db.parent / "none.db", before=b))
    blank.eta_history({})
    assert json.loads(store.get_meta(HISTORY_KEY))["imported"] == 0  # a blank slate is done


def test_register_wires_history_off_the_hook_path(pkg, fake_ctx, monkeypatch, tmp_path):
    import sqlite3 as sq
    from eta_plugin import store as store_mod
    db = make_db(tmp_path / "state.db")
    monkeypatch.setattr(store_mod, "_default_connect", lambda: sq.connect(":memory:", check_same_thread=False))
    monkeypatch.setattr(pkg, "_hermes_state_db", lambda: db)
    pkg.register(fake_ctx)
    text = fake_ctx.commands["eta"]("")
    assert "Read 6 past delegated runs" in text and "subagent:orchestrator" in text


def test_config_off_skips_the_read(pkg, fake_ctx, monkeypatch, tmp_path):
    import sqlite3 as sq
    from eta_plugin import store as store_mod
    monkeypatch.setattr(store_mod, "_default_connect", lambda: sq.connect(":memory:", check_same_thread=False))
    monkeypatch.setattr(pkg, "_hermes_state_db", lambda: pytest.fail("read_history is off"))
    fake_ctx.config = {"read_history": False}
    pkg.register(fake_ctx)
    assert "No runs recorded yet" in fake_ctx.commands["eta"]("")


def test_config_off_as_text_or_zero_still_skips_the_read(pkg, fake_ctx, monkeypatch):
    import sqlite3 as sq
    from eta_plugin import store as store_mod
    monkeypatch.setattr(pkg, "_hermes_state_db", lambda: pytest.fail("read_history is off"))
    for off in ("false", " No ", "off", "0", 0):
        monkeypatch.setattr(store_mod, "_default_connect", lambda: sq.connect(":memory:", check_same_thread=False))
        fake_ctx.config = {"read_history": off}
        pkg.register(fake_ctx)
        assert "No runs recorded yet" in fake_ctx.commands["eta"]("")


def test_two_processes_import_only_once(pkg, tmp_path, state_db):
    """Two Hermes processes of one profile (gateway and CLI) share the plugin's file: whichever
    reaches first use second must not add the same past runs again."""
    import sqlite3 as sq
    from eta_plugin.history import read_delegated_runs
    from eta_plugin.store import RunStore
    from eta_plugin.tools import HISTORY_KEY
    path = tmp_path / "plugin.db"
    a = RunStore(lambda: sq.connect(path, check_same_thread=False))
    b = RunStore(lambda: sq.connect(path, check_same_thread=False))
    reader = lambda before: read_delegated_runs(state_db, before=before)  # noqa: E731
    ha, hb = _handlers(a, reader), _handlers(b, reader)
    assert b.get_meta(HISTORY_KEY) is None                 # b checked before a imported
    ha.eta_history({})
    assert hb.store.import_history([("subagent:leaf", NOW - 100, 50.0, True)], HISTORY_KEY, "{}") == 0
    total = sq.connect(path).execute("SELECT COUNT(*) FROM runs WHERE source = 'history'").fetchone()[0]
    assert total == 6

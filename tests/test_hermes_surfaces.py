"""The plugin's reads of Hermes go through public surfaces only (catalog rule 9). Hermes is not
installed here: its public modules are stood in by fakes, and the cron ledger is a real SQLite
file with Hermes 0.23's schema."""

import ast
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_793_900_000.0

# Hermes 0.23's ledger (cron/executions.py _initialize_schema), columns in table order.
LEDGER_V023 = """CREATE TABLE executions (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL, process_id TEXT NOT NULL,
    pid INTEGER NOT NULL, process_started_at INTEGER, status TEXT NOT NULL,
    handoff_pending INTEGER NOT NULL DEFAULT 0, handoff_started_at REAL, claimed_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT, error TEXT, delivery_outcome TEXT, scheduled_instant TEXT,
    progress_at TEXT)"""
# A ledger written before the later columns existed.
LEDGER_OLD = """CREATE TABLE executions (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL, process_id TEXT NOT NULL,
    pid INTEGER NOT NULL, process_started_at INTEGER, status TEXT NOT NULL, claimed_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT, error TEXT)"""


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    """Fakes of the public Hermes modules the plugin imports; edit the returned state."""
    state = SimpleNamespace(config={}, multiplex=False, home=tmp_path / "home")
    state.home.mkdir()

    def load_config_readonly():
        if isinstance(state.config, Exception):
            raise state.config
        return state.config

    for name, mod in {
            "hermes_cli": ModuleType("hermes_cli"),
            "hermes_cli.config": SimpleNamespace(load_config_readonly=load_config_readonly),
            "agent": ModuleType("agent"),
            "agent.secret_scope": SimpleNamespace(is_multiplex_active=lambda: state.multiplex),
            "hermes_constants": SimpleNamespace(get_hermes_home=lambda: state.home)}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    return state


def _register(pkg, fake_ctx, monkeypatch):
    from eta_plugin import store as store_mod
    monkeypatch.setattr(store_mod, "_default_connect",
                        lambda: sqlite3.connect(":memory:", check_same_thread=False))
    pkg.register(fake_ctx)
    return fake_ctx


# --- public surfaces only ----------------------------------------------------------------------

# What the plugin may import from Hermes: modules in Hermes's own public list (_PUBLIC in
# hermes_cli/plugin_validate_private_imports.py). Keep in step with that list.
PUBLIC_HERMES = {"hermes_cli.config", "hermes_constants", "agent.secret_scope", "tools.registry",
                 "plugins.plugin_storage"}
HERMES_TOP = {"hermes_cli", "hermes_constants", "hermes_time", "hermes_state", "agent", "cron",
              "tools", "plugins", "gateway", "utils", "run_agent"}


def test_only_public_hermes_modules_are_imported():
    seen = set()
    for path in ROOT.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and not node.level and node.module:
                mods = [node.module]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            else:
                continue
            for mod in mods:
                if mod.split(".")[0] in HERMES_TOP:
                    assert mod in PUBLIC_HERMES, f"{path.name}:{node.lineno} imports {mod}"
                    seen.add(mod)
    assert seen == PUBLIC_HERMES


# --- time zone ----------------------------------------------------------------------------------

def test_hermes_zone_from_config(pkg, hermes, monkeypatch):
    assert pkg._hermes_tz() is None  # nothing configured: server-local, like hermes_time.now
    hermes.config = {"timezone": " America/New_York "}
    assert pkg._hermes_tz() == ZoneInfo("America/New_York")
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Tokyo")  # the env var wins, as in hermes_time
    assert pkg._hermes_tz() == ZoneInfo("Asia/Tokyo")
    hermes.multiplex = True  # there it holds only the default profile's zone: config decides
    assert pkg._hermes_tz() == ZoneInfo("America/New_York")
    hermes.config = {"timezone": "Mars/Base"}
    assert pkg._hermes_tz() is None  # unloadable: server-local, as Hermes falls back
    hermes.config = OSError("config unreadable")
    assert pkg._hermes_tz() is None


def test_configured_hermes_zone_reaches_the_answer(pkg, fake_ctx, hermes, monkeypatch):
    hermes.config = {"timezone": "America/Chicago"}
    ctx = _register(pkg, fake_ctx, monkeypatch)
    out = json.loads(ctx.tools["eta_estimate"]({"lane": "ci", "remaining_items": 1,
                                                "avg_run_minutes": 10}))
    assert out["timezone"] == "America/Chicago" and out["timezone_source"] == "hermes"


# --- kanban dispatch cap --------------------------------------------------------------------------

def test_dispatch_cap_from_config(pkg, hermes, monkeypatch):
    monkeypatch.setattr(pkg, "_mem_total_kib", lambda: pytest.fail("cap is configured"))
    hermes.config = {"kanban": {"max_in_progress": 4}}
    assert pkg._hermes_dispatch_cap() == 4
    hermes.config = {"kanban": {"max_in_progress": "3"}}  # Hermes reads it with int()
    assert pkg._hermes_dispatch_cap() == 3
    hermes.config = {"kanban": {"max_in_progress": 4, "max_in_progress_per_profile": 2}}
    assert pkg._hermes_dispatch_cap() == 2
    for per in ("2", 2.0):  # the per-profile cap is read with int() as well
        hermes.config = {"kanban": {"max_in_progress": 4, "max_in_progress_per_profile": per}}
        assert pkg._hermes_dispatch_cap() == 2


def test_dispatch_cap_unset_uses_hermes_memory_default(pkg, hermes, monkeypatch):
    gib = 1024 * 1024
    for total, cap in ((1 * gib, 2), (3 * gib, 6), (64 * gib, 8)):  # 512 MiB a worker, 2..8
        monkeypatch.setattr(pkg, "_mem_total_kib", lambda total=total: total)
        for kanban in ({}, {"max_in_progress": 0}, {"max_in_progress": "many"}):
            hermes.config = {"kanban": kanban}
            got, note = pkg._hermes_dispatch_cap()
            assert got == cap and f"At most {cap} kanban" in note and "max_in_progress" in note
    hermes.config = {"kanban": {"max_in_progress_per_profile": 3}}  # a set cap is lower: no note
    assert pkg._hermes_dispatch_cap() == 3
    hermes.config = OSError("config unreadable")  # Hermes then runs on the derived default
    assert pkg._hermes_dispatch_cap()[0] == 8
    monkeypatch.setattr(pkg, "_mem_total_kib", lambda: None)  # macOS, Windows: no default cap
    hermes.config = {}
    assert pkg._hermes_dispatch_cap() is None


def test_mem_total_read_from_meminfo(pkg, tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotalish:  1 kB\nMemTotal:       16318412 kB\nMemFree:  1 kB\n")
    assert pkg._mem_total_kib(str(meminfo)) == 16318412
    assert pkg._mem_total_kib(str(tmp_path / "absent")) is None


# --- cron run ledger ----------------------------------------------------------------------------

def _iso(ts, hours):
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=hours))).isoformat()


def _ledger(home, schema, rows):
    path = home / "cron" / "executions.db"
    path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(schema)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(executions)")]
    for row in rows:
        full = {"source": "scheduler", "process_id": "p", "pid": 1, **row}
        keys = [k for k in full if k in cols]
        conn.execute(f"INSERT INTO executions ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                     [full[k] for k in keys])
    conn.commit()
    conn.close()
    return path, cols


def test_missing_cron_ledger_is_none_and_never_created(pkg, hermes):
    assert pkg._hermes_cron_runs("nightly") is None
    assert not (hermes.home / "cron").exists()
    (hermes.home / "cron").mkdir()
    assert pkg._hermes_cron_runs("nightly") is None
    assert list((hermes.home / "cron").iterdir()) == []


def test_cron_ledger_rows_match_list_executions(pkg, hermes):
    rows = [{"id": f"n{i}", "job_id": "nightly", "status": "completed",
             "claimed_at": _iso(NOW - 86400 * (i + 10), -5), "started_at": _iso(NOW - 86400 * (i + 10), -5),
             "finished_at": _iso(NOW - 86400 * (i + 10) + 600, -5)} for i in range(55)]
    # Across the DST change text order is not time order: 10:30-04:00 sorts after 10:00-05:00 but
    # is 14:30Z against 15:00Z, so the -05:00 run is the newer one (Hermes orders by julianday).
    rows += [{"id": "edt", "job_id": "nightly", "status": "completed", "claimed_at": "2026-11-01T10:30:00-04:00",
              "started_at": "2026-11-01T10:30:00-04:00", "finished_at": "2026-11-01T10:40:00-04:00"},
             {"id": "est", "job_id": "nightly", "status": "running", "claimed_at": "2026-11-01T10:00:00-05:00",
              "started_at": "2026-11-01T10:00:00-05:00", "progress_at": "2026-11-01T10:05:00-05:00"},
             {"id": "other", "job_id": "weekly", "status": "completed", "claimed_at": _iso(NOW, -5)}]
    path, cols = _ledger(hermes.home, LEDGER_V023, rows)
    before = (path.stat().st_mtime_ns, sorted(p.name for p in path.parent.iterdir()))
    got = pkg._hermes_cron_runs("nightly")
    assert len(got) == 50 and [r["id"] for r in got[:3]] == ["est", "edt", "n0"]
    assert all(list(r) == cols for r in got) and {r["job_id"] for r in got} == {"nightly"}
    assert got[0]["progress_at"] == "2026-11-01T10:05:00-05:00" and got[1]["handoff_pending"] == 0
    assert (path.stat().st_mtime_ns, sorted(p.name for p in path.parent.iterdir())) == before


def test_wal_cron_ledger_rows_read_content_untouched(pkg, hermes):
    """Hermes keeps its ledger in WAL mode. A read-only open may leave empty -wal/-shm files when
    no Hermes process has it open; the ledger's rows and bytes are never changed."""
    rows = [{"id": f"w{i}", "job_id": "nightly", "status": "completed", "claimed_at": _iso(NOW - 86400 * i, -5)}
            for i in range(3)]
    path, cols = _ledger(hermes.home, LEDGER_V023, rows)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.close()
    before = path.read_bytes()
    got = pkg._hermes_cron_runs("nightly")
    assert [r["id"] for r in got] == ["w0", "w1", "w2"] and all(list(r) == cols for r in got)
    assert path.read_bytes() == before


def test_ro_uri_keeps_a_unc_server_in_the_path(pkg):
    from pathlib import PureWindowsPath

    class P:
        def __init__(self, uri):
            self.uri = uri

        def as_uri(self):
            return self.uri

    assert pkg._ro_uri(P(PureWindowsPath(r"\\server\share\h\cron\executions.db").as_uri())) == \
        "file:////server/share/h/cron/executions.db"
    assert pkg._ro_uri(P(PureWindowsPath(r"C:\Users\a\.hermes\cron\executions.db").as_uri())) == \
        "file:///C:/Users/a/.hermes/cron/executions.db"
    assert pkg._ro_uri(P("file:///home/a/.hermes/cron/executions.db")) == "file:///home/a/.hermes/cron/executions.db"


def test_old_cron_ledger_is_read_as_is_not_upgraded(pkg, hermes):
    path, cols = _ledger(hermes.home, LEDGER_OLD, [
        {"id": "a", "job_id": "j", "status": "completed", "claimed_at": _iso(NOW - 900, 0),
         "started_at": _iso(NOW - 900, 0), "finished_at": _iso(NOW - 300, 0)}])
    got = pkg._hermes_cron_runs("j")
    assert [r["id"] for r in got] == ["a"] and "progress_at" not in got[0]
    conn = sqlite3.connect(path)
    assert [r[1] for r in conn.execute("PRAGMA table_info(executions)")] == cols
    conn.close()


def test_cron_ledger_wired_through_register(pkg, fake_ctx, hermes, monkeypatch):
    ctx = _register(pkg, fake_ctx, monkeypatch)
    none = json.loads(ctx.tools["eta_estimate"]({"cron_job": "nightly"}))
    assert any("No cron run history" in n for n in none["notes"])
    assert not (hermes.home / "cron").exists()
    now = datetime.now().timestamp()
    _ledger(hermes.home, LEDGER_V023, [
        {"id": f"r{d}", "job_id": "nightly", "status": "completed", "claimed_at": _iso(now - d * 86400, -5),
         "started_at": _iso(now - d * 86400, -5), "finished_at": _iso(now - d * 86400 + 600, -5)}
        for d in (1, 2, 3)])
    out = json.loads(ctx.tools["eta_estimate"]({"cron_job": "nightly"}))
    assert out["status"] == "ok" and out["based_on_runs"] == 3, out
    (hermes.home / "cron" / "executions.db").write_bytes(b"not a database")
    broken = json.loads(ctx.tools["eta_estimate"]({"cron_job": "nightly"}))
    assert any("No cron run history" in n for n in broken["notes"])

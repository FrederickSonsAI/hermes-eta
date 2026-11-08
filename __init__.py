"""eta: honest finish-time estimates for agent work.

Registers three tools (eta_estimate, eta_record_run, eta_history), the subagent_stop hook (records
each delegated run's role, outcome and duration), an /eta slash command and a bundled skill.
Kanban history is read live through Hermes's own kanban tools (ctx.dispatch_tool), so it is
correct across profiles; cron history is read, read-only, from Hermes's cron run ledger;
everything else is recorded in the plugin's own SQLite file. On first use (never from a hook)
past delegated runs are imported once, read-only and bounded, from Hermes's session store
(history.py).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from . import schemas
from .history import BUSY_TIMEOUT_MS, read_delegated_runs
from .store import RunStore
from .tools import Handlers

TOOLSET = "eta"

# Only Hermes's public plugin surfaces are imported (catalog rule 9): hermes_cli.config,
# hermes_constants, agent.secret_scope, tools.registry. Hermes's own readers for the time zone, the
# kanban caps and the cron ledger are internals, so the few lines of each are mirrored below.


def _hermes_config() -> dict:
    """Hermes's merged config (config.yaml + managed overlay + ${VAR} expansion), the same layers
    hermes_time and the kanban dispatcher read. ctx.get_config only sees this plugin's settings."""
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly()
    return cfg if isinstance(cfg, dict) else {}


def _hermes_tz():
    """Hermes's own configured zone, in hermes_time's order: HERMES_TIMEZONE, then config.yaml
    `timezone`. In a process that multiplexes profiles the env var holds only the default
    profile's value, so there (as in Hermes) config.yaml decides. None means server-local, as in
    hermes_time.now; so does a name ZoneInfo cannot load (Hermes falls back the same way)."""
    try:
        from agent.secret_scope import is_multiplex_active
        multiplex = is_multiplex_active()
    except Exception:
        multiplex = False
    name = "" if multiplex else os.getenv("HERMES_TIMEZONE", "").strip()
    if not name:
        try:
            value = _hermes_config().get("timezone")
        except Exception:
            value = None
        name = value.strip() if isinstance(value, str) else ""
    try:
        return ZoneInfo(name) if name else None
    except Exception:
        return None


# Hermes's default for an unset kanban.max_in_progress (derive_default_max_in_progress, 0.23.0):
# one worker per 512 MiB of MemTotal, clamped to 2..8; none where /proc/meminfo is unreadable
# (macOS, Windows). No public surface exposes it, so it is mirrored; an answer it limits says so,
# because a later Hermes may change the formula.
_DERIVED_MIB_PER_WORKER, _DERIVED_FLOOR, _DERIVED_CEILING = 512, 2, 8
DERIVED_CAP_NOTE = ("At most {cap} kanban task(s) run at once here: kanban.max_in_progress is "
                    "unset or not a positive whole number, so this assumes Hermes's memory-derived default (as of Hermes 0.23); "
                    "set it in config.yaml to pin the number.")


def _mem_total_kib(path: str = "/proc/meminfo"):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                if line.split(":", 1)[0] == "MemTotal":
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _derived_max_in_progress():
    kib = _mem_total_kib()
    if isinstance(kib, bool) or not isinstance(kib, int) or kib <= 0:
        return None
    return max(_DERIVED_FLOOR, min((kib // 1024) // _DERIVED_MIB_PER_WORKER, _DERIVED_CEILING))


def _configured_cap(raw):
    """kanban.max_in_progress as Hermes reads it (configured_max_in_progress): int(raw) when it is
    at least 1, so "4" counts; anything else is unset."""
    try:
        value = int(raw) if raw is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value is not None and value >= 1 else None


def _hermes_dispatch_cap():
    """The kanban dispatcher's effective concurrency cap for one assignee, if Hermes has one:
    the host cap (kanban.max_in_progress, or Hermes's memory-derived default) and the
    per-profile cap, whichever is lower. None means uncapped or unknown. When the cap in force
    is the derived default, returns (cap, note) so an answer it limits can say so."""
    try:
        kanban = _hermes_config().get("kanban")
    except Exception:
        kanban = None  # Hermes then runs on the derived default, with no per-profile cap
    kanban = kanban if isinstance(kanban, dict) else {}
    host = _configured_cap(kanban.get("max_in_progress"))
    derived = _derived_max_in_progress() if host is None else None
    per = _configured_cap(kanban.get("max_in_progress_per_profile"))  # Hermes reads it with int() too
    caps = [c for c in (host if host is not None else derived, per) if c is not None]
    if not caps:
        return None
    cap = min(caps)
    if derived is not None and (per is None or derived < per):
        return cap, DERIVED_CAP_NOTE.format(cap=cap)
    return cap


def _hermes_tool_visible(name: str) -> bool:
    """True when Hermes's own availability check offers tool `name` here (the same per-tool
    check that decides which tools the model sees). ctx.dispatch_tool does not apply it."""
    from tools.registry import registry
    return bool(registry.get_definitions({name}, quiet=True))


def _ro_uri(path: Path) -> str:
    """A file: URI SQLite accepts. A Windows UNC path (a home on a mapped network drive resolves to
    one) gives file://server/share, which SQLite refuses ("invalid uri authority"); an empty
    authority keeps the server in the path instead."""
    uri = path.as_uri()
    if uri.startswith("file://") and not uri.startswith("file:///"):
        uri = "file:////" + uri[len("file://"):]
    return uri


def _hermes_cron_runs(job_id: str):
    """Newest-first run history of one cron job: the rows, columns and order of Hermes's
    cron.executions.list_executions (limit 50). That reader is internal, so the ledger is opened
    here read-only (mode=ro, query_only) at Hermes's path for it. None when this Hermes has no
    ledger yet: it is never created just to read it. Unlike Hermes's reader this never upgrades
    an old ledger's schema; columns an old ledger lacks are just absent from the rows."""
    from hermes_constants import get_hermes_home
    path = get_hermes_home().resolve() / "cron" / "executions.db"
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"{_ro_uri(path)}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000,
                           check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        # Stamps carry a local offset that moves at DST, so text order is not time order.
        rows = conn.execute(
            "SELECT * FROM executions WHERE job_id=? "
            "ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC LIMIT 50",
            (str(job_id),)).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _hermes_state_db() -> Path:
    """Hermes's session store for the active profile (hermes_state's default path)."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "state.db"


def register(ctx):
    store = RunStore()

    def default_tz():
        try:
            return ctx.get_config("timezone", default=None)
        except Exception:
            return None

    def cron_reader(job_id):
        try:
            return _hermes_cron_runs(job_id)
        except Exception:
            return None

    def read_history_enabled():
        try:
            value = ctx.get_config("read_history", default=True)
        except Exception:
            return True
        if isinstance(value, bool):
            return value
        # A hand-edited config.yaml can hold "false" (quoted) or 0: those still mean off.
        if isinstance(value, str) and value.strip().lower() in ("false", "no", "off", "0"):
            return False
        if isinstance(value, (int, float)) and value == 0:
            return False
        return True

    def history_reader(before):
        # Read-only, bounded (row cap and time budget), never writes to Hermes's files.
        return read_delegated_runs(_hermes_state_db(), before=before)

    h = Handlers(store, default_tz=default_tz, dispatch=ctx.dispatch_tool, host_tz=_hermes_tz,
                 dispatch_cap=_hermes_dispatch_cap, cron_reader=cron_reader,
                 tool_visible=_hermes_tool_visible, history_reader=history_reader,
                 history_enabled=read_history_enabled)

    ctx.register_tool(name="eta_estimate", toolset=TOOLSET, schema=schemas.ETA_ESTIMATE,
                      handler=h.eta_estimate)
    ctx.register_tool(name="eta_record_run", toolset=TOOLSET, schema=schemas.ETA_RECORD_RUN,
                      handler=h.eta_record_run)
    ctx.register_tool(name="eta_history", toolset=TOOLSET, schema=schemas.ETA_HISTORY,
                      handler=h.eta_history)

    # Observer, fired once per delegated child on the parent thread
    # (tools/delegate_tool_results.py _fire_subagent_stop_hooks). One small SQLite insert.
    ctx.register_hook("subagent_stop", h.on_subagent_stop)

    ctx.register_command("eta", h.slash_eta,
                         description="Estimate when work in a lane finishes",
                         args_hint="[<lane> <remaining> [ahead] | @<assignee> [board] | cron <job id>]")

    skill_md = Path(__file__).parent / "skills" / "eta" / "SKILL.md"
    if skill_md.exists():
        ctx.register_skill("eta", skill_md)

"""eta: honest finish-time estimates for agent work.

Registers three tools (eta_estimate, eta_record_run, eta_history), the subagent_stop hook (records
each delegated run's role, outcome and duration), an /eta slash command and a bundled skill.
Kanban history is read live through Hermes's own kanban tools (ctx.dispatch_tool), so it is
correct across profiles; cron history is read from Hermes's cron run ledger; everything else is
recorded in the plugin's own SQLite file. On first use (never from a hook) past delegated runs are
imported once, read-only and bounded, from Hermes's session store (history.py).
"""

from __future__ import annotations

from pathlib import Path

from . import schemas
from .history import read_delegated_runs
from .store import RunStore
from .tools import Handlers

TOOLSET = "eta"


def _hermes_tz():
    """Hermes's own configured zone (None means server-local, as in hermes_time.now)."""
    from hermes_time import get_timezone
    return get_timezone()


def _hermes_dispatch_cap():
    """The kanban dispatcher's effective concurrency cap for one assignee, if Hermes has one:
    the host cap (kanban.max_in_progress, or Hermes's memory-derived default) and the
    per-profile cap, whichever is lower. None means uncapped or unknown."""
    caps = []
    try:
        from hermes_cli.kanban_db_dispatch import configured_max_in_progress, resolve_max_in_progress
        host = resolve_max_in_progress(configured_max_in_progress())
        if isinstance(host, int) and not isinstance(host, bool) and host > 0:
            caps.append(host)
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config_readonly
        per = ((load_config_readonly() or {}).get("kanban") or {}).get("max_in_progress_per_profile")
        if isinstance(per, int) and not isinstance(per, bool) and per > 0:
            caps.append(per)
    except Exception:
        pass
    return min(caps) if caps else None


def _hermes_tool_visible(name: str) -> bool:
    """True when Hermes's own availability check offers tool `name` here (the same per-tool
    check that decides which tools the model sees). ctx.dispatch_tool does not apply it."""
    from tools.registry import registry
    return bool(registry.get_definitions({name}, quiet=True))


def _hermes_cron_runs(job_id: str):
    """Newest-first run history of one cron job (read-only query). None when this Hermes has
    no cron run ledger yet: the ledger is never created just to read it."""
    from cron import executions
    from hermes_constants import get_hermes_home
    path = getattr(executions, "EXECUTIONS_FILE", None) or (
        get_hermes_home().resolve() / "cron" / "executions.db")
    if not Path(path).exists():
        return None
    return executions.list_executions(job_id=job_id, limit=50)


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

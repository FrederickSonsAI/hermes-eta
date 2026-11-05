"""eta: honest finish-time estimates for agent work.

Registers three tools (eta_estimate, eta_record_run, eta_history), an /eta slash command,
three kanban lifecycle hooks that record run history automatically, and a bundled skill.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import schemas
from .store import RunStore
from .tools import Handlers

logger = logging.getLogger(__name__)

TOOLSET = "eta"


def _claim_key(task_id, run_id) -> str:
    return f"{task_id}:{run_id if run_id is not None else ''}"


def _lane(assignee, board) -> str:
    return (assignee or board or "kanban").strip()


def make_hooks(store: RunStore):
    def on_claimed(task_id=None, board=None, assignee=None, run_id=None, **kwargs):
        try:
            store.open_claim(_claim_key(task_id, run_id), _lane(assignee, board))
        except Exception:  # hooks must never break the agent
            logger.debug("eta: could not record claim", exc_info=True)

    def on_completed(task_id=None, board=None, assignee=None, run_id=None, **kwargs):
        try:
            store.close_claim(_claim_key(task_id, run_id), _lane(assignee, board), ok=True)
        except Exception:
            logger.debug("eta: could not record completion", exc_info=True)

    def on_blocked(task_id=None, board=None, assignee=None, run_id=None, **kwargs):
        try:
            store.close_claim(_claim_key(task_id, run_id), _lane(assignee, board), ok=False)
        except Exception:
            logger.debug("eta: could not record block", exc_info=True)

    return on_claimed, on_completed, on_blocked


def register(ctx):
    store = RunStore()

    def default_tz():
        try:
            return ctx.get_config("timezone", default=None)
        except Exception:
            return None

    h = Handlers(store, default_tz=default_tz)

    ctx.register_tool(name="eta_estimate", toolset=TOOLSET, schema=schemas.ETA_ESTIMATE,
                      handler=h.eta_estimate)
    ctx.register_tool(name="eta_record_run", toolset=TOOLSET, schema=schemas.ETA_RECORD_RUN,
                      handler=h.eta_record_run)
    ctx.register_tool(name="eta_history", toolset=TOOLSET, schema=schemas.ETA_HISTORY,
                      handler=h.eta_history)

    on_claimed, on_completed, on_blocked = make_hooks(store)
    ctx.register_hook("kanban_task_claimed", on_claimed)
    ctx.register_hook("kanban_task_completed", on_completed)
    ctx.register_hook("kanban_task_blocked", on_blocked)

    ctx.register_command("eta", h.slash_eta,
                         description="Estimate when work in a lane finishes",
                         args_hint="<lane> <remaining items> [queued ahead]")

    skill_md = Path(__file__).parent / "skills" / "eta" / "SKILL.md"
    if skill_md.exists():
        ctx.register_skill("eta", skill_md)

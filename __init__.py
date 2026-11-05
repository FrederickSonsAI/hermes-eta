"""eta: honest finish-time estimates for agent work.

Registers three tools (eta_estimate, eta_record_run, eta_history), an /eta slash command and a
bundled skill. Kanban history is read live through Hermes's own kanban tools (ctx.dispatch_tool),
so it is correct across profiles; other work is recorded in the plugin's own SQLite file.
"""

from __future__ import annotations

from pathlib import Path

from . import schemas
from .store import RunStore
from .tools import Handlers

TOOLSET = "eta"


def register(ctx):
    store = RunStore()

    def default_tz():
        try:
            return ctx.get_config("timezone", default=None)
        except Exception:
            return None

    h = Handlers(store, default_tz=default_tz, dispatch=ctx.dispatch_tool)

    ctx.register_tool(name="eta_estimate", toolset=TOOLSET, schema=schemas.ETA_ESTIMATE,
                      handler=h.eta_estimate)
    ctx.register_tool(name="eta_record_run", toolset=TOOLSET, schema=schemas.ETA_RECORD_RUN,
                      handler=h.eta_record_run)
    ctx.register_tool(name="eta_history", toolset=TOOLSET, schema=schemas.ETA_HISTORY,
                      handler=h.eta_history)

    ctx.register_command("eta", h.slash_eta,
                         description="Estimate when work in a lane finishes",
                         args_hint="[<lane> <remaining> [ahead] | @<assignee> [board]]")

    skill_md = Path(__file__).parent / "skills" / "eta" / "SKILL.md"
    if skill_md.exists():
        ctx.register_skill("eta", skill_md)

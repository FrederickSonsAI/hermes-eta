## What does this PR do?

Adds the `eta` community plugin to the catalog (Hermes Agent Plugin Hackathon entry).

Before: asking an agent "when will this be done?" gets a guess, or the same stale ETA repeated after the facts changed.

After: `eta_estimate` returns a time range (likely and worst case) computed from how long runs in that lane actually take, how often they fail, how much is queued ahead, how many run at once and when a daily cap resets. When work keeps failing it returns "no honest ETA" instead of a time, and when a step waits on a person it says the clock starts when they act.

How: three tools (`eta_estimate`, `eta_record_run`, `eta_history`), an `/eta` slash command and a bundled `eta:eta` skill. For kanban work it reads an assignee's open tasks and run history live through `kanban_list` / `kanban_show` via `ctx.dispatch_tool`, only where Hermes offers those tools. Delegated subagent runs are recorded automatically through the `subagent_stop` hook, and on first use past ones are read once from Hermes's session history, cron jobs are read from the cron run ledger, and other work can be recorded with `eta_record_run`, all into one SQLite file via `plugin_db`. With little history it says how many runs an answer rests on and widens the worst case; with none it gives a labelled rough guess or says what it needs.

## Related Issue

None; new catalog entry.

## Type of Change

- [x] ✨ New feature (non-breaking change that adds functionality)

## Changes Made

- `plugin-catalog/eta.yaml`: new entry pinned to `FrederickSonsAI/hermes-eta@f91ddda119a233d07199e61b6221a018f1afded6` (v0.1.0).

Surfaces the plugin uses: `ctx.register_tool`, `ctx.register_command`, `ctx.register_skill`, `ctx.dispatch_tool`, `ctx.get_config`, `plugins.plugin_storage.plugin_db`. One hook (`subagent_stop`, bounded write, fails open); no core overrides, no middleware, no Desktop or dashboard code.

Disclosure (rule 13):
- No network calls, no shell commands, no credentials or environment variables, no telemetry.
- Writes only `$HERMES_HOME/plugin-data/eta/data.db` (lane names, run start times, durations, success flags; trimmed to 2,000 rows per lane).
- Kanban estimates read the board through Hermes's own `kanban_list` / `kanban_show`, only where Hermes offers them; note that `kanban_list` itself promotes dependency-cleared tasks to ready.
- Reads the cron run ledger if it exists, and Hermes's time zone and kanban concurrency settings.
- Reads `$HERMES_HOME/state.db` once on first use, with SQLite read-only mode (`mode=ro`, `query_only`), bounded to 6,000 runs and 1.5 s, to learn from delegated subagent runs made before install. Takes only role, start, duration and success, never goals, summaries, message text or session ids. Off with `read_history: false`.
- One hook, `subagent_stop`: stores the child's role, outcome and duration only, never its summary or tool calls; no background processes.

## How to Test

1. `hermes plugins install FrederickSonsAI/hermes-eta`
2. `/eta mylane 10` after recording a few runs with `eta_record_run`, or `/eta @<assignee>` on a kanban board.
3. Plugin tests: `python -m pytest -c tests/pytest.ini --rootdir tests tests` (93 pass).

## Checklist

### Code

- [x] I searched for existing PRs to make sure this isn't a duplicate
- [x] My PR contains **only** changes related to this feature
- [x] I've added tests for my changes (93 tests in the plugin repo)

### Documentation & Housekeeping

- [x] I've updated relevant documentation (plugin README) — or N/A

🤖 Generated with [Claude Code](https://claude.com/claude-code)

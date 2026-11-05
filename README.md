# eta: honest ETAs for Hermes

Ask your agent "when will this be done?" and get a time range built from real run history:
how long runs in that lane actually take, how often they fail, how much is queued ahead, how
many can run at once and when a daily cap resets. When work keeps failing, it says there is no
honest ETA instead of inventing one. When a step waits on you, it says the clock starts when
you act.

```
> when will the docs migration finish? 14 pages left on the writer lane
Likely between 3:40 PM and 5:10 PM today.
Rests on: 82% of writer runs succeed, typical run 6 min, 2 lanes.
Biggest risk: 9 runs are queued ahead of this work.
```

## What it adds

| Surface | Name | What it does |
|---|---|---|
| Tool | `eta_estimate` | Time range (likely and worst case), key numbers, biggest risk, or `unbounded` / `blocked_on_person`. With `kanban_assignee` it reads that assignee's open tasks and past runs live from the board |
| Tool | `eta_record_run` | Record one finished run for a lane (cron, CI, delegated agents) |
| Tool | `eta_history` | Per-lane runs in 3h and 24h, success rate, typical and slow run length, failure streak |
| Command | `/eta <lane> <remaining> [ahead]`, `/eta @<assignee> [board]` | Quick estimate in chat; `/eta` alone lists lanes |
| Skill | `eta:eta` | The workflow for answering ETA questions well |

## Install

```
hermes plugins install FrederickSonsAI/hermes-eta
hermes plugins enable eta
```

Optional setting: `plugins.entries.eta.settings.timezone` (for example `America/New_York`).
It also shows in the Desktop under Settings, Plugins.

## Kanban, live

`eta_estimate(kanban_assignee="writer")` (or `/eta @writer`) calls Hermes's own `kanban_list`
and `kanban_show` tools through `ctx.dispatch_tool`. Both are read-only, and the board is shared,
so it works across profiles. Open tasks (triage, todo, scheduled, ready,
running, review) are the remaining work; runs that ended `completed` or
`review_requested` count as successes and `crashed`, `timed_out`, `spawn_failed` or `gave_up` as
failures. Blocked tasks usually wait on a
person, so they are reported, not counted as failures. This needs the kanban toolset in the
session (an orchestrator profile).

## How the estimate works

- Runs needed = remaining items / success rate (last 24 hours, falling back to all history).
- Time = (runs queued ahead + runs needed) x typical run length / parallel lanes.
- Worst case uses slow (90th percentile) runs and a success rate 15 points lower.
- A daily run cap pushes the remainder past the next reset hour; all math is in UTC, so
  daylight-saving changes never shift the answer.
- Three failures in a row, under 15% success, a cap of 0, or more than a year of capped work
  returns `unbounded`: no honest ETA.
- Fewer than 5 recorded runs is marked low confidence.

## Disclosure

- No network calls. No shell commands. No credentials, no environment variables.
- Writes only its own SQLite file in Hermes's per-plugin data directory
  (`<hermes home>/plugin-data/eta/data.db`), holding lane names, run start times, durations
  and success flags for runs recorded with `eta_record_run`. Old rows are trimmed to 2,000 per lane.
- When asked for a kanban estimate, reads the board through Hermes's `kanban_list` and
  `kanban_show` tools (task statuses and run times). It never writes to the board.
- No hooks, no background processes, no telemetry.

## Development

```
python -m pytest -c tests/pytest.ini --rootdir tests tests
hermes plugins validate . --install-deps
```

MIT licensed.

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
| Tool | `eta_estimate` | Time range (likely and worst case), key numbers, biggest risk, or `unbounded` / `blocked_on_person` |
| Tool | `eta_record_run` | Record one finished run for a lane (cron, CI, delegated agents) |
| Tool | `eta_history` | Per-lane runs in 3h and 24h, success rate, typical and slow run length, failure streak |
| Hooks | `kanban_task_claimed`, `kanban_task_completed`, `kanban_task_blocked` | Record kanban run history automatically, per assignee |
| Command | `/eta <lane> <remaining> [queued ahead]` | Quick estimate in chat; `/eta` alone lists lanes |
| Skill | `eta:eta` | The workflow for answering ETA questions well |

## Install

```
hermes plugins install <owner>/hermes-eta
hermes plugins enable eta
```

Optional setting: `plugins.entries.eta.settings.timezone` (for example `America/New_York`).
It also shows in the Desktop under Settings, Plugins.

## How the estimate works

- Runs needed = remaining items / success rate (last 24 hours, falling back to all history).
- Time = (runs queued ahead + runs needed) x typical run length / parallel lanes.
- Worst case uses slow (90th percentile) runs and a success rate 15 points lower.
- A daily run cap pushes the remainder past the next reset hour.
- Three failures in a row, or under 15% success, returns `unbounded`: no honest ETA.
- Fewer than 5 recorded runs is marked low confidence.

## Disclosure

- No network calls. No shell commands. No credentials, no environment variables.
- Reads and writes only its own SQLite file in Hermes's per-plugin data directory
  (`<hermes home>/plugin-data/eta/data.db`), holding lane names, run start times, durations
  and success flags. Old rows are trimmed to 2,000 per lane.
- No background processes, no telemetry.
- Hooks only observe kanban events; they never change tasks, approvals or tool calls.

## Development

```
python -m pytest -c tests/pytest.ini --rootdir tests tests
hermes plugins validate . --install-deps
```

MIT licensed.

# eta: honest ETAs for Hermes

Ask your agent "when will this be done?" and get a time range built from real run history:
how long runs in that lane actually take, how often they fail, how much is queued ahead, how
many can run at once and when a daily cap resets. Time already spent on running work is taken
off. When work keeps failing, it says there is no honest ETA instead of inventing one. When a
step waits on you, it says the clock starts when you act.

It is useful from the first install. Delegated subagent runs are recorded automatically, kanban
boards and cron jobs are read live, and with little or no history the answer says exactly how
much it rests on:

```
Before (no plugin):   "Should be done in about an hour."            (a guess)
Day one, 0 runs:      Rough guess: Likely between 3:52 PM and 5:04 PM today. No finished runs
                      for this work yet, so this rests only on how long the current run has
                      been going (12 min).
After 2 runs:         Likely between 4:20 PM and 5:20 PM today. Based on 2 runs, so the worst
                      case is wide.
```

```
> when will the docs migration finish? 14 pages left on the writer lane
Likely between 3:40 PM and 5:10 PM today.
Rests on: 82% of writer runs succeed, typical run 6 min, 2 lanes.
Biggest risk: 9 runs are queued ahead of this work.
```

## What it adds

| Surface | Name | What it does |
|---|---|---|
| Tool | `eta_estimate` | Time range (likely and worst case), key numbers, biggest risk, or `unbounded` / `blocked_on_person`. With `kanban_assignee` it reads that assignee's open, running and finished tasks live from the board; with `cron_job` it reads that job's run history; `running_minutes` says how long the current run has been going |
| Tool | `eta_record_run` | Record one finished run for a lane (CI, scripts, anything Hermes does not report itself) |
| Tool | `eta_history` | Per-lane runs in 3h and 24h, success rate, typical and slow run length, failure streak |
| Hook | `subagent_stop` | Records each delegated subagent run (role, outcome, duration) as lane `subagent:<role>`; `lane="subagent"` pools them (an orchestrator's run includes its children's time, so ask for `subagent:leaf` to estimate leaf work) |
| Command | `/eta <lane> <remaining> [ahead]`, `/eta @<assignee> [board]`, `/eta cron <job id> [remaining]` | Quick estimate in chat; `/eta` alone lists lanes |
| Skill | `eta:eta` | The workflow for answering ETA questions well |

## Install

```
hermes plugins install FrederickSonsAI/hermes-eta
hermes plugins enable eta
```

## Settings

| Key | Default | What it does |
|---|---|---|
| `plugins.entries.eta.settings.timezone` | empty | IANA zone for answers, e.g. `America/New_York`. Empty means Hermes's own time zone (`timezone` in config.yaml, else the machine's local zone) |

No setting is required. A zone that cannot be loaded (a typo, or a device with no time zone
data) never fails the estimate: the answer falls back to Hermes's zone, then UTC, and says so.

## Kanban, live

`eta_estimate(kanban_assignee="writer")` (or `/eta @writer`) calls Hermes's own kanban tools
through `ctx.dispatch_tool`, so it works across profiles: one `kanban_list` call for the whole
board (up to 200 tasks), plus `kanban_show` for at most 6 tasks (running ones first, then the
most recently finished), because only `kanban_show` returns each attempt's outcome. Open tasks
(triage, todo, scheduled, ready, running, review) are the remaining work. Only the assignee's own
attempts count (a task's run list also holds a reviewer's runs). Attempts that ended `completed`
or `review_requested` count as successes, except a hand-off to review that the review then sent
back (`changes_requested`), which counts as a failed attempt; for the reviewer, that review run
itself is a finished run. `crashed`, `timed_out`, `spawn_failed`, `gave_up`, `stale` and
`reclaimed` (worker died, claim expired) count as failed attempts; a reclaim an operator caused
(manual reclaim, ancestor reopened, task archived) counts as neither, as in Hermes's own failure
count. Blocked tasks usually wait on a person, so they are reported, not counted as failures.

Parallel lanes default to how many of the assignee's tasks are running or ready now, within
Hermes's dispatch caps (`kanban.max_in_progress` or its memory-derived default, and
`kanban.max_in_progress_per_profile`). A running task is credited with the time its current
attempt has already run.

Side effect to know about: Hermes's `kanban_list` (like `hermes kanban list`) first promotes
tasks whose dependencies have cleared to `ready`. The plugin itself writes nothing to the board.
No read-only kanban tool lists a board, so this cannot be avoided without bypassing Hermes's
kanban tools. The board is read only where Hermes itself offers `kanban_list` (a profile with
the kanban toolset, not a delegated child or a dispatcher-spawned worker); elsewhere the
estimate says the kanban tools are not available and calls nothing.

## Cron, live

`eta_estimate(cron_job="<job id>")` (or `/eta cron <job id>`) reads that job's last 50 runs from
Hermes's cron run ledger: completed runs count as successes, failed and unknown as failures,
cancelled and missed are skipped. A run in progress is credited with its elapsed time and
flagged when it has shown no progress for over an hour. If the job is not running, the answer is
how long one run takes once it starts, not when the schedule fires it.

## How the estimate works

- "Likely" is the typical case. When at least half the runs succeed (last 24 hours, falling back
  to all history), the typical item takes one run, so runs needed = remaining items. Below half,
  it is the expected number, remaining items / success rate, not rounded up.
- Time = (runs queued ahead + runs needed) x typical run length / parallel lanes.
- Retries for failures go in the worst case: slow (90th percentile) runs and remaining items /
  a success rate 15 points lower, rounded up to whole runs.
- Changed after a walk-forward backtest on one fleet's real jobs: the first version always
  rounded remaining / success rate up, so with under 5 runs (rate pulled below 100%) one job
  counted as two runs and "likely" doubled. The fix is a logic change, no constant was tuned, and
  it was checked on the same data that showed the flaw.
- Runs already going are credited with the time they have run; one past the slow mark gets no
  credit in the worst case (it may have to start over) and is named as the biggest risk.
- A daily run cap pushes the remainder past the next reset hour; all math is in UTC, so
  daylight-saving changes never shift the answer.
- Three failures in a row, under 15% success, a cap of 0, or more than a year of capped work
  returns `unbounded`: no honest ETA.
- Under 5 runs: the answer says "Based on N runs, so the worst case is wide", the success rate
  is pulled toward 80% (so one lucky or unlucky run cannot decide it), and the worst case uses
  at least twice the typical run and a success rate 30 points lower. These are deliberate
  choices, not measured constants: the 80% counts as two runs, so one failure gives 53%, one
  success 87%, and from 5 runs on only the observed rate is used; the slowest of a handful of
  runs is a poor guide to the slow tail, so the worst case never assumes less than twice the
  typical run. One failure in one run is
  not "no honest ETA"; three failures in a row still is.
- No finished runs: if a run is in progress, a labelled rough guess from its elapsed time
  (as long again likely, three times as long again at the slow end, plus a retry when nothing
  says how often it fails). Otherwise, a labelled rough guess from other work recorded on this
  Hermes (for example subagent runs). Otherwise it says plainly what it needs: a typical run
  length, how long the current run has been going, or one finished run.

## Disclosure

- No network calls. No shell commands. No credentials, no environment variables, no telemetry,
  no background processes.
- Writes only its own SQLite file in Hermes's per-plugin data directory
  (`<hermes home>/plugin-data/eta/data.db`): lane names, run start times, durations and
  success flags, for runs recorded with `eta_record_run` and, automatically, for every
  delegated subagent run Hermes reports through the `subagent_stop` hook (lane
  `subagent:<role>`). From the hook it keeps only the role, status and duration; it never
  stores the child's summary, tool calls or session ids. Old rows are trimmed to 2,000 per lane.
- Hook: `subagent_stop`, an observer. Hermes fires it once per delegated child (batch and
  nested delegations included: each child is reported by its own parent) on the parent's
  thread. It runs one small insert, waits at most a quarter of a second for the file (if
  another thread or process holds it, that run is not recorded), never changes the delegation,
  and swallows its own errors. Interrupted children are not recorded.
- Reads, only when asked for an estimate:
  - the kanban board, through Hermes's `kanban_list` and `kanban_show` tools (task statuses,
    run times and outcomes). `kanban_list` promotes dependency-cleared tasks to `ready` as part
    of Hermes's own listing; the plugin itself never writes to the board.
  - a cron job's run history, through Hermes's `cron.executions.list_executions` (statuses and
    timestamps), only when the cron run ledger already exists. Opening it is Hermes's own code,
    which may upgrade an old ledger's schema.
  - Hermes's time zone (`hermes_time.get_timezone`) and kanban concurrency settings
    (`kanban.max_in_progress`, `kanban.max_in_progress_per_profile`; when the first is unset,
    Hermes's own default is derived from the machine's total memory).
  - whether Hermes offers the kanban tools here (Hermes's tool registry availability check).
- Blocks nothing and never asks for approval.

## Per surface

- CLI, TUI, Desktop, web dashboard, gateway: the tools, hook and skill work the same everywhere
  Hermes runs; `/eta` works wherever plugin slash commands do.
- Desktop: the time zone setting is under Settings, Plugins.
- Web dashboard: there is no plugin settings form; set `plugins.entries.eta.settings.timezone`
  on the Config page in YAML mode.
- Nous mobile app (iOS and Android beta): it is a remote for your own gateway, so the plugin
  runs on the gateway host, not the phone. Install it there.
- Android / Termux: run `pkg install git` first (any `hermes plugins install` needs it) and
  `pkg install tzdata` so named time zones load (without it, answers fall back to Hermes's
  zone or UTC and say so). Note that Nous's own Termux page currently marks the Termux package
  as not working.
- Windows, macOS, Linux: nothing extra; pure Python standard library.

## Development

```
python -m pytest -c tests/pytest.ini --rootdir tests tests
hermes plugins validate . --install-deps
```

MIT licensed.

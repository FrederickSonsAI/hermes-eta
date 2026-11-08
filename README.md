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
| `plugins.entries.eta.settings.read_history` | `true` | On first use, read past delegated subagent runs from Hermes's session history once (see Disclosure). `false` skips it; runs already read stay until trimmed |

No setting is required. A zone that cannot be loaded (a typo, or a device with no time zone
data) never fails the estimate: the answer falls back to Hermes's zone, then UTC, and says so.

## Kanban, live

`eta_estimate(kanban_assignee="writer")` (or `/eta @writer`) calls Hermes's own kanban tools
through `ctx.dispatch_tool`, so it works across profiles: one `kanban_list` call for the whole
board (up to 200 tasks), plus `kanban_show` for at most 6 tasks, because only `kanban_show`
returns each attempt's outcome: running ones first, then, taking turns, the most recently
finished and the most recently first-claimed of the tasks that were attempted but are not running
or done (sent back to ready after a failed attempt, blocked after a give-up, in review). Hermes
marks a task's first claim in `kanban_list` and never clears it, so no extra call is needed to
find them, and a lane that keeps crashing shows its failures instead of a confident range. Open
tasks (triage, todo, scheduled, ready, running, review) are the remaining work. Only the assignee's own
attempts count (a task's run list also holds a reviewer's runs). Attempts that ended `completed`
or `review_requested` count as successes, except a hand-off to review that the review then sent
back (`changes_requested`), which counts as a failed attempt; for the reviewer, that review run
itself is a finished run. `crashed`, `timed_out`, `spawn_failed`, `gave_up`, `stale` and
`reclaimed` (worker died, claim expired) count as failed attempts; a reclaim an operator caused
(manual reclaim, ancestor reopened, task archived) counts as neither, as in Hermes's own failure
count. Blocked tasks usually wait on a person, so being blocked is not a failure (their failed
attempts still count) and they are reported, not counted as remaining work. The same goes for a
todo task whose parent on the board (from `kanban_list`'s `parents` field) is blocked, directly or
through another such todo task: Hermes keeps it in todo until that parent is done, so it waits on
the same person. A parent assigned to someone else is not on the assignee's board and is not seen.
When blocked tasks and the todo tasks that wait on them are all that is left, they are the
remaining work and the answer is `blocked_on_person`: "Clock starts when someone unblocks N
blocked task(s)."

Parallel lanes default to how many of the assignee's tasks are running or ready now (when only
blocked tasks are left, how many will be ready once unblocked), within Hermes's dispatch caps
(`kanban.max_in_progress` or its memory-derived default, and
`kanban.max_in_progress_per_profile`). Hermes has no public reader for that default, so the
plugin mirrors Hermes 0.23's formula (one worker per 512 MiB of total memory, 2 to 8; none where
`/proc/meminfo` cannot be read); when it is the cap that limits the lanes, the answer says so and
how to pin it. A running task is credited with the time its current attempt has already run.

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
flagged when it has shown no progress for over an hour. If the job is not running, a range is how
long one run takes once it starts, not when the schedule fires it, and the answer says so. A "No
honest ETA" answer only notes that the job is not running now.

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
  returns `unbounded`: no honest ETA. When the work also waits on a person, that answer and a
  `no_data` one still start with "Clock starts when ...".
- Under 5 runs: the answer says "Based on N runs, so the worst case is wide", the success rate
  is pulled toward 80% (so one lucky or unlucky run cannot decide it), and the worst case uses
  at least twice the typical run and a success rate 30 points lower. These are deliberate
  choices, not measured constants: the 80% counts as two runs, so one failure gives 53%, one
  success 87%, and from 5 runs on only the observed rate is used; the slowest of a handful of
  runs is a poor guide to the slow tail, so the worst case never assumes less than twice the
  typical run. One failure in one run is
  not "no honest ETA"; three failures in a row still is.
- Past runs: on first use (the first estimate, `/eta` or `eta_history`, never the hook) it reads
  the delegated subagent runs already in Hermes's session history, so subagent lanes have data
  from day one. Only runs that ended before the hook's first recorded run are taken, so nothing
  is counted twice. Because "likely" uses the last 24 hours when there are any, old history
  mostly matters in the first days after install; on a synthetic two-year history the first 5
  estimates per lane were answered 98% of the time instead of 83%, and later estimates were the
  same with or without it.
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
  - a cron job's run history (statuses and timestamps) from Hermes's cron run ledger
    (`<hermes home>/cron/executions.db`), only when it already exists: the same rows and order
    as Hermes's own `list_executions`, opened with SQLite's read-only mode (`mode=ro`,
    `query_only`), so it is never created and an old ledger's schema is never upgraded.
  - Hermes's time zone (`HERMES_TIMEZONE`, else `timezone` in config.yaml, as Hermes resolves
    it) and kanban concurrency settings (`kanban.max_in_progress`,
    `kanban.max_in_progress_per_profile`; when the first is unset, Hermes's default is derived
    from the machine's total memory), through Hermes's public config API
    (`hermes_cli.config.load_config_readonly`).
  - Only Hermes's public plugin surfaces are imported (`hermes_cli.config`, `hermes_constants`,
    `agent.secret_scope`, `tools.registry`, `plugins.plugin_storage`), so `hermes plugins
    validate` reports no private core imports.
  - whether Hermes offers the kanban tools here (Hermes's tool registry availability check).
- Reads, once, on first use: Hermes's session history file (`<hermes home>/state.db`), to learn
  from delegated subagent runs made before the plugin was installed. Hermes has no public plugin
  API for this, so it opens the file with SQLite's read-only mode (`mode=ro`, `query_only`) and
  never writes to it; SQLite itself may create its usual `-wal`/`-shm` coordination files next to
  a database in WAL mode, as any reader does. From each `delegate_task` result and background
  delegation it takes only the role, start, duration and success; never a goal, summary, message
  text, tool trace or session id. It is bounded: at most 6,000 runs from the 4,000 newest
  delegating sessions, a 1.5 second time budget, and at most a 0.2 second wait if Hermes is
  writing. A busy store is tried again the next time Hermes starts; a missing, unreadable or
  unrecognised one (another Hermes version) is skipped, and the answer says how many past runs
  were read. Turn it off with `read_history: false`.
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

---
name: eta
description: Answer "when will this be done?" with a time range built from live numbers, never a guess or a repeated old ETA.
---

# ETA

Give a completion estimate built from live numbers, so the answer changes when the facts change.
Never repeat an earlier ETA without recomputing it.

## 1. Pin down what is being estimated
- Match the ask to one piece of work: a kanban task, a cron job, a delegated agent, a queue.
- Find who or what runs the next step (the lane: an assignee, agent or queue name).
- Check whether any step waits on a person (an approval, a reply, a sign-in). A step that waits
  on a person has no clock: say so, and estimate from the moment they act.

## 2. Pull live numbers
- For kanban work, `eta_estimate` with `kanban_assignee` reads open, running and finished tasks
  live. For a cron job, pass `cron_job` (its id). For delegated subagents, use `lane="subagent"`:
  their runs are recorded automatically.
- If something is already running, pass `running_minutes` (how long it has been going) unless
  the board or cron history already shows it.
- For other work, `eta_history` shows each lane's runs in the last 3h and 24h, success rate,
  typical and slow run length and the current failure streak. Record runs with `eta_record_run`
  as they finish.
- Count how much work is left and how many runs are queued ahead of it.
- Note limits: how many runs can go at once, any daily run or budget cap and when it resets.

## 3. Compute
- Call `eta_estimate` with `remaining_items` and the `lane` (or `kanban_assignee`), and any number you know better
  (`queue_ahead`, `lanes`, `daily_run_cap`, `runs_left_today`, `reset_hour`, `review_minutes`,
  `waiting_on`, `timezone`).
- Runs needed = remaining items / success rate. Time = (runs ahead + runs needed) x typical run
  length / parallel lanes, pushed past the daily reset when the cap runs out.
- If the lane keeps failing the same way (`status: unbounded`), there is no honest ETA.

## 4. Answer
- First line: a time range in the user's time zone, e.g. "Likely between 2 and 4 PM today."
  Never a single precise time.
- Then the one or two numbers it rests on and the single biggest thing that could blow it up.
- If a step waits on a person, say "Clock starts when <they do X>."
- If it keeps failing, say plainly there is no honest ETA until the failure is fixed, and what
  fixing it takes.
- Keep the "Based on N runs" or "Rough guess" wording from `answer`: it tells the user how much
  the estimate rests on. If `status` is `no_data`, say what it needs instead of guessing.
- Keep it short and say how fresh the data is (`as_of`).

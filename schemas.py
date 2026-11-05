"""Tool schemas: what the model reads to decide when to call each tool."""

ETA_ESTIMATE = {
    "name": "eta_estimate",
    "description": (
        "Estimate when a piece of agent work will finish, as a time range (likely and worst "
        "case), from real run history. Use it whenever the user asks 'when will this be done', "
        "'how long', or for an ETA on a task, queue, job, agent or kanban board. Pass the lane "
        "(the agent or queue doing the work) to use its recorded runs, or kanban_assignee to read a "
        "kanban assignee's open tasks and past runs live, and override "
        "any number you know better. Returns 'unbounded' instead of a time when the work keeps "
        "failing, and 'blocked_on_person' when a step waits on a human. Always recompute: never "
        "repeat an earlier ETA."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "remaining_items": {"type": "integer", "minimum": 0,
                                "description": "Units of work left (tasks, steps or jobs). Optional "
                                               "with kanban_assignee: open tasks are counted."},
            "kanban_assignee": {"type": "string",
                                "description": "Kanban assignee (profile) whose board history and open "
                                               "tasks to read live through the kanban tools."},
            "board": {"type": "string", "description": "Kanban board slug (default: current board)."},
            "lane": {"type": "string",
                     "description": "Agent, kanban assignee or queue name whose history to use."},
            "queue_ahead": {"type": "integer", "minimum": 0,
                            "description": "Runs queued ahead of this work in the same lane."},
            "lanes": {"type": "integer", "minimum": 1,
                      "description": "How many runs can go at once (parallel workers)."},
            "avg_run_minutes": {"type": "number", "exclusiveMinimum": 0,
                                "description": "Override the typical run length in minutes."},
            "success_rate": {"type": "number", "minimum": 0, "maximum": 1,
                             "description": "Override the share of runs that succeed (0 to 1)."},
            "daily_run_cap": {"type": "integer", "minimum": 0,
                              "description": "Max runs per day (budget or rate limit)."},
            "runs_left_today": {"type": "integer", "minimum": 0,
                                "description": "Runs still allowed before the next daily reset."},
            "reset_hour": {"type": "integer", "minimum": 0, "maximum": 23,
                           "description": "Local hour the daily cap resets (default 0, midnight)."},
            "timezone": {"type": "string",
                         "description": "IANA time zone for the answer, e.g. America/New_York."},
            "review_minutes": {"type": "number", "minimum": 0,
                               "description": "Extra time for review or tests after the runs."},
            "waiting_on": {"type": "string",
                           "description": "A step that waits on a person, e.g. 'you approve the PR'."},
        },
        "required": [],
    },
}

ETA_RECORD_RUN = {
    "name": "eta_record_run",
    "description": (
        "Record one finished run (how long it took and whether it succeeded) for a lane, so "
        "later ETAs use real numbers. Kanban tasks are recorded automatically; use this for "
        "other work such as cron jobs, CI runs or delegated agents."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lane": {"type": "string", "description": "Agent, assignee or queue name."},
            "duration_minutes": {"type": "number", "minimum": 0},
            "ok": {"type": "boolean", "description": "True if the run succeeded."},
        },
        "required": ["lane", "duration_minutes", "ok"],
    },
}

ETA_HISTORY = {
    "name": "eta_history",
    "description": (
        "Show recorded run history per lane: runs in the last 3h and 24h, success rate, typical "
        "and slow run length, and the current failure streak."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lane": {"type": "string", "description": "One lane; omit for all lanes."},
        },
    },
}

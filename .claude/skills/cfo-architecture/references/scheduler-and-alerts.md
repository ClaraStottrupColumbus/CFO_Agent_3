# Autonomy — the scheduler, tasks, rules and alerts

Governs `app/scheduler.py`, `app/tasks.py`, `app/rules.py` and `app/alerts.py`. Tested by
`tests/test_schedule_math.py`, `tests/test_rules.py` and `tests/test_alerts_dedup.py`.

`scheduler.py` is one daemon thread; each due task runs on its own worker, a per-task guard prevents
self-overlap, failures land in `last_status`/`last_error`. Schedule math (`tasks.compute_next_run`) is
a pure function — that is why it is tested. `TASK_TYPES = (data_refresh, driver_scan,
budget_revision, assumption_refresh, drift_scan, custom_prompt)`; `TASK_KIND` maps task type to
session kind explicitly (never `ttype.split("_")[0]`).

`rules.py` detects deterministically in pandas and **never calls the API**; only the narrative in
`alerts.py` does, and it falls back to the finding title when the network is down. The finding shape
`{rule_id, entity, period, metric_value, threshold, severity, title}` is `.format(**finding)`-ed into
`URGENCY_ANALYSIS_PROMPT` — renaming a key breaks prompt formatting at runtime, not at import. Alert
dedup is **windowed with magnitude bucketing**, not permanent-per-period, so a re-breach at a higher
magnitude re-fires.

There are **three** rules, and there were five. `driver_stale` and `scenario_ebitda_floor` are gone
because both fired on every evaluation and trained the CFO to mute the other three. The distinction
worth keeping is **state vs event**: nothing *happens* when a driver crosses its staleness limit, and
a scenario under the EBITDA floor stays under it until someone acts — so both re-fired the same
finding every window. Neither is lost as information: staleness is a badge, a summary count and a
card rule on `#/drivers`, and the floor's arithmetic (`rules._rerun_active_scenario`, still tested)
now feeds `main.budget_state` and the home screen headline. An alert should be something that
happened; anything that is merely true belongs on a screen.

Background agent runs take `reporting.AGENT_SEMAPHORE` (bounded at 2); interactive chat never
acquires it, so user requests can't queue behind an 03:00 market scan.

## See also

- `references/sessions.md` — `TASK_KIND` maps onto the session kinds, and `get_run_params` is
  injected into the scheduler rather than imported, which is also why `model_for_session` is pure.
- `references/api-and-gates.md` — startup report generation is gated on both the API key and
  `setup_complete()`.

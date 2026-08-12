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

## Idle burn — one scan, one owner, one cheap narrative

The concurrency cap bounds how many background turns run *at once*; it says nothing about how many
run *at all*. Three changes address that, and each reverses something this file previously described
as fine:

- **`data_refresh` no longer runs the drift scan.** It did, *and* the dedicated `drift_scan` task ran
  the same scan on the same hourly interval — so every finding was evaluated twice an hour, each scan
  spending up to `MAX_ANALYSES_PER_SCAN` full agent turns. `data/alerts.json` showed it plainly: 6
  alerts at 10:00 and 6 at 11:00 with nobody using the app. Refreshing data and reacting to it are
  two jobs; the task named for one must not quietly do both.
- **The seeded drift scan is six-hourly** (`DRIFT_SCAN_INTERVAL`), not hourly — it scans *simulated
  jittered* data, so hourly bought very little signal. `tasks._migrate` lifts an already-seeded
  install off the old default, because changing `default_tasks` alone is inert on every machine that
  has already run the app. It only touches a value that is exactly the old default and stamps the
  task, so a deliberate hourly choice is not silently overridden on the next load.
- **`alerts._narrative_params` narrows the narrative turn**: web research off, effort `medium`. One
  alert could otherwise fan out into 8 searches and 8 fetches of unbounded page content, unattended,
  three at a time — to caption a finding `rules.py` had already computed. Local tools stay on, so the
  figure is still verified, which is the verification `URGENCY_ANALYSIS_PROMPT` actually asks for
  (it names `driver_status` and `driver_sensitivity`, never the web). It must not mutate the caller's
  params — the scan reuses one dict across alerts.

Covered by `tests/test_idle_burn.py`.

## See also

- `references/sessions.md` — `TASK_KIND` maps onto the session kinds, and `get_run_params` is
  injected into the scheduler rather than imported, which is also why `model_for_session` is pure.
- `references/api-and-gates.md` — startup report generation is gated on both the API key and
  `setup_complete()`.

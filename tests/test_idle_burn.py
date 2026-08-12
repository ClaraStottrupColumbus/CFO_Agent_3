# The unattended half of the token bill.
#
# None of this is reachable from a smoke test: it is about what the app spends
# when nobody is using it. Two things had made that expensive — the same drift
# scan wired to two hourly tasks, and an alert narrative that ran the full
# web-research loop to caption a finding rules.py had already computed.

from app import alerts, tasks


# ---------- One scan, one owner ----------

def test_data_refresh_does_not_also_run_the_drift_scan():
    """Both jobs were hourly and both scanned, so every finding was evaluated
    twice an hour, each scan costing up to MAX_ANALYSES_PER_SCAN agent turns.
    The scan belongs to the task named for it."""
    import inspect
    body = inspect.getsource(tasks.execute_task)
    refresh = body.split('if ttype == "data_refresh":')[1].split("if ttype ==")[0]
    assert "run_drift_scan" not in refresh
    # ...but the dedicated task still owns it.
    assert "run_drift_scan" in body


def test_drift_scan_is_still_a_seeded_task():
    seeded = {t["type"] for t in tasks.default_tasks()}
    assert "drift_scan" in seeded


def test_the_seeded_drift_scan_is_six_hourly():
    scan = next(t for t in tasks.default_tasks() if t["type"] == "drift_scan")
    assert scan["schedule"]["interval_seconds"] == tasks.DRIFT_SCAN_INTERVAL
    assert tasks.DRIFT_SCAN_INTERVAL == 21_600


# ---------- Migrating an already-seeded install ----------
#
# default_tasks() only runs on a machine that has never seeded. Without the
# migration the new interval would be inert everywhere the app has already run.

def _stored(interval, ttype="drift_scan", **extra):
    return [{"id": "x", "type": ttype, "name": "Drift scan",
             "schedule": {"mode": "interval", "interval_seconds": interval}, **extra}]


def test_the_old_shipped_default_is_lifted():
    items, changed = tasks._migrate(_stored(3_600))
    assert changed
    assert items[0]["schedule"]["interval_seconds"] == tasks.DRIFT_SCAN_INTERVAL


def test_a_hand_picked_interval_is_left_alone():
    """Only a value that is EXACTLY the old default is touched — anything else
    is a choice someone made."""
    for chosen in (1_800, 7_200, 43_200):
        items, changed = tasks._migrate(_stored(chosen))
        assert not changed
        assert items[0]["schedule"]["interval_seconds"] == chosen


def test_migration_runs_once_and_does_not_fight_the_user():
    """Someone who deliberately sets hourly back afterwards must keep it — a
    setting that silently reverts on next load is worse than the old default."""
    items, _ = tasks._migrate(_stored(3_600))
    items[0]["schedule"]["interval_seconds"] = 3_600      # user picks hourly again
    items, changed = tasks._migrate(items)
    assert not changed
    assert items[0]["schedule"]["interval_seconds"] == 3_600


def test_other_task_types_are_untouched():
    items, changed = tasks._migrate(_stored(3_600, ttype="data_refresh"))
    assert not changed
    assert items[0]["schedule"]["interval_seconds"] == 3_600


def test_non_interval_schedules_are_untouched():
    stored = [{"id": "x", "type": "drift_scan",
               "schedule": {"mode": "daily", "time": "07:00"}}]
    items, changed = tasks._migrate(stored)
    assert not changed
    assert items[0]["schedule"] == {"mode": "daily", "time": "07:00"}


def test_a_task_with_no_schedule_does_not_raise():
    items, changed = tasks._migrate([{"id": "x", "type": "drift_scan"}])
    assert not changed and items


# ---------- The alert narrative runs on the cheap path ----------

def test_the_narrative_turn_drops_web_research():
    """One alert could fan out into 8 searches and 8 fetches of unbounded page
    content, unattended, three at a time. URGENCY_ANALYSIS_PROMPT asks for
    driver_status and driver_sensitivity — local tools — not the open web."""
    params = {"model": "claude-sonnet-5", "effort": "xhigh",
              "research": {"enabled": True, "max_searches": 8, "max_fetches": 8}}
    narrowed = alerts._narrative_params(params)
    assert narrowed["research"]["enabled"] is False
    assert narrowed["effort"] == "medium"


def test_narrowing_does_not_mutate_the_caller_s_params():
    """The scan reuses one params dict across up to MAX_ANALYSES_PER_SCAN
    alerts, and run_drift_scan's own turn shares it."""
    params = {"model": "claude-sonnet-5", "effort": "xhigh",
              "research": {"enabled": True, "max_searches": 8}}
    alerts._narrative_params(params)
    assert params["research"]["enabled"] is True
    assert params["effort"] == "xhigh"


def test_narrowing_keeps_the_general_model_and_survives_absent_research():
    narrowed = alerts._narrative_params({"model": "claude-sonnet-5"})
    assert narrowed["model"] == "claude-sonnet-5"
    assert narrowed["research"] == {"enabled": False}

# Windowed dedup with magnitude bucketing (§7).
#
# The reference dedups permanently on rule:entity:period. That is wrong here: a
# driver legitimately re-breaches — wheat crosses +10% in week 1 and +18% in
# week 3, and the escalation IS the news — so a permanent key swallows the
# second alert entirely.

import time

import pytest

from app import alerts


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """Point the alert store and dedup log at a temp dir, so tests never touch
    real runtime state."""
    monkeypatch.setattr(alerts, "ALERTS_FILE", tmp_path / "alerts.json")
    monkeypatch.setattr(alerts, "LOG_FILE", tmp_path / "alerts_log.jsonl")
    yield


def finding(rule_id="driver_moved_since_lock", entity="wheat", value=12.0,
            period="2026-12", severity="warning"):
    return {"rule_id": rule_id, "entity": entity, "period": period,
            "metric_value": value, "threshold": 10.0, "severity": severity,
            "title": f"{entity} moved {value}%", "context": {"unit": "EUR/t"}}


# ---------- The window ----------

def test_the_same_key_inside_the_window_is_suppressed():
    f = finding()
    alerts.create_alert(f, "narrative")
    assert alerts.recently_alerted(alerts.dedup_key(f), alerts.window_hours(f))


def test_the_same_key_outside_the_window_re_fires():
    f = finding()
    alerts.create_alert(f, "narrative")
    key = alerts.dedup_key(f)
    later = time.time() + alerts.window_hours(f) * 3600 + 60
    assert not alerts.recently_alerted(key, alerts.window_hours(f), now=later)


def test_driver_rules_use_a_short_window_and_period_rules_a_long_one():
    # Driver rules: 72h, no period. Period-bucketed rules: 720h, with period —
    # which reproduces the reference's permanent-per-period behaviour through
    # the same code path. One mechanism, two behaviours.
    assert alerts.window_hours(finding(rule_id="driver_moved_since_lock")) == 72
    assert alerts.window_hours(finding(rule_id="budget_line_variance")) == 720


def test_an_unknown_rule_id_gets_a_safe_default_window():
    assert alerts.window_hours({"rule_id": "not_a_rule"}) == 72


# ---------- Magnitude bucketing ----------

def test_the_same_breach_at_the_same_level_is_one_alert():
    a, b = finding(value=12.0), finding(value=13.4)   # same 5% bucket
    assert alerts.dedup_key(a) == alerts.dedup_key(b)


def test_an_escalation_re_fires_inside_the_window():
    """The load-bearing case: wheat at +12% then +18% is news twice."""
    first = finding(value=12.0)
    alerts.create_alert(first, "n")
    escalated = finding(value=18.0)
    assert alerts.dedup_key(escalated) != alerts.dedup_key(first)
    assert not alerts.recently_alerted(alerts.dedup_key(escalated),
                                       alerts.window_hours(escalated))


def test_a_de_escalation_also_gets_its_own_key():
    alerts.create_alert(finding(value=18.0), "n")
    calmer = finding(value=11.0)
    assert not alerts.recently_alerted(alerts.dedup_key(calmer), alerts.window_hours(calmer))


def test_bucket_floors_to_the_step():
    assert alerts.bucket(12.0, 5.0) == 2
    assert alerts.bucket(14.9, 5.0) == 2
    assert alerts.bucket(15.0, 5.0) == 3


def test_bucket_survives_junk_input():
    assert alerts.bucket("n/a", 5.0) == 0
    assert alerts.bucket(None, 5.0) == 0
    assert alerts.bucket(12.0, 0) == 0


def test_period_is_in_the_key_only_for_period_bucketed_rules():
    drift_a = finding(rule_id="driver_moved_since_lock", period="2026-11")
    drift_b = finding(rule_id="driver_moved_since_lock", period="2026-12")
    assert alerts.dedup_key(drift_a) == alerts.dedup_key(drift_b)

    var_a = finding(rule_id="budget_line_variance", entity="Poultry Feed", period="2026-11")
    var_b = finding(rule_id="budget_line_variance", entity="Poultry Feed", period="2026-12")
    assert alerts.dedup_key(var_a) != alerts.dedup_key(var_b)


def test_different_entities_never_collide():
    assert alerts.dedup_key(finding(entity="wheat")) != alerts.dedup_key(finding(entity="maize"))


def test_different_rules_never_collide():
    a = finding(rule_id="driver_moved_since_lock")
    b = finding(rule_id="unit_cost_breach")
    assert alerts.dedup_key(a) != alerts.dedup_key(b)


# ---------- Persistence and the context passthrough ----------

def test_context_survives_alert_creation():
    # The reference copies a fixed key set, so richer findings would silently
    # lose their unit, hedge coverage and product line.
    f = finding()
    f["context"] = {"unit": "EUR/t", "hedge_coverage": 0.45, "adverse": True}
    created = alerts.create_alert(f, "narrative")
    assert created["context"]["hedge_coverage"] == 0.45
    assert alerts.get_alert(created["id"])["context"]["adverse"] is True


def test_creating_an_alert_writes_the_dedup_log():
    f = finding()
    alerts.create_alert(f, "n")
    assert alerts.recently_alerted(alerts.dedup_key(f), 1)


def test_unread_summary_tracks_reads():
    a = alerts.create_alert(finding(entity="wheat"), "n")
    alerts.create_alert(finding(entity="maize"), "n")
    assert alerts.summary()["unread"] == 2
    alerts.mark_read(a["id"])
    assert alerts.summary()["unread"] == 1
    assert alerts.mark_all_read() == 1
    assert alerts.summary()["unread"] == 0


def test_delete_removes_the_alert():
    a = alerts.create_alert(finding(), "n")
    assert alerts.delete_alert(a["id"]) is True
    assert alerts.get_alert(a["id"]) is None
    assert alerts.delete_alert("nope") is False


# ---------- The scan: cost cap and the offline fallback ----------

def test_the_scan_caps_analyses_and_defers_the_rest(monkeypatch):
    findings = [finding(entity=f"driver_{i}", value=12.0 + i) for i in range(7)]
    monkeypatch.setattr(alerts.rules, "evaluate", lambda cfg=None: findings)
    monkeypatch.setattr(alerts, "_analyse", lambda f, p, prof=None: "narrative")

    detail = alerts.run_drift_scan({"model": "claude-opus-5"})
    assert f"{alerts.MAX_ANALYSES_PER_SCAN} new alerts" in detail
    assert "4 more findings deferred" in detail
    assert len(alerts.list_alerts()) == alerts.MAX_ANALYSES_PER_SCAN


def test_a_second_scan_picks_up_the_deferred_findings(monkeypatch):
    findings = [finding(entity=f"driver_{i}", value=12.0 + i) for i in range(5)]
    monkeypatch.setattr(alerts.rules, "evaluate", lambda cfg=None: findings)
    monkeypatch.setattr(alerts, "_analyse", lambda f, p, prof=None: "narrative")

    alerts.run_drift_scan({})
    alerts.run_drift_scan({})
    assert len(alerts.list_alerts()) == 5


def test_the_narrative_falls_back_to_the_finding_title(monkeypatch):
    # Alert creation must never depend on the network being reachable.
    monkeypatch.setattr(alerts.reporting, "run_agent_to_text",
                        lambda *a, **k: ("", "network unreachable"))
    text = alerts._analyse(finding(), {"model": "claude-opus-5"})
    assert finding()["title"] in text
    assert "network unreachable" in text


def test_the_fallback_also_covers_an_unexpected_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(alerts.reporting, "run_agent_to_text", boom)
    assert "kaboom" in alerts._analyse(finding(), {})


def test_a_successful_narrative_is_used_verbatim(monkeypatch):
    monkeypatch.setattr(alerts.reporting, "run_agent_to_text",
                        lambda *a, **k: ("Chicken meal is up 16%.", None))
    assert alerts._analyse(finding(), {}) == "Chicken meal is up 16%."

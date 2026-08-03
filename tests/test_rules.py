# One fires / doesn't-fire pair per rule at the threshold boundary, plus
# severity escalation. CSV fixtures throughout — viable because every loader
# prefers Parquet and falls back to CSV.

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app import drivers, rules, scenarios, tools


@pytest.fixture
def data(tmp_path, monkeypatch):
    """Redirect every loader at a temp directory and return a builder."""
    monkeypatch.setattr(tools, "DATA_DIR", tmp_path)
    monkeypatch.setattr(drivers, "DATA_DIR", tmp_path)
    monkeypatch.setattr(drivers, "DRIVERS_FILE", tmp_path / "drivers.parquet")
    monkeypatch.setattr(drivers, "PRICES_FILE", tmp_path / "driver_prices.parquet")
    monkeypatch.setattr(drivers, "PRICES_CSV", tmp_path / "driver_prices.csv")
    monkeypatch.setattr(drivers, "ASSUMPTIONS_FILE", tmp_path / "locked_assumptions.json")
    monkeypatch.setattr(scenarios, "SCENARIOS_FILE", tmp_path / "scenarios.json")
    return Builder(tmp_path)


class Builder:
    def __init__(self, root):
        self.root = root
        self.write_bva()
        self.write_drivers()
        self.write_prices()
        self.lock({})

    def _csv(self, name, rows):
        pd.DataFrame(rows).to_csv(self.root / f"{name}.csv", index=False)

    def write_bva(self, actual_revenue=1_000_000.0, budget_revenue=1_000_000.0,
                  actual_cogs=800_000.0, volume=2000.0):
        self._csv("budget_vs_actuals", [{
            "month": "2026-12", "product_line": "Poultry Feed", "region": "Iberia",
            "volume_tonnes_actual": volume, "volume_tonnes_budget": volume,
            "revenue_actual_eur": actual_revenue, "revenue_budget_eur": budget_revenue,
            "cogs_actual_eur": actual_cogs, "cogs_budget_eur": 780_000.0,
            "opex_actual_eur": 50_000.0, "opex_budget_eur": 50_000.0,
        }])

    def write_drivers(self, stale_after_days=7, adverse_direction="up"):
        self._csv("drivers", [{
            "driver_id": "wheat", "name": "Feed wheat", "category": "ingredient",
            "unit": "EUR/t", "quote_currency": "EUR", "baseline": 250.0,
            "hedge_coverage": 0.0, "adverse_direction": adverse_direction,
            "stale_after_days": stale_after_days, "search_hint": "",
        }])

    def write_prices(self, price=250.0, age_days=0.0):
        recorded = (datetime.now(timezone.utc)
                    - timedelta(days=age_days)).isoformat(timespec="seconds")
        self._csv("driver_prices", [{
            "month": "2026-12", "driver_id": "wheat", "price": price, "currency": "EUR",
            "source": "seed", "source_url": "", "revision": 0,
            "recorded_at": recorded, "note": "",
        }])

    def lock(self, assumptions):
        (self.root / "locked_assumptions.json").write_text(json.dumps({
            "locked_at": "2026-09-01T00:00:00+00:00", "scenario_id": None,
            "assumptions": {k: {"value": v} for k, v in assumptions.items()},
        }))

    def scenario(self, by_line=None, prices=None, pass_through=0.0):
        scenarios.save_scenario({
            "name": "locked", "assumptions": {},
            "price_pass_through": pass_through,
            "by_product_line": by_line or [],
            "driver_prices_used": prices or {},
            "totals": {}, "active": True,
        })


def fire(rule_id, findings):
    return [f for f in findings if f["rule_id"] == rule_id]


def only(rule_id, cfg_threshold=None):
    """Evaluate with just one rule enabled."""
    cfg = [dict(r, enabled=(r["id"] == rule_id)) for r in rules.DEFAULT_RULES]
    if cfg_threshold is not None:
        for r in cfg:
            if r["id"] == rule_id:
                r["threshold"] = cfg_threshold
    return rules.evaluate(cfg)


# ---------- 1. driver_moved_since_lock ----------

def test_drift_does_not_fire_at_the_threshold(data):
    data.lock({"wheat": 250.0})
    data.write_prices(price=275.0)          # exactly +10%
    assert fire("driver_moved_since_lock", only("driver_moved_since_lock")) == []


def test_drift_fires_just_past_the_threshold(data):
    data.lock({"wheat": 250.0})
    data.write_prices(price=275.5)          # +10.2%
    found = fire("driver_moved_since_lock", only("driver_moved_since_lock"))
    assert len(found) == 1
    assert found[0]["entity"] == "wheat"


def test_drift_fires_on_a_favourable_move_too(data):
    # A big favourable move is still news — the rule is on magnitude, and the
    # title says which way it cuts.
    data.lock({"wheat": 250.0})
    data.write_prices(price=200.0)          # -20%, favourable for a cost input
    found = fire("driver_moved_since_lock", only("driver_moved_since_lock"))
    assert len(found) == 1
    assert "favourable" in found[0]["title"]


def test_drift_severity_escalates_to_critical(data):
    """The direction fix: drift is higher-is-worse, so copying the reference's
    lower-is-worse _severity would mark every drift 'warning' forever."""
    data.lock({"wheat": 250.0})
    data.write_prices(price=280.0)          # +12%, under 1.5x threshold
    assert fire("driver_moved_since_lock", only("driver_moved_since_lock"))[0]["severity"] == "warning"

    data.write_prices(price=300.0)          # +20%, past 15%
    assert fire("driver_moved_since_lock", only("driver_moved_since_lock"))[0]["severity"] == "critical"


def test_drift_does_not_fire_with_nothing_locked(data):
    data.write_prices(price=400.0)
    assert fire("driver_moved_since_lock", only("driver_moved_since_lock")) == []


# ---------- 2. driver_stale ----------

def test_stale_does_not_fire_within_the_limit(data):
    data.write_drivers(stale_after_days=7)
    data.write_prices(age_days=6.0)
    assert fire("driver_stale", only("driver_stale")) == []


def test_stale_fires_past_the_limit(data):
    data.write_drivers(stale_after_days=7)
    data.write_prices(age_days=9.0)
    found = fire("driver_stale", only("driver_stale"))
    assert len(found) == 1
    assert "9 days ago" in found[0]["title"]


def test_staleness_is_per_driver_not_global(data):
    # FX goes stale in a day, wage inflation in a month.
    data.write_prices(age_days=9.0)
    data.write_drivers(stale_after_days=30)
    assert fire("driver_stale", only("driver_stale")) == []
    data.write_drivers(stale_after_days=3)
    assert len(fire("driver_stale", only("driver_stale"))) == 1


def test_a_never_verified_driver_fires(data):
    data.write_drivers()
    pd.DataFrame(columns=["month", "driver_id", "price", "currency", "source",
                          "source_url", "revision", "recorded_at", "note"]).to_csv(
        data.root / "driver_prices.csv", index=False)
    found = fire("driver_stale", only("driver_stale"))
    assert len(found) == 1
    assert "never been verified" in found[0]["title"]


# ---------- 3. budget_line_variance ----------

def test_variance_does_not_fire_at_the_threshold(data):
    data.write_bva(actual_revenue=950_000.0, budget_revenue=1_000_000.0)   # exactly -5%
    assert fire("budget_line_variance", only("budget_line_variance")) == []


def test_variance_fires_just_past_the_threshold(data):
    data.write_bva(actual_revenue=949_000.0, budget_revenue=1_000_000.0)   # -5.1%
    found = fire("budget_line_variance", only("budget_line_variance"))
    assert len(found) == 1
    assert found[0]["entity"] == "Poultry Feed"


def test_variance_severity_is_lower_is_worse(data):
    data.write_bva(actual_revenue=940_000.0, budget_revenue=1_000_000.0)   # -6%
    assert fire("budget_line_variance", only("budget_line_variance"))[0]["severity"] == "warning"
    data.write_bva(actual_revenue=900_000.0, budget_revenue=1_000_000.0)   # -10%, past -7.5%
    assert fire("budget_line_variance", only("budget_line_variance"))[0]["severity"] == "critical"


def test_variance_skips_a_line_with_no_budget(data):
    """A region that opened after the budget was set is missing data, not
    missing target — it must not be reported as a breach."""
    pd.DataFrame([{
        "month": "2026-12", "product_line": "Poultry Feed", "region": "Adriatic",
        "volume_tonnes_actual": 100.0, "volume_tonnes_budget": None,
        "revenue_actual_eur": 50_000.0, "revenue_budget_eur": None,
        "cogs_actual_eur": 40_000.0, "cogs_budget_eur": None,
        "opex_actual_eur": 2_000.0, "opex_budget_eur": None,
    }]).to_csv(data.root / "budget_vs_actuals.csv", index=False)
    assert fire("budget_line_variance", only("budget_line_variance")) == []


def test_beating_budget_never_fires(data):
    data.write_bva(actual_revenue=1_100_000.0, budget_revenue=1_000_000.0)
    assert fire("budget_line_variance", only("budget_line_variance")) == []


# ---------- 4. unit_cost_breach ----------

def test_unit_cost_does_not_fire_at_the_threshold(data):
    # Assumed €400/t; actual €416/t is exactly +4%.
    data.scenario(by_line=[{"product_line": "Poultry Feed", "volume_tonnes": 2000.0,
                            "cogs_eur": 800_000.0}])
    data.write_bva(actual_cogs=832_000.0, volume=2000.0)
    assert fire("unit_cost_breach", only("unit_cost_breach")) == []


def test_unit_cost_fires_just_past_the_threshold(data):
    data.scenario(by_line=[{"product_line": "Poultry Feed", "volume_tonnes": 2000.0,
                            "cogs_eur": 800_000.0}])
    data.write_bva(actual_cogs=840_000.0, volume=2000.0)     # €420/t, +5%
    found = fire("unit_cost_breach", only("unit_cost_breach"))
    assert len(found) == 1
    assert found[0]["context"]["assumed_unit_cost"] == pytest.approx(400.0)


def test_unit_cost_catches_inflation_that_volume_growth_hides(data):
    """The separation-of-drivers rule. Total COGS is up 25%, but so is volume —
    an amount-based rule sees growth, this one sees the cost breach."""
    data.scenario(by_line=[{"product_line": "Poultry Feed", "volume_tonnes": 2000.0,
                            "cogs_eur": 800_000.0}])
    data.write_bva(actual_cogs=1_000_000.0, volume=2350.0)   # €425.5/t, +6.4%
    assert len(fire("unit_cost_breach", only("unit_cost_breach"))) == 1


def test_unit_cost_does_not_fire_without_a_scenario(data):
    data.write_bva(actual_cogs=2_000_000.0, volume=2000.0)
    assert fire("unit_cost_breach", only("unit_cost_breach")) == []


# ---------- 5. scenario_ebitda_floor ----------

def _bom(root):
    pd.DataFrame([{"product_line": "Poultry Feed", "driver_id": "wheat",
                   "qty_per_tonne": 0.5, "unit": "EUR/t"}]).to_csv(
        root / "bill_of_materials.csv", index=False)


def _budget_year(root, revenue=12_000_000.0, cogs=9_000_000.0, opex=1_800_000.0):
    """A 2027 budget year with no actuals — revenue 12M, EBITDA 1.2M = 10%."""
    rows = [{"month": "2026-12", "product_line": "Poultry Feed", "region": "Iberia",
             "volume_tonnes_actual": 2000.0, "volume_tonnes_budget": 2000.0,
             "revenue_actual_eur": 1_000_000.0, "revenue_budget_eur": 1_000_000.0,
             "cogs_actual_eur": 800_000.0, "cogs_budget_eur": 800_000.0,
             "opex_actual_eur": 50_000.0, "opex_budget_eur": 50_000.0}]
    rows.append({"month": "2027-01", "product_line": "Poultry Feed", "region": "Iberia",
                 "volume_tonnes_actual": None, "volume_tonnes_budget": 20_000.0,
                 "revenue_actual_eur": None, "revenue_budget_eur": revenue,
                 "cogs_actual_eur": None, "cogs_budget_eur": cogs,
                 "opex_actual_eur": None, "opex_budget_eur": opex})
    pd.DataFrame(rows).to_csv(root / "budget_vs_actuals.csv", index=False)


def test_ebitda_floor_does_not_fire_when_prices_have_not_moved(data):
    _bom(data.root)
    _budget_year(data.root)
    data.scenario(prices={"wheat": 250.0})
    data.write_prices(price=250.0)
    assert fire("scenario_ebitda_floor", only("scenario_ebitda_floor")) == []


def test_ebitda_floor_fires_when_a_driver_move_pushes_margin_under(data):
    # Baseline margin is 10%. Wheat doubling adds 0.5 t/t x 20000 t x €250 =
    # €2.5M of COGS, which takes EBITDA from €1.2M to -€1.3M.
    _bom(data.root)
    _budget_year(data.root)
    data.scenario(prices={"wheat": 250.0})
    data.write_prices(price=500.0)
    found = fire("scenario_ebitda_floor", only("scenario_ebitda_floor"))
    assert len(found) == 1
    assert found[0]["metric_value"] < 8.0
    assert found[0]["context"]["delta_ebitda_eur"] < 0


def test_ebitda_floor_does_not_fire_without_a_scenario(data):
    _bom(data.root)
    _budget_year(data.root)
    assert fire("scenario_ebitda_floor", only("scenario_ebitda_floor")) == []


def test_a_falling_driver_lifts_the_projection(data):
    _bom(data.root)
    _budget_year(data.root)
    data.scenario(prices={"wheat": 250.0})
    data.write_prices(price=200.0)
    assert fire("scenario_ebitda_floor", only("scenario_ebitda_floor")) == []
    assert rules._rerun_active_scenario()["delta_ebitda_eur"] > 0


# ---------- Shape and configuration ----------

def test_every_finding_carries_the_reference_shape(data):
    data.lock({"wheat": 250.0})
    data.write_prices(price=400.0)
    for f in rules.evaluate():
        # URGENCY_ANALYSIS_PROMPT is .format(**finding) — a renamed key breaks
        # prompt formatting at runtime, not at import.
        assert {"rule_id", "entity", "period", "metric_value", "threshold",
                "severity", "title"} <= set(f)


def test_disabled_rules_never_fire(data):
    data.lock({"wheat": 250.0})
    data.write_prices(price=400.0)
    cfg = [dict(r, enabled=False) for r in rules.DEFAULT_RULES]
    assert rules.evaluate(cfg) == []


def test_thresholds_round_trip_through_save(tmp_path, monkeypatch):
    monkeypatch.setattr(rules, "RULES_FILE", tmp_path / "rules.json")
    saved = rules.save_rules([{"id": "driver_moved_since_lock", "threshold": 15.0,
                               "enabled": False}])
    stored = {r["id"]: r for r in saved}
    assert stored["driver_moved_since_lock"]["threshold"] == 15.0
    assert stored["driver_moved_since_lock"]["enabled"] is False
    # Unlisted rules keep their defaults, so a new rule appears after an upgrade.
    assert stored["driver_stale"]["enabled"] is True
    assert {r["id"] for r in rules.get_rules()} == set(rules.RULE_IDS)


def test_an_unknown_rule_id_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(rules, "RULES_FILE", tmp_path / "rules.json")
    with pytest.raises(ValueError, match="Unknown rule id"):
        rules.save_rules([{"id": "made_up", "threshold": 1.0}])


def test_evaluate_returns_empty_when_the_data_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "DATA_DIR", tmp_path)
    monkeypatch.setattr(drivers, "DRIVERS_FILE", tmp_path / "drivers.parquet")
    monkeypatch.setattr(drivers, "PRICES_FILE", tmp_path / "driver_prices.parquet")
    monkeypatch.setattr(drivers, "PRICES_CSV", tmp_path / "driver_prices.csv")
    assert rules.evaluate() == []

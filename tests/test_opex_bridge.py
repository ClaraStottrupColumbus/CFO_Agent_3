# The opex bridge is what finally makes opex_plan's driver_id column mean
# something: wage inflation reaches the budget through the same machinery as
# chicken meal instead of as a hand-typed number.
#
# Two properties matter more than the arithmetic. First, resolution order —
# override beats driver beats default, and getting that wrong silently ignores
# whichever the CFO actually stated. Second, the bridge returns a WEIGHTED RATE
# rather than a substitute total, because opex_plan is cut by cost centre while
# project_pnl's baseline opex is cut by product line and the two need not
# reconcile.

import pytest

from app.budget import opex_bridge, project_pnl

TOL = 1e-6

# One month; project_pnl passes twelve identical months and the weighted rate
# is scale-free, so one is enough to reason about.
OPEX_ROWS = [
    {"cost_centre": "Production", "driver_id": "wage_inflation", "amount_eur": 400_000.0},
    {"cost_centre": "Logistics", "driver_id": "road_freight", "amount_eur": 260_000.0},
    {"cost_centre": "G&A", "driver_id": "wage_inflation", "amount_eur": 140_000.0},
    {"cost_centre": "R&D", "driver_id": None, "amount_eur": 200_000.0},
]
TOTAL = 1_000_000.0


def centres(result):
    return {r["cost_centre"]: r for r in result["by_cost_centre"]}


# ---------- Resolution order ----------

def test_a_shocked_driver_moves_only_the_centres_mapped_to_it():
    res = opex_bridge(OPEX_ROWS, {"wage_inflation": 5.0}, {}, 0.0)
    by = centres(res)
    assert by["Production"]["delta_eur"] == pytest.approx(20_000.0, abs=TOL)
    assert by["G&A"]["delta_eur"] == pytest.approx(7_000.0, abs=TOL)
    assert by["Logistics"]["delta_eur"] == pytest.approx(0.0, abs=TOL)
    assert by["R&D"]["delta_eur"] == pytest.approx(0.0, abs=TOL)
    assert by["Production"]["basis"] == "driver"


def test_an_unmapped_centre_falls_back_to_the_default_rate():
    res = opex_bridge(OPEX_ROWS, {}, {}, 3.0)
    by = centres(res)
    assert by["R&D"]["basis"] == "default"
    assert by["R&D"]["delta_eur"] == pytest.approx(6_000.0, abs=TOL)
    # With nothing else stated, every centre moves at the default.
    assert res["weighted_pct"] == pytest.approx(3.0, abs=TOL)


def test_a_cost_centre_override_beats_its_driver():
    res = opex_bridge(OPEX_ROWS, {"wage_inflation": 5.0}, {"Production": 1.0}, 0.0)
    by = centres(res)
    assert by["Production"]["basis"] == "override"
    assert by["Production"]["delta_eur"] == pytest.approx(4_000.0, abs=TOL)
    # G&A is on the same driver and was not overridden.
    assert by["G&A"]["delta_eur"] == pytest.approx(7_000.0, abs=TOL)


def test_an_override_keyed_by_driver_id_reaches_every_centre_on_that_driver():
    res = opex_bridge(OPEX_ROWS, {"wage_inflation": 5.0}, {"wage_inflation": 10.0}, 0.0)
    by = centres(res)
    assert by["Production"]["delta_eur"] == pytest.approx(40_000.0, abs=TOL)
    assert by["G&A"]["delta_eur"] == pytest.approx(14_000.0, abs=TOL)
    assert by["Production"]["basis"] == "override"


def test_a_driver_override_still_loses_to_a_cost_centre_override():
    res = opex_bridge(OPEX_ROWS, {}, {"wage_inflation": 10.0, "G&A": 0.0}, 0.0)
    by = centres(res)
    assert by["Production"]["delta_eur"] == pytest.approx(40_000.0, abs=TOL)
    assert by["G&A"]["delta_eur"] == pytest.approx(0.0, abs=TOL)


# ---------- The weighted rate ----------

def test_the_weighted_rate_is_the_delta_over_the_base():
    res = opex_bridge(OPEX_ROWS, {"wage_inflation": 5.0}, {}, 0.0)
    assert res["base_eur"] == pytest.approx(TOTAL, abs=TOL)
    assert res["delta_eur"] == pytest.approx(27_000.0, abs=TOL)
    assert res["weighted_pct"] == pytest.approx(2.7, abs=TOL)


def test_monthly_rows_for_the_same_centre_collapse_into_one():
    twelve = [dict(r, amount_eur=r["amount_eur"] / 12.0) for r in OPEX_ROWS for _ in range(12)]
    once = opex_bridge(OPEX_ROWS, {"wage_inflation": 5.0}, {}, 0.0)
    many = opex_bridge(twelve, {"wage_inflation": 5.0}, {}, 0.0)
    assert len(many["by_cost_centre"]) == len(once["by_cost_centre"])
    assert many["weighted_pct"] == pytest.approx(once["weighted_pct"], abs=TOL)


def test_with_no_rows_the_weighted_rate_is_the_default():
    # A company with no opex plan still gets a scenario — on the old blanket
    # percentage, exactly as before the bridge existed.
    assert opex_bridge([], {"wage_inflation": 5.0}, {}, 3.0)["weighted_pct"] == pytest.approx(3.0)
    assert opex_bridge(None, {}, {}, 0.0)["by_cost_centre"] == []


# ---------- Wired through project_pnl ----------

BOM = [{"product_line": "Poultry", "driver_id": "wheat", "qty_per_tonne": 0.4}]
PRICES = {"wheat": 250.0, "wage_inflation": 1.0}
BASE = [{"month": "2027-01", "product_line": "Poultry", "volume_tonnes": 1000.0,
         "revenue_eur": 520_000.0, "cogs_eur": 380_000.0, "opex_eur": 60_000.0}]


def test_project_pnl_applies_the_weighted_rate_to_the_baseline_opex_level():
    # The bridge's own base (1.0M, by cost centre) is deliberately NOT the
    # baseline opex level (60k, by product line) — only the RATE crosses over.
    res = project_pnl(BASE, BOM, PRICES, {"drivers": {"wage_inflation": 5.0}},
                      opex_rows=OPEX_ROWS)
    assert res["opex_bridge"]["weighted_pct"] == pytest.approx(2.7, abs=TOL)
    assert res["totals"]["opex_eur"] == pytest.approx(60_000.0 * 1.027, abs=1e-4)


def test_a_wage_driver_outside_the_bill_of_materials_touches_opex_only():
    res = project_pnl(BASE, BOM, PRICES, {"drivers": {"wage_inflation": 5.0}},
                      opex_rows=OPEX_ROWS)
    assert res["totals"]["cogs_eur"] == pytest.approx(380_000.0, abs=1e-4)
    assert res["totals"]["revenue_eur"] == pytest.approx(520_000.0, abs=1e-4)
    assert res["totals"]["opex_eur"] > 60_000.0


def test_the_opex_block_overrides_reach_project_pnl():
    res = project_pnl(BASE, BOM, PRICES, {"opex": {"Production": 10.0}}, opex_rows=OPEX_ROWS)
    # 400k of 1.0M at +10% = +4.0% weighted.
    assert res["opex_bridge"]["weighted_pct"] == pytest.approx(4.0, abs=TOL)


def test_without_opex_rows_the_blanket_inflation_still_applies():
    res = project_pnl(BASE, BOM, PRICES, {}, opex_inflation_pct=3.0)
    assert res["totals"]["opex_eur"] == pytest.approx(60_000.0 * 1.03, abs=1e-4)


def test_the_opex_step_of_the_ebitda_bridge_is_the_negated_opex_move():
    res = project_pnl(BASE, BOM, PRICES, {"drivers": {"wage_inflation": 5.0}},
                      opex_rows=OPEX_ROWS)
    assert res["ebitda_bridge"]["opex_eur"] == pytest.approx(
        -(res["totals"]["opex_eur"] - 60_000.0), abs=1e-6)
    assert res["ebitda_bridge"]["check_residual"] == pytest.approx(0.0, abs=1e-6)

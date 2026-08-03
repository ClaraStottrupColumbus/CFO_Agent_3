# Exposure comes out of the bill of materials, so sensitivity is a fact about
# the data rather than a guessed elasticity. The breakeven solve is analytic —
# the test that matters is feeding the answer back in and landing on the floor.

import pytest

from app.budget import breakeven_shock, driver_exposure, sensitivity

TOL = 1e-6

BOM = [
    {"product_line": "Poultry", "driver_id": "chicken_meal", "qty_per_tonne": 0.22},
    {"product_line": "Poultry", "driver_id": "wheat", "qty_per_tonne": 0.40},
    {"product_line": "Swine", "driver_id": "chicken_meal", "qty_per_tonne": 0.05},
    {"product_line": "Swine", "driver_id": "wheat", "qty_per_tonne": 0.55},
]
VOLUMES = {"Poultry": 10_000.0, "Swine": 6_000.0}

BASELINE = {"revenue_eur": 20_000_000.0, "cogs_eur": 14_000_000.0, "opex_eur": 4_000_000.0}


# ---------- Exposure ----------

def test_exposure_is_sum_of_qty_times_volume_times_price():
    # chicken_meal: 0.22*10000 + 0.05*6000 = 2500 t, at €480/t
    assert driver_exposure(BOM, VOLUMES, 480.0, "chicken_meal") == pytest.approx(
        2500.0 * 480.0, abs=TOL)


def test_exposure_ignores_other_drivers():
    # wheat: 0.40*10000 + 0.55*6000 = 7300 t
    assert driver_exposure(BOM, VOLUMES, 250.0, "wheat") == pytest.approx(
        7300.0 * 250.0, abs=TOL)


def test_unknown_driver_has_zero_exposure():
    assert driver_exposure(BOM, VOLUMES, 100.0, "not_a_driver") == 0.0


def test_a_product_line_with_no_volume_contributes_nothing():
    assert driver_exposure(BOM, {"Poultry": 0.0, "Swine": 0.0}, 480.0,
                           "chicken_meal") == 0.0


# ---------- Sensitivity ----------

def test_ten_percent_shock_unhedged_moves_cogs_by_ten_percent_of_exposure():
    exposure = 1_200_000.0
    res = sensitivity(exposure, 10.0, hedge_coverage=0.0)
    assert res["delta_cogs_eur"] == pytest.approx(120_000.0, abs=TOL)


def test_fifty_percent_hedge_halves_the_cogs_impact():
    exposure = 1_200_000.0
    unhedged = sensitivity(exposure, 10.0, hedge_coverage=0.0)["delta_cogs_eur"]
    hedged = sensitivity(exposure, 10.0, hedge_coverage=0.5)["delta_cogs_eur"]
    assert hedged == pytest.approx(unhedged / 2.0, abs=TOL)


def test_full_hedge_neutralises_the_shock():
    assert sensitivity(1_200_000.0, 40.0, hedge_coverage=1.0)["delta_cogs_eur"] == pytest.approx(
        0.0, abs=TOL)


def test_pass_through_dampens_the_ebitda_hit_but_not_the_cogs_hit():
    full = sensitivity(1_000_000.0, 10.0, price_pass_through=0.0)
    half = sensitivity(1_000_000.0, 10.0, price_pass_through=0.5)
    assert half["delta_cogs_eur"] == pytest.approx(full["delta_cogs_eur"], abs=TOL)
    assert half["delta_ebitda_eur"] == pytest.approx(full["delta_ebitda_eur"] / 2.0, abs=TOL)
    # Recovering half the cost increase also lifts revenue.
    assert half["delta_revenue_eur"] == pytest.approx(50_000.0, abs=TOL)


def test_a_falling_cost_driver_raises_ebitda():
    res = sensitivity(1_000_000.0, -8.0, hedge_coverage=0.0, baseline=BASELINE)
    assert res["delta_cogs_eur"] < 0
    assert res["delta_ebitda_eur"] > 0
    assert res["projected_ebitda_eur"] > res["baseline_ebitda_eur"]


def test_baseline_margins_are_reported_when_a_baseline_is_given():
    res = sensitivity(1_000_000.0, 0.0, baseline=BASELINE)
    # (20M - 14M - 4M) / 20M = 10%
    assert res["baseline_ebitda_margin_pct"] == pytest.approx(10.0, abs=TOL)
    assert res["projected_ebitda_margin_pct"] == pytest.approx(10.0, abs=TOL)
    assert res["margin_delta_pp"] == pytest.approx(0.0, abs=TOL)


def test_hedge_and_pass_through_are_clamped_to_the_unit_interval():
    assert sensitivity(1_000.0, 10.0, hedge_coverage=5.0)["hedge_coverage"] == 1.0
    assert sensitivity(1_000.0, 10.0, hedge_coverage=-2.0)["hedge_coverage"] == 0.0
    assert sensitivity(1_000.0, 10.0, price_pass_through=9.0)["price_pass_through"] == 1.0


# ---------- Breakeven ----------

def test_breakeven_shock_fed_back_reproduces_the_floor():
    exposure = 3_000_000.0
    floor = 8.0
    shock = breakeven_shock(exposure, BASELINE, floor, hedge_coverage=0.15,
                            price_pass_through=0.30)
    assert shock is not None
    res = sensitivity(exposure, shock, hedge_coverage=0.15,
                      price_pass_through=0.30, baseline=BASELINE)
    assert res["projected_ebitda_margin_pct"] == pytest.approx(floor, abs=1e-6)


def test_breakeven_round_trips_with_no_hedge_and_no_pass_through():
    exposure = 2_500_000.0
    shock = breakeven_shock(exposure, BASELINE, 5.0)
    res = sensitivity(exposure, shock, baseline=BASELINE)
    assert res["projected_ebitda_margin_pct"] == pytest.approx(5.0, abs=1e-6)


def test_breakeven_is_positive_when_the_floor_is_below_todays_margin():
    # Today 10%; a floor of 8% must be reached by costs going UP.
    assert breakeven_shock(3_000_000.0, BASELINE, 8.0) > 0


def test_breakeven_is_negative_when_the_floor_is_above_todays_margin():
    # Today 10%; a 12% floor is only reachable if costs FALL.
    assert breakeven_shock(3_000_000.0, BASELINE, 12.0) < 0


def test_fully_hedged_driver_has_no_breakeven():
    assert breakeven_shock(3_000_000.0, BASELINE, 8.0, hedge_coverage=1.0) is None


def test_zero_exposure_has_no_breakeven():
    assert breakeven_shock(0.0, BASELINE, 8.0) is None

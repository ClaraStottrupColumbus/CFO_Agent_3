# The scenario engine is what the monthly revision revises against, so its
# invariants are structural: an empty assumption set changes nothing, the
# monthly rows reconcile to the annual total, and EBITDA is always
# revenue - COGS - opex on every single row.

import pytest

from app.budget import project_pnl

TOL = 1e-6

BOM = [
    {"product_line": "Poultry", "driver_id": "chicken_meal", "qty_per_tonne": 0.22},
    {"product_line": "Poultry", "driver_id": "wheat", "qty_per_tonne": 0.40},
    {"product_line": "Swine", "driver_id": "wheat", "qty_per_tonne": 0.55},
]
PRICES = {"chicken_meal": 480.0, "wheat": 250.0}


def baseline_rows(months=12):
    out = []
    for i in range(months):
        month = f"2027-{i + 1:02d}"
        out.append({"month": month, "product_line": "Poultry", "volume_tonnes": 1000.0,
                    "revenue_eur": 520_000.0, "cogs_eur": 380_000.0, "opex_eur": 60_000.0})
        out.append({"month": month, "product_line": "Swine", "volume_tonnes": 700.0,
                    "revenue_eur": 300_000.0, "cogs_eur": 225_000.0, "opex_eur": 40_000.0})
    return out


# ---------- The identity assumption ----------

def test_all_zero_assumptions_reproduce_the_baseline_exactly():
    base = baseline_rows()
    res = project_pnl(base, BOM, PRICES, {})
    for original, projected in zip(base, res["rows"]):
        assert projected["revenue_eur"] == pytest.approx(original["revenue_eur"], abs=TOL)
        assert projected["cogs_eur"] == pytest.approx(original["cogs_eur"], abs=TOL)
        assert projected["opex_eur"] == pytest.approx(original["opex_eur"], abs=TOL)


def test_explicit_zero_percent_assumptions_also_reproduce_the_baseline():
    res = project_pnl(baseline_rows(), BOM, PRICES,
                      {"chicken_meal": 0.0, "wheat": 0.0})
    assert res["totals"]["cogs_eur"] == pytest.approx(
        project_pnl(baseline_rows(), BOM, PRICES, {})["totals"]["cogs_eur"], abs=TOL)


def test_a_driver_not_in_any_bill_of_materials_changes_nothing():
    res = project_pnl(baseline_rows(), BOM, PRICES, {"sea_freight": 40.0})
    assert res["totals"]["cogs_eur"] == pytest.approx(
        sum(r["cogs_eur"] for r in baseline_rows()), abs=TOL)


# ---------- Structural invariants ----------

def test_ebitda_identity_holds_on_every_row():
    res = project_pnl(baseline_rows(), BOM, PRICES,
                      {"chicken_meal": 28.0, "wheat": -8.0},
                      price_pass_through=0.35, opex_inflation_pct=3.0)
    for row in res["rows"]:
        assert row["ebitda_eur"] == pytest.approx(
            row["revenue_eur"] - row["cogs_eur"] - row["opex_eur"], abs=TOL)
        assert row["gross_margin_eur"] == pytest.approx(
            row["revenue_eur"] - row["cogs_eur"], abs=TOL)


def test_monthly_rows_sum_to_the_annual_total():
    res = project_pnl(baseline_rows(), BOM, PRICES,
                      {"chicken_meal": 28.0, "wheat": -8.0}, price_pass_through=0.35)
    for field in ("revenue_eur", "cogs_eur", "opex_eur", "ebitda_eur", "volume_tonnes"):
        assert sum(m[field] for m in res["by_month"]) == pytest.approx(
            res["totals"][field], abs=1e-4)


def test_product_line_totals_sum_to_the_annual_total():
    res = project_pnl(baseline_rows(), BOM, PRICES, {"chicken_meal": 12.0})
    for field in ("revenue_eur", "cogs_eur", "ebitda_eur"):
        assert sum(p[field] for p in res["by_product_line"]) == pytest.approx(
            res["totals"][field], abs=1e-4)


def test_by_month_has_one_entry_per_month_in_order():
    res = project_pnl(baseline_rows(months=12), BOM, PRICES, {})
    assert [m["month"] for m in res["by_month"]] == [f"2027-{i:02d}" for i in range(1, 13)]


# ---------- Directional behaviour ----------

def test_a_cost_driver_rising_raises_cogs_by_the_bom_weighted_amount():
    res = project_pnl(baseline_rows(months=1), BOM, PRICES, {"chicken_meal": 10.0})
    # Only Poultry uses chicken meal: 0.22 t/t * 1000 t * 480 EUR/t * 10%
    expected = 0.10 * 0.22 * 1000.0 * 480.0
    assert res["driver_impact_eur"]["chicken_meal"] == pytest.approx(expected, abs=TOL)
    assert res["totals"]["cogs_eur"] == pytest.approx(
        380_000.0 + 225_000.0 + expected, abs=TOL)


def test_a_falling_driver_lowers_cogs_and_lifts_ebitda():
    flat = project_pnl(baseline_rows(), BOM, PRICES, {})
    cheaper = project_pnl(baseline_rows(), BOM, PRICES, {"wheat": -8.0})
    assert cheaper["totals"]["cogs_eur"] < flat["totals"]["cogs_eur"]
    assert cheaper["totals"]["ebitda_eur"] > flat["totals"]["ebitda_eur"]


def test_hedging_a_driver_dampens_its_impact():
    unhedged = project_pnl(baseline_rows(), BOM, PRICES, {"chicken_meal": 20.0})
    hedged = project_pnl(baseline_rows(), BOM, PRICES, {"chicken_meal": 20.0},
                         hedges={"chicken_meal": 0.5})
    assert hedged["driver_impact_eur"]["chicken_meal"] == pytest.approx(
        unhedged["driver_impact_eur"]["chicken_meal"] / 2.0, abs=TOL)


def test_pass_through_lifts_revenue_alongside_cogs():
    res = project_pnl(baseline_rows(), BOM, PRICES, {"chicken_meal": 20.0},
                      price_pass_through=0.5)
    base_revenue = sum(r["revenue_eur"] for r in baseline_rows())
    d_cogs = res["totals"]["cogs_eur"] - sum(r["cogs_eur"] for r in baseline_rows())
    assert res["totals"]["revenue_eur"] == pytest.approx(
        base_revenue + 0.5 * d_cogs, abs=1e-4)


def test_opex_inflation_applies_to_opex_only():
    res = project_pnl(baseline_rows(), BOM, PRICES, {}, opex_inflation_pct=3.0)
    base_opex = sum(r["opex_eur"] for r in baseline_rows())
    assert res["totals"]["opex_eur"] == pytest.approx(base_opex * 1.03, abs=1e-4)
    assert res["totals"]["revenue_eur"] == pytest.approx(
        sum(r["revenue_eur"] for r in baseline_rows()), abs=TOL)


# ---------- Degenerate input ----------

def test_empty_baseline_yields_empty_rows_and_null_margin():
    res = project_pnl([], BOM, PRICES, {"wheat": 10.0})
    assert res["rows"] == []
    assert res["totals"]["revenue_eur"] == 0
    assert res["totals"]["ebitda_margin_pct"] is None


def test_margin_percentages_are_computed_from_projected_revenue():
    res = project_pnl(baseline_rows(months=1), BOM, PRICES, {})
    t = res["totals"]
    assert t["ebitda_margin_pct"] == pytest.approx(
        t["ebitda_eur"] / t["revenue_eur"] * 100.0, abs=TOL)
    assert t["gross_margin_pct"] == pytest.approx(
        t["gross_margin_eur"] / t["revenue_eur"] * 100.0, abs=TOL)

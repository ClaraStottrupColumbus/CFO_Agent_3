# The scenario engine is what the monthly revision revises against, so its
# invariants are structural: an empty assumption set changes nothing, the
# monthly rows reconcile to the annual total, and EBITDA is always
# revenue - COGS - opex on every single row.

import pytest

from app.budget import normalise_assumptions, project_pnl, repriced_drivers

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


# ---------- The assumption vocabulary ----------
#
# Every scenario written before the four blocks existed is a flat dict, so the
# lift has to be exact in both directions or saved scenarios read as empty.

def test_a_flat_dict_lifts_into_the_drivers_block():
    assert normalise_assumptions({"chicken_meal": 12.0, "wheat": -8.0}) == {
        "drivers": {"chicken_meal": 12.0, "wheat": -8.0},
        "volume": {}, "price": {}, "opex": {}}


def test_a_blocked_dict_passes_through_and_fills_missing_blocks():
    assert normalise_assumptions({"volume": {"Poultry": 10}}) == {
        "drivers": {}, "volume": {"Poultry": 10.0}, "price": {}, "opex": {}}


def test_a_driver_named_after_a_block_still_reads_as_a_driver():
    # The discriminator is the VALUE being a dict, not the key being a block
    # name — so naming a driver "price" cannot flip the shape detection.
    assert normalise_assumptions({"price": 5.0}) == {
        "drivers": {"price": 5.0}, "volume": {}, "price": {}, "opex": {}}


def test_junk_is_dropped_rather_than_raised_on():
    # normalise runs on READ; a half-written file must not take the page down.
    assert normalise_assumptions({"wheat": "twelve", "chicken_meal": 3}) == {
        "drivers": {"chicken_meal": 3.0}, "volume": {}, "price": {}, "opex": {}}
    assert normalise_assumptions(None)["drivers"] == {}


def test_a_flat_dict_and_its_blocked_form_project_identically():
    flat = project_pnl(baseline_rows(), BOM, PRICES, {"chicken_meal": 15.0})
    blocked = project_pnl(baseline_rows(), BOM, PRICES,
                          {"drivers": {"chicken_meal": 15.0}})
    assert blocked["totals"] == flat["totals"]


# ---------- Which locked prices moved under a recompute ----------
#
# Recomputing a scenario re-reads today's locked assumptions, so its EBITDA can
# move with not one percentage changed. This is what lets the edit form say so
# instead of handing back a number the CFO cannot account for.

def test_repriced_names_only_the_drivers_whose_locked_price_moved():
    rows = repriced_drivers({"chicken_meal": 400.0, "wheat": 200.0},
                            {"chicken_meal": 424.0, "wheat": 200.0})
    assert [r["driver_id"] for r in rows] == ["chicken_meal"]
    assert rows[0]["from"] == 400.0 and rows[0]["to"] == 424.0
    assert rows[0]["pct"] == pytest.approx(6.0)


def test_repriced_is_empty_when_nothing_moved_or_there_is_no_earlier_record():
    assert repriced_drivers({"wheat": 200.0}, {"wheat": 200.0}) == []
    assert repriced_drivers({}, {"wheat": 200.0}) == []


def test_a_driver_absent_from_the_earlier_computation_is_not_a_move():
    # A driver added to the catalog since is not something that "moved" — there is
    # no earlier price to have moved from.
    rows = repriced_drivers({"wheat": 200.0}, {"wheat": 200.0, "electricity": 90.0})
    assert rows == []


def test_repriced_reports_none_pct_rather_than_dividing_by_a_zero_baseline():
    rows = repriced_drivers({"wheat": 0.0}, {"wheat": 200.0})
    assert rows[0]["pct"] is None
    assert rows[0]["to"] == 200.0


def test_a_move_too_small_to_print_is_not_reported():
    # The real case this guards: a price stored as a spot fallback and re-read off
    # a rounded locked value differs in the fifth decimal, and every USD-quoted
    # driver inherits that from the FX rate. Reported literally, an edit that
    # changed nothing names fourteen drivers at "+0.0%".
    rows = repriced_drivers({"chicken_meal": 581.882233672406, "eur_usd": 1.0495444330932484},
                            {"chicken_meal": 581.9069080514531, "eur_usd": 1.0495})
    assert rows == []
    # Just over the threshold is still reported.
    assert repriced_drivers({"wheat": 200.0}, {"wheat": 200.2})[0]["pct"] == pytest.approx(0.1)


def test_a_price_that_appeared_from_zero_is_always_reported():
    assert repriced_drivers({"wheat": 0.0}, {"wheat": 0.0}) == []
    assert repriced_drivers({"wheat": 0.0}, {"wheat": 0.0001})[0]["pct"] is None


def test_repriced_is_ordered_by_the_size_of_the_move():
    rows = repriced_drivers({"a": 100.0, "b": 100.0},
                            {"a": 110.0, "b": 150.0})
    assert [r["driver_id"] for r in rows] == ["b", "a"]


def test_unparseable_prices_are_skipped_rather_than_raised_on():
    assert repriced_drivers({"wheat": None}, {"wheat": 200.0}) == []
    assert repriced_drivers({"wheat": 200.0}, {"wheat": "twelve"}) == []


# ---------- Volume ----------

def test_volume_scales_revenue_and_cogs_but_never_opex():
    base = baseline_rows()
    res = project_pnl(base, BOM, PRICES, {"volume": {"*": 10.0}})
    assert res["totals"]["revenue_eur"] == pytest.approx(
        sum(r["revenue_eur"] for r in base) * 1.10, abs=1e-4)
    assert res["totals"]["cogs_eur"] == pytest.approx(
        sum(r["cogs_eur"] for r in base) * 1.10, abs=1e-4)
    assert res["totals"]["opex_eur"] == pytest.approx(
        sum(r["opex_eur"] for r in base), abs=1e-4)
    assert res["totals"]["volume_tonnes"] == pytest.approx(
        sum(r["volume_tonnes"] for r in base) * 1.10, abs=1e-4)


def test_volume_alone_leaves_gross_margin_percent_unchanged():
    flat = project_pnl(baseline_rows(), BOM, PRICES, {})
    grown = project_pnl(baseline_rows(), BOM, PRICES, {"volume": {"*": 10.0}})
    assert grown["totals"]["gross_margin_pct"] == pytest.approx(
        flat["totals"]["gross_margin_pct"], abs=TOL)


def test_a_volume_assumption_applies_only_to_the_named_line():
    base = baseline_rows(months=1)
    res = project_pnl(base, BOM, PRICES, {"volume": {"Swine": -20.0}})
    by_line = {r["product_line"]: r for r in res["by_product_line"]}
    assert by_line["Poultry"]["volume_tonnes"] == pytest.approx(1000.0, abs=TOL)
    assert by_line["Swine"]["volume_tonnes"] == pytest.approx(560.0, abs=TOL)


def test_a_named_line_beats_the_wildcard():
    res = project_pnl(baseline_rows(months=1), BOM, PRICES,
                      {"volume": {"*": 10.0, "Swine": 0.0}})
    by_line = {r["product_line"]: r for r in res["by_product_line"]}
    assert by_line["Poultry"]["volume_tonnes"] == pytest.approx(1100.0, abs=TOL)
    assert by_line["Swine"]["volume_tonnes"] == pytest.approx(700.0, abs=TOL)


def test_driver_cost_is_computed_on_the_post_volume_tonnage():
    # 10% more Poultry tonnes consume 10% more chicken meal, so the driver
    # shock has to land on 1100 t, not on the original 1000 t.
    res = project_pnl(baseline_rows(months=1), BOM, PRICES,
                      {"drivers": {"chicken_meal": 10.0}, "volume": {"Poultry": 10.0}})
    assert res["driver_impact_eur"]["chicken_meal"] == pytest.approx(
        0.10 * 0.22 * 1100.0 * 480.0, abs=TOL)


# ---------- Price, and its interaction with pass-through ----------

def test_price_moves_revenue_only():
    base = baseline_rows()
    res = project_pnl(base, BOM, PRICES, {"price": {"*": 4.0}})
    assert res["totals"]["revenue_eur"] == pytest.approx(
        sum(r["revenue_eur"] for r in base) * 1.04, abs=1e-4)
    assert res["totals"]["cogs_eur"] == pytest.approx(
        sum(r["cogs_eur"] for r in base), abs=1e-4)
    assert res["totals"]["opex_eur"] == pytest.approx(
        sum(r["opex_eur"] for r in base), abs=1e-4)


def test_an_explicit_price_suppresses_pass_through_on_that_line_only():
    # THE double-counting test. tau and an explicit price are the same lever;
    # applying both would overstate recovery and quietly inflate EBITDA.
    base = baseline_rows(months=1)
    shock = {"drivers": {"wheat": 20.0}, "price": {"Poultry": 0.0}}
    res = project_pnl(base, BOM, PRICES, shock, price_pass_through=0.5)

    rows = {r["product_line"]: r for r in res["rows"]}
    # Poultry priced deliberately at +0%: revenue is untouched despite the shock.
    assert rows["Poultry"]["revenue_eur"] == pytest.approx(520_000.0, abs=1e-4)
    # Swine was not priced, so tau still recovers half of its wheat delta.
    swine_d_cogs = 0.20 * 0.55 * 700.0 * 250.0
    assert rows["Swine"]["revenue_eur"] == pytest.approx(
        300_000.0 + 0.5 * swine_d_cogs, abs=1e-4)


def test_a_wildcard_price_counts_as_explicit_everywhere():
    base = baseline_rows(months=1)
    shock = {"drivers": {"wheat": 20.0}, "price": {"*": 3.0}}
    res = project_pnl(base, BOM, PRICES, shock, price_pass_through=0.5)
    for row in res["rows"]:
        original = next(r for r in base if r["product_line"] == row["product_line"])
        # Exactly +3%, with no tau on top.
        assert row["revenue_eur"] == pytest.approx(original["revenue_eur"] * 1.03, abs=1e-4)


def test_pass_through_still_applies_when_no_price_is_stated():
    res = project_pnl(baseline_rows(), BOM, PRICES, {"drivers": {"chicken_meal": 20.0}},
                      price_pass_through=0.5)
    base_revenue = sum(r["revenue_eur"] for r in baseline_rows())
    d_cogs = res["totals"]["cogs_eur"] - sum(r["cogs_eur"] for r in baseline_rows())
    assert res["totals"]["revenue_eur"] == pytest.approx(base_revenue + 0.5 * d_cogs, abs=1e-4)


# ---------- The EBITDA bridge ----------

def test_the_ebitda_bridge_sums_to_the_total_ebitda_move():
    # Same additivity discipline as variance_decomposition's check_residual: a
    # bridge that does not close is a bridge nobody can defend to a board.
    res = project_pnl(baseline_rows(), BOM, PRICES,
                      {"drivers": {"chicken_meal": 28.0, "wheat": -8.0},
                       "volume": {"Poultry": 12.0, "Swine": -5.0},
                       "price": {"Poultry": 4.0}},
                      price_pass_through=0.35, opex_inflation_pct=3.0)
    b = res["ebitda_bridge"]
    assert b["check_residual"] == pytest.approx(0.0, abs=1e-6)
    assert (b["volume_eur"] + b["price_eur"] + b["opex_eur"]
            + sum(b["drivers_eur"].values())) == pytest.approx(
        b["projected_ebitda_eur"] - b["baseline_ebitda_eur"], abs=1e-6)


def test_the_bridge_closes_for_a_volume_only_scenario():
    res = project_pnl(baseline_rows(), BOM, PRICES, {"volume": {"*": -15.0}})
    b = res["ebitda_bridge"]
    assert b["price_eur"] == pytest.approx(0.0, abs=TOL)
    assert b["drivers_eur"] == {}
    assert b["volume_eur"] == pytest.approx(
        b["projected_ebitda_eur"] - b["baseline_ebitda_eur"], abs=1e-6)


def test_an_all_zero_scenario_has_a_bridge_of_zeroes():
    b = project_pnl(baseline_rows(), BOM, PRICES, {})["ebitda_bridge"]
    assert b["total_eur"] == pytest.approx(0.0, abs=TOL)
    assert b["baseline_ebitda_eur"] == pytest.approx(b["projected_ebitda_eur"], abs=1e-6)


def test_a_driver_bridge_entry_is_net_of_the_pass_through_it_triggers():
    res = project_pnl(baseline_rows(months=1), BOM, PRICES,
                      {"chicken_meal": 10.0}, price_pass_through=0.4)
    gross = res["driver_impact_eur"]["chicken_meal"]
    assert res["ebitda_bridge"]["drivers_eur"]["chicken_meal"] == pytest.approx(
        -gross * 0.6, abs=TOL)


def test_volume_delta_tonnes_is_reported_against_the_baseline():
    res = project_pnl(baseline_rows(months=1), BOM, PRICES, {"volume": {"Poultry": 10.0}})
    assert res["volume_delta_tonnes"] == pytest.approx(100.0, abs=TOL)

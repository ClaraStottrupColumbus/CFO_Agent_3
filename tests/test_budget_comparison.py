# Comparing any two budgets side by side on the Budget page.
#
# Two halves, and the split is the same one the module itself keeps:
#
#   * The engine — `derive()`'s explicit-amount extension and `_pct_or_none`. Pure,
#     fixture-free, no pandas, no data directory. The FIRST test asserts the
#     extension is byte-for-byte inert when the new keys are absent, exactly as
#     test_forward_curve.py asserts the flat-curve no-op first: that property is
#     what makes this one engine rather than a fork of it.
#   * The orchestration — snapshots off real datasets. CSV fixtures in tmp_path
#     with monkeypatch.setattr on the module path constants, per the repo
#     convention, plus scenarios.SCENARIOS_FILE which test_budget_plan.py's
#     fixture does not need to patch and this one does.
#
# The load-bearing test in here is
# `test_the_configured_pair_reproduces_the_persisted_plan`: the comparison is a
# SECOND orchestration over the same leaves as `materialise_from_datasets`, and
# that test is what stops the two drifting apart.

import json

import pytest

from app import budgetplan as bp


# --------------------------------------------------------------------------
# The engine — pure, no fixtures
# --------------------------------------------------------------------------

def _plan(variables, revenue=1000.0, **baseline):
    base = {"current_year": 2026, "budget_year": 2027, "revenue": revenue}
    base.update(baseline)
    return {"company": {"currency": "EUR", "name": "Test"},
            "baseline": base, "variables": variables}


def test_omitting_the_explicit_amounts_is_byte_for_byte_what_it_was():
    """The pure-extension property, asserted first.

    Every stored config goes down this path — `validate` never emits
    `next_amount` or `revenue_next` — so if this moves, the whole Budget page
    moved and the comparison was not an extension at all.
    """
    d = bp.derive(_plan(
        [{"id": "a", "label": "A", "current_amount": 100.0,
          "expected_change_pct": 10.0, "include": True},
         {"id": "b", "label": "B", "current_amount": 400.0,
          "expected_change_pct": -5.0, "include": True}],
        revenue_change_pct=5.0))
    a, b = {r["id"]: r for r in d["ranked"]}["a"], {r["id"]: r for r in d["ranked"]}["b"]
    assert a["next_amount"] == pytest.approx(110.0)
    assert a["expected_change_pct"] == pytest.approx(10.0)
    assert a["direction"] == "up"
    assert b["next_amount"] == pytest.approx(380.0)
    assert b["direction"] == "down"
    assert d["totals"]["revenue_next"] == pytest.approx(1050.0)
    assert d["totals"]["revenue_change_pct"] == pytest.approx(5.0)
    # Every row reports full coverage, so nothing on the existing page changes.
    assert {r["coverage"] for r in d["ranked"]} == {"both"}


def test_an_explicit_next_amount_wins_over_the_percentage():
    d = bp.derive(_plan([{"id": "a", "label": "A", "current_amount": 100.0,
                          "expected_change_pct": 10.0, "next_amount": 250.0,
                          "include": True}]))
    row = d["ranked"][0]
    assert row["next_amount"] == pytest.approx(250.0)
    assert row["delta"] == pytest.approx(150.0)
    # Derived from the two amounts, not the stale 10% that was also supplied.
    assert row["expected_change_pct"] == pytest.approx(150.0)


def test_an_explicit_revenue_next_is_used_verbatim():
    """Not round-tripped through a percentage: pct -> multiply -> back is lossy,
    and the headline revenue should be the other budget's own sum."""
    d = bp.derive(_plan([{"id": "a", "label": "A", "current_amount": 10.0,
                          "next_amount": 10.0, "include": True}],
                        revenue=78_037_908.67, revenue_next=82_466_018.09))
    assert d["totals"]["revenue_next"] == pytest.approx(82_466_018.09, abs=1e-9)
    assert d["totals"]["revenue_delta"] == pytest.approx(4_428_109.42, abs=1e-6)


def test_a_line_the_baseline_does_not_carry_has_an_undefined_percentage():
    """None, never 0. "This line is new" and "this line is flat" are different
    claims, and 0% asserts the second next to a delta of millions — the same rule
    as driver_status.forward_12m and cash.cash_conversion_pct."""
    d = bp.derive(_plan([{"id": "new", "label": "New", "current_amount": 0.0,
                          "next_amount": 1000.0, "coverage": "right", "include": True}]))
    row = d["ranked"][0]
    assert row["expected_change_pct"] is None
    assert row["delta"] == pytest.approx(1000.0)
    assert row["direction"] == "up"          # NOT "flat"
    assert row["coverage"] == "right"


def test_a_line_that_disappears_is_minus_one_hundred_not_undefined():
    """The line existed and was removed — that percentage is defined."""
    d = bp.derive(_plan([{"id": "gone", "label": "Gone", "current_amount": 500.0,
                          "next_amount": 0.0, "coverage": "left", "include": True}]))
    row = d["ranked"][0]
    assert row["expected_change_pct"] == pytest.approx(-100.0)
    assert row["direction"] == "down"


def test_pct_or_none_boundaries():
    assert bp._pct_or_none(0.0, 0.0) == 0.0            # nothing on either side
    assert bp._pct_or_none(0.0, 500.0) is None         # undefined
    assert bp._pct_or_none(500.0, 0.0) == pytest.approx(-100.0)
    assert bp._pct_or_none(200.0, 250.0) == pytest.approx(25.0)


def test_pct_text_keeps_the_float_output_stable():
    """The brief and the templated read format percentages through this, so the
    float branch has to stay byte-identical to the f-strings it replaced."""
    assert bp._pct_text(10.0) == "+10.0%"
    assert bp._pct_text(-5.25) == "-5.2%"
    assert bp._pct_text(None) == "new"


def test_the_same_budget_on_both_sides_is_all_zero_and_does_not_crash():
    d = bp.derive(_plan([{"id": "a", "label": "A", "current_amount": 100.0,
                          "next_amount": 100.0, "include": True},
                         {"id": "b", "label": "B", "current_amount": 400.0,
                          "next_amount": 400.0, "include": True}],
                        revenue=1000.0, revenue_next=1000.0))
    assert d["totals"]["cost_delta"] == pytest.approx(0.0)
    assert d["totals"]["revenue_delta"] == pytest.approx(0.0)
    assert d["totals"]["margin_delta_pp"] == pytest.approx(0.0)
    assert {r["direction"] for r in d["ranked"]} == {"flat"}
    # total_abs_delta is 0, so impact_share must not divide by it.
    assert {r["impact_share"] for r in d["ranked"]} == {0.0}
    # Ranking still resolves to a stable, contiguous order.
    assert [r["rank"] for r in d["ranked"]] == [1, 2]


def test_templated_narrative_survives_an_undefined_percentage():
    plan = _plan([{"id": "new", "label": "New line", "current_amount": 0.0,
                   "next_amount": 1000.0, "coverage": "right", "include": True}])
    text = bp.templated_narrative(plan)
    assert "New line" in text
    assert "new" in text                       # not "+0.0%"


def test_the_brief_survives_an_undefined_percentage():
    plan = _plan([{"id": "new", "label": "New line", "current_amount": 0.0,
                   "next_amount": 1000.0, "coverage": "right", "include": True}])
    assert "New line" in bp.brief(plan)


def test_parse_selection_round_trips_and_rejects_garbage():
    assert bp.parse_selection("budget:2026") == ("budget", "2026")
    assert bp.parse_selection("actual:2026") == ("actual", "2026")
    # A scenario id is opaque and must survive a colon inside it intact.
    assert bp.parse_selection("scenario:ab:cd") == ("scenario", "ab:cd")
    assert bp.parse_selection("BUDGET:2026") == ("budget", "2026")
    for bad in ("", None, "2026", "year:2026", "budget:", ":2026", "nope:1"):
        assert bp.parse_selection(bad) is None


def test_comparison_narrative_says_so_when_both_sides_match():
    derived = bp.derive(_plan([{"id": "a", "label": "A", "current_amount": 100.0,
                                "next_amount": 100.0, "include": True}]))
    side = {"label": "2027 budget (as booked)", "short_label": "2027 budget"}
    text = bp.comparison_narrative(derived, side, side, True)
    assert "Both sides are" in text
    assert "zero" in text


# --------------------------------------------------------------------------
# The orchestration — CSV fixtures in tmp_path
# --------------------------------------------------------------------------

import pandas as pd

from app import drivers as drivers_mod
from app import scenarios as scenarios_mod
from app import tools as tools_mod


@pytest.fixture
def datasets(tmp_path, monkeypatch):
    """A miniature company with THREE budget years and two regions.

    Two shapes here are the point rather than filler: `Adriatic` has 2027 figures
    and no 2026 budget at all (the real dataset's "the data isn't there" hook, in
    miniature), and the `G&A` cost centre is absent from 2026's opex plan, which is
    the only way to exercise a cost line that exists on one side only — the seeded
    data never does, because every centre runs in every year.
    """
    monkeypatch.setattr(tools_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bp, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bp, "PLAN_FILE", tmp_path / "budget_plan.json")
    monkeypatch.setattr(drivers_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(drivers_mod, "DRIVERS_FILE", tmp_path / "drivers.parquet")
    monkeypatch.setattr(drivers_mod, "PRICES_FILE", tmp_path / "driver_prices.parquet")
    monkeypatch.setattr(drivers_mod, "PRICES_CSV", tmp_path / "driver_prices.csv")
    monkeypatch.setattr(drivers_mod, "FORWARDS_FILE", tmp_path / "driver_forwards.parquet")
    monkeypatch.setattr(drivers_mod, "FORWARDS_CSV", tmp_path / "driver_forwards.csv")
    monkeypatch.setattr(drivers_mod, "ASSUMPTIONS_FILE", tmp_path / "locked_assumptions.json")
    monkeypatch.setattr(scenarios_mod, "SCENARIOS_FILE", tmp_path / "scenarios.json")

    def csv(name, rows):
        pd.DataFrame(rows).to_csv(tmp_path / f"{name}.csv", index=False)

    def row(month, region, vol_a, vol_b, rev_a, rev_b, cogs_a, cogs_b, opex_a, opex_b):
        return {"month": month, "product_line": "Feed", "region": region,
                "volume_tonnes_actual": vol_a, "volume_tonnes_budget": vol_b,
                "revenue_actual_eur": rev_a, "revenue_budget_eur": rev_b,
                "cogs_actual_eur": cogs_a, "cogs_budget_eur": cogs_b,
                "opex_actual_eur": opex_a, "opex_budget_eur": opex_b}

    csv("budget_vs_actuals", [
        # 2025 — closed, budget and actual.
        row("2025-06", "Iberia", 450.0, 440.0, 440_000.0, 430_000.0,
            270_000.0, 264_000.0, 54_000.0, 52_000.0),
        # 2026 — closed. Adriatic has actuals and NO budget.
        row("2026-06", "Iberia", 500.0, 500.0, 500_000.0, 500_000.0,
            300_000.0, 300_000.0, 60_000.0, 60_000.0),
        row("2026-06", "Adriatic", 100.0, None, 95_000.0, None,
            60_000.0, None, 11_000.0, None),
        # 2027 — the plan year: budget only, and Adriatic is budgeted this time.
        row("2027-06", "Iberia", None, 600.0, None, 630_000.0,
            None, 380_000.0, None, 66_000.0),
        row("2027-06", "Adriatic", None, 150.0, None, 160_000.0,
            None, 96_000.0, None, 17_000.0),
    ])
    csv("bill_of_materials", [
        {"product_line": "Feed", "driver_id": "wheat", "qty_per_tonne": 0.4, "unit": "EUR/t"},
    ])
    csv("drivers", [
        {"driver_id": "wheat", "name": "Feed wheat", "category": "ingredient",
         "unit": "EUR/t", "quote_currency": "EUR", "baseline": 250.0,
         "hedge_coverage": 0.0, "adverse_direction": "up", "stale_after_days": 7,
         "search_hint": ""},
        {"driver_id": "wage_inflation", "name": "Wage inflation", "category": "labour",
         "unit": "%/yr", "quote_currency": "EUR", "baseline": 3.0,
         "hedge_coverage": 0.0, "adverse_direction": "up", "stale_after_days": 30,
         "search_hint": ""},
    ])
    csv("driver_prices", [
        {"month": "2025-06", "driver_id": "wheat", "price": 240.0, "currency": "EUR",
         "source": "seed", "source_url": "https://exchange.example/wheat", "revision": 0,
         "recorded_at": "2025-06-01T00:00:00+00:00", "note": ""},
        {"month": "2026-06", "driver_id": "wheat", "price": 250.0, "currency": "EUR",
         "source": "seed", "source_url": "https://exchange.example/wheat", "revision": 0,
         "recorded_at": "2026-06-01T00:00:00+00:00", "note": ""},
        {"month": "2026-06", "driver_id": "wage_inflation", "price": 3.0, "currency": "EUR",
         "source": "seed", "source_url": "", "revision": 0,
         "recorded_at": "2026-06-01T00:00:00+00:00", "note": ""},
    ])
    csv("opex_plan", [
        # No G&A in 2026: the cost line exists on the 2027 side only.
        {"month": "2026-06", "cost_centre": "Production", "amount_eur": 30_000.0,
         "amount_budget_eur": 30_000.0, "headcount": 10, "driver_id": "wage_inflation"},
        {"month": "2027-06", "cost_centre": "Production", "amount_eur": 33_000.0,
         "amount_budget_eur": 33_000.0, "headcount": 10, "driver_id": "wage_inflation"},
        {"month": "2027-06", "cost_centre": "G&A", "amount_eur": 11_000.0,
         "amount_budget_eur": 11_000.0, "headcount": 4, "driver_id": "wage_inflation"},
    ])
    drivers_mod.lock_assumptions({"wheat": {"value": 260.0, "unit": "EUR/t",
                                            "source_url": "https://exchange.example/wheat"}})
    return tmp_path


def _scenario(**over):
    """A stored scenario whose projection is internally consistent, so a snapshot
    built from it can be checked against its own totals."""
    fields = {
        "name": "Forward case", "baseline": "budget_2027", "basis": "forward",
        "assumptions": {"drivers": {"wheat": 5.0}},
        "totals": {"volume_tonnes": 750.0, "revenue_eur": 790_000.0,
                   "cogs_eur": 476_000.0, "opex_eur": 83_000.0},
        "by_month": [{"month": "2027-06", "revenue_eur": 790_000.0}],
        "by_product_line": [{"product_line": "Feed", "volume_tonnes": 750.0,
                             "revenue_eur": 790_000.0, "cogs_eur": 476_000.0,
                             "opex_eur": 83_000.0}],
        "driver_impact_eur": {"wheat": 3_900.0},
        "driver_prices_used": {"wheat": 260.0},
        "opex_bridge": {"by_cost_centre": [
            {"cost_centre": "Production", "driver_id": "wage_inflation",
             "basis": "driver", "pct": 3.0, "base_eur": 33_000.0, "delta_eur": 990.0},
            {"cost_centre": "G&A", "driver_id": "wage_inflation",
             "basis": "driver", "pct": 3.0, "base_eur": 11_000.0, "delta_eur": 330.0}]},
        "active": True,
    }
    fields.update(over)
    return scenarios_mod.save_scenario(fields)


def test_options_enumerate_every_budget_year_actual_year_and_scenario(datasets):
    _scenario()
    options = bp.comparison_options()
    keys = [o["key"] for o in options["budgets"]]
    assert "budget:2027" in keys and "budget:2026" in keys and "budget:2025" in keys
    # 2027 is budget-only, so it is not an outturn.
    assert "actual:2026" in keys and "actual:2025" in keys
    assert "actual:2027" not in keys
    assert any(k.startswith("scenario:") for k in keys)
    assert {o["group"] for o in options["budgets"]} == {
        "Budget years", "Actuals", "Scenarios"}


def test_the_defaults_are_the_prior_year_budget_and_the_active_scenario(datasets):
    stored = _scenario()
    options = bp.comparison_options()
    assert options["defaults"]["left"] == "budget:2026"       # plan year minus one
    assert options["defaults"]["right"] == f"scenario:{stored['id']}"
    assert options["fallback"] is None


def test_no_scenario_at_all_falls_back_to_the_plan_budget_and_says_so(datasets):
    options = bp.comparison_options()
    assert options["defaults"]["right"] == "budget:2027"
    assert options["fallback"]["field"] == "right"
    assert "no scenarios" in options["fallback"]["reason"].lower()


def test_an_unflagged_scenario_is_reported_as_a_fallback_not_as_active(datasets):
    """`scenarios.get_active()` silently falls back to the most recently updated
    record. A dropdown that labelled that one "active" would assert something
    false, so the flag comes off the record and the substitution is stated."""
    stored = _scenario(active=False)
    # save_scenario makes the first scenario active, so clear the flag on disk.
    raw = json.loads((datasets / "scenarios.json").read_text())
    raw["scenarios"][0]["active"] = False
    (datasets / "scenarios.json").write_text(json.dumps(raw))

    options = bp.comparison_options()
    entry = next(o for o in options["budgets"] if o["key"] == f"scenario:{stored['id']}")
    assert entry["active"] is False
    assert options["defaults"]["right"] == f"scenario:{stored['id']}"
    assert "flagged active" in options["fallback"]["reason"]
    assert "most recently updated" in options["fallback"]["reason"]


def test_a_budget_year_snapshot_reconciles_to_the_pnl(datasets):
    ctx = bp._comparison_context()
    snap = bp._year_snapshot("budget", "2027", ctx)
    assert not isinstance(snap, str)
    lines = sum(line["amount"] for line in snap["lines"].values())
    assert lines == pytest.approx(snap["cogs"] + snap["opex"], abs=1e-6)


def test_an_actuals_snapshot_reconciles_to_the_pnl_too(datasets):
    ctx = bp._comparison_context()
    snap = bp._year_snapshot("actual", "2026", ctx)
    assert not isinstance(snap, str)
    lines = sum(line["amount"] for line in snap["lines"].values())
    assert lines == pytest.approx(snap["cogs"] + snap["opex"], abs=1e-6)


def test_the_two_year_kinds_price_their_drivers_differently(datasets):
    """A booked budget is valued at the LOCKED set; an outturn at what was actually
    paid. Without that split the two kinds would be indistinguishable, and the
    price story — the only thing a budget-to-budget pair cannot show, because a
    budget year has no lock of its own — would have nowhere to live.
    """
    ctx = bp._comparison_context()
    budget = bp._year_snapshot("budget", "2026", ctx)
    actual = bp._year_snapshot("actual", "2026", ctx)

    # The volumes differ because Adriatic has 2026 actuals and no 2026 budget:
    # 500 t budgeted against 600 t outturned. That is the coverage gap, not noise.
    assert budget["volume"] == pytest.approx(500.0)
    assert actual["volume"] == pytest.approx(600.0)
    assert budget["regions"] == ["Iberia"]
    assert actual["regions"] == ["Adriatic", "Iberia"]

    # 260 locked vs 250 mean paid — the prices, not just the tonnage, differ.
    assert budget["lines"]["wheat"]["amount"] == pytest.approx(500.0 * 0.4 * 260.0)
    assert actual["lines"]["wheat"]["amount"] == pytest.approx(600.0 * 0.4 * 250.0)
    assert bp.BUDGET_PRICE_NOTE in budget["notes"]
    assert bp.ACTUAL_PRICE_NOTE in actual["notes"]


def test_a_scenario_snapshot_reconciles_to_its_stored_totals(datasets):
    """The load-bearing property of the scenario side.

    Driver spend is `driver_exposure(…, locked) + driver_impact_eur`, which is
    project_pnl's own arithmetic — hedge coverage and forward curve included —
    rather than a re-derivation of it. If this drifts, the hero's margin stops
    being the real margin and nothing below it is defensible.
    """
    stored = _scenario()
    ctx = bp._comparison_context()
    snap = bp._scenario_snapshot(stored, ctx)
    assert not isinstance(snap, str)
    lines = sum(line["amount"] for line in snap["lines"].values())
    assert lines == pytest.approx(snap["cogs"] + snap["opex"], abs=1e-6)
    # The driver line carries the impact, not just the locked valuation.
    assert snap["lines"]["wheat"]["amount"] == pytest.approx(750.0 * 0.4 * 260.0 + 3_900.0)
    assert snap["basis"] == "forward"


def test_the_configured_pair_reproduces_the_persisted_plan(datasets):
    """The regression guard for "keep existing functionality working".

    `materialise_from_datasets` and the comparison are two orchestrations over the
    same leaves, and `actual:<closed year>` against `budget:<plan year>` is exactly
    the pair the first one freezes. Every line both can express must agree.

    They diverge on ONE line, and the divergence is a pre-existing limitation of
    the persisted model rather than a disagreement about arithmetic: a stored
    variable is "this year's amount × a percentage", so a cost line that starts in
    the plan year has current_amount 0, `_pct_change(0, X)` is 0, and its money
    silently vanishes from the stored cost base. The comparison states amounts
    outright and therefore carries it. That is asserted below rather than papered
    over — see test_a_cost_centre_missing_from_one_side_is_named_not_collapsed.
    """
    saved = bp.rebuild_from_datasets({"name": "Test", "currency": "EUR"})
    assert not isinstance(saved, str)
    stored = bp.derive(saved)
    result = bp.compare("actual:2026", "budget:2027")
    assert result["available"] is True
    fresh = result["derived"]

    assert {r["id"] for r in fresh["ranked"]} == {r["id"] for r in stored["ranked"]}
    # The closed side is expressible either way, so it must match exactly.
    for field in ("revenue_current", "cost_current", "revenue_next"):
        assert fresh["totals"][field] == pytest.approx(stored["totals"][field], rel=1e-6)

    sr = {r["id"]: r for r in stored["ranked"]}
    nr = {r["id"]: r for r in fresh["ranked"]}
    # Every line the stored plan CAN express agrees on both amounts.
    shared = [k for k in sr if sr[k]["current_amount"] > 0]
    assert shared
    for k in shared:
        assert nr[k]["current_amount"] == pytest.approx(sr[k]["current_amount"], rel=1e-6)
        assert nr[k]["next_amount"] == pytest.approx(sr[k]["next_amount"], rel=1e-4)

    # And the whole difference in the plan-year cost base is the lines the stored
    # model cannot express — nothing else.
    unrepresentable = sum(nr[k]["next_amount"] for k in nr if sr[k]["current_amount"] <= 0)
    assert unrepresentable > 0
    assert fresh["totals"]["cost_next"] == pytest.approx(
        stored["totals"]["cost_next"] + unrepresentable, rel=1e-6)


def test_a_cost_centre_missing_from_one_side_is_named_not_collapsed(datasets):
    """G&A runs in 2027 and not in 2026, so it exists on one side only. The line
    must appear once, be flagged, and report an undefined percentage rather than a
    fabricated one."""
    result = bp.compare("budget:2026", "budget:2027")
    assert result["available"] is True
    rows = {r["id"]: r for r in result["derived"]["ranked"]}
    ga = next(r for k, r in rows.items() if k.startswith("opex_g"))
    assert result["only_in"][ga["id"]] == "right"
    assert ga["coverage"] == "right"
    assert ga["current_amount"] == pytest.approx(0.0)
    assert ga["next_amount"] > 0
    assert ga["expected_change_pct"] is None
    assert ga["direction"] == "up"


def test_a_region_without_a_budget_is_quantified_not_merely_mentioned(datasets):
    """Adriatic has 2027 figures and no 2026 budget, so part of the "growth" is a
    market the baseline never covered. Naming it is not enough — the share is what
    stops the number reading as organic growth."""
    result = bp.compare("budget:2026", "budget:2027")
    joined = " ".join(result["notes"])
    assert "Adriatic" in joined
    assert "of the revenue step" in joined
    assert "Adriatic" in result["narrative"]


def test_the_same_selection_on_both_sides_is_flagged_and_zero(datasets):
    result = bp.compare("budget:2027", "budget:2027")
    assert result["available"] is True
    assert result["same"] is True
    assert result["derived"]["totals"]["cost_delta"] == pytest.approx(0.0)
    assert result["only_in"] == {}
    assert "Both sides are" in result["narrative"]
    assert any("same budget" in n for n in result["notes"])


def test_the_cash_strip_gets_a_scenario_id_only_from_a_scenario(datasets):
    stored = _scenario()
    from_scenario = bp.compare("budget:2026", f"scenario:{stored['id']}")
    assert from_scenario["cash"]["scenario_id"] == stored["id"]

    from_year = bp.compare("budget:2026", "budget:2027")
    assert from_year["cash"]["scenario_id"] is None
    assert "projected from a scenario" in from_year["cash"]["reason"]


def test_a_period_mismatch_is_stated(datasets, monkeypatch):
    """Two budgets covering different numbers of months are compared on their own
    full periods, and the asymmetry is said out loud."""
    stored = _scenario(by_month=[{"month": "2027-06"}, {"month": "2027-07"}])
    result = bp.compare("budget:2026", f"scenario:{stored['id']}")
    joined = " ".join(result["notes"])
    assert "covers 1 months against 2" in joined or "covers 1 month" in joined


def test_an_unknown_selection_enumerates_the_valid_ones(datasets):
    result = bp.compare("budget:1999", "budget:2027")
    assert result["available"] is False
    assert result["field"] == "left"
    assert "2027" in result["error"] and "2026" in result["error"]
    # The options travel even on a failure, so the select can recover.
    assert result["options"]
    assert result["defaults"]


def test_a_malformed_selection_names_the_three_kinds(datasets):
    result = bp.compare("year:2026", "budget:2027")
    assert result["available"] is False
    for kind in ("budget:YYYY", "actual:YYYY", "scenario:"):
        assert kind in result["error"]


def test_a_deleted_scenario_id_is_teachable_and_lists_what_is_left(datasets):
    stored = _scenario()
    scenarios_mod.delete_scenario(stored["id"])
    result = bp.compare("budget:2026", f"scenario:{stored['id']}")
    assert result["available"] is False
    assert result["field"] == "right"
    assert "deleted in another tab" in result["error"]


def test_a_scenario_with_no_projection_refuses_teachably(datasets):
    stored = _scenario(totals={}, by_product_line=[])
    result = bp.compare("budget:2026", f"scenario:{stored['id']}")
    assert result["available"] is False
    assert "no stored projection" in result["error"]


def test_a_missing_dataset_is_a_state_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(tools_mod, "DATA_DIR", tmp_path)
    result = bp.compare("budget:2026", "budget:2027")
    assert result["available"] is False
    assert result["options"] == []
    assert "nothing to compare" in result["error"]


def test_the_comparison_never_goes_through_the_validator(datasets):
    """`validate()` clamps budget_year to current_year + 1 and requires integer
    years, so a non-consecutive pair — or a pair labelled by scenario name — would
    be truncated or rejected by it. The overlay is never persisted, and this test
    is what stops someone routing it through save_config later.
    """
    calls = []
    monkeypatch_target = bp.save_config
    try:
        bp.save_config = lambda payload: calls.append(payload)      # noqa: F811
        result = bp.compare("budget:2025", "budget:2027")
    finally:
        bp.save_config = monkeypatch_target
    assert result["available"] is True
    assert calls == []
    # A three-year gap survives, and the labels are budgets rather than years.
    assert result["derived"]["totals"]["current_year"] == "2025 budget (as booked)"
    assert result["derived"]["totals"]["budget_year"] == "2027 budget (as booked)"


def test_the_stored_plan_still_derives_with_no_comparison_involved(datasets):
    """The persisted path is untouched: no coverage flags, no undefined
    percentages, every figure a float."""
    saved = bp.rebuild_from_datasets({"name": "Test", "currency": "EUR"})
    d = bp.derive(saved)
    assert {r["coverage"] for r in d["ranked"]} == {"both"}
    assert all(isinstance(r["expected_change_pct"], float) for r in d["ranked"])

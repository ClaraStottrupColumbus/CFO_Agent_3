# Phase 5 — cash and working capital.
#
# The phase adds the one thing a budget can be entirely right about and still
# kill a company: cash. Four properties are what make the projection worth
# quoting, and they are what this file pins.
#
#   1. **The days are measured, not typed.** working_capital_days reads DSO/DIO/
#      DPO off the company's own balances, the same discipline as
#      driver_exposure reading qty-per-tonne off the bill of materials. A day
#      count somebody invented is an assumption with no source.
#   2. **It closes.** Σ free cash flow == closing cash − opening cash, and the
#      year's working-capital swing == the closing balances less the opening
#      ones. A cash plan that does not reconcile to a balance-sheet movement is
#      one nobody can defend, which is the same argument `check_residual`
#      carries in the variance and EBITDA bridges.
#   3. **The sign convention holds.** Receivables and inventory rising consume
#      cash; payables rising release it. Get one sign wrong and the trough moves
#      the wrong way, which is the failure that actually costs money.
#   4. **Nothing is invented.** With no tax rate stated, no tax is deducted and
#      the projection says so; with no opening balance sheet, the first month
#      shows no step and the projection says so.
#
# The tool-level tests at the bottom use CSV fixtures at a tmp_path, per the
# repo convention — they cover the wiring (scenario → days → capex → overrides)
# where the interesting bugs live rather than the arithmetic above.

import json

import pandas as pd
import pytest

from app import cash

# One flat year: constant trading, so anything that moves is the machinery
# moving rather than the business.
FLAT = [{"month": f"2027-{m:02d}", "revenue_eur": 1_000_000.0,
         "cogs_eur": 700_000.0, "opex_eur": 150_000.0} for m in range(1, 13)]

DAYS = {"dso_days": 45.0, "dio_days": 30.0, "dpo_days": 40.0}


def project(rows=None, **kwargs):
    return cash.project_cashflow(rows if rows is not None else FLAT,
                                 **{**DAYS, **kwargs})


# --------------------------------------------------------------------------
# 1. Measuring the days
# --------------------------------------------------------------------------

def history(months=12, *, receivables=1_500_000.0, inventory=800_000.0,
            payables=900_000.0, revenue=1_000_000.0, cogs=800_000.0):
    return [{"month": f"2026-{m:02d}", "revenue_eur": revenue, "cogs_eur": cogs,
             "receivables_eur": receivables, "inventory_eur": inventory,
             "payables_eur": payables, "cash_eur": 2_000_000.0}
            for m in range(1, months + 1)]


def test_days_are_measured_off_the_balances():
    """DSO = mean(receivables) × Σdays / Σrevenue — checked against the figure
    computed by hand, so a refactor that changes the definition fails here."""
    result = cash.working_capital_days(history())
    assert result["months_used"] == 12
    assert result["days_in_window"] == 365            # 2026 is not a leap year
    assert result["dso_days"] == pytest.approx(1_500_000.0 * 365 / 12_000_000.0)
    assert result["dio_days"] == pytest.approx(800_000.0 * 365 / 9_600_000.0)
    assert result["dpo_days"] == pytest.approx(900_000.0 * 365 / 9_600_000.0)
    assert result["basis"] == "measured"


def test_the_cash_conversion_cycle_is_dso_plus_dio_minus_dpo():
    result = cash.working_capital_days(history())
    assert result["cash_conversion_cycle_days"] == pytest.approx(
        result["dso_days"] + result["dio_days"] - result["dpo_days"])


def test_a_trailing_window_uses_only_the_last_n_months():
    """A CFO quotes a trailing window; one month's balance over one month's
    revenue is mostly noise about when the invoices went out."""
    rows = history(6)
    rows += [{"month": f"2026-{m:02d}", "revenue_eur": 1_000_000.0, "cogs_eur": 800_000.0,
              "receivables_eur": 3_000_000.0, "inventory_eur": 800_000.0,
              "payables_eur": 900_000.0} for m in range(7, 13)]
    whole = cash.working_capital_days(rows)
    recent = cash.working_capital_days(rows, months=6)
    assert recent["months_used"] == 6
    assert recent["from_month"] == "2026-07"
    # The last six months carry double the receivables, so the window measures
    # them alone (184 days of H2) rather than averaging the whole year down.
    assert recent["dso_days"] == pytest.approx(3_000_000.0 * 184 / 6_000_000.0)
    assert recent["dso_days"] > whole["dso_days"]


def test_no_history_is_an_error_not_an_exception():
    result = cash.working_capital_days([])
    assert "error" in result and "cannot be measured" in result["error"]


def test_a_window_with_no_trading_is_an_error():
    rows = [{"month": "2026-01", "revenue_eur": 0.0, "cogs_eur": 0.0,
             "receivables_eur": 10.0, "inventory_eur": 10.0, "payables_eur": 10.0}]
    assert "error" in cash.working_capital_days(rows)


def test_duplicate_months_sum_flows_but_do_not_double_the_balance():
    """Balances are stocks. Two rows for the same month must not read as twice
    the receivables — that would halve the measured DSO on an un-aggregated
    frame and nothing downstream would notice."""
    rows = history(1) + history(1)
    result = cash.working_capital_days(rows)
    assert result["months_used"] == 1
    assert result["receivables_avg_eur"] == 1_500_000.0
    assert result["revenue_eur"] == 2_000_000.0


# --------------------------------------------------------------------------
# 2. The projection closes
# --------------------------------------------------------------------------

def test_free_cash_flow_sums_to_the_change_in_cash():
    result = project(opening_cash_eur=2_000_000.0,
                     capex_by_month={"2027-03": 500_000.0}, tax_rate_pct=25.0)
    t = result["totals"]
    assert t["check_residual"] == pytest.approx(0.0, abs=1e-6)
    assert t["closing_cash_eur"] - t["opening_cash_eur"] == pytest.approx(
        t["free_cash_flow_eur"])


def test_every_month_reconciles_to_its_own_components():
    result = project(opening_cash_eur=1_000_000.0,
                     capex_by_month={"2027-06": 250_000.0}, tax_rate_pct=22.0)
    for row in result["months"]:
        assert row["free_cash_flow_eur"] == pytest.approx(
            row["ebitda_eur"] - row["working_capital_change_eur"]
            - row["tax_eur"] - row["capex_eur"])
        assert row["closing_cash_eur"] == pytest.approx(
            row["opening_cash_eur"] + row["free_cash_flow_eur"])


def test_the_working_capital_swing_equals_the_balance_sheet_movement():
    """The invariant that makes the swing defensible: the year's cash absorption
    is exactly the closing balances less the opening ones, so it can always be
    pointed at a balance-sheet movement rather than at a modelling artefact."""
    rows = [dict(r, revenue_eur=r["revenue_eur"] * (1 + i * 0.02),
                 cogs_eur=r["cogs_eur"] * (1 + i * 0.03))
            for i, r in enumerate(FLAT)]
    result = project(rows, opening_balances={"receivables": 1_200_000.0,
                                             "inventory": 600_000.0,
                                             "payables": 800_000.0})
    opening, closing = result["opening_balances"], result["closing_balances"]
    expected = ((closing["receivables"] - opening["receivables"])
                + (closing["inventory"] - opening["inventory"])
                - (closing["payables"] - opening["payables"]))
    assert result["totals"]["working_capital_change_eur"] == pytest.approx(expected)


def test_ebitda_is_taken_from_the_row_when_present():
    """project_pnl's by_month carries ebitda_eur; the derived fallback exists for
    a caller that only has the three lines. They must agree."""
    with_ebitda = [dict(r, ebitda_eur=150_000.0) for r in FLAT]
    assert (project(with_ebitda)["totals"]["ebitda_eur"]
            == pytest.approx(project()["totals"]["ebitda_eur"]))


def test_the_bridge_closes_from_ebitda_to_free_cash_flow():
    result = project(capex_by_month={"2027-02": 300_000.0}, tax_rate_pct=25.0,
                     opening_balances={"receivables": 900_000.0,
                                       "inventory": 400_000.0, "payables": 1_000_000.0})
    points = cash.cash_bridge(result)
    assert points[0]["label"] == "EBITDA" and points[0]["kind"] == "absolute"
    assert points[-1]["label"] == "Free cash flow" and points[-1]["kind"] == "absolute"
    deltas = sum(p["value"] for p in points if p["kind"] == "delta")
    assert points[0]["value"] + deltas == pytest.approx(points[-1]["value"], abs=0.05)


# --------------------------------------------------------------------------
# 3. The sign convention
# --------------------------------------------------------------------------

def test_a_longer_dso_consumes_cash():
    slow = project(dso_days=90.0, opening_balances={"receivables": 1_000_000.0,
                                                    "inventory": 500_000.0,
                                                    "payables": 700_000.0})
    fast = project(dso_days=20.0, opening_balances={"receivables": 1_000_000.0,
                                                    "inventory": 500_000.0,
                                                    "payables": 700_000.0})
    assert (slow["totals"]["free_cash_flow_eur"]
            < fast["totals"]["free_cash_flow_eur"])


def test_a_longer_dpo_releases_cash():
    """Paying suppliers later is a cash inflow. This is the sign most easily got
    backwards, and getting it backwards moves the trough the wrong way."""
    base = {"opening_balances": {"receivables": 1_000_000.0, "inventory": 500_000.0,
                                 "payables": 700_000.0}}
    late = project(dpo_days=80.0, **base)
    early = project(dpo_days=10.0, **base)
    assert (late["totals"]["free_cash_flow_eur"]
            > early["totals"]["free_cash_flow_eur"])


def test_growth_absorbs_cash_even_with_flat_margins():
    """The sentence this whole module exists to let the agent say: a scenario can
    add EBITDA and still cost cash, because the receivables and inventory behind
    the extra revenue have to be funded first."""
    growing = [dict(r, revenue_eur=r["revenue_eur"] * (1.03 ** i),
                    cogs_eur=r["cogs_eur"] * (1.03 ** i),
                    opex_eur=r["opex_eur"] * (1.03 ** i))
               for i, r in enumerate(FLAT)]
    flat_year = project(opening_balances={"receivables": 1_500_000.0,
                                          "inventory": 600_000.0,
                                          "payables": 800_000.0})
    grown = project(growing, opening_balances={"receivables": 1_500_000.0,
                                               "inventory": 600_000.0,
                                               "payables": 800_000.0})
    assert grown["totals"]["ebitda_eur"] > flat_year["totals"]["ebitda_eur"]
    assert (grown["totals"]["working_capital_change_eur"]
            > flat_year["totals"]["working_capital_change_eur"])


# --------------------------------------------------------------------------
# 4. Nothing is invented
# --------------------------------------------------------------------------

def test_no_tax_rate_means_no_tax_and_a_caveat_saying_so():
    result = project()
    assert result["totals"]["tax_eur"] == 0.0
    assert result["tax_rate_pct"] is None
    assert any("no tax is deducted" in c for c in result["caveats"])


def test_a_stated_rate_taxes_only_positive_ebitda():
    """No carry-back is modelled, so a loss-making month does not refund."""
    rows = [dict(FLAT[0], month="2027-01"),
            dict(FLAT[1], month="2027-02", revenue_eur=100_000.0)]
    result = project(rows, tax_rate_pct=25.0)
    assert result["months"][0]["tax_eur"] > 0
    assert result["months"][1]["ebitda_eur"] < 0
    assert result["months"][1]["tax_eur"] == 0.0


def test_an_implied_opening_balance_sheet_is_declared_and_shows_no_first_step():
    result = project()
    assert result["opening_balances_implied"] is True
    assert result["months"][0]["working_capital_change_eur"] == pytest.approx(0.0)
    assert any("implied by the first projected month" in c for c in result["caveats"])


def test_the_financing_limitation_is_always_stated():
    """There is no depreciation, interest or debt dataset, so this is free cash
    flow before financing and every projection has to say so — the same rule as
    the missing payroll dataset."""
    for result in (project(), project(tax_rate_pct=25.0)):
        assert any("not modelled" in c.lower() and "interest" in c
                   for c in result["caveats"])


def test_no_rows_is_an_error_not_an_exception():
    assert "error" in cash.project_cashflow([], **DAYS)


def test_cash_conversion_is_none_not_zero_when_there_is_no_ebitda():
    """"The ratio is undefined" and "none of it converts" are different answers,
    and a UI rendering 0% for the first is lying — the same rule driver_status
    follows for a driver with no forward curve."""
    rows = [dict(r, revenue_eur=0.0, cogs_eur=0.0, opex_eur=0.0) for r in FLAT]
    assert project(rows)["totals"]["cash_conversion_pct"] is None


# --------------------------------------------------------------------------
# 5. The trough — the number a CFO acts on
# --------------------------------------------------------------------------

def test_the_trough_is_the_lowest_closing_balance_and_its_month():
    result = project(opening_cash_eur=1_000_000.0,
                     capex_by_month={"2027-02": 3_000_000.0})
    lowest = min(result["months"], key=lambda r: r["closing_cash_eur"])
    assert result["trough"]["month"] == lowest["month"] == "2027-02"
    assert result["trough"]["closing_cash_eur"] == pytest.approx(
        lowest["closing_cash_eur"])


def test_a_minimum_cash_floor_names_every_month_below_it():
    result = project(opening_cash_eur=500_000.0,
                     capex_by_month={"2027-02": 2_000_000.0},
                     min_cash_eur=250_000.0)
    assert result["breaches_minimum"] is True
    assert "2027-02" in result["months_below_minimum"]
    assert result["min_cash_eur"] == 250_000.0


def test_no_floor_means_no_breach_rather_than_a_breach_at_zero():
    result = project(opening_cash_eur=-5_000_000.0)
    assert result["min_cash_eur"] is None
    assert result["breaches_minimum"] is False


def test_calendar_days_are_real_days():
    result = project()
    by_month = {r["month"]: r["days"] for r in result["months"]}
    assert by_month["2027-02"] == 28 and by_month["2027-01"] == 31
    assert cash.days_in_month("2028-02") == 29          # leap year
    assert cash.days_in_month("not-a-month") == cash.FALLBACK_DAYS


# --------------------------------------------------------------------------
# 6. The tool: the wiring, on CSV fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Datasets, a scenario and a profile at a tmp_path — the whole path
    cash_flow_projection walks, with nothing real on disk."""
    from app import drivers, profile as profile_mod, scenarios, tools

    monkeypatch.setattr(tools, "DATA_DIR", tmp_path)
    monkeypatch.setattr(drivers, "DATA_DIR", tmp_path)
    monkeypatch.setattr(drivers, "DRIVERS_FILE", tmp_path / "drivers.parquet")
    monkeypatch.setattr(drivers, "PRICES_FILE", tmp_path / "driver_prices.parquet")
    monkeypatch.setattr(drivers, "PRICES_CSV", tmp_path / "driver_prices.csv")
    monkeypatch.setattr(drivers, "ASSUMPTIONS_FILE", tmp_path / "locked_assumptions.json")
    monkeypatch.setattr(scenarios, "SCENARIOS_FILE", tmp_path / "scenarios.json")
    monkeypatch.setattr(profile_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(profile_mod, "PROFILE_FILE", tmp_path / "company_profile.json")

    def csv(name, rows):
        pd.DataFrame(rows).to_csv(tmp_path / f"{name}.csv", index=False)

    # Two closed months of balances, then the budget year's P&L.
    csv("working_capital", [
        {"month": "2026-11", "revenue_eur": 1_000_000.0, "cogs_eur": 800_000.0,
         "receivables_eur": 1_500_000.0, "inventory_eur": 800_000.0,
         "payables_eur": 900_000.0, "cash_eur": 3_000_000.0},
        {"month": "2026-12", "revenue_eur": 1_000_000.0, "cogs_eur": 800_000.0,
         "receivables_eur": 1_500_000.0, "inventory_eur": 800_000.0,
         "payables_eur": 900_000.0, "cash_eur": 3_200_000.0},
    ])
    csv("capex_plan", [
        {"month": "2027-03", "project": "Extruder line", "category": "capacity",
         "amount_eur": 0.0, "amount_budget_eur": 900_000.0, "status": "committed"},
        {"month": "2027-09", "project": "Warehouse racking", "category": "capacity",
         "amount_eur": 0.0, "amount_budget_eur": 400_000.0, "status": "proposed"},
        {"month": "2026-05", "project": "Old project", "category": "capacity",
         "amount_eur": 100_000.0, "amount_budget_eur": 100_000.0, "status": "committed"},
    ])

    scenarios.save_scenario({
        "name": "Test plan", "baseline": "budget_2027", "assumptions": {},
        "totals": {"revenue_eur": 12_000_000.0, "ebitda_eur": 1_800_000.0},
        "by_month": [{"month": f"2027-{m:02d}", "revenue_eur": 1_000_000.0,
                      "cogs_eur": 700_000.0, "opex_eur": 150_000.0,
                      "ebitda_eur": 150_000.0} for m in range(1, 13)],
        "active": True,
    })
    return tmp_path


def test_the_tool_measures_the_days_and_spends_the_capex_plan(wired):
    from app import tools

    result = tools.cash_flow_projection()
    assert "error" not in result
    assert result["days"]["basis"] == "measured"
    # Nov (30) + Dec (31) = 61 days of history; the tool rounds days to 1dp.
    assert result["days"]["dso_days"] == round(1_500_000.0 * 61 / 2_000_000.0, 1)
    # Only the budget year's capex, and the proposed project is included by
    # default because it is in the plan.
    assert result["totals"]["capex_eur"] == pytest.approx(1_300_000.0)
    assert result["capex"]["committed_eur"] == pytest.approx(900_000.0)
    assert result["capex"]["proposed_eur"] == pytest.approx(400_000.0)
    assert result["source_file"]


def test_deferring_the_proposed_capex_is_a_lever_the_tool_offers(wired):
    from app import tools

    kept = tools.cash_flow_projection()
    deferred = tools.cash_flow_projection(include_proposed_capex=False)
    assert deferred["totals"]["capex_eur"] == pytest.approx(900_000.0)
    assert (deferred["totals"]["closing_cash_eur"]
            > kept["totals"]["closing_cash_eur"])


def test_the_opening_cash_and_balances_come_from_the_dataset(wired):
    from app import tools

    result = tools.cash_flow_projection()
    assert result["totals"]["opening_cash_eur"] == pytest.approx(3_200_000.0)
    assert result["opening_balances_implied"] is False
    assert result["opening_balances"]["receivables"] == pytest.approx(1_500_000.0)


def test_a_cfo_override_beats_the_measured_days_and_is_labelled(wired):
    """A stated day count is a decision ("we are going to collect faster"), not a
    measurement, and the projection has to say which it is."""
    from app import profile as profile_mod, tools

    profile_mod.set_working_capital({"dso_days": 30.0, "tax_rate_pct": 25.0})
    result = tools.cash_flow_projection()
    assert result["days"]["dso_days"] == 30.0
    assert result["days"]["basis"] == "measured + stated"
    assert result["days"]["stated"] == ["dso_days"]
    assert result["totals"]["tax_eur"] > 0


def test_a_tool_argument_beats_the_profile(wired):
    from app import profile as profile_mod, tools

    profile_mod.set_working_capital({"dso_days": 30.0})
    result = tools.cash_flow_projection(dso_days=75.0)
    assert result["days"]["dso_days"] == 75.0


def test_no_working_capital_dataset_and_no_stated_days_is_a_teachable_error(wired):
    (wired / "working_capital.csv").unlink()
    from app import tools

    result = tools.cash_flow_projection()
    assert "error" in result
    assert "dso_days" in result["error"]


def test_stated_days_alone_carry_the_projection_with_no_dataset(wired):
    """A company with no working-capital history can still plan cash by stating
    its terms — the dataset makes the days measured, it does not gate them."""
    (wired / "working_capital.csv").unlink()
    from app import profile as profile_mod, tools

    profile_mod.set_working_capital({"dso_days": 45.0, "dio_days": 30.0,
                                     "dpo_days": 40.0,
                                     "opening_cash_eur": 1_000_000.0})
    result = tools.cash_flow_projection()
    assert "error" not in result
    assert result["days"]["basis"] == "stated"
    assert result["totals"]["opening_cash_eur"] == 1_000_000.0
    assert result["opening_balances_implied"] is True


def test_an_unknown_scenario_enumerates_the_ones_that_exist(wired):
    from app import tools

    result = tools.cash_flow_projection(scenario_id="nope")
    assert "error" in result and "Test plan" in result["error"]


def test_the_profile_working_capital_block_rejects_nonsense(wired):
    from app import profile as profile_mod

    saved = profile_mod.set_working_capital(
        {"dso_days": "not a number", "dio_days": -5.0, "dpo_days": 40.0,
         "tax_rate_pct": 900.0, "note": "  terms are 45 days  "})
    wc = saved["working_capital"]
    assert wc["dso_days"] is None          # unparseable → not stated
    assert wc["dio_days"] == 0.0           # negative days clamp to zero
    assert wc["dpo_days"] == 40.0
    assert wc["tax_rate_pct"] == 100.0     # a rate above 100% is not a rate
    assert wc["note"] == "terms are 45 days"
    assert json.loads((wired / "company_profile.json").read_text())["working_capital"]

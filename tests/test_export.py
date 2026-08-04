# Export: the workbook and the board pack.
#
# The property that matters is provenance survival. A budget exported without
# source_url / retrieved_at is a spreadsheet anyone could have typed; the whole
# reason to export from this tool is that the assumptions arrive with the pages
# they were read from attached. Most of this file asserts exactly that, on both
# formats.
#
# Everything here is pure — packs are built from plain dicts, no dataset, no
# tmp_path — because export.py takes provenance as an argument rather than
# reading drivers.py.

from io import BytesIO

import pytest

from app import export

openpyxl = pytest.importorskip("openpyxl")

SHEETS = ["Summary", "Monthly P&L", "By product line", "Cash flow", "Assumptions",
          "Driver bridge", "Version diff"]

DRIVERS = [
    {"driver_id": "chicken_meal", "name": "Chicken meal", "unit": "EUR/t",
     "value": 525.0, "source_url": "https://ex.example/meal-nov",
     "retrieved_at": "2026-11-12T09:00:00+00:00",
     "locked_at": "2026-11-12T10:00:00+00:00", "rationale": "November re-lock"},
    {"driver_id": "wheat", "name": "Feed wheat", "unit": "EUR/t", "value": 212.0,
     "source_url": "https://ex.example/wheat", "retrieved_at": "2026-11-10T09:00:00+00:00",
     "locked_at": None, "rationale": None},
]

SCENARIO = {
    "id": "sc1",
    "name": "2027 budget · November re-lock",
    "note": "Chicken meal re-locked after the September position went adverse.",
    "baseline": "budget_2027",
    "active": True,
    "updated_at": 1_770_000_000.0,
    "assumptions": {"drivers": {"chicken_meal": 12.0}, "volume": {"Aqua Feed": -15.0},
                    "price": {"*": 4.0}, "opex": {"Production": 3.0}},
    "price_pass_through": 0.35,
    "opex_inflation_pct": 2.0,
    "totals": {"volume_tonnes": 150_000.0, "revenue_eur": 82_000_000.0,
               "cogs_eur": 66_000_000.0, "opex_eur": 4_300_000.0,
               "gross_margin_eur": 16_000_000.0, "ebitda_eur": 11_700_000.0,
               "ebitda_margin_pct": 14.27},
    "by_month": [{"month": "2027-01", "volume_tonnes": 11_800.0,
                  "revenue_eur": 6_300_000.0, "cogs_eur": 5_100_000.0,
                  "gross_margin_eur": 1_200_000.0, "opex_eur": 330_000.0,
                  "ebitda_eur": 870_000.0}],
    "by_product_line": [{"product_line": "Aqua Feed", "volume_tonnes": 100_000.0,
                         "revenue_eur": 50_000_000.0, "cogs_eur": 40_000_000.0,
                         "gross_margin_eur": 10_000_000.0, "opex_eur": 2_500_000.0,
                         "ebitda_eur": 7_500_000.0, "ebitda_margin_pct": 15.0}],
    "driver_impact_eur": {"chicken_meal": 1_400_000.0},
    "opex_bridge": {"by_cost_centre": [
        {"cost_centre": "Production", "driver_id": "wage_inflation", "basis": "override",
         "pct": 3.0, "base_eur": 400_000.0, "delta_eur": 12_000.0}],
        "base_eur": 400_000.0, "delta_eur": 12_000.0, "weighted_pct": 3.0,
        "default_pct": 2.0},
    "ebitda_bridge": {"baseline_ebitda_eur": 12_000_000.0,
                      "projected_ebitda_eur": 11_700_000.0,
                      "volume_eur": -900_000.0, "price_eur": 1_200_000.0,
                      "opex_eur": -12_000.0,
                      "drivers_eur": {"chicken_meal": -600_000.0, "wheat": 12_000.0}},
}

VERSION = {
    "id": "v2", "version_no": 2, "label": "November re-lock", "status": "approved",
    "scenario_id": "sc1", "scenario_name": SCENARIO["name"],
    "baseline": "budget_2027", "note": None,
    "assumptions_snapshot": SCENARIO["assumptions"],
    "price_pass_through": 0.35, "opex_inflation_pct": 2.0,
    "totals": SCENARIO["totals"], "by_month": SCENARIO["by_month"],
    "by_product_line": SCENARIO["by_product_line"],
    "opex_bridge": SCENARIO["opex_bridge"], "ebitda_bridge": SCENARIO["ebitda_bridge"],
    "drivers_snapshot": DRIVERS, "locked_at": "2026-11-12T10:00:00+00:00",
    "created_at": 1_770_000_000.0, "created_by": "N. Bentmann",
    "approved_by": "A. Board", "approved_at": 1_770_100_000.0,
}

DIFF = {
    "from": {"id": "v1", "version_no": 1, "label": "September lock"},
    "to": {"id": "v2", "version_no": 2, "label": "November re-lock"},
    "assumptions": {"drivers": [{"key": "chicken_meal", "from_pct": 5.0, "to_pct": 12.0,
                                 "delta_pp": 7.0, "change": "changed"}],
                    "volume": [], "price": [], "opex": []},
    "assumption_change_count": 1,
    "locked_values": [{"driver_id": "chicken_meal", "name": "Chicken meal",
                       "unit": "EUR/t", "from_value": 420.0, "to_value": 525.0,
                       "delta_pct": 25.0, "to_source_url": "https://ex.example/meal-nov",
                       "to_retrieved_at": "2026-11-12T09:00:00+00:00"}],
    "totals": {}, "by_product_line": [],
    "ebitda_delta_eur": -2_400_000.0,
    "ebitda_bridge": [{"label": "v1 EBITDA", "value": 14_100_000.0, "kind": "absolute"},
                      {"label": "chicken_meal", "value": -2_400_000.0, "kind": "delta"},
                      {"label": "v2 EBITDA", "value": 11_700_000.0, "kind": "absolute"}],
    "check_residual": 0.0,
}


CASH = {
    "days": {"dso_days": 51.9, "dio_days": 35.2, "dpo_days": 40.1,
             "cash_conversion_cycle_days": 47.0, "basis": "measured",
             "stated": [], "measured_from": "2026-01..2026-12"},
    "totals": {"ebitda_eur": 11_591_244.0, "working_capital_change_eur": 737_457.0,
               "tax_eur": 2_781_899.0, "capex_eur": 6_020_000.0,
               "free_cash_flow_eur": 2_051_888.0, "opening_cash_eur": 3_790_364.0,
               "closing_cash_eur": 5_842_252.0, "cash_conversion_pct": 17.7},
    "months": [{"month": "2027-01", "ebitda_eur": 894_477.0,
                "working_capital_change_eur": -922_074.0, "tax_eur": 214_674.0,
                "capex_eur": 0.0, "free_cash_flow_eur": 1_601_877.0,
                "closing_cash_eur": 5_392_241.0}],
    "trough": {"month": "2027-04", "closing_cash_eur": 2_580_000.0},
    "min_cash_eur": 3_000_000.0, "months_below_minimum": ["2027-02", "2027-04"],
    "breaches_minimum": True, "tax_rate_pct": 24.0,
    "capex": {"total_eur": 6_020_000.0, "committed_eur": 5_180_000.0,
              "proposed_eur": 840_000.0, "proposed_included": True,
              "projects": [{"month": "2027-02", "project": "Extruder line (phase 1)",
                            "category": "capacity", "status": "committed",
                            "amount_eur": 1_750_000.0}]},
    "caveats": ["This is free cash flow before financing: interest and debt are not modelled."],
}


def scenario_pack(cash=None):
    return export.scenario_pack(SCENARIO, drivers_snapshot=DRIVERS,
                                locked_at="2026-11-12T10:00:00+00:00",
                                company="Delta Feeds NV", cash=cash)


def version_pack():
    return export.version_pack(VERSION, company="Delta Feeds NV", diff=DIFF)


def load(pack):
    return openpyxl.load_workbook(BytesIO(export.workbook(pack)))


def cells(ws):
    """Every cell value in a sheet, as strings, for substring assertions."""
    return [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]


# ---------- Packs ----------

def test_a_pack_normalises_a_legacy_flat_assumption_dict():
    legacy = dict(SCENARIO, assumptions={"chicken_meal": 12.0})
    pack = export.scenario_pack(legacy, drivers_snapshot=DRIVERS)
    assert pack["assumptions"]["drivers"] == {"chicken_meal": 12.0}
    assert pack["assumptions"]["volume"] == {}


def test_a_version_pack_reports_the_approval_attestation():
    meta = dict(version_pack()["meta"])
    assert meta["Approved by"] == "A. Board"
    assert meta["Status"] == "approved"
    assert meta["Created by"] == "N. Bentmann"


def test_filenames_are_safe_to_write_to_disk():
    name = export.filename(scenario_pack(), "xlsx")
    assert name.endswith(".xlsx")
    assert not set(name) & set(' /\\:*?"<>|')


# ---------- The workbook ----------

def test_the_workbook_has_the_seven_sheets():
    assert load(scenario_pack()).sheetnames == SHEETS


def test_the_cash_sheet_carries_the_days_their_basis_and_the_trough():
    ws = load(scenario_pack(cash=CASH))["Cash flow"]
    values = cells(ws)
    assert "51.9" in values and "47" in values
    # Whether the days were measured or decided is this sheet's provenance, the
    # same argument the Assumptions sheet makes for a driver price.
    assert "measured" in values
    assert "2027-04" in values
    assert "2027-02, 2027-04" in values


def test_the_cash_sheet_states_the_before_financing_limitation():
    values = cells(load(scenario_pack(cash=CASH))["Cash flow"])
    assert any("not modelled" in v for v in values)


def test_the_capital_plan_is_listed_project_by_project():
    values = cells(load(scenario_pack(cash=CASH))["Cash flow"])
    assert "Extruder line (phase 1)" in values and "committed" in values


def test_a_pack_with_no_cash_profile_says_why_rather_than_showing_zeros():
    ws = load(scenario_pack())["Cash flow"]
    assert "working_capital" in str(ws["A1"].value)


def test_a_version_freezes_its_own_cash_profile():
    """A version's cash comes from its snapshot, never from today's measurement —
    the same rule as its driver provenance."""
    pack = export.version_pack(dict(VERSION, cash_snapshot=CASH),
                               company="Delta Feeds NV")
    assert pack["cash"]["days"]["dso_days"] == 51.9
    assert export.version_pack(VERSION)["cash"] is None


def test_the_board_pack_reports_cash_with_its_caveats():
    text = export.board_pack_markdown(scenario_pack(cash=CASH))
    assert "## Cash" in text
    assert "2027-04" in text
    assert "3,000,000 € minimum" in text
    assert "DSO 51.9" in text
    assert "before financing" in text


def test_the_board_pack_omits_the_cash_section_when_there_is_none():
    assert "## Cash" not in export.board_pack_markdown(scenario_pack())


def test_every_assumed_driver_arrives_with_its_source_and_retrieval_date():
    # This is the reason to export from this tool rather than from a spreadsheet.
    ws = load(scenario_pack())["Assumptions"]
    values = cells(ws)
    assert "https://ex.example/meal-nov" in values
    assert "2026-11-12T09:00:00+00:00" in values
    assert "2026-11-12T10:00:00+00:00" in values
    assert "November re-lock" in values


def test_drivers_the_scenario_left_alone_are_still_listed():
    # A board pack has to show what the budget rests on, not only what was shocked.
    values = cells(load(scenario_pack())["Assumptions"])
    assert "Feed wheat" in values
    assert "https://ex.example/wheat" in values


def test_all_four_assumption_blocks_reach_the_sheet():
    values = cells(load(scenario_pack())["Assumptions"])
    for label in ("Driver price", "Volume", "Selling price", "Opex"):
        assert label in values
    assert "Aqua Feed" in values and "*" in values


def test_the_driver_bridge_sheet_closes():
    ws = load(scenario_pack())["Driver bridge"]
    rows = {r[0].value: r[1].value for r in ws.iter_rows(min_row=2, max_col=2)
            if r[0].value}
    # openpyxl reads an integral float back as an int, so accept both.
    steps = sum(v for k, v in rows.items()
                if k not in ("Baseline EBITDA", "Scenario EBITDA", "Opex by cost centre")
                and isinstance(v, (int, float)) and not isinstance(v, bool))
    assert (rows["Baseline EBITDA"] + steps) == pytest.approx(rows["Scenario EBITDA"],
                                                              abs=0.01)


def test_the_bridge_lists_every_driver_rather_than_folding_the_small_ones():
    # Unlike the chart's waterfall, which folds to fit WATERFALL_MAX_POINTS.
    labels = [label for label, _, _ in export.bridge_rows(SCENARIO["ebitda_bridge"])]
    assert "chicken_meal" in labels and "wheat" in labels
    assert "Other drivers" not in labels


def test_monthly_and_product_line_sheets_carry_their_rows():
    wb = load(scenario_pack())
    assert cells(wb["Monthly P&L"])[7] == "2027-01"
    assert "Aqua Feed" in cells(wb["By product line"])


def test_the_version_diff_sheet_is_present_and_honest_when_there_is_nothing_to_compare():
    ws = load(scenario_pack())["Version diff"]
    assert ws["A1"].value == "No prior version to compare against."


def test_the_version_diff_sheet_carries_the_re_lock_and_its_source():
    values = cells(load(version_pack())["Version diff"])
    assert "v1 → v2" in values
    assert "chicken_meal" in values
    assert "https://ex.example/meal-nov" in values


# ---------- The board pack ----------

def test_the_board_pack_carries_the_headline_and_the_provenance():
    md = export.board_pack_markdown(scenario_pack())
    assert md.startswith("# Delta Feeds NV — 2027 budget · November re-lock")
    assert "| EBITDA (€) | 11,700,000 |" in md
    assert "[source](https://ex.example/meal-nov)" in md
    assert "2026-11-12T09:00:00+00:00" in md


def test_the_board_pack_reports_the_opex_bridge_by_cost_centre():
    md = export.board_pack_markdown(scenario_pack())
    assert "## Opex by cost centre" in md
    assert "Production" in md and "wage_inflation" in md
    assert "Weighted opex growth: **3.00%**" in md


def test_the_board_pack_states_the_version_move():
    md = export.board_pack_markdown(version_pack())
    assert "## Change from v1 to v2" in md
    assert "EBITDA moves **-2,400,000 €**." in md
    assert "| Chicken meal | 420.00 | 525.00 | 25.00% |" in md


def test_the_board_pack_needs_no_third_party_package(monkeypatch):
    # Markdown export must survive a missing openpyxl — that is why the import
    # lives inside workbook().
    import builtins
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("openpyxl"):
            raise ImportError("openpyxl blocked for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert "EBITDA" in export.board_pack_markdown(scenario_pack())
    with pytest.raises(RuntimeError, match="openpyxl"):
        export.workbook(scenario_pack())


def test_a_pack_with_nothing_in_it_still_exports():
    empty = export.scenario_pack({"id": "x", "name": "Empty"})
    assert "# Budget — Empty" in export.board_pack_markdown(empty)
    assert load(empty).sheetnames == SHEETS

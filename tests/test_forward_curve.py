# Phase 3.1 — forward curves.
#
# The point of the phase is that the app stops extrapolating and starts reading
# the curve the market publishes. Three things have to hold for that to be worth
# anything, and they are what this file covers:
#
#   1. The SAME two guards stand in front of the second dataset. A curve is
#      model-written data, so `append_forward` must refuse an uncited URL and a
#      figure outside the sanity band exactly as `append_observation` does. A
#      second, laxer implementation of the trust boundary would be worse than no
#      forward curve at all.
#   2. Recording is all-or-nothing and supersedes rather than mutates — a curve
#      with one bad month writes nothing, and re-reading the same page wins
#      without deleting what it replaced.
#   3. `project_pnl` on a curve is a pure EXTENSION: with a flat curve at the
#      locked price the arithmetic is byte-for-byte what it was, and the EBITDA
#      bridge still closes. That property is what lets the basis be a switch
#      rather than a fork.
#
# CSV fixtures throughout, at a tmp_path, per the repo convention.

import pandas as pd
import pytest

from app import budget, drivers
from app.citations import normalise_url

VISITED = {normalise_url("https://exchange.example/curves/wheat")}
CITED = "https://exchange.example/curves/wheat"


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(drivers, "DATA_DIR", tmp_path)
    monkeypatch.setattr(drivers, "DRIVERS_FILE", tmp_path / "drivers.parquet")
    monkeypatch.setattr(drivers, "PRICES_FILE", tmp_path / "driver_prices.parquet")
    monkeypatch.setattr(drivers, "PRICES_CSV", tmp_path / "driver_prices.csv")
    monkeypatch.setattr(drivers, "FORWARDS_FILE", tmp_path / "driver_forwards.parquet")
    monkeypatch.setattr(drivers, "FORWARDS_CSV", tmp_path / "driver_forwards.csv")
    monkeypatch.setattr(drivers, "ASSUMPTIONS_FILE", tmp_path / "locked_assumptions.json")

    pd.DataFrame([
        {"driver_id": "wheat", "name": "Feed wheat", "category": "ingredient",
         "unit": "EUR/t", "quote_currency": "EUR", "baseline": 245.0,
         "hedge_coverage": 0.0, "adverse_direction": "up", "stale_after_days": 7,
         "search_hint": ""},
    ]).to_csv(tmp_path / "drivers.csv", index=False)

    pd.DataFrame([
        {"month": "2026-12", "driver_id": "wheat", "price": 250.0, "currency": "EUR",
         "source": "seed", "source_url": "", "revision": 0,
         "recorded_at": "2026-12-01T00:00:00+00:00", "note": ""},
    ]).to_csv(tmp_path / "driver_prices.csv", index=False)
    return tmp_path


def curve(*pairs):
    return [{"quote_month": m, "price": p} for m, p in pairs]


# ---------- The guards, unchanged, on the second dataset ----------

def test_a_curve_from_an_unvisited_page_is_refused(data):
    result = drivers.append_forward("wheat", curve(("2027-01", 260.0)),
                                    source_url="https://elsewhere.example/curve",
                                    fetched_urls=VISITED)
    assert "error" in result
    assert "not fetched in this turn" in result["error"]
    assert drivers.load_forwards().empty      # and nothing was written


def test_a_curve_from_the_visited_page_is_recorded(data):
    result = drivers.append_forward("wheat", curve(("2027-01", 260.0), ("2027-02", 262.0)),
                                    source_url=CITED, fetched_urls=VISITED,
                                    curve_date="2026-12-15")
    assert result["recorded"] is True
    assert result["months"] == 2
    assert result["curve_date"] == "2026-12-15"
    assert result["spot_reference"] == 250.0
    assert len(drivers.load_forwards()) == 2


def test_a_point_outside_the_sanity_band_rejects_the_whole_curve(data):
    # 2000 is 8x the 250 spot — a units mistake, not a market.
    result = drivers.append_forward(
        "wheat", curve(("2027-01", 260.0), ("2027-02", 2000.0)),
        source_url=CITED, fetched_urls=VISITED)
    assert "error" in result
    assert result["quote_month"] == "2027-02"
    # All or nothing: the good month must not have landed either, or the stored
    # curve is one nobody could have read off the cited page.
    assert drivers.load_forwards().empty


def test_the_band_can_be_overridden_deliberately(data):
    result = drivers.append_forward("wheat", curve(("2027-01", 2000.0)),
                                    source_url=CITED, fetched_urls=VISITED,
                                    override_sanity_check=True)
    assert result["recorded"] is True


def test_an_unknown_driver_is_refused_with_the_valid_ids(data):
    result = drivers.append_forward("unobtainium", curve(("2027-01", 1.0)),
                                    source_url=CITED, fetched_urls=VISITED)
    assert "wheat" in result["error"]


def test_a_duplicated_quote_month_is_refused(data):
    result = drivers.append_forward("wheat", curve(("2027-01", 260.0), ("2027-01", 261.0)),
                                    source_url=CITED, fetched_urls=VISITED)
    assert "same month" in result["error"]
    assert drivers.load_forwards().empty


def test_an_unparseable_month_is_refused_teachably(data):
    result = drivers.append_forward("wheat", curve(("January 2027", 260.0)),
                                    source_url=CITED, fetched_urls=VISITED)
    assert "YYYY-MM" in result["error"]


def test_the_manual_path_can_skip_provenance(data):
    """The same escape hatch append_observation has: a human typed it."""
    result = drivers.append_forward("wheat", curve(("2027-01", 260.0)),
                                    source_url="manual entry", fetched_urls=set(),
                                    verify_provenance=False)
    assert result["recorded"] is True


# ---------- Reading a curve back ----------

def test_the_newest_curve_wins_per_month_without_deleting_the_old(data):
    drivers.append_forward("wheat", curve(("2027-01", 260.0), ("2027-02", 262.0)),
                           source_url=CITED, fetched_urls=VISITED, curve_date="2026-11-01")
    drivers.append_forward("wheat", curve(("2027-01", 280.0)),
                           source_url=CITED, fetched_urls=VISITED, curve_date="2026-12-01")

    points = drivers.forward_curve("wheat", start_month="2027-01")
    prices = {p["quote_month"]: p["price"] for p in points}
    assert prices["2027-01"] == 280.0        # superseded by the newer curve
    assert prices["2027-02"] == 262.0        # the older curve still covers this month
    assert len(drivers.load_forwards()) == 3  # and nothing was mutated away


def test_a_rewrite_of_the_same_curve_date_bumps_the_revision(data):
    drivers.append_forward("wheat", curve(("2027-01", 260.0)), source_url=CITED,
                           fetched_urls=VISITED, curve_date="2026-12-01")
    second = drivers.append_forward("wheat", curve(("2027-01", 265.0)), source_url=CITED,
                                    fetched_urls=VISITED, curve_date="2026-12-01")
    assert second["revision"] == 1
    points = drivers.forward_curve("wheat", start_month="2027-01")
    assert points[0]["price"] == 265.0


def test_months_that_have_closed_are_dropped(data):
    drivers.append_forward("wheat", curve(("2027-01", 260.0), ("2027-06", 270.0)),
                           source_url=CITED, fetched_urls=VISITED)
    assert [p["quote_month"] for p in
            drivers.forward_curve("wheat", start_month="2027-03")] == ["2027-06"]


def test_an_entirely_historical_curve_is_returned_rather_than_silence(data):
    """A caller asking for a curve that exists should not get an empty list."""
    drivers.append_forward("wheat", curve(("2027-01", 260.0)), source_url=CITED,
                           fetched_urls=VISITED)
    assert drivers.forward_curve("wheat", start_month="2030-01")


def test_the_horizon_caps_the_number_of_months(data):
    drivers.append_forward("wheat", curve(*[(f"2027-{m:02d}", 260.0) for m in range(1, 13)]),
                           source_url=CITED, fetched_urls=VISITED)
    assert len(drivers.forward_curve("wheat", months=3, start_month="2027-01")) == 3


def test_a_driver_with_no_curve_reads_as_empty_not_as_an_error(data):
    assert drivers.forward_curve("wheat") == []


# ---------- driver_status ----------

def test_driver_status_reports_the_forward_against_the_lock(data):
    drivers.lock_assumptions({"wheat": {"value": 250.0, "unit": "EUR/t"}})
    drivers.append_forward("wheat", curve(("2027-01", 260.0), ("2027-02", 265.0)),
                           source_url=CITED, fetched_urls=VISITED)

    row = next(d for d in drivers.driver_status() if d["driver_id"] == "wheat")
    assert row["forward_12m"] == pytest.approx(262.5)
    assert row["forward_vs_lock_pct"] == pytest.approx(5.0)
    assert row["forward_months"] == 2


def test_no_curve_reads_as_none_not_as_zero(data):
    """'We have not looked' and 'the market says flat' are different states, and
    a UI that renders 0% for the first one is lying."""
    drivers.lock_assumptions({"wheat": {"value": 250.0, "unit": "EUR/t"}})
    row = next(d for d in drivers.driver_status() if d["driver_id"] == "wheat")
    assert row["forward_12m"] is None
    assert row["forward_vs_lock_pct"] is None


# ---------- project_pnl on a curve ----------

BASELINE = [
    {"month": f"2027-{m:02d}", "product_line": "Poultry Feed", "volume_tonnes": 1000.0,
     "revenue_eur": 400_000.0, "cogs_eur": 340_000.0, "opex_eur": 20_000.0}
    for m in range(1, 4)
]
BOM = [{"product_line": "Poultry Feed", "driver_id": "wheat", "qty_per_tonne": 0.4}]
LOCKED = {"wheat": 250.0}


def test_a_flat_curve_at_the_locked_price_changes_nothing():
    """The extension property. With the curve sitting exactly on the lock, the
    projection must be identical to one built with no curve at all — that is
    what makes `basis` a switch rather than a second engine."""
    flat = {r["month"]: {"wheat": 250.0} for r in BASELINE}
    without = budget.project_pnl(BASELINE, BOM, LOCKED, {})
    with_curve = budget.project_pnl(BASELINE, BOM, LOCKED, {}, driver_prices_by_month=flat)
    assert with_curve["totals"] == without["totals"]
    assert with_curve["ebitda_bridge"]["total_eur"] == without["ebitda_bridge"]["total_eur"]


def test_a_curve_moves_cost_month_by_month():
    """Seasonality falls out of the curve instead of being assumed: three months
    at three prices give three different COGS off one identical baseline."""
    shaped = {"2027-01": {"wheat": 300.0},     # +50 on the lock
              "2027-02": {"wheat": 250.0},     # on the lock
              "2027-03": {"wheat": 200.0}}     # −50
    out = budget.project_pnl(BASELINE, BOM, LOCKED, {}, driver_prices_by_month=shaped)
    by_month = {r["month"]: r["delta_cogs_eur"] for r in out["rows"]}
    # 0.4 t/t × 1000 t × ±50 EUR/t
    assert by_month["2027-01"] == pytest.approx(20_000.0)
    assert by_month["2027-02"] == pytest.approx(0.0)
    assert by_month["2027-03"] == pytest.approx(-20_000.0)


def test_a_month_the_curve_does_not_cover_holds_at_the_locked_price():
    partial = {"2027-01": {"wheat": 300.0}}
    out = budget.project_pnl(BASELINE, BOM, LOCKED, {}, driver_prices_by_month=partial)
    by_month = {r["month"]: r["delta_cogs_eur"] for r in out["rows"]}
    assert by_month["2027-01"] == pytest.approx(20_000.0)
    assert by_month["2027-02"] == pytest.approx(0.0)
    assert by_month["2027-03"] == pytest.approx(0.0)


def test_a_stated_percentage_compounds_on_top_of_the_curve():
    """The two are different claims — "the market is here" and "and I think it
    goes 10% further" — so they multiply rather than one replacing the other."""
    flat = {r["month"]: {"wheat": 300.0} for r in BASELINE}
    out = budget.project_pnl(BASELINE, BOM, LOCKED, {"wheat": 10.0},
                             driver_prices_by_month=flat)
    # 300 × 1.10 = 330, i.e. +80 on the 250 lock, × 0.4 × 1000 = 32 000 a month.
    assert out["rows"][0]["delta_cogs_eur"] == pytest.approx(32_000.0)


def test_hedging_still_damps_a_curve_move():
    flat = {r["month"]: {"wheat": 300.0} for r in BASELINE}
    out = budget.project_pnl(BASELINE, BOM, LOCKED, {}, hedges={"wheat": 0.75},
                             driver_prices_by_month=flat)
    assert out["rows"][0]["delta_cogs_eur"] == pytest.approx(5_000.0)


def test_the_ebitda_bridge_still_closes_on_a_curve():
    """The same additivity discipline as everywhere else: a bridge that does not
    close is one nobody can defend to a board."""
    shaped = {"2027-01": {"wheat": 310.0}, "2027-02": {"wheat": 240.0},
              "2027-03": {"wheat": 275.0}}
    out = budget.project_pnl(
        BASELINE, BOM, LOCKED,
        {"volume": {"*": 5.0}, "price": {"Poultry Feed": 2.0}, "drivers": {"wheat": 3.0}},
        price_pass_through=0.4, driver_prices_by_month=shaped)
    bridge = out["ebitda_bridge"]
    assert bridge["check_residual"] == pytest.approx(0.0, abs=1e-6)
    assert (bridge["projected_ebitda_eur"] - bridge["baseline_ebitda_eur"]
            == pytest.approx(bridge["total_eur"]))

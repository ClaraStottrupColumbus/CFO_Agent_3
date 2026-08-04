# Budget versions: the freeze, the approval state machine, and the diff.
#
# The diff is the part that has to survive a board room — "the €2.4M swing came
# from re-locking chicken meal on 12 Nov" — so most of this file is about it
# being complete (nothing silently dropped) and additive (the bridge closes).
#
# Storage tests redirect VERSIONS_FILE at a tmp_path, the same monkeypatch
# discipline as tests/test_rules.py.

import pytest

from app import budgetversions


TOL = 1e-6


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(budgetversions, "VERSIONS_FILE", tmp_path / "budget_versions.json")
    return tmp_path


def scenario(name="Base case", *, assumptions=None, ebitda=1_000_000.0,
             revenue=10_000_000.0, bridge=None, lines=None, sid="sc1"):
    return {
        "id": sid,
        "name": name,
        "baseline": "budget_2027",
        "assumptions": assumptions if assumptions is not None else {"chicken_meal": 5.0},
        "price_pass_through": 0.35,
        "opex_inflation_pct": 2.0,
        "totals": {"volume_tonnes": 150_000.0, "revenue_eur": revenue,
                   "cogs_eur": revenue - ebitda - 500_000.0, "opex_eur": 500_000.0,
                   "gross_margin_eur": revenue - (revenue - ebitda - 500_000.0),
                   "ebitda_eur": ebitda,
                   "ebitda_margin_pct": ebitda / revenue * 100.0},
        "by_month": [{"month": "2027-01", "revenue_eur": revenue / 12,
                      "ebitda_eur": ebitda / 12}],
        "by_product_line": lines or [
            {"product_line": "Aqua Feed", "volume_tonnes": 100_000.0,
             "revenue_eur": revenue * 0.6, "cogs_eur": revenue * 0.5,
             "opex_eur": 300_000.0, "ebitda_eur": ebitda * 0.6},
        ],
        "ebitda_bridge": bridge if bridge is not None else {
            "baseline_ebitda_eur": 1_200_000.0, "projected_ebitda_eur": ebitda,
            "volume_eur": 0.0, "price_eur": 0.0, "opex_eur": -100_000.0,
            "drivers_eur": {"chicken_meal": ebitda - 1_200_000.0 + 100_000.0},
        },
    }


def driver_rows(chicken=420.0, freight=1800.0, *, url="https://ex.example/meal"):
    return [
        {"driver_id": "chicken_meal", "name": "Chicken meal", "unit": "EUR/t",
         "value": chicken, "source_url": url, "retrieved_at": "2026-11-12T09:00:00+00:00",
         "locked_at": "2026-11-12T10:00:00+00:00", "rationale": "September lock"},
        {"driver_id": "sea_freight", "name": "Sea freight", "unit": "EUR/FEU",
         "value": freight, "source_url": "https://ex.example/freight",
         "retrieved_at": "2026-11-12T09:00:00+00:00", "locked_at": None,
         "rationale": None},
    ]


# ---------- Creating and numbering ----------

def test_versions_number_from_one_and_chain_to_their_parent(store):
    first = budgetversions.create_version(scenario(), label="Opening plan")
    second = budgetversions.create_version(scenario("Re-lock"), label="November re-lock")

    assert (first["version_no"], second["version_no"]) == (1, 2)
    assert first["parent_version_id"] is None
    assert second["parent_version_id"] == first["id"]
    assert first["status"] == "draft"
    # Newest first, so a rail renders in the order a CFO reads it.
    assert [v["version_no"] for v in budgetversions.list_versions()] == [2, 1]


def test_a_version_freezes_the_provenance_it_was_created_with(store):
    version = budgetversions.create_version(
        scenario(), drivers_snapshot=driver_rows(), locked_at="2026-11-12T10:00:00+00:00")
    # Later research must not be able to rewrite what was approved: the snapshot
    # is a copy, not a reference.
    stored = budgetversions.get_version(version["id"])
    assert stored["drivers_snapshot"][0]["value"] == 420.0
    assert stored["drivers_snapshot"][0]["source_url"] == "https://ex.example/meal"
    assert stored["locked_at"] == "2026-11-12T10:00:00+00:00"


def test_a_version_needs_a_stored_scenario(store):
    with pytest.raises(ValueError):
        budgetversions.create_version({"name": "unsaved"})


def test_a_legacy_flat_assumption_snapshot_lifts_on_read(store):
    # Same hazard as scenarios.py: read as an empty drivers block, a diff would
    # show no assumption change at all.
    budgetversions.create_version(scenario(assumptions={"chicken_meal": 8.0}))
    stored = budgetversions.list_versions()[0]
    assert stored["assumptions_snapshot"]["drivers"] == {"chicken_meal": 8.0}
    assert stored["assumptions_snapshot"]["volume"] == {}


# ---------- The approval state machine ----------

def test_submit_then_approve_records_the_attestation(store):
    version = budgetversions.create_version(scenario())
    budgetversions.submit(version["id"], "N. Bentmann")
    approved = budgetversions.approve(version["id"], "A. Board")

    assert approved["status"] == "approved"
    assert approved["submitted_by"] == "N. Bentmann"
    assert approved["approved_by"] == "A. Board"
    assert approved["approved_at"] is not None


def test_approval_requires_a_name_because_there_is_no_authentication(store):
    version = budgetversions.create_version(scenario())
    with pytest.raises(ValueError, match="approved_by"):
        budgetversions.approve(version["id"], "   ")


def test_approving_a_second_version_supersedes_the_first(store):
    first = budgetversions.create_version(scenario())
    second = budgetversions.create_version(scenario("Re-lock"))
    budgetversions.approve(first["id"], "A. Board")
    budgetversions.approve(second["id"], "A. Board")

    # At most one approved version — otherwise "what are we running on?" has two
    # answers.
    assert budgetversions.get_version(first["id"])["status"] == "superseded"
    assert budgetversions.approved_version()["id"] == second["id"]


def test_an_approved_version_cannot_be_re_approved_or_re_submitted(store):
    version = budgetversions.create_version(scenario())
    budgetversions.approve(version["id"], "A. Board")
    with pytest.raises(ValueError):
        budgetversions.approve(version["id"], "A. Board")
    with pytest.raises(ValueError):
        budgetversions.submit(version["id"], "A. Board")


def test_transitions_on_a_missing_version_raise_keyerror(store):
    with pytest.raises(KeyError):
        budgetversions.approve("nope", "A. Board")


# ---------- Which scenario an approval freezes ----------
#
# The CFO can edit a scenario by hand, EXCEPT the one an approved version was
# frozen from: the approved budget has to keep saying what was approved. This is
# the lookup behind that, and it lives here because a version knows what it froze
# while a scenario knows nothing about versions.

def test_an_approved_version_freezes_the_scenario_it_was_made_from(store):
    version = budgetversions.create_version(scenario(sid="sc1"))
    budgetversions.approve(version["id"], "A. Board")

    frozen = budgetversions.approved_freeze()
    assert frozen["scenario_id"] == "sc1"
    assert frozen["version_no"] == 1
    assert frozen["approved_by"] == "A. Board"
    assert budgetversions.approved_freeze_of("sc1") == frozen


def test_a_draft_or_submitted_version_does_not_freeze_its_scenario(store):
    # A budget under review is still being explored. Only an approval is a claim
    # the company has committed to those numbers.
    draft = budgetversions.create_version(scenario(sid="sc1"))
    assert budgetversions.approved_freeze() is None
    budgetversions.submit(draft["id"], "N. Bentmann")
    assert budgetversions.approved_freeze() is None
    assert budgetversions.approved_freeze_of("sc1") is None


def test_superseding_hands_the_older_scenario_back(store):
    first = budgetversions.create_version(scenario(sid="sc1"))
    second = budgetversions.create_version(scenario("Re-lock", sid="sc2"))
    budgetversions.approve(first["id"], "A. Board")
    budgetversions.approve(second["id"], "A. Board")

    # At most one approved version, so at most one frozen scenario — sc1 becomes
    # editable again the moment its version is superseded.
    assert budgetversions.approved_freeze_of("sc1") is None
    assert budgetversions.approved_freeze_of("sc2")["version_no"] == 2


def test_approved_freeze_is_none_with_no_versions_and_for_unknown_ids(store):
    assert budgetversions.approved_freeze() is None
    assert budgetversions.approved_freeze_of("sc1") is None

    version = budgetversions.create_version(scenario(sid="sc1"))
    budgetversions.approve(version["id"], "A. Board")
    assert budgetversions.approved_freeze_of("sc-other") is None
    assert budgetversions.approved_freeze_of(None) is None
    assert budgetversions.approved_freeze_of("") is None


# ---------- The diff ----------

def build(a_kwargs, b_kwargs, *, a_drivers=None, b_drivers=None):
    a = {"id": "a", "version_no": 1, "label": "v1", "status": "superseded",
         "drivers_snapshot": a_drivers or [], **a_kwargs}
    b = {"id": "b", "version_no": 2, "label": "v2", "status": "draft",
         "drivers_snapshot": b_drivers or [], **b_kwargs}
    return budgetversions.diff(a, b)


def snap(sc):
    """A scenario as a version record would hold it."""
    return {"assumptions_snapshot": sc["assumptions"], "totals": sc["totals"],
            "by_product_line": sc["by_product_line"],
            "ebitda_bridge": sc["ebitda_bridge"]}


def test_the_ebitda_bridge_between_versions_closes():
    a = snap(scenario(ebitda=1_000_000.0))
    b = snap(scenario(ebitda=1_400_000.0, assumptions={"chicken_meal": 2.0}))
    d = build(a, b)

    assert d["ebitda_delta_eur"] == pytest.approx(400_000.0, abs=TOL)
    # The same additivity discipline as variance_decomposition's check_residual:
    # a bridge that does not close is one nobody can defend.
    assert d["check_residual"] == pytest.approx(0.0, abs=0.01)
    steps = sum(p["value"] for p in d["ebitda_bridge"] if p["kind"] == "delta")
    assert steps == pytest.approx(400_000.0, abs=0.01)
    ends = [p for p in d["ebitda_bridge"] if p["kind"] == "absolute"]
    assert (ends[0]["value"], ends[-1]["value"]) == (1_000_000.0, 1_400_000.0)


def test_the_bridge_names_the_driver_that_moved():
    a = snap(scenario(ebitda=1_000_000.0))
    b = snap(scenario(ebitda=1_400_000.0))
    labels = {p["label"]: p["value"] for p in build(a, b)["ebitda_bridge"]}
    assert labels["chicken_meal"] == pytest.approx(400_000.0, abs=0.01)


def test_a_version_with_no_stored_bridge_still_diffs_and_still_closes():
    # Versions written before the bridge existed must not silently drop the move.
    a = snap(scenario(ebitda=1_000_000.0, bridge={}))
    b = snap(scenario(ebitda=900_000.0, bridge={}))
    d = build(a, b)
    assert d["check_residual"] == pytest.approx(0.0, abs=0.01)
    assert any(p["label"] == "Unattributed" for p in d["ebitda_bridge"])


def test_a_re_cut_baseline_gets_its_own_step_rather_than_being_smeared():
    a = snap(scenario(ebitda=1_000_000.0))
    b_scenario = scenario(ebitda=1_000_000.0)
    b_scenario["ebitda_bridge"]["baseline_ebitda_eur"] = 1_500_000.0
    b_scenario["ebitda_bridge"]["drivers_eur"] = {"chicken_meal": -400_000.0}
    d = build(a, snap(b_scenario))
    labels = {p["label"]: p["value"] for p in d["ebitda_bridge"]}
    assert labels["Baseline plan"] == pytest.approx(300_000.0, abs=0.01)
    assert d["check_residual"] == pytest.approx(0.0, abs=0.01)


def test_assumption_changes_include_added_removed_and_changed():
    a = snap(scenario(assumptions={"drivers": {"chicken_meal": 5.0, "wheat": -8.0},
                                   "volume": {}}))
    b = snap(scenario(assumptions={"drivers": {"chicken_meal": 12.0},
                                   "volume": {"Aqua Feed": -15.0}}))
    d = build(a, b)
    by_key = {row["key"]: row for row in d["assumptions"]["drivers"]}

    assert by_key["chicken_meal"]["change"] == "changed"
    assert by_key["chicken_meal"]["delta_pp"] == pytest.approx(7.0, abs=TOL)
    # A dropped assumption is a change, not silence.
    assert by_key["wheat"]["change"] == "removed"
    assert d["assumptions"]["volume"][0] == {
        "key": "Aqua Feed", "from_pct": None, "to_pct": -15.0,
        "delta_pp": -15.0, "change": "added"}
    assert d["assumption_change_count"] == 3


def test_an_unchanged_assumption_is_not_reported_as_a_change():
    a = snap(scenario(assumptions={"chicken_meal": 5.0}))
    b = snap(scenario(assumptions={"chicken_meal": 5.0}))
    assert build(a, b)["assumption_change_count"] == 0


def test_a_re_lock_shows_up_even_when_the_percentages_are_identical():
    # The whole point of the locked_values cut: same assumption, different price
    # underneath it, and the budget moved.
    a = snap(scenario(assumptions={"chicken_meal": 5.0}, ebitda=1_000_000.0))
    b = snap(scenario(assumptions={"chicken_meal": 5.0}, ebitda=800_000.0))
    d = build(a, b, a_drivers=driver_rows(chicken=420.0),
              b_drivers=driver_rows(chicken=525.0, url="https://ex.example/nov"))

    assert d["assumption_change_count"] == 0
    row = next(r for r in d["locked_values"] if r["driver_id"] == "chicken_meal")
    assert row["from_value"] == 420.0 and row["to_value"] == 525.0
    assert row["delta_pct"] == pytest.approx(25.0, abs=TOL)
    # The provenance of the NEW value travels with the diff.
    assert row["to_source_url"] == "https://ex.example/nov"
    # A driver that did not move is not listed.
    assert [r["driver_id"] for r in d["locked_values"]] == ["chicken_meal"]


def test_totals_and_product_lines_carry_deltas():
    a = snap(scenario(ebitda=1_000_000.0, revenue=10_000_000.0))
    b = snap(scenario(ebitda=1_400_000.0, revenue=11_000_000.0))
    d = build(a, b)

    assert d["totals"]["ebitda_eur"]["delta"] == pytest.approx(400_000.0, abs=TOL)
    assert d["totals"]["revenue_eur"]["delta"] == pytest.approx(1_000_000.0, abs=TOL)
    line = d["by_product_line"][0]
    assert line["product_line"] == "Aqua Feed"
    assert line["ebitda_eur_delta"] == pytest.approx(240_000.0, abs=TOL)


def test_diff_survives_empty_records():
    d = budgetversions.diff({}, {})
    assert d["ebitda_delta_eur"] == 0.0
    assert d["assumption_change_count"] == 0
    assert d["check_residual"] == pytest.approx(0.0, abs=TOL)

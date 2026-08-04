# The scenario store's replace-by-id path.
#
# It was latent until the CFO's edit form landed: `build_budget_scenario` now
# passes an `id` when recomputing an existing scenario in place, so "overwrite,
# don't append" and "an edit must not move the active flag" stopped being
# properties nobody depended on. Both are pinned here.
#
# SCENARIOS_FILE is redirected at a tmp_path — the same monkeypatch discipline as
# tests/test_rules.py — so nothing here reads a dataset or needs pandas.

import pytest

from app import scenarios


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(scenarios, "SCENARIOS_FILE", tmp_path / "scenarios.json")
    return tmp_path


def two_scenarios():
    """One active, one not.

    Two, deliberately: with a single scenario the "first one saved is always
    active" rule would make every active-flag assertion pass for the wrong
    reason.
    """
    first = scenarios.save_scenario({"name": "2027 budget (as locked)",
                                     "assumptions": {}, "active": True})
    second = scenarios.save_scenario({"name": "Freight holds all year",
                                      "assumptions": {"sea_freight": 12.0}})
    return first, second


# ---------- Replace, don't append ----------

def test_replacing_by_id_overwrites_in_place_and_keeps_created_at(store):
    first, _ = two_scenarios()
    replaced = scenarios.save_scenario({
        "id": first["id"], "name": "2027 budget (edited)",
        "assumptions": {"drivers": {"chicken_meal": 8.0}},
        "totals": {"ebitda_eur": 11_400_000.0}})

    assert replaced["id"] == first["id"]
    assert replaced["created_at"] == first["created_at"]
    assert len(scenarios.list_scenarios()) == 2          # not three
    stored = scenarios.get_scenario(first["id"])
    assert stored["name"] == "2027 budget (edited)"
    assert stored["assumptions"]["drivers"] == {"chicken_meal": 8.0}
    assert stored["totals"]["ebitda_eur"] == 11_400_000.0


def test_updated_at_is_bumped_on_replace(store):
    first, _ = two_scenarios()
    replaced = scenarios.save_scenario({"id": first["id"], "name": first["name"]})
    assert replaced["updated_at"] >= first["updated_at"]
    # list_scenarios sorts by updated_at desc, so the edited one leads.
    assert scenarios.list_scenarios()[0]["id"] == first["id"]


# ---------- The active flag: asserted, never denied ----------

def test_an_edit_that_omits_active_leaves_the_active_scenario_active(store):
    active, other = two_scenarios()
    scenarios.save_scenario({"id": active["id"], "name": "Edited",
                             "assumptions": {"drivers": {"wheat": -4.0}}})

    # The whole hazard: an edit that passed active=False would quietly take the
    # budget off the scenario the CFO was only correcting.
    assert scenarios.get_scenario(active["id"])["active"] is True
    assert scenarios.get_scenario(other["id"])["active"] is False
    assert scenarios.get_active()["id"] == active["id"]


def test_editing_a_saved_scenario_does_not_steal_active_from_the_active_one(store):
    active, other = two_scenarios()
    scenarios.save_scenario({"id": other["id"], "name": "Freight, edited",
                             "assumptions": {"drivers": {"sea_freight": 20.0}}})

    assert scenarios.get_scenario(other["id"])["active"] is False
    assert scenarios.get_active()["id"] == active["id"]


def test_active_true_on_replace_moves_the_flag_and_deactivates_the_others(store):
    active, other = two_scenarios()
    scenarios.save_scenario({"id": other["id"], "name": other["name"], "active": True})

    assert scenarios.get_scenario(other["id"])["active"] is True
    assert scenarios.get_scenario(active["id"])["active"] is False


def test_the_first_scenario_saved_is_active_even_without_being_asked(store):
    # The rule the omission logic above sits beside: the monthly revision and the
    # EBITDA-floor rule must always have something to evaluate.
    only = scenarios.save_scenario({"name": "Only one", "assumptions": {}})
    assert only["active"] is True


# ---------- Legacy shapes survive an edit ----------

def test_a_legacy_flat_assumption_dict_lifts_to_blocks_on_replace(store):
    first, _ = two_scenarios()
    replaced = scenarios.save_scenario({"id": first["id"], "name": first["name"],
                                        "assumptions": {"chicken_meal": 15.0}})
    assert replaced["assumptions"]["drivers"] == {"chicken_meal": 15.0}
    assert replaced["assumptions"]["volume"] == {}
    assert scenarios.summary(replaced)["assumption_count"] == 1

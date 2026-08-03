# The headline test. The four effects must sum EXACTLY to the total delta —
# that additivity identity is what makes the decomposition trustworthy, and
# hiding the joint residual inside the price effect is how variance
# decompositions silently drift.

import pytest

from app.budget import variance_decomposition

TOL = 1e-6


def rows(*triples):
    return [{"entity": e, "price": p, "volume": q} for e, p, q in triples]


def assert_additive(res):
    """price + volume + mix + joint == total delta."""
    total = (res["price_effect"] + res["volume_effect"]
             + res["mix_effect"] + res["joint_effect"])
    assert res["total_delta"] == pytest.approx(total, abs=TOL)
    assert res["check_residual"] == pytest.approx(0.0, abs=TOL)


# ---------- The identity ----------

def test_additivity_on_a_mixed_move():
    base = rows(("Poultry", 400.0, 1000.0), ("Swine", 350.0, 800.0), ("Premium", 900.0, 200.0))
    curr = rows(("Poultry", 436.0, 1050.0), ("Swine", 330.0, 700.0), ("Premium", 915.0, 260.0))
    res = variance_decomposition(base, curr)
    assert_additive(res)
    assert res["total_delta"] == pytest.approx(res["current_value"] - res["base_value"], abs=TOL)


def test_additivity_holds_when_a_line_is_launched_or_discontinued():
    base = rows(("Poultry", 400.0, 1000.0), ("Legacy", 300.0, 500.0))
    curr = rows(("Poultry", 420.0, 1100.0), ("NewLine", 700.0, 150.0))
    res = variance_decomposition(base, curr)
    assert_additive(res)
    # A launched/discontinued line carries its price across, so it is a
    # volume/mix story rather than a spurious +/-100% price move.
    assert res["price_effect"] == pytest.approx(20.0 * 1000.0, abs=TOL)


def test_additivity_with_many_lines_and_awkward_numbers():
    base = rows(*[(f"L{i}", 100.0 + i * 7.3, 50.0 + i * 11.1) for i in range(12)])
    curr = rows(*[(f"L{i}", 100.0 + i * 6.1, 60.0 - i * 0.7) for i in range(12)])
    assert_additive(variance_decomposition(base, curr))


# ---------- Each effect isolated ----------

def test_pure_price_move_gives_zero_volume_and_zero_mix():
    base = rows(("Poultry", 400.0, 1000.0), ("Swine", 350.0, 500.0))
    curr = rows(("Poultry", 440.0, 1000.0), ("Swine", 385.0, 500.0))
    res = variance_decomposition(base, curr)
    assert res["volume_effect"] == pytest.approx(0.0, abs=TOL)
    assert res["mix_effect"] == pytest.approx(0.0, abs=TOL)
    assert res["joint_effect"] == pytest.approx(0.0, abs=TOL)   # volumes unchanged
    assert res["price_effect"] == pytest.approx(40.0 * 1000 + 35.0 * 500, abs=TOL)
    assert_additive(res)


def test_mix_only_shift_gives_zero_price_and_zero_volume():
    # Same total volume, same prices — only the split between lines changes.
    base = rows(("Poultry", 400.0, 1000.0), ("Premium", 900.0, 200.0))
    curr = rows(("Poultry", 400.0, 900.0), ("Premium", 900.0, 300.0))
    res = variance_decomposition(base, curr)
    assert res["price_effect"] == pytest.approx(0.0, abs=TOL)
    assert res["volume_effect"] == pytest.approx(0.0, abs=TOL)   # Q unchanged
    assert res["joint_effect"] == pytest.approx(0.0, abs=TOL)    # no price move
    assert res["mix_effect"] == pytest.approx(res["total_delta"], abs=TOL)
    assert res["mix_effect"] > 0                                 # shifted to the dearer line
    assert_additive(res)


def test_pure_volume_scaling_gives_zero_mix_and_zero_price():
    # Every line scaled by the same factor: volume moves, mix does not.
    base = rows(("Poultry", 400.0, 1000.0), ("Premium", 900.0, 200.0))
    curr = rows(("Poultry", 400.0, 1200.0), ("Premium", 900.0, 240.0))
    res = variance_decomposition(base, curr)
    assert res["price_effect"] == pytest.approx(0.0, abs=TOL)
    assert res["mix_effect"] == pytest.approx(0.0, abs=TOL)
    assert res["volume_effect"] == pytest.approx(res["total_delta"], abs=TOL)
    assert_additive(res)


def test_joint_effect_is_reported_not_folded_into_price():
    # Price and volume both move on one line, so the interaction is non-zero.
    base = rows(("Poultry", 400.0, 1000.0))
    curr = rows(("Poultry", 450.0, 1200.0))
    res = variance_decomposition(base, curr)
    assert res["joint_effect"] == pytest.approx(50.0 * 200.0, abs=TOL)
    assert res["price_effect"] == pytest.approx(50.0 * 1000.0, abs=TOL)
    assert_additive(res)


# ---------- Degenerate input returns an error dict, never raises ----------

def test_zero_base_volume_returns_error_not_zero_division():
    base = rows(("Poultry", 400.0, 0.0))
    curr = rows(("Poultry", 420.0, 100.0))
    res = variance_decomposition(base, curr)
    assert "error" in res
    assert "base" in res["error"].lower()


def test_missing_volume_column_is_treated_as_zero_volume():
    base = [{"entity": "Poultry", "price": 400.0}]        # no volume key at all
    curr = rows(("Poultry", 420.0, 100.0))
    res = variance_decomposition(base, curr)
    assert "error" in res


def test_empty_input_returns_error():
    assert "error" in variance_decomposition([], [])


def test_non_numeric_price_returns_teachable_error():
    base = [{"entity": "Poultry", "price": "n/a", "volume": 10}]
    res = variance_decomposition(base, rows(("Poultry", 5.0, 10.0)))
    assert "error" in res
    assert "Poultry" in res["error"]


def test_missing_key_column_names_the_columns_present():
    res = variance_decomposition([{"price": 1.0, "volume": 1.0}], [])
    assert "error" in res
    assert "entity" in res["error"]


# ---------- Row aggregation ----------

def test_duplicate_entities_are_combined_volume_weighted():
    # Same product line split across two regions must equal the pre-summed case.
    split = [{"entity": "Poultry", "price": 400.0, "volume": 600.0},
             {"entity": "Poultry", "price": 450.0, "volume": 400.0}]
    curr = rows(("Poultry", 500.0, 1000.0))
    res = variance_decomposition(split, curr)
    # Volume-weighted base price: (400*600 + 450*400) / 1000 = 420
    assert res["base_avg_price"] == pytest.approx(420.0, abs=TOL)
    assert res["base_value"] == pytest.approx(420_000.0, abs=TOL)
    assert_additive(res)


def test_custom_column_names_are_honoured():
    base = [{"product": "A", "unit_cost": 10.0, "tonnes": 100.0}]
    curr = [{"product": "A", "unit_cost": 12.0, "tonnes": 100.0}]
    res = variance_decomposition(base, curr, key="product",
                                 price_key="unit_cost", volume_key="tonnes")
    assert res["price_effect"] == pytest.approx(200.0, abs=TOL)
    assert_additive(res)

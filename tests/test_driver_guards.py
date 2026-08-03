# The provenance boundary (§4). record_driver_observation is the one place in
# this system where a model mistake becomes durable, wrong data, so its two
# guards are pure predicates and this is their coverage.
#
# The case that matters most is the fails-CLOSED one: if verify_source_url
# compared raw URLs while citations.py stored normalised ones, every legitimate
# observation would be refused, the agent would report that it cannot verify its
# own sources, and nothing in the logs would point at the comparison.

import pytest

from app.citations import normalise_url
from app.drivers import check_sanity_band, verify_source_url

VISITED = {normalise_url("https://www.example.com/prices/chicken-meal")}


# ---------- verify_source_url ----------

def test_exact_visited_url_is_accepted():
    assert verify_source_url("https://www.example.com/prices/chicken-meal", VISITED)


def test_url_absent_from_the_set_is_refused():
    assert not verify_source_url("https://elsewhere.example/prices", VISITED)


def test_empty_visited_set_refuses_everything():
    assert not verify_source_url("https://www.example.com/prices/chicken-meal", set())
    assert not verify_source_url("https://www.example.com/prices/chicken-meal", None)


def test_missing_url_is_refused():
    assert not verify_source_url(None, VISITED)
    assert not verify_source_url("", VISITED)
    assert not verify_source_url("   ", VISITED)


# The fails-closed cases: each of these differs from the visited URL only in a
# way normalise_url is supposed to erase, and each MUST still verify.

@pytest.mark.parametrize("variant", [
    "https://www.example.com/prices/chicken-meal/",            # trailing slash
    "https://WWW.EXAMPLE.COM/prices/chicken-meal",             # host casing
    "https://example.com/prices/chicken-meal",                 # no www.
    "https://www.example.com/prices/chicken-meal#latest",      # fragment
    "https://www.example.com/prices/chicken-meal?utm_source=x",  # utm param
    "https://www.example.com/prices/chicken-meal?ref=newsletter",  # ref param
    "https://www.example.com:443/prices/chicken-meal",         # default port
    "  https://www.example.com/prices/chicken-meal  ",         # surrounding space
])
def test_normalisation_equivalent_urls_are_accepted(variant):
    assert verify_source_url(variant, VISITED)


def test_a_different_path_is_still_refused():
    # Normalisation must not be so aggressive that it accepts the wrong page.
    assert not verify_source_url("https://www.example.com/prices/wheat", VISITED)


def test_a_meaningful_query_parameter_is_not_stripped():
    visited = {normalise_url("https://example.com/series?id=42")}
    assert verify_source_url("https://example.com/series?id=42", visited)
    assert not verify_source_url("https://example.com/series?id=99", visited)


def test_the_visited_set_may_hold_raw_urls_and_still_match():
    # Both sides go through normalise_url, so an un-normalised set still works.
    raw = {"https://WWW.Example.com/prices/chicken-meal/"}
    assert verify_source_url("https://example.com/prices/chicken-meal", raw)


# ---------- check_sanity_band ----------

PREV = 100.0


def test_a_normal_move_is_accepted():
    assert check_sanity_band(112.0, PREV) is None


def test_accepted_at_exactly_the_band_edges():
    assert check_sanity_band(20.0, PREV) is None    # exactly 0.2x
    assert check_sanity_band(500.0, PREV) is None   # exactly 5.0x


def test_refused_just_outside_the_band():
    assert check_sanity_band(19.99, PREV) is not None
    assert check_sanity_band(500.01, PREV) is not None


def test_the_rejection_names_the_previous_value_so_the_error_is_teachable():
    rejection = check_sanity_band(5000.0, PREV)
    assert rejection is not None
    assert "100" in rejection["error"]
    assert rejection["previous_value"] == PREV
    assert rejection["rejected_value"] == 5000.0
    assert "override_sanity_check" in rejection["error"]


def test_override_admits_a_genuine_spike():
    assert check_sanity_band(5000.0, PREV, override=True) is None


def test_no_previous_value_is_accepted_rather_than_crashing():
    # A first observation has nothing to compare against; refusing it would make
    # a newly added driver impossible to seed.
    assert check_sanity_band(412.0, None) is None


def test_a_zero_or_negative_previous_value_is_not_used_as_a_band():
    assert check_sanity_band(412.0, 0.0) is None
    assert check_sanity_band(412.0, -5.0) is None


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_non_positive_prices_are_always_refused(bad):
    rejection = check_sanity_band(bad, PREV)
    assert rejection is not None
    assert "greater than zero" in rejection["error"]


def test_non_positive_price_is_refused_even_with_override():
    assert check_sanity_band(0.0, PREV, override=True) is not None


@pytest.mark.parametrize("bad", ["n/a", None, "", [1]])
def test_non_numeric_prices_are_refused(bad):
    rejection = check_sanity_band(bad, PREV)
    assert rejection is not None
    assert "number" in rejection["error"]


def test_non_finite_prices_are_refused():
    assert check_sanity_band(float("inf"), PREV) is not None
    assert check_sanity_band(float("nan"), PREV) is not None


def test_a_non_numeric_previous_value_does_not_crash_the_guard():
    assert check_sanity_band(412.0, "unknown") is None

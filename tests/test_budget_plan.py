# Budget Outlook's arithmetic and its persistence.
#
# The whole page is one function — derive() — plus a validator and a JSON file,
# so this is where the feature is actually verified. The ranking test is the
# important one: "ranked by materiality" is the product claim, and the failure
# mode is silent (an alphabetical or amount-ordered list still *looks* right).
#
# The plan file is a module-level path constant purely so it can be redirected
# at tmp_path here, following tests/test_rules.py.

import json

import pytest

from app import budgetplan as bp

TOL = 1e-6


@pytest.fixture(autouse=True)
def plan_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bp, "PLAN_FILE", tmp_path / "budget_plan.json")
    return tmp_path / "budget_plan.json"


def make_plan(variables, *, revenue=1_000_000.0, revenue_pct=0.0):
    """A configured plan carrying exactly the variables a test cares about."""
    plan = bp.default_plan()
    plan["configured"] = True
    plan["company"] = {"name": "Testco", "industry": "other", "size": "mid",
                       "currency": "EUR", "fiscal_year_start_month": 1}
    plan["baseline"] = {"current_year": 2026, "budget_year": 2027,
                        "revenue": revenue, "revenue_change_pct": revenue_pct}
    plan["variables"] = [
        {"id": v[0], "label": v[0].replace("_", " ").title(), "category": "other",
         "description": "", "current_amount": v[1], "expected_change_pct": v[2],
         "assumption": "", "default_note": "", "include": v[3] if len(v) > 3 else True}
        for v in variables
    ]
    return plan


# ---------- Ranking: the product claim ----------

def test_ranking_is_by_materiality_not_by_amount():
    # 'payroll' is 20x bigger but barely moves; 'freight' is small and moves a
    # lot. Materiality — share of the cost base x expected change — puts freight
    # first, and 40,000 > 10,000 confirms it is the delta doing the ordering.
    plan = make_plan([("payroll", 1_000_000.0, 1.0),
                      ("freight", 100_000.0, 40.0)])
    ranked = bp.derive(plan)["ranked"]
    assert [r["id"] for r in ranked] == ["freight", "payroll"]
    assert ranked[0]["delta"] == pytest.approx(40_000.0, rel=TOL)
    assert ranked[1]["delta"] == pytest.approx(10_000.0, rel=TOL)


def test_ranking_is_not_alphabetical():
    plan = make_plan([("aaa_tiny", 1_000.0, 1.0),
                      ("zzz_huge", 500_000.0, 10.0)])
    assert [r["id"] for r in bp.derive(plan)["ranked"]] == ["zzz_huge", "aaa_tiny"]


def test_a_big_percentage_on_a_trivial_line_does_not_outrank_the_payroll():
    # The specific trap the formula exists to avoid: sorting on the percentage
    # would put stationery (+90%) above payroll (+3%).
    plan = make_plan([("payroll", 2_000_000.0, 3.0),
                      ("stationery", 5_000.0, 90.0)])
    ranked = bp.derive(plan)["ranked"]
    assert ranked[0]["id"] == "payroll"
    assert ranked[0]["expected_change_pct"] < ranked[1]["expected_change_pct"]


def test_ties_are_broken_deterministically():
    # Two identical movements. Whatever the order is, it must not change between
    # calls — a list that reshuffles on refresh reads as the data moving.
    plan = make_plan([("beta", 100_000.0, 5.0), ("alpha", 100_000.0, 5.0)])
    first = [r["id"] for r in bp.derive(plan)["ranked"]]
    second = [r["id"] for r in bp.derive(plan)["ranked"]]
    assert first == second == ["alpha", "beta"]


def test_rank_numbers_are_contiguous_from_one():
    plan = make_plan([(f"v{i}", 1000.0 * (i + 1), 2.0) for i in range(12)])
    ranked = bp.derive(plan)["ranked"]
    assert [r["rank"] for r in ranked] == list(range(1, 13))


def test_top_and_rest_partition_the_ranking():
    plan = make_plan([(f"v{i}", 1000.0 * (i + 1), 2.0) for i in range(12)])
    d = bp.derive(plan)
    assert len(d["top"]) == bp.TOP_N
    assert d["top"] + d["rest"] == d["ranked"]


# ---------- Additivity ----------

def test_line_deltas_sum_to_the_total_cost_movement():
    plan = make_plan([("a", 400_000.0, 3.5), ("b", 250_000.0, -2.0),
                      ("c", 90_000.0, 12.0), ("d", 10_000.0, 0.0)])
    d = bp.derive(plan)
    assert sum(r["delta"] for r in d["ranked"]) == pytest.approx(
        d["totals"]["cost_delta"], rel=TOL, abs=TOL)


def test_next_cost_base_is_the_current_base_plus_the_movement():
    plan = make_plan([("a", 400_000.0, 3.5), ("b", 250_000.0, -2.0)])
    t = bp.derive(plan)["totals"]
    assert t["cost_next"] == pytest.approx(t["cost_current"] + t["cost_delta"],
                                           rel=TOL, abs=TOL)


def test_impact_shares_sum_to_one():
    plan = make_plan([("a", 400_000.0, 3.5), ("b", 250_000.0, -2.0),
                      ("c", 90_000.0, 12.0)])
    ranked = bp.derive(plan)["ranked"]
    assert sum(r["impact_share"] for r in ranked) == pytest.approx(1.0, rel=TOL)


def test_margin_is_revenue_minus_cost_on_both_sides():
    plan = make_plan([("a", 600_000.0, 10.0)], revenue=1_000_000.0, revenue_pct=5.0)
    t = bp.derive(plan)["totals"]
    assert t["margin_current"] == pytest.approx(1_000_000.0 - 600_000.0, rel=TOL)
    assert t["margin_next"] == pytest.approx(1_050_000.0 - 660_000.0, rel=TOL)
    assert t["margin_delta_pp"] == pytest.approx(
        t["margin_pct_next"] - t["margin_pct_current"], rel=TOL)


# ---------- Exclusion ----------

def test_excluded_variables_affect_neither_totals_nor_ranking():
    included_only = make_plan([("a", 400_000.0, 5.0)])
    with_excluded = make_plan([("a", 400_000.0, 5.0),
                               ("ghost", 9_000_000.0, 50.0, False)])
    a = bp.derive(included_only)
    b = bp.derive(with_excluded)
    assert [r["id"] for r in b["ranked"]] == ["a"]
    assert b["totals"]["cost_next"] == pytest.approx(a["totals"]["cost_next"], rel=TOL)
    assert b["totals"]["excluded_count"] == 1


# ---------- Direction ----------

@pytest.mark.parametrize("pct,expected", [
    (5.0, "up"), (-5.0, "down"), (0.0, "flat"),
    (0.04, "flat"),      # inside the epsilon
    (-0.04, "flat"),
    (0.06, "up"),        # just outside it
    (-0.06, "down"),
])
def test_direction_labels(pct, expected):
    plan = make_plan([("a", 100_000.0, pct)])
    assert bp.derive(plan)["ranked"][0]["direction"] == expected


def test_a_negative_zero_percentage_is_flat_not_down():
    plan = make_plan([("a", 100_000.0, -0.0)])
    assert bp.derive(plan)["ranked"][0]["direction"] == "flat"


# ---------- Empty / degenerate configurations ----------

def test_no_variables_does_not_divide_by_zero():
    d = bp.derive(make_plan([]))
    assert d["ranked"] == []
    assert d["totals"]["cost_current"] == 0.0
    assert d["totals"]["margin_pct_current"] == pytest.approx(100.0, rel=TOL)


def test_zero_revenue_does_not_divide_by_zero():
    d = bp.derive(make_plan([("a", 100.0, 5.0)], revenue=0.0))
    assert d["totals"]["margin_pct_current"] == 0.0
    assert d["totals"]["margin_pct_next"] == 0.0


def test_templated_narrative_survives_an_empty_configuration():
    text = bp.templated_narrative(make_plan([]))
    assert text.strip()
    assert "2027" in text


def test_templated_narrative_names_the_top_mover():
    plan = make_plan([("payroll", 1_000_000.0, 1.0), ("freight", 100_000.0, 40.0)])
    assert "Freight" in bp.templated_narrative(plan)


# ---------- Industry defaults ----------

def test_every_industry_ships_a_usable_default_set():
    for key in bp.INDUSTRIES:
        variables = bp.defaults_for(key, 1_000_000.0)
        assert variables, f"{key} has no defaults"
        assert all(v["label"] and v["description"] for v in variables), key
        assert all(v["current_amount"] > 0 for v in variables), key


def test_defaults_reference_only_real_catalogue_entries():
    for key, meta in bp.INDUSTRIES.items():
        for d in meta["defaults"]:
            assert d["id"] in bp.CATALOGUE, f"{key} -> {d['id']}"


def test_default_shares_leave_a_positive_margin():
    # A default set that spends more than 100% of revenue would open the page on
    # a loss for every new user.
    for key, meta in bp.INDUSTRIES.items():
        total = sum(d["share_of_revenue_pct"] for d in meta["defaults"])
        assert 0 < total < 100, f"{key} defaults to {total}% of revenue"


def test_an_unknown_industry_falls_back_rather_than_raising():
    fallback = bp.defaults_for("not_a_real_industry", 500_000.0)
    assert fallback == bp.defaults_for(bp.DEFAULT_INDUSTRY, 500_000.0)


def test_defaults_scale_with_revenue():
    small = bp.defaults_for("manufacturing", 1_000_000.0)
    big = bp.defaults_for("manufacturing", 10_000_000.0)
    assert big[0]["current_amount"] == pytest.approx(small[0]["current_amount"] * 10, rel=TOL)


def test_defaults_survive_a_nonsense_revenue():
    assert all(v["current_amount"] == 0 for v in bp.defaults_for("other", "not a number"))


# ---------- Fingerprint ----------

def test_fingerprint_is_stable_across_calls():
    plan = make_plan([("a", 400_000.0, 5.0)])
    assert bp.fingerprint(plan) == bp.fingerprint(plan)


def test_fingerprint_changes_when_an_included_percentage_changes():
    before = bp.fingerprint(make_plan([("a", 400_000.0, 5.0)]))
    after = bp.fingerprint(make_plan([("a", 400_000.0, 5.5)]))
    assert before != after


def test_fingerprint_changes_when_an_assumption_note_changes():
    # The note is not arithmetic, but it IS in the brief the model reads, so a
    # narrative written before the note existed is stale.
    plan = make_plan([("a", 400_000.0, 5.0)])
    before = bp.fingerprint(plan)
    plan["variables"][0]["assumption"] = "Renegotiated in Q3."
    assert bp.fingerprint(plan) != before


def test_fingerprint_ignores_an_excluded_variable():
    a = bp.fingerprint(make_plan([("a", 400_000.0, 5.0)]))
    b = bp.fingerprint(make_plan([("a", 400_000.0, 5.0),
                                  ("ghost", 1.0, 99.0, False)]))
    assert a == b


# ---------- Validation ----------

def valid_payload(**overrides):
    payload = {
        "company": {"name": "Testco", "industry": "other", "size": "mid",
                    "currency": "EUR", "fiscal_year_start_month": 1},
        "baseline": {"current_year": 2026, "revenue": 1_000_000.0,
                     "revenue_change_pct": 3.0},
        "variables": [{"id": "salaries", "label": "Salaries", "current_amount": 400_000.0,
                       "expected_change_pct": 4.0, "include": True}],
    }
    payload.update(overrides)
    return payload


def test_a_valid_payload_cleans_to_a_dict():
    cleaned = bp.validate(valid_payload())
    assert isinstance(cleaned, dict)
    assert cleaned["baseline"]["budget_year"] == 2027   # always current + 1


def test_missing_company_name_is_rejected():
    payload = valid_payload()
    payload["company"]["name"] = "   "
    assert bp.validate(payload) == "Company name is required."


def test_zero_revenue_is_rejected():
    payload = valid_payload()
    payload["baseline"]["revenue"] = 0
    assert "greater than 0" in bp.validate(payload)


def test_negative_revenue_is_rejected():
    payload = valid_payload()
    payload["baseline"]["revenue"] = -5
    assert "greater than 0" in bp.validate(payload)


def test_an_out_of_range_percentage_names_the_valid_range():
    payload = valid_payload()
    payload["variables"][0]["expected_change_pct"] = 250
    message = bp.validate(payload)
    assert "-100" in message and "100" in message and "Salaries" in message


def test_a_negative_amount_is_rejected():
    payload = valid_payload()
    payload["variables"][0]["current_amount"] = -1
    assert "negative" in bp.validate(payload)


def test_an_unknown_industry_enumerates_the_valid_options():
    payload = valid_payload()
    payload["company"]["industry"] = "space_mining"
    message = bp.validate(payload)
    assert "manufacturing" in message and "software_saas" in message


def test_duplicate_variable_ids_are_rejected():
    payload = valid_payload()
    payload["variables"] = payload["variables"] * 2
    assert "Duplicate" in bp.validate(payload)


def test_an_empty_variable_list_is_rejected():
    assert "at least one" in bp.validate(valid_payload(variables=[]))


def test_all_variables_excluded_is_rejected():
    payload = valid_payload()
    payload["variables"][0]["include"] = False
    assert "At least one variable must be included" in bp.validate(payload)


def test_a_bad_currency_code_is_rejected():
    payload = valid_payload()
    payload["company"]["currency"] = "Euros"
    assert "3-letter" in bp.validate(payload)


def test_a_fiscal_month_outside_the_year_is_rejected():
    payload = valid_payload()
    payload["company"]["fiscal_year_start_month"] = 13
    assert "between 1 and 12" in bp.validate(payload)


def test_a_variable_id_that_is_not_a_slug_is_rejected():
    payload = valid_payload()
    payload["variables"][0]["id"] = "Not A Slug!"
    assert "not valid" in bp.validate(payload)


def test_a_catalogue_variable_inherits_its_description():
    payload = valid_payload()
    payload["variables"][0]["description"] = ""
    cleaned = bp.validate(payload)
    assert cleaned["variables"][0]["description"] == bp.CATALOGUE["salaries"]["description"]


# ---------- Persistence ----------

def test_an_absent_file_reads_as_unconfigured():
    assert bp.get_plan()["configured"] is False


def test_save_config_marks_the_plan_configured_and_round_trips(plan_file):
    saved = bp.save_config(valid_payload())
    assert saved["configured"] is True
    assert plan_file.exists()
    assert bp.get_plan()["company"]["name"] == "Testco"
    assert json.loads(plan_file.read_text())["baseline"]["budget_year"] == 2027


def test_save_config_returns_the_error_string_and_writes_nothing(plan_file):
    payload = valid_payload()
    payload["company"]["name"] = ""
    assert isinstance(bp.save_config(payload), str)
    assert not plan_file.exists()


def test_saving_new_numbers_drops_a_narrative_written_against_the_old_ones():
    bp.save_config(valid_payload())
    plan = bp.get_plan()
    plan["narrative"] = {"text": "stale", "generated_at": 1, "fingerprint": "old", "model": "x"}
    bp._write(plan)

    payload = valid_payload()
    payload["variables"][0]["expected_change_pct"] = 9.0
    assert bp.save_config(payload)["narrative"]["text"] == ""


def test_resaving_identical_numbers_keeps_the_cached_narrative():
    bp.save_config(valid_payload())
    plan = bp.get_plan()
    fresh = {"text": "still true", "generated_at": 1,
             "fingerprint": bp.fingerprint(plan), "model": "x"}
    plan["narrative"] = fresh
    bp._write(plan)
    assert bp.save_config(valid_payload())["narrative"]["text"] == "still true"


def test_chat_appends_and_trims_to_the_cap():
    bp.save_config(valid_payload())
    for i in range(bp.MAX_CHAT_MESSAGES + 8):
        bp.append_chat("user", f"message {i}")
    chat = bp.get_plan()["chat"]
    assert len(chat) == bp.MAX_CHAT_MESSAGES
    assert chat[-1]["text"] == f"message {bp.MAX_CHAT_MESSAGES + 7}"


def test_clear_chat_leaves_the_configuration_intact():
    bp.save_config(valid_payload())
    bp.append_chat("user", "hello")
    plan = bp.clear_chat()
    assert plan["chat"] == []
    assert plan["configured"] is True


def test_reset_clears_configuration_narrative_and_chat():
    bp.save_config(valid_payload())
    bp.append_chat("user", "hello")
    plan = bp.get_plan()
    plan["narrative"] = {"text": "something", "generated_at": 1, "fingerprint": "f", "model": "x"}
    bp._write(plan)

    after = bp.reset_plan()
    assert after["configured"] is False
    assert after["variables"] == []
    assert after["chat"] == []
    assert after["narrative"]["text"] == ""
    assert after["company"]["name"] == ""
    # And it survives a round-trip through disk, not just in memory.
    assert bp.get_plan()["configured"] is False


def test_a_corrupt_plan_file_reads_as_first_run(plan_file):
    plan_file.write_text("{ not json")
    assert bp.get_plan()["configured"] is False


# ---------- The model-facing brief ----------

def test_the_brief_carries_every_included_variable_and_the_totals():
    plan = make_plan([("payroll", 1_000_000.0, 1.0), ("freight", 100_000.0, 40.0)])
    text = bp.brief(plan)
    assert "Payroll" in text and "Freight" in text
    assert "Cost base 2027" in text
    assert "ranked by materiality" in text.lower()


def test_the_brief_flags_excluded_variables_as_excluded():
    plan = make_plan([("payroll", 1_000_000.0, 1.0),
                      ("ghost", 500_000.0, 20.0, False)])
    text = bp.brief(plan)
    assert "EXCLUDED" in text
    assert "Ghost" in text.split("EXCLUDED")[1]


def test_starter_questions_are_grounded_in_this_company():
    plan = make_plan([("payroll", 1_000_000.0, 1.0), ("freight", 100_000.0, 40.0)])
    questions = bp.starter_questions(plan)
    assert 3 <= len(questions) <= 4
    assert any("freight" in q.lower() for q in questions)


def test_starter_questions_do_not_crash_without_variables():
    assert bp.starter_questions(make_plan([]))


# ---------- Failure messages ----------

class _Auth(Exception):
    pass


_Auth.__name__ = "AuthenticationError"


def test_an_auth_failure_names_the_key_and_not_the_request_id():
    message = bp.explain_api_failure(
        _Auth("Error code: 401 - {'request_id': 'req_011Cdf'} invalid x-api-key"))
    assert "ANTHROPIC_API_KEY" in message
    assert "req_011Cdf" not in message
    assert "computed locally" in message


def test_an_unrecognised_failure_still_reassures_about_the_figures():
    assert "computed locally" in bp.explain_api_failure(ValueError("boom"))


def test_the_chat_system_prompt_embeds_the_brief_and_the_scope_rule():
    plan = make_plan([("payroll", 1_000_000.0, 1.0)])
    system = bp.build_chat_system(plan)
    assert "Testco" in system
    assert "2027" in system
    assert "out of scope" in system
    assert "Payroll" in system

# The pure config-derivation half of agent.py. The streaming shell stays
# untested by design, but these functions encode two of the four constraints
# that shaped the whole design, and both fail in ways a smoke test would miss:
# a wrong tool-version string 400s on the first turn, and the effort/thinking
# pairing 400s only at the top two effort levels.

import pytest

from app.agent import (AVAILABLE_MODELS, DEFAULT_MODEL, DEFAULT_MODEL_GENERAL,
                       DEFAULT_MODEL_HEAVY, EFFORT_LEVELS, FALLBACK_BETA,
                       MESSAGE_CACHE_BREAKPOINTS, MODEL_CAPS, accumulate_usage,
                       build_system, build_tools, clamp_effort, mark_cache_breakpoint,
                       model_request_fields, volatile_context)


# ---------- Constraint 1: the picker cannot offer a model that cannot research ----------

def test_available_models_is_derived_from_the_registry():
    # One registry, so a model can never appear in the picker without caps —
    # and it is a SUBSET, not the whole registry: a model we know about is not
    # automatically one the loop can call.
    assert set(AVAILABLE_MODELS) <= set(MODEL_CAPS)
    assert AVAILABLE_MODELS, "the picker must offer at least one model"
    assert DEFAULT_MODEL in AVAILABLE_MODELS


def test_haiku_is_not_offered():
    # It supports neither the web tools nor adaptive thinking, and errors on
    # output_config.effort. Excluded rather than special-cased in the loop.
    assert "claude-haiku-4-5" not in AVAILABLE_MODELS


@pytest.mark.parametrize("model", AVAILABLE_MODELS)
def test_every_offered_model_can_research_and_think_and_take_effort(model):
    caps = MODEL_CAPS[model]
    assert caps["web_search"] and caps["web_fetch"]
    assert caps["thinking"] == "adaptive"
    assert caps["effort"] is True


def test_both_tier_defaults_are_offered():
    # The two slots in settings both coerce against AVAILABLE_MODELS, so a
    # default outside it would silently resolve to something else on first read.
    assert DEFAULT_MODEL_HEAVY in AVAILABLE_MODELS
    assert DEFAULT_MODEL_GENERAL in AVAILABLE_MODELS
    assert DEFAULT_MODEL_HEAVY != DEFAULT_MODEL_GENERAL


def test_fallbacks_is_sent_only_to_a_model_that_accepts_it():
    """The invariant that used to be enforced by excluding models from the
    picker. `betas` + `fallbacks` 400 on a model without server-side fallback
    routing — 'claude-sonnet-5 does not support the `fallbacks` parameter' —
    so the parameter is now absent on such a model rather than the model being
    absent from the app.

    This is the assertion that lets AVAILABLE_MODELS stop deriving from
    `fallbacks`. Delete it and the derivation silently becomes unsafe again.
    """
    opus = model_request_fields("claude-opus-5", "high")
    assert opus["fallbacks"] == "default"
    assert opus["betas"] == [FALLBACK_BETA]

    sonnet = model_request_fields("claude-sonnet-5", "high")
    assert "fallbacks" not in sonnet
    # The beta header and the parameter are paired; sending the header alone is
    # pointless and sending it with the wrong parameter form is a 400.
    assert "betas" not in sonnet


@pytest.mark.parametrize("model", AVAILABLE_MODELS)
def test_effort_reaches_every_offered_model(model):
    # effort is the one model-dependent field EVERY offered model takes, which
    # is why `effort` is part of the AVAILABLE_MODELS derivation and `fallbacks`
    # is not.
    assert model_request_fields(model, "medium")["output_config"] == {"effort": "medium"}


def test_an_unknown_model_gets_the_default_request_fields():
    assert model_request_fields("not-a-model", "high") == model_request_fields(DEFAULT_MODEL, "high")


@pytest.mark.parametrize("model", sorted(MODEL_CAPS))
def test_every_model_in_the_registry_declares_fallbacks(model):
    # Missing rather than False is the dangerous case: caps.get("fallbacks")
    # would read as falsy and quietly drop server-side routing on a model that
    # supports it, which nothing would ever surface.
    assert isinstance(MODEL_CAPS[model]["fallbacks"], bool)


@pytest.mark.parametrize("model", AVAILABLE_MODELS)
def test_search_and_fetch_versions_are_read_from_separate_keys(model):
    """The guard against synthesising a tool name that does not exist.

    The pre-4.6 basic variants are web_search_20250305 and web_fetch_20250910 —
    two different dates. A shared version key would silently produce
    web_fetch_20250305 and 400 on the first turn.
    """
    caps = MODEL_CAPS[model]
    tools = {t["name"]: t["type"] for t in build_tools(model) if t["name"].startswith("web_")}
    assert tools["web_search"] == f"web_search_{caps['web_search']}"
    assert tools["web_fetch"] == f"web_fetch_{caps['web_fetch']}"


def test_a_hypothetical_model_with_split_versions_keeps_them_split(monkeypatch):
    monkeypatch.setitem(MODEL_CAPS, "legacy-model",
                        {"web_search": "20250305", "web_fetch": "20250910",
                         "thinking": "adaptive", "effort": True, "thinking_default_on": False})
    types = {t["name"]: t["type"] for t in build_tools("legacy-model")
             if t["name"].startswith("web_")}
    assert types == {"web_search": "web_search_20250305",
                     "web_fetch": "web_fetch_20250910"}


# ---------- Tool assembly ----------

def test_code_execution_is_never_declared():
    # The _20260209 variants run dynamic filtering internally; a second
    # execution environment confuses the model.
    for model in AVAILABLE_MODELS:
        assert not any("code_execution" in str(t.get("type", ""))
                       for t in build_tools(model))


def test_web_fetch_enables_citations():
    fetch = next(t for t in build_tools(DEFAULT_MODEL) if t["name"] == "web_fetch")
    assert fetch["citations"] == {"enabled": True}


def test_research_can_be_disabled_leaving_only_local_tools():
    tools = build_tools(DEFAULT_MODEL, {"enabled": False})
    assert not any(t["name"].startswith("web_") for t in tools)
    assert any(t["name"] == "variance_analysis" for t in tools)


def test_research_limits_are_forwarded():
    tools = build_tools(DEFAULT_MODEL, {"enabled": True, "max_searches": 4, "max_fetches": 6})
    by_name = {t["name"]: t for t in tools}
    assert by_name["web_search"]["max_uses"] == 4
    assert by_name["web_fetch"]["max_uses"] == 6


def test_setup_mode_adds_propose_watchlist_first():
    tools = build_tools(DEFAULT_MODEL, setup=True)
    assert tools[0]["name"] == "propose_watchlist"
    # The input_schema IS the proposal schema — structured outputs are
    # incompatible with citations, so the proposal must arrive as a tool call.
    assert "cost_drivers" in tools[0]["input_schema"]["properties"]


def test_propose_watchlist_is_absent_outside_setup():
    assert not any(t.get("name") == "propose_watchlist" for t in build_tools(DEFAULT_MODEL))


def test_tool_order_is_deterministic():
    # `tools` renders before `system` in the cached prefix, so a reordering
    # here silently invalidates the prompt cache on every request.
    a = [t.get("name") for t in build_tools(DEFAULT_MODEL, {"enabled": True})]
    b = [t.get("name") for t in build_tools(DEFAULT_MODEL, {"enabled": True})]
    assert a == b


def test_an_unknown_model_falls_back_to_the_default_caps():
    assert build_tools("not-a-model") == build_tools(DEFAULT_MODEL)


# ---------- Constraint 4: disabling thinking is effort-gated ----------

@pytest.mark.parametrize("level", EFFORT_LEVELS)
def test_effort_is_untouched_when_reasoning_is_on(level):
    assert clamp_effort(level, True) == level


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_effort_at_or_below_high_survives_with_reasoning_off(level):
    assert clamp_effort(level, False) == level


@pytest.mark.parametrize("level", ["xhigh", "max"])
def test_the_two_levels_that_would_400_are_clamped(level):
    # {"type": "disabled"} paired with xhigh/max is rejected on claude-opus-5,
    # and it is validated per request — so a session that raises effort
    # mid-conversation would fail where earlier turns passed.
    assert clamp_effort(level, False) == "high"


def test_an_unknown_effort_falls_back_to_the_default():
    assert clamp_effort("turbo", True) == "high"
    assert clamp_effort(None, True) == "high"


# ---------- System prompt split ----------

PROFILE = {
    "setup_complete": True,
    "company": {"name": "NordFeed", "industry": "Animal nutrition",
                "reporting_currency": "EUR", "budget_year": 2027},
    "description": "We make compound feed for poultry, swine and aquaculture.",
    "product_lines": [{"name": "Poultry Feed"}],
    "markets": [{"name": "Iberia"}],
    "cost_drivers": [{"driver_id": "chicken_meal", "name": "Chicken meal", "unit": "USD/t",
                      "quote_currency": "USD", "why": "~22% of poultry feed cost"}],
}


def _render(profile):
    from app.agent import _render_profile
    return _render_profile(profile)


def test_exactly_one_block_is_cached():
    blocks = build_system(_render(PROFILE), volatile_context(2, 3))
    assert len(blocks) == 2
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]


def test_the_stable_profile_lives_inside_the_cached_block():
    blocks = build_system(_render(PROFILE), volatile_context(2, 3))
    assert "NordFeed" in blocks[0]["text"]
    assert "chicken_meal" in blocks[0]["text"]


def test_the_volatile_line_never_leaks_into_the_cached_block():
    # A date in the cached block means zero cache reads forever, with no error.
    blocks = build_system(_render(PROFILE), volatile_context(2, 3))
    assert "Today's date" not in blocks[0]["text"]
    assert "Today's date" in blocks[1]["text"]
    assert "stale" in blocks[1]["text"]


def test_no_volatile_line_yields_a_single_block():
    assert len(build_system(_render(PROFILE), None)) == 1


def test_an_unconfirmed_profile_renders_nothing():
    assert _render({"setup_complete": False, "company": {"name": "X"}}) is None
    assert _render(None) is None


def test_the_cached_prefix_clears_the_1024_token_floor():
    """A prefix below the model's minimum silently does not cache — no error,
    just cache_creation_input_tokens: 0. ~4 chars/token is a conservative
    estimate, so this is a floor check, not an exact count.

    1024, not 512: the minimum is per model and is NOT monotonic across
    generations — claude-opus-5 caches from 512 tokens but claude-sonnet-5, the
    general-tier default that now runs most turns, needs 1024. A prefix between
    the two would cache on reports and quietly stop caching on chat, which is
    the traffic that most needs it."""
    text = build_system(_render(PROFILE), volatile_context())[0]["text"]
    assert len(text) / 4 > 1024


def test_thinking_off_adds_a_generic_tag_guardrail():
    text = build_system(None, None, reasoning=False)[1]["text"]
    # Naming the tags is measurably less effective, and telling the model not
    # to reason makes the leakage worse — so neither appears.
    assert "internal or system XML tags" in text
    assert "<thinking>" not in text
    assert "do not think" not in text.lower()


def test_thinking_on_omits_the_guardrail():
    assert "internal or system XML tags" not in build_system(None, None, reasoning=True)[0]["text"]


def test_the_reasoning_toggle_never_forks_the_cached_block():
    """The guardrail varies with the `reasoning` flag, so it must sit BELOW the
    breakpoint. Background tasks run reasoning=False and chat runs
    reasoning=True; if that difference reached the cached block the two would
    warm separate prefixes and neither would ever read the other's — with
    nothing erroring to say so."""
    on = build_system(_render(PROFILE), volatile_context(2, 3), reasoning=True)
    off = build_system(_render(PROFILE), volatile_context(2, 3), reasoning=False)
    assert on[0]["text"] == off[0]["text"]
    assert "internal or system XML tags" in off[1]["text"]
    # The volatile line still has to survive alongside it.
    assert "Today's date" in off[1]["text"]


# ---------- Message-side cache breakpoints ----------
#
# The API caps cache_control at 4 blocks per request and build_system spends one.
# Marks persist on the blocks they were set on, so an unbounded loop breaches the
# cap on the FOURTH tool round — a 400 that a short smoke test never reaches and
# that only shows up on the long research turns this optimisation exists for.

def _tool_results(n: int = 2) -> list[dict]:
    return [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "{}"}
            for i in range(n)]


def _marks(convo: list[dict]) -> list[dict]:
    return [block
            for message in convo if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and "cache_control" in block]


def _run_rounds(n: int) -> list[dict]:
    """n tool rounds through the loop's real append sequence."""
    convo: list[dict] = [{"role": "user", "content": "plain string content"}]
    for _ in range(n):
        blocks = _tool_results()
        mark_cache_breakpoint(convo, blocks)
        convo.append({"role": "user", "content": blocks})
    return convo


def test_the_breakpoint_lands_on_the_last_block_of_the_newest_turn():
    convo = _run_rounds(1)
    blocks = convo[-1]["content"]
    assert "cache_control" not in blocks[0]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}


def test_marks_never_exceed_the_budget_over_a_full_tool_loop():
    # MAX_TOOL_TURNS is 8, so run past it. Unbounded, this would be 10.
    for rounds in range(1, 11):
        assert len(_marks(_run_rounds(rounds))) <= MESSAGE_CACHE_BREAKPOINTS


def test_the_two_surviving_marks_are_the_most_recent_ones():
    # Stripping the NEWEST instead of the oldest would keep the cache pinned to
    # a stale prefix and quietly stop extending it.
    convo = _run_rounds(4)
    marked_turns = [i for i, m in enumerate(convo)
                    if isinstance(m["content"], list)
                    and any("cache_control" in b for b in m["content"])]
    assert marked_turns == [len(convo) - 2, len(convo) - 1]


def test_a_string_content_turn_is_left_alone():
    # Incoming history from reporting.py carries plain-string content, which
    # cannot take a subscript assignment.
    convo = _run_rounds(3)
    assert convo[0]["content"] == "plain string content"


def test_non_dict_blocks_are_skipped():
    """The pause_turn path appends response.content — SDK block objects, not
    dicts. Scanning them for marks must not blow up on attribute access."""
    class SDKBlock:                      # deliberately not a dict
        type = "text"

    convo: list[dict] = [{"role": "assistant", "content": [SDKBlock()]}]
    blocks = _tool_results()
    mark_cache_breakpoint(convo, blocks)          # must not raise
    convo.append({"role": "user", "content": blocks})
    assert len(_marks(convo)) == 1


def test_an_empty_tool_result_list_is_a_no_op():
    convo = _run_rounds(1)
    before = len(_marks(convo))
    mark_cache_breakpoint(convo, [])
    assert len(_marks(convo)) == before


# ---------- Usage accounting ----------

def test_usage_accumulates_across_rounds():
    class Usage:
        input_tokens = 10
        cache_creation_input_tokens = 20
        cache_read_input_tokens = 30
        output_tokens = 40

    total = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    accumulate_usage(total, Usage())
    accumulate_usage(total, Usage())
    assert total == {"input": 20, "cache_write": 40, "cache_read": 60, "output": 80}


def test_usage_tolerates_missing_and_absent_fields():
    """Some error paths return no usage, or usage with fields unset. Accounting
    must never be the thing that breaks a turn."""
    class Partial:
        input_tokens = 5
        cache_read_input_tokens = None

    total = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    accumulate_usage(total, None)
    accumulate_usage(total, Partial())
    assert total == {"input": 5, "cache_write": 0, "cache_read": 0, "output": 0}


# ---------- Prompt integrity ----------

def test_urgency_prompt_formats_against_the_finding_shape():
    # .format(**finding) breaks at runtime, not import, if a key is renamed.
    from app.agent import URGENCY_ANALYSIS_PROMPT
    finding = {"rule_id": "driver_moved_since_lock", "entity": "chicken_meal",
               "period": "2026-12", "metric_value": 15.9, "threshold": 10.0,
               "severity": "critical", "title": "Chicken meal moved 15.9% since lock"}
    rendered = URGENCY_ANALYSIS_PROMPT.format(**finding)
    assert "chicken_meal" in rendered and "15.9" in rendered

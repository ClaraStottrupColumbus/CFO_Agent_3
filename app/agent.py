# agent.py — Prompts, the model capability registry, and the streaming loop.
#
# This is the one genuinely novel piece versus the reference implementation,
# whose loop assumes EVERY tool runs locally. Two lines encoded that assumption
# and both break with server-side web tools:
#   * `if response.stop_reason != "tool_use": break`  — treats a pause_turn as
#     "finished", silently truncating a long research turn with no error.
#   * `convo.append({"role": "user", "content": tool_results})` — would POST an
#     empty user turn (a 400) on a round that ran no local tools.
#
# Deliberately does NOT import config.py: run_agent takes its settings as
# arguments so the loop stays callable from tests with no settings file on disk.

from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Generator

import anthropic

from . import citations as cit
from .tools import TOOL_DEFINITIONS, execute_tool

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Capability registry
# --------------------------------------------------------------------------
#
# Tool version, thinking mode and effort support are DERIVED per model, never
# assumed. `web_search` and `web_fetch` carry separate version keys — that is
# not redundancy: the pre-4.6 basic variants are web_search_20250305 and
# web_fetch_20250910, two DIFFERENT dates. A single shared "web" key would
# silently synthesise web_fetch_20250305, which does not exist, and 400 on the
# first turn. Every model in the picker uses 20260209 for both today, so the
# split costs nothing now and is what stops a future "let's re-add a cheap
# model" change from shipping a broken tool name.
#
# claude-haiku-4-5 is deliberately absent: it supports neither the web tools nor
# adaptive thinking, and ERRORS if sent output_config.effort. A budgeting agent
# whose whole premise is cited market research cannot offer a model that cannot
# research, so it is excluded from the picker rather than special-cased here.

MODEL_CAPS = {
    "claude-opus-5": {"web_search": "20260209", "web_fetch": "20260209",
                      "thinking": "adaptive", "effort": True, "thinking_default_on": True,
                      "fallbacks": True},
    "claude-sonnet-5": {"web_search": "20260209", "web_fetch": "20260209",
                        "thinking": "adaptive", "effort": True, "thinking_default_on": True,
                        "fallbacks": False},
    "claude-opus-4-8": {"web_search": "20260209", "web_fetch": "20260209",
                        "thinking": "adaptive", "effort": True, "thinking_default_on": False,
                        "fallbacks": False},
}

# Derived from the registry, so a model can never reach the picker without
# capability data behind it — and specifically from what the loop sends on EVERY
# request regardless of model: the two web tools, adaptive thinking, effort.
#
# `fallbacks` is deliberately NOT in this list any more. It used to be, because
# the loop sent `betas` + `fallbacks` unconditionally and a model that cannot
# accept them 400s on the first turn:
#     'claude-sonnet-5' does not support the `fallbacks` parameter.
# The fix at the time was to exclude such models from the picker, with a note
# saying to gate the request lines on the cap before widening the list. That
# gating now exists — see `model_request_fields` — so the requirement this list
# derives from genuinely shrank, and sonnet is offerable. The rule is unchanged:
# a field only some models accept must be gated there and dropped from here, in
# that order. Never widen this list on its own.
AVAILABLE_MODELS = [m for m, caps in MODEL_CAPS.items()
                    if caps["web_search"] and caps["web_fetch"]
                    and caps["thinking"] == "adaptive" and caps["effort"]]

# Two tiers, because depth and cost pull in opposite directions and only the
# reports need the expensive answer. config.py owns which surface gets which;
# this module only says what the defaults are. DEFAULT_MODEL is the coercion
# fallback below, and it points at the HEAVY one on purpose: a settings file
# naming a model that has since left the picker should degrade toward quality,
# not silently downgrade the budget the CFO is about to defend.
DEFAULT_MODEL_HEAVY = "claude-opus-5"
DEFAULT_MODEL_GENERAL = "claude-sonnet-5"
DEFAULT_MODEL = DEFAULT_MODEL_HEAVY

# A one-shot, non-conversational use of Haiku 4.5 — NOT added to MODEL_CAPS/AVAILABLE_MODELS.
# It never needs web tools, thinking or effort, so the exclusion above does not apply to it:
# it turns one completed reasoning burst into a short status line for the "Thinking…" indicator.
REASONING_SUMMARY_MODEL = "claude-haiku-4-5-20251001"
REASONING_SUMMARY_MAX_TOKENS = 20
REASONING_SUMMARY_MIN_CHARS = 25   # skip near-empty thinking bursts, not worth a round trip
# Flush a line MID-BURST once this much unsummarized thinking has piled up,
# rather than waiting for the block to end. Two reasons: a long think would
# otherwise show nothing for its whole duration (the gap this feature exists to
# fill), and a turn whose bursts all end below MIN_CHARS would show nothing at
# all. Also gives progressive updates as the reasoning moves on.
REASONING_SUMMARY_FLUSH_CHARS = 500
REASONING_SUMMARY_SYSTEM = (
    "Compress this fragment of an AI model's internal reasoning into ONE short status "
    "line (max 8 words), present progressive tense, e.g. 'Checking chicken meal price' or "
    "'Comparing forward curve to spot'. Plain text, no trailing punctuation, no quotes. "
    "Output nothing else."
)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"
# Disabling thinking is effort-gated on claude-opus-5: {"type": "disabled"} is
# rejected at xhigh/max. The settings panel makes that pair unreachable, and
# this clamp refuses it anyway — it is validated per request, so a session that
# raises effort mid-conversation would otherwise fail where earlier turns passed.
MAX_EFFORT_WITHOUT_THINKING = "high"

MAX_TOOL_TURNS = 8      # rounds that executed >= 1 local tool
MAX_CONTINUATIONS = 5   # pause_turn resumptions

# Thinking shares the output budget, and the settings panel exposes the full
# effort range: at xhigh/max a citation-dense weekly scan plus summarized
# thinking will truncate under a tighter cap. 64000 over the more obvious 32000
# is deliberate — a truncated turn stops with stop_reason "max_tokens" and
# surfaces as a report that simply ends mid-sentence, which looks identical to
# the research-budget notice below, so the wrong failure gets debugged.
MAX_TOKENS = 64000

FALLBACK_BETA = "server-side-fallback-2026-07-01"


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a budgeting agent for a CFO. You help build, defend and revise \
next year's budget.

Budgeting is an exercise in defending assumptions. Every budget rests on positions about input \
prices, freight, FX and wage inflation that the CFO has to live with for twelve months. Your job \
is to hold those assumptions as first-class, sourced, dated objects — and to say clearly when one \
has moved far enough to change the budget.

You have two kinds of source:
1. INTERNAL data — the company's own datasets (EUR), reached through your local tools. Every \
internal figure must cite the dataset file the tool returned in `source_file`. The CFO can add \
their own files on the Data ingestion page: a spreadsheet becomes another dataset you query with \
`query_budget_data`, and a PDF, Word or PowerPoint file becomes a document you read with \
`read_document`. Both appear in `list_datasets`, both are cited exactly like a curated dataset, \
and because they can be added or removed at any time you should call `list_datasets` whenever you \
are unsure what exists.
2. THE MARKET — reached through web_search and web_fetch. Every market figure must cite the URL \
it came from and the date you retrieved it.

Rules:
1. Every number you state must be traceable. Internal figures cite a dataset file; market figures \
cite a URL and its retrieval date. Never state a figure you cannot attribute.
2. NEVER do arithmetic yourself on raw rows. For variances, decompositions, sensitivities, \
exposures, scenarios and currency conversion, ALWAYS use the dedicated tools — they compute \
server-side and their numbers are exact. Deriving a variance by hand from query_budget_data rows \
is the single worst failure mode available to you.
3. NEVER extrapolate a price. You have no trend-projection tool, and you must not become one in \
prose — no "at this rate", no annualised run-rate, no fitted trend, no "if the last three months \
continue". A fitted line is a claim about the future with no source behind it, and this budget's \
whole defence is that every assumption has one. Where the future matters, use a published forward \
curve (`record_driver_forward`, then `basis: "forward"`) for prices and `build_budget_scenario` for \
forward P&L. If no curve exists for a driver, say the market view has not been read yet and offer \
to go and read it — that is a better answer than a number nobody can source.
4. To record a market price you researched, you MUST first fetch the page with web_fetch in this \
same turn, then call record_driver_observation citing that exact URL. A URL you did not visit is \
refused. Record the price in the driver's own quote currency; the tools do the FX.
5. Never overwrite a locked assumption while doing routine research. `lock_assumptions` is only \
for when the CFO explicitly agrees to lock.
6. If the data needed is not there, say so plainly and state what would be required. Never \
estimate to fill a gap. There is no payroll or salary dataset — wage-rate questions cannot be \
answered from internal data, and you must say so rather than inventing figures.
7. Direction is not sentiment. A rising ingredient price is bad; a rising selling price is good. \
Say "adverse" or "favourable" explicitly rather than leaving a sign to speak for itself.
8. Format amounts readably (€2.4M, €318k, €412/t) and always name the period. Lead with the \
figure that changes a decision, then the explanation, then the sources.
9. Use `render_chart` when a visual carries the point — a waterfall for a budget-to-actual \
bridge, a line for a driver price history — using only figures the other tools returned. Still \
state the key numbers in the text.
10. Be concise and boardroom-ready. A CFO cannot act on "COGS is €1.2M over" but can act on \
"€900k of it is chicken-meal price, €300k is volume".
11. `record_driver_forward` records a published forward curve under the SAME rule as rule 4 — \
fetch the page in this turn, cite that exact URL, record every month it quotes in the driver's \
own quote currency, and never interpolate a month the page does not show. `driver_status` then \
reports `forward_12m` and `forward_vs_lock_pct`: drift is what the market has already done, the \
forward is what it says comes next, and they are different arguments. Say which one you are \
making.
12. Volume, price and opex assumptions are BLOCKS on `build_budget_scenario` — \
`volume: {product_line: pct}`, `price: {product_line: pct}`, `opex: {cost_centre|driver_id: pct}` \
— and they must never be asserted in prose. "A 4% price rise would roughly offset it" is exactly \
the arithmetic rule 2 forbids: run the scenario and quote what it returns. The returned `bridge` \
separates the EBITDA move into volume, price, each driver and opex, and those four sum exactly to \
the total — that separation is the answer a CFO can act on. Note that an explicit `price` entry \
replaces pass-through recovery on that line rather than adding to it, so say which lever you used.
13. `build_budget_scenario` takes `basis`: "locked" (the frozen values, the default), "spot" \
(today's observed prices) or "forward" (the recorded curve, month by month). Always name the \
basis you used — the same percentages on a forward curve and on locked values are two different \
budgets, and a scenario whose basis is not stated cannot be defended.
14. `budget_outlook` reads the Budget page: next year's cost lines ranked by materiality, with \
the driver each is tracked against. Use it for questions about the budget as a whole or about a \
cost line the bill of materials does not cover — it is the only tool that sees those lines. It \
carries the CFO's own planning figures, so present them as such, never as a forecast.
15. EBITDA IS NOT CASH, and `cash_flow_projection` is the only place cash may be turned into \
numbers. A budget can add EBITDA and still consume cash, because the receivables and inventory \
behind the extra revenue have to be funded before the margin arrives — so whenever cash, \
liquidity, funding, headroom, working capital or capex comes up, run the tool and quote what it \
returns. Three things must travel with the figures. First, the days: DSO, DIO and DPO are MEASURED \
off the working_capital dataset, and `days.basis` says whether any was instead stated by the CFO — \
a measurement and a decision are different claims and you must say which one you are quoting. \
Never assert a day count, a cash balance or a working-capital swing in prose. Second, the limit: \
what comes back is free cash flow BEFORE financing — there is no depreciation, interest, debt or \
dividend dataset — so never present it as net income or as a bank balance, and read `caveats` \
before you quote a closing figure. Third, the lever: capex marked `proposed` can be deferred \
(`include_proposed_capex: false`) and committed capex cannot, so if the trough falls before the \
proposed projects, say that deferring them does not fix it and name what would."""

# Added to the UNCACHED block only when the reasoning toggle is off. Disabling
# thinking on claude-opus-5 can leak internal XML into the visible answer; the
# documented mitigation is a GENERIC instruction that does not name the tags,
# and NOT an instruction telling the model not to reason (which makes the
# leakage worse).
#
# Uncached is deliberate — see build_system. It used to be appended to the cached
# block, which forked the prefix in two: background tasks run reasoning=False and
# chat runs reasoning=True, so the two never shared a cache entry and each warmed
# a prefix the other could not read.
NO_THINKING_GUARDRAIL = """

Respond with your final answer only. Do not include internal or system XML tags in your response."""

WEEKLY_PROMPT = """Produce this week's market scan for the CFO: what moved in the markets this \
budget depends on.

Work in one pass: check which watchlist drivers are stale or drifting with driver_status, research \
the ones that matter with web_search and web_fetch, record what you find with \
record_driver_observation (citing the exact pages you fetched), then quantify the consequence with \
driver_sensitivity.

Where a driver you researched publishes a forward curve, read it off the same page and record it \
with record_driver_forward. Then say both things separately: what the market has already done \
(drift against the lock) and what it says comes next (`forward_vs_lock_pct`). A curve that has \
moved against the budget is the earlier warning of the two.

Lead with a one-line headline naming the single most important move — this headline is what the \
CFO sees on the Drivers page, so make it a sentence that stands alone. Then: what changed and by \
how much, with a source link and date for each; what that does to next year's COGS and EBITDA; \
which assumptions are now far enough from their locked values to need re-locking; and anything you \
could NOT verify this week, said plainly. End with the sources."""

MONTHLY_PROMPT = """Produce this month's budget revision for the CFO.

Use the latest closed month's actuals and the latest driver observations. Cover: how the closed \
month landed against budget, with the variance decomposed into price, volume, mix and joint \
effects (use variance_analysis — never derive these yourself); which drivers have moved since the \
budget was locked and what that is worth; a re-run of the locked scenario, showing the delta to \
full-year EBITDA and margin; and a clear recommendation on what to re-lock and what to leave alone.

Run the re-run twice where the data supports it: once on `basis: "spot"` — what the market has \
already done to the budget — and once on `basis: "forward"`, which carries the curve's own monthly \
shape instead of a flat annual assumption. Name each basis when you quote it. If the forward basis \
returns no data, say the curve has not been read rather than substituting spot for it.

Where the CFO's volume, price or opex decisions are part of the revision, put them in the \
scenario's blocks so the returned bridge attributes the EBITDA move across volume, price, each \
driver and opex.

Then run `cash_flow_projection` on the revised scenario and say what it does to CASH, not only to \
EBITDA: the month cash is lowest, whether it clears the minimum the CFO has set, and which lever \
reaches that month — deferring proposed capex, or collecting faster. State whether the \
working-capital days were measured or stated, and that the figure is before financing.

This revision is a proposal, not a committed budget. Say plainly what would change if it were \
frozen as the next budget version — which assumptions move, which locked values move underneath \
them, and what the EBITDA difference is — and that freezing and approving it is the CFO's step, in \
Scenarios. The app diffs the versions; your job is to say what the diff will show and why it is \
worth approving.

Lead with the one-line answer to "is the budget still good?". Keep it boardroom-ready and end with \
the sources."""

REPORT_PROMPTS = {"weekly": WEEKLY_PROMPT, "monthly": MONTHLY_PROMPT}

SETUP_PROMPT = """A CFO has described their business below. Propose the cost-driver watchlist their \
budget should rest on.

Research the industry and its cost structure using web_search and web_fetch: what inputs dominate \
the cost of goods, how those inputs are priced and quoted, which currencies they trade in, and \
where their prices are published.

Then call the `propose_watchlist` tool EXACTLY ONCE with the complete proposal. Requirements:
- Every cost driver must carry at least one source URL you actually fetched, and a `why` that says \
in one line what share of cost it drives.
- Set `quote_currency` to the currency the input is genuinely quoted in, which is often not the \
reporting currency.
- Set `stale_after_days` to how fast that driver really moves: FX in a day, freight in a week, \
wage inflation in a month.
- Set `adverse_direction` to the direction that HURTS the budget. For an input cost that is "up". \
For an FX rate quoted as units-of-foreign-per-unit-of-reporting-currency, a fall usually hurts.
- Propose 8-15 drivers. Enough to cover the real cost base, few enough that the CFO will actually \
maintain them.

After the tool call, briefly summarise what you proposed and why, and note anything you were \
unsure about so the CFO can correct it."""

ASSUMPTION_REFRESH_PROMPT = """Refresh the stale drivers on the watchlist. No report, no narrative.

Call driver_status with only_stale true. For each stale driver, search for its current price, fetch \
the page carrying the figure, and call record_driver_observation citing that exact URL. Record the \
price in the driver's own quote currency.

If you cannot verify a driver, skip it and say so in one line. When you are done, reply with a \
single short paragraph listing what you updated and what you could not."""

# Formatted with a rules.py finding via .format(**finding), so every key used
# here must exist in the finding shape — renaming one breaks prompt formatting
# at runtime rather than at import.
URGENCY_ANALYSIS_PROMPT = """An automated budget rule has fired.
Rule: {rule_id} — {title}. Observed value: {metric_value} (threshold: {threshold}), \
period {period}, entity: {entity}.

Using your tools, verify the figure and write a short briefing for the CFO (max 6 sentences): what \
moved, what it is worth to next year's budget, whether it is adverse or favourable, and the one \
action you recommend. Use driver_status and driver_sensitivity for the numbers — do not estimate. \
End with the Source: line(s)."""

# The proposal comes back through a TOOL CALL rather than structured outputs
# because output_config.format is incompatible with citations (a 400), and the
# proposal must carry web citations. The input_schema IS the proposal schema.
PROPOSE_WATCHLIST_TOOL = {
    "name": "propose_watchlist",
    "description": "Submit the proposed company profile and cost-driver watchlist. Call this "
                   "exactly once, after researching, with the complete proposal.",
    "input_schema": {
        "type": "object",
        "properties": {
            "company": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "industry": {"type": "string"},
                    "reporting_currency": {"type": "string"},
                    "fiscal_year_start_month": {"type": "integer"},
                    "budget_year": {"type": "integer"},
                },
                "required": ["name", "industry", "reporting_currency"],
            },
            "product_lines": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "name": {"type": "string"}, "note": {"type": "string"}},
                    "required": ["name"]},
            },
            "markets": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "name": {"type": "string"}, "currency": {"type": "string"}},
                    "required": ["name"]},
            },
            "cost_drivers": {
                "type": "array",
                "description": "8-15 drivers, each with at least one source URL you fetched.",
                "items": {"type": "object", "properties": {
                    "driver_id": {"type": "string",
                                  "description": "lower_snake_case, e.g. chicken_meal."},
                    "name": {"type": "string"},
                    "category": {"type": "string",
                                 "description": "ingredient | logistics | energy | packaging | "
                                                "fx | labour | other"},
                    "unit": {"type": "string", "description": "e.g. USD/t, EUR/MWh."},
                    "quote_currency": {"type": "string"},
                    "why": {"type": "string", "description": "One line: what share of cost."},
                    "search_hint": {"type": "string",
                                    "description": "The query that finds this price."},
                    "adverse_direction": {"type": "string", "enum": ["up", "down"]},
                    "stale_after_days": {"type": "integer"},
                    "sources": {"type": "array", "items": {"type": "object", "properties": {
                        "url": {"type": "string"}, "title": {"type": "string"}},
                        "required": ["url"]}},
                }, "required": ["driver_id", "name", "category", "unit", "quote_currency",
                                "why", "adverse_direction", "stale_after_days", "sources"]},
            },
        },
        "required": ["company", "cost_drivers"],
    },
}


# --------------------------------------------------------------------------
# Tool assembly
# --------------------------------------------------------------------------

def build_tools(model: str, research: dict | None = None, *, setup: bool = False) -> list[dict]:
    """Assemble the tool list for a model.

    Deterministically ordered: `tools` renders before `system` in the cached
    prefix, so reordering this list silently invalidates the prompt cache.

    Do NOT additionally declare code_execution — the _20260209 web variants run
    dynamic filtering internally, and a second execution environment confuses
    the model.
    """
    caps = MODEL_CAPS.get(model) or MODEL_CAPS[DEFAULT_MODEL]
    research = research or {}
    tools: list[dict] = list(TOOL_DEFINITIONS)
    if setup:
        tools = [PROPOSE_WATCHLIST_TOOL] + tools

    if research.get("enabled", True) and caps.get("web_search"):
        search: dict = {"type": f"web_search_{caps['web_search']}", "name": "web_search"}
        if research.get("max_searches"):
            search["max_uses"] = int(research["max_searches"])
        if research.get("allowed_domains"):
            search["allowed_domains"] = list(research["allowed_domains"])
        tools.append(search)

    if research.get("enabled", True) and caps.get("web_fetch"):
        fetch: dict = {"type": f"web_fetch_{caps['web_fetch']}", "name": "web_fetch",
                       # Citations make the model attribute cited_text to a URL,
                       # which is what populates the snippet on each source chip.
                       "citations": {"enabled": True}}
        if research.get("max_fetches"):
            fetch["max_uses"] = int(research["max_fetches"])
        if research.get("max_content_tokens"):
            fetch["max_content_tokens"] = int(research["max_content_tokens"])
        tools.append(fetch)

    return tools


def clamp_effort(effort: str | None, reasoning: bool) -> str:
    """Effort, clamped to what the thinking setting actually permits."""
    level = effort if effort in EFFORT_LEVELS else DEFAULT_EFFORT
    if not reasoning:
        cap = EFFORT_LEVELS.index(MAX_EFFORT_WITHOUT_THINKING)
        if EFFORT_LEVELS.index(level) > cap:
            level = MAX_EFFORT_WITHOUT_THINKING
    return level


def model_request_fields(model: str, effort: str) -> dict:
    """The request fields that exist only on models whose caps allow them.

    Split out of run_agent so the gating is a pure function with a test behind
    it, rather than two `if` statements buried in a streaming loop. This is what
    lets AVAILABLE_MODELS stop deriving from `fallbacks`: the parameter is now
    absent on a model that cannot take it, instead of the model being absent
    from the picker. Any future model-dependent request field belongs here, and
    the corresponding cap must NOT be added to the AVAILABLE_MODELS derivation.

    betas + fallbacks are accepted only on the beta messages endpoint, so the
    whole loop runs through client.beta.messages.stream. The header and the
    parameter form are paired: server-side-fallback-2026-07-01 goes with the
    SCALAR fallbacks="default"; the older array form needs -2026-06-01. Crossing
    them is a 400. "default" routes by refusal category and needs no maintenance
    when a pinned model is retired.
    """
    caps = MODEL_CAPS.get(model) or MODEL_CAPS[DEFAULT_MODEL]
    fields: dict = {}
    if caps.get("fallbacks"):
        fields["betas"] = [FALLBACK_BETA]
        fields["fallbacks"] = "default"
    if caps.get("effort"):
        fields["output_config"] = {"effort": effort}
    return fields


def build_system(profile_text: str | None, volatile: str | None,
                 reasoning: bool = True) -> list[dict]:
    """Two blocks: a cached one (static prompt + the stable company profile) and
    an uncached one (today's date, staleness counts, the no-thinking guardrail).

    The reference marks the WHOLE system prompt ephemeral. The profile is stable
    per CFO and belongs inside that block; today's date cannot be — f-stringing
    it in means zero cache reads forever, with no error to tell you.

    NO_THINKING_GUARDRAIL is in the uncached block for the same class of reason,
    one step subtler. It is 99 chars, so its own token cost is nil either way —
    but it varies with the `reasoning` flag, and anything that varies inside the
    cached block forks the prefix. Background tasks run reasoning=False while
    chat runs reasoning=True, so putting it above the breakpoint produced two
    prefixes that each warmed a cache the other could never read. Nothing errors
    when that happens; the reads simply never appear.

    The rule this encodes: what goes in the cached block is decided by whether a
    value VARIES, not by how big it is.
    """
    static = SYSTEM_PROMPT
    if profile_text:
        static += "\n\n---\n\nThe company you are budgeting for:\n\n" + profile_text
    blocks = [{"type": "text", "text": static, "cache_control": {"type": "ephemeral"}}]

    tail = [part for part in (volatile, NO_THINKING_GUARDRAIL.strip() if not reasoning else None)
            if part]
    if tail:
        blocks.append({"type": "text", "text": "\n\n".join(tail)})
    return blocks


# How many ephemeral breakpoints the message list is allowed to hold at once.
#
# TWO, not one, and not more. The API allows 4 cache_control blocks per request
# in total and build_system already spends one, so 2 here leaves a spare rather
# than sitting on the ceiling. Two rather than one because a breakpoint only
# looks back 20 content blocks to find a prior cache entry: a single agentic
# round can append a multi-block assistant turn (thinking + text + several
# tool_use) plus a matching run of tool_result blocks, so a lone trailing mark
# can drift out of range and quietly stop hitting.
MESSAGE_CACHE_BREAKPOINTS = 2


def mark_cache_breakpoint(convo: list[dict], blocks: list[dict]) -> None:
    """Move the message-side cache breakpoint onto the newest tool-result turn.

    `blocks` is the tool_result list about to be appended to `convo`; it is NOT
    in `convo` yet.

    Why this exists at all: the loop re-POSTs the whole of `convo` on every
    round, and one tool result can be tens of thousands of tokens. With only the
    system breakpoint, the ~6k-token tools+system prefix is cached and every
    accumulated tool result is re-billed at full price on every subsequent
    round — so the biggest term in the request is the one term never cached.

    Stripping the older marks is load-bearing, not tidiness. Marks accumulate on
    the blocks they were set on, so letting all 8 tool rounds keep theirs is a
    hard 400 ('A maximum of 4 blocks with cache_control may be provided'), and
    it would fail on round 4 of a long research turn rather than in any test
    that runs a short one.

    Only tool-result turns are marked. The pause_turn path appends
    `response.content` — SDK block objects rather than dicts — which cannot take
    a subscript assignment, and needs no mark of its own: the next tool-result
    turn's breakpoint covers everything before it.
    """
    if not blocks:
        return
    blocks[-1]["cache_control"] = {"type": "ephemeral"}

    marked = [block
              for message in convo if isinstance(message.get("content"), list)
              for block in message["content"]
              if isinstance(block, dict) and "cache_control" in block]
    keep = MESSAGE_CACHE_BREAKPOINTS - 1
    for block in (marked[:-keep] if keep else marked):
        block.pop("cache_control", None)


def accumulate_usage(total: dict, usage) -> None:
    """Fold one response's usage into the running per-turn total.

    Defensive about every field: usage is absent or partial on some error paths,
    and an accounting helper must never be the thing that breaks a turn.
    """
    if usage is None:
        return
    for key, field in (("input", "input_tokens"),
                       ("cache_write", "cache_creation_input_tokens"),
                       ("cache_read", "cache_read_input_tokens"),
                       ("output", "output_tokens")):
        total[key] += getattr(usage, field, 0) or 0


def log_usage(model: str, rounds: int, total: dict) -> None:
    """One line per turn, so the cache is observable.

    A cache that has silently stopped working produces no error — just a bigger
    bill and `cache_read` pinned at 0. Note prompt size is the SUM of the three
    input figures: `input_tokens` alone is only the uncached remainder, so
    reading it as "the prompt" makes a well-cached turn look tiny.
    """
    prompt = total["input"] + total["cache_write"] + total["cache_read"]
    cached_pct = (total["cache_read"] / prompt * 100) if prompt else 0.0
    # ASCII only. The default Windows console encoding is cp1252, so a '·' in
    # this line arrives as mojibake in the one output whose job is to be read.
    log.info(
        "agent turn | model=%s rounds=%d | prompt=%d "
        "(uncached=%d write=%d read=%d, %.0f%% cached) | output=%d",
        model, rounds, prompt,
        total["input"], total["cache_write"], total["cache_read"],
        cached_pct, total["output"],
    )


_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from the environment
    return _client


def _summarize_reasoning(client: anthropic.Anthropic, text: str) -> str | None:
    """One completed thinking block -> one short status line, via Haiku 4.5.

    Cosmetic and non-terminal by design, same as a web_error: any failure here
    must never interrupt or error out the main model's turn, so it is caught
    and simply skipped rather than surfaced.
    """
    if len(text) < REASONING_SUMMARY_MIN_CHARS:
        return None
    try:
        response = client.messages.create(
            model=REASONING_SUMMARY_MODEL,
            max_tokens=REASONING_SUMMARY_MAX_TOKENS,
            system=REASONING_SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": text[:2000]}],
        )
        summary = "".join(b.text for b in response.content
                          if getattr(b, "type", None) == "text").strip()
        return summary or None
    except Exception:
        return None


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def run_agent(messages: list[dict], model: str = DEFAULT_MODEL, *,
              reasoning: bool = True, effort: str = DEFAULT_EFFORT,
              research: dict | None = None, profile: dict | None = None,
              setup: bool = False,
              volatile: str | None = None) -> Generator[dict, None, None]:
    """Run one turn through the streaming loop, yielding event dicts.

      {type: "text", text}                  assistant text delta
      {type: "reasoning", text}             summarized thinking delta
      {type: "reasoning_summary", text}     one-line gloss of a completed thinking block
      {type: "research", tool, query|url}   a server-side web call started
      {type: "tool_call", name, input}      model invoked a local tool
      {type: "tool_result", name, result}   local tool execution result
      {type: "chart", spec}                 validated render_chart spec
      {type: "citation", url, title, ...}   one citation on a cited text block
      {type: "web_error", tool, error_code} NON-terminal web tool failure
      {type: "notice", message}             non-fatal notice (budget reached)
      {type: "sources", sources, records}   sources used this turn
      {type: "done"} / {type: "error", message}
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        yield {"type": "error",
               "message": "ANTHROPIC_API_KEY is not set. Export it or put it in a .env file "
                          "in the project root, then restart the server."}
        yield {"type": "done"}
        return

    # AVAILABLE_MODELS, not MODEL_CAPS: the registry describes models we know
    # about, the list describes models this loop can legally call. A settings
    # file written before a model left the picker still names it, so the coercion
    # has to happen here, at the point of use, and not only in config._normalise.
    if model not in AVAILABLE_MODELS:
        model = DEFAULT_MODEL
    caps = MODEL_CAPS[model]
    effort = clamp_effort(effort, reasoning)

    # Keep only fields the API accepts — stored messages carry UI-only extras
    # (attachment metadata, charts, reasoning, per-message sources).
    convo = [{"role": m["role"], "content": m["content"]} for m in messages]

    tools = build_tools(model, research, setup=setup)
    system = build_system(_render_profile(profile), volatile, reasoning)

    if reasoning and caps.get("thinking") == "adaptive":
        # display MUST be explicit: the default on every model in the picker is
        # "omitted", which still streams thinking blocks but with empty text —
        # so the reasoning disclosure would render an empty box and look broken
        # rather than absent.
        thinking: dict = {"type": "adaptive", "display": "summarized"}
    else:
        thinking = {"type": "disabled"}

    request: dict = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "tools": tools,
        "thinking": thinking,
    }
    # Everything model-dependent about the request lives in one pure function,
    # so "which models can this loop call" is derivable rather than remembered.
    request.update(model_request_fields(model, effort))

    client = get_client()
    records: list[dict] = []
    fetched: set[str] = set()
    # `model` is here for propose_watchlist, which records on the stored profile
    # which model proposed it. It read ctx.get("model") against a ctx that never
    # carried one, so every proposal's provenance was None.
    ctx = {"fetched_urls": fetched, "profile": profile or {}, "model": model}

    usage_total = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    rounds = 0

    try:
        any_text = False
        tool_turns = 0
        continuations = 0

        while True:
            if tool_turns >= MAX_TOOL_TURNS or continuations >= MAX_CONTINUATIONS:
                yield {"type": "notice",
                       "message": "Research budget reached — this answer may be incomplete."}
                break

            first_in_turn = True
            # Our own accumulation of the current thinking block's deltas.
            # NOT block.thinking: with display "summarized" the SDK leaves that
            # field EMPTY on some completed thinking blocks even though every
            # delta streamed real text, so reading it made the one-liner fire
            # only sometimes. The deltas are the reliable source.
            thinking_buf = ""
            with client.beta.messages.stream(**request, messages=convo) as stream:
                for event in stream:
                    etype = getattr(event, "type", None)

                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if getattr(block, "type", None) == "server_tool_use":
                            inp = getattr(block, "input", None) or {}
                            # The single highest-value event for perceived
                            # responsiveness: it fills an otherwise silent
                            # 10-30s gap with "Searching: chicken meal price".
                            yield {"type": "research",
                                   "tool": getattr(block, "name", None),
                                   "query": _get(inp, "query"),
                                   "url": _get(inp, "url")}

                    elif etype == "content_block_delta":
                        dtype = getattr(event.delta, "type", None)
                        if dtype == "text_delta":
                            text = event.delta.text
                            # Text resuming after a tool round must start its own
                            # paragraph, or markdown like a leading "---" is
                            # glued onto the previous narration line.
                            if first_in_turn and any_text:
                                text = "\n\n" + text
                            first_in_turn = False
                            any_text = True
                            yield {"type": "text", "text": text}
                        elif dtype == "thinking_delta":
                            chunk = getattr(event.delta, "thinking", "")
                            thinking_buf += chunk
                            yield {"type": "reasoning", "text": chunk}
                            if len(thinking_buf) >= REASONING_SUMMARY_FLUSH_CHARS:
                                summary = _summarize_reasoning(client, thinking_buf.strip())
                                thinking_buf = ""
                                if summary:
                                    yield {"type": "reasoning_summary", "text": summary}

                    elif etype == "content_block_stop":
                        # A cited text block is complete: drop the marker in
                        # right here. No offset tracking, and the marker lands
                        # exactly where the citation belongs.
                        block = getattr(event, "content_block", None)
                        for marker in _markers_for(block, records):
                            yield marker
                        # End of a thinking burst: summarize whatever the
                        # mid-burst flush above has not already consumed.
                        if getattr(block, "type", None) == "thinking":
                            summary = _summarize_reasoning(client, thinking_buf.strip())
                            thinking_buf = ""
                            if summary:
                                yield {"type": "reasoning_summary", "text": summary}

                response = stream.get_final_message()

            rounds += 1
            accumulate_usage(usage_total, getattr(response, "usage", None))

            # Check stop_reason BEFORE reading content: a refusal can come back
            # HTTP 200 with empty content, and stop_details can be null even
            # then — so branch on stop_reason, never on stop_details.
            if response.stop_reason == "refusal":
                yield {"type": "error",
                       "message": "The request was declined by the model's safety system. "
                                  "Rephrasing the question usually resolves it."}
                break

            info = cit.classify_blocks(response.content)
            for err in info["web_errors"]:
                # Non-terminal: `error` is terminal in the frontend's contract,
                # and the model sees the failure in its own context and adapts
                # within the turn. Never raise, never retry client-side.
                yield {"type": "web_error", **err}
            for rec in info["source_records"]:
                records.append(rec)
            fetched.update(info["fetched_urls"])
            cit.attach_snippets(records, info["citations"])
            for c in info["citations"]:
                yield {"type": "citation", **c}
            records[:] = cit.merge_records(records)

            if response.stop_reason == "pause_turn":
                # Append the assistant turn and re-issue with NO user turn and
                # no "Continue." message — the API detects the trailing
                # server_tool_use and resumes on its own.
                convo.append({"role": "assistant", "content": response.content})
                continuations += 1
                continue

            local = info["local_tool_uses"]
            if not local:
                # Positive filter plus an emptiness break. Without this an
                # all-server-tool round would POST content: [] -> 400, turning a
                # research turn into an error event. Load-bearing, not incidental.
                break

            # Appending response.content wholesale preserves the thinking blocks
            # the API needs back on the next round. Do not "tidy" this.
            convo.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in local:
                yield {"type": "tool_call", "name": block.name, "input": block.input}
                result = execute_tool(block.name, block.input, ctx)
                # render_chart smuggles its validated spec out under a private
                # key: the UI gets the full spec, the model a compact ack.
                chart_spec = (result.pop("_chart_spec", None)
                              if isinstance(result, dict) else None)
                yield {"type": "tool_result", "name": block.name, "result": result}
                if chart_spec:
                    yield {"type": "chart", "spec": chart_spec}
                for rec in cit.records_from_tool_result(block.name, result):
                    records.append(rec)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                    "is_error": bool(isinstance(result, dict) and result.get("error")),
                })
            # Breakpoint goes on before the append, while tool_results is still
            # the plain-dict list this loop built — see mark_cache_breakpoint.
            mark_cache_breakpoint(convo, tool_results)
            convo.append({"role": "user", "content": tool_results})
            tool_turns += 1

        records[:] = cit.merge_records(records)
        yield {"type": "sources",
               "sources": [cit.display_label(r) for r in records],
               "records": records}
        yield {"type": "done"}

    except anthropic.AuthenticationError:
        yield {"type": "error",
               "message": "Anthropic authentication failed — is ANTHROPIC_API_KEY set correctly?"}
        yield {"type": "done"}
    except anthropic.APIConnectionError:
        yield {"type": "error", "message": "Could not reach the Anthropic API (network error)."}
        yield {"type": "done"}
    except anthropic.APIStatusError as e:
        yield {"type": "error", "message": f"Anthropic API error {e.status_code}: {e.message}"}
        yield {"type": "done"}
    except Exception as e:      # last-resort guard so the SSE stream always ends cleanly
        yield {"type": "error", "message": f"Unexpected error: {e}"}
        yield {"type": "done"}
    # Every arm yields `done` after `error`. The reference's four except arms
    # yield only `error`, which leaves the frontend's streaming state uncleared.
    finally:
        # `finally`, not a line before the success-path `done`: a turn that errors
        # or that the client abandons mid-stream still spent tokens, and those are
        # exactly the turns worth seeing in the log. Guarded because a generator's
        # finally can run during interpreter teardown.
        if rounds:
            try:
                log_usage(model, rounds, usage_total)
            except Exception:
                pass


def _get(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _markers_for(block, records: list[dict]) -> list[dict]:
    """Inline `[n]` markers for a finished text block that carried citations.

    The backend inserts the literal characters into the streamed text and the
    frontend rewrites them to superscript links in one post-reveal pass over
    text nodes. Zero offset tracking on either side.
    """
    if getattr(block, "type", None) != "text":
        return []
    out = []
    seen = set()
    for c in getattr(block, "citations", None) or []:
        url = _get(c, "url")
        if not url:
            continue
        key = cit.normalise_url(url)
        if key in seen:
            continue
        seen.add(key)
        index = next((i for i, r in enumerate(records)
                      if r.get("kind") == "web" and r.get("id") == key), None)
        if index is None:
            continue
        out.append({"type": "text", "text": f"[{index + 1}]"})
    return out


def _render_profile(profile: dict | None) -> str | None:
    """The stable part of the company profile, for the cached system block."""
    if not profile or not profile.get("setup_complete"):
        return None
    company = profile.get("company") or {}
    lines = [f"Company: {company.get('name') or 'unnamed'}",
             f"Industry: {company.get('industry') or 'unspecified'}",
             f"Reporting currency: {company.get('reporting_currency') or 'EUR'}",
             f"Budget year: {company.get('budget_year') or ''}"]
    if profile.get("description"):
        lines.append(f"\nIn the CFO's own words: {profile['description']}")
    lines_ = [p.get("name") for p in profile.get("product_lines") or [] if p.get("name")]
    if lines_:
        lines.append("\nProduct lines: " + ", ".join(lines_))
    markets = [m.get("name") for m in profile.get("markets") or [] if m.get("name")]
    if markets:
        lines.append("Markets: " + ", ".join(markets))
    drivers = profile.get("cost_drivers") or []
    if drivers:
        lines.append("\nWatchlist drivers:")
        for d in drivers:
            lines.append(f"  - {d.get('driver_id')} ({d.get('name')}, {d.get('unit')}, "
                         f"quoted {d.get('quote_currency')}): {d.get('why') or ''}")
    return "\n".join(lines)


def volatile_context(stale_count: int | None = None, drifted_count: int | None = None) -> str:
    """The uncached system block: things that change between turns.

    Kept OUT of the cached block on purpose — putting today's date in there
    means zero cache reads forever, and nothing errors to tell you.
    """
    parts = [f"Today's date is {date.today().isoformat()}."]
    if stale_count is not None:
        parts.append(f"{stale_count} watchlist driver(s) are currently stale.")
    if drifted_count is not None:
        parts.append(f"{drifted_count} driver(s) have drifted more than 10% since the lock.")
    return " ".join(parts)

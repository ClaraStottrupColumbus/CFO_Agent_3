# CFO Budgeting Agent — Implementation Plan

How the third CFO agent is built: an agent that helps a CFO construct next year's budget.
Companion to [ARCHITECTURE.md](ARCHITECTURE.md), which blueprints the scaffolding this plan
reuses.

---

## Context

`CFO_Agent_3` is currently docs-only: a single [ARCHITECTURE.md](ARCHITECTURE.md) that
blueprints a sibling implementation (`../Agent 1/CFO_Agent_1`, a conversational finance
agent) and marks which layers are domain-agnostic scaffolding versus finance-specific domain
code. `../CFO_Agent_2` is a second agent already built on that scaffolding, so the "clone the
shell, replace the domain" recipe is proven.

**Path note, because every file reference below depends on it:** the two siblings do not sit
at symmetrical paths. Agent 1 is at `../Agent 1/CFO_Agent_1/` — nested one level deeper,
under a directory whose name **contains a space** — while Agent 2 is at `../CFO_Agent_2/`.
Any `cp`/`diff` against Agent 1 needs the path quoted (`cp "../Agent 1/CFO_Agent_1/app/store.py" app/`)
or the copy silently resolves to the wrong place. Line references in this plan were verified
against those paths.

This plan builds a **third** agent, for budgeting.

Budgeting is an exercise in defending assumptions. A CFO setting a budget for animal food
has to take a position on wheat and chicken-meal prices, sea freight, FX, and wage
inflation — then live with those positions for twelve months. Today those assumptions are
gathered by hand, go stale silently, and are rarely traceable to a source. So the agent's
job is threefold: ingest the internal history (prior-year budget, actuals, volumes, cost
structure) *and* the external market autonomously; hold the resulting assumptions as
first-class objects that each carry a citation and a last-verified date; and tell the CFO
when one has moved far enough to change the budget.

Intended outcome: the CFO completes a short guided setup, and thereafter has a standing
weekly market scan, a monthly budget revision, an auditable driver watchlist where every
number carries its source, and scenario tooling to test what a ±X% move in any driver does
to the P&L — with all arithmetic computed in Python, never by the model.

## Decisions taken before planning

| Decision | Choice |
|---|---|
| Web ingestion | Anthropic server-side `web_search_20260209` + `web_fetch_20260209` — no new dependencies, native citations |
| Report kinds | `chat` + `weekly` (market/driver scan) + `monthly` (budget revision) |
| Setup | Agent-assisted: free-text business description → agent proposes a cited watchlist → CFO edits and confirms |
| Scaffolding | Copy the domain-agnostic layers from `CFO_Agent_1`; write the domain fresh |
| LLM | Anthropic only; model picked in settings. Picker is `claude-opus-5` (default), `claude-sonnet-5`, `claude-opus-4-8` — every model offered supports web tools, adaptive thinking *and* effort, so no model in the UI can hit constraint 1 |
| Reasoning toggle | Adaptive thinking with `display: "summarized"`, streamed as its own event type |
| Port | 8323 (Agent 1 = 8321, Agent 2 = 8322) |

## Four constraints that shaped the design

These were discovered during design review and each one invalidates an otherwise obvious
approach. They are stated up front because they explain choices that would otherwise look
arbitrary.

1. **`claude-haiku-4-5` is incompatible with three requirements at once, so it is not
   offered.** It does not support `web_search_20260209` / `web_fetch_20260209` (those need
   Opus 5/4.8/4.7/4.6 or Sonnet 5/4.6), does not support adaptive thinking, and **errors** if
   sent `output_config.effort`. Its context is 200 K, not 1 M. A budgeting agent whose whole
   premise is cited market research cannot offer a model that cannot research, so Haiku is
   **excluded from the picker** rather than special-cased in the loop — which deletes a
   branch from `build_tools`, from the thinking config, and from §12.5. Where a cheap model
   is genuinely wanted (`assumption_refresh`), §8's per-task `model` override selects
   `claude-sonnet-5`.

   The `MODEL_CAPS` registry in §2 stays regardless — tool version, thinking mode and effort
   are derived per model, never assumed — and it carries **separate `web_search` and
   `web_fetch` version keys**. That is not redundancy: the pre-4.6 basic variants are
   `web_search_20250305` and `web_fetch_20250910` — two **different dates**. A single shared
   `"web": "20250305"` key would silently synthesise `web_fetch_20250305`, which does not
   exist, and 400 on the first turn. Every model in the picker today uses `20260209` for
   both, so the split costs nothing now and is the thing that stops a future "let's re-add a
   cheap model" change from shipping a broken tool name.
2. **`output_config.format` (structured outputs) is incompatible with citations — a 400.**
   The setup proposal must carry web citations, so it cannot use structured outputs. The
   proposal comes back through a `propose_watchlist` **tool call** whose `input_schema` *is*
   the proposal schema (§6). This is a constraint, not a preference.
3. **`escapeHtml` in the reference frontend escapes `& < >` but not quotes**
   (`../Agent 1/CFO_Agent_1/static/app.js:74`). Interpolating a model-supplied URL into
   `href="${escapeHtml(url)}"` is attribute injection, and `javascript:` URLs survive it.
   Every web citation link must be built imperatively with a protocol allowlist (§9).
4. **Disabling thinking is effort-gated on `claude-opus-5`.** `{"type": "disabled"}` is
   rejected at effort `xhigh`/`max`. So the reasoning toggle and the effort selector are
   coupled, and the coupling has to be expressed in the settings payload rather than
   discovered as an API error.

---

## 1. Architecture

Same shape as the blueprint — single uvicorn process, JSON files on disk, threads, no
database, no build step, no frontend framework.

```
Browser (vanilla JS SPA, hash-routed)
   │  fetch() JSON  +  SSE stream
   ▼
FastAPI (app/main.py) ── single process, uvicorn --port 8323
   ├── REST: sessions, settings, profile, drivers, scenarios, tasks, alerts, rules, datasets
   ├── SSE: POST /api/sessions/{id}/chat        → streams agent events
   ├── SSE: POST /api/profile/propose           → streams the setup research turn
   └── lifespan: seed data → scheduler thread → (gated on setup) background reports
        │
        ├── reporting.py   one shared "run a turn against a session" path
        │      └── agent.py    Anthropic streaming loop: local tools + server-side web tools
        │             ├── tools.py      pandas over CSV/Parquet → JSON + source record
        │             ├── citations.py  pure: block classification, source records
        │             └── budget.py     pure: variance, sensitivity, scenario engine
        ├── scheduler.py   daemon thread → tasks.py → drivers.py / rules.py / alerts.py
        └── store.py       JSON files on disk = all persistence
```

### Module list

| File | Responsibility | Provenance |
|---|---|---|
| `app/budget.py` | **Pure maths**: variance decomposition, exposure/sensitivity, scenario engine. No I/O. | net-new |
| `app/citations.py` | **Pure**: `classify_blocks`, source-record build, URL normalisation, dedup. | net-new |
| `app/config.py` | Merge-over-defaults settings, atomic write | ported from `CFO_Agent_2/app/config.py` |
| `app/profile.py` | Company profile CRUD, `setup_complete`, `render_for_prompt` | net-new |
| `app/store.py` | Session persistence, atomic writes under a lock | **verbatim** |
| `app/drivers.py` | Driver catalog, validated observation append, assumption lock/read | net-new |
| `app/scenarios.py` | Scenario persistence; re-run-with-latest-prices | net-new |
| `app/tools.py` | Local tool schemas + pandas impls + `execute_tool(name, input, ctx)` | fresh (§4) |
| `app/agent.py` | Prompts, `MODEL_CAPS`, `build_tools`, the streaming loop, event vocabulary | loop shape reused, **materially extended** (§2) |
| `app/reporting.py` | `run_session_turn`, `period_key`, attachments, semaphore, `run_agent_to_text` | reused; source/reasoning persistence extended |
| `app/rules.py` | Five threshold rules → findings | fresh, finding shape preserved |
| `app/alerts.py` | Windowed dedup log, narrative + fallback, persistence | reused, dedup swapped (§7) |
| `app/tasks.py` | Task CRUD, `compute_next_run`, `execute_task` | schedule math verbatim; task types fresh |
| `app/scheduler.py` | Daemon thread dispatching due tasks | **verbatim** |
| `app/generate_data.py` | Seeded demo datasets, overview refresh | fresh (§5) |
| `app/main.py` | Routes, SSE, setup endpoints + gating, static, `.env`, lifespan | route *shape* reused, contents fresh |
| `static/{index.html,app.js,style.css}` | The SPA | shell reused, domain views fresh (§9) |
| `static/columbus-tokens.css` | Brand tokens | **verbatim** — all three agents share one look |

`budget.py` and `citations.py` being **leaf modules with zero app imports** is load-bearing:
it is what makes the test strategy in §10 possible without mocking the SDK or touching disk.

**Import discipline** (carried over): `reporting.py` exists purely so `tasks.py` and
`alerts.py` can run agent turns without importing `main.py`. Two refinements in the same
spirit:

- **`agent.py` must not import `config.py`.** Signature becomes
  `run_agent(messages, model, *, reasoning=True, effort="high", research=None, profile=None)`
  — `main.py` and `reporting.py` read config and pass it through. This keeps the loop
  callable from tests with no settings file on disk.
- **`scheduler.__init__` widens from `get_model` to `get_run_params`**, a zero-arg callable
  returning `{model, reasoning, effort, research}`. Same callable-injection pattern that
  exists specifically to avoid the circular import
  (`../Agent 1/CFO_Agent_1/app/scheduler.py:26-28`).

**Reuse ledger — copy with zero edits:**

- `store.py` — its `KINDS` is *already* `("chat", "weekly", "monthly")`
  (`../Agent 1/CFO_Agent_1/app/store.py:17`), exactly the shape chosen here, so the single most
  load-bearing scaffolding assumption needs no change at all.
- `scheduler.py`, and `compute_next_run` + `_validate_schedule` inside `tasks.py`.
- `tests/test_schedule_math.py` — passes unmodified against unmodified schedule math.
- `reporting.period_key` — ISO week / `%Y-%m` is already correct for these two kinds.
- `columbus-tokens.css`.
- `render_chart`'s validator core and the `renderRules` settings editor.

Dependencies unchanged from the reference — `anthropic`, `fastapi`, `uvicorn[standard]`,
`pandas`, `pyarrow`, `numpy`, `pytest`. No new packages: server-side web tools mean no
`httpx`/`bs4`. Pin `anthropic>=0.116.0` (proven in the sibling venv on Python 3.14); the
loop reads `block.type` as a string, so newer server-tool block types degrade gracefully if
SDK typing lags.

---

## 2. The agent loop — the one genuinely novel piece

`CFO_Agent_1`'s `run_agent` (`../Agent 1/CFO_Agent_1/app/agent.py:112-208`) assumes **every** tool
runs locally. Two lines encode that assumption and both break with server-side web tools:

- `agent.py:169` — `if response.stop_reason != "tool_use": break`
- `agent.py:196` — `convo.append({"role": "user", "content": tool_results})`

**a. Capability registry, not inline branching.** Tool assembly becomes
`build_tools(model, research_cfg)` rather than a module-level constant:

```python
MODEL_CAPS = {
  "claude-opus-5":   {"web_search": "20260209", "web_fetch": "20260209",
                      "thinking": "adaptive", "effort": True, "thinking_default_on": True},
  "claude-sonnet-5": {"web_search": "20260209", "web_fetch": "20260209",
                      "thinking": "adaptive", "effort": True, "thinking_default_on": True},
  "claude-opus-4-8": {"web_search": "20260209", "web_fetch": "20260209",
                      "thinking": "adaptive", "effort": True, "thinking_default_on": False},
}
```

`thinking_default_on` records what happens when `thinking` is **omitted** — adaptive on
Opus 5 and Sonnet 5, *no* thinking on Opus 4.8. The loop always sends `thinking` explicitly
(§2f), so nothing branches on this flag; it is here so the asymmetry is written down rather
than rediscovered, and so `AVAILABLE_MODELS` and the `max_tokens` reasoning in §2f can be
sanity-checked against it. `AVAILABLE_MODELS` is derived as `list(MODEL_CAPS)` — one
registry, so a model can never appear in the picker without capability data.

Do **not** additionally declare `code_execution` — the `_20260209` variants run dynamic
filtering internally, and a second execution environment confuses the model. Keep the tool
list deterministically ordered: `tools` renders before `system`, so reordering invalidates
the prompt cache.

**b. Handle `pause_turn`, with a separate budget.** A long web-search turn stops with
`stop_reason == "pause_turn"`; the reference treats that as "finished" and breaks, silently
truncating with no error. Fix: append `{"role": "assistant", "content": response.content}`
and re-issue with **no** user turn and **no** "Continue." message — the API detects the
trailing `server_tool_use` and resumes.

A single `MAX_TOOL_TURNS` is not enough: a citation-dense weekly scan can burn 3–4
iterations on pauses before it ever calls a local tool. Two counters:

```python
MAX_TOOL_TURNS   = 8   # rounds that executed >= 1 local tool
MAX_CONTINUATIONS = 5  # pause_turn resumptions
```

When either cap is hit, emit `{"type": "notice", "message": "research budget reached"}`
before `done`, so truncation is visible rather than silent.

**c. Never dispatch server tools locally, and never POST an empty turn.** Replace the
negative guard with a positive filter plus an emptiness break:

```python
local = [b for b in response.content if b.type == "tool_use"]
if not local:
    break   # never POST content: [] -> 400 -> a research turn becomes an error event
```

`server_tool_use`, `web_search_tool_result` and `web_fetch_tool_result` are never executed
locally. This is now load-bearing rather than incidental, so it gets a comment.

**d. Surface server-tool errors.** These arrive as HTTP 200. Note the asymmetry: on success
`web_search_tool_result.content` is a **list** of results; on error it is a single **object**
carrying `error_code`. Branch on that before indexing, and emit a distinct `web_error`
event — not `error`, which is terminal in the reference's contract. Never raise and never
retry client-side: the model sees the error in its own context and adapts within the turn.

**e. Handle `stop_reason == "refusal"`.** `claude-opus-5` runs safety classifiers and can
return HTTP 200 with `stop_reason: "refusal"` and possibly empty `content`. Check
`stop_reason` **before** reading content; branch on `stop_reason`, never on `stop_details`
(which can be `null` even on a refusal). Opt into server-side fallback by default:
`betas=["server-side-fallback-2026-07-01"]` + `fallbacks="default"`.

**This changes the call site, which is easy to miss.** `betas` and `fallbacks` are accepted
only on the beta messages endpoint, so the stream becomes
`client.beta.messages.stream(...)` — not `client.messages.stream(...)` as the reference uses
at `../Agent 1/CFO_Agent_1/app/agent.py:148`. Two consequences, both benign here: response blocks
arrive as `Beta*` variants (harmless precisely because the loop compares `block.type` as a
string, per §1's SDK-typing note — this is now load-bearing rather than incidental), and the
header/parameter pairing is fixed — `server-side-fallback-2026-07-01` goes with the scalar
`fallbacks="default"`, while the older array form `fallbacks=[{"model": …}]` needs
`-2026-06-01`. Crossing them is a 400. Prefer `"default"`: it routes by refusal category and
needs no maintenance when a pinned fallback model is retired.

**f. Thread the reasoning toggle.** When on, pass
`thinking={"type": "adaptive", "display": "summarized"}` and forward `thinking_delta` as a
new `{"type": "reasoning", "text": ...}` event. When off, send `{"type": "disabled"}` with
effort **clamped to ≤ `high`** (constraint 4). `display: "summarized"` must be explicit —
the default on every model in the picker is `"omitted"`, which still streams `thinking`
blocks but with empty text, so the reasoning disclosure in §9 would render an empty box and
look broken rather than absent. Keep appending `response.content` wholesale
(`agent.py:172`) so thinking blocks survive into the next tool round — that already works
and must not be "tidied".

Raise `max_tokens` from the reference's 16000 to **64000** for every caller. On
`claude-opus-5` thinking is on by default and shares the output budget, and the settings
panel exposes the full effort range: at `xhigh`/`max` a citation-dense weekly scan plus
summarized thinking will truncate under a tighter cap. 64000 is chosen over the more
obvious 32000 deliberately — the failure it prevents is **silent and misattributable**. A
truncated turn stops with `stop_reason: "max_tokens"`, which surfaces as a report that
simply ends mid-sentence; the loop's own truncation signal is the `notice` event about the
research budget (§2b), so the two failures look identical in the UI and the wrong one gets
debugged. All streaming, so the larger cap carries no HTTP-timeout risk.

**g. Split the system prompt into cached and uncached blocks.** The reference marks the
whole system prompt `cache_control: ephemeral` (`agent.py:150-151`). The company profile is
stable per CFO and can live inside that block; today's date or a stale-driver count cannot —
f-stringing those in means zero cache reads forever, with no error to tell you. Use two
blocks: `[{static prompt + rendered profile, cache_control: ephemeral}, {volatile line}]`.
Verify with `usage.cache_read_input_tokens`.

**Know the floor before reading that number.** The minimum cacheable prefix on
`claude-opus-5` is 512 tokens (half the 1024 of `claude-opus-4-8` — the minimum is *not*
monotonic across generations, so it is a per-model fact, not a constant). A prefix below the
floor silently does not cache: `cache_creation_input_tokens: 0`, no error. The static prompt
plus a rendered profile clears 512 comfortably, but the no-key / empty-profile path in §6
may not — so a zero on that path means "too short", not "the volatile block leaked into the
cached one". §12.8 must be run with a confirmed profile or its result is uninterpretable.

**h. Event vocabulary.** Existing: `text`, `tool_call`, `tool_result`, `chart`, `sources`,
`done`, `error`. Add:

| Event | Purpose |
|---|---|
| `reasoning` | Summarized thinking delta (§9) |
| `research` | Emitted on `content_block_start` for a `server_tool_use` — lets the UI show "Searching: chicken meal price index" during an otherwise silent 10–30 s gap. The single highest-value new event for perceived responsiveness. |
| `web_error` | Non-terminal web tool failure |
| `citation` | One per citation on a text block (§3) |
| `notice` | Budget-reached and similar non-fatal notices |

Also fix a latent bug worth not inheriting: the reference's four `except` arms yield `error`
but **not** `done` (`agent.py:200-208`), so a failed turn leaves the frontend's streaming
state uncleared. Yield `done` after `error` in every arm.

---

## 3. The citation model

Data transparency is the requirement and the reason web ingestion was routed through the
server-side tools. `app/citations.py` holds this logic, pure and I/O-free — it is the
testable core of the loop change.

**Unified source record:**

```jsonc
{"kind": "dataset", "id": "data/cost_buildup.parquet",
 "label": "cost_buildup.parquet", "tool": "cost_buildup"}

{"kind": "web", "id": "<normalised url>", "url": "…", "title": "…",
 "accessed": "2026-08-03", "via": "web_search" | "web_fetch", "snippet": null}
```

`kind` + `id` is the dedup key, held in an ordered dict so first-appearance order survives.
`normalise_url` lowercases the host, drops the fragment, strips a trailing slash and
`utm_*`/`ref` params — without it a page that is both searched *and* fetched yields two
chips for one source. Never store `encrypted_content`; it is opaque and large.

Built from three places in the loop: local tool `source_file`s; `web_search_tool_result`
hits; `web_fetch_tool_result` (using its `retrieved_at` as `accessed`).

**`classify_blocks` also returns the visited-URL set**, normalised, alongside the source
records — it does not leave the loop to accumulate that separately. This is a testability
decision, not tidiness: `ctx["fetched_urls"]` is the input to the provenance guard in §4, and
if the loop assembles it inline then the guard's input is produced by the one part of the
codebase §10 deliberately does not test. Returning it from a pure function puts it under
`test_citations.py`, so a bug that quietly leaves the set empty (every write refused) or
over-full (the guard defeated) fails a test instead of a demo. It also forces the set and the
records through the *same* `normalise_url`, which is what makes the guard's comparison
meaningful — see the matching test in §10.

**Citations on text blocks.** With `citations: {"enabled": true}` on `web_fetch`, the
response splits into multiple text blocks and cited ones carry a `citations` array. Streaming
those as deltas is awkward, so do a post-stream pass over `response.content` after
`stream.get_final_message()` and attach each `cited_text` as `snippet` on the matching web
record. Also emit a `citation` event per citation.

**Two persistence changes.**

*Type* — normalise on read so anything already persisted as a plain string renders
byte-identically. Add `session["source_records"]` (list of dicts) alongside the existing
`"sources"` (list of display strings), and have every reader use
`session.get("source_records") or []`. Emit one additive event:
`{"type": "sources", "sources": [str…], "records": [dict…]}` — an older frontend reads
`sources` and ignores `records`.

*Scope* — **this is where the reference pattern actually breaks.** Session-level `sources`
is fine for four dataset files. It is wrong for web citations: a market scan cites a dozen
URLs, and after three turns the session array is a thirty-chip soup pinned to whichever
assistant message happens to be last (`app.js:518`). Citations belong to the turn that made
them. Persist per-message as a UI-only field `msg.sources` — the same category of field as
`msg.charts`, stripped before the API call — while still unioning into `session.sources` for
backward compatibility.

**Inline markers: build them, narrowly.** Normally over-engineering, but `#/weekly` exists
specifically to answer "which number came from where"; a chip row says the answer is
somewhere across twelve pages. The cheap design that doesn't fight the reveal loop: the
backend inserts literal `[3]` characters into the streamed text and emits a matching
`citation` event. The frontend needs zero offset tracking — `[3]` flows through
`renderMarkdown` as ordinary text, and one post-reveal pass over **text nodes** (never a
regex on `innerHTML`, which would corrupt attributes) rewrites it to a `<sup>` link. Run the
rewrite only in the final clean render, never on a reveal tick, or a `.fade-new` span
boundary can split a marker.

Preserve the property that `render_chart` returns **no** source, so charts never pollute the
list — and preserve it for every future presentation-only tool.

The prompt additionally requires: **market figures cite a URL and its retrieval date;
internal figures cite a dataset file.**

---

## 4. Local tool surface (`tools.py`)

Conventions preserved: teachable `{"error": "…"}` dicts enumerating valid options so the
model self-corrects within one turn; `source_file` on every result; all arithmetic in
pandas; `_load` prefers Parquet and falls back to CSV so tests can use CSV fixtures;
presentation via the `_chart_spec` side channel.

**One signature change: `execute_tool(name, tool_input, ctx)`.** `ctx` carries
`{"fetched_urls": set[str], "session_id": str, "profile": dict}`, where `fetched_urls` holds
**normalised** URLs (§3) so the guard below compares like with like. Required because the
write tool must verify a cited URL was actually visited this turn — see below.

| Tool | Purpose |
|---|---|
| `list_datasets` | Discovery: datasets, watchlist drivers, saved scenarios, locked assumptions |
| `query_budget_data` | Escape hatch — filter / group-by / aggregate / sort / limit |
| `variance_analysis` | Actual vs budget vs prior year vs scenario, decomposed **price × volume × mix × joint** |
| `cost_buildup` | €/tonne bridge per product: each ingredient, packaging, freight, labour, energy, overhead |
| `driver_status` | Locked assumption vs latest observation vs staleness, per driver |
| `record_driver_observation` | **Write** — persist a web-researched driver price so pandas can compute on it |
| `driver_sensitivity` | ±X% on named drivers → ΔCOGS / ΔEBITDA / margin pp, plus breakeven shock |
| `build_budget_scenario` | Apply an assumption set to a baseline → full P&L projection; persists |
| `lock_assumptions` | Freeze the agreed assumption set with provenance |
| `project_series` | Trend + seasonality — **volumes and driver price series only** |
| `render_chart` | Presentation only; validates a spec |

### The decomposition (in `budget.py`, pure)

For products *i* with unit price/cost *p* and volume *q*, base 0 → current 1,
`Q = Σq`, `p̄₀ = Σp₀q₀ / Q₀`, `s₁ᵢ = q₁ᵢ/Q₁`:

```
price_effect  = Σ (p₁ᵢ − p₀ᵢ) · q₀ᵢ
volume_effect = (Q₁ − Q₀) · p̄₀
mix_effect    = Q₁ · ( Σ p₀ᵢ·s₁ᵢ − p̄₀ )
joint_effect  = Σ (p₁ᵢ − p₀ᵢ)(q₁ᵢ − q₀ᵢ)
```

These four sum **exactly** to `Σp₁q₁ − Σp₀q₀`. Report `joint_effect` explicitly rather than
folding it into price: the additivity identity is the invariant the tests assert, and hiding
the residual is how variance decompositions silently drift. A CFO cannot act on "COGS is
€1.2M over" but can act on "€900k of it is chicken-meal price, €300k is volume".

Sensitivity, for driver *d* with exposure
`E_d = Σᵢ qty_per_tonne(i,d) · volume(i) · price(d)`:

```
Δcogs     = shock · E_d · (1 − hedge_coverage_d)
Δebitda   = −Δcogs · (1 − price_pass_through)
breakeven = shock at which projected EBITDA margin == floor   (solved analytically)
```

Driving sensitivity off the bill of materials rather than a guessed elasticity is the point:
`E_d` is a fact in the data, and the model never touches the arithmetic.

### Three corrections to the initial tool hypothesis

- **`forecast` renamed to `project_series` and restricted.** A CFO asking "what should next
  year's revenue be" would otherwise get least-squares extrapolation instead of the scenario
  engine. Restrict inputs to volumes and driver price series, and add an explicit prompt rule
  forbidding it for P&L lines — mirroring the reference's rule 7 (`agent.py:63-66`), which
  forbids deriving variances from raw rows. **This is the most likely quality failure in the
  finished app**, so the guard is a prompt rule *and* a schema restriction.
- **`build_budget_scenario` must persist.** Without writing to `data/scenarios.json`, the
  monthly revision has nothing to revise against and the EBITDA-floor rule has no object to
  evaluate. Scenario persistence is load-bearing, not a convenience.
- **Three tools were missing:** `record_driver_observation` (the only path from web research
  to pandas), `driver_status` (what both the monthly report and two rules need), and
  `lock_assumptions`. Splitting `lock_assumptions` out from `record_driver_observation` is
  deliberate — the model must not be able to overwrite the CFO's locked assumption while
  doing routine research.

### `record_driver_observation` — a new trust boundary

Server-side web tools return results into the model's *context*, not to disk, so nothing
downstream can compute on them. The write tool closes that loop: the whole weekly scan
becomes one turn — `web_search` → `web_fetch` → `record_driver_observation` ×N →
`driver_sensitivity` → `render_chart`. The alternative (a second ingestion pass) would need
another LLM call to re-parse the same pages and would run *after* the research turn, so the
scan could not compute on what it had just found.

Because this makes a dataset model-writable, `drivers.py` guards it:

- `driver_id` must exist; `price` finite; `month` parseable.
- **Sanity band:** reject a price outside 0.2×–5.0× the last known value, with a teachable
  error naming the previous value, unless `override_sanity_check: true`. The model can then
  justify a genuine spike rather than silently poisoning the table.
- **`source_url` required *and* verified** against `ctx["fetched_urls"]`, the per-turn set
  §3's `classify_blocks` returns from web tool results. This closes the invented-citation
  hole — the agent cannot record a number attributed to a page it never visited. It is the
  one place in this design where a model mistake would become durable, wrong data.

**Both guards are pure predicates, and they are the two functions in this codebase that most
need tests.** Factor them out of the I/O path rather than writing them inline in the write
tool:

```python
verify_source_url(url, fetched_urls) -> bool          # normalises both sides, then compares
check_sanity_band(price, previous, *, override=False) -> dict | None   # None == accepted
```

`drivers.py` then reads the previous value, calls the predicates, and appends — I/O around a
tested core, the same shape that makes `budget.py` testable. Putting them inline would leave
the single irreversible write in the system verified only by hand in a browser (§12.6),
which is exactly backwards: highest consequence, weakest coverage. `test_driver_guards.py`
in §10 covers them.

The normalisation detail is the one worth stating explicitly, because getting it wrong fails
*closed* and looks like model misbehaviour: if `verify_source_url` compares raw URLs while
`classify_blocks` stored normalised ones, then every legitimate observation is refused with
a provenance error, the agent dutifully retries and reports it cannot verify its own
sources, and nothing in the logs points at the comparison.
- Append-only with a `revision` column; never mutate seeded history. Atomic `.tmp` +
  `Path.replace()` under a lock, because a scheduler worker and an interactive chat can
  write concurrently.

---

## 5. Data layer

Seeded RNG (`RNG_SEED = 42`), fixed anchor (`2026-12-01`), 36 months of history, budget year
2027. CSV + Parquet for every table. Reporting currency EUR with some USD-quoted inputs so
FX genuinely matters.

| Dataset | Grain |
|---|---|
| `budget_vs_actuals` | month × product line × region — volumes, revenue, COGS, opex (actual and budget) |
| `product_lines` | product line — segment, pack format, list price, target margin |
| `bill_of_materials` | product line × input — `qty_per_tonne`, `driver_id` |
| `drivers` | driver — category, unit, quote currency, baseline, hedge coverage, search hint |
| `driver_prices` | month × driver — price, currency, source, `source_url`, `revision` (append-only) |
| `opex_plan` | month × cost centre — amount, headcount, driver link |
| `budget_overview.csv` | KPI rows + `last_refreshed_utc` — deliberately CSV so both readers are exercised |

Unit price and unit cost are **derived in tools** (`revenue / volume`), not stored — one
source of truth, and it is what makes the decomposition trustworthy.

**FX folds into `drivers`/`driver_prices` as `category="fx"`** rather than a separate table.
FX then flows through the same sensitivity machinery for free, and there is one fewer schema
to maintain.

**Story hooks** (the demo narrative), mirroring the reference's approach:

- **Chicken meal +28%** over six months with the assumption locked in September → drift rule
  fires; Poultry Feed has the highest usage per tonne, so its unit cost is up ~9% against a
  ~3% price increase — a large negative price effect in `variance_analysis`.
- **Freight +40%** over two months, only 15% hedged → the largest sensitivity exposure.
- **Wheat −8%** → a partial offset, so the agent is not only delivering bad news.
- **EUR weakening vs USD** → soybean meal rises in EUR on a flat USD price. A clean
  demonstration of arithmetic the model must not do itself.
- **Premium line volume +12% vs budget** → a non-trivial mix effect, which is what makes the
  decomposition worth showing at all.
- **A region opened mid-year with no prior-year budget** → forces "the data isn't there,
  here's what would be needed" (mirrors the reference's UK & Ireland hook).
- **No headcount/payroll dataset** → the agent must say so rather than invent numbers.

---

## 6. Setup / onboarding (net-new — no precedent in either sibling)

Persisted to `data/company_profile.json` using `CFO_Agent_2`'s merge-over-defaults +
atomic-write pattern (`../CFO_Agent_2/app/config.py:34-69`).

```jsonc
{
  "version": 1, "setup_complete": false, "created_at": 0, "updated_at": 0,
  "description": "<CFO free text, verbatim>",
  "company": {"name": "…", "industry": "…", "reporting_currency": "EUR",
              "fiscal_year_start_month": 1, "budget_year": 2027},
  "product_lines": [{"name": "Poultry Feed", "note": "…"}],
  "markets":       [{"name": "Iberia", "currency": "EUR"}],
  "cost_drivers":  [{"driver_id": "chicken_meal", "name": "Chicken meal",
                     "category": "ingredient", "unit": "EUR/t", "quote_currency": "USD",
                     "why": "~22% of poultry feed cost",
                     "search_hint": "chicken meal price index Europe",
                     "sources": [{"url": "…", "title": "…", "accessed": "…"}],
                     "current_value": null, "assumption_value": null,
                     "adverse_direction": "up", "stale_after_days": 7,
                     "confirmed_by_cfo": false}],
  "proposal": {"generated_at": 0, "model": "…", "raw": {…}, "source_records": […]}
}
```

Keeping the agent's original `proposal` separate from the confirmed profile lets the UI diff
the CFO's edits and lets research be re-run without losing their corrections.

**The proposal flow**, run as a real session so it inherits streaming, citations and a
readable transcript for free:

1. `POST /api/profile/propose {description, currency, budget_year, fiscal_year_start}`
   creates a session and streams `run_session_turn` with `SETUP_PROMPT`.
2. The prompt instructs: research the industry and its cost structure with
   `web_search`/`web_fetch`, then call `propose_watchlist` **once** with the full proposal,
   citing a source URL for every driver.
3. **`propose_watchlist` is a local tool whose `input_schema` *is* the proposal schema** —
   this is constraint 2 in action. It validates, writes to `company_profile.json` under
   `proposal` (leaving `setup_complete` false), and returns a compact ack, reusing exactly
   the `_chart_spec` side-channel trick for structured output.
4. The SSE stream terminates with `{"type": "proposal", "profile": {…}}` so the wizard can
   populate the edit form.
5. `POST /api/profile` with the CFO-edited object validates, sets `confirmed_by_cfo` per
   driver, sets `setup_complete: true`, then runs side effects in order: seed the `drivers`
   table from confirmed drivers → generate datasets keyed to the profile's product lines →
   create the default scheduled tasks → `scheduler.wake()`.

**Gating.** A `require_setup()` FastAPI dependency returning `409 {"error":
"setup_incomplete"}` on `/api/sessions*`, `/api/tasks*`, `/api/alerts*`, `/api/rules`,
`/api/datasets*`. `/api/settings` and `/api/profile*` stay open so the settings panel and
wizard function.

**Startup gating.** The reference's `_startup_generate_reports` gates only on the API key
(`../Agent 1/CFO_Agent_1/app/main.py:71`). Here it must gate on `setup_complete` **as well** —
generating a weekly market scan before the agent knows which markets matter burns tokens
producing a report the CFO cannot use.

**No `ANTHROPIC_API_KEY`.** `GET /api/profile` reports `has_api_key: false`, and the wizard
offers two offline paths: fill the profile by hand, or load a bundled demo profile
(`data/demo_profile.json`, the animal-feed persona). `run_agent` already yields a clean
missing-key error event (`agent.py:124-129`) so the propose stream degrades gracefully.
Everything deterministic keeps working: datasets, all local tools via the Data page, rule
evaluation, and alert creation with the finding title as the narrative.

---

## 7. Rules → alerts

`rules.py` is pure pandas over stable tables and never calls the API; `alerts.py` narrates.
Preserve the finding shape exactly —
`{rule_id, entity, period, metric_value, threshold, severity, title}`, verified at
`../Agent 1/CFO_Agent_1/app/rules.py:91-97`. Its `URGENCY_ANALYSIS_PROMPT` is `.format(**finding)`,
so renaming a key breaks prompt formatting at runtime rather than at import.

| `rule_id` | Fires when |
|---|---|
| `driver_moved_since_lock` | \|latest − locked\| / locked > 10% |
| `driver_stale` | A watchlist driver has no observation within its `stale_after_days` |
| `budget_line_variance` | Product-line variance vs budget worse than −5% in the latest closed month |
| `unit_cost_breach` | Actual €/tonne exceeds the locked scenario's assumed unit cost by > 4% |
| `scenario_ebitda_floor` | The locked scenario re-run on latest prices drops EBITDA margin below 8% |

`unit_cost_breach` is the separation-of-drivers rule — it catches cost inflation that volume
growth hides in the amount. `scenario_ebitda_floor` is the "so what" rule and needs nothing
but pandas, so detection stays API-free. No separate FX rule: FX lives in `drivers`, so
`driver_moved_since_lock` covers it.

**Two helpers need fixing rather than copying.**

`_severity` hard-codes `value <= critical_at` (`rules.py:69-70`), i.e. lower-is-worse.
Budget drift is higher-is-worse — a driver moving *up* 20% is the bad case — so it needs an
explicit direction argument. Copying it as-is would silently mark every drift finding
`warning` and never `critical`.

`alerts.create_alert` copies a fixed key set (`alerts.py:71-86`), so richer findings need a
`context` passthrough (driver unit, hedge coverage, product line) or the extra fields
silently vanish.

**Dedup: windowed, with magnitude bucketing.** The reference dedups permanently on
`rule_id:entity:period` (`alerts.py:62-63`). That is wrong here twice over: a driver
legitimately re-breaches (wheat crosses +10% in week 1, +18% in week 3, and the *escalation*
is the news), and `driver_stale` would fire once and then never again as it gets staler.

Port `CFO_Agent_2`'s `recently_alerted(key, window_hours)` / `append_log`
(`../CFO_Agent_2/app/alerts.py:44-59`), with one addition — bucket the magnitude so
escalation re-fires inside the window:

```python
key = f"{rule_id}:{entity}:{bucket(metric_value, step=5)}"
```

Per-rule config carries `dedup_window_hours` and whether the key includes `period`. Driver
rules: no period, 72 h. Period-bucketed rules: with period, 720 h — which reproduces the
reference's permanent-per-period behaviour through the same code path. One mechanism, two
behaviours, configurable.

Keep `MAX_ANALYSES_PER_SCAN = 3` and the deterministic narrative fallback unchanged — alert
creation must never depend on the network.

---

## 8. Scheduled task types

`_validate_schedule` and `compute_next_run` are reused unchanged, which is what gives the
CFO interval/daily/weekly/monthly cadence out of the box.

| Type | Does | Default |
|---|---|---|
| `data_refresh` | Built-in, undeletable, interval-only. Recompute the overview from stable tables + latest observations, then run the rule scan. **No LLM.** | interval 1 h |
| `driver_scan` (kind `weekly`) | Web-heavy market scan; **writes** observations; ends with a sensitivity view. Skips if the ISO week already has one. | weekly, Mon 07:00 |
| `budget_revision` (kind `monthly`) | Re-forecast: local actuals + latest observations, re-run the locked scenario, report the delta and what to re-lock. | monthly, day 3, 07:00 |
| `assumption_refresh` | Cheap, narrative-free pass that only records observations for stale drivers. Uses `run_agent_to_text`; creates no session. | daily 06:00 |
| `drift_scan` | `rules.evaluate` → windowed dedup → narrative → `alerts.json` | interval 1 h |
| `custom_prompt` | Arbitrary scheduled question; lands as a chat in the sidebar | user-defined |

`assumption_refresh` deliberately decouples "keep the data fresh" from "write a report" —
the same split the reference makes between `data_refresh` and `weekly_report`, which is what
lets the refresh run daily and cheaply while the narrative runs weekly.

**Two concrete breakages to fix.** `execute_task` derives the report kind via
`ttype.split("_")[0]` (`tasks.py:315`), which no longer works — replace with an explicit
`TASK_KIND = {"driver_scan": "weekly", "budget_revision": "monthly"}`. And add an optional
per-task `model` override so `assumption_refresh` can run on `claude-sonnet-5` while
`driver_scan` runs on `claude-opus-5`. **The override must be validated against
`MODEL_CAPS`** and fall back to the configured model if absent — it is the one place a model
id enters the system without passing through the settings picker, so it is the one place
constraint 1 could be reintroduced by a hand-edited `tasks.json`.

`period_key` needs **no change** — ISO week for `weekly`, `%Y-%m` for `monthly` is already
exactly right (`../Agent 1/CFO_Agent_1/app/reporting.py:21-27`).

---

## 9. Frontend

Reuse the shell wholesale: hash routing, `showOnly`/`ALL_VIEWS`, the sidebar/folder model,
`renderMarkdown`, the rAF reveal loop (130 ms cadence, `max(14, backlog/5)` chars,
`fade-new` spans, the 120 px auto-scroll rule, the `document.hidden` flush path), hand-built
SVG charts, the `innerHTML`-then-wire-events card pattern, the polling timers, and the
settings panel's four-step "a key becomes a control" pattern.

**Brand discipline is non-negotiable:** ~70% cream canvas / 25% navy / 5% orange reserved
for primary CTAs only; 20 px card radii; pill buttons and chips; soft navy-tinted shadows;
Instrument Serif display + Onest body; no emoji in chrome. `columbus-tokens.css` is copied
verbatim and `style.css` re-maps it onto the same local semantic names — no new brand
colours are invented anywhere in this plan.

### 9.0 Prerequisite refactors — three later sections depend on these

**Extract the reveal loop.** The streaming animation is trapped in the closure of
`sendMessage` (`app.js:594-746`): `assistantText`, `contentEl`, `pendingSources` and
`revealRaf` are locals, and the scroll math hardcodes `messagesEl` even though the *message
helpers* were carefully parameterised on `container`. The setup wizard and the reasoning
disclosure both need the same loop on a different container. Extract
`createStreamRenderer(container, bubble, opts) → {handleEvent, finish, fail}` — body
verbatim with `messagesEl` → `container`, plus an `opts.onEvent` hook and an
`opts.onRevealDone` callback fired next to `addSources`. This is the largest structural
change; everything else is cheap once it exists.

**It is also the critical path, so land it as a no-op first.** Six of the eight items in §9's
build order depend on this extraction, so if it slips they all slip — and because it moves
streaming animation state, a subtle regression here (a dropped `fade-new` boundary, a broken
near-bottom check) shows up later as a *citations* or *reasoning* bug and gets debugged in
the wrong file. Extract it and ship it against the existing chat view alone, with no new
callers and no new features, and confirm the reveal cadence, the 120 px auto-scroll rule and
the `document.hidden` flush path are byte-for-byte unchanged. Only then point the wizard and
the reasoning disclosure at it.

**`addAttachment(file, target)`.** `pendingAttachments` is one module global that
`renderFeature` wipes on every navigation (`app.js:867`); the wizard needs its own array.

**Async boot + a boot view.** `route()` runs synchronously at the bottom of the file
(`app.js:1651-1654`) and `#home-view` ships without `.hidden`, so home paints instantly.
Gating requires an `await` before the first route. Add `#boot-view` as the **only** view
without `hidden` (every other view, including home, gains it), fetch the profile, then
route. No flash of the wrong view, ever, because nothing is visible until `route()` decides.

### Views and routes

`#/` home · `#/setup` (ungated) · `#/chat[/id]` · `#/weekly[/id]` · `#/monthly[/id]` ·
`#/drivers` · `#/scenarios[/id]` · `#/data` · `#/scheduler` · `#/alerts` · settings panel.

The gate goes at the top of `route()`, right after the existing timer cleanup:

```js
if (!profile?.setup_complete && kind !== "setup") { location.replace("#/setup"); return; }
```

**The field name is `setup_complete`, matching what §6 persists and what `GET /api/profile`
returns.** Spelling it `completed` here would read as `undefined` — permanently falsy — so
every route would redirect to `#/setup` forever, including after the CFO confirms the
profile. It fails as a hard lock-out rather than a leak, so it cannot escape a smoke test,
but it also cannot be *diagnosed* from the symptom: the wizard works, the POST succeeds,
`company_profile.json` is correct on disk, and the app still refuses to leave setup. Keep
one name end to end.

`location.replace` (not assignment) so a gated URL never enters history and Back cannot
bounce into it. No loop, because `#/setup` is itself ungated.

**`#/scenarios` — reversing the earlier "no separate view" call.** Three things a
scenario-in-a-chat-thread cannot do: next month's revision must compare against the scenario
locked *last* month (needs an id and an addressable shape); `#/drivers` must name where "the
assumption locked into the budget" came from (needs a link target); and base/upside/downside
comparison is inherently cross-thread. The restraint that keeps it cheap: **creation stays
conversational** — the agent persists a scenario via a tool during a revision, and the view
is read / compare / activate / delete only. One `ALL_VIEWS` entry, one card renderer built
like `taskCard`, one endpoint family. No scenario-authoring form, ever.

Also: no `#/drivers/{id}` route. Expand history inline via a lazily-fetched panel copying
`wirePreview`'s load-once-then-toggle shape (`app.js:1082-1107`), keeping `route()` at ~20
lines — which is a feature of the reference worth protecting.

Division of labour worth stating in the copy: `#/data` answers *"what do you know about
me"*; `#/drivers` answers *"what do you know about the world"*. Don't merge them.

### `#/drivers` — the conceptual heart

Grouped by category (a CFO thinks "my ingredient basket", not "my alphabetical list"), with
a summary strip that is the line telling them whether to trust the budget this morning:
`14 drivers · 11 verified today · 2 stale · 1 could not be verified`.

Each `driverCard(d)` is built exactly like `taskCard`: name, category chip, value **now**,
value **in budget**, the drift between them, the source as a real clickable citation, last
verified, and actions — `Re-verify now` · `History` · `Ask about this driver` · `Edit`.

Three design points that matter:

- **Direction is not sentiment.** A rising ingredient price is bad; a rising sales price is
  good. Never colour the delta by sign. Render the number in neutral `--ink` with
  `tabular-nums` and append an explicit word — `adverse` or `favourable` — derived from the
  server-supplied `adverse_direction`. This is the single easiest way to make a budgeting UI
  lie, so it is worth being explicit about.
- **Staleness is per-driver, not global.** FX is stale in a day, freight in a week,
  inflation in a month. The server supplies `stale_after_days` and `verify_status`. A fresh
  driver gets **no** badge — absence is the calm state; don't decorate eleven healthy rows.
  A stale one gets a 4 px `--cta` left rule, reusing the `.alert-card.unread` device, which
  stays inside the orange budget because it is four pixels.
- **`Ask about this driver`** sets `pendingHomeMessage` and navigates to `#/chat`, copying
  `investigateAlert` (`app.js:1423-1430`) precisely. Pre-seed a prompt and let the lazy
  session path take over — one of the reference's best ideas, transferring verbatim.

Re-verify is an agent turn, so it is slow: `POST` is fire-and-forget, the card shows a
spinner, and the view polls every 2 s **while any driver is running**, reusing the
`renderTaskCards` idiom including its `!hidden` guard. **`route()` must clear
`driverPollTimer`** alongside the existing four (`app.js:1455-1458`) — forgetting it leaks a
2 s fetch loop after navigation, exactly the class of bug the reference centralises against.

### Citations

Normalise at the top of `addSources`; that is the whole backward-compatibility story. Dataset
chips keep `.source-chip` exactly as-is; web chips become an **outline** variant
(transparent fill + inset navy ring — literally the existing header-link treatment) with a
small ↗ glyph. Distinguished by weight and shape, **not hue**. Chip text is the page title
truncated ~34 chars, with the full URL and accessed date in `title`. More than three dataset
chips collapse behind a count chip: the local files are the boring part.

**The security fix (constraint 3), stated as code because it is easy to get wrong:**

```js
try { const u = new URL(src.url); if (!/^https?:$/.test(u.protocol)) throw 0; } catch { /* plain chip */ }
const a = document.createElement("a");
a.href = src.url; a.target = "_blank"; a.rel = "noopener";
a.className = "source-chip is-web"; a.textContent = label;
```

Never `innerHTML` a model-supplied URL anywhere in this codebase.

Timing rule preserved: `pendingSources` and `pendingCitations` are both flushed in the
existing "fully revealed and the stream is over" branch, and both flush immediately on the
`document.hidden` path.

### Reasoning display

A `<details class="reasoning">` **inside** the assistant bubble, above `.content` — the
bubble is the semantic unit of one answer, and the sources row already lives inside it.
Created lazily on the first reasoning event. Summary reads `Reasoning · 8s · 3 steps` with a
spinner while live.

- **Auto-collapse** on the first `text` event, *unless* the user has toggled it themselves —
  directly analogous to "never fight a user who has scrolled up".
- **Interleaving:** reasoning resumes after each tool round. Append to the *same* disclosure
  with a `\n\n` separator and bump the step count; do not create one box per round, or a
  five-tool turn produces five collapsed boxes.
- **Render plainly — do not reuse the reveal loop.** The loop re-renders the whole content
  via `innerHTML` every 130 ms; a second concurrent instance doubles that on the same frame
  budget, and summarized reasoning is frequently longer than the answer. It is for glancing
  at, not close reading. `textContent +=` with `white-space: pre-wrap` — which also makes the
  reasoning path immune to background-tab rAF suspension by construction.
- **Bounded height is load-bearing:** `max-height: 11rem; overflow-y: auto`, self-scrolling.
  Because the outer height barely changes, the existing 120 px near-bottom rule keeps
  working untouched. An unbounded stream would shove the answer off screen, inverting the
  priority.
- **Error path:** `flushReveal` and the `error` branch must clear `.is-live`, or a stream
  that dies mid-reasoning leaves a spinner turning forever.
- **Persisted** as `msg.reasoning`, a UI-only field alongside `msg.charts`, capped ~20 kB so
  session JSON doesn't balloon.
- **Defaults differ by caller:** the toggle governs interactive turns; background runs pass
  reasoning off unconditionally (nobody watches an 03:00 run). The one worthwhile exception
  is the weekly market scan, where the trail is genuinely evidence — per-task opt-in,
  default off.
- **Do not gate it behind `show_debug`.** That serves developers with raw JSON; reasoning
  serves the CFO with prose. Different audience, different control, different typography.

### Charts

**Waterfall: build it.** A waterfall *is* the existing bar chart with a per-bar baseline
instead of one shared `y0`. Spec: `chart_type: "waterfall"`, exactly one series, points
carrying `kind: "absolute" | "delta"`. Renderer changes, all inside `addChart`: accumulate a
running total for the domain; `const base = running; running += v;` in the bar loop, reusing
the existing rounded-path emitter unchanged; colour **by role** (absolute / adverse /
favourable) rather than series index, which stays inside the validated palette because its
warning forbids reordering and swapping, not role-mapping; 1 px connector lines between bar
tops, without which a waterfall reads as random floating bars; and a 3-item role legend,
because the colour coding is otherwise unexplained.

**One override that matters:** `labelEvery = Math.ceil(labels.length/8)` (`app.js:263`)
drops labels, which is fatal here — every bar must be named or the chart is meaningless.
Waterfall forces `labelEvery = 1` and caps at ~9 points in the validator. The existing
≤4-series / ≤36-point caps need a per-type branch.

~60 lines, one new branch, no library. Worth it unambiguously: the budget→actual bridge is
*the* CFO chart, and its whole value is the "which effect dominates" read that a table
cannot deliver.

**Tornado/sensitivity: don't build a chart type.** Reversing the earlier "reuse horizontal
bars" call — the renderer is vertical-only, and transposing it means rewriting `xCenter`,
`y`, tick placement and label placement. That is not a branch, it is a second renderer, and
it will bit-rot. The model can already emit a markdown table, and `renderMarkdown` plus the
navy-header brand table styling renders it well. That covers ~90% of the value at zero cost.
Revisit with in-cell bars (a `<td>` containing a percentage-width div, ~25 lines total) if
the tables feel flat. A tornado chart type would be the first piece of this frontend that
costs more than it returns.

### Home screen

Keep the card anatomy (eyebrow / display-serif h3 / body / orange arrow pinned bottom) and
keep the quick-ask composer and the 5 s-polled schedule overview with its bouncing ball
**verbatim** — that is the best "the agent is working for you" signal in the app. Four cards
(the `auto-fit, minmax(250px, 1fr)` grid reflows to 2×2 with no CSS change): Ask · Market
scan · Budget revision · Budget drivers.

Replace the generic "What would you like to do?" with **budget state**:

```
BUDGET 2027 · DRAFT
Three assumptions have drifted since you locked the budget.
Drift to date: +€4.2M cost · 1.1 pp gross margin
[14 drivers] [3 drifted] [2 stale] [last revision 28 Jul]
```

That `net_delta` line — the cumulative budget effect of everything that has drifted since
lock — is the highest-value pixel on the page; it is what a CFO opens the app to learn. If
the backend cannot compute it, hide the whole block, following the reference's own
graceful-degradation move for the schedules section.

### Settings panel

Sections, most-touched first: **Company profile** (read-only line + `Edit profile` →
`#/setup`) · **Model** · **Reasoning** (toggle + effort, where `applySettings` **disables**
the effort select when the toggle is off, and choosing the deepest level forces the toggle on
— constraint 4, reflected in the payload rather than discovered as a 400) · **Market data
refresh** (change the interval options: 30 s and 1 min are demo pacing and nonsense for
commodity quotes — use hourly / 6-hourly / daily / weekly) · **Driver verification cadence**
· **Drift thresholds** (the reference's `renderRules` editor reused *verbatim* — only labels
and thresholds change; the cleanest reuse in this plan) · **Notifications** · **Debugging**.

Rule thresholds stay in `data/rules.json` behind `/api/rules`, **not** in settings — two
homes for one number is how thresholds drift.

`GET /api/settings` must keep returning `scheduler` and `alerts`: the 10 s frontend poll
drives the refresh badge, the alert bell and browser notifications off that one payload, and
dropping either silently kills three features. Adding a `drivers: {stale, failed}` block
there buys a second header badge with no new timer.

### Copy

`KIND_META` — `chat`: "Ask" / placeholder *"What does soymeal at €412/t do to next year's
gross margin?"* · `weekly`: "Market scan" / "What moved this week in the markets your budget
depends on." · `monthly`: "Budget revision" / "This month's re-forecast: what changed, what
it's worth, and what to do about it."

**One trap:** the strings `kind === "weekly" ? "Weekly" : "Monthly"` and `"week"`/`"month"`
are hardcoded in five places (`app.js:435`, `547`, `930` twice, and the report-poll copy
path). All must read `KIND_META[kind].noun` / `.periodNoun`, or new labels leak "Weekly
report" into a Market-scan banner.

Empty states are written in a CFO's register, not a developer's — e.g. `#/drivers`: *"No
drivers yet. Your budget rests on assumptions whether or not you write them down. Add the
ones that matter, or re-run setup and let the agent propose a watchlist."*

Errors inside the wizard use inline `.error-text`, never `alert()` (which the reference uses
at `app.js:497`, `938`, `1347`) — a modal thrown after five minutes of editing feels
punitive, and the typed description must never be cleared.

---

## 10. Tests

Pure functions get tests; I/O and the API surface do not. Two things in this codebase are
consequential enough that untested is not defensible, and both are pure: the variance and
sensitivity maths (wrong numbers, confidently presented) and the provenance guard (wrong
data, durably written). Everything else is a demo.

| Test file | Covers |
|---|---|
| `test_variance_decomposition.py` | **The headline test.** `price + volume + mix + joint == total Δ` (the additivity identity); a pure price move gives zero volume and zero mix; a mix-only shift gives zero price and zero volume; zero/missing volume returns `{"error": …}` rather than raising `ZeroDivisionError` |
| `test_sensitivity.py` | Exposure equals `Σ qty × volume × price`; +10% with 0% hedge moves COGS by exactly 10% of exposure; 50% hedge halves it; `breakeven_pct` fed back reproduces the floor; a falling cost driver *raises* EBITDA |
| `test_scenario_engine.py` | All-zero assumptions reproduce the baseline exactly; monthly rows sum to the annual total; `EBITDA == revenue − COGS − opex` on every row |
| `test_citations.py` | `classify_blocks` over dict fixtures: server-tool blocks never reach local dispatch; an error object in `content` yields `web_error` and no crash; `render_chart` contributes no source; search + fetch of one URL collapse to one record; **the returned visited-URL set is normalised** and yields exactly one entry for that URL (this is the guard's input — see `test_driver_guards.py`); a legacy session with no `source_records` upgrades cleanly |
| `test_rules.py` | One fires/doesn't-fire pair per rule at the threshold boundary, plus severity escalation (CSV fixtures, viable because `_load` falls back to CSV) |
| `test_alerts_dedup.py` | Same key inside the window suppressed; outside it re-fires; magnitude-bucket escalation re-fires *inside* the window; the cost cap at 3; narrative falls back to the finding title |
| `test_driver_guards.py` | **The provenance boundary** (§4). `verify_source_url`: a URL absent from the set is refused; a URL that differs from the visited one only by trailing slash, host casing or a `utm_*` param is **accepted** (the fails-closed case — both sides must go through `normalise_url`); an empty set refuses everything. `check_sanity_band`: accepted at exactly 0.2× and 5.0×, refused just outside; `override_sanity_check: true` admits a genuine spike; the rejection dict names the previous value so the error is teachable; no previous value (first observation) is accepted rather than crashing |
| `test_schedule_math.py` | **Copied verbatim** — 13 cases, unchanged maths |

Deliberately untested: `main.py` routes, the SSE layer, the streaming shell of `run_agent`,
anything touching the network.

---

## 11. Build order

Maths first, red-to-green, before any I/O exists — then data, then tools, then the loop.

1. `budget.py` + its three test files.
2. `generate_data.py`, `drivers.py` (pure guards first, then the I/O around them) +
   `test_driver_guards.py`, `scenarios.py`.
3. `tools.py`, verifying each tool standalone via `python -m` before the model sees it.
4. `citations.py` + `test_citations.py`, then `agent.py`'s loop rewrite against those helpers.
5. `config.py`, `profile.py`, `reporting.py`.
6. `rules.py`, `alerts.py` + their tests; `tasks.py`, `scheduler.py`.
7. `main.py` last: setup endpoints and gating, then the rest of the route surface.
8. Frontend in the order of §9.0 (prerequisite refactors) → setup wizard → citations →
   reasoning → drivers → home/copy → waterfall → scenarios.

## 12. Verification

1. **Unit tests:** `.venv/bin/python -m pytest -q` — all eight files green.
2. **Cold start, no key:** unset `ANTHROPIC_API_KEY`, launch, confirm the setup view appears
   with the manual fallback, the app is navigable, and no traceback appears.
3. **Setup flow:** with a key, submit a free-text animal-food description; confirm the
   proposal streams, `propose_watchlist` is called once, every proposed driver carries a
   source link, edits persist, and `company_profile.json` is written atomically.
4. **Server-tool loop:** ask a question forcing multi-round web research. In the debug view,
   confirm `server_tool_use` blocks are **not** dispatched locally, a `pause_turn` resumes
   rather than truncating, and the answer carries both web and dataset chips. Verify web
   chips open in a new tab and that a `javascript:` URL in a source is rendered inert.
5. **Every model in the picker:** send one turn on each of the three, with the reasoning
   toggle both on and off, **and at each effort level**. The effort sweep is the point: the
   one remaining 400 in this design is `thinking: {"type": "disabled"}` paired with effort
   `xhigh` or `max` on `claude-opus-5` (constraint 4 — it is accepted at `high` and below, so
   testing only the default hides it), and it is validated *per request*, so a session that
   raises effort mid-conversation fails where earlier turns passed. Confirm the settings
   panel's coupling makes that combination unreachable from the UI, then confirm the clamp in
   `run_agent` refuses it anyway.
6. **Provenance guard:** `test_driver_guards.py` is the real coverage here; this step only
   confirms the guard is *wired in*. Ask the agent to record a driver observation citing a
   URL it never fetched and confirm it is refused with a teachable error rather than written.
   Then run the inverse, which is the case that actually breaks in practice: ask it to record
   an observation from a page it **did** fetch, and confirm the write succeeds. A guard that
   refuses everything passes the first half of this test and is worse than no guard.
7. **Arithmetic provenance:** ask for a variance and independently reproduce the
   decomposition from the CSVs in a REPL. The four effects must sum to the total exactly — if
   the model did the arithmetic, they won't.
8. **Prompt cache:** with a **confirmed profile** (§2g — an empty profile can fall under the
   512-token floor and never cache, which is not the same failure), run two turns in one
   session and confirm `usage.cache_read_input_tokens > 0` on the second. Zero means the
   volatile block leaked into the cached one. Also confirm
   `cache_creation_input_tokens > 0` on the *first* turn — if both are zero the prefix is
   simply too short and the test is measuring nothing.
9. **Scheduler:** create a task at each cadence, confirm the computed next-run times, then
   `Run now` the driver refresh and confirm an observation with a citation lands and a drift
   alert fires. Navigate away from `#/drivers` and confirm the 2 s poll stops.
10. **Browser check:** drive the running app — setup → market scan → drivers → alerts.
    Confirm the waterfall renders with every bar labelled and connectors drawn, that a
    rising cost driver reads *adverse* and a rising sales price reads *favourable*, and that
    the brand ratio holds (cream canvas, orange only on primary CTAs).

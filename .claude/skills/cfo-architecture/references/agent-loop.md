# The agent loop is the novel part

Governs `app/agent.py` — the streaming loop, the model-capability registry and the backend↔browser
event contract. Tested by `tests/test_agent_config.py` and `tests/test_model_tiers.py`.

`app/agent.py` — the reference's loop assumed every tool runs locally; this one mixes
local pandas tools with Anthropic's **server-side** `web_search` / `web_fetch`. Three things there are
correctness-critical and commented as such:

- `stop_reason == "pause_turn"` must **continue** (re-issue with no user turn), not break — treating
  it as "finished" silently truncates a research turn with no error. Bounded by `MAX_CONTINUATIONS`.
- A round with **no local tool uses** must break before appending a user turn, or it POSTs
  `content: []` → 400. `MAX_TOOL_TURNS` counts only rounds that ran ≥1 local tool.
- `MODEL_CAPS` derives tool version, thinking mode, effort and **`fallbacks`** support per model, and
  **`model_request_fields` is the one place a model-dependent request field is written**.
  `AVAILABLE_MODELS` is a subset of the registry — the models whose caps clear what the loop needs of
  every model: the two web tools (this app's premise is cited research), adaptive thinking, effort.
  `fallbacks` is deliberately **not** in that derivation any more. It was, and it had to be, back
  when the loop sent `betas` + `fallbacks` unconditionally — that was a real 400 on every turn for
  anyone who picked sonnet, and excluding the model was the fix available at the time. Gating the two
  lines on the cap is the better fix, and once it exists the requirement genuinely shrinks and sonnet
  becomes offerable. The rule is unchanged and is the thing to preserve: a field only some models
  accept gets gated in `model_request_fields` and dropped from the `AVAILABLE_MODELS` derivation,
  **in that order**. Never widen the list on its own.
  `web_search` and `web_fetch` carry separate version keys deliberately (pre-4.6 dates differ).

## Prompt caching, and why it is two-sided

`build_system` splits the system prompt into a **cached** block (static prompt + stable company
profile) and an **uncached** one (`volatile_context`: today's date, staleness counts, and
`NO_THINKING_GUARDRAIL`). Putting the date in the cached block means zero cache reads forever with
nothing erroring to tell you. Tool order in `build_tools` is part of the cached prefix — reordering
silently invalidates the cache. Render order is `tools` → `system` → `messages`, so the single
breakpoint on the last system block covers **both** tools and system.

The guardrail is uncached for a subtler version of the same reason, and the rule it encodes is the
one to keep: **what belongs above the breakpoint is decided by whether a value varies, not by how
big it is.** It is 99 chars, so its own cost is nil either way — but it varies with the `reasoning`
flag, background tasks run `reasoning=False` and chat runs `reasoning=True`, so above the breakpoint
it produced two prefixes that each warmed a cache the other could never read.

`mark_cache_breakpoint` puts the **message-side** breakpoint on the newest tool-result turn. This is
the half that matters most and the half that was missing: the loop re-POSTs the whole of `convo`
every round, so without it the ~6k-token prefix was cached and every accumulated tool result —
easily tens of thousands of tokens — was re-billed at full price on every subsequent round. Three
constraints hold it together:

- **At most 2 marks alive at once, older ones stripped.** The API allows 4 `cache_control` blocks per
  request and `build_system` spends one. Marks persist on the blocks they were set on, so letting all
  8 tool rounds keep theirs is a hard 400 — on round 4 of a long research turn, not in any short test.
- **Two rather than one**, because a breakpoint looks back only 20 content blocks for a prior entry;
  one round can append a multi-block assistant turn plus a run of tool_result blocks, and a lone
  trailing mark drifts out of range and quietly stops hitting.
- **Tool-result turns only.** The `pause_turn` path appends `response.content` — SDK block objects,
  not dicts — and needs no mark: the next tool-result breakpoint covers everything before it.

`accumulate_usage` / `log_usage` emit one line per turn, and exist because **a cache that stops
working produces no error, just a bigger bill**. Prompt size is the SUM of the three input figures —
`input_tokens` alone is only the uncached remainder, so reading it as "the prompt" makes a
well-cached turn look tiny. `0% cached` on turn 2+ of a session is the signal that something above
invalidated the prefix. Measured on a 3-round tool turn after this landed: 97% cached, 416 uncached
tokens out of 49,451. `main.py` calls `logging.basicConfig` for this — uvicorn leaves the root logger
bare, so app-level INFO would otherwise be dropped silently.

Event vocabulary yielded by `run_agent` (the backend↔browser contract, documented in its docstring):
`text`, `reasoning`, `reasoning_summary`, `research`, `tool_call`, `tool_result`, `chart`, `citation`,
`web_error`, `notice`, `sources`, `done`, `error`. `reasoning_summary` is a Haiku 4.5 one-liner gloss
of one completed thinking block, fired once per burst from `content_block_stop` — cosmetic only, never
persisted (`reporting.run_session_turn` doesn't special-case it, so it passes through to the SSE
stream and nowhere else), and its failure is silently swallowed rather than raised, same as
`web_error` being non-terminal. `error` is **terminal** in the frontend's contract — a failed
web tool must yield `web_error` instead, and every error arm yields `done` after it so the frontend's
streaming state clears.

## See also

- `references/sessions.md` — which of the two configured models runs a turn, and why that decision
  lives in `reporting.model_for_session` rather than here.
- `references/tools-and-datasets.md` — the local tools this loop dispatches, and the `_chart_spec`
  key `render_chart` returns.
- `references/drivers-trust-boundary.md` — why the server-side web tools cannot write to disk
  directly, and the two guards that stand in front of the ones that can.
- `references/data-ingestion.md` — `REASONING_SUMMARY_MODEL` is the same pinned Haiku id the document
  converter uses, deliberately.

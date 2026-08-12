# A session is the only user-visible object

Governs `app/reporting.py`, `app/store.py`, the session JSON under `data/history/{kind}/{id}.json`
and the session routes in `app/main.py`.

Chat, weekly market scan and monthly budget revision are all **sessions** — one JSON file under
`data/history/{kind}/{id}.json`, `KINDS = ("chat", "weekly", "monthly")`. A report is just a session
whose first user message is a preset prompt (`WEEKLY_PROMPT` / `MONTHLY_PROMPT`), so report
generation, report follow-ups, scheduled prompts, the setup proposal stream and ordinary chat all run
through one function, `reporting.run_session_turn`. Child chats under a report carry `parent_id`
**and the report's own kind** — `startThread` and `resolveTargetSession` both call
`apiCreateSession(view.kind, reportId)`, so a follow-up question about a market scan is itself a
`weekly` session. Kind alone therefore does *not* separate a report from a conversation about it;
`parent_id` is the discriminator, which is why `is_conversation` and `model_for_session` both test it
and why the sidebar lists filter on `!s.parent_id`.

**Which of the two configured models runs a turn is decided from that pair**, in the pure
`reporting.model_for_session`: a `weekly`/`monthly` session with no parent is the heavy model
(default `claude-opus-5`), everything else is the general one (default `claude-sonnet-5`). The
decision lives next to the session rather than at the four call sites because the session is the only
place both facts are available — and because `tasks.execute_task` receives an already-resolved params
dict and cannot import `config`, which is the whole reason `get_run_params` is *injected* into the
scheduler. `get_run_params()` stays zero-arg and ships both models; its `model` key is the **general**
one, so every pre-split reader of `params["model"]` — the budget-page narrative, the alert narrative,
driver verification, the assumption refresh — is on the cheap model with no edit, and only the report
path opts up. A per-task override in `tasks.py` sets both slots, or a pinned scan would silently keep
running on the configured heavy model.

Neither the `monthly` nor the `weekly` kind has **a tab of its own**. Both are the same merge, done
twice, and neither changed anything server-side:

- A budget revision exists to produce a scenario, so both live under `#/scenarios`: "+ New scenario"
  creates a `monthly` session, streams one turn seeded with a client-composed build prompt, and the
  agent's `build_budget_scenario` tool persists the result. Past revisions — including everything the
  scheduled `budget_revision` task writes — list in the Revisions panel. Each card also carries
  **Edit assumptions**, which recomputes that scenario in place through the same tool — except the one
  an approved version was frozen from, which shows its frozen state instead.
- A market scan exists to say which drivers moved, so it lives under `#/drivers` as the **"This week"
  strip**: the latest scan's headline, the drifted drivers, and a button into the full transcript.
  `WEEKLY_PROMPT` asks for a headline that stands alone precisely because the strip shows that line
  and nothing else; `scanHeadline()` takes the first line of the first assistant turn and strips
  markdown rather than rendering it. The strip loads **once per visit**, not on the 2 s verify poll —
  and `renderWeekStrip` bails while a scan is running, the same guard `renderLockPanel` uses.

`#/monthly` and `#/weekly` with no id `location.replace` to `#/scenarios` and `#/drivers`; an
individual transcript still opens at `#/{kind}/{id}` in the ordinary thread view, whose sidebar lists
every past one. `NAV_ALIAS = {monthly: "scenarios", weekly: "drivers"}` points each kind at the pill
it now lives under.

UI-only message fields (`attachments`, `text`, `charts`, `reasoning`, per-message `sources`) are
persisted but stripped before the API call.

Every store module (`store`, `profile`, `config`, `tasks`, `alerts`, `rules`, `scenarios`, `drivers`)
writes to `.tmp` then `Path.replace()` under a `threading.Lock`. Keep that pattern — a scheduler
worker and an interactive stream do write concurrently.

## See also

- `references/agent-loop.md` — what `run_session_turn` streams, and the event vocabulary it passes
  through.
- `references/scheduler-and-alerts.md` — the background callers of `run_session_turn` and the
  `AGENT_SEMAPHORE` that keeps them off interactive chat.
- `references/frontend.md` — `startThread`, `resolveTargetSession`, `NAV_ALIAS` and the thread view.

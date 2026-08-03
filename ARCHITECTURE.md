# Architecture — CFO Finance Agent

How this app is built, end to end: process model, backend layers, the agent loop, the
data layer, and exactly how the UI is rendered. Written to double as a blueprint: the
last section marks what is **domain-agnostic scaffolding** (reuse verbatim) versus
**finance-specific** (replace) when building a second agent with the same feel.

---

## 1. At a glance

```
Browser (vanilla JS SPA, hash-routed)
   │  fetch() JSON  +  SSE stream (text/event-stream)
   ▼
FastAPI  (app/main.py)  ── single process, uvicorn
   ├── REST: sessions, settings, tasks, alerts, rules, datasets
   ├── SSE: POST /api/sessions/{id}/chat  → streams agent events
   ├── static mount: /static, "/" → index.html
   └── lifespan: seed data → start scheduler thread → background report generation
        │
        ├── reporting.py   one shared "run a turn against a session" code path
        │      └── agent.py   Anthropic streaming tool-use loop
        │             └── tools.py   pandas over CSV/Parquet, returns JSON + source_file
        ├── scheduler.py   daemon thread → tasks.py (execute) → alerts.py / rules.py
        └── store.py       JSON files on disk = all persistence
```

No database, no message broker, no build step, no frontend framework. Everything is
files on disk plus threads in one Python process. That is deliberate — it is what makes
the app clonable.

**Stack**: Python 3.11+, FastAPI + uvicorn, `anthropic` SDK, pandas + pyarrow, pytest.
Frontend: plain HTML/CSS/JS, no dependencies, no bundler.
Run: `uvicorn app.main:app --port 8000` (see [.claude/launch.json](.claude/launch.json)).

---

## 2. Backend modules

| File | Responsibility | Depends on |
|---|---|---|
| [app/main.py](app/main.py) | FastAPI app: routes, Pydantic request models, SSE endpoint, static mount, `.env` loading, runtime settings, lifespan wiring | everything |
| [app/agent.py](app/agent.py) | System prompt, preset prompts, model list, the streaming tool-use loop, event vocabulary | tools |
| [app/tools.py](app/tools.py) | Anthropic tool JSON schemas + their pandas implementations + `execute_tool` dispatch | — |
| [app/reporting.py](app/reporting.py) | `run_session_turn` — the one path every agent turn goes through; period keys; attachment→content-block conversion; background concurrency semaphore | store, agent |
| [app/store.py](app/store.py) | Session persistence (`data/history/{kind}/{id}.json`), atomic writes under a lock | — |
| [app/tasks.py](app/tasks.py) | Task CRUD in `data/tasks.json`, pure schedule math (`compute_next_run`), task execution | alerts, generate_data, reporting, store |
| [app/scheduler.py](app/scheduler.py) | Daemon thread that dispatches due tasks onto worker threads | tasks, alerts, generate_data |
| [app/rules.py](app/rules.py) | Deterministic threshold rules over the datasets → "findings". No API calls | tools |
| [app/alerts.py](app/alerts.py) | Findings → deduped, agent-narrated alerts in `data/alerts.json` | rules, reporting, agent |
| [app/generate_data.py](app/generate_data.py) | Seeds/refreshes the sample datasets (CSV + Parquet) | — |

**Import discipline**: `reporting.py` exists purely so background modules (`tasks`,
`alerts`) can run agent turns without importing `main.py` — that would be circular. The
scheduler receives `get_model` as a callable for the same reason
([scheduler.py:26](app/scheduler.py#L26)).

---

## 3. The core abstraction: a *session*

Everything the user can read later is a **session** — one JSON file:

```jsonc
{
  "id": "hex", "kind": "chat|weekly|monthly",
  "title": "…", "period": "2026-W29" | "2026-07" | null,
  "parent_id": null,              // set → this is a chat thread nested under a report
  "created_at": 0, "updated_at": 0,
  "messages": [ {"role": "user", "content": "…" | [blocks],
                 "attachments": [...], "text": "…",     // UI-only
                 "charts": [spec]} ],                    // UI-only, on assistant msgs
  "sources": ["data/revenue_monthly.parquet"]
}
```

The key design decision: **a report is not a special type — it is a session whose first
user message is a preset prompt** (`WEEKLY_PROMPT` / `MONTHLY_PROMPT` in
[agent.py:72-85](app/agent.py#L72-L85)). So report generation, report follow-ups,
scheduled prompts and ordinary chat all run through the identical function,
`run_session_turn` ([reporting.py:73](app/reporting.py#L73)). One code path, four
features.

Nesting: a report is a top-level session; chats started under it carry `parent_id`. When
such a thread runs, the parent's first two messages are prepended as context *without
being stored on the child* ([reporting.py:98-101](app/reporting.py#L98-L101)).

**UI-only fields** (`attachments`, `text`, `charts`) are stored but stripped before the
API call ([agent.py:135](app/agent.py#L135)) — they exist so the frontend can re-render a
reopened session faithfully.

Persistence is write-to-`.tmp` then `Path.replace()` under a `threading.Lock`, in every
store module (`store`, `tasks`, `alerts`, `rules`, settings). Atomic, concurrent-safe,
zero setup.

---

## 4. The agent loop

`run_agent(messages, model)` in [agent.py:112](app/agent.py#L112) is a generator. Per
call it loops up to `MAX_TOOL_TURNS = 8` times:

1. `client.messages.stream(...)` with the system prompt (marked
   `cache_control: ephemeral` for prompt caching), `TOOL_DEFINITIONS`, and the
   conversation. `max_tokens=16000` because Sonnet 5's adaptive thinking shares the
   output budget.
2. Forward `text_delta` events as `{type: "text"}`. Thinking deltas are *not* forwarded,
   but the whole `response.content` is appended back to the conversation so thinking
   blocks survive the next round.
3. If `stop_reason != "tool_use"` → break.
4. Otherwise execute each `tool_use` block locally via `execute_tool`, emit
   `tool_call` + `tool_result` events, append `tool_result` blocks as a user turn, loop.

**Event vocabulary** (the contract between backend and browser):

| event | meaning |
|---|---|
| `text` | assistant text delta |
| `tool_call` / `tool_result` | shown only when "Show raw tool calls" is on |
| `chart` | a validated chart spec to draw |
| `sources` | dataset files touched this turn |
| `done` / `error` | terminal |

`main.py` wraps each dict as `data: {json}\n\n` — that is the entire SSE layer
([main.py:216-221](app/main.py#L216-L221)).

Two neat tricks worth carrying over:

- **Paragraph repair**: text resuming after a tool round is prefixed with `\n\n` so
  markdown like a leading `---` isn't glued onto the previous line
  ([agent.py:162](app/agent.py#L162)).
- **The `_chart_spec` side channel**: `render_chart` returns the full validated spec
  under a private key; `agent.py` pops it off, streams it to the UI as a `chart` event,
  and hands the model only a compact ack — the data is already in its context, so
  there's no point paying tokens for it twice
  ([agent.py:180-186](app/agent.py#L180-L186)).

Errors never raise out of the generator: auth / connection / status / catch-all are each
yielded as an `error` event so the SSE stream always terminates cleanly.

---

## 5. Tools

Two kinds live in [tools.py](app/tools.py):

**Data tools** — `list_datasets`, `query_financial_data` (filters/group-by/aggregate/
sort/limit), `compare_periods` (vs budget / prior period / prior year), `trend`
(MoM/YoY/CAGR), `forecast` (least-squares trend + seasonal offsets). Every one returns
`{"source_file": "data/…", …}`; the loop collects those into the `sources` event, and the
system prompt requires the model to cite them.

**Presentation tool** — `render_chart`, which computes nothing and just validates a spec
(≤4 series, ≤36 points, finite numbers) for the UI to draw.

The governing principle, stated in the system prompt and enforced by tool design:
**arithmetic happens in pandas, never in the model.** Variances, percentages, weighted
blended margins, CAGR and projections are all computed server-side, and the prompt
explicitly forbids deriving them from `query_financial_data` rows
([agent.py:63-66](app/agent.py#L63-L66)). Errors are returned as
`{"error": "…"}` dicts with *teachable* messages ("Valid metrics: …") rather than
exceptions, so the model can self-correct within the same turn.

---

## 6. Autonomy: scheduler, rules, alerts

**Scheduler** ([scheduler.py](app/scheduler.py)) is one daemon thread. Each loop it reads
`data/tasks.json`, computes each task's next run, dispatches anything due onto its own
worker thread, then waits on an `Event` until the soonest due time (capped at 30 s, so
file edits are picked up). `wake()` is called after every task mutation. A per-task
`_running_ids` set prevents self-overlap; failures land in `last_status`/`last_error`
rather than killing the thread.

**Schedule math** is a pure function, `compute_next_run`
([tasks.py:228](app/tasks.py#L228)) — which is why it is unit-testable
([tests/test_schedule_math.py](tests/test_schedule_math.py)). Interval tasks count from
the last run, floored at scheduler start *and* task creation, so a restart never replays
an overdue interval and a new task waits a full interval before its first run. Wall-clock
modes (daily/weekly/monthly) return the next future occurrence; missed ones are never
caught up. `day_of_month` clamps to the month length.

Task types: `data_refresh` (built-in, fixed id, undeletable, only its pacing is
editable), `weekly_report`, `monthly_report`, `custom_prompt` (result appears as a chat
in the sidebar), `urgency_scan`.

**Rules → alerts** is a deliberate split:

- [rules.py](app/rules.py) evaluates configurable thresholds in plain pandas against the
  *stable* monthly detail files (not the jittered overview, so alerts don't flap).
  Detection never needs the API. Output: findings.
- [alerts.py](app/alerts.py) dedups on `rule_id:entity:period`, asks the agent to write a
  short briefing per new finding (capped at 3 per scan for cost), and persists. If the
  API is unavailable the narrative falls back to the finding title — alert creation never
  depends on the network.

**Concurrency guard**: background agent runs take
`reporting.AGENT_SEMAPHORE` (bounded at 2). Interactive chat never acquires it, so user
requests can't queue behind scheduled work ([reporting.py:18](app/reporting.py#L18)).

---

## 7. Data layer

Four datasets, each written as both CSV and Parquet by
[generate_data.py](app/generate_data.py) (seeded RNG, fixed anchor month → a stable demo
with deliberate story hooks: UK & Ireland missing budget, Managed Cloud margin eroding).
`tools._load` prefers Parquet and falls back to CSV — which is also what lets the tests
use plain CSV fixtures. The curated overview is intentionally CSV so both readers are
exercised.

Everything on disk, all human-readable:

```
data/
  *.csv, *.parquet          the datasets
  history/{chat,weekly,monthly}/{id}.json    sessions
  tasks.json  alerts.json  rules.json  settings.json
```

---

## 8. How the UI is rendered

Three files, no framework, no build: [static/index.html](static/index.html) (271 lines of
static markup), [static/app.js](static/app.js) (~1650 lines), and
[static/style.css](static/style.css) + [static/columbus-tokens.css](static/columbus-tokens.css).

### 8.1 Structure: all views ship in the HTML

`index.html` contains **every** view as a sibling element — home, feature (chat/reports),
data, scheduler, alerts, plus the settings side panel. None are templated. `showOnly(el)`
toggles a `.hidden` class across the `ALL_VIEWS` array
([app.js:847-850](static/app.js#L847-L850)). The header (logo, topic switcher, Scheduler
/ Data links, refresh badge, alerts bell, gear) is persistent across all of them.

### 8.2 Routing

Hash-based, ~20 lines ([app.js:1454](static/app.js#L1454)). `location.hash` →
`route()` → one of `renderFeature(kind, id)`, `renderData()`, `renderScheduler()`,
`renderAlerts()`, `showHome()`. Routes: `#/`, `#/chat`, `#/chat/{id}`, `#/weekly`,
`#/weekly/{id}`, `#/monthly`, `#/data`, `#/scheduler`, `#/alerts`. Back button works
everywhere for free. `route()` also clears every outstanding timer (report poll, task
countdown, task poll, home schedules) — the single place navigation cleanup happens.

Navigation is by assigning `location.hash`; `history.replaceState` is used when a session
is created lazily mid-turn so the URL gains the id without a new history entry.

### 8.3 Rendering: `innerHTML` from template strings, then wire events

The consistent pattern throughout — e.g. `taskCard`, `alertCard`, `sessionRow`,
`renderDatasetCards`:

```js
const card = document.createElement("div");
card.className = "task-card";
card.innerHTML = `… ${escapeHtml(t.name)} …`;      // build markup
card.querySelector(".task-run").addEventListener(…) // then attach listeners
return card;
```

Every interpolated value goes through `escapeHtml`. There is no virtual DOM and no
diffing: a changed list is simply re-rendered wholesale.

### 8.4 Markdown

A ~55-line hand-rolled renderer, `renderMarkdown`
([app.js:78](static/app.js#L78)): escape first, then a line loop handling tables
(`|…|` with separator-row detection), ATX headings (shifted down one level — the page owns
`h1`), `hr`, `ul`/`ol`, paragraphs; inline pass for `**bold**`, `*italic*`, `` `code` ``.
Lists and tables accumulate and `flush()` on the next non-matching line. No sanitizer
needed because nothing unescaped ever enters.

### 8.5 Streaming with smooth text reveal

This is the piece that makes the app *feel* right, and it is the most subtle code in the
frontend ([app.js:594-746](static/app.js#L594-L746)).

Network chunks accumulate into `assistantText`, but a `requestAnimationFrame` loop
reveals them separately on a fixed **130 ms cadence**, in batches of
`max(14, backlog/5)` characters — so the display never falls far behind a fast stream and
never stutters on a slow one. Each newly revealed slice is wrapped in a
`<span class="fade-new">` whose CSS animation duration is matched to the tick, so text
fades in instead of popping.

The wrapping trick: a `\u0001` sentinel is inserted at the fade boundary *before*
markdown parsing (it survives escaping), then swapped for the opening span afterwards —
the parser auto-closes it at the block end. Only the final line is wrapped (a span can't
cross block elements), table rows are skipped (a sentinel breaks the `|…|` match), and
list/heading markers are stepped over so their regexes still match.

Three more details that matter:

- **Auto-scroll only when already near the bottom** (within 120 px) — never fight a user
  who has scrolled up.
- **`document.hidden` → skip the animation entirely** (`flushReveal`), because rAF is
  suspended in background tabs and the text would otherwise stall.
- **Sources are appended only after the text has fully revealed**, so the chips don't
  appear above still-arriving text.

SSE parsing is manual: `resp.body.getReader()`, decode, split the buffer on `\n\n`, strip
the `data: ` prefix, `JSON.parse`, dispatch in `handleEvent`.

### 8.6 Charts

Charts are drawn as **hand-built SVG strings** — no charting library
([app.js:228-353](static/app.js#L228-L353)). Given a server-validated spec:
union the labels across series in first-appearance order, compute a min/max, generate
round-numbered ticks (`chartTicks`), lay out in a fixed `560×240` viewBox that scales to
the bubble, then emit grid lines, axis labels (≤8 x labels), and either rounded-top bar
paths or polylines. A four-color categorical palette in fixed order
(`CHART_COLORS`, validated for lightness/chroma/CVD/contrast — the comment warns not to
reorder it). On top: a mousemove crosshair with a per-series tooltip, a legend for
multi-series, and a collapsible "View data" table of the exact plotted values.

Charts are stored on the assistant message (`msg.charts`) and re-rendered on reopen.

### 8.7 Polling instead of push

There is no websocket. Live-ness comes from a few timers:

| what | interval | where |
|---|---|---|
| scheduler badge + alert bell (`/api/settings` carries both) | 10 s | [app.js:1640](static/app.js#L1640) |
| home-screen task overview | 5 s | `showHome` |
| task next-run countdowns (client-side math) | 1 s | `renderTaskCards` |
| re-fetch tasks while one is `running` | 2 s | `renderTaskCards` |
| poll for a startup-generated report | 3 s | `scheduleReportPoll` |

Browser notifications ride the same 10 s settings poll: the payload's
`{unread, latest_created_at}` drives the badge, and anything newer than the last poll
fires a `Notification` (deduped by `tag: alert.id`).

### 8.8 Lazy session creation

A "New chat" doesn't hit the server. `resolveTargetSession()`
([app.js:568](static/app.js#L568)) creates the session only when the first message is
actually sent — and it is also where "send from a report view" transparently spins up a
child thread and switches the view into it. The home-screen quick-ask composer stashes the
question in `pendingHomeMessage` and navigates; `renderFeature` consumes it once the chat
view exists (sending immediately would race the `hashchange`).

### 8.9 Styling

[columbus-tokens.css](static/columbus-tokens.css) is a pure design-token file (brand
palette, type scale, spacing grid, radii, shadows, motion, gradients).
[style.css](static/style.css) first maps those onto app-local semantic names
(`--bg`, `--ink`, `--accent`, `--cta`…) and then styles by section. Brand discipline:
~70% cream canvas / 25% navy / **5% orange reserved for primary CTAs only**; 20 px card
radii; pill buttons and chips; soft navy-tinted shadows; Instrument Serif for display +
Onest for body. Static assets are served with `Cache-Control: no-cache` via middleware
plus `?v=11` query strings, because browsers otherwise heuristically cache `app.js` and
users see stale UI ([main.py:152-157](app/main.py#L152-L157)).

---

## 9. Request flows

**A chat turn**
```
composer submit → resolveTargetSession() [create session if needed]
  → POST /api/sessions/{id}/chat {message, model, attachments}
  → run_session_turn: append user msg, auto-title, prepend parent context if a thread
  → run_agent: stream ↔ execute_tool … until stop
  → events → SSE → handleEvent → rAF reveal loop / addChart / addDebugBlock
  → on "done": persist assistant text + charts + sources; refreshSidebar()
```

**Startup**
```
lifespan → seed datasets if missing → scheduler.start()
        → background thread: ensure_report("weekly"), ensure_report("monthly")
UI meanwhile shows a spinner and polls every 3 s until the report has ≥2 messages
```

**A scheduled task**
```
scheduler loop → due? → worker thread → tasks.execute_task
  data_refresh   → regenerate curated overview (jitter) → urgency scan
  *_report       → skip if the period already has one, else generate
  custom_prompt  → new chat session + run_session_turn
  urgency_scan   → rules.evaluate → dedup → agent narrative → alerts.json
                                            → next /api/settings poll → bell + notification
```

---

## 10. Building the next agent

The valuable part of this codebase is the scaffolding, not the finance. Roughly:

**Reuse essentially unchanged** (the "feel"):

- `store.py` — sessions, nesting, atomic JSON persistence. Fully domain-agnostic.
- `reporting.py` — `run_session_turn`, attachment→content-block conversion, the
  background semaphore. Only `period_key` and the preset-prompt lookup are domain-shaped.
- `scheduler.py` + the schedule math in `tasks.py` — untouched except the task-type list.
- `agent.py`'s **loop** — the streaming tool-use machinery and event vocabulary. Only the
  prompts and model list change.
- The **entire frontend shell**: routing, `showOnly`, sidebar/folder model, markdown
  renderer, the streaming reveal animation, SVG charts, card/`escapeHtml` pattern,
  polling timers, settings panel, design tokens. This is where the app's character lives
  and almost none of it knows what a business unit is.
- `main.py`'s route *shape*: `/api/sessions`, `/api/settings`, `/api/tasks`,
  `/api/alerts`, `/api/rules` + the SSE endpoint.

**Replace wholesale** (the domain):

- `tools.py` — new tool schemas and implementations. Keep the conventions:
  `{"error": …}` dicts with teachable messages, a `source_file` on every result, all
  arithmetic server-side, and a presentation tool using the `_chart_spec` side channel.
- `generate_data.py` / the `data/` datasets, and `DATASETS` metadata.
- `rules.py` — different thresholds, same finding shape
  (`rule_id, entity, period, metric_value, threshold, severity, title`) so `alerts.py`
  keeps working untouched.
- `SYSTEM_PROMPT`, `WEEKLY_PROMPT` / `MONTHLY_PROMPT`, `URGENCY_ANALYSIS_PROMPT`.
- The three home-screen cards, `KIND_META`, headings and placeholder copy in the UI.

**Decide deliberately** — these are the load-bearing assumptions, and a different feature
set may want different answers:

- `KINDS = ("chat", "weekly", "monthly")` in `store.py` and the parallel `kind` handling
  in `app.js` (`KIND_META`, the topic switcher, the folder sidebar). Two periodic report
  kinds is a *choice*; a different agent might want one, three, or none.
- `period_key` — ISO week / calendar month. Whatever "one per period" means in the new
  domain goes here.
- Sessions as flat JSON files. Fine to a few thousand sessions; past that, swap `store.py`
  for SQLite behind the same five functions.
- Polling over websockets — simple and adequate, but a chattier agent may want a push
  channel.

**Tests** ([tests/](tests/)) cover exactly the three things worth covering: rule
evaluation, alert dedup/fallback/cost-cap, and schedule math. Keep that ratio — pure
functions get tests, I/O and the API surface don't.

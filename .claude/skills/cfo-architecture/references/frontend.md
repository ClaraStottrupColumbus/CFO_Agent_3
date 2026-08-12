# Frontend

Governs `static/app.js`, `static/index.html`, `static/style.css` and `static/columbus-tokens.css`.
The Budget tab's own JS and CSS are in `references/budget-tab.md`.

`static/app.js` — one file, hash-routed, `showOnly(viewEl)` over `ALL_VIEWS`. Every
view ships in `static/index.html` as a sibling `<main>`; nothing is templated.
Rendering is `innerHTML` from template strings and then attach listeners; a changed list is
re-rendered wholesale. **Three escapes, by position, and they are not interchangeable:** text nodes
through `escapeHtml`, anything inside a quoted attribute through **`escapeAttr`** (`escapeHtml`
leaves quotes alone, so a model-supplied name containing `" onfocus="…` closes the value and opens a
handler without ever needing a `<`), and URLs through `safeHttpUrl` — no amount of escaping stops a
`javascript:` URL, so links are built imperatively with a protocol allowlist. Charts are hand-built SVG strings
(`CHART_COLORS` is validated for lightness/chroma/CVD/contrast — do not reorder). Liveness comes from
polling timers, not websockets; `route()` is the single place navigation clears them.

A bare `#/` is **Home** (`renderHome`) — and so is any unrecognised kind. Home is *not* a second view:
it is `#feature-view` wearing `.is-home`, which hides the sidebar and the header and centres the
composer. That matters, because `sendMessage`, `resolveTargetSession`, `createStreamRenderer` and the
attachment code all close over the module-level `#composer` / `#input` / `#messages` singletons; a
separate home view would mean parameterising every one of them. Only two things differ at runtime:
`view.isHome`, and the two lines at the top of `sendMessage` that drop the `.home-hero` and add
`.has-thread` on the first question. Nothing navigates — `resolveTargetSession`'s `replaceState`
fires no `hashchange`, so the conversation continues on Home while staying recoverable on reload.
`renderFeature` must strip `is-home`/`has-thread`, or the thread view renders with no sidebar.

`#/chat` is **Ask**, the conversation archive (`#ask-view`, `renderAsk`) — a searchable grid of past
chats, not a fresh one. `#/chat/{id}` opens one in the ordinary thread view. Anything that means "ask
a new question" (`+ New question`, an alert investigation, a driver or scenario hand-off) goes through
`goHome()`, which exists because `location.hash = "#/"` fires no event when the hash is already `#/`.

`#topic-switcher` is the app's one navigation surface, and **`route()` is the only place that decides
nav chrome** — one `updateNav(kind || "home")` call after the profile gate sets both visibility and the
active tab. Render functions must not touch the switcher themselves; that is exactly how `#/drivers`
and `#/scenarios` used to inherit whichever pill was highlighted last. `updateNav` hides the nav only
where its links would be traps: `#/setup` (everything else 409s until the profile is confirmed) and
Budget Outlook's first run, read through the synchronous `BudgetPlan.isConfigured()` — safe because
`boot()` awaits `BudgetPlan.load()` before the first `route()`.

`.card` (the surface under every driver, scenario, version and archived conversation) and `.panel`
(the cream-50 module container they group into) live in `style.css`. Three surfaces separate by
lightness alone — cream canvas, cream-50 panel, white card — so section boundaries need no rules
drawn between them. The lock form and the "This week" strip on `#/drivers` and the version rail on
`#/scenarios` add no fourth surface for the same reason; the approved version carries the same 4 px accent rule as the active
scenario, because it means the same thing — *this is the one you are running on*.

`renderLockPanel` returns early while `lockFormOpen`: `refreshDrivers` re-paints every 2 s during a
verify run, and re-rendering the form would wipe what the CFO is typing into it. The scenario edit form
solves the same problem **structurally instead of with a flag**: `#scenario-edit` is a *sibling* of
`#scenarios-body` and `#versions-body`, so a finished build, an activate or a delete repaints the lists
and cannot reach inside the form — the `#bo-cash-form-host` rule again. `editingScenarioId` therefore
lives outside the form too, letting a repaint mark the card being edited without reading an input.
Navigating in closes the form (`renderScenarios` → `closeScenarioEdit`); a repaint never does.

**`.hidden` is scoped per component in both stylesheets, and there is no global rule** — so a component
that toggles the class without declaring it has a collapse that silently does nothing. That was the
History button on `#/drivers` for as long as `.driver-history.hidden` was missing: the handler toggled,
the table stayed, and the button's label never changed either. Declare the rule next to the element,
and give the control a visible state (`History` / `Hide history` plus `aria-expanded`) so a dead toggle
is visible immediately rather than a year later. `.error-text.hidden` and `#scenario-edit-form.hidden`
were missing for the same reason, and `.bo-compare.hidden`, `.bo-cmp-error.hidden`, `.bo-read-tag.hidden`
and `.bo-regen.hidden` are declared next to their elements for the same reason.

One more trap in the same family, in the other direction: **`style.css` ships a bare
`select { max-width: 190px }`**, so any new `<select>` outside `.bo-field` silently inherits it — and
`width: 100%` does not beat a `max-width`. `.bo-compare select` and the settings panel's two model
selects lift it explicitly, with `max-width: none`. That second example used to be `#model-select`,
which set `width: 100%` **and nothing else** — so the one rule this file cited as the good example
was itself clipped to 190px in a 340px panel for as long as it existed. A scenario name clipped to
190px is a stub nobody can tell apart from the next one.

`createStreamRenderer(container, bubble, opts)` is the extracted streaming reveal loop, shared by
chat, the setup wizard and the reasoning disclosure. Its 130 ms cadence, `max(14, backlog/5)` batch
size, `.fade-new` span boundary (via the `\\u0001` sentinel inserted before markdown parsing), the
120 px near-bottom scroll rule and the `document.hidden` flush path are byte-for-byte from the
reference. A regression in any of them surfaces later as a *citations* or *reasoning* bug and gets
debugged in the wrong file. Citation `[n]` markers are rewritten to superscript links in
`finalRender` only — never on a reveal tick, where a fade span can split a marker in half.

Styling: `static/columbus-tokens.css` is pure design tokens;
`static/style.css` maps them to app-local semantic names and then styles by
section. Brand ratio ~70% cream canvas / 25% navy / **5% orange reserved for primary CTAs only**.

**Editing anything under `static/` requires bumping the `?v=N` query string** on all five asset links
in `static/index.html` — see `CLAUDE.md`.

## See also

- `references/agent-loop.md` — the event vocabulary the SSE reader in `app.js` consumes, and why
  `error` is terminal but `web_error` is not.
- `references/sessions.md` — `startThread`, `resolveTargetSession` and `NAV_ALIAS`.
- `references/budget-tab.md` — `static/budget.js` / `static/budget.css`, which follow the same
  repaint-host and `.hidden` rules.

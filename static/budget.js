// budget.js — Budget Outlook: the configuration gate, the overview and the
// scoped chat rail.
//
// Loaded BEFORE app.js. That order matters: this file only declares (it calls
// nothing at parse time), and app.js finishes by calling boot() → route(),
// which needs window.BudgetPlan to already exist. Everything this file borrows
// from app.js — escapeHtml, renderMarkdown, addUserMessage, addAssistantMessage,
// createStreamRenderer, readSSE, scrollDown, showOnly — is referenced inside
// function bodies, so it resolves at call time, long after app.js has run.
//
// Kept out of app.js on purpose. The brief was "must not be a structural copy
// of the other agents"; a separate file is what makes that checkable by
// inspection rather than by discipline. The cost is that index.html now
// version-stamps five assets instead of three.
//
// SECURITY NOTE, and it is the one thing to be careful about in here:
// app.js's escapeHtml escapes & < > but NOT quotes. So no free text is ever
// interpolated into an HTML attribute below. Values that live in attributes are
// slugs from our own controlled id set; user text either goes into a text
// position (via escapeHtml) or is assigned to `.value` / `.textContent` after
// the markup is in the DOM.

(function () {
  "use strict";

  const state = {
    data: null,          // the whole GET /api/budgetplan payload
    showAll: false,
    openId: null,
    streaming: false,
    lastRowFocus: null,
    configVars: [],      // the config screen's working copy
    resetArmed: false,
  };

  const $ = id => document.getElementById(id);

  // ---------- Formatting ----------

  function money(value, currency, { compact = true } = {}) {
    const v = Number(value) || 0;
    const ccy = currency || "EUR";
    if (!compact) {
      return `${ccy} ${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
    }
    const abs = Math.abs(v);
    if (abs >= 1e9) return `${ccy} ${(v / 1e9).toFixed(2)}bn`;
    if (abs >= 1e6) return `${ccy} ${(v / 1e6).toFixed(2)}m`;
    if (abs >= 1e3) return `${ccy} ${(v / 1e3).toFixed(1)}k`;
    return `${ccy} ${v.toFixed(0)}`;
  }

  function signedMoney(value, currency, opts) {
    const sign = Number(value) > 0 ? "+" : Number(value) < 0 ? "−" : "";
    return sign + money(Math.abs(Number(value) || 0), currency, opts);
  }

  function pct(value, digits = 1) {
    const v = Number(value) || 0;
    return `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(digits)}%`;
  }

  // Direction is carried by the glyph and the word, never by colouring the
  // number: a cost line falling is good and a revenue line falling is not, so
  // a red/green figure would be lying half the time.
  const ARROWS = { up: "↑", down: "↓", flat: "→" };
  const WORDS = { up: "up", down: "down", flat: "flat" };

  // ---------- Data ----------

  async function load() {
    try {
      const resp = await fetch("/api/budgetplan");
      state.data = await resp.json();
    } catch {
      state.data = { plan: { configured: false }, derived: null,
                     industries: [], sizes: [], has_api_key: false };
    }
    return state.data;
  }

  function plan() { return (state.data && state.data.plan) || {}; }
  function derived() { return (state.data && state.data.derived) || null; }
  function currency() { return (plan().company || {}).currency || "EUR"; }

  // ---------- Router entry point ----------

  async function route(sub) {
    if (!state.data) await load();
    if (sub === "config") { renderConfig(); return; }
    if (!plan().configured) {
      // First run. `replace`, not assign, so Back cannot bounce into an
      // overview that has nothing to show — the same reasoning as app.js's
      // profile gate, applied to this feature's own independent flag.
      location.replace("#/budget/config");
      return;
    }
    renderOverview();
  }

  // ============================================================
  // Overview
  // ============================================================

  function renderOverview() {
    showOnly($("budget-view"));
    closeDrawer({ silent: true });
    const d = derived();
    const view = $("budget-view");

    if (!d || !d.ranked.length) {
      view.innerHTML = `<div class="bo-empty">
        <p>No budgeting variables are switched on yet.</p>
        <p><a href="#/budget/config">Open configuration</a> and include at least one.</p>
      </div>`;
      return;
    }

    const t = d.totals;
    const company = plan().company || {};
    const industry = (state.data.industries || [])
      .find(i => i.id === company.industry);

    view.innerHTML = `
      <div class="bo-shell">
        <div class="bo-main">
          <section class="bo-hero">
            <div class="bo-eyebrow">
              <span>${escapeHtml(company.name || "")}</span>
              <span class="bo-dot">/</span>
              <span>${escapeHtml(industry ? industry.label : "")}</span>
              <span class="bo-dot">/</span>
              <span>${escapeHtml(String(t.budget_year))} budget</span>
            </div>
            <p class="bo-read is-loading" id="bo-read">Reading the numbers…</p>
            <div class="bo-stats">
              <div class="bo-stat">
                <span class="bo-stat-label">Revenue ${escapeHtml(String(t.budget_year))}</span>
                <span class="bo-stat-value">${escapeHtml(money(t.revenue_next, t.currency))}</span>
                <span class="bo-stat-sub">${escapeHtml(pct(t.revenue_change_pct))} on ${escapeHtml(String(t.current_year))}</span>
              </div>
              <div class="bo-stat">
                <span class="bo-stat-label">Cost base ${escapeHtml(String(t.budget_year))}</span>
                <span class="bo-stat-value">${escapeHtml(money(t.cost_next, t.currency))}</span>
                <span class="bo-stat-sub">${escapeHtml(pct(t.cost_change_pct))} · ${escapeHtml(signedMoney(t.cost_delta, t.currency))}</span>
              </div>
              <div class="bo-stat">
                <span class="bo-stat-label">Operating margin</span>
                <span class="bo-stat-value">${t.margin_pct_next.toFixed(1)}%</span>
                <span class="bo-stat-sub">${escapeHtml(pct(t.margin_delta_pp))
                  .replace("%", "pp")} from ${t.margin_pct_current.toFixed(1)}%</span>
              </div>
            </div>
            <button class="bo-regen" id="bo-regen" type="button">Rewrite this read</button>
          </section>

          <div class="bo-section-head">
            <h2>What moves the ${escapeHtml(String(t.budget_year))} budget</h2>
            <p class="bo-section-note">Ranked by materiality — share of the cost base × expected change</p>
          </div>
          <div class="bo-rows" id="bo-rows"></div>

          <div class="bo-foot">
            <p>${escapeHtml(String(t.included_count))} variable${t.included_count === 1 ? "" : "s"} included${
              t.excluded_count ? `, ${escapeHtml(String(t.excluded_count))} excluded` : ""
            }. These are your own planning assumptions, not a forecast.
            <a href="#/budget/config">Edit configuration</a></p>
          </div>
        </div>

        <aside class="bo-chat">
          <div class="bo-chat-head">
            <h2>Ask about this budget</h2>
            <button class="bo-linkish" id="bo-clear-chat" type="button">Clear</button>
          </div>
          <!-- Starters live INSIDE the scroll area, directly under the scope
               note. In their own flex child below it they were pinned to the
               bottom of an empty rail, reading as a footer rather than as an
               invitation. They are removed on the first send. -->
          <div class="bo-messages" id="bo-messages">
            <div class="bo-starters" id="bo-starters"></div>
          </div>
          <form class="bo-composer" id="bo-composer">
            <textarea id="bo-input" rows="2" placeholder="Ask about next year's budget…"></textarea>
            <button class="bo-send" id="bo-send" type="submit">Send</button>
          </form>
        </aside>
      </div>
      <div class="bo-scrim hidden" id="bo-scrim"></div>
      <aside class="bo-drawer hidden" id="bo-drawer" role="dialog" aria-modal="true"
             aria-labelledby="bo-drawer-title" tabindex="-1"></aside>
    `;

    renderRows();
    renderNarrative();
    renderChat();
    wireOverview();
  }

  function renderRows() {
    const d = derived();
    const t = d.totals;
    const rows = state.showAll ? d.ranked : d.top;
    const host = $("bo-rows");

    host.innerHTML = rows.map(r => `
      <button class="bo-row" type="button" data-id="${r.id}" data-direction="${r.direction}">
        <span class="bo-rank">${r.rank}</span>
        <span>
          <span class="bo-name">${escapeHtml(r.label)}</span>
          <span class="bo-meta">${escapeHtml(r.category)} · ${r.share_of_cost_pct.toFixed(1)}% of cost base</span>
          <span class="bo-bar"><span class="bo-bar-fill" style="width:${Math.max(1, r.impact_share * 100).toFixed(2)}%"></span></span>
        </span>
        <span class="bo-figures">
          <span class="bo-next">${escapeHtml(money(r.next_amount, t.currency))}</span>
          <span class="bo-delta"><span class="bo-arrow">${ARROWS[r.direction]}</span>
            ${escapeHtml(signedMoney(r.delta, t.currency))} · ${escapeHtml(pct(r.expected_change_pct))} ${WORDS[r.direction]}</span>
        </span>
      </button>
    `).join("") + (d.rest.length ? `
      <button class="bo-showall" id="bo-showall" type="button">${
        state.showAll ? `Show top ${d.top.length} only`
                      : `Show all ${d.ranked.length} variables →`
      }</button>` : "");

    host.querySelectorAll(".bo-row").forEach(btn =>
      btn.addEventListener("click", () => openDrawer(btn.dataset.id, btn)));
    const showall = $("bo-showall");
    if (showall) showall.addEventListener("click", () => {
      state.showAll = !state.showAll;
      renderRows();
    });
  }

  function wireOverview() {
    $("bo-regen").addEventListener("click", () => renderNarrative({ force: true }));
    $("bo-composer").addEventListener("submit", e => {
      e.preventDefault();
      const input = $("bo-input");
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      send(text);
    });
    // Enter sends, Shift+Enter newlines — the same contract as the main composer.
    $("bo-input").addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        $("bo-composer").requestSubmit();
      }
    });
    $("bo-clear-chat").addEventListener("click", async () => {
      await fetch("/api/budgetplan/chat/clear", { method: "POST" }).catch(() => {});
      plan().chat = [];
      renderChat();
    });
    $("bo-scrim").addEventListener("click", () => closeDrawer());
    document.addEventListener("keydown", onEsc);
  }

  function onEsc(e) {
    if (e.key !== "Escape") return;
    const drawer = $("bo-drawer");
    if (drawer && !drawer.classList.contains("hidden")) closeDrawer();
  }

  // ---------- The plain-language read ----------

  async function renderNarrative({ force = false } = {}) {
    const el = $("bo-read");
    const btn = $("bo-regen");
    if (!el) return;
    if (force) { el.classList.add("is-loading"); el.textContent = "Rewriting…"; }
    if (btn) btn.disabled = true;
    try {
      const resp = await fetch(`/api/budgetplan/narrative${force ? "?force=true" : ""}`,
                               { method: "POST" });
      if (!resp.ok) throw new Error(String(resp.status));
      const data = await resp.json();
      el.textContent = (data.narrative && data.narrative.text) || "";
    } catch {
      el.textContent = "Could not write the summary. The figures below are computed "
                     + "locally and are unaffected.";
    }
    el.classList.remove("is-loading");
    if (btn) btn.disabled = false;
  }

  // ============================================================
  // Detail drawer
  // ============================================================

  function openDrawer(id, sourceEl) {
    const d = derived();
    const row = d.ranked.find(r => String(r.id) === String(id));
    if (!row) return;
    const t = d.totals;
    const ccy = t.currency;

    state.openId = id;
    state.lastRowFocus = sourceEl || null;
    document.querySelectorAll(".bo-row").forEach(b =>
      b.classList.toggle("is-open", b.dataset.id === String(id)));

    // Margin effect: the delta expressed against next year's revenue, which is
    // the number a CFO actually reacts to. A cost rising costs margin, hence
    // the sign flip.
    const marginPp = t.revenue_next ? (-row.delta / t.revenue_next) * 100 : 0;
    const peak = Math.max(row.current_amount, row.next_amount, 1);
    const note = row.assumption || row.default_note;
    const isDefault = !row.assumption && !!row.default_note;

    const drawer = $("bo-drawer");
    drawer.innerHTML = `
      <div class="bo-drawer-head">
        <span class="bo-drawer-rank">Rank ${row.rank} of ${d.ranked.length} by materiality</span>
        <h2 id="bo-drawer-title">${escapeHtml(row.label)}</h2>
        <span class="bo-drawer-rank">${escapeHtml(row.category)}</span>
        <button class="bo-drawer-close" id="bo-drawer-close" type="button" aria-label="Close">✕</button>
      </div>
      <div class="bo-drawer-body">
        <div class="bo-block">
          <h3>What it is</h3>
          <p>${escapeHtml(row.description || "No description recorded for this variable.")}</p>
        </div>

        <div class="bo-block">
          <h3>How it's derived</h3>
          <div class="bo-derivation">${escapeHtml(
            `${t.current_year} amount        ${money(row.current_amount, ccy, { compact: false })}\n` +
            `expected change      ${pct(row.expected_change_pct)}\n` +
            `${"─".repeat(40)}\n` +
            `${t.budget_year} amount        ${money(row.next_amount, ccy, { compact: false })}\n` +
            `change               ${signedMoney(row.delta, ccy, { compact: false })}`
          )}</div>
          <p>You entered the ${escapeHtml(String(t.current_year))} amount and the expected change in
             configuration. Next year's figure is the one multiplication above — nothing is
             modelled or extrapolated.</p>
        </div>

        <div class="bo-block">
          <h3>The assumption behind it</h3>
          ${note
            ? `<p class="bo-assumption${isDefault ? " is-default" : ""}">${escapeHtml(note)}
                 <span class="bo-source">${isDefault ? "Shipped default — not yet reviewed" : "Your note"}</span>
               </p>`
            : `<p class="bo-assumption is-default">No assumption recorded.
                 <span class="bo-source">Add one in configuration</span></p>`}
        </div>

        <div class="bo-block">
          <h3>Trend</h3>
          <div class="bo-trend">
            <div class="bo-trend-col">
              <span class="bo-trend-value">${escapeHtml(money(row.current_amount, ccy))}</span>
              <span class="bo-trend-bar" style="height:${(row.current_amount / peak * 100).toFixed(1)}%"></span>
              <span class="bo-trend-label">${escapeHtml(String(t.current_year))}</span>
            </div>
            <div class="bo-trend-col is-next">
              <span class="bo-trend-value">${escapeHtml(money(row.next_amount, ccy))}</span>
              <span class="bo-trend-bar" style="height:${(row.next_amount / peak * 100).toFixed(1)}%"></span>
              <span class="bo-trend-label">${escapeHtml(String(t.budget_year))}</span>
            </div>
          </div>
          <p>Two points, because that is all the configuration holds — this year and your
             plan for next. No history is being implied.</p>
        </div>

        <div class="bo-block">
          <h3>Impact on the total budget</h3>
          <dl class="bo-facts">
            <dt>Change in spend</dt><dd>${escapeHtml(signedMoney(row.delta, ccy))}</dd>
            <dt>Share of the ${escapeHtml(String(t.current_year))} cost base</dt><dd>${row.share_of_cost_pct.toFixed(1)}%</dd>
            <dt>Share of total budget movement</dt><dd>${(row.impact_share * 100).toFixed(1)}%</dd>
            <dt>Effect on operating margin</dt><dd>${escapeHtml(pct(marginPp).replace("%", "pp"))}</dd>
            <dt>Cost base without this change</dt><dd>${escapeHtml(money(t.cost_next - row.delta, ccy))}</dd>
          </dl>
          <button class="bo-drawer-ask" id="bo-drawer-ask" type="button">Ask about this variable</button>
        </div>
      </div>
    `;

    $("bo-scrim").classList.remove("hidden");
    drawer.classList.remove("hidden");
    drawer.focus();
    $("bo-drawer-close").addEventListener("click", () => closeDrawer());
    $("bo-drawer-ask").addEventListener("click", () => {
      closeDrawer();
      send(`Talk me through ${row.label} — is ${pct(row.expected_change_pct)} the right `
         + `assumption for ${t.budget_year}, and what should I sanity-check?`);
    });
  }

  function closeDrawer({ silent = false } = {}) {
    const drawer = $("bo-drawer");
    const scrim = $("bo-scrim");
    if (drawer) drawer.classList.add("hidden");
    if (scrim) scrim.classList.add("hidden");
    document.querySelectorAll(".bo-row.is-open").forEach(b => b.classList.remove("is-open"));
    state.openId = null;
    // Focus goes back where it came from, or a keyboard user is dumped at the
    // top of the document every time they close a variable.
    if (!silent && state.lastRowFocus && document.contains(state.lastRowFocus)) {
      state.lastRowFocus.focus();
    }
    state.lastRowFocus = null;
  }

  // ============================================================
  // Chat
  // ============================================================

  function renderChat() {
    const container = $("bo-messages");
    if (!container) return;
    container.innerHTML = "";
    const history = plan().chat || [];

    if (!history.length) {
      const intro = document.createElement("p");
      intro.className = "bo-scope-note";
      intro.textContent = state.data.has_api_key
        ? "Scoped to this budget only. Every answer comes from the configuration on "
          + "this page — nothing else."
        : "Chat needs an ANTHROPIC_API_KEY. The figures on this page are computed "
          + "locally and are unaffected.";
      container.appendChild(intro);
    }

    // Recreated on every render because the container is cleared wholesale.
    const starters = document.createElement("div");
    starters.className = "bo-starters";
    starters.id = "bo-starters";
    container.appendChild(starters);

    history.forEach(m => {
      if (m.role === "user") addUserMessage(container, m.text, []);
      else addAssistantMessage(container, m.text);
    });
    renderStarters();
    scrollDown(container);
  }

  function renderStarters() {
    const host = $("bo-starters");
    if (!host) return;
    const history = plan().chat || [];
    if (history.length || !state.data.has_api_key) { host.innerHTML = ""; return; }

    const d = derived();
    const t = d.totals;
    const top = d.ranked[0];
    const second = d.ranked[1];
    const questions = [
      t.cost_delta >= 0
        ? `What's driving the ${t.budget_year} cost increase?`
        : `Why is the ${t.budget_year} cost base coming down?`,
      `How exposed am I if ${top.label.toLowerCase()} moves further than ${pct(top.expected_change_pct)}?`,
      second
        ? `Which assumption looks weakest — ${top.label.toLowerCase()} or ${second.label.toLowerCase()}?`
        : `Which of my assumptions looks weakest?`,
      `What would it take to hold margin at ${t.margin_pct_current.toFixed(1)}%?`,
    ];

    host.innerHTML = questions.map((_, i) =>
      `<button class="bo-starter" type="button" data-i="${i}"></button>`).join("");
    // Text set after insertion, not interpolated — question strings contain
    // company-derived labels and never touch an attribute.
    host.querySelectorAll(".bo-starter").forEach((btn, i) => {
      btn.textContent = questions[i];
      btn.addEventListener("click", () => send(questions[i]));
    });
  }

  async function send(text) {
    if (state.streaming) return;
    const container = $("bo-messages");
    if (!container) return;

    const intro = container.querySelector(".bo-scope-note");
    if (intro) intro.remove();
    $("bo-starters").innerHTML = "";

    state.streaming = true;
    const sendBtn = $("bo-send");
    if (sendBtn) sendBtn.disabled = true;

    addUserMessage(container, text, []);
    const bubble = addAssistantMessage(container);
    const stream = createStreamRenderer(container, bubble);

    try {
      const resp = await fetch("/api/budgetplan/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      await readSSE(resp, stream.handleEvent);
    } catch (err) {
      stream.fail(err.message);
    } finally {
      stream.finish();
      state.streaming = false;
      if (sendBtn) sendBtn.disabled = false;
      // Re-sync so a reload shows the same transcript the server stored.
      await load();
    }
  }

  // ============================================================
  // Configuration
  // ============================================================

  const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                  "August", "September", "October", "November", "December"];

  function renderConfig() {
    showOnly($("budget-config-view"));
    state.resetArmed = false;
    const p = plan();
    const configured = !!p.configured;
    const company = p.company || {};
    const baseline = p.baseline || {};
    const thisYear = baseline.current_year || new Date().getFullYear();

    state.configVars = (p.variables || []).map(v => Object.assign({}, v));

    const industryOpts = (state.data.industries || []).map(i =>
      `<option value="${i.id}">${escapeHtml(i.label)}</option>`).join("");
    const sizeOpts = (state.data.sizes || []).map(s =>
      `<option value="${s.id}">${escapeHtml(s.label)}</option>`).join("");
    const monthOpts = MONTHS.map((m, i) =>
      `<option value="${i + 1}">${escapeHtml(m)}</option>`).join("");

    $("budget-config-view").innerHTML = `
      <div class="bo-config">
        <div class="bo-config-head">
          <span class="bo-drawer-rank">${configured ? "Configuration" : "First run"}</span>
          <h2>${configured ? "Edit your budget configuration"
                           : "Set up next year's budget picture"}</h2>
          <p>Everything on the overview is derived from what you enter here. Pick your
             industry to load a sensible starting set of cost lines, correct the numbers
             that matter, and switch off anything that doesn't apply.</p>
        </div>

        <form id="bo-config-form">
          <fieldset class="bo-fieldset">
            <legend>Company</legend>
            <p class="bo-legend-note">Used to scope the overview and the chat.</p>
            <div class="bo-grid">
              <label class="bo-field"><span>Company name</span>
                <input type="text" id="bo-c-name" required maxlength="120"></label>
              <label class="bo-field"><span>Industry</span>
                <select id="bo-c-industry">${industryOpts}</select></label>
              <label class="bo-field"><span>Size</span>
                <select id="bo-c-size">${sizeOpts}</select></label>
              <label class="bo-field"><span>Reporting currency</span>
                <input type="text" id="bo-c-ccy" maxlength="3" placeholder="EUR"></label>
              <label class="bo-field"><span>Fiscal year starts</span>
                <select id="bo-c-fy">${monthOpts}</select></label>
            </div>
          </fieldset>

          <fieldset class="bo-fieldset">
            <legend>Baseline</legend>
            <p class="bo-legend-note">This year's revenue anchors the default cost lines and
               the margin on the overview.</p>
            <div class="bo-grid">
              <label class="bo-field"><span>Current year</span>
                <input type="number" id="bo-c-year" min="2000" max="2100" step="1"></label>
              <label class="bo-field"><span>Current-year revenue</span>
                <input type="number" id="bo-c-rev" min="0" step="1000">
                <span class="bo-field-hint">In your reporting currency, whole units.</span></label>
              <label class="bo-field"><span>Expected revenue change</span>
                <input type="number" id="bo-c-revpct" min="-100" max="100" step="0.1">
                <span class="bo-field-hint">Percent, next year vs this year.</span></label>
            </div>
            <div class="bo-actions" style="border:0;padding-top:var(--space-5)">
              <button type="button" class="bo-ghost" id="bo-load-defaults">
                Load ${configured ? "fresh " : ""}defaults for this industry</button>
              <span class="bo-field-hint">Replaces the table below. Your edits are lost.</span>
            </div>
          </fieldset>

          <fieldset class="bo-fieldset">
            <legend>Budgeting variables</legend>
            <p class="bo-legend-note">Amounts are this year's spend. Expected change is what you
               think each line does next year. The overview ranks them by how much they actually
               move the budget, so the amount matters as much as the percentage.</p>
            <div class="bo-vars-wrap">
              <table class="bo-vars">
                <thead><tr>
                  <th class="bo-col-inc" title="Include in the budget">In</th>
                  <th>Variable</th>
                  <th class="bo-col-amount">This year</th>
                  <th class="bo-col-pct">Change %</th>
                  <th class="bo-col-note">Assumption</th>
                  <th class="bo-col-del"></th>
                </tr></thead>
                <tbody id="bo-var-rows"></tbody>
              </table>
            </div>
            <div class="bo-actions" style="border:0;padding-top:var(--space-5)">
              <button type="button" class="bo-ghost" id="bo-add-var">+ Add a variable</button>
            </div>
          </fieldset>

          <p class="bo-error hidden" id="bo-config-error"></p>

          <div class="bo-actions">
            <button type="submit" class="bo-primary" id="bo-save">
              ${configured ? "Save configuration" : "Build my budget overview"}</button>
            ${configured ? `<a class="bo-ghost" href="#/budget"
                              style="text-decoration:none">Cancel</a>` : ""}
            <span class="bo-spacer"></span>
            ${configured ? `<button type="button" class="bo-ghost bo-danger"
                              id="bo-reset">Reset configuration</button>` : ""}
          </div>

          <div class="bo-reset-confirm hidden" id="bo-reset-confirm">
            <p>This clears your company details, every variable and the chat history, and
               returns Budget Outlook to its first-run state. It cannot be undone.</p>
            <button type="button" class="bo-ghost bo-danger" id="bo-reset-yes">Yes, reset everything</button>
            <button type="button" class="bo-ghost" id="bo-reset-no">Cancel</button>
          </div>
        </form>
      </div>
    `;

    // Values are ASSIGNED, never interpolated — escapeHtml does not escape
    // quotes, so a company name containing one would break out of an attribute.
    $("bo-c-name").value = company.name || "";
    $("bo-c-industry").value = company.industry || "other";
    $("bo-c-size").value = company.size || "mid";
    $("bo-c-ccy").value = company.currency || "EUR";
    $("bo-c-fy").value = String(company.fiscal_year_start_month || 1);
    $("bo-c-year").value = String(thisYear);
    $("bo-c-rev").value = baseline.revenue ? String(baseline.revenue) : "";
    $("bo-c-revpct").value = String(baseline.revenue_change_pct || 0);

    if (!state.configVars.length) loadDefaults({ silent: true });
    else renderVarRows();

    wireConfig();
  }

  function renderVarRows() {
    const host = $("bo-var-rows");
    host.innerHTML = state.configVars.map((v, i) => `
      <tr data-i="${i}">
        <td class="bo-col-inc"><input type="checkbox" class="bo-v-inc"></td>
        <td><input type="text" class="bo-v-label" maxlength="80"></td>
        <td class="bo-col-amount"><input type="number" class="bo-v-amount" min="0" step="1000"></td>
        <td class="bo-col-pct"><input type="number" class="bo-v-pct" min="-100" max="100" step="0.1"></td>
        <td class="bo-col-note"><input type="text" class="bo-v-note" maxlength="600"></td>
        <td class="bo-col-del"><button type="button" class="bo-rowdel" aria-label="Remove">✕</button></td>
      </tr>
    `).join("");

    host.querySelectorAll("tr").forEach(tr => {
      const i = Number(tr.dataset.i);
      const v = state.configVars[i];
      const inc = tr.querySelector(".bo-v-inc");
      const label = tr.querySelector(".bo-v-label");
      const amount = tr.querySelector(".bo-v-amount");
      const pctEl = tr.querySelector(".bo-v-pct");
      const note = tr.querySelector(".bo-v-note");

      inc.checked = v.include !== false;
      label.value = v.label || "";
      amount.value = String(v.current_amount ?? 0);
      pctEl.value = String(v.expected_change_pct ?? 0);
      note.value = v.assumption || "";
      note.placeholder = v.default_note || "Why do you expect this?";
      tr.classList.toggle("is-excluded", !inc.checked);

      inc.addEventListener("change", () => {
        v.include = inc.checked;
        tr.classList.toggle("is-excluded", !inc.checked);
      });
      label.addEventListener("input", () => { v.label = label.value; });
      amount.addEventListener("input", () => { v.current_amount = Number(amount.value) || 0; });
      pctEl.addEventListener("input", () => { v.expected_change_pct = Number(pctEl.value) || 0; });
      note.addEventListener("input", () => { v.assumption = note.value; });
      tr.querySelector(".bo-rowdel").addEventListener("click", () => {
        state.configVars.splice(i, 1);
        renderVarRows();     // wholesale re-render; indices would otherwise drift
      });
    });
  }

  async function loadDefaults({ silent = false } = {}) {
    const industry = $("bo-c-industry").value;
    const revenue = Number($("bo-c-rev").value) || 0;
    try {
      const resp = await fetch(
        `/api/budgetplan/defaults?industry=${encodeURIComponent(industry)}&revenue=${revenue}`);
      const data = await resp.json();
      state.configVars = data.variables || [];
    } catch {
      if (!silent) showConfigError("Could not load the defaults for that industry.");
    }
    renderVarRows();
  }

  function showConfigError(message) {
    const el = $("bo-config-error");
    el.textContent = message;
    el.classList.remove("hidden");
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // A table is "untouched" while every amount is still zero — which is exactly
  // the first-run state, because the defaults are sized against a revenue the
  // user has not typed yet. Reloading then is free; reloading after they have
  // entered real numbers would throw their work away, so it is gated on this.
  function varsAreUntouched() {
    return !state.configVars.length
        || state.configVars.every(v => !Number(v.current_amount));
  }

  // `input`, not `change`. On the revenue field `change` only fires on blur, so
  // a user who types a revenue and goes straight for Save ships a table of
  // zeros — the defaults were sized before the revenue existed. Debounced so
  // typing "48000000" is one request, not eight.
  let defaultsTimer = null;
  function reloadDefaultsIfUntouched() {
    clearTimeout(defaultsTimer);
    defaultsTimer = setTimeout(() => {
      if (varsAreUntouched()) loadDefaults({ silent: true });
    }, 250);
  }

  function wireConfig() {
    $("bo-load-defaults").addEventListener("click", () => loadDefaults());
    $("bo-c-industry").addEventListener("input", reloadDefaultsIfUntouched);
    $("bo-c-rev").addEventListener("input", reloadDefaultsIfUntouched);

    $("bo-add-var").addEventListener("click", () => {
      const n = state.configVars.length + 1;
      state.configVars.push({
        id: `custom_${n}_${Math.random().toString(36).slice(2, 7)}`,
        label: "", category: "other", description: "",
        current_amount: 0, expected_change_pct: 0,
        assumption: "", default_note: "", include: true,
      });
      renderVarRows();
      const rows = $("bo-var-rows").querySelectorAll("tr");
      const last = rows[rows.length - 1];
      if (last) last.querySelector(".bo-v-label").focus();
    });

    const reset = $("bo-reset");
    if (reset) {
      const box = $("bo-reset-confirm");
      reset.addEventListener("click", () => {
        state.resetArmed = !state.resetArmed;
        box.classList.toggle("hidden", !state.resetArmed);
      });
      $("bo-reset-no").addEventListener("click", () => {
        state.resetArmed = false;
        box.classList.add("hidden");
      });
      $("bo-reset-yes").addEventListener("click", async () => {
        await fetch("/api/budgetplan/reset", { method: "POST" }).catch(() => {});
        await load();
        state.showAll = false;
        renderConfig();
      });
    }

    $("bo-config-form").addEventListener("submit", submitConfig);
  }

  async function submitConfig(e) {
    e.preventDefault();
    const btn = $("bo-save");
    const err = $("bo-config-error");
    err.classList.add("hidden");

    const payload = {
      company: {
        name: $("bo-c-name").value.trim(),
        industry: $("bo-c-industry").value,
        size: $("bo-c-size").value,
        currency: $("bo-c-ccy").value.trim().toUpperCase(),
        fiscal_year_start_month: Number($("bo-c-fy").value),
      },
      baseline: {
        current_year: Number($("bo-c-year").value),
        revenue: Number($("bo-c-rev").value),
        revenue_change_pct: Number($("bo-c-revpct").value),
      },
      variables: state.configVars,
    };

    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "Saving…";
    try {
      const resp = await fetch("/api/budgetplan/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        // The backend's validators return one teachable sentence; show it
        // verbatim rather than a generic failure.
        const body = await resp.json().catch(() => null);
        const detail = body && body.detail;
        throw new Error((detail && detail.error) || `Could not save (${resp.status}).`);
      }
      await load();
      state.showAll = false;
      // Writing the hash from #/budget/config fires hashchange → route() →
      // renderOverview(). Rendering here as well would run the whole overview
      // twice and, worse, POST /narrative twice. Only render directly when the
      // hash did not actually change and no event is coming.
      const wasAlreadyThere = location.hash === "#/budget";
      location.hash = "#/budget";
      if (wasAlreadyThere) renderOverview();
    } catch (ex) {
      showConfigError(ex.message);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  window.BudgetPlan = { route, load };
})();

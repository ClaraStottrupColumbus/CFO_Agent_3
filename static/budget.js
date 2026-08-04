// budget.js — the Budget tab: the configuration gate, the overview and the
// hand-off rail.
//
// Loaded BEFORE app.js. That order matters: this file only declares (it calls
// nothing at parse time), and app.js finishes by calling boot() → route(),
// which needs window.BudgetPlan to already exist. Everything this file borrows
// from app.js — escapeHtml, escapeAttr, safeHttpUrl, showOnly, askFromBudget —
// is referenced inside function bodies, so it resolves at call time, long after
// app.js has run.
//
// The page has TWO input modes and one engine. In BOM mode every cost line is
// materialised from the datasets and carries the driver_id it was priced from,
// so the drawer can show that driver's locked value, its source URL and what
// the market has done since. In simple mode the CFO types the lines. The
// overview code below does not branch on the mode at all — it branches on
// whether a row has a `driver_id`, which is the only difference that reaches
// the screen.
//
// There is NO chat loop in here. It had one while the feature was tool-less;
// now that a cost line names a real driver, a question about one belongs to the
// main agent, which can actually go and look. Every "ask" affordance therefore
// hands off through app.js's askFromBudget() — the same path an alert, a driver
// card and a scenario card already use.
//
// SECURITY NOTE, and it is the one thing to be careful about in here:
// app.js's escapeHtml escapes & < > but NOT quotes. Anything reaching a quoted
// attribute goes through escapeAttr, and any URL through safeHttpUrl — a driver
// source URL is model-supplied and no amount of escaping stops `javascript:`.
// User text otherwise goes into a text position (via escapeHtml) or is assigned
// to `.value` / `.textContent` after the markup is in the DOM.

(function () {
  "use strict";

  const state = {
    data: null,          // the whole GET /api/budgetplan payload
    showAll: false,
    openId: null,
    lastRowFocus: null,
    configVars: [],      // the config screen's working copy
    resetArmed: false,
    mixLabels: [],       // cost-mix segment labels, applied as titles after render
    cash: null,          // the whole GET /api/cash payload, or null when gated
    cashProposed: true,  // include the capex still marked proposed
    cashBarTitles: [],   // cash-bar titles, applied as properties after render
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

  // The same threshold derive() uses for a row's direction (budgetplan.py
  // FLAT_EPS_PCT), applied to the header figures so a budget that hasn't moved
  // says "flat" instead of pct()'s misleading "+0.0%".
  const FLAT_EPS_PCT = 0.05;
  function direction(value) {
    const v = Number(value) || 0;
    if (Math.abs(v) < FLAT_EPS_PCT) return "flat";
    return v > 0 ? "up" : "down";
  }

  // A trend badge: glyph + word + figure, on one neutral translucent pill. No
  // colour, for the reason above.
  function badge(value, unit = "%") {
    const dir = direction(value);
    const figure = dir === "flat" ? "" :
      ` ${pct(value).replace("%", unit === "pp" ? "pp" : "%")}`;
    return `<span class="bo-metric-badge" data-direction="${dir}">
      <span class="bo-metric-arrow" aria-hidden="true">${ARROWS[dir]}</span>
      <span>${WORDS[dir]}${escapeHtml(figure)}</span></span>`;
  }

  // Clamp a percentage to a meter's 0–100 track. A negative operating margin is
  // real and must not render as a bar hanging off the left of its own track.
  function meterPct(value) {
    return Math.max(0, Math.min(100, Number(value) || 0));
  }

  function num(value) { return Number(value) || 0; }
  function year(value) { return value == null ? "" : String(value); }

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

  // Provenance for a materialised line, keyed by driver_id. Empty (never
  // absent) when there is no watchlist, so callers need no guard.
  function driverMeta(id) {
    return (state.data && state.data.drivers && state.data.drivers[id]) || null;
  }
  function bomAvailable() { return !!(state.data && state.data.bom_available); }

  // The cash profile lives behind a GATED route, unlike everything else on this
  // page: it reads the scenario engine and the datasets, which only exist after
  // setup. A 409 therefore means "not yet", not "broken" — state.cash stays null
  // and the section simply does not render, the same way BOM mode does not offer
  // itself until the datasets are there. The mode follows the data.
  async function loadCash() {
    try {
      const resp = await fetch("/api/cash?include_proposed_capex="
                               + (state.cashProposed ? "true" : "false"));
      state.cash = resp.ok ? await resp.json() : null;
    } catch {
      state.cash = null;
    }
    return state.cash;
  }
  // Every "ask" leaves this page for the main agent. Guarded on setup because
  // the sessions API is gated: offering a button that 409s is worse than saying
  // why it is not there.
  function canAsk() { return !!(state.data && state.data.setup_complete); }

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

  // What the cost base is actually made of, as one segmented bar. It reads off
  // the same `ranked` rows the list below is built from, so the header and the
  // body tell one story rather than two — and it answers "where does the money
  // go" before the reader has scrolled. Five segments at most: past that the
  // slivers are too thin to hit or to label honestly.
  const MIX_MAX = 4;

  function costMix(d, t) {
    const total = num(t.cost_next);
    if (!total) return "";
    const rows = d.ranked.slice().sort((a, b) => num(b.next_amount) - num(a.next_amount));
    const top = rows.slice(0, MIX_MAX);
    const restAmount = rows.slice(MIX_MAX)
      .reduce((sum, r) => sum + num(r.next_amount), 0);

    const segs = top.map((r, i) => ({
      label: r.label || r.id,
      share: num(r.next_amount) / total * 100,
      tone: i,
    }));
    if (restAmount > 0) {
      segs.push({ label: `${rows.length - MIX_MAX} other`, share: restAmount / total * 100, tone: MIX_MAX });
    }

    // Segment labels are user text and a title="" is an attribute, which
    // escapeHtml does not make safe (it leaves quotes alone — see the note at the
    // top of this file). They are stashed here and assigned as properties in
    // wireOverview instead. data-tone and the widths are our own numbers.
    state.mixLabels = segs.map(s => s.label);

    return `<div class="bo-mix">
      <span class="bo-mix-label">Cost base ${escapeHtml(year(t.budget_year))} · what it's made of</span>
      <span class="bo-mix-bar">${segs.map(s =>
        `<span class="bo-mix-seg" data-tone="${s.tone}" style="width:${s.share.toFixed(2)}%"></span>`
      ).join("")}</span>
      <span class="bo-mix-legend">${segs.map(s =>
        `<span class="bo-mix-tag"><span class="bo-mix-swatch" data-tone="${s.tone}"></span>${
          escapeHtml(s.label)} <span class="bo-mix-share">${s.share.toFixed(0)}%</span></span>`).join("")}</span>
    </div>`;
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

    const by = escapeHtml(year(t.budget_year));

    view.innerHTML = `
      <div class="bo-shell">
        <div class="bo-main">
          <section class="bo-hero">
            <div class="bo-eyebrow">
              <span>${escapeHtml(company.name || "")}</span>
              <span class="bo-dot">/</span>
              <span>${escapeHtml(industry ? industry.label : "")}</span>
              <span class="bo-dot">/</span>
              <span>${by} budget</span>
            </div>

            <div class="bo-metrics">
              <div class="bo-metric">
                <span class="bo-metric-label">Revenue ${by}</span>
                <span class="bo-metric-value">${escapeHtml(money(t.revenue_next, t.currency))}</span>
                ${badge(t.revenue_change_pct)}
                <span class="bo-meter" role="presentation">
                  <span class="bo-meter-fill" style="width:${meterPct(
                    num(t.revenue_next) ? num(t.revenue_current) / num(t.revenue_next) * 100 : 0)}%"></span>
                </span>
                <span class="bo-metric-foot">from ${escapeHtml(money(t.revenue_current, t.currency))} in ${escapeHtml(year(t.current_year))}</span>
              </div>

              <div class="bo-metric">
                <span class="bo-metric-label">Cost base ${by}</span>
                <span class="bo-metric-value">${escapeHtml(money(t.cost_next, t.currency))}</span>
                ${badge(t.cost_change_pct)}
                <span class="bo-meter" role="presentation">
                  <span class="bo-meter-fill" style="width:${meterPct(
                    num(t.revenue_next) ? num(t.cost_next) / num(t.revenue_next) * 100 : 0)}%"></span>
                </span>
                <span class="bo-metric-foot">${escapeHtml(signedMoney(t.cost_delta, t.currency))} · ${
                  meterPct(num(t.revenue_next) ? num(t.cost_next) / num(t.revenue_next) * 100 : 0).toFixed(0)
                }% of revenue</span>
              </div>

              <div class="bo-metric">
                <span class="bo-metric-label">Operating margin</span>
                <span class="bo-metric-value">${num(t.margin_pct_next).toFixed(1)}%</span>
                ${badge(t.margin_delta_pp, "pp")}
                <span class="bo-meter" role="presentation">
                  <span class="bo-meter-fill" style="width:${meterPct(t.margin_pct_next)}%"></span>
                  <span class="bo-meter-tick" style="left:${meterPct(t.margin_pct_current)}%"
                        title="${num(t.margin_pct_current).toFixed(1)}% today"></span>
                </span>
                <span class="bo-metric-foot">was ${num(t.margin_pct_current).toFixed(1)}% in ${escapeHtml(year(t.current_year))}</span>
              </div>
            </div>

            ${costMix(d, t)}
          </section>

          <!-- The model's read, moved off the gradient and onto the cream: it is
               prose, and prose is easier to trust at body size on a light
               surface than as a display-serif headline in white. Both ids are
               load-bearing — renderNarrative() and wireOverview() find the
               paragraph and the button by id and are otherwise untouched. -->
          <section class="bo-readout">
            <div class="bo-readout-head">
              <span class="bo-readout-label">The read</span>
              <button class="bo-regen" id="bo-regen" type="button">Rewrite this read</button>
            </div>
            <p class="bo-read is-loading" id="bo-read">Reading the numbers…</p>
          </section>

          <!-- Cash. Empty until renderCash() decides there is something to show:
               the route is gated, so on a fresh install this stays an empty
               section rather than an error the CFO cannot act on. -->
          <section class="bo-cash hidden" id="bo-cash">
            <div class="bo-cash-body" id="bo-cash-body"></div>
            <div id="bo-cash-form-host"></div>
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
          </div>
          <div class="bo-messages" id="bo-messages">
            ${sourceBlock()}
            <p class="bo-scope-note" id="bo-scope-note"></p>
            <div class="bo-starters" id="bo-starters"></div>
          </div>
          <form class="bo-composer" id="bo-composer">
            <textarea id="bo-input" rows="2" placeholder="Ask about next year's budget…"></textarea>
            <button class="bo-send" id="bo-send" type="submit">Ask</button>
          </form>
        </aside>
      </div>
      <div class="bo-scrim hidden" id="bo-scrim"></div>
      <aside class="bo-drawer hidden" id="bo-drawer" role="dialog" aria-modal="true"
             aria-labelledby="bo-drawer-title" tabindex="-1"></aside>
    `;

    renderRows();
    renderNarrative();
    renderCash();
    renderAskRail();
    wireOverview();
  }

  // Where the numbers came from, stated on the page rather than implied. In BOM
  // mode this is the difference between "a budget somebody typed" and "a budget
  // priced off the bill of materials", and it is the one place the rebuild
  // lives — a destructive action, so it is a button with a warning, never
  // automatic.
  function sourceBlock() {
    const linked = (derived()?.ranked || []).filter(r => r.driver_id).length;
    if (!bomAvailable()) {
      return `<div class="bo-source-block">
        <span class="bo-source-mode">Entered by hand</span>
        <p>Every line below is a figure you typed. Once cost datasets exist, this page can
           price them off the bill of materials instead.</p>
      </div>`;
    }
    return `<div class="bo-source-block">
      <span class="bo-source-mode">Priced from your data</span>
      <p>${escapeHtml(String(linked))} of ${escapeHtml(String((derived()?.ranked || []).length))}
         lines are priced through the bill of materials and the opex plan, each against a
         tracked driver. Open a line to see what it is locked at and where that came from.</p>
      <div class="bo-source-actions">
        <a class="bo-linkish" href="#/drivers">Drivers &amp; locked assumptions →</a>
        <button class="bo-linkish" id="bo-rebuild" type="button">Rebuild from my data</button>
      </div>
      <p class="bo-rebuild-warn hidden" id="bo-rebuild-warn">This replaces every line and every
         note you have edited here with the figures in the datasets. It cannot be undone.
         <button class="bo-linkish" id="bo-rebuild-yes" type="button">Yes, rebuild</button>
         <button class="bo-linkish" id="bo-rebuild-no" type="button">Cancel</button></p>
    </div>`;
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
    // Assigned as a property, never interpolated into the markup: see costMix.
    document.querySelectorAll("#budget-view .bo-mix-seg").forEach((el, i) => {
      el.title = (state.mixLabels || [])[i] || "";
    });
    $("bo-composer").addEventListener("submit", e => {
      e.preventDefault();
      const input = $("bo-input");
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      ask(text);
    });
    // Enter sends, Shift+Enter newlines — the same contract as the main composer.
    $("bo-input").addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        $("bo-composer").requestSubmit();
      }
    });
    wireRebuild();
    $("bo-scrim").addEventListener("click", () => closeDrawer());
    document.addEventListener("keydown", onEsc);
  }

  function wireRebuild() {
    const btn = $("bo-rebuild");
    if (!btn) return;                       // simple mode: nothing to rebuild from
    const warn = $("bo-rebuild-warn");
    btn.addEventListener("click", () => warn.classList.toggle("hidden"));
    $("bo-rebuild-no").addEventListener("click", () => warn.classList.add("hidden"));
    $("bo-rebuild-yes").addEventListener("click", async () => {
      const yes = $("bo-rebuild-yes");
      yes.disabled = true;
      yes.textContent = "Rebuilding…";
      try {
        const resp = await fetch("/api/budgetplan/rebuild", { method: "POST" });
        if (!resp.ok) {
          const body = await resp.json().catch(() => null);
          throw new Error((body && body.detail && body.detail.error)
                          || `Could not rebuild (${resp.status}).`);
        }
        await load();
        state.showAll = false;
        renderOverview();
      } catch (ex) {
        warn.textContent = ex.message;
      }
    });
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
  // Cash — the one thing a budget can be entirely right about and still miss
  // ============================================================
  //
  // Everything here is computed by app/cash.py through the SAME
  // cash_flow_projection the agent calls, so the number on this page and the
  // number in an answer cannot disagree. Two rules the markup follows:
  //
  //   * The body and the form render into separate hosts. Toggling "defer the
  //     proposed capex" re-renders the body only, so it cannot wipe half-typed
  //     values out of the form — the same problem renderLockPanel solves with
  //     lockFormOpen, solved here by never re-rendering the form at all.
  //   * A day count is labelled measured or stated wherever it appears. It is
  //     the whole provenance of the section: a measurement is a fact off the
  //     balance sheet, a stated day is the CFO deciding something.

  async function renderCash({ reload = true } = {}) {
    const section = $("bo-cash");
    if (!section) return;
    if (reload) await loadCash();
    const cash = state.cash;
    if (!cash) { section.classList.add("hidden"); return; }
    section.classList.remove("hidden");
    renderCashBody();
    if (!$("bo-cash-form")) renderCashForm();
    // With no profile there is nothing to render but the fix, so the form opens
    // itself rather than hiding behind a button the empty state does not draw.
    if (!cash.available) {
      const form = $("bo-cash-form");
      if (form) form.classList.remove("hidden");
    }
  }

  function renderCashBody() {
    const host = $("bo-cash-body");
    const cash = state.cash;
    if (!host || !cash) return;
    const ccy = currency();

    if (!cash.available) {
      // Not a failure: "we have not measured your working capital yet" is a
      // state, and the fix is the form immediately below.
      host.innerHTML = `<div class="bo-cash-head">
          <span class="bo-cash-label">Cash</span>
          <span class="bo-cash-basis">not yet measurable</span>
        </div>
        <p class="bo-cash-empty">${escapeHtml(cash.error
          || "The cash profile needs either a working-capital history or your own "
           + "collection, inventory and payment terms.")}</p>`;
      return;
    }

    const t = cash.totals || {};
    const days = cash.days || {};
    const trough = cash.trough || {};
    const breach = !!cash.breaches_minimum;
    const stated = (days.stated || []).length;
    const basisNote = days.basis === "measured"
      ? `measured from ${escapeHtml(String(days.measured_from || "your balance sheet"))}`
      : days.basis === "stated" ? "the terms you stated"
      : `measured, with ${stated} you stated`;

    host.innerHTML = `
      <div class="bo-cash-head">
        <span class="bo-cash-label">Cash ${escapeHtml(String(cash.year || ""))}</span>
        <span class="bo-cash-basis">${basisNote}</span>
      </div>

      <div class="bo-cash-metrics">
        <div class="bo-metric">
          <span class="bo-metric-label">Free cash flow</span>
          <span class="bo-metric-value">${escapeHtml(signedMoney(t.free_cash_flow_eur, ccy))}</span>
          <span class="bo-metric-foot">on ${escapeHtml(money(t.ebitda_eur, ccy))} of EBITDA${
            t.cash_conversion_pct == null ? ""
              : ` · ${num(t.cash_conversion_pct).toFixed(0)}% converts`}</span>
        </div>
        <div class="bo-metric" data-breach="${breach}">
          <span class="bo-metric-label">Cash low point</span>
          <span class="bo-metric-value">${escapeHtml(money(trough.closing_cash_eur, ccy))}</span>
          <span class="bo-metric-foot">${escapeHtml(String(trough.month || "—"))}${
            cash.min_cash_eur == null ? ""
              : breach ? ` · below your ${escapeHtml(money(cash.min_cash_eur, ccy))} floor`
                       : ` · clears your ${escapeHtml(money(cash.min_cash_eur, ccy))} floor`}</span>
        </div>
        <div class="bo-metric">
          <span class="bo-metric-label">Cash conversion cycle</span>
          <span class="bo-metric-value">${num(days.cash_conversion_cycle_days).toFixed(0)} days</span>
          <span class="bo-metric-foot">DSO ${num(days.dso_days).toFixed(0)} ·
            DIO ${num(days.dio_days).toFixed(0)} · DPO ${num(days.dpo_days).toFixed(0)}</span>
        </div>
      </div>

      ${cashChart(cash, ccy)}

      <div class="bo-cash-steps">${[
        ["EBITDA", t.ebitda_eur],
        ["Working capital", -num(t.working_capital_change_eur)],
        ["Cash tax", -num(t.tax_eur)],
        ["Capex", -num(t.capex_eur)],
      ].map(([label, value]) => `<span class="bo-cash-step">
          <span class="bo-cash-step-label">${escapeHtml(label)}</span>
          <span class="bo-cash-step-value">${escapeHtml(signedMoney(value, ccy))}</span>
        </span>`).join("")}</div>

      <div class="bo-cash-actions">
        <label class="bo-cash-toggle">
          <input type="checkbox" id="bo-cash-defer" ${state.cashProposed ? "" : "checked"}>
          <span>Defer the capex still marked proposed${
            cash.capex && cash.capex.proposed_eur
              ? ` (${escapeHtml(money(cash.capex.proposed_eur, ccy))})` : ""}</span>
        </label>
        ${canAsk() ? `<button class="bo-linkish" id="bo-cash-ask" type="button">Ask about the
          low point</button>` : ""}
        <button class="bo-linkish" id="bo-cash-edit" type="button">Cash assumptions</button>
      </div>

      <p class="bo-cash-caveat">${escapeHtml((cash.caveats || [])[0] || "")}</p>`;

    // Titles are user-independent here, but they are still assigned as
    // properties rather than interpolated — one rule, no exceptions.
    host.querySelectorAll(".bo-cash-bar").forEach((el, i) => {
      el.title = state.cashBarTitles[i] || "";
    });
    wireCashBody();
  }

  // Twelve bars of closing cash, with the floor drawn across them. This is the
  // shape a P&L cannot show: EBITDA can be flat while cash dips under the line
  // in one month because the capex and the working-capital build land together.
  function cashChart(cash, ccy) {
    const months = cash.months || [];
    if (!months.length) return "";
    const peak = Math.max(...months.map(m => num(m.closing_cash_eur)), 1);
    const floor = cash.min_cash_eur;
    const below = new Set(cash.months_below_minimum || []);
    state.cashBarTitles = months.map(m =>
      `${m.month}: ${money(m.closing_cash_eur, ccy)} closing`);

    const floorLine = floor == null ? "" :
      `<span class="bo-cash-floor" style="bottom:${
        Math.max(0, Math.min(100, num(floor) / peak * 100)).toFixed(2)}%"></span>`;

    // The bars and the floor share one positioning context, so `bottom: N%` on
    // the floor and `height: N%` on a bar resolve against the SAME box. With the
    // axis inside that box they did not, and the line sat a few pixels above
    // where the arithmetic put it — which on a covenant line is not cosmetic.
    return `<div class="bo-cash-chart">
      <span class="bo-cash-bars">
        ${floorLine}
        ${months.map(m => `<span class="bo-cash-bar" data-breach="${below.has(m.month)}"
          style="height:${Math.max(1, num(m.closing_cash_eur) / peak * 100).toFixed(2)}%"></span>`
        ).join("")}
      </span>
      <span class="bo-cash-axis">
        <span>${escapeHtml(String((months[0] || {}).month || ""))}</span>
        <span>${escapeHtml(String((months[months.length - 1] || {}).month || ""))}</span>
      </span>
    </div>`;
  }

  function wireCashBody() {
    const defer = $("bo-cash-defer");
    if (defer) defer.addEventListener("change", async () => {
      state.cashProposed = !defer.checked;
      defer.disabled = true;
      await renderCash();                 // body only — the form is left alone
    });
    const ask = $("bo-cash-ask");
    if (ask) ask.addEventListener("click", () => askAboutCash());
    const edit = $("bo-cash-edit");
    if (edit) edit.addEventListener("click", () => {
      const form = $("bo-cash-form");
      if (form) {
        form.classList.toggle("hidden");
        if (!form.classList.contains("hidden")) form.querySelector("input").focus();
      }
    });
  }

  // The cash inputs. Every one is optional, and empty means "not stated" rather
  // than zero — a blank DSO is measured off the balance sheet, a blank tax rate
  // means no tax is deducted and the projection says so. The placeholders carry
  // the measured figure so the CFO can see what they are overriding.
  function renderCashForm() {
    const host = $("bo-cash-form-host");
    const cash = state.cash;
    if (!host || !cash) return;
    const wc = cash.working_capital || {};
    const days = cash.days || {};
    const measured = f => (days[f] == null ? "measured" : `measured: ${num(days[f]).toFixed(0)}`);
    const value = v => (v == null ? "" : String(v));

    const fields = [
      ["dso_days", "Days sales outstanding", measured("dso_days")],
      ["dio_days", "Days inventory outstanding", measured("dio_days")],
      ["dpo_days", "Days payable outstanding", measured("dpo_days")],
      ["opening_cash_eur", "Opening cash", "from your balance sheet"],
      ["tax_rate_pct", "Cash tax rate (%)", "not stated — no tax deducted"],
      ["min_cash_eur", "Minimum cash / facility floor", "none tested"],
    ];

    host.innerHTML = `<form class="bo-cash-form hidden" id="bo-cash-form">
      <p class="bo-cash-form-note">Leave a field empty to measure it from your own balance
         sheet, or to leave it out of the projection entirely. A number you type here is a
         decision, and it is labelled as one wherever it appears.</p>
      <div class="bo-cash-grid">${fields.map(([id, label, hint]) => `
        <label class="bo-field"><span>${escapeHtml(label)}</span>
          <input type="number" step="any" id="bo-wc-${escapeAttr(id)}"
                 placeholder="${escapeAttr(hint)}"></label>`).join("")}
      </div>
      <label class="bo-field bo-cash-note-field"><span>Note</span>
        <input type="text" id="bo-wc-note" maxlength="280"></label>
      <div class="bo-cash-form-actions">
        <button class="bo-send" type="submit">Save cash assumptions</button>
        <span class="bo-cash-form-status" id="bo-wc-status"></span>
      </div>
    </form>`;

    // Values assigned after insertion, never interpolated into value="…".
    fields.forEach(([id]) => { $(`bo-wc-${id}`).value = value(wc[id]); });
    $("bo-wc-note").value = wc.note || "";
    $("bo-cash-form").addEventListener("submit", submitCashForm);
  }

  async function submitCashForm(e) {
    e.preventDefault();
    const status = $("bo-wc-status");
    const payload = { note: $("bo-wc-note").value.trim() };
    ["dso_days", "dio_days", "dpo_days", "opening_cash_eur", "tax_rate_pct",
     "min_cash_eur"].forEach(id => {
      const raw = $(`bo-wc-${id}`).value.trim();
      // An empty field is sent as null — the CFO deliberately clearing it back
      // to "not stated" — while the backend's exclude_unset keeps anything this
      // form does not send.
      payload[id] = raw === "" ? null : Number(raw);
    });
    status.textContent = "Saving…";
    try {
      const resp = await fetch("/api/profile/working-capital", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) throw new Error(String(resp.status));
      await renderCash();
      renderCashForm();
      const form = $("bo-cash-form");
      if (form) form.classList.remove("hidden");
      $("bo-wc-status").textContent = "Saved.";
    } catch {
      status.textContent = "Could not save. The figures on this page are unchanged.";
    }
  }

  // Its own hand-off rather than ask(), which prefixes every question with "use
  // budget_outlook to read it" — the wrong tool for a cash question, and telling
  // the agent to reach for two is how it reaches for neither.
  function askAboutCash() {
    const cash = state.cash;
    if (!cash || !cash.available || !canAsk()) return;
    const trough = cash.trough || {};
    askFromBudget(
      `Cash on the ${cash.year} budget bottoms out at `
      + `${money(trough.closing_cash_eur, currency())} in ${trough.month}. Use `
      + `cash_flow_projection: what is driving that month, does it clear the minimum, `
      + `and what would actually fix it?`);
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

        ${provenanceBlock(row)}

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
    wireProvenance(row);
    $("bo-drawer-close").addEventListener("click", () => closeDrawer());
    const askBtn = $("bo-drawer-ask");
    askBtn.disabled = !canAsk();
    askBtn.addEventListener("click", () => {
      closeDrawer();
      ask(`Talk me through ${row.label} — is ${pct(row.expected_change_pct)} the right `
        + `assumption for ${t.budget_year}, and what should I sanity-check?`
        + (row.driver_id ? ` It tracks driver ${row.driver_id}.` : ""));
    });
  }

  // The block that only a materialised line can show, and the reason this page
  // is worth more than a spreadsheet: what the figure is locked at, the page
  // that figure was read off, and what the market has done since — drift for
  // what already happened, the forward curve for what it says comes next.
  //
  // A line with no driver_id gets nothing rather than an empty shell: "we have
  // not linked this" and "we linked it and found nothing" are different states.
  function provenanceBlock(row) {
    if (!row.driver_id) return "";
    const d = driverMeta(row.driver_id);
    if (!d) {
      return `<div class="bo-block">
        <h3>Where it comes from</h3>
        <p class="bo-assumption is-default">This line names driver
          <code>${escapeHtml(row.driver_id)}</code>, which is not on the watchlist.
          <span class="bo-source">Nothing is being tracked against it</span></p>
      </div>`;
    }

    const unit = d.unit || "";
    const facts = [];
    if (d.locked_value != null) {
      facts.push([`Locked into the budget`,
                  `${Number(d.locked_value).toLocaleString(undefined,
                      { maximumFractionDigits: 2 })} ${unit}`.trim()]);
    }
    if (d.latest_value != null) {
      facts.push([`Latest observation${d.latest_month ? ` (${d.latest_month})` : ""}`,
                  `${Number(d.latest_value).toLocaleString(undefined,
                      { maximumFractionDigits: 2 })} ${unit}`.trim()]);
    }
    if (d.drift_pct != null) facts.push(["Moved since we locked", pct(d.drift_pct)]);
    if (d.forward_12m != null) {
      facts.push([`Forward curve, next ${d.forward_months || 12} months`,
                  `${Number(d.forward_12m).toLocaleString(undefined,
                      { maximumFractionDigits: 2 })} ${unit}`.trim()
                  + (d.forward_vs_lock_pct != null
                       ? ` · ${pct(d.forward_vs_lock_pct)} vs the lock` : "")]);
    }
    facts.push(["Freshness", d.verify_status === "fresh" ? "Verified recently"
                : d.verify_status === "stale" ? "Past its staleness limit"
                : "Never verified"]);

    return `<div class="bo-block">
      <h3>Where it comes from</h3>
      <dl class="bo-facts">${facts.map(([k, v]) =>
        `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("")}</dl>
      <p class="bo-prov-foot" id="bo-prov-foot"></p>
      <a class="bo-linkish" href="#/drivers">Verify or re-lock on Drivers →</a>
    </div>`;
  }

  // The source link is built imperatively with a protocol allowlist: it is a
  // model-supplied URL, and escaping does not stop `javascript:`.
  function wireProvenance(row) {
    const foot = $("bo-prov-foot");
    if (!foot) return;
    const d = row.driver_id ? driverMeta(row.driver_id) : null;
    const url = d && safeHttpUrl(d.locked_source_url || d.latest_source_url || "");
    if (!url) {
      foot.textContent = d ? "No source URL was recorded for this figure." : "";
      return;
    }
    foot.textContent = "Read from ";
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = new URL(url).hostname;
    foot.appendChild(a);
    if (d.last_verified_utc) {
      foot.appendChild(document.createTextNode(
        ` on ${String(d.last_verified_utc).slice(0, 10)}`));
    }
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
  // Asking — a hand-off, not a second chat implementation
  // ============================================================

  function renderAskRail() {
    const note = $("bo-scope-note");
    const host = $("bo-starters");
    const composer = $("bo-composer");
    if (!note || !host) return;

    if (!canAsk()) {
      note.textContent = "Finish setup to ask about this budget — questions run through the "
                       + "main agent, which needs your company profile first. The figures on "
                       + "this page are computed locally and are unaffected.";
      host.innerHTML = "";
      if (composer) composer.classList.add("hidden");
      return;
    }
    if (composer) composer.classList.remove("hidden");
    note.textContent = "Questions open in Ask, where the agent can go and check the drivers, "
                     + "the bill of materials and the market behind these numbers.";

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
      btn.addEventListener("click", () => ask(questions[i]));
    });
  }

  // The single exit from this page into the agent. Prefixed so the answer is
  // about THIS budget rather than the scenario engine's view of it — the agent
  // reads the page through the budget_outlook tool.
  function ask(question) {
    if (!canAsk()) return;
    askFromBudget(`About next year's budget (use budget_outlook to read it): ${question}`);
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
          ${bomAvailable() ? `
          <p class="bo-config-bom">You have cost datasets, so this budget can be built from
             them instead — every ingredient priced through the bill of materials, every cost
             centre off the opex plan, each linked to the driver it is tracked against.
             <button type="button" class="bo-linkish" id="bo-build-from-data">Build it from my
             data</button></p>` : ""}
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
                  <th class="bo-col-driver" title="Track this line against a watchlist driver">Driver</th>
                  <th class="bo-col-note">Assumption</th>
                  <th class="bo-col-del"></th>
                </tr></thead>
                <tbody id="bo-var-rows"></tbody>
              </table>
            </div>
            <p class="bo-legend-note">Naming a driver links a cost line to a tracked market
               price, so it inherits that price's locked value, its source and its place in a
               frozen budget version. Leave it empty for a line nobody tracks.</p>
            <datalist id="bo-driver-list">${driverOptions()}</datalist>
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

  // Options for the driver datalist. Values are driver ids — our own slugs —
  // but the labels are model-supplied names, so they go through escapeAttr.
  function driverOptions() {
    const catalogue = (state.data && state.data.drivers) || {};
    return Object.keys(catalogue).sort().map(id =>
      `<option value="${escapeAttr(id)}">${escapeAttr(catalogue[id].name || id)}</option>`
    ).join("");
  }

  function renderVarRows() {
    const host = $("bo-var-rows");
    host.innerHTML = state.configVars.map((v, i) => `
      <tr data-i="${i}">
        <td class="bo-col-inc"><input type="checkbox" class="bo-v-inc"></td>
        <td><input type="text" class="bo-v-label" maxlength="80"></td>
        <td class="bo-col-amount"><input type="number" class="bo-v-amount" min="0" step="1000"></td>
        <td class="bo-col-pct"><input type="number" class="bo-v-pct" min="-100" max="100" step="0.1"></td>
        <td class="bo-col-driver"><input type="text" class="bo-v-driver" list="bo-driver-list"
              maxlength="49" placeholder="none"></td>
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
      const driver = tr.querySelector(".bo-v-driver");
      const note = tr.querySelector(".bo-v-note");

      inc.checked = v.include !== false;
      label.value = v.label || "";
      amount.value = String(v.current_amount ?? 0);
      pctEl.value = String(v.expected_change_pct ?? 0);
      driver.value = v.driver_id || "";
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
      driver.addEventListener("input", () => {
        v.driver_id = driver.value.trim().toLowerCase() || null;
      });
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
    const fromData = $("bo-build-from-data");
    if (fromData) fromData.addEventListener("click", async () => {
      fromData.disabled = true;
      fromData.textContent = "Building…";
      try {
        const resp = await fetch("/api/budgetplan/rebuild", { method: "POST" });
        if (!resp.ok) {
          const body = await resp.json().catch(() => null);
          throw new Error((body && body.detail && body.detail.error)
                          || `Could not build (${resp.status}).`);
        }
        await load();
        state.showAll = false;
        // Same reasoning as submitConfig: writing the hash fires hashchange →
        // route() → renderOverview(), so rendering here too would double it.
        const wasAlreadyThere = location.hash === "#/budget";
        location.hash = "#/budget";
        if (wasAlreadyThere) renderOverview();
      } catch (ex) {
        fromData.disabled = false;
        fromData.textContent = "Build it from my data";
        showConfigError(ex.message);
      }
    });
    $("bo-load-defaults").addEventListener("click", () => loadDefaults());
    $("bo-c-industry").addEventListener("input", reloadDefaultsIfUntouched);
    $("bo-c-rev").addEventListener("input", reloadDefaultsIfUntouched);

    $("bo-add-var").addEventListener("click", () => {
      const n = state.configVars.length + 1;
      state.configVars.push({
        id: `custom_${n}_${Math.random().toString(36).slice(2, 7)}`,
        label: "", category: "other", description: "",
        current_amount: 0, expected_change_pct: 0,
        assumption: "", default_note: "", driver_id: null, include: true,
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

  // isConfigured is synchronous on purpose: app.js's route() calls it to decide
  // nav chrome before dispatching, and boot() awaits BudgetPlan.load() before the
  // first route(), so state.data is always populated by then.
  window.BudgetPlan = { route, load, isConfigured: () => !!plan().configured };
})();

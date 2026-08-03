// app.js — Client logic for the CFO Finance Agent demo.
// Hash-routed SPA: a home screen (#/) with feature cards + a quick-ask
// composer, a shared feature view for chat (#/chat, #/chat/{id}), weekly
// (#/weekly) and monthly (#/monthly) reports, and a data view (#/data)
// listing the curated datasets. Conversations and reports are persisted
// server-side as "sessions"; this file lists them in the sidebar, loads them
// on demand, and streams new turns from /api/sessions/{id}/chat (SSE). The
// settings panel handles model selection, the scheduled-refresh controls,
// and the raw-debug view.

const homeView = document.getElementById("home-view");
const featureView = document.getElementById("feature-view");
const dataView = document.getElementById("data-view");
const schedulerView = document.getElementById("scheduler-view");
const alertsView = document.getElementById("alerts-view");
const bootView = document.getElementById("boot-view");
const setupView = document.getElementById("setup-view");
const driversView = document.getElementById("drivers-view");
const scenariosView = document.getElementById("scenarios-view");
const schedulerLink = document.getElementById("scheduler-link");
const alertsBtn = document.getElementById("alerts-btn");
const alertsBadge = document.getElementById("alerts-badge");
const taskListEl = document.getElementById("task-list");
const taskForm = document.getElementById("task-form");
const alertsListEl = document.getElementById("alerts-list");
const homeComposer = document.getElementById("home-composer");
const homeInput = document.getElementById("home-input");
const datasetsListEl = document.getElementById("datasets-list");
const dataRefreshStatus = document.getElementById("data-refresh-status");
const dataRefreshNow = document.getElementById("data-refresh-now");
const dataLink = document.getElementById("data-link");
const messagesEl = document.getElementById("messages");
const composer = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const settingsPanel = document.getElementById("settings-panel");
const schedulerBadge = document.getElementById("scheduler-badge");
const sidebarTitleEl = document.getElementById("sidebar-title");
const sessionListEl = document.getElementById("session-list");
const newSessionBtn = document.getElementById("new-session-btn");
const featureHeading = document.getElementById("feature-heading");
const featureSub = document.getElementById("feature-sub");
const topicSwitcher = document.getElementById("topic-switcher");
const attachBtn = document.getElementById("attach-btn");
const fileInput = document.getElementById("file-input");
const attachmentsEl = document.getElementById("attachments");

let settings = { model: null, show_debug: false };
let profile = null;             // company profile; gates every route until confirmed
let driverPollTimer = null;     // 2s poll while any driver is re-verifying
let pendingAttachments = [];   // files staged for the next message: {name, kind, media_type, data}
let pendingHomeMessage = null; // question typed on the home composer, sent once the chat view is up
let pendingAlertId = null;     // alert being investigated — linked to the chat session once created
let lastAlertTs = null;        // newest alert seen by the poll; null until the first poll seeds it

// Current view state.
const view = {
  kind: "chat",       // "chat" | "weekly" | "monthly"
  mode: "chat",       // "chat" | "report" (viewing a report) | "child" (a thread under a report)
  sessionId: null,    // active session, or null for an unsaved new chat
  parentId: null,     // the report id, when mode === "child"
  parent: null,       // the loaded parent report (for the context banner)
  streaming: false,
};

// noun / periodNoun exist because the strings "Weekly"/"Monthly" and
// "week"/"month" were hardcoded in five places in the reference. Every one of
// them now reads from here, or new labels leak "Weekly report" into a
// Market-scan banner.
const KIND_META = {
  chat:    { heading: "Ask", noun: "Chat", periodNoun: "",
             sub: "Every figure carries its source — a dataset file, or a page with the date it was read.",
             sidebar: "Conversations", newLabel: "+ New question",
             placeholder: "What does soymeal at €412/t do to next year's gross margin?" },
  weekly:  { heading: "Market scan", noun: "Market scan", periodNoun: "week",
             sub: "What moved this week in the markets your budget depends on.",
             sidebar: "Market scans", newLabel: "+ New market scan",
             placeholder: "Ask a follow-up about this scan…" },
  monthly: { heading: "Budget revision", noun: "Budget revision", periodNoun: "month",
             sub: "This month's re-forecast: what changed, what it's worth, and what to do about it.",
             sidebar: "Budget revisions", newLabel: "+ New budget revision",
             placeholder: "Ask a follow-up about this revision…" },
};

// ---------- Minimal markdown rendering (bold, code, tables, lists) ----------

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  const out = [];
  let list = null, table = null;

  const flush = () => {
    if (list) { out.push(`<${list.tag}>` + list.items.map(i => `<li>${i}</li>`).join("") + `</${list.tag}>`); list = null; }
    if (table) {
      const [head, ...body] = table;
      out.push("<table><tr>" + head.map(c => `<th>${c}</th>`).join("") + "</tr>" +
        body.map(r => "<tr>" + r.map(c => `<td>${c}</td>`).join("") + "</tr>").join("") + "</table>");
      table = null;
    }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^\|.*\|$/.test(line)) {
      const cells = line.slice(1, -1).split("|").map(c => inline(c.trim()));
      if (cells.every(c => /^:?-{2,}:?$/.test(c))) continue; // separator row
      (table = table || []).push(cells);
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)/);
    if (heading) {
      flush();
      const level = Math.min(heading[1].length + 1, 6); // shift down: the page owns h1
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flush(); out.push("<hr>"); continue; }
    const bullet = line.match(/^[-*•]\s+(.*)/);
    const numbered = line.match(/^\d+[.)]\s+(.*)/);
    if (bullet || numbered) {
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tag !== tag) { flush(); list = { tag, items: [] }; }
      list.items.push(inline((bullet || numbered)[1]));
      continue;
    }
    flush();
    if (line) out.push(`<p>${inline(line)}</p>`);
  }
  flush();
  return out.join("");

  function inline(s) {
    return s
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }
}

// ---------- Message rendering (all helpers take a target container) ----------

function addUserMessage(container, text, attachments) {
  const div = document.createElement("div");
  div.className = "msg user";
  div.innerHTML = `<div class="bubble"></div>`;
  const bubble = div.querySelector(".bubble");
  if (attachments && attachments.length) {
    const wrap = document.createElement("div");
    wrap.className = "msg-attachments";
    wrap.innerHTML = attachments.map(a =>
      `<span class="attach-chip static">${escapeHtml(a.name)}</span>`).join("");
    bubble.appendChild(wrap);
  }
  if (text) {
    const t = document.createElement("div");
    t.className = "msg-text";
    t.textContent = text;
    bubble.appendChild(t);
  }
  container.appendChild(div);
  scrollDown(container);
}

function addAssistantMessage(container, initialText) {
  const div = document.createElement("div");
  div.className = "msg assistant";
  div.innerHTML = `<div class="bubble"><div class="content thinking">Thinking…</div></div>`;
  container.appendChild(div);
  const bubble = div.querySelector(".bubble");
  if (initialText) {
    const content = bubble.querySelector(".content");
    content.classList.remove("thinking");
    content.innerHTML = renderMarkdown(initialText);
  }
  scrollDown(container);
  return bubble;
}

function scrollDown(container) {
  container.scrollTop = container.scrollHeight;
}

function addDebugBlock(bubble, title, payload) {
  if (!settings.show_debug) return;
  const details = document.createElement("details");
  details.className = "debug-block";
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(payload, null, 2);
  const summary = document.createElement("summary");
  summary.textContent = title;
  details.append(summary, pre);
  bubble.appendChild(details);
}

// ---------- Sources ----------
//
// Normalising at the top of addSources IS the whole backward-compatibility
// story: an older session persisted plain strings, a newer one persists
// records, and both render identically.
function normaliseSource(s) {
  if (typeof s === "string") return { kind: "dataset", label: s, id: s };
  return s || {};
}

// SECURITY: escapeHtml escapes & < > but NOT quotes, so interpolating a
// model-supplied URL into href="${escapeHtml(url)}" is attribute injection —
// and a javascript: URL survives it intact. Every web citation link is
// therefore built imperatively with a protocol allowlist. Never innerHTML a
// model-supplied URL anywhere in this codebase.
function safeHttpUrl(raw) {
  try {
    const u = new URL(raw);
    if (!/^https?:$/.test(u.protocol)) return null;
    return u.href;
  } catch { return null; }
}

function truncate(text, n) {
  const t = String(text || "");
  return t.length > n ? t.slice(0, n - 1).trimEnd() + "…" : t;
}

function sourceChip(rec) {
  const href = rec.kind === "web" ? safeHttpUrl(rec.url) : null;
  const label = truncate(rec.title || rec.label || rec.url || rec.id || "source", 34);

  if (!href) {
    // Dataset chips keep .source-chip exactly as-is — and a web record whose
    // URL failed the allowlist degrades to an inert chip rather than a link.
    const span = document.createElement("span");
    span.className = "source-chip";
    span.textContent = label;
    if (rec.kind === "web") span.title = String(rec.url || "");
    return span;
  }
  const a = document.createElement("a");
  a.href = href;                       // assigned, never interpolated
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.className = "source-chip is-web";  // outline variant: weight and shape, not hue
  a.textContent = label;
  a.title = rec.accessed ? `${rec.url}\n\nRetrieved ${rec.accessed}` : String(rec.url);
  const glyph = document.createElement("span");
  glyph.className = "chip-glyph";
  glyph.textContent = "↗";
  a.appendChild(glyph);
  return a;
}

const MAX_DATASET_CHIPS = 3;

function addSources(bubble, sources) {
  if (!sources || !sources.length) return;
  bubble.querySelectorAll(".sources").forEach(el => el.remove());
  const records = sources.map(normaliseSource);

  const wrap = document.createElement("div");
  wrap.className = "sources";
  const label = document.createElement("span");
  label.className = "sources-label";
  label.textContent = "Sources";
  wrap.appendChild(label);

  const web = records.filter(r => r.kind === "web");
  const datasets = records.filter(r => r.kind !== "web");

  web.forEach(r => wrap.appendChild(sourceChip(r)));
  // More than three dataset chips collapse behind a count: the local files are
  // the boring part of a market scan's provenance.
  datasets.slice(0, MAX_DATASET_CHIPS).forEach(r => wrap.appendChild(sourceChip(r)));
  if (datasets.length > MAX_DATASET_CHIPS) {
    const more = document.createElement("span");
    more.className = "source-chip is-count";
    more.textContent = `+${datasets.length - MAX_DATASET_CHIPS} datasets`;
    more.title = datasets.slice(MAX_DATASET_CHIPS)
      .map(r => r.label || r.id).join("\n");
    wrap.appendChild(more);
  }
  bubble.appendChild(wrap);
}

// Rewrite literal "[3]" markers to superscript links, in ONE pass over TEXT
// NODES. A regex over innerHTML would corrupt attributes; walking text nodes
// cannot. Run only from the final clean render — on a reveal tick a .fade-new
// span boundary can split a marker in half.
function linkCitationMarkers(root, citations) {
  const byIndex = new Map();
  const seen = [];
  citations.forEach(c => {
    if (!seen.some(s => s.id === c.id)) seen.push(c);
  });
  seen.forEach((c, i) => byIndex.set(String(i + 1), c));
  if (!byIndex.size) return;

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const targets = [];
  while (walker.nextNode()) {
    if (/\[\d+\]/.test(walker.currentNode.nodeValue)) targets.push(walker.currentNode);
  }
  targets.forEach(node => {
    if (node.parentElement && node.parentElement.closest("a, code, pre")) return;
    const frag = document.createDocumentFragment();
    let last = 0;
    const text = node.nodeValue;
    text.replace(/\[(\d+)\]/g, (match, num, offset) => {
      const cite = byIndex.get(num);
      if (!cite) return match;
      if (offset > last) frag.appendChild(document.createTextNode(text.slice(last, offset)));
      const href = safeHttpUrl(cite.url);
      const sup = document.createElement("sup");
      sup.className = "cite-marker";
      if (href) {
        const a = document.createElement("a");
        a.href = href;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = num;
        a.title = cite.cited_text ? truncate(cite.cited_text, 160) : String(cite.url);
        sup.appendChild(a);
      } else {
        sup.textContent = num;
      }
      frag.appendChild(sup);
      last = offset + match.length;
      return match;
    });
    if (!frag.childNodes.length) return;
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  });
}

// ---------- Research status ----------
//
// Fills the otherwise silent 10-30s gap while a server-side web tool runs.
function showResearchStatus(bubble, ev) {
  let el = bubble.querySelector(".research-status");
  if (!el) {
    el = document.createElement("div");
    el.className = "research-status";
    const content = bubble.querySelector(".content");
    bubble.insertBefore(el, content);
  }
  const what = ev.query || ev.url || "";
  const verb = ev.tool === "web_fetch" ? "Reading" : "Searching";
  el.textContent = what ? `${verb}: ${truncate(what, 70)}` : `${verb}…`;
}

function clearResearchStatus(bubble) {
  const el = bubble.querySelector(".research-status");
  if (el) el.remove();
}

function addNotice(bubble, message) {
  if (!message) return;
  const el = document.createElement("p");
  el.className = "notice-text";
  el.textContent = message;
  bubble.appendChild(el);
}

// ---------- Reasoning disclosure ----------
//
// Rendered PLAINLY with textContent += and white-space: pre-wrap — never
// through the reveal loop, which re-renders whole content via innerHTML every
// 130ms. A second concurrent instance would double that on the same frame
// budget, and summarized reasoning is frequently longer than the answer. It is
// for glancing at, not close reading — and textContent is immune to
// background-tab rAF suspension by construction.
function ensureReasoning(bubble) {
  let box = bubble.querySelector("details.reasoning");
  if (box) return box;
  box = document.createElement("details");
  box.className = "reasoning is-live";
  box.open = true;
  const summary = document.createElement("summary");
  summary.innerHTML = '<span class="reasoning-label">Reasoning</span>' +
                      '<span class="reasoning-meta"></span>' +
                      '<span class="reasoning-spinner"></span>';
  const body = document.createElement("div");
  body.className = "reasoning-body";
  box.appendChild(summary);
  box.appendChild(body);
  box.dataset.steps = "1";
  box.dataset.startedAt = String(Date.now());
  // Never fight a user who has toggled it themselves — the same principle as
  // never fighting a user who has scrolled up.
  box.addEventListener("toggle", () => { box.dataset.userToggled = "1"; });
  bubble.insertBefore(box, bubble.querySelector(".content"));
  return box;
}

function appendReasoning(bubble, text) {
  if (!text) return;
  const box = ensureReasoning(bubble);
  const body = box.querySelector(".reasoning-body");
  // Reasoning resumes after each tool round. Append to the SAME disclosure with
  // a separator and bump the step count — one box per round would give a
  // five-tool turn five collapsed boxes.
  if (box.dataset.resumed === "1") {
    body.textContent += "\n\n";
    box.dataset.steps = String(Number(box.dataset.steps || 1) + 1);
    box.dataset.resumed = "0";
  }
  body.textContent += text;
  body.scrollTop = body.scrollHeight;
  updateReasoningMeta(box);
}

function updateReasoningMeta(box) {
  const secs = Math.max(1, Math.round((Date.now() - Number(box.dataset.startedAt)) / 1000));
  const steps = Number(box.dataset.steps || 1);
  box.querySelector(".reasoning-meta").textContent =
    ` · ${secs}s · ${steps} step${steps === 1 ? "" : "s"}`;
}

function collapseReasoning(bubble) {
  const box = bubble.querySelector("details.reasoning");
  if (!box) return;
  box.dataset.resumed = "1";      // next reasoning delta starts a new step
  if (box.dataset.userToggled !== "1") box.open = false;
}

function stopReasoningSpinner(bubble) {
  const box = bubble.querySelector("details.reasoning");
  if (!box) return;
  // Without this a stream that dies mid-reasoning leaves a spinner turning
  // forever.
  box.classList.remove("is-live");
  updateReasoningMeta(box);
}

// ---------- Inline charts (the render_chart tool) ----------
// Server-validated spec: {chart_type: "line"|"bar", title, unit?, series:
// [{name, points: [{label, value}]}]}. Rendered as a plain SVG on the white
// bubble surface. Colors are brand-derived categorical steps in FIXED order
// (validated for lightness/chroma/CVD/contrast with the dataviz palette
// checker — don't reorder or swap casually).

const CHART_COLORS = ["#4144C4", "#B0568F", "#E56A00", "#0F9AA0"];

function fmtChartValue(v, unit) {
  if (unit === "%" ) return `${Math.round(v * 10) / 10}%`;
  if (unit === "pp") return `${Math.round(v * 10) / 10} pp`;
  const abs = Math.abs(v);
  let s;
  if (abs >= 1e9) s = `${(v / 1e9).toFixed(1)}B`;
  else if (abs >= 1e6) s = `${(v / 1e6).toFixed(1)}M`;
  else if (abs >= 1e3) s = `${Math.round(v / 1e3)}k`;
  else s = String(Math.round(v * 10) / 10);
  return unit === "EUR" ? `€${s}` : s;
}

// A handful of round-numbered ticks spanning [min, max].
function chartTicks(min, max) {
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const step = Math.pow(10, Math.floor(Math.log10(span / 4)));
  const scaled = span / 4 / step;
  const nice = step * (scaled >= 5 ? 10 : scaled >= 2 ? 5 : scaled >= 1 ? 2 : 1);
  const ticks = [];
  for (let t = Math.ceil(min / nice) * nice; t <= max + 1e-9; t += nice) ticks.push(t);
  return ticks;
}

function addChart(bubble, spec) {
  if (!spec || !Array.isArray(spec.series) || !spec.series.length) return;
  const card = document.createElement("figure");
  card.className = "chart-card";

  // Shared x domain: labels in order of first appearance across series.
  const labels = [];
  for (const s of spec.series) for (const p of s.points || []) {
    if (!labels.includes(p.label)) labels.push(p.label);
  }
  const byLabel = spec.series.map(s => {
    const m = new Map();
    for (const p of s.points || []) m.set(p.label, p.value);
    return m;
  });
  const values = spec.series.flatMap(s => (s.points || []).map(p => p.value));
  let vMin = Math.min(...values), vMax = Math.max(...values);
  if (spec.chart_type === "bar") { vMin = Math.min(vMin, 0); vMax = Math.max(vMax, 0); }
  const ticks = chartTicks(vMin, vMax);
  vMin = Math.min(vMin, ticks[0]);
  vMax = Math.max(vMax, ticks[ticks.length - 1]);

  // Geometry (viewBox space; the SVG scales to the bubble width).
  const W = 560, H = 240, padL = 52, padR = 10, padT = 10, padB = 24;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const y = v => padT + plotH - ((v - vMin) / (vMax - vMin)) * plotH;
  const xCenter = i => padL + (labels.length === 1 ? plotW / 2 : (i + 0.5) * (plotW / labels.length));

  const INK = "#3D3D52", MUTED = "#76768A", GRID = "rgba(1,1,144,0.08)";
  let svg = "";

  for (const t of ticks) {                            // recessive grid + y labels
    svg += `<line x1="${padL}" x2="${W - padR}" y1="${y(t)}" y2="${y(t)}" stroke="${GRID}" stroke-width="1"/>` +
           `<text x="${padL - 6}" y="${y(t) + 3.5}" text-anchor="end" font-size="10" fill="${MUTED}">${escapeHtml(fmtChartValue(t, spec.unit))}</text>`;
  }
  const labelEvery = Math.ceil(labels.length / 8);    // at most ~8 x labels
  labels.forEach((lab, i) => {
    if (i % labelEvery) return;
    const short = /^\d{4}-\d{2}$/.test(lab) && labels.length > 8 ? lab.slice(2) : lab;
    svg += `<text x="${xCenter(i)}" y="${H - 8}" text-anchor="middle" font-size="10" fill="${MUTED}">${escapeHtml(short.length > 14 ? short.slice(0, 13) + "…" : short)}</text>`;
  });

  if (spec.chart_type === "bar") {
    const y0 = y(Math.max(vMin, Math.min(0, vMax)));
    const slot = plotW / labels.length;
    const groupW = Math.min(slot * 0.7, 26 * spec.series.length);
    const barW = groupW / spec.series.length - 2;     // 2px surface gap between bars
    labels.forEach((lab, i) => {
      spec.series.forEach((s, si) => {
        const v = byLabel[si].get(lab);
        if (v === undefined) return;
        const bx = xCenter(i) - groupW / 2 + si * (barW + 2);
        const top = Math.min(y(v), y0), h = Math.max(Math.abs(y(v) - y0), 1);
        const r = Math.min(4, barW / 2, h);           // rounded data end, square baseline end
        const up = v >= 0;
        svg += `<path d="M${bx},${up ? top + h : top} v${up ? -(h - r) : h - r} q0,${up ? -r : r} ${r},${up ? -r : r} h${barW - 2 * r} q${r},0 ${r},${up ? r : -r} v${up ? h - r : -(h - r)} z" fill="${CHART_COLORS[si % 4]}"/>`;
      });
    });
    svg += `<line x1="${padL}" x2="${W - padR}" y1="${y0}" y2="${y0}" stroke="${INK}" stroke-width="1"/>`;
  } else {
    spec.series.forEach((s, si) => {
      const pts = labels.map((lab, i) => byLabel[si].has(lab) ? `${xCenter(i)},${y(byLabel[si].get(lab))}` : null)
        .filter(Boolean);
      if (pts.length > 1) {
        svg += `<polyline points="${pts.join(" ")}" fill="none" stroke="${CHART_COLORS[si % 4]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
      } else if (pts.length === 1) {
        const [px, py] = pts[0].split(",");
        svg += `<circle cx="${px}" cy="${py}" r="4" fill="${CHART_COLORS[si % 4]}"/>`;
      }
    });
  }

  const legend = spec.series.length > 1
    ? `<div class="chart-legend">` + spec.series.map((s, si) =>
        `<span class="chart-legend-item"><span class="chart-swatch" style="background:${CHART_COLORS[si % 4]}"></span>${escapeHtml(s.name)}</span>`).join("") + `</div>`
    : "";
  const tableRows = labels.map((lab, i) =>
    `<tr><td>${escapeHtml(lab)}</td>` + spec.series.map((s, si) => {
      const v = byLabel[si].get(lab);
      return `<td>${v === undefined ? "—" : escapeHtml(fmtChartValue(v, spec.unit))}</td>`;
    }).join("") + `</tr>`).join("");

  card.innerHTML = `
    <figcaption class="chart-title">${escapeHtml(spec.title)}</figcaption>
    ${legend}
    <div class="chart-plot">
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${escapeHtml(spec.title)}">${svg}
        <line class="chart-crosshair" y1="${padT}" y2="${padT + plotH}" stroke="${MUTED}" stroke-width="1" stroke-dasharray="3 3" visibility="hidden"/>
      </svg>
      <div class="chart-tooltip hidden"></div>
    </div>
    <details class="chart-data"><summary>View data</summary>
      <div class="data-table-wrap"><table class="data-table">
        <tr><th></th>${spec.series.map(s => `<th>${escapeHtml(s.name)}</th>`).join("")}</tr>${tableRows}
      </table></div>
    </details>`;

  // Hover layer: nearest-label crosshair + a tooltip listing every series.
  const plot = card.querySelector(".chart-plot");
  const svgEl = plot.querySelector("svg");
  const crosshair = plot.querySelector(".chart-crosshair");
  const tooltip = plot.querySelector(".chart-tooltip");
  svgEl.addEventListener("mousemove", (e) => {
    const rect = svgEl.getBoundingClientRect();
    const vx = (e.clientX - rect.left) * (W / rect.width);
    const i = Math.max(0, Math.min(labels.length - 1,
      Math.floor((vx - padL) / (plotW / labels.length))));
    crosshair.setAttribute("x1", xCenter(i));
    crosshair.setAttribute("x2", xCenter(i));
    crosshair.setAttribute("visibility", "visible");
    tooltip.innerHTML = `<strong>${escapeHtml(labels[i])}</strong>` + spec.series.map((s, si) => {
      const v = byLabel[si].get(labels[i]);
      return v === undefined ? "" :
        `<span><span class="chart-swatch" style="background:${CHART_COLORS[si % 4]}"></span>${escapeHtml(s.name)}: ${escapeHtml(fmtChartValue(v, spec.unit))}</span>`;
    }).join("");
    tooltip.classList.remove("hidden");
    const px = (xCenter(i) / W) * rect.width;
    tooltip.style.left = `${Math.min(Math.max(px, 70), rect.width - 70)}px`;
  });
  svgEl.addEventListener("mouseleave", () => {
    crosshair.setAttribute("visibility", "hidden");
    tooltip.classList.add("hidden");
  });

  bubble.appendChild(card);
}

// ---------- Session API ----------

async function apiListSessions(kind) {
  const resp = await fetch(`/api/sessions?kind=${encodeURIComponent(kind)}`);
  if (!resp.ok) throw new Error(`Could not list sessions (${resp.status})`);
  return resp.json();
}

async function apiGetSession(id) {
  const resp = await fetch(`/api/sessions/${id}`);
  if (!resp.ok) throw new Error(`Could not load session (${resp.status})`);
  return resp.json();
}

async function apiCreateSession(kind, parentId) {
  const body = { kind };
  if (parentId) body.parent_id = parentId;
  const resp = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`Could not create session (${resp.status})`);
  return resp.json();
}

async function apiDeleteSession(id) {
  await fetch(`/api/sessions/${id}`, { method: "DELETE" });
}

async function apiGenerateReport(kind) {
  const resp = await fetch(`/api/sessions/report/${kind}/generate`, { method: "POST" });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || `Generation failed (${resp.status})`);
  return data;
}

// ---------- Sidebar ----------

function fmtWhen(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
                 : d.toLocaleDateString();
}

async function refreshSidebar() {
  let data;
  try {
    data = await apiListSessions(view.kind);
  } catch {
    sessionListEl.innerHTML = `<li class="empty">Could not load.</li>`;
    return;
  }
  sessionListEl.innerHTML = "";
  if (view.kind === "chat") { renderChatSidebar(data.sessions || []); return; }
  renderReportSidebar(data);
}

// A flat list of past conversations (Chat).
function renderChatSidebar(items) {
  if (!items.length) {
    sessionListEl.innerHTML = `<li class="empty">No conversations yet.</li>`;
    return;
  }
  for (const s of items) sessionListEl.appendChild(sessionRow(s, s.title || "Untitled", "conversation"));
}

// Folder structure for reports: each report, with its chat threads nested beneath.
function renderReportSidebar(data) {
  const items = data.sessions || [];
  const reports = items.filter(s => !s.parent_id);
  const childrenBy = {};
  for (const s of items) if (s.parent_id) (childrenBy[s.parent_id] ||= []).push(s);

  if (data.generating) {
    const li = document.createElement("li");
    li.className = "empty";
    li.innerHTML = `<span class="mini-spinner"></span> Generating this ${view.kind === "weekly" ? "week" : "month"}'s report…`;
    sessionListEl.appendChild(li);
  }
  if (!reports.length && !data.generating) {
    sessionListEl.appendChild(Object.assign(document.createElement("li"),
      { className: "empty", textContent: "No reports yet." }));
    return;
  }

  for (const report of reports) {
    const folder = document.createElement("li");
    folder.className = "report-folder";
    // Report row
    folder.appendChild(sessionRow(report, report.period || report.title || "Report", "report",
      { isReport: true }));
    // Nested chat threads + "New chat"
    const sub = document.createElement("ul");
    sub.className = "thread-list";
    const threads = (childrenBy[report.id] || []).sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
    for (const t of threads) sub.appendChild(sessionRow(t, t.title || "New chat", "chat"));
    const add = document.createElement("li");
    add.className = "thread-add";
    add.innerHTML = `<button class="thread-add-btn">+ New chat</button>`;
    add.querySelector("button").addEventListener("click", () => startThread(report.id));
    sub.appendChild(add);
    folder.appendChild(sub);
    sessionListEl.appendChild(folder);
  }
}

// One clickable/deletable row (a report, a report thread, or a chat).
function sessionRow(s, label, noun, opts = {}) {
  const li = document.createElement("li");
  li.className = "session-item" + (s.id === view.sessionId ? " active" : "") + (opts.isReport ? " is-report" : "");
  li.innerHTML = `
    <button class="session-open">
      <span class="session-label">${escapeHtml(label)}</span>
      <span class="session-when">${fmtWhen(s.updated_at)}</span>
    </button>
    <button class="session-del" title="Delete" aria-label="Delete">×</button>`;
  li.querySelector(".session-open").addEventListener("click", () => {
    location.hash = `#/${view.kind}/${s.id}`;
  });
  li.querySelector(".session-del").addEventListener("click", async (e) => {
    e.stopPropagation();
    const msg = opts.isReport
      ? "Delete this report and all its chats?"
      : "Delete this " + noun + "?";
    if (!confirm(msg)) return;
    await apiDeleteSession(s.id);
    if (s.id === view.sessionId || s.id === view.parentId) location.hash = `#/${view.kind}`;
    else refreshSidebar();
  });
  return li;
}

// Create a fresh chat thread under a report and open it.
async function startThread(reportId) {
  try {
    const child = await apiCreateSession(view.kind, reportId);
    location.hash = `#/${view.kind}/${child.id}`;
  } catch (err) {
    alert(err.message);
  }
}

// ---------- Rendering stored messages ----------

// Render a message thread into #messages. `hideFirstUser` drops the preset
// report-generation prompt when showing a report overview.
function renderThread(msgs, sources, { hideFirstUser = false } = {}) {
  msgs.forEach((m, i) => {
    if (m.role === "user") {
      if (hideFirstUser && i === 0) return;
      // content may be a plain string or a list of blocks (when files were attached);
      // the typed text is stored separately in m.text for display.
      const text = m.text != null ? m.text
        : (typeof m.content === "string" ? m.content
           : ((m.content.find(b => b.type === "text") || {}).text || ""));
      addUserMessage(messagesEl, text, m.attachments || []);
    } else {
      const bubble = addAssistantMessage(messagesEl, m.content);
      (m.charts || []).forEach(spec => addChart(bubble, spec));
      if (m.reasoning) restoreReasoning(bubble, m.reasoning);
      // Citations belong to the TURN that made them. The reference pins the
      // whole session's sources to whichever assistant message happens to be
      // last, which is fine for four dataset files and wrong for a market scan
      // citing a dozen URLs across three turns.
      if (m.sources && m.sources.length) addSources(bubble, m.sources);
      else if (i === msgs.length - 1 && !msgs.some(x => x.sources)) addSources(bubble, sources);
    }
  });
  scrollDown(messagesEl);
}

// Standalone chat conversation.
function renderChatView(session) {
  messagesEl.innerHTML = "";
  const msgs = (session && session.messages) || [];
  if (!msgs.length) {
    messagesEl.innerHTML = `<div class="msg assistant"><div class="bubble"><p class="thinking">New conversation — ask a question to begin.</p></div></div>`;
    return;
  }
  renderThread(msgs, session.sources);
}

// A report overview (the generated report, hiding its preset prompt).
function renderReportView(report) {
  messagesEl.innerHTML = "";
  renderThread(report.messages || [], report.sources, { hideFirstUser: true });
}

// A chat thread under a report: a compact context banner + the thread's own turns.
function renderChildView(child, parent) {
  messagesEl.innerHTML = "";
  const banner = document.createElement("div");
  banner.className = "report-banner";
  const label = parent ? (parent.period || parent.title || "report") : "report";
  banner.innerHTML = `<span class="report-banner-label">In context: ${view.kind === "weekly" ? "Weekly" : "Monthly"} report · ${escapeHtml(label)}</span>` +
    (parent ? `<a class="report-banner-link" href="#/${view.kind}/${parent.id}">View report ↗</a>` : "");
  messagesEl.appendChild(banner);

  const msgs = child.messages || [];
  if (!msgs.length) {
    const hint = document.createElement("div");
    hint.className = "msg assistant";
    hint.innerHTML = `<div class="bubble"><p class="thinking">New chat about this report — ask a question to begin.</p></div>`;
    messagesEl.appendChild(hint);
    return;
  }
  renderThread(msgs, child.sources);
}

// ---------- Chat flow (send a turn to the active session) ----------

// Work out which session this message goes to, creating one if needed:
//  - report view  → start a NEW chat thread under the report, switch to it
//  - empty chat   → create the chat lazily
//  - child/chat   → the already-active session
async function resolveTargetSession() {
  if (view.mode === "report") {
    const reportId = view.sessionId;
    const child = await apiCreateSession(view.kind, reportId);
    view.mode = "child";
    view.parentId = reportId;
    view.sessionId = child.id;
    // view.parent (the report) is already loaded from renderFeature.
    history.replaceState(null, "", `#/${view.kind}/${child.id}`);
    input.placeholder = KIND_META[view.kind].placeholder;
    renderChildView({ messages: [] }, view.parent);   // banner + empty
    messagesEl.querySelector(".thinking")?.closest(".msg")?.remove();
    return child.id;
  }
  if (view.mode === "chat" && !view.sessionId) {
    const s = await apiCreateSession(view.kind);
    view.sessionId = s.id;
    history.replaceState(null, "", `#/chat/${s.id}`);
    if (messagesEl.querySelector(".thinking")) messagesEl.innerHTML = "";
    return s.id;
  }
  // child thread with no turns yet — clear its "ask a question" hint
  if (view.mode === "child") messagesEl.querySelector(".thinking")?.closest(".msg")?.remove();
  return view.sessionId;
}

// ---------- The stream renderer ----------
//
// Extracted verbatim out of sendMessage's closure. It was trapped there —
// assistantText, contentEl, pendingSources and revealRaf were locals, and the
// scroll math hardcoded messagesEl even though the message helpers were
// carefully parameterised on `container`. The setup wizard and the reasoning
// disclosure both need this same loop on a different container.
//
// The reveal cadence, the max(14, backlog/5) batch size, the .fade-new span
// boundary, the 120px near-bottom rule and the document.hidden flush path are
// byte-for-byte unchanged from the reference. A subtle regression in any of
// them would surface later as a *citations* or *reasoning* bug and get
// debugged in the wrong file.
function createStreamRenderer(container, bubble, opts = {}) {
  const contentEl = bubble.querySelector(".content");
  let assistantText = "";
  let shownLen = 0;
  let lastTickAt = 0;
  let revealRaf = null;
  let streamEnded = false;
  let pendingSources = null;
  let pendingCitations = [];

  const TICK_MS = 130;   // .fade-new's CSS duration is matched to this

  function renderShown(fadeFrom) {
    const shown = assistantText.slice(0, shownLen);
    if (fadeFrom < shown.length) {
      const lineStart = shown.lastIndexOf("\n") + 1;
      const line = shown.slice(lineStart);
      if (!line.startsWith("|")) {
        const marker = line.match(/^(#{1,6}\s+|[-*\u2022]\s+|\d+[.)]\s+)/);
        const from = Math.max(fadeFrom, lineStart + (marker ? marker[1].length : 0));
        if (from < shown.length) {
          contentEl.innerHTML = renderMarkdown(shown.slice(0, from) + "\u0001" + shown.slice(from))
            .replace("\u0001", '<span class="fade-new">');
          return;
        }
      }
    }
    contentEl.innerHTML = renderMarkdown(shown);
  }

  // The clean final render. Citation markers are rewritten to superscript links
  // HERE and only here — never on a reveal tick, where a .fade-new span
  // boundary can split a "[3]" marker in half.
  function finalRender() {
    if (!assistantText) return;
    contentEl.innerHTML = renderMarkdown(assistantText);
    if (pendingCitations.length) linkCitationMarkers(contentEl, pendingCitations);
  }

  function revealTick(now) {
    if (shownLen < assistantText.length && now - lastTickAt >= TICK_MS) {
      lastTickAt = now;
      const backlog = assistantText.length - shownLen;
      const prevShown = shownLen;
      shownLen = Math.min(assistantText.length, shownLen + Math.max(14, Math.ceil(backlog / 5)));
      const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 120;
      renderShown(prevShown);
      if (nearBottom) scrollDown(container);
    }
    if (!streamEnded || shownLen < assistantText.length) {
      revealRaf = requestAnimationFrame(revealTick);
    } else {                     // fully revealed and the stream is over
      revealRaf = null;
      finalRender();
      if (pendingSources) { addSources(bubble, pendingSources); scrollDown(container); }
      pendingSources = null;
      if (opts.onRevealDone) opts.onRevealDone();
    }
  }

  function flushReveal() {       // show everything at once (used on errors)
    if (revealRaf) { cancelAnimationFrame(revealRaf); revealRaf = null; }
    shownLen = assistantText.length;
    finalRender();
    stopReasoningSpinner(bubble);
  }

  function handleEvent(ev) {
    if (opts.onEvent) opts.onEvent(ev);
    if (ev.type === "text") {
      assistantText += ev.text;
      contentEl.classList.remove("thinking");
      collapseReasoning(bubble);
      // Hidden tab: rAF is suspended, so render directly — nobody is watching
      // and the text must not stall.
      if (document.hidden) flushReveal();
      else if (!revealRaf) revealRaf = requestAnimationFrame(revealTick);
    } else if (ev.type === "reasoning") {
      appendReasoning(bubble, ev.text);
    } else if (ev.type === "research") {
      showResearchStatus(bubble, ev);
    } else if (ev.type === "citation") {
      pendingCitations.push(ev);
    } else if (ev.type === "tool_call") {
      addDebugBlock(bubble, `\u2192 tool call: ${ev.name}`, ev.input);
    } else if (ev.type === "tool_result") {
      clearResearchStatus(bubble);
      addDebugBlock(bubble, `\u2190 tool result: ${ev.name}`, ev.result);
    } else if (ev.type === "chart") {
      addChart(bubble, ev.spec);
      scrollDown(container);
    } else if (ev.type === "web_error") {
      // Non-terminal: the model sees the failure and adapts within the turn.
      addNotice(bubble, `Could not reach a source (${ev.tool}: ${ev.error_code}).`);
    } else if (ev.type === "notice") {
      addNotice(bubble, ev.message);
    } else if (ev.type === "sources") {
      pendingSources = ev.records && ev.records.length ? ev.records : ev.sources;
      if (document.hidden) {
        flushReveal();
        addSources(bubble, pendingSources);
        pendingSources = null;
      }
    } else if (ev.type === "error") {
      flushReveal();
      clearResearchStatus(bubble);
      contentEl.classList.remove("thinking");
      contentEl.innerHTML += `<p class="error-text">\u26a0 ${escapeHtml(ev.message)}</p>`;
    }
  }

  function finish() {
    streamEnded = true;
    clearResearchStatus(bubble);
    stopReasoningSpinner(bubble);
    // If the stream ended with nothing left to animate the loop may not be
    // running — kick it once so remaining text/sources still land.
    if (!revealRaf && (shownLen < assistantText.length || pendingSources)) {
      revealRaf = requestAnimationFrame(revealTick);
    } else if (!revealRaf) {
      finalRender();
    }
  }

  function fail(message) {
    flushReveal();
    contentEl.classList.remove("thinking");
    contentEl.innerHTML += `<span class="error-text">\u26a0 ${escapeHtml(message)}</span>`;
  }

  return { handleEvent, finish, fail, getText: () => assistantText };
}

// Read an SSE body, dispatching each event to `onEvent`.
async function readSSE(resp, onEvent) {
  if (!resp.ok || !resp.body) throw new Error(`Server error ${resp.status}`);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      if (chunk.startsWith("data: ")) onEvent(JSON.parse(chunk.slice(6)));
    }
  }
}

async function sendMessage(text, attachments) {
  attachments = attachments || [];
  const targetId = await resolveTargetSession();

  if (pendingAlertId) {
    fetch(`/api/alerts/${pendingAlertId}/read`, { method: "POST" }).catch(() => {});
    pendingAlertId = null;
  }

  addUserMessage(messagesEl, text, attachments.map(a => ({ name: a.name })));
  const bubble = addAssistantMessage(messagesEl);
  const stream = createStreamRenderer(messagesEl, bubble);
  view.streaming = true;
  sendBtn.disabled = true;

  try {
    const resp = await fetch(`/api/sessions/${targetId}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, attachments }),
    });
    await readSSE(resp, stream.handleEvent);
  } catch (err) {
    stream.fail(err.message);
  } finally {
    stream.finish();
    view.streaming = false;
    sendBtn.disabled = false;
    input.focus();
    refreshSidebar();
  }
}

composer.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if ((!text && !pendingAttachments.length) || view.streaming) return;
  input.value = "";
  const attachments = pendingAttachments;
  pendingAttachments = [];
  renderAttachments();
  sendMessage(text, attachments);
});

// Home-screen quick-ask composer: stash the question and navigate to a fresh
// chat; renderFeature() consumes it once the chat view is set up (sending from
// here would race the hashchange → route() re-render).
homeComposer.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = homeInput.value.trim();
  if (!text) return;
  homeInput.value = "";
  pendingHomeMessage = text;
  location.hash = "#/chat";
});

// ---------- File attachments ----------

const MAX_FILE_BYTES = 8 * 1024 * 1024;   // 8 MB per file
const TEXT_EXTS = ["txt", "csv", "tsv", "md", "json", "log", "yaml", "yml", "xml"];
const IMAGE_EXTS = ["png", "jpg", "jpeg", "gif", "webp"];

attachBtn.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", async () => {
  for (const file of fileInput.files) await addAttachment(file);
  fileInput.value = "";
  renderAttachments();
});

async function addAttachment(file) {
  if (file.size > MAX_FILE_BYTES) {
    alert(`"${file.name}" is larger than 8 MB and was skipped.`);
    return;
  }
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  const type = file.type || "";
  const isImage = type.startsWith("image/") || IMAGE_EXTS.includes(ext);
  const isPdf = type === "application/pdf" || ext === "pdf";
  const isText = type.startsWith("text/") || TEXT_EXTS.includes(ext);
  try {
    if (isImage) {
      pendingAttachments.push({ name: file.name, kind: "image",
        media_type: type || `image/${ext === "jpg" ? "jpeg" : ext}`, data: await readBase64(file) });
    } else if (isPdf) {
      pendingAttachments.push({ name: file.name, kind: "document",
        media_type: "application/pdf", data: await readBase64(file) });
    } else if (isText) {
      pendingAttachments.push({ name: file.name, kind: "text",
        media_type: "text/plain", data: await readText(file) });
    } else {
      alert(`"${file.name}" isn't a supported type. Use images, PDF, or text/CSV files.`);
    }
  } catch {
    alert(`Could not read "${file.name}".`);
  }
}

function readBase64(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1] || "");
    r.onerror = rej;
    r.readAsDataURL(file);
  });
}
function readText(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result));
    r.onerror = rej;
    r.readAsText(file);
  });
}

function renderAttachments() {
  attachmentsEl.innerHTML = pendingAttachments.map((a, i) =>
    `<span class="attach-chip">${escapeHtml(a.name)}<button type="button" class="attach-remove" data-i="${i}" aria-label="Remove ${escapeHtml(a.name)}">×</button></span>`).join("");
  attachmentsEl.querySelectorAll(".attach-remove").forEach(b =>
    b.addEventListener("click", () => {
      pendingAttachments.splice(Number(b.dataset.i), 1);
      renderAttachments();
    }));
}

// ---------- New-session button (Chat only; reports create threads per-folder) ----------

newSessionBtn.addEventListener("click", () => {
  if (view.kind === "chat") location.hash = "#/chat";   // fresh unsaved chat
});

// ---------- Router ----------

const ALL_VIEWS = [bootView, setupView, homeView, featureView, dataView,
                   schedulerView, alertsView, driversView, scenariosView];
function showOnly(viewEl) {
  ALL_VIEWS.forEach(v => v.classList.toggle("hidden", v !== viewEl));
}

async function renderFeature(kind, sessionId) {
  view.kind = kind;
  view.sessionId = sessionId || null;
  view.parentId = null;
  view.parent = null;
  const meta = KIND_META[kind];

  showOnly(featureView);
  featureHeading.textContent = meta.heading;
  featureSub.textContent = meta.sub;
  sidebarTitleEl.textContent = meta.sidebar;
  newSessionBtn.textContent = meta.newLabel;
  newSessionBtn.style.display = kind === "chat" ? "" : "none";  // reports use per-folder "+ New chat"
  input.placeholder = meta.placeholder;
  updateTopicSwitcher(kind);
  pendingAttachments = [];       // don't carry staged files across views
  renderAttachments();

  await refreshSidebar();

  if (kind === "chat") {
    view.mode = "chat";
    if (!sessionId) {
      renderChatView(null);
      // A question typed on the home screen: send it now that the chat view
      // is up. sendMessage → resolveTargetSession creates the session lazily.
      const pending = pendingHomeMessage;
      pendingHomeMessage = null;
      if (pending) sendMessage(pending, []);
      else input.focus();
      return;
    }
    messagesEl.innerHTML = `<div class="msg assistant"><div class="bubble"><p class="thinking">Loading…</p></div></div>`;
    try { renderChatView(await apiGetSession(sessionId)); }
    catch { messagesEl.innerHTML = `<div class="msg assistant"><div class="bubble"><p class="error-text">⚠ Could not load this conversation.</p></div></div>`; }
    input.focus();
    return;
  }

  // Reports (weekly / monthly)
  if (!sessionId) {
    const data = await apiListSessions(kind);
    const ready = s => !s.parent_id && (s.message_count || 0) >= 2;
    const current = (data.sessions || []).find(s => s.period === data.current_period && ready(s))
                 || (data.sessions || []).find(ready);
    if (current) { location.replace(`#/${kind}/${current.id}`); return; }
    renderReportPlaceholder(kind, data.generating);
    if (data.generating) scheduleReportPoll(kind);
    return;
  }

  messagesEl.innerHTML = `<div class="msg assistant"><div class="bubble"><p class="thinking">Loading…</p></div></div>`;
  let session;
  try {
    session = await apiGetSession(sessionId);
  } catch {
    messagesEl.innerHTML = `<div class="msg assistant"><div class="bubble"><p class="error-text">⚠ Could not load this session.</p></div></div>`;
    return;
  }
  if (session.parent_id) {
    // A chat thread under a report.
    view.mode = "child";
    view.parentId = session.parent_id;
    view.parent = await apiGetSession(session.parent_id).catch(() => null);
    renderChildView(session, view.parent);
  } else {
    // The report overview itself. Keep it in view.parent so a thread started from
    // the report composer can reference it (banner + parent_id).
    view.mode = "report";
    view.parent = session;
    renderReportView(session);
  }
  input.focus();
}

function renderReportPlaceholder(kind, generating) {
  const meta = KIND_META[kind];
  if (generating) {
    messagesEl.innerHTML = `<div class="msg assistant"><div class="bubble"><p class="thinking"><span class="mini-spinner"></span> Generating this ${kind === "weekly" ? "week" : "month"}'s report… this can take a few seconds. It'll appear here automatically.</p></div></div>`;
    return;
  }
  messagesEl.innerHTML = `<div class="msg assistant"><div class="bubble"><p>No report yet.</p><p><button id="gen-report-btn" class="primary-btn" style="width:auto">Generate this ${kind === "weekly" ? "week" : "month"}'s report</button></p></div></div>`;
  const btn = document.getElementById("gen-report-btn");
  if (btn) btn.addEventListener("click", async () => {
    btn.disabled = true; btn.textContent = "Generating…";
    try { const r = await apiGenerateReport(kind); location.hash = `#/${kind}/${r.id}`; }
    catch (err) { alert(err.message); btn.disabled = false; }
  });
}

// Poll for a startup-generated report to appear, then open it.
let reportPollTimer = null;
function scheduleReportPoll(kind) {
  clearTimeout(reportPollTimer);
  reportPollTimer = setTimeout(async () => {
    if (view.kind !== kind || view.sessionId) return;  // navigated away
    try {
      const data = await apiListSessions(kind);
      const current = (data.sessions || []).find(s =>
        s.period === data.current_period && (s.message_count || 0) >= 2);
      if (current) { location.replace(`#/${kind}/${current.id}`); return; }
    } catch { /* server busy — keep polling */ }
    if (view.kind === kind && !view.sessionId) scheduleReportPoll(kind);
  }, 3000);
}

function updateTopicSwitcher(kind) {
  topicSwitcher.classList.remove("hidden");
  topicSwitcher.querySelectorAll("a").forEach(a =>
    a.classList.toggle("active", a.dataset.kind === kind));
  dataLink.classList.toggle("active", kind === "data");
  schedulerLink.classList.toggle("active", kind === "scheduler");
}

function showHome() {
  showOnly(homeView);
  topicSwitcher.classList.add("hidden");   // topics are already on the home screen
  dataLink.classList.remove("active");
  schedulerLink.classList.remove("active");
  view.sessionId = null;
  pendingHomeMessage = null;               // don't carry an unconsumed question around
  pendingAlertId = null;
  renderHomeSchedules();
  // Keep the overview live while the home screen is up (running state changes
  // within seconds); route() clears the timer on navigation.
  homeSchedulesTimer = setInterval(renderHomeSchedules, 5000);
}

// ---------- Home-screen schedule overview ----------

const homeSchedulesEl = document.getElementById("home-schedules");
const homeSchedulesList = document.getElementById("home-schedules-list");
let homeSchedulesTimer = null;

async function renderHomeSchedules() {
  let tasks;
  try {
    const resp = await fetch("/api/tasks");
    if (!resp.ok) throw new Error();
    tasks = (await resp.json()).tasks || [];
  } catch {
    homeSchedulesEl.classList.add("hidden");   // old backend / server down — just omit the section
    return;
  }
  if (!tasks.length) { homeSchedulesEl.classList.add("hidden"); return; }
  homeSchedulesEl.classList.remove("hidden");
  homeSchedulesList.innerHTML = "";
  for (const t of tasks) {
    const li = document.createElement("li");
    li.className = "home-schedule-row";
    let status;
    if (t.last_status === "running") {
      status = `<span class="home-schedule-status is-running"><span class="ball-stage"><span class="bounce-ball"></span></span>Running now</span>`;
    } else if (!t.enabled) {
      status = `<span class="home-schedule-status is-paused">Paused</span>`;
    } else if (t.last_status === "error") {
      status = `<span class="home-schedule-status is-error">Active · last run failed · next ${countdownText(t.next_run_utc)}</span>`;
    } else {
      status = `<span class="home-schedule-status is-idle">Active · next ${countdownText(t.next_run_utc)}</span>`;
    }
    li.innerHTML = `
      <span class="home-schedule-name">${escapeHtml(t.name)}</span>
      <span class="home-schedule-when">${escapeHtml(scheduleSummary(t.schedule))}</span>
      ${status}`;
    li.addEventListener("click", () => { location.hash = "#/scheduler"; });
    homeSchedulesList.appendChild(li);
  }
}

// ---------- Data ingestion view (#/data) ----------

async function renderData() {
  view.sessionId = null;
  showOnly(dataView);
  updateTopicSwitcher("data");
  datasetsListEl.innerHTML = `<p class="empty">Loading datasets…</p>`;
  try {
    const [dataResp, settingsResp] = await Promise.all([fetch("/api/datasets"), fetch("/api/settings")]);
    if (!dataResp.ok) throw new Error(`Server error ${dataResp.status}`);
    const data = await dataResp.json();
    const cfg = settingsResp.ok ? await settingsResp.json() : {};
    renderDatasetCards(data.datasets || [], cfg.scheduler || {});
  } catch (err) {
    datasetsListEl.innerHTML = `<p class="error-text">⚠ Could not load datasets: ${escapeHtml(err.message)}</p>`;
  }
}

function renderDatasetCards(datasets, scheduler) {
  dataRefreshStatus.textContent = scheduler.enabled
    ? `Scheduled refresh: every ${scheduler.interval_seconds}s`
    : "Scheduled refresh: off";
  datasetsListEl.innerHTML = "";
  if (!datasets.length) {
    datasetsListEl.innerHTML = `<p class="empty">No datasets found.</p>`;
    return;
  }
  for (const d of datasets) {
    const card = document.createElement("div");
    card.className = "data-card";
    if (d.error) {
      card.innerHTML = `<div class="data-card-head"><h3>${escapeHtml(d.name)}</h3></div>
        <p class="error-text">⚠ ${escapeHtml(d.error)}</p>`;
      datasetsListEl.appendChild(card);
      continue;
    }
    const meta = `${d.columns.length} columns · ${d.rows} rows` +
      (d.period ? ` · ${escapeHtml(d.period)}` : "");
    const freshness = d.last_refreshed_utc
      ? `<p class="data-freshness">Last refreshed: <strong>${new Date(d.last_refreshed_utc).toLocaleString()}</strong> — kept fresh by the scheduled refresh job</p>`
      : "";
    card.innerHTML = `
      <div class="data-card-head">
        <h3>${escapeHtml(d.name)}</h3>
        <span class="format-badge">${escapeHtml(d.format || "")}</span>
      </div>
      <p class="data-desc">${escapeHtml(d.description || "")}</p>
      <div class="data-meta">
        <span class="source-chip">data/${escapeHtml(d.source_file)}</span>
        <span class="data-meta-text">${meta}</span>
      </div>
      <div class="data-columns">${d.columns.map(c => `<code>${escapeHtml(c)}</code>`).join("")}</div>
      ${freshness}
      <button type="button" class="secondary data-preview-btn">Preview</button>
      <div class="data-preview hidden"></div>`;
    wirePreview(card, d.name);
    datasetsListEl.appendChild(card);
  }
}

// Lazy-load the first rows of a dataset on first click, then just toggle.
function wirePreview(card, name) {
  const btn = card.querySelector(".data-preview-btn");
  const box = card.querySelector(".data-preview");
  btn.addEventListener("click", async () => {
    if (!box.dataset.loaded) {
      btn.disabled = true;
      try {
        const resp = await fetch(`/api/datasets/${encodeURIComponent(name)}/preview?rows=5`);
        const p = await resp.json();
        if (!resp.ok) throw new Error(p.error || `Server error ${resp.status}`);
        const head = p.columns.map(c => `<th>${escapeHtml(c)}</th>`).join("");
        const body = p.records.map(r =>
          "<tr>" + p.columns.map(c => `<td>${escapeHtml(String(r[c] ?? ""))}</td>`).join("") + "</tr>").join("");
        box.innerHTML = `<div class="data-table-wrap"><table class="data-table"><tr>${head}</tr>${body}</table></div>
          <p class="hint">First ${p.records.length} of ${p.rows_total} rows · ${escapeHtml(p.source_file)}</p>`;
        box.dataset.loaded = "1";
      } catch (err) {
        box.innerHTML = `<p class="error-text">⚠ ${escapeHtml(err.message)}</p>`;
      } finally {
        btn.disabled = false;
      }
    }
    box.classList.toggle("hidden");
    btn.textContent = box.classList.contains("hidden") ? "Preview" : "Hide preview";
  });
}

// Manual refresh from the data view — re-render so last_refreshed_utc visibly moves.
dataRefreshNow.addEventListener("click", async () => {
  dataRefreshNow.disabled = true;
  try {
    await fetch("/api/refresh-now", { method: "POST" });
    await renderData();
    loadSettings();   // keep the header badge + settings panel in sync
  } finally {
    dataRefreshNow.disabled = false;
  }
});

// ---------- Scheduler view (#/scheduler) ----------

const TASK_TYPE_META = {
  data_refresh:   "Data refresh",
  weekly_report:  "Weekly report",
  monthly_report: "Monthly report",
  custom_prompt:  "Custom prompt",
  urgency_scan:   "Urgency scan",
};
const DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

let taskCountdownTimer = null;   // 1s next-run countdown, cleared on route change
let taskPollTimer = null;        // re-fetch while a task is running
let editingTaskId = null;        // task loaded into the form, or null for "new"

function fmtInterval(s) {
  if (s % 3600 === 0) return s === 3600 ? "hour" : `${s / 3600} hours`;
  if (s % 60 === 0) return s === 60 ? "minute" : `${s / 60} minutes`;
  return `${s} seconds`;
}

function scheduleSummary(sched) {
  if (!sched) return "—";
  if (sched.mode === "interval") return `every ${fmtInterval(sched.interval_seconds || 300)}`;
  if (sched.mode === "daily") return `daily at ${sched.time}`;
  if (sched.mode === "weekly") return `${DOW_NAMES[sched.day_of_week || 0]}s at ${sched.time}`;
  if (sched.mode === "monthly") {
    const d = sched.day_of_month || 1;
    const suffix = d % 10 === 1 && d !== 11 ? "st" : d % 10 === 2 && d !== 12 ? "nd"
                 : d % 10 === 3 && d !== 13 ? "rd" : "th";
    return `the ${d}${suffix} at ${sched.time}`;
  }
  return "—";
}

function statusChip(t) {
  if (t.last_status === "running") return `<span class="status-chip running"><span class="mini-spinner"></span>running</span>`;
  if (t.last_status === "error") return `<span class="status-chip error">error</span>`;
  if (t.last_status === "ok") return `<span class="status-chip ok">ok</span>`;
  return `<span class="status-chip never">never run</span>`;
}

async function renderScheduler() {
  view.sessionId = null;
  showOnly(schedulerView);
  updateTopicSwitcher("scheduler");
  taskListEl.innerHTML = `<p class="empty">Loading tasks…</p>`;
  await refreshTaskList();
}

async function refreshTaskList() {
  let data;
  try {
    const resp = await fetch("/api/tasks");
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    taskListEl.innerHTML = `<p class="error-text">⚠ Could not load tasks: ${escapeHtml(err.message)}</p>`;
    return;
  }
  renderTaskCards(data.tasks || []);
}

function renderTaskCards(tasks) {
  clearInterval(taskCountdownTimer);
  clearTimeout(taskPollTimer);
  taskListEl.innerHTML = "";
  if (!tasks.length) {
    taskListEl.innerHTML = `<p class="empty">No tasks yet — create one to put the agent to work.</p>`;
    return;
  }
  for (const t of tasks) taskListEl.appendChild(taskCard(t));

  // Live countdown to each task's next run, computed client-side.
  taskCountdownTimer = setInterval(() => {
    taskListEl.querySelectorAll("[data-next-run]").forEach(el => {
      el.textContent = countdownText(el.dataset.nextRun);
    });
  }, 1000);

  // While anything is running, keep re-fetching so the status chip resolves.
  if (tasks.some(t => t.last_status === "running")) {
    taskPollTimer = setTimeout(() => {
      if (!schedulerView.classList.contains("hidden")) refreshTaskList();
    }, 2000);
  }
}

function countdownText(nextRunUtc) {
  if (!nextRunUtc) return "paused";
  const secs = Math.round((new Date(nextRunUtc) - Date.now()) / 1000);
  if (secs <= 0) return "due now";
  if (secs < 90) return `in ${secs}s`;
  if (secs < 5400) return `in ${Math.round(secs / 60)}m`;
  if (secs < 90000) return `in ${Math.round(secs / 3600)}h`;
  return `in ${Math.round(secs / 86400)}d`;
}

function taskCard(t) {
  const card = document.createElement("div");
  card.className = "task-card";
  const lastRun = t.last_run_utc ? new Date(t.last_run_utc).toLocaleString() : "never";
  card.innerHTML = `
    <div class="task-card-head">
      <h3>${escapeHtml(t.name)}</h3>
      <span class="task-type-badge">${TASK_TYPE_META[t.type] || escapeHtml(t.type)}</span>
      ${statusChip(t)}
    </div>
    <p class="task-meta">
      Runs <strong>${escapeHtml(scheduleSummary(t.schedule))}</strong>
      · next <strong data-next-run="${t.enabled && t.next_run_utc ? escapeHtml(t.next_run_utc) : ""}">${countdownText(t.enabled ? t.next_run_utc : null)}</strong>
      · last run: ${escapeHtml(lastRun)}${t.last_detail ? ` — ${escapeHtml(t.last_detail)}` : ""}
    </p>
    ${t.type === "custom_prompt" && t.prompt ? `<p class="task-prompt-preview">“${escapeHtml(t.prompt)}”</p>` : ""}
    ${t.last_status === "error" && t.last_error ? `<p class="task-error">⚠ ${escapeHtml(t.last_error)}</p>` : ""}
    <div class="task-actions">
      <button class="secondary task-run">Run now</button>
      <button class="secondary task-edit">Edit</button>
      ${t.builtin ? "" : `<button class="secondary danger task-del">Delete</button>`}
      <label class="task-toggle"><input type="checkbox" class="task-enabled" ${t.enabled ? "checked" : ""}> Enabled</label>
    </div>`;

  card.querySelector(".task-run").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await fetch(`/api/tasks/${t.id}/run-now`, { method: "POST" }); }
    finally { setTimeout(refreshTaskList, 600); }
  });
  card.querySelector(".task-edit").addEventListener("click", () => openTaskForm(t));
  card.querySelector(".task-enabled").addEventListener("change", async (e) => {
    await fetch(`/api/tasks/${t.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: e.target.checked }),
    });
    refreshTaskList();
  });
  const del = card.querySelector(".task-del");
  if (del) del.addEventListener("click", async () => {
    if (!confirm(`Delete the task "${t.name}"?`)) return;
    await fetch(`/api/tasks/${t.id}`, { method: "DELETE" });
    refreshTaskList();
  });
  return card;
}

// ----- Task form (create / edit) -----

const taskName = document.getElementById("task-name");
const taskType = document.getElementById("task-type");
const taskPrompt = document.getElementById("task-prompt");
const taskMode = document.getElementById("task-mode");
const taskInterval = document.getElementById("task-interval");
const taskTime = document.getElementById("task-time");
const taskDow = document.getElementById("task-dow");
const taskDom = document.getElementById("task-dom");
const taskEnabled = document.getElementById("task-enabled");

function updateTaskFormFields() {
  const mode = taskMode.value;
  taskForm.querySelector(".task-prompt-row").classList.toggle("hidden", taskType.value !== "custom_prompt");
  taskForm.querySelector(".task-interval-row").classList.toggle("hidden", mode !== "interval");
  taskForm.querySelector(".task-time-row").classList.toggle("hidden", mode === "interval");
  taskForm.querySelector(".task-dow-row").classList.toggle("hidden", mode !== "weekly");
  taskForm.querySelector(".task-dom-row").classList.toggle("hidden", mode !== "monthly");
}
taskType.addEventListener("change", updateTaskFormFields);
taskMode.addEventListener("change", updateTaskFormFields);

function openTaskForm(task) {
  editingTaskId = task ? task.id : null;
  document.getElementById("task-form-title").textContent = task ? `Edit “${task.name}”` : "New task";
  taskName.value = task ? task.name : "";
  taskType.value = task ? task.type : "custom_prompt";
  taskPrompt.value = (task && task.prompt) || "";
  const sched = (task && task.schedule) || { mode: "interval", interval_seconds: 300 };
  taskMode.value = sched.mode;
  if (sched.mode === "interval") {
    taskInterval.value = [...taskInterval.options].some(o => o.value === String(sched.interval_seconds))
      ? String(sched.interval_seconds) : "300";
  }
  taskTime.value = sched.time || "07:30";
  taskDow.value = String(sched.day_of_week ?? 0);
  taskDom.value = String(sched.day_of_month ?? 1);
  taskEnabled.checked = task ? !!task.enabled : true;
  // The built-in refresh task keeps its identity: only pacing is editable.
  const locked = !!(task && task.builtin);
  taskName.disabled = locked;
  taskType.disabled = locked;
  taskMode.disabled = locked;
  updateTaskFormFields();
  taskForm.classList.remove("hidden");
  taskName.focus();
}

function closeTaskForm() {
  editingTaskId = null;
  taskForm.classList.add("hidden");
}

document.getElementById("new-task-btn").addEventListener("click", () => openTaskForm(null));
document.getElementById("task-form-cancel").addEventListener("click", closeTaskForm);

taskForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const mode = taskMode.value;
  const schedule = { mode };
  if (mode === "interval") schedule.interval_seconds = parseInt(taskInterval.value, 10);
  else schedule.time = taskTime.value || "07:30";
  if (mode === "weekly") schedule.day_of_week = parseInt(taskDow.value, 10);
  if (mode === "monthly") schedule.day_of_month = parseInt(taskDom.value, 10);
  const payload = {
    name: taskName.value.trim(),
    type: taskType.value,
    prompt: taskPrompt.value.trim() || null,
    schedule,
    enabled: taskEnabled.checked,
  };
  try {
    const resp = await fetch(editingTaskId ? `/api/tasks/${editingTaskId}` : "/api/tasks", {
      method: editingTaskId ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `Server error ${resp.status}`);
  } catch (err) {
    alert(err.message);
    return;
  }
  closeTaskForm();
  refreshTaskList();
});

// ---------- Alerts view (#/alerts) ----------

async function renderAlerts() {
  view.sessionId = null;
  showOnly(alertsView);
  updateTopicSwitcher("alerts");
  alertsListEl.innerHTML = `<p class="empty">Loading alerts…</p>`;
  let data;
  try {
    const resp = await fetch("/api/alerts");
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    alertsListEl.innerHTML = `<p class="error-text">⚠ Could not load alerts: ${escapeHtml(err.message)}</p>`;
    return;
  }
  renderAlertCards(data.alerts || []);
}

function renderAlertCards(items) {
  alertsListEl.innerHTML = "";
  if (!items.length) {
    alertsListEl.innerHTML = `<p class="empty">No alerts — all monitored thresholds are healthy. Adjust the urgency rules in Settings, or press “Scan now”.</p>`;
    return;
  }
  for (const a of items) alertsListEl.appendChild(alertCard(a));
}

function alertCard(a) {
  const card = document.createElement("div");
  card.className = "alert-card" + (a.read ? "" : " unread");
  card.innerHTML = `
    <div class="alert-card-head">
      <span class="sev-chip sev-${escapeHtml(a.severity)}">${escapeHtml(a.severity)}</span>
      <h3>${escapeHtml(a.title)}</h3>
      <span class="alert-when">${fmtWhen(a.created_at)}</span>
    </div>
    <div class="alert-meta">
      <span class="source-chip">rule: ${escapeHtml(a.rule_id)}</span>
      <span class="source-chip">${escapeHtml(a.entity)}</span>
      <span class="source-chip">${escapeHtml(a.period)}</span>
    </div>
    <div class="alert-narrative">${renderMarkdown(a.narrative || "")}</div>
    <div class="alert-actions">
      <button class="secondary alert-investigate">Investigate in chat</button>
      ${a.chat_session_id ? `<a class="alert-chat-link" href="#/chat/${escapeHtml(a.chat_session_id)}">View investigation ↗</a>` : ""}
      ${a.read ? "" : `<button class="secondary alert-read">Mark read</button>`}
      <button class="secondary danger alert-del">Delete</button>
    </div>`;

  card.querySelector(".alert-investigate").addEventListener("click", () => investigateAlert(a));
  const readBtn = card.querySelector(".alert-read");
  if (readBtn) readBtn.addEventListener("click", async () => {
    await fetch(`/api/alerts/${a.id}/read`, { method: "POST" });
    renderAlerts();
    loadSettings();   // sync the bell badge right away
  });
  card.querySelector(".alert-del").addEventListener("click", async () => {
    if (!confirm("Delete this alert?")) return;
    await fetch(`/api/alerts/${a.id}`, { method: "DELETE" });
    renderAlerts();
    loadSettings();
  });
  return card;
}

// Open a fresh chat pre-seeded with the alert context; the normal lazy-session
// streaming path (pendingHomeMessage) takes over, and once the session exists
// sendMessage links it back to the alert.
function investigateAlert(a) {
  fetch(`/api/alerts/${a.id}/read`, { method: "POST" }).catch(() => {});
  pendingAlertId = a.id;
  pendingHomeMessage = `Investigate this alert: ${a.title} (rule ${a.rule_id}, observed value ` +
    `${a.metric_value} vs threshold ${a.threshold}, period ${a.period}). Verify the figures, ` +
    `explain the drivers and the recent trend, and recommend actions.`;
  location.hash = "#/chat";
}

document.getElementById("alerts-read-all").addEventListener("click", async () => {
  await fetch("/api/alerts/read-all", { method: "POST" });
  renderAlerts();
  loadSettings();
});

document.getElementById("alerts-scan-now").addEventListener("click", async (e) => {
  const btn = e.target;
  btn.disabled = true;
  btn.textContent = "Scanning…";
  try {
    await fetch("/api/alerts/scan", { method: "POST" });
    await renderAlerts();
    loadSettings();
  } finally {
    btn.disabled = false;
    btn.textContent = "Scan now";
  }
});

alertsBtn.addEventListener("click", () => { location.hash = "#/alerts"; });

function route() {
  clearTimeout(reportPollTimer);
  clearInterval(taskCountdownTimer);
  clearTimeout(taskPollTimer);
  clearInterval(homeSchedulesTimer);
  clearInterval(driverPollTimer);     // forgetting this leaks a 2s fetch loop
  const hash = location.hash.replace(/^#\/?/, "");
  const [kind, id] = hash.split("/");

  // The gate. `setup_complete` is spelled exactly as the API returns it and as
  // profile.py persists it — spelling it `completed` here would read as
  // undefined, permanently falsy, and redirect every route to #/setup forever,
  // including after the CFO confirms. It fails as a hard lock-out that a smoke
  // test catches but cannot diagnose, so the name is kept identical end to end.
  if (!profile?.setup_complete && kind !== "setup") {
    // replace, not assign: a gated URL never enters history, so Back can't
    // bounce into it. No loop, because #/setup is itself ungated.
    location.replace("#/setup");
    return;
  }
  if (kind === "setup") {
    renderSetup();
  } else if (kind === "drivers") {
    renderDrivers();
  } else if (kind === "scenarios") {
    renderScenarios(id);
  } else if (kind === "chat" || kind === "weekly" || kind === "monthly") {
    renderFeature(kind, id);
  } else if (kind === "data") {
    renderData();
  } else if (kind === "scheduler") {
    renderScheduler();
  } else if (kind === "alerts") {
    renderAlerts();
  } else {
    showHome();
  }
}

window.addEventListener("hashchange", route);

// ---------- Settings panel ----------

const modelSelect = document.getElementById("model-select");
const refreshEnabled = document.getElementById("refresh-enabled");
const refreshInterval = document.getElementById("refresh-interval");
const refreshStatus = document.getElementById("refresh-status");
const showDebug = document.getElementById("show-debug");

document.getElementById("settings-btn").addEventListener("click", () =>
  settingsPanel.classList.toggle("hidden"));
document.getElementById("close-settings").addEventListener("click", () =>
  settingsPanel.classList.add("hidden"));

function applySettings(data) {
  settings.model = data.model;
  settings.show_debug = data.show_debug;
  modelSelect.innerHTML = data.available_models
    .map(m => `<option value="${m}" ${m === data.model ? "selected" : ""}>${m}</option>`).join("");
  showDebug.checked = data.show_debug;
  refreshEnabled.checked = data.scheduler.enabled;
  refreshInterval.value = String(data.scheduler.interval_seconds);
  updateSchedulerUi(data.scheduler);
  updateAlertsUi(data.alerts);
}

function updateSchedulerUi(s) {
  const last = s.last_refresh_utc ? new Date(s.last_refresh_utc).toLocaleTimeString() : "not yet";
  refreshStatus.textContent = s.enabled
    ? `Last refresh: ${last} · next in ~${s.next_refresh_in_seconds}s`
    : `Paused · last refresh: ${last}`;
  schedulerBadge.textContent = s.enabled ? `refresh: every ${s.interval_seconds}s` : "refresh: off";
  schedulerBadge.classList.toggle("off", !s.enabled);
}

async function loadSettings() {
  const resp = await fetch("/api/settings");
  applySettings(await resp.json());
}

async function pushSettings(update) {
  const resp = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
  applySettings(await resp.json());
}

modelSelect.addEventListener("change", () => pushSettings({ model: modelSelect.value }));
showDebug.addEventListener("change", () => pushSettings({ show_debug: showDebug.checked }));
refreshEnabled.addEventListener("change", () => pushSettings({ refresh_enabled: refreshEnabled.checked }));
refreshInterval.addEventListener("change", () =>
  pushSettings({ refresh_interval_seconds: parseInt(refreshInterval.value, 10) }));
document.getElementById("refresh-now").addEventListener("click", async () => {
  await fetch("/api/refresh-now", { method: "POST" });
  loadSettings();
});

// ---------- Alert badge + browser notifications ----------
// New-alert delivery rides the existing 10s settings poll: the payload carries
// {unread, latest_created_at}, which drives the bell badge and (when granted)
// a browser notification for anything newer than the last poll.

function updateAlertsUi(a) {
  if (!a) return;
  const unread = a.unread || 0;
  alertsBadge.textContent = unread > 9 ? "9+" : String(unread);
  alertsBadge.classList.toggle("hidden", unread === 0);
  const latest = a.latest_created_at || 0;
  if (lastAlertTs === null) { lastAlertTs = latest; return; }   // first poll: don't notify about old alerts
  if (latest > lastAlertTs) {
    const since = lastAlertTs;
    lastAlertTs = latest;
    notifyNewAlerts(since);
    if (location.hash.replace(/^#\/?/, "").startsWith("alerts")) renderAlerts();  // live-update the open inbox
  }
}

async function notifyNewAlerts(sinceTs) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const data = await (await fetch("/api/alerts")).json();
    const fresh = (data.alerts || []).filter(a => (a.created_at || 0) > sinceTs).slice(0, 3);
    for (const a of fresh) {
      const n = new Notification(a.title, {
        body: (a.narrative || "").replace(/[*_`#]/g, "").slice(0, 140),
        tag: a.id,   // no duplicate popups for the same alert
      });
      n.onclick = () => { window.focus(); location.hash = "#/alerts"; };
    }
  } catch { /* notification is best-effort */ }
}

const notifyEnable = document.getElementById("notify-enable");
const notifyHint = document.getElementById("notify-hint");

function refreshNotifyUi() {
  if (!("Notification" in window)) {
    notifyEnable.disabled = true;
    notifyHint.textContent = "This browser doesn't support notifications.";
  } else if (Notification.permission === "granted") {
    notifyEnable.disabled = true;
    notifyEnable.textContent = "Notifications enabled ✓";
    notifyHint.textContent = "You'll get a desktop notification when a new alert lands.";
  } else if (Notification.permission === "denied") {
    notifyEnable.disabled = true;
    notifyEnable.textContent = "Notifications blocked";
    notifyHint.textContent = "Notifications are blocked — allow them in the browser's site settings.";
  }
}

notifyEnable.addEventListener("click", async () => {
  if (!("Notification" in window)) return;
  await Notification.requestPermission();
  refreshNotifyUi();
});

// ---------- Urgency rules (settings panel) ----------

const rulesListEl = document.getElementById("rules-list");

async function loadRules() {
  try {
    const resp = await fetch("/api/rules");
    if (!resp.ok) return;
    renderRules((await resp.json()).rules || []);
  } catch { /* settings panel just shows nothing */ }
}

function renderRules(ruleItems) {
  rulesListEl.innerHTML = "";
  for (const r of ruleItems) {
    const row = document.createElement("label");
    row.className = "row rule-row";
    row.dataset.id = r.id;
    row.innerHTML = `
      <span class="rule-label">
        <input type="checkbox" class="rule-enabled" ${r.enabled ? "checked" : ""}>
        <span>${escapeHtml(r.label)}</span>
      </span>
      <input type="number" class="rule-threshold" step="any" value="${Number(r.threshold)}">`;
    rulesListEl.appendChild(row);
  }
  rulesListEl.querySelectorAll("input").forEach(el =>
    el.addEventListener("change", pushRules));
}

async function pushRules() {
  const rules = [...rulesListEl.querySelectorAll(".rule-row")].map(row => ({
    id: row.dataset.id,
    enabled: row.querySelector(".rule-enabled").checked,
    threshold: parseFloat(row.querySelector(".rule-threshold").value),
  })).filter(r => Number.isFinite(r.threshold));
  try {
    const resp = await fetch("/api/rules", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rules }),
    });
    if (resp.ok) renderRules((await resp.json()).rules || []);
  } catch { /* keep the local state; next open re-syncs */ }
}

// Keep the scheduler badge + alert bell fresh.
setInterval(async () => {
  try {
    const resp = await fetch("/api/settings");
    const data = await resp.json();
    updateSchedulerUi(data.scheduler);
    updateAlertsUi(data.alerts);
  } catch { /* server restarting — ignore */ }
}, 10000);

// ---------- Boot ----------
//
// route() ran synchronously in the reference and #home-view shipped without
// .hidden, so home painted instantly. Gating needs an await before the first
// route, so #boot-view is the ONLY view without `hidden` and every other view
// (home included) gains it. Nothing is visible until route() decides, so there
// is no flash of the wrong view, ever.
async function loadProfile() {
  try {
    profile = await (await fetch("/api/profile")).json();
  } catch {
    profile = { setup_complete: false, has_api_key: false };
  }
  return profile;
}

async function boot() {
  await loadProfile();
  loadSettings();
  if (profile.setup_complete) loadRules();
  refreshNotifyUi();
  route();
}

boot();

// ==========================================================================
// Setup wizard  (#/setup — the only ungated route)
// ==========================================================================

function restoreReasoning(bubble, text) {
  const box = ensureReasoning(bubble);
  box.querySelector(".reasoning-body").textContent = text;
  box.open = false;
  box.dataset.userToggled = "1";     // a reopened session must not auto-collapse
  box.classList.remove("is-live");
  box.querySelector(".reasoning-meta").textContent = "";
}

const setupBody = document.getElementById("setup-body");

function renderSetup() {
  showOnly(setupView);
  const p = profile || {};
  if (p.setup_complete || (p.proposal && p.proposal.raw)) renderSetupEditor();
  else renderSetupIntro();
}

function renderSetupIntro() {
  const p = profile || {};
  setupBody.innerHTML = `
    <header class="page-head">
      <p class="eyebrow">Setup</p>
      <h2>Tell me about your business</h2>
      <p class="page-sub">Your budget rests on assumptions about prices you don't control.
        Describe what you make and sell, and I'll research which of those actually move your
        numbers — then you decide what to keep.</p>
    </header>
    <form id="setup-form" class="setup-form card">
      <label class="field">
        <span class="field-label">What does your company do?</span>
        <textarea id="setup-desc" rows="7" placeholder="We manufacture compound animal feed for poultry, swine and aquaculture across five plants in Europe. We buy grain and protein meals on the open market, mostly quoted in USD, and ship a meaningful share by sea container."></textarea>
      </label>
      <div class="field-row">
        <label class="field"><span class="field-label">Reporting currency</span>
          <input id="setup-ccy" value="EUR" maxlength="3"></label>
        <label class="field"><span class="field-label">Budget year</span>
          <input id="setup-year" type="number" value="${new Date().getFullYear() + 1}"></label>
        <label class="field"><span class="field-label">Fiscal year starts</span>
          <input id="setup-fy" type="number" min="1" max="12" value="1"></label>
      </div>
      <p id="setup-error" class="error-text hidden"></p>
      <div class="setup-actions">
        <button type="submit" class="btn-primary" ${p.has_api_key ? "" : "disabled"}>
          Research my cost drivers</button>
        <button type="button" id="setup-manual" class="btn-ghost">Fill it in by hand</button>
        ${p.has_demo_profile ? '<button type="button" id="setup-demo" class="btn-ghost">Load the demo company</button>' : ""}
      </div>
      ${p.has_api_key ? "" : `<p class="page-sub setup-nokey">No <code>ANTHROPIC_API_KEY</code> is set, so
        I can't research for you. You can still fill the watchlist in by hand or load the demo
        company — everything deterministic keeps working: the datasets, the drivers page, rule
        evaluation and alerts.</p>`}
    </form>
    <div id="setup-stream" class="setup-stream hidden"><div class="messages" id="setup-messages"></div></div>`;

  document.getElementById("setup-manual").addEventListener("click", () => {
    profile.proposal = { raw: { company: {}, product_lines: [], markets: [], cost_drivers: [] } };
    renderSetupEditor();
  });
  const demoBtn = document.getElementById("setup-demo");
  if (demoBtn) demoBtn.addEventListener("click", async () => {
    demoBtn.disabled = true;
    profile = await (await fetch("/api/profile/demo", { method: "POST" })).json();
    location.replace("#/");
    route();
  });
  document.getElementById("setup-form").addEventListener("submit", runProposal);
}

async function runProposal(e) {
  e.preventDefault();
  const desc = document.getElementById("setup-desc").value.trim();
  const err = document.getElementById("setup-error");
  if (desc.length < 30) {
    // Inline, never alert() — a modal thrown after five minutes of typing feels
    // punitive, and the typed description must never be cleared.
    err.textContent = "Tell me a little more — a sentence or two about what you make and sell.";
    err.classList.remove("hidden");
    return;
  }
  err.classList.add("hidden");
  e.target.querySelector("button[type=submit]").disabled = true;

  const wrap = document.getElementById("setup-stream");
  wrap.classList.remove("hidden");
  const container = document.getElementById("setup-messages");
  container.innerHTML = "";
  const bubble = addAssistantMessage(container);
  // The same reveal loop, on a different container — which is why it was
  // extracted in the first place.
  const stream = createStreamRenderer(container, bubble);
  let proposed = null;

  try {
    const resp = await fetch("/api/profile/propose", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        description: desc,
        currency: document.getElementById("setup-ccy").value.trim().toUpperCase() || "EUR",
        budget_year: Number(document.getElementById("setup-year").value) || null,
        fiscal_year_start: Number(document.getElementById("setup-fy").value) || 1,
      }),
    });
    await readSSE(resp, (ev) => {
      if (ev.type === "proposal") proposed = ev.profile;
      else stream.handleEvent(ev);
    });
  } catch (ex) {
    stream.fail(ex.message);
  } finally {
    stream.finish();
    e.target.querySelector("button[type=submit]").disabled = false;
  }
  if (proposed && proposed.proposal) {
    profile = proposed;
    renderSetupEditor();
  }
}

function renderSetupEditor() {
  const p = profile || {};
  const src = p.setup_complete ? p : (p.proposal && p.proposal.raw) || {};
  const company = src.company || {};
  const drivers = src.cost_drivers || [];
  setupBody.innerHTML = `
    <header class="page-head">
      <p class="eyebrow">Setup · review</p>
      <h2>Your watchlist</h2>
      <p class="page-sub">Edit anything. These are the assumptions the budget will be defended on,
        so they should be yours, not mine.</p>
    </header>
    <form id="profile-form" class="setup-form card">
      <div class="field-row">
        <label class="field"><span class="field-label">Company</span>
          <input id="pf-name" value="${escapeHtml(company.name || "")}"></label>
        <label class="field"><span class="field-label">Industry</span>
          <input id="pf-industry" value="${escapeHtml(company.industry || "")}"></label>
      </div>
      <div class="field-row">
        <label class="field"><span class="field-label">Currency</span>
          <input id="pf-ccy" value="${escapeHtml(company.reporting_currency || "EUR")}" maxlength="3"></label>
        <label class="field"><span class="field-label">Budget year</span>
          <input id="pf-year" type="number" value="${company.budget_year || new Date().getFullYear() + 1}"></label>
      </div>
      <h3 class="section-head">Cost drivers <span class="muted">${drivers.length}</span></h3>
      <div id="pf-drivers" class="driver-editor"></div>
      <button type="button" id="pf-add" class="btn-ghost">+ Add a driver</button>
      <p id="pf-error" class="error-text hidden"></p>
      <div class="setup-actions">
        <button type="submit" class="btn-primary">Confirm and start</button>
        <button type="button" id="pf-back" class="btn-ghost">Start over</button>
      </div>
    </form>`;

  const list = document.getElementById("pf-drivers");
  drivers.forEach(d => list.appendChild(driverRow(d)));
  if (!drivers.length) list.appendChild(driverRow({}));

  document.getElementById("pf-add").addEventListener("click",
    () => list.appendChild(driverRow({})));
  document.getElementById("pf-back").addEventListener("click", () => {
    profile.proposal = null; profile.setup_complete = false; renderSetupIntro();
  });
  document.getElementById("profile-form").addEventListener("submit", submitProfile);
}

const DRIVER_CATEGORIES = ["ingredient", "logistics", "energy", "packaging", "fx", "labour", "other"];

function driverRow(d) {
  const row = document.createElement("div");
  row.className = "driver-row";
  row.innerHTML = `
    <input class="dr-id" placeholder="driver_id" value="${escapeHtml(d.driver_id || "")}">
    <input class="dr-name" placeholder="Name" value="${escapeHtml(d.name || "")}">
    <select class="dr-cat">${DRIVER_CATEGORIES.map(c =>
      `<option value="${c}"${c === (d.category || "other") ? " selected" : ""}>${c}</option>`).join("")}</select>
    <input class="dr-unit" placeholder="EUR/t" value="${escapeHtml(d.unit || "")}">
    <input class="dr-ccy" placeholder="EUR" maxlength="3" value="${escapeHtml(d.quote_currency || "EUR")}">
    <select class="dr-dir">
      <option value="up"${(d.adverse_direction || "up") === "up" ? " selected" : ""}>up hurts</option>
      <option value="down"${d.adverse_direction === "down" ? " selected" : ""}>down hurts</option>
    </select>
    <input class="dr-stale" type="number" min="1" max="365" value="${d.stale_after_days || 7}" title="Stale after (days)">
    <button type="button" class="dr-del" title="Remove">×</button>`;
  row.querySelector(".dr-del").addEventListener("click", () => row.remove());
  const why = d.why || "";
  if (why) {
    const note = document.createElement("p");
    note.className = "driver-why";
    note.textContent = why;
    const srcs = (d.sources || []).map(x => x.url).filter(Boolean);
    if (srcs.length) {
      const link = safeHttpUrl(srcs[0]);
      if (link) {
        const a = document.createElement("a");
        a.href = link; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.className = "driver-why-src"; a.textContent = " source ↗";
        note.appendChild(a);
      }
    }
    row.appendChild(note);
  }
  row.dataset.why = why;
  row.dataset.sources = JSON.stringify(d.sources || []);
  row.dataset.hint = d.search_hint || "";
  return row;
}

async function submitProfile(e) {
  e.preventDefault();
  const err = document.getElementById("pf-error");
  const rows = [...document.querySelectorAll(".driver-row")];
  const cost_drivers = rows.map(r => ({
    driver_id: r.querySelector(".dr-id").value.trim().toLowerCase(),
    name: r.querySelector(".dr-name").value.trim(),
    category: r.querySelector(".dr-cat").value,
    unit: r.querySelector(".dr-unit").value.trim(),
    quote_currency: r.querySelector(".dr-ccy").value.trim().toUpperCase(),
    adverse_direction: r.querySelector(".dr-dir").value,
    stale_after_days: Number(r.querySelector(".dr-stale").value) || 7,
    why: r.dataset.why || "",
    search_hint: r.dataset.hint || "",
    sources: JSON.parse(r.dataset.sources || "[]"),
  })).filter(d => d.driver_id);

  if (!cost_drivers.length) {
    err.textContent = "Add at least one driver — the watchlist is the point.";
    err.classList.remove("hidden");
    return;
  }
  const payload = {
    description: (profile && profile.description) || "",
    company: {
      name: document.getElementById("pf-name").value.trim(),
      industry: document.getElementById("pf-industry").value.trim(),
      reporting_currency: document.getElementById("pf-ccy").value.trim().toUpperCase(),
      budget_year: Number(document.getElementById("pf-year").value) || null,
    },
    product_lines: ((profile.proposal && profile.proposal.raw.product_lines) || profile.product_lines || []),
    markets: ((profile.proposal && profile.proposal.raw.markets) || profile.markets || []),
    cost_drivers,
  };
  const resp = await fetch("/api/profile", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    err.textContent = (body.detail && body.detail.error) || "Could not save the profile.";
    err.classList.remove("hidden");
    return;
  }
  profile = await resp.json();
  loadRules();
  location.replace("#/");
  route();
}

// ==========================================================================
// #/drivers — the conceptual heart
// ==========================================================================

function renderDrivers() {
  showOnly(driversView);
  refreshDrivers();
}

async function refreshDrivers() {
  let data;
  try { data = await (await fetch("/api/drivers")).json(); }
  catch { return; }
  if (driversView.classList.contains("hidden")) return;   // navigated away mid-fetch

  const s = data.summary;
  const bits = [`${s.total} drivers`, `${s.fresh} verified`];
  if (s.stale) bits.push(`${s.stale} stale`);
  if (s.never_verified) bits.push(`${s.never_verified} could not be verified`);
  if (s.drifted) bits.push(`${s.drifted} drifted`);
  document.getElementById("drivers-summary").textContent = bits.join(" · ");

  const body = document.getElementById("drivers-body");
  if (!data.drivers.length) {
    body.innerHTML = `<p class="empty">No drivers yet. Your budget rests on assumptions whether
      or not you write them down. Add the ones that matter, or
      <a href="#/setup">re-run setup</a> and let me propose a watchlist.</p>`;
    return;
  }
  const groups = {};
  data.drivers.forEach(d => (groups[d.category || "other"] ||= []).push(d));
  body.innerHTML = "";
  // Grouped by category: a CFO thinks "my ingredient basket", not "my
  // alphabetical list".
  Object.keys(groups).sort().forEach(cat => {
    const h = document.createElement("h3");
    h.className = "section-head";
    h.textContent = cat;
    body.appendChild(h);
    const grid = document.createElement("div");
    grid.className = "card-grid";
    groups[cat].forEach(d => grid.appendChild(driverCard(d)));
    body.appendChild(grid);
  });

  clearInterval(driverPollTimer);
  if (data.running) driverPollTimer = setInterval(refreshDrivers, 2000);
}

function fmtNum(v, digits = 2) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString(undefined,
    { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function driverCard(d) {
  const card = document.createElement("article");
  // Absence is the calm state: a fresh driver gets no badge, and only a stale
  // one gets the 4px --cta left rule (which stays inside the orange budget
  // precisely because it is four pixels).
  card.className = "card driver-card" + (d.verify_status !== "fresh" ? " is-stale" : "");

  // Direction is not sentiment. A rising ingredient price is bad; a rising
  // sales price is good. The delta is NEVER coloured by sign — it renders in
  // neutral --ink with tabular-nums and an explicit word derived from the
  // server-supplied adverse_direction.
  let drift = "—";
  if (d.drift_pct !== null && d.drift_pct !== undefined) {
    const word = d.adverse === true ? "adverse" : d.adverse === false ? "favourable" : "";
    drift = `<span class="num">${d.drift_pct > 0 ? "+" : ""}${fmtNum(d.drift_pct, 1)}%</span>` +
            (word ? ` <span class="drift-word">${word}</span>` : "");
  }
  const verified = d.last_verified_utc
    ? `Verified ${fmtWhen(Date.parse(d.last_verified_utc) / 1000)}`
    : "Never verified";

  card.innerHTML = `
    <div class="driver-head">
      <h4>${escapeHtml(d.name || d.driver_id)}</h4>
      <span class="chip">${escapeHtml(d.category || "other")}</span>
    </div>
    <dl class="driver-figures">
      <div><dt>Now</dt><dd class="num">${fmtNum(d.latest_value)} <span class="unit">${escapeHtml(d.unit || "")}</span></dd></div>
      <div><dt>In budget</dt><dd class="num">${fmtNum(d.locked_value)}</dd></div>
      <div><dt>Drift</dt><dd>${drift}</dd></div>
    </dl>
    <p class="driver-meta">${escapeHtml(verified)}${d.verify_status === "stale"
      ? ` · limit ${d.stale_after_days} days` : ""}</p>
    <div class="driver-source"></div>
    <div class="driver-actions">
      <button class="btn-ghost dv-verify"${d.verifying ? " disabled" : ""}>${
        d.verifying ? "Verifying…" : "Re-verify now"}</button>
      <button class="btn-ghost dv-history">History</button>
      <button class="btn-ghost dv-ask">Ask about this driver</button>
    </div>
    <div class="driver-history hidden"></div>`;

  const srcWrap = card.querySelector(".driver-source");
  if (d.latest_source_url) {
    const chip = sourceChip({ kind: "web", url: d.latest_source_url,
                              title: d.latest_source_url, accessed: d.latest_month });
    srcWrap.appendChild(chip);
  }
  card.querySelector(".dv-verify").addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Verifying…";
    await fetch(`/api/drivers/${d.driver_id}/verify`, { method: "POST" }).catch(() => {});
    clearInterval(driverPollTimer);
    driverPollTimer = setInterval(refreshDrivers, 2000);
  });
  // Load-once-then-toggle, copying wirePreview's shape — which is what keeps
  // route() at ~20 lines instead of growing a #/drivers/{id} route.
  card.querySelector(".dv-history").addEventListener("click", async () => {
    const panel = card.querySelector(".driver-history");
    if (panel.dataset.loaded) { panel.classList.toggle("hidden"); return; }
    panel.classList.remove("hidden");
    panel.textContent = "Loading…";
    try {
      const h = await (await fetch(`/api/drivers/${d.driver_id}/history`)).json();
      const rows = h.observations.slice(-14).reverse();
      panel.innerHTML = `<table class="mini-table"><thead><tr><th>Month</th><th>Price</th>
        <th>Source</th></tr></thead><tbody>${rows.map(o =>
        `<tr><td>${escapeHtml(o.month)}</td><td class="num">${fmtNum(o.price)}</td>
         <td>${escapeHtml(o.source || "")}</td></tr>`).join("")}</tbody></table>`;
      panel.dataset.loaded = "1";
    } catch { panel.textContent = "Could not load history."; }
  });
  // Pre-seed a prompt and let the lazy session path take over — copying
  // investigateAlert precisely.
  card.querySelector(".dv-ask").addEventListener("click", () => {
    pendingHomeMessage = `Tell me about ${d.name || d.driver_id}: where it stands now against ` +
      `what we locked into the budget, what it's worth to next year's EBITDA, and whether I ` +
      `should re-lock it.`;
    location.hash = "#/chat";
  });
  return card;
}

// ==========================================================================
// #/scenarios — read / compare / activate / delete only. No authoring form, ever.
// ==========================================================================

async function renderScenarios() {
  showOnly(scenariosView);
  const body = document.getElementById("scenarios-body");
  let data;
  try { data = await (await fetch("/api/scenarios")).json(); }
  catch { body.innerHTML = `<p class="empty">Could not load scenarios.</p>`; return; }

  if (!data.scenarios.length) {
    body.innerHTML = `<p class="empty">No scenarios yet. Ask for one in a budget revision —
      "what if freight stays where it is all year?" — and it'll be saved here.</p>`;
    return;
  }
  const grid = document.createElement("div");
  grid.className = "card-grid";
  data.scenarios.forEach(s => grid.appendChild(scenarioCard(s)));
  body.innerHTML = "";
  body.appendChild(grid);
}

function scenarioCard(s) {
  const card = document.createElement("article");
  card.className = "card scenario-card" + (s.active ? " is-active" : "");
  const eur = (v) => v === null || v === undefined ? "—"
    : `€${(v / 1e6).toLocaleString(undefined, { maximumFractionDigits: 2 })}M`;
  card.innerHTML = `
    <div class="driver-head">
      <h4>${escapeHtml(s.name || "Untitled")}</h4>
      ${s.active ? '<span class="chip is-active-chip">Active</span>' : ""}
    </div>
    ${s.note ? `<p class="page-sub">${escapeHtml(s.note)}</p>` : ""}
    <dl class="driver-figures">
      <div><dt>Revenue</dt><dd class="num">${eur(s.revenue_eur)}</dd></div>
      <div><dt>EBITDA</dt><dd class="num">${eur(s.ebitda_eur)}</dd></div>
      <div><dt>Margin</dt><dd class="num">${s.ebitda_margin_pct == null ? "—"
        : fmtNum(s.ebitda_margin_pct, 1) + "%"}</dd></div>
    </dl>
    <p class="driver-meta">${s.assumption_count} assumption${s.assumption_count === 1 ? "" : "s"}
      · updated ${escapeHtml(fmtWhen(s.updated_at))}</p>
    <div class="driver-actions">
      ${s.active ? "" : '<button class="btn-ghost sc-activate">Make active</button>'}
      <button class="btn-ghost sc-ask">Ask about this</button>
      ${s.active ? "" : '<button class="btn-ghost sc-delete">Delete</button>'}
    </div>`;
  const act = card.querySelector(".sc-activate");
  if (act) act.addEventListener("click", async () => {
    await fetch(`/api/scenarios/${s.id}/activate`, { method: "POST" });
    renderScenarios();
  });
  const del = card.querySelector(".sc-delete");
  if (del) del.addEventListener("click", async () => {
    await fetch(`/api/scenarios/${s.id}`, { method: "DELETE" });
    renderScenarios();
  });
  card.querySelector(".sc-ask").addEventListener("click", () => {
    pendingHomeMessage = `Walk me through the "${s.name}" scenario: what it assumes, ` +
      `what it does to next year's EBITDA, and how it compares to what we locked.`;
    location.hash = "#/chat";
  });
  return card;
}

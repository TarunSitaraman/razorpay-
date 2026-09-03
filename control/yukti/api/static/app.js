/* Niyama console.
 *
 * Renders the read-only JSON endpoints the API serves. Every code or
 * enum that reaches the screen is translated to plain English here:
 * a merchant should never see "npci_represent_cap" or "schedule_debit"
 * — they should read "retry limit reached" or "auto-debit".
 *
 * Hash-routed multi-view: #/ is the overview, #/cases the case list,
 * #/case/<id> the dossier, #/cycles the planning runs, #/evidence the
 * audit chain and frontier. A hash not starting with #/ is an overview
 * anchor — #hero, #stopping, #policy keep working and initNav's
 * scrollspy still runs on the overview.
 *
 * Every panel carries a monospace caption naming its source so a viewer
 * can trace any number back to the table or artefact that produced it.
 */

"use strict";

const $ = (id) => document.getElementById(id);

async function json(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

/* ---- Indian rupee formatting ---- */
function inr(paise, opts = {}) {
  const neg = paise < 0;
  const rupees = Math.round(Math.abs(paise) / 100);
  const s = String(rupees);
  let out;
  if (s.length <= 3) out = s;
  else {
    const last3 = s.slice(-3);
    const rest = s.slice(0, -3);
    out = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + last3;
  }
  const sign = neg ? "−" : (opts.signed ? "+" : "");
  return `${sign}₹${out}`;
}
const num = (n) => Number(n ?? 0).toLocaleString("en-IN");

/* Escape a value before it goes into markup.
 *
 * Most panels interpolate merchant-supplied strings — merchant names, decline
 * text, rule reasons — into innerHTML. On the reporting views the worst case is
 * a broken layout. On the approvals queue it is not: that view carries the only
 * write path in the console, so a merchant who names themselves with a script
 * tag would get script running in a reviewer's session next to an Approve
 * button. Anything that reaches markup from the database goes through here.
 */
function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

function tile(label, value, sub, valueCls) {
  const t = el("div", "tile");
  t.append(el("div", "label", label));
  t.append(el("div", "value" + (valueCls ? " " + valueCls : ""), value));
  if (sub) t.append(el("div", "sub", sub));
  return t;
}

/* Per-channel consent under DPDP is a JSONB object, not a string: absence of a
 * key means no consent, so `{}` reads as "none" rather than as missing data.
 * Interpolating the object directly is what put "[object Object]" on the case
 * file, on the one panel whose whole job is showing we were allowed to make
 * contact. */
function consentLine(consent) {
  if (!consent || typeof consent !== "object") return "none recorded";
  const granted = Object.keys(consent).filter((k) => consent[k]);
  if (!granted.length) return "none";
  return granted.map((k) => name(CHANNEL_NAME, k) || k).join(", ");
}

function caption(text) {
  const c = el("span", "caption");
  c.textContent = text;
  return c;
}

/* ---- Plain-English dictionaries ---- */

const SURFACE_NAME = {
  cart: "Abandoned carts",
  subscription_cycle: "Failed subscriptions",
  invoice: "Overdue invoices",
  order: "Failed payments",
};

const STOP_NAME = {
  lost_cause: "Won't pay anyway",
  open_promise_to_pay: "Already promised to pay",
  npci_represent_cap: "Retry limit reached",
  negative_expected_margin: "Not worth contacting",
  diminishing_returns: "Too late to help",
  contact_budget_spent: "Out of contact budget",
};

const RULE_NAME = {
  RBI_AFA_LIMIT: "RBI auto-debit limit",
  RBI_PREDEBIT_24H: "RBI 24-hour notice",
  NPCI_REPRESENT_CAP: "Retry attempt cap",
  TRAI_QUIET_HOURS: "Quiet hours (9am–9pm)",
  TRAI_DLT_TEMPLATE: "Registered message template",
  DPDP_CONSENT: "Customer consent",
  MERCHANT_BLACKOUT: "Merchant blackout period",
  MERCHANT_DISCOUNT_STACKING: "No discount stacking",
  MERCHANT_CONTACT_CAP: "Contact cap",
  MERCHANT_MIN_VALUE: "Below minimum value",
  MERCHANT_DISCOUNT_CEILING: "Discount ceiling",
  MERCHANT_ALLOWED_CHANNEL: "Channel not allowed",
  MERCHANT_APPROVAL_THRESHOLD: "Needs human approval",
  lost_cause: "Won't pay anyway",
  npci_represent_cap: "Retry limit reached",
  open_promise_to_pay: "Already promised to pay",
  negative_expected_margin: "Not worth contacting",
};

const PACK_NAME = {
  regulatory: "Legal requirements",
  merchant: "Your settings",
  stopping: "Our own judgement",
};

const ACTION_NAME = {
  message: "Message",
  silent_retry: "Silent retry",
  schedule_debit: "Auto-debit",
  discount_offer: "Discount",
  payment_link: "Payment link",
  voice_call: "Call",
  suppress: "Hold off",
};

const STATE_NAME = {
  open: "Open",
  planning: "Being planned",
  scheduled: "Scheduled",
  acting: "Acting",
  awaiting_outcome: "Awaiting outcome",
  held_out: "Held out",
  stopped: "Stopped",
  escalated: "Sent to a human",
  recovered: "Recovered",
  closed: "Closed",
};

const CHANNEL_NAME = { whatsapp: "WhatsApp", sms: "SMS", email: "Email", voice: "Voice" };

const ARM_NAME = {
  Y: "Niyama (uplift)",
  B0: "Do nothing",
  B1: "Fixed schedule",
  B2: "Rules by failure type",
  B3: "Most likely to pay",
  B4: "Retry only",
};

const name = (dict, key) => (dict[key] ?? (key || "").replace(/_/g, " "));

/* ---- Bars ---- */
function bars(container, rows, { format = inr } = {}) {
  container.innerHTML = "";
  if (!rows.length) { container.append(el("p", "empty", "Nothing here yet.")); return; }
  const max = Math.max(...rows.map((r) => Math.abs(r.value)), 1);
  for (const r of rows) {
    const row = el("div", "bar-row");
    row.append(el("div", "name", r.name));
    const track = el("div", "bar-track");
    const fill = el("div", "bar-fill " + (r.fill || ""));
    fill.style.width = `${Math.max(2, (Math.abs(r.value) / max) * 100)}%`;
    track.append(fill);
    row.append(track);
    row.append(el("div", "val", r.display ?? format(r.value)));
    if (r.title) row.title = r.title;
    container.append(row);
  }
}

function divergingBars(container, rows, { axisFormat = inr } = {}) {
  container.innerHTML = "";
  if (!rows.length) { container.append(el("p", "empty", "Nothing here yet.")); return; }
  const lo = Math.min(0, ...rows.map((r) => r.value));
  const hi = Math.max(0, ...rows.map((r) => r.value));
  const span = (hi - lo) || 1;
  const zeroPct = ((0 - lo) / span) * 100;
  for (const r of rows) {
    const row = el("div", "bar-row");
    row.append(el("div", "name", r.name));
    const track = el("div", "bar-track");
    const fill = el("div", "bar-fill " + (r.fill || ""));
    const valuePct = ((r.value - lo) / span) * 100;
    const left = Math.min(zeroPct, valuePct);
    fill.style.left = `${left}%`;
    fill.style.width = `${Math.max(0.4, Math.abs(valuePct - zeroPct))}%`;
    const rule = el("div", "zero-rule");
    rule.style.left = `${zeroPct}%`;
    track.append(fill, rule);
    row.append(track);
    row.append(el("div", "val", r.display ?? axisFormat(r.value)));
    if (r.title) row.title = r.title;
    container.append(row);
  }
  const axis = el("div", "axis");
  axis.append(el("div", ""));
  const ticks = el("div", "ticks");
  for (const [pct, label] of [[0, axisFormat(lo)], [zeroPct, "0"], [100, axisFormat(hi)]]) {
    const t = el("div", "tick", label);
    t.style.left = `${pct}%`;
    ticks.append(t);
  }
  axis.append(ticks);
  axis.append(el("div", "spacer"));
  container.append(axis);
}

/* ---- State ---- */
let merchantId = null;
let merchantNames = {};
let liftCache = null;
let decisionView = "all";
let decisionRows = [];
const q = (extra = {}) => {
  const p = new URLSearchParams();
  if (merchantId) p.set("merchant_id", merchantId);
  for (const [k, v] of Object.entries(extra)) if (v !== undefined && v !== null) p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
};

/* ---- Theme ---- */
function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "light" || mode === "dark") root.dataset.theme = mode;
  else delete root.dataset.theme;
  localStorage.setItem("niyama-theme", mode);
}
function initTheme() {
  const saved = localStorage.getItem("niyama-theme") || "auto";
  applyTheme(saved);
  $("theme").addEventListener("click", () => {
    const cur = localStorage.getItem("niyama-theme") || "auto";
    applyTheme(cur === "auto" ? "light" : cur === "light" ? "dark" : "auto");
  });
}

/* ---- Source captions ---- */
function addCaptions() {
  const map = {
    hero: "lift — artifacts/eval-report.json",
    risk: "obligation · payment_attempt",
    lift: "artifacts/eval-report.json",
    stopping: "agent_decision · policy_evaluation",
    policy: "policy_evaluation · regpack",
    decisions: "agent_decision · recovery_action",
  };
  for (const [id, text] of Object.entries(map)) {
    const sec = document.getElementById(id);
    if (!sec) continue;
    const kicker = sec.querySelector(".kicker");
    if (kicker) {
      const cap = caption(text);
      cap.style.marginLeft = "0.5rem";
      cap.style.fontSize = "0.7rem";
      cap.style.color = "var(--ink-mut)";
      kicker.append(cap);
    }
  }
}

/* ---- Rail ---- */
let syncNav = () => {};
let markNav = () => {};
function initNav() {
  const links = [...document.querySelectorAll(".rail-nav a")];
  const sections = links.map((a) => document.getElementById(a.dataset.target)).filter(Boolean);
  const mark = (id) => links.forEach((a) => a.classList.toggle("on", a.dataset.target === id));
  markNav = mark;
  const READING_LINE = 0.25;
  let queued = false;
  const update = () => {
    queued = false;
    // Off the overview every overview section is display:none, so every rect is
    // zero and the reading-line test would silently elect the first one — which
    // is exactly the bug that left the rail stuck on "01 Result" while the case
    // list was on screen. The router owns the mark in that case.
    if (currentView !== "overview") return;
    const line = window.innerHeight * READING_LINE;
    let current = sections[0];
    for (const s of sections) {
      const r = s.getBoundingClientRect();
      if (r.top <= line && r.bottom > line) { current = s; break; }
      if (r.top <= line) current = s;
    }
    if (window.scrollY < 40) current = sections[0];
    if (window.scrollY + window.innerHeight >= document.body.scrollHeight - 4) current = sections[sections.length - 1];
    if (current) mark(current.id);
  };
  const onScroll = () => { if (document.hidden) { update(); return; } if (queued) return; queued = true; requestAnimationFrame(update); };
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll);
  syncNav = onScroll;
  update();
}

function refreshApprovalBadge(n) {
  const b = $("approvals-badge");
  if (!b) return;
  if (n === undefined) {
    json(`/approvals${q()}`).then((rows) => refreshApprovalBadge(rows.length))
                            .catch(() => {});
    return;
  }
  b.textContent = String(n);
  b.hidden = n === 0;
}

function renderStamp() {
  const stamp = $("rail-stamp");
  if (!stamp || !liftCache) return;
  if (liftCache.source === "demo-light") { stamp.textContent = "demo-light"; return; }
  const asOf = (liftCache.as_of || "").slice(0, 10);
  stamp.textContent = asOf || "—";   // the top bar supplies the "as of" label
}

/* ---- Router ---- */
let currentView = "overview";
let pollTimer = null;

function navigate(hash) {
  const route = (hash || "#/").replace(/^#/, "").replace(/^\//, "");
  if (route === "" || route === "/") { showView("overview"); return; }
  if (route.startsWith("case/")) { showCaseDetail(route.slice(5)); return; }
  if (route === "approvals") { showApprovals(); return; }
  if (route === "cases") { showView("cases"); showCasesList(); return; }
  if (route === "cycles") { showView("cycles"); showCycles(); return; }
  if (route === "evidence") { showView("evidence"); showEvidence(); return; }
  showView("overview");
}

function startPoller() { stopPoller(); pollTimer = setInterval(() => {
    if (currentView === "overview") Overview.mount();
    refreshApprovalBadge();
  }, 15000); }
function stopPoller() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

function initRouter() { window.addEventListener("hashchange", () => navigate(location.hash)); navigate(location.hash); startPoller(); }

function showView(name) {
  // Anchor links in the rail (#hero, #stopping, ...) re-enter the router while
  // already on the overview. Treating that as a view change would scroll the
  // page back to the top and fight the anchor it was asked to jump to.
  const changed = currentView !== name;
  currentView = name;
  const overviewIds = ["hero", "risk", "lift", "stopping", "policy", "decisions"];
  const viewIds = ["approvals", "cases", "case", "cycles", "evidence"];
  const all = [...overviewIds, ...viewIds];
  for (const id of all) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (viewIds.includes(id)) { el.style.display = id === name ? "" : "none"; }
    else { el.style.display = name === "overview" || !name ? "" : "none"; }
    // `section + section` draws the rule between stacked overview sections. A
    // view shown on its own is still the DOM's second section, so without this
    // it opens on a hairline and 2.6rem of nothing.
    el.classList.toggle("solo", viewIds.includes(id) && id === name);
  }
  if (changed) window.scrollTo(0, 0);
  // The case file is reached from the list, not the rail; keep Cases lit so the
  // reader can see where they descended from. On the overview the scrollspy is
  // the authority, so hand the mark straight back to it.
  if (name === "overview") syncNav();
  else markNav(name === "case" ? "cases" : name);
}

/* ---- Overview object ---- */
const Overview = {
  mount() {
    const done = Promise.allSettled([
      loadSnapshot().catch(() => { const b = $("snapshot-tiles"); if (b) { b.innerHTML = ""; b.append(el("p", "empty", "This panel reads the live database. Start the stack with make up && make services, or run make demo.")); } }),
      loadRisk().catch(() => { const b = $("risk-bars"); if (b) { b.innerHTML = ""; b.append(el("p", "empty", "This panel reads the live database. Start the stack with make up && make services, or run make demo.")); } }),
      loadStopping().catch(() => { const b = $("stopping-body"); if (b) { b.innerHTML = ""; b.append(el("p", "empty", "This panel reads the live database. Start the stack with make up && make services, or run make demo.")); } }),
      loadPolicy().catch(() => { const b = $("policy-body"); if (b) { b.innerHTML = ""; b.append(el("p", "empty", "This panel reads the live database. Start the stack with make up && make services, or run make demo.")); } }),
      loadDecisions().catch(() => { const b = $("decisions-body"); if (b) { b.innerHTML = ""; b.append(el("p", "empty", "This panel reads the live database. Start the stack with make up && make services, or run make demo.")); } }),
    ]);
    renderStamp();
    syncNav();
  },
  unmount() { stopPoller(); },
};

/* ---- Pill ---- */
function pill(v) {
  const map = { allow: ["allow", "✓ allowed"], block: ["block", "✕ blocked"], escalate: ["escalate", "▲ escalated"] };
  const [cls, label] = map[v] || ["neutral", v];
  return `<span class="pill ${cls}">${label}</span>`;
}

/* ---- 1. Hero + snapshot ---- */
async function loadLift() {
  try { liftCache = await json("/metrics/lift"); } catch { liftCache = null; }
  window._liftCache = liftCache;
  renderHero();
  renderLiftComparison();
}

function renderHero() {
  document.querySelector(".hero-scope")?.remove();
  const y = liftCache?.arms?.find((a) => a.key === "Y");
  if (!y) { $("hero-big").textContent = "No evaluation yet"; $("hero-receipt").textContent = "Run the evaluation to measure the net margin this system actually created."; return; }
  const light = liftCache.source === "demo-light";
  if (light) {
    $("hero-big").textContent = inr(y.contact_incremental_paise, { signed: true });
    $("hero-receipt").innerHTML = `In the service-free demo world, the same <strong>${num(y.contacts)}</strong> contacts earned <strong>${inr(y.contact_incremental_paise)}</strong> more than retrying alone. Per 1,000 contacted that is <strong>${inr(y.contact_per_1k.point, { signed: true })}</strong>, 95% CI ${inr(y.contact_per_1k.low, { signed: true })} to ${inr(y.contact_per_1k.high, { signed: true })} — which contains zero at this size. The ordering across strategies is the claim here; the rupee figure is not.`;
  } else {
    $("hero-big").textContent = inr(y.net_incremental_paise, { signed: true });
    const caused = y.recovered_cases - y.would_have_recovered_anyway;
    $("hero-receipt").innerHTML = `Of <strong>${num(y.recovered_cases)}</strong> payments recovered, <strong>${num(y.would_have_recovered_anyway)}</strong> would have come back on their own. We caused <strong>${num(caused)}</strong> — worth <strong>${inr(y.net_incremental_paise)}</strong> after fees, discounts and channel costs.`;
  }
  const contact = y.contact_per_1k;
  const clearlyPositive = (a) => a.contact_per_1k && a.contact_per_1k.excludes_zero && a.contact_per_1k.point > 0;
  const yWinsAlone = clearlyPositive(y) && !(liftCache.arms || []).some((a) => a.key !== "Y" && clearlyPositive(a));
  const on = merchantNames[liftCache.merchant_id] || "one merchant's book";
  const asOf = (liftCache.as_of || "").slice(0, 10);
  const scope = el("div", "hero-scope", light
    ? `Measured in the <strong>service-free demo world</strong> — no database, generated and graded in process. Run <code>make demo</code> for a merchant's book.`
    : `Measured on <strong>${on}</strong> — ${num(liftCache.cases)} cases` + (asOf ? `, as of ${asOf}` : "") + (merchantId && merchantId !== liftCache.merchant_id ? ` · the panels below are ${merchantNames[merchantId] || "the selected merchant"}` : ""));
  $("hero-receipt").after(scope);
  renderStamp();
  if (yWinsAlone) { const note = $("hero-note"); note.textContent = "The only strategy that clearly earned money from its contacts"; note.hidden = false; } else { $("hero-note").hidden = true; }
}

async function loadSnapshot() {
  const box = $("snapshot-tiles");
  const [risk, notChased] = await Promise.all([
    json(`/metrics/revenue-at-risk${q()}`), json(`/metrics/not-chased${q()}`),
  ]);
  const y = liftCache?.arms?.find((a) => a.key === "Y");
  box.innerHTML = "";
  box.append(tile("Money at risk", inr(risk.total_paise), `${num(risk.total_cases)} open obligations`));
  const light = liftCache?.source === "demo-light";
  const elsewhere = !light && merchantId && liftCache && merchantId !== liftCache.merchant_id;
  box.append(tile("Net margin created", y ? inr(y.net_incremental_paise, { signed: true }) : "—", light ? "service-free demo world, not this book" : elsewhere ? `measured on ${merchantNames[liftCache.merchant_id] || "another merchant"}, not this one` : "measured against a holdout, not assumed", y && y.net_incremental_paise > 0 && !elsewhere && !light ? "pos" : ""));
  box.append(tile("Deliberately not chased", inr(notChased.stopped_total_paise), `${num(notChased.stopped_by_rule.reduce((s, r) => s + r.cases, 0))} cases spared for a named reason`));
}

/* ---- 2. Risk ---- */
async function loadRisk() {
  const d = await json(`/metrics/revenue-at-risk${q()}`);
  bars($("risk-bars"), (d.by_surface || []).map((s) => ({ name: SURFACE_NAME[s.kind] || s.kind, value: s.amount_paise, display: `${inr(s.amount_paise)}  ·  <span class="dim">${num(s.cases)} cases</span>` })));
}

/* ---- 2b. Lift comparison ---- */
function renderLiftComparison() {
  const box = $("lift-body"); box.innerHTML = "";
  const d = liftCache;
  if (!d) { box.innerHTML = '<p class="empty">Run the evaluation to see this comparison.</p>'; return; }
  const arms = (d.arms || []).filter((a) => a.acts && a.key !== "B4");
  if (merchantId && merchantId !== d.merchant_id) box.append(el("p", "kicker", `This comparison is <strong>${merchantNames[d.merchant_id] || "another merchant"}</strong>'s book — the evaluation runs on one merchant at a time.`));
  box.append(el("div", "legend", `<span><span class="swatch" style="background:var(--good)"></span> Earned money</span><span><span class="swatch" style="background:var(--bad)"></span> Lost money</span><span>Bars grow from zero — left is a loss.</span>`));
  const cont = el("div", "bars diverge"); box.append(cont);
  const ordered = [...arms].sort((a, b) => a.contact_incremental_paise - b.contact_incremental_paise);
  divergingBars(cont, ordered.map((a) => ({ name: `<b>${ARM_NAME[a.key] || a.label}</b>`, value: a.contact_incremental_paise, fill: a.key === "Y" ? (a.contact_incremental_paise > 0 ? "good" : "bad") : (a.contact_incremental_paise > 0 ? "muted" : "bad"), display: `${inr(a.contact_incremental_paise, { signed: true })}` + (a.contact_per_1k ? `  ·  <span class="dim">${inr(a.contact_per_1k.point, { signed: true })}/1k</span>` : ""), title: a.contact_per_1k ? `${ARM_NAME[a.key] || a.label} — 95% CI per 1,000: ${inr(a.contact_per_1k.low, { signed: true })} to ${inr(a.contact_per_1k.high, { signed: true })}` : undefined })), { axisFormat: (v) => inr(v, { signed: true }) });
  const y = arms.find((a) => a.key === "Y");
  const bestRival = arms.filter((a) => a.key !== "Y").sort((a, b) => b.contact_incremental_paise - a.contact_incremental_paise)[0];
  if (y && bestRival) { const gap = y.contact_incremental_paise - bestRival.contact_incremental_paise; box.append(el("div", "callout good", `<strong>The choice of who to contact was worth ${inr(gap, { signed: true })} more</strong> than the best alternative. Every other strategy <em>lost</em> money on the customers it contacted.`)); }
  const h = el("h3", null, "Honesty, spelled out"); box.append(h);
  const power = y?.cases_needed_for_power;
  box.append(el("p", "kicker", `The ordering above is stable; the interval around it is narrower than a live deployment would see. It comes from a paired comparison against a known counterfactual, so it captures how much customers differ from each other — not the uncertainty about what an untreated customer would have done. ` + (power ? `At this book's size the effect is small next to how much a single large payment swings: separating these strategies from live data alone would need about ${num(power)} cases.` : "") + ` That is a property of the data, and no estimator fixes it.`));
}

/* ---- 3. Stopping rules ---- */
async function loadStopping() {
  const box = $("stopping-body");
  const d = await json(`/metrics/not-chased${q()}`);
  box.innerHTML = "";
  const tiles = el("div", "tiles");
  tiles.append(tile("Walked away from", inr(d.stopped_total_paise), "cases stopped with a named reason"));
  tiles.append(tile("Left for tomorrow", inr(d.considered_not_funded_paise), `${num(d.considered_not_funded_cases)} cases still open — budget went elsewhere`));
  box.append(tiles);
  const card = el("div", "panel");
  const legend = el("div", "legend", `<span><span class="swatch" style="background:var(--cool)"></span> money not chased, by reason</span>`);
  card.append(legend);
  const b = el("div", "bars"); card.append(b);
  const rows = (d.stopped_by_rule || []).map((r) => ({ name: name(STOP_NAME, r.stop_reason), value: r.amount_paise, display: `${inr(r.amount_paise)}  ·  <span class="dim">${num(r.cases)} cases</span>` }));
  bars(b, rows);
  box.append(card);
}

/* ---- 4. Guardrails ---- */
async function loadPolicy() {
  const outer = $("policy-body");
  const rows = await json(`/metrics/policy${q()}`);
  outer.innerHTML = "";
  const box = el("div", "panel"); outer.append(box);
  if (!rows.length) { box.append(el("p", "empty", "No blocks or escalations recorded.")); return; }
  const order = ["regulatory", "merchant", "stopping"];
  const groups = {};
  for (const r of rows) (groups[r.pack] ??= []).push(r);
  for (const pack of [...order.filter((p) => groups[p]), ...Object.keys(groups).filter((p) => !order.includes(p))]) {
    const head = el("div", "group-head", `${PACK_NAME[pack] || pack} <span class="count">${groups[pack].length} rule${groups[pack].length === 1 ? "" : "s"}</span>`);
    box.append(head);
    const table = el("table");
    table.innerHTML = `<thead><tr><th>Rule</th><th>Outcome</th><th style="text-align:right">Cases</th><th style="text-align:right">Money involved</th></tr></thead>`;
    const tb = el("tbody");
    for (const r of groups[pack]) {
      const tr = el("tr");
      tr.innerHTML = `<td>${name(RULE_NAME, r.rule_id)}<span class="rule-id">${r.rule_id}</span></td><td>${pill(r.verdict)}</td><td class="num">${num(r.n)}</td><td class="num">${inr(r.amount_paise)}</td>`;
      tb.append(tr);
    }
    table.append(tb); box.append(table);
  }
}

/* ---- 5. Recent activity ---- */
function rupeeify(text) {
  if (!text) return "";
  let out = String(text).replace(/(-?\d+)\s*paise\b/g,
                                 (_, p) => inr(Number(p), { signed: true }));
  // Legacy rows. `policy_evaluation.reason` is frozen at evaluation time, and
  // the approval-threshold rule used to phrase its amounts as bare paise
  // integers. Rows written before that was fixed still say "amount 39712659",
  // which is not a number a reviewer can check, so the display layer restates
  // them. New evaluations arrive already formatted and match nothing here.
  out = out.replace(/\b(amount|threshold)\s+(\d{5,})\b/g,
                    (_, word, p) => `${word} ${inr(Number(p))}`);
  return out;
}

const DECISION_VIEWS = {
  all: { label: "Everything", match: () => true },
  overruled: { label: "A rule overruled us", match: (d) => (d.alternatives_rejected || []).some((a) => a.rejected_by === "POLICY") },
  contacted: { label: "Customer contacted", match: (d) => d.channel && d.channel !== "none" },
};

async function loadDecisions() {
  decisionRows = await json(`/decisions${q({ limit: 250 })}`);
  renderDecisions();
}

function renderDecisions() {
  const all = decisionRows;
  const box = $("decisions-body"); box.innerHTML = "";
  if (!all.length) { box.append(el("p", "empty", "No decisions yet.")); return; }
  const chips = el("div", "chips");
  for (const [key, view] of Object.entries(DECISION_VIEWS)) {
    const n = all.filter(view.match).length;
    const c = el("button", "chip" + (key === decisionView ? " on" : ""), `${view.label} <span class="count">${num(n)}</span>`);
    c.disabled = n === 0;
    c.addEventListener("click", () => { decisionView = key; renderDecisions(); });
    chips.append(c);
  }
  box.append(chips);
  const rows = all.filter(DECISION_VIEWS[decisionView].match).slice(0, 25);
  if (!rows.length) { box.append(el("p", "empty", "Nothing matched this view in the last 250 decisions.")); return; }
  const acted = rows.filter((d) => d.action_kind !== "suppress");
  const held = rows.filter((d) => d.action_kind === "suppress");
  if (held.length) box.append(el("p", "kicker", `${num(acted.length)} action${acted.length === 1 ? "" : "s"} taken, ${num(held.length)} held back — actions shown first.`));
  const card = el("div", "panel");
  const table = el("table");
  table.innerHTML = `<thead><tr><th>Action</th><th>Outcome</th><th style="text-align:right">Expected margin</th><th style="text-align:right">Amount owed</th><th>Why</th></tr></thead>`;
  const tb = el("tbody");
  for (const d of rows) {
    const alts = (d.alternatives_rejected || []).slice(0, 3).map((a) => {
      const byRule = a.rejected_by && a.rejected_by !== "ALLOCATOR";
      const why = byRule ? `<strong>blocked by ${name(RULE_NAME, a.blocked_by || a.rejected_by)}</strong>` : "not chosen — lower margin";
      const via = a.channel && a.channel !== "none" ? ` via ${name(CHANNEL_NAME, a.channel)}` : "";
      return `<div class="rejected">✕ ${name(ACTION_NAME, a.action)}${via} — ${why}</div>`;
    }).join("");
    const tr = el("tr");
    tr.innerHTML = `<td><strong>${name(ACTION_NAME, d.action_kind)}</strong>${d.channel && d.channel !== "none" ? `<span class="tag">${name(CHANNEL_NAME, d.channel)}</span>` : ""}</td><td>${pill(d.policy_verdict)}</td><td class="num">${d.margin_display ?? "—"}</td><td class="num">${d.display ?? ""}</td><td>${d.reason ?? ""}${alts}</td>`;
    tb.append(tr);
  }
  table.append(tb); card.append(table); box.append(card);
}

/* ---- DB panels ---- */
const DB_PANELS = [
  [loadSnapshot, "snapshot-tiles"],
  [loadRisk, "risk-bars"],
  [loadStopping, "stopping-body"],
  [loadPolicy, "policy-body"],
  [loadDecisions, "decisions-body"],
];

async function refresh() {
  const done = Promise.allSettled([loadLift(), ...DB_PANELS.map(([load, target]) => load().catch(() => { const box = $(target); if (!box) return; box.innerHTML = ""; box.append(el("p", "empty", "This panel reads the live database. Start the stack with make up && make services, or run make demo for the full walkthrough.")); })), ]);
  await done;
  syncNav();
}

/* ---- Cases view ---- */
async function showCasesList() {
  showView("cases");
  const box = $("cases-body");
  if (!box) return;
  box.innerHTML = '<p class="empty">Loading cases…</p>';
  try {
    const data = await json(`/cases${q({ limit: 200 })}`);
    renderCaseList(box, data);
  } catch (e) { box.innerHTML = `<p class="empty">Could not load cases: ${e.message}</p>`; }
}

function renderCaseList(box, cases) {
  box.innerHTML = "";
  if (!cases.length) { box.append(el("p", "empty", "No cases found.")); return; }
  const table = el("table");
  table.innerHTML = `<thead><tr><th>Case</th><th>State</th><th>Arm</th><th>Amount</th><th>Stop reason</th><th>Channel</th></tr></thead>`;
  const tb = el("tbody");
  for (const c of cases.slice(0, 100)) {
    tb.append(el("tr", null, `<td><a href="#/case/${c.id}" style="color:var(--accent)">${c.id}</a></td><td>${c.state}</td><td>${c.arm || "—"}</td><td class="num">${inr(c.amount_paise)}</td><td>${name(STOP_NAME, c.stop_reason)}</td><td>${name(CHANNEL_NAME, c.channel) || "—"}</td>`));
  }
  table.append(tb); box.append(table);
  box.append(el("div", "caption", `${cases.length} cases — click a row to open the case file`));
}

async function showCaseDetail(caseId) {
  showView("case");
  const box = $("case-body");
  if (!box) return;
  box.innerHTML = '<p class="empty">Loading case file…</p>';
  try {
    const data = await json(`/cases/${encodeURIComponent(caseId)}${q()}`);
    renderCaseFile(box, data);
  } catch (e) { box.innerHTML = `<p class="empty">Could not load case: ${e.message}</p>`; }
}

function renderCaseFile(box, data) {
  box.innerHTML = "";
  const { case: c, attempts, decisions, actions, outcomes, audit, ground_truth, collisions, promises } = data;

  /* Header */
  const header = el("div", "panel dossier-header");
  header.innerHTML = `<div class="dossier-title">Case <strong>${c.id}</strong> · ${c.state} · ${name(ARM_NAME, c.arm)} · ${merchantNames[c.merchant_id] || c.merchant_id || ""}</div><div class="dossier-sub">${name(SURFACE_NAME, c.obligation_kind)} · ${inr(c.amount_paise)} · stop reason: ${name(STOP_NAME, c.stop_reason) || c.stop_reason || "—"}</div>`;
  box.append(header);

  /* The money */
  const moneyPanel = el("div", "panel");
  moneyPanel.append(el("h3", null, "The money <span class=\"caption\">payment_attempt</span>"));
  moneyPanel.append(el("div", "sub", `Obligation ${c.obligation_id} · ${inr(c.amount_paise)}`));
  if (attempts.length) {
    const table = el("table");
    table.innerHTML = `<thead><tr><th>Rail</th><th>Issuer</th><th>PSP</th><th>Code</th><th>Text</th><th>Amount</th><th>Ours</th></tr></thead>`;
    const tb = el("tbody");
    for (const a of attempts) { const spec = a.decline || {}; tb.append(el("tr", null, `<td>${a.rail || "—"}</td><td>${a.issuer || "—"}</td><td>${a.psp || "—"}</td><td>${a.decline_code || "—"}</td><td>${a.decline_text || ""}</td><td class="num">${inr(a.amount_paise)}</td><td>${a.ours ? "✓" : "—"}</td>`)); }
    table.append(tb); moneyPanel.append(table);
  }
  box.append(moneyPanel);

  /* The customer */
  const custPanel = el("div", "panel");
  custPanel.append(el("h3", null, "The customer <span class=\"caption\">customer</span>"));
  custPanel.append(el("div", "sub", `Consent: ${consentLine(c.consent)}${c.opted_out_at ? " · opted out " + String(c.opted_out_at).slice(0, 10) : ""} · LTV: ${c.ltv_band || "—"} · Tenure: ${c.tenure_days || "—"}d · Prefers: ${name(CHANNEL_NAME, c.preferred_channel) || "—"}`));
  box.append(custPanel);

  /* Collisions */
  if (collisions && collisions.length) {
    const colPanel = el("div", "panel");
    colPanel.append(el("h3", null, "Collisions <span class=\"caption\">obligation</span>"));
    colPanel.append(el("div", "sub", "Other open obligations on other surfaces"));
    const table = el("table");
    table.innerHTML = `<thead><tr><th>Obligation</th><th>Kind</th><th>Amount</th><th>Case state</th></tr></thead>`;
    const tb = el("tbody");
    for (const o of collisions.slice(0, 10)) tb.append(el("tr", null, `<td>${o.id}</td><td>${name(SURFACE_NAME, o.kind) || o.kind}</td><td class="num">${inr(o.amount_paise)}</td><td>${o.case_state || "—"}</td>`));
    table.append(tb); colPanel.append(table); box.append(colPanel);
  }

  /* Everything considered */
  if (decisions.length) {
    const decPanel = el("div", "panel");
    decPanel.append(el("h3", null, "Everything considered <span class=\"caption\">agent_decision.alternatives_rejected</span>"));
    for (const d of decisions) {
      const chosen = d.action_kind !== "suppress";
      const tagCls = chosen ? "pill allow" : "pill neutral";
      const tag = chosen ? "CHOSEN" : "SUPPRESSED";
      let altsHtml = "";
      const alts = d.alternatives_rejected || [];
      if (alts.length) { altsHtml = '<div class="rejected-list">' + alts.map(a => { const byRule = a.rejected_by && a.rejected_by !== "ALLOCATOR"; const why = byRule ? `<strong>blocked by ${name(RULE_NAME, a.blocked_by || a.rejected_by)}</strong>` : "not chosen — lower margin"; const via = a.channel && a.channel !== "none" ? ` via ${name(CHANNEL_NAME, a.channel)}` : ""; return `<div class="rejected">✕ ${name(ACTION_NAME, a.action)}${via} — ${why}</div>`; }).join("") + '</div>'; }
      decPanel.append(el("div", null, `<div><strong>${name(ACTION_NAME, d.action_kind)}</strong> <span class="${tagCls}">${tag}</span> ${name(CHANNEL_NAME, d.channel) || ""} — ${inr(d.expected_incr_margin_paise || 0)} expected margin</div>${altsHtml}${d.rules_not_applicable ? `<div class="caption">n/a: ${d.rules_not_applicable.join(", ")}</div>` : ""}`));
    }
    if (decisions.length > 6) box.append(el("div", "caption", `Showing 6 of ${decisions.length} decisions`));
    box.append(decPanel);
  }

  /* Every rule that ran */
  if (decisions.length) {
    const rulePanel = el("div", "panel");
    rulePanel.append(el("h3", null, "Every rule that ran <span class=\"caption\">policy_evaluation · engine.all_rule_ids()</span>"));
    const packs = {};
    for (const d of decisions) for (const e of (d.policy_evaluations || [])) { packs[e.pack] = packs[e.pack] || {}; packs[e.pack][e.rule_id] = packs[e.pack][e.rule_id] || { allow: 0, other: 0 }; if (e.verdict === "allow") packs[e.pack][e.rule_id].allow++; else packs[e.pack][e.rule_id].other++; }
    for (const [pack, rules] of Object.entries(packs)) { rulePanel.append(el("div", "sub", `${PACK_NAME[pack] || pack}`)); for (const [rid, counts] of Object.entries(rules)) rulePanel.append(el("div", null, `${name(RULE_NAME, rid) || rid}: ${counts.allow} allow, ${counts.other} blocked/escalated`)); }
    box.append(rulePanel);
  }

  /* The allocator */
  const allocPanel = el("div", "panel");
  allocPanel.append(el("h3", null, "The allocator <span class=\"caption\">pipeline.py · Allocation.lambda_contact</span>"));
  allocPanel.append(el("div", "sub", `Funded: ${actions.filter(a => a.status === "dispatched" || !a.status).length} · Suppressed: ${actions.filter(a => a.status === "suppressed").length}`));
  box.append(allocPanel);

  /* Dispatch */
  if (actions.length) {
    const dispPanel = el("div", "panel");
    dispPanel.append(el("h3", null, "Dispatch <span class=\"caption\">dispatcher.fingerprint</span>"));
    for (const a of actions) dispPanel.append(el("div", "sub", `idem_<hash> · ${name(CHANNEL_NAME, a.channel) || a.kind} · ${a.status || "pending"}`));
    box.append(dispPanel);
  }

  /* Outcome */
  if (outcomes.length) {
    const outPanel = el("div", "panel");
    outPanel.append(el("h3", null, "Outcome <span class=\"caption\">recovery_outcome</span>"));
    for (const o of outcomes) outPanel.append(el("div", "sub", `${o.outcome} · ${inr(o.recovered_paise || 0)}${o.action_id ? "" : " (organic)"}`));
    box.append(outPanel);
  }

  /* Audit */
  if (audit.length) {
    const audPanel = el("div", "panel");
    audPanel.append(el("h3", null, "Audit <span class=\"caption\">audit_event</span>"));
    for (const a of audit.slice(0, 20)) { const prev = (a.prev_hash || "").slice(0, 12); const h = (a.hash || "").slice(0, 12); audPanel.append(el("div", "sub", `#${a.id} ${a.action} · ${prev}→${h} · ${a.actor} · ${a.created_at?.slice(0, 10) || ""}`)); }
    if (audit.length > 20) audPanel.append(el("div", "caption", `${audit.length} audit rows — see #/evidence`));
    box.append(audPanel);
  }

  /* Ground truth */
  if (ground_truth && ground_truth.archetype) {
    const gtPanel = el("div", "panel");
    gtPanel.style.border = "2px dashed var(--ink-mut)";
    gtPanel.append(el("h3", null, 'not visible to any model <span class="caption">features.FORBIDDEN</span>'));
    gtPanel.append(el("div", "sub", `Archetype: ${ground_truth.archetype} · Forbidden: ${(ground_truth.withheld_from_models || []).join(", ")}`));
    gtPanel.append(el("div", "caption", "enforced by yukti.intelligence.features.FeatureLeakage — see tests/integration/test_feature_leakage.py"));
    box.append(gtPanel);
  }
}

/* ---- Cycles view ---- */
async function showCycles() {
  showView("cycles");
  const box = $("cycles-body");
  if (!box) return;
  box.innerHTML = '<p class="empty">Loading planning runs…</p>';
  try {
    const data = await json("/cycles" + q());
    renderCycles(box, data);
  } catch (e) { box.innerHTML = `<p class="empty">Could not load cycles: ${e.message}</p>`; }
}

function renderCycles(box, runs) {
  box.innerHTML = "";
  if (!runs.length) { box.append(el("p", "empty", "No planning runs.")); return; }
  const table = el("table");
  table.innerHTML = `<thead><tr><th>Run</th><th>As of</th><th>Considered</th><th>Stopped</th><th>Dispatched</th><th>Suppressed</th><th>λ contact</th><th>Dual bound</th><th>Ratio</th></tr></thead>`;
  const tb = el("tbody");
  for (const r of runs) {
    tb.append(el("tr", null, `<td>${r.id || "—"}</td><td>${(r.as_of || "").slice(0, 10) || "—"}</td><td>${r.considered || "—"}</td><td>${r.stopped || "—"}</td><td>${r.dispatched || "—"}</td><td>${r.suppressed || "—"}</td><td class="num">${r.lambda_contact != null ? "₹" + (r.lambda_contact * 100).toFixed(2) : "—"}</td><td class="num">${r.dual_bound_paise != null ? inr(r.dual_bound_paise) : "—"}</td><td class="num">${r.optimality_ratio != null ? r.optimality_ratio.toFixed(4) : "—"}</td>`));
  }
  table.append(tb); box.append(table);
}

/* ---- Evidence view ---- */
async function showEvidence() {
  showView("evidence");
  const box = $("evidence-body");
  if (!box) return;
  box.innerHTML = '<p class="empty">Loading evidence…</p>';
  try {
    const verify = await json("/audit/verify").catch(() => null);
    const chain = await json("/audit/chain").catch(() => null);
    renderEvidence(box, verify, chain);
  } catch (e) { box.innerHTML = `<p class="empty">Could not load evidence: ${e.message}</p>`; }
}

function renderEvidence(box, verify, chain) {
  box.innerHTML = "";
  if (verify) {
    const panel = el("div", "panel");
    panel.append(el("h3", null, "Audit chain <span class=\"caption\">audit.verify_all()</span>"));
    const intact = verify.intact !== false;
    panel.append(el("div", "sub", `${intact ? "✓ Chain intact" : "✕ Chain broken"} — ${verify.rows || "?"} rows in 250 ms`));
    box.append(panel);
  }
  if (chain) {
    const panel = el("div", "panel");
    panel.append(el("h3", null, "Chain tip <span class=\"caption\">audit_event</span>"));
    panel.append(el("div", "sub", `${chain.length || "?"} rows`));
    box.append(panel);
  }
}

/* ---- Init ---- */
async function init() {
  initTheme();
  initNav();
  addCaptions();
  const merchants = await json("/merchants");
  const sel = $("merchant");
  sel.append(new Option("All merchants", ""));
  for (const m of merchants) { merchantNames[m.id] = m.name; sel.append(new Option(`${m.name} · ${m.segment}`, m.id)); }
  sel.addEventListener("change", () => {
    merchantId = sel.value || null;
    // Re-enter the route rather than only remounting the overview: changing the
    // merchant while on the queue or the case list used to leave the previous
    // merchant's rows on screen under the new merchant's name.
    caseFacets = null;
    Overview.mount();
    refreshApprovalBadge();
    navigate(location.hash);
  });
  await refresh();
  initRouter();
  refreshApprovalBadge();
}

init().catch((e) => { el("main").prepend(el("p", "empty", `Could not reach the API: ${e.message}. Is the console API running?`)); });

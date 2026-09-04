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
      // A routed section that is not the current view is display:none, so its
      // rect is all zeros — top 0 passes the reading-line test and the loop
      // would elect the last hidden one, which is how the rail came to say
      // "Evidence" while the reader was halfway down Today.
      if (r.height === 0) continue;
      if (r.top <= line && r.bottom > line) { current = s; break; }
      if (r.top <= line) current = s;
    }
    // Only `hero` is in the rail now. The analysis sections below it are
    // reached by the inline jump list, and while reading them the reader is
    // still on Today — so the rail says so rather than going blank.
    if (current && !links.some((a) => a.dataset.target === current.id)) {
      current = sections[0];
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
  else markNav(name === "case" ? "cases" : name === "cycles" ? "evidence" : name);
}

/* ---- Overview object ---- */
const Overview = {
  mount() {
    const done = Promise.allSettled([
      loadToday().catch(() => { const b = $("today-body"); if (b) { b.innerHTML = ""; b.append(el("p", "empty", "This panel reads the live database. Start the stack with make up && make services, or run make demo.")); } }),
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

/* ---- DB panels ----
 * Wrapped in arrows so each name is resolved when the panel runs, not when this
 * array is built. app.js is evaluated before views/*.js, so capturing the
 * functions by value here bound whatever app.js had defined at that moment.
 * While app.js carried its own copies that silently worked — and meant the
 * first render went through app.js's versions while the poller and the merchant
 * selector went through views', two code paths for the same panels chosen by
 * how you arrived. */
const DB_PANELS = [
  [() => loadSnapshot(), "snapshot-tiles"],
  [() => loadRisk(),     "risk-bars"],
  [() => loadStopping(), "stopping-body"],
  [() => loadPolicy(),   "policy-body"],
  [() => loadDecisions(), "decisions-body"],
];

async function refresh() {
  await loadLift().catch(() => {});
  const done = Promise.allSettled([loadToday(), ...DB_PANELS.map(([load, target]) => load().catch(() => { const box = $(target); if (!box) return; box.innerHTML = ""; box.append(el("p", "empty", "This panel reads the live database. Start the stack with make up && make services, or run make demo for the full walkthrough.")); })), ]);
  await done;
  syncNav();
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

/* Niyama console.
 *
 * Renders the read-only JSON endpoints the API serves. Crucially, every code or
 * enum that reaches the screen is translated to plain English here (see the
 * *_NAME maps below): a merchant should never see "npci_represent_cap" or
 * "schedule_debit" — they should read "retry limit reached" or "auto-debit".
 *
 * The page is a narrative in four questions, read top to bottom:
 *   1. How much is at stake?   (hero + snapshot + risk)
 *   2. Was acting worth it?    (lift comparison)
 *   3. What did we NOT do?     (stopping rules)
 *   4. What kept us honest?    (guardrails + recent activity)
 */

"use strict";

const $ = (id) => document.getElementById(id);

async function json(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

/* ---- Indian rupee formatting (paise -> ₹, lakh/crore grouping) ---- */
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
  if (!rows.length) {
    container.append(el("p", "empty", "Nothing here yet."));
    return;
  }
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

let merchantId = null;
let merchantNames = {};
const q = (extra = {}) => {
  const p = new URLSearchParams();
  if (merchantId) p.set("merchant_id", merchantId);
  for (const [k, v] of Object.entries(extra)) if (v !== undefined && v !== null) p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
};

/* ---- 1. Hero + snapshot ---- */
let liftCache = null;

async function loadLift() {
  try {
    liftCache = await json("/metrics/lift");
  } catch {
    liftCache = null;
  }
  renderHero();
  renderLiftComparison();
}

function renderHero() {
  document.querySelector(".hero-scope")?.remove();
  const y = liftCache?.arms?.find((a) => a.key === "Y");
  if (!y) {
    $("hero-big").textContent = "No evaluation yet";
    $("hero-receipt").textContent = "Run the evaluation to measure the net margin this system actually created.";
    return;
  }
  // `make demo-light` produces a service-free bundle from the sensitivity
  // harness: no merchant, no holdout arm, and — at that sample size — an
  // interval that contains zero. It gets its own hero rather than being forced
  // through copy written for a merchant's book.
  const light = liftCache.source === "demo-light";
  if (light) {
    $("hero-big").textContent = inr(y.contact_incremental_paise, { signed: true });
    $("hero-receipt").innerHTML =
      `In the service-free demo world, the same <strong>${num(y.contacts)}</strong> contacts ` +
      `earned <strong>${inr(y.contact_incremental_paise)}</strong> more than retrying alone. ` +
      `Per 1,000 contacted that is <strong>${inr(y.contact_per_1k.point, { signed: true })}</strong>, ` +
      `95% CI ${inr(y.contact_per_1k.low, { signed: true })} to ` +
      `${inr(y.contact_per_1k.high, { signed: true })} — which contains zero at this size. ` +
      `The ordering across strategies is the claim here; the rupee figure is not.`;
  } else {
    $("hero-big").textContent = inr(y.net_incremental_paise, { signed: true });
    const caused = y.recovered_cases - y.would_have_recovered_anyway;
    $("hero-receipt").innerHTML =
      `Of <strong>${num(y.recovered_cases)}</strong> payments recovered, ` +
      `<strong>${num(y.would_have_recovered_anyway)}</strong> would have come back on their own. ` +
      `We caused <strong>${num(caused)}</strong> — worth <strong>${inr(y.net_incremental_paise)}</strong> ` +
      `after fees, discounts and channel costs.`;
  }

  // "Only strategy clearly in positive territory": an interval that excludes
  // zero on the *up* side. A rival that excludes zero on the down side (contact
  // reliably hurt) is a different and less flattering claim, so it must not
  // suppress the badge.
  const contact = y.contact_per_1k;
  const clearlyPositive = (a) => a.contact_per_1k
    && a.contact_per_1k.excludes_zero && a.contact_per_1k.point > 0;
  const yWinsAlone = clearlyPositive(y)
    && !(liftCache.arms || []).some((a) => a.key !== "Y" && clearlyPositive(a));
  // Whose result this is. `/metrics/lift` serves the last `make eval`, which
  // runs on one merchant's book — so the dropdown does NOT move this number.
  // Leaving that unsaid meant selecting another merchant showed them somebody
  // else's lift as if it were theirs.
  const on = merchantNames[liftCache.merchant_id] || "one merchant's book";
  const asOf = (liftCache.as_of || "").slice(0, 10);
  const scope = el("div", "hero-scope", light
    ? `Measured in the <strong>service-free demo world</strong> — no database, `
      + `generated and graded in process. Run <code>make demo</code> for a merchant's book.`
    : `Measured on <strong>${on}</strong> — ${num(liftCache.cases)} cases`
      + (asOf ? `, as of ${asOf}` : "")
      + (merchantId && merchantId !== liftCache.merchant_id
          ? ` · the panels below are ${merchantNames[merchantId] || "the selected merchant"}`
          : ""));
  $("hero-receipt").after(scope);

  if (yWinsAlone) {
    const note = $("hero-note");
    note.textContent = "The only strategy that clearly earned money from its contacts";
    note.hidden = false;
  } else {
    $("hero-note").hidden = true;
  }
}

async function loadSnapshot() {
  const box = $("snapshot-tiles");
  // Fetch first, empty second. Clearing before the await left this panel blank
  // for the length of a round trip — on a five-second refresh cycle that is a
  // visible flicker, and on a hard refresh mid-recording it is a bare heading.
  const [risk, notChased] = await Promise.all([
    json(`/metrics/revenue-at-risk${q()}`),
    json(`/metrics/not-chased${q()}`),
  ]);
  const y = liftCache?.arms?.find((a) => a.key === "Y");
  box.innerHTML = "";

  box.append(tile(
    "Money at risk",
    inr(risk.total_paise),
    `${num(risk.total_cases)} open obligations`,
  ));
  // Same caveat as the hero: the evaluation is one merchant's book, so this
  // tile has to say whose when the rest of the page is someone else's.
  const light = liftCache?.source === "demo-light";
  const elsewhere = !light && merchantId && liftCache
    && merchantId !== liftCache.merchant_id;
  box.append(tile(
    "Net margin created",
    y ? inr(y.net_incremental_paise, { signed: true }) : "—",
    light
      ? "service-free demo world, not this book"
      : elsewhere
        ? `measured on ${merchantNames[liftCache.merchant_id] || "another merchant"}, not this one`
        : "measured against a holdout, not assumed",
    y && y.net_incremental_paise > 0 && !elsewhere && !light ? "pos" : "",
  ));
  box.append(tile(
    "Deliberately not chased",
    inr(notChased.stopped_total_paise),
    `${num(notChased.stopped_by_rule.reduce((s, r) => s + r.cases, 0))} cases spared for a named reason`,
  ));
}

/* ---- 2. Risk ---- */
async function loadRisk() {
  const d = await json(`/metrics/revenue-at-risk${q()}`);
  bars($("risk-bars"), (d.by_surface || []).map((s) => ({
    name: SURFACE_NAME[s.kind] || s.kind,
    value: s.amount_paise,
    display: `${inr(s.amount_paise)}  ·  <span class="dim">${num(s.cases)} cases</span>`,
  })));
}

/* ---- 2b. Lift comparison ---- */
function renderLiftComparison() {
  const box = $("lift-body");
  box.innerHTML = "";
  const d = liftCache;
  if (!d) {
    box.innerHTML = '<p class="empty">Run the evaluation to see this comparison.</p>';
    return;
  }
  const arms = (d.arms || []).filter((a) => a.acts && a.key !== "B4");
  if (merchantId && merchantId !== d.merchant_id) {
    box.append(el("p", "kicker",
      `This comparison is <strong>${merchantNames[d.merchant_id] || "another merchant"}</strong>'s book — `
      + `the evaluation runs on one merchant at a time. Everything else on this page is `
      + `${merchantNames[merchantId] || "the merchant you selected"}.`));
  }
  box.append(el("div", "legend",
    `<span><span class="swatch" style="background:var(--good)"></span> Niyama</span>` +
    `<span><span class="swatch" style="background:var(--neutral)"></span> Other strategies</span>`
  ));

  const cont = el("div", "bars");
  box.append(cont);
  bars(cont, arms.map((a) => ({
    name: `<b>${ARM_NAME[a.key] || a.label}</b>`,
    value: a.contact_incremental_paise,
    fill: a.key === "Y" ? (a.contact_incremental_paise > 0 ? "good" : "bad") : "muted",
    display: `${inr(a.contact_incremental_paise, { signed: true })}`
      + (a.contact_per_1k ? `  ·  <span class="dim">${inr(a.contact_per_1k.point, { signed: true })}/1k</span>` : ""),
    title: a.contact_per_1k
      ? `${ARM_NAME[a.key] || a.label} — 95% CI per 1,000: ${inr(a.contact_per_1k.low, { signed: true })} to ${inr(a.contact_per_1k.high, { signed: true })}`
      : undefined,
  })), { format: (v) => inr(v, { signed: true }) });

  const y = arms.find((a) => a.key === "Y");
  const bestRival = arms.filter((a) => a.key !== "Y")
    .sort((a, b) => b.contact_incremental_paise - a.contact_incremental_paise)[0];

  if (y && bestRival) {
    const gap = y.contact_incremental_paise - bestRival.contact_incremental_paise;
    box.append(el("div", "callout good",
      `<strong>The choice of who to contact was worth ${inr(gap, { signed: true })} more</strong> ` +
      `than the best alternative. Every other strategy <em>lost</em> money on the customers it contacted.`));
  }

  const h = el("h3", null, "Honesty, spelled out");
  box.append(h);
  // The console has to volunteer the same limitation the evaluation docs lead
  // with. A dashboard that quotes an interval excluding zero without saying
  // what the interval does and does not cover is the exact overstatement this
  // product exists to argue against.
  const power = y?.cases_needed_for_power;
  box.append(el("p", "kicker",
    `The ordering above is stable; the interval around it is narrower than a live ` +
    `deployment would see. It comes from a paired comparison against a known ` +
    `counterfactual, so it captures how much customers differ from each other — not ` +
    `the uncertainty about what an untreated customer would have done. ` +
    (power
      ? `At this book's size the effect is small next to how much a single large payment swings: ` +
        `separating these strategies from live data alone would need about ${num(power)} cases. `
      : "") +
    `That is a property of the data, and no estimator fixes it.`));
}

/* ---- 3. Stopping rules ---- */
async function loadStopping() {
  const box = $("stopping-body");
  const d = await json(`/metrics/not-chased${q()}`);
  box.innerHTML = "";

  const tiles = el("div", "tiles");
  tiles.append(tile(
    "Walked away from",
    inr(d.stopped_total_paise),
    "cases stopped with a named reason",
  ));
  tiles.append(tile(
    "Left for tomorrow",
    inr(d.considered_not_funded_paise),
    `${num(d.considered_not_funded_cases)} cases still open — budget went elsewhere`,
  ));
  box.append(tiles);

  const card = el("div", "card");
  card.style.marginTop = "0.9rem";
  const legend = el("div", "legend",
    `<span><span class="swatch" style="background:var(--accent)"></span> by reason</span>`);
  card.append(legend);
  const b = el("div", "bars");
  card.append(b);
  const rows = (d.stopped_by_rule || []).map((r) => ({
    name: name(STOP_NAME, r.stop_reason),
    value: r.amount_paise,
    display: `${inr(r.amount_paise)}  ·  <span class="dim">${num(r.cases)} cases</span>`,
  }));
  bars(b, rows);
  box.append(card);
}

/* ---- 4. Guardrails ---- */
async function loadPolicy() {
  const box = $("policy-body");
  const rows = await json(`/metrics/policy${q()}`);
  box.innerHTML = "";
  if (!rows.length) {
    box.append(el("p", "empty", "No blocks or escalations recorded."));
    return;
  }
  // Group by pack so regulatory / merchant / stopping read as distinct things.
  const order = ["regulatory", "merchant", "stopping"];
  const groups = {};
  for (const r of rows) (groups[r.pack] ??= []).push(r);
  for (const pack of [...order.filter((p) => groups[p]), ...Object.keys(groups).filter((p) => !order.includes(p))]) {
    const head = el("div", "group-head",
      `${PACK_NAME[pack] || pack} <span class="count">${groups[pack].length} rule${groups[pack].length === 1 ? "" : "s"}</span>`);
    box.append(head);
    const table = el("table");
    table.innerHTML = `<thead><tr>
      <th>Rule</th><th>Outcome</th>
      <th style="text-align:right">Cases</th><th style="text-align:right">Money involved</th>
    </tr></thead>`;
    const tb = el("tbody");
    for (const r of groups[pack]) {
      const tr = el("tr");
      tr.innerHTML = `
        <td>${name(RULE_NAME, r.rule_id)}</td>
        <td>${pill(r.verdict)}</td>
        <td class="num">${num(r.n)}</td>
        <td class="num">${inr(r.amount_paise)}</td>`;
      tb.append(tr);
    }
    table.append(tb);
    box.append(table);
  }
}

function pill(v) {
  const map = {
    allow: ["allow", "✓ allowed"],
    block: ["block", "✕ blocked"],
    escalate: ["escalate", "▲ escalated"],
  };
  const [cls, label] = map[v] || ["neutral", v];
  return `<span class="pill ${cls}">${label}</span>`;
}

/* ---- 5. Recent activity ---- */
function rupeeify(text) {
  if (!text) return "";
  return String(text).replace(/(-?\d+)\s*paise\b/g, (_, p) => inr(Number(p), { signed: true }));
}

// Views over the feed. "Overruled" is the one that matters in a walkthrough:
// it isolates the decisions where a rule refused the action the allocator
// ranked first, which is otherwise a needle in a page of routine retries.
const DECISION_VIEWS = {
  all: { label: "Everything", match: () => true },
  overruled: {
    label: "A rule overruled us",
    match: (d) => (d.alternatives_rejected || []).some((a) => a.rejected_by === "POLICY"),
  },
  contacted: {
    label: "Customer contacted",
    match: (d) => d.channel && d.channel !== "none",
  },
};
let decisionView = "all";
let decisionRows = [];

async function loadDecisions() {
  // Over-fetch so the views have something to filter. The feed still renders
  // 25 rows; the rest is only ever counted.
  decisionRows = await json(`/decisions${q({ limit: 250 })}`);
  renderDecisions();
}

function renderDecisions() {
  const all = decisionRows;
  const box = $("decisions-body");
  box.innerHTML = "";
  if (!all.length) {
    box.append(el("p", "empty", "No decisions yet."));
    return;
  }

  const chips = el("div", "chips");
  for (const [key, view] of Object.entries(DECISION_VIEWS)) {
    const n = all.filter(view.match).length;
    const c = el("button", "chip" + (key === decisionView ? " on" : ""),
      `${view.label} <span class="count">${num(n)}</span>`);
    c.disabled = n === 0;
    c.addEventListener("click", () => { decisionView = key; renderDecisions(); });
    chips.append(c);
  }
  box.append(chips);

  const rows = all.filter(DECISION_VIEWS[decisionView].match).slice(0, 25);
  if (!rows.length) {
    box.append(el("p", "empty", "Nothing matched this view in the last 250 decisions."));
    return;
  }
  const acted = rows.filter((d) => d.action_kind !== "suppress");
  const held = rows.filter((d) => d.action_kind === "suppress");
  if (held.length) {
    box.append(el("p", "kicker",
      `${num(acted.length)} action${acted.length === 1 ? "" : "s"} taken, ` +
      `${num(held.length)} held back — actions shown first.`));
  }
  const card = el("div", "card");
  const table = el("table");
  table.innerHTML = `<thead><tr>
    <th>Action</th><th>Outcome</th>
    <th style="text-align:right">Expected margin</th>
    <th style="text-align:right">Value</th><th>Why</th>
  </tr></thead>`;
  const tb = el("tbody");
  for (const d of rows) {
    const alts = (d.alternatives_rejected || []).slice(0, 3).map((a) => {
      // `rejected_by` says who refused (ALLOCATOR or POLICY); `blocked_by`
      // names the rule. Showing the former where the latter belongs turned the
      // strongest line in the feed into "blocked by POLICY".
      const byRule = a.rejected_by && a.rejected_by !== "ALLOCATOR";
      const why = byRule
        ? `<strong>blocked by ${name(RULE_NAME, a.blocked_by || a.rejected_by)}</strong>`
        : "not chosen — lower margin";
      const via = a.channel && a.channel !== "none" ? ` via ${name(CHANNEL_NAME, a.channel)}` : "";
      return `<div class="rejected">✕ ${name(ACTION_NAME, a.action)}${via} — ${why}</div>`;
    }).join("");
    const tr = el("tr");
    tr.innerHTML = `
      <td><strong>${name(ACTION_NAME, d.action_kind)}</strong>
          ${d.channel && d.channel !== "none" ? `<span class="tag">${name(CHANNEL_NAME, d.channel)}</span>` : ""}</td>
      <td>${pill(d.policy_verdict)}</td>
      <td class="num">${d.margin_display ?? "—"}</td>
      <td class="num">${d.display ?? ""}</td>
      <td>${d.reason ?? ""}${alts}</td>`;
    tb.append(tr);
  }
  table.append(tb);
  card.append(table);
  box.append(card);
}

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
  // Cycle auto -> light -> dark -> auto
  $("theme").addEventListener("click", () => {
    const cur = localStorage.getItem("niyama-theme") || "auto";
    applyTheme(cur === "auto" ? "light" : cur === "light" ? "dark" : "auto");
  });
}

// Every panel below the lift result reads the database. `make demo-light`
// deliberately has no database, and a failed fetch used to leave those panels
// simply blank — which reads as "this product has nothing to say" rather than
// "this view needs the stack running".
const DB_PANELS = [
  [loadSnapshot, "snapshot-tiles"],
  [loadRisk, "risk-bars"],
  [loadStopping, "stopping-body"],
  [loadPolicy, "policy-body"],
  [loadDecisions, "decisions-body"],
];

async function refresh() {
  await Promise.allSettled([
    loadLift(),
    ...DB_PANELS.map(([load, target]) => load().catch(() => {
      const box = $(target);
      if (!box) return;
      box.innerHTML = "";
      box.append(el("p", "empty",
        "This panel reads the live database. Start the stack with "
        + "make up && make services, or run make demo for the full walkthrough."));
    })),
  ]);
}

async function init() {
  initTheme();
  const merchants = await json("/merchants");
  const sel = $("merchant");
  sel.append(new Option("All merchants", ""));
  for (const m of merchants) {
    merchantNames[m.id] = m.name;
    sel.append(new Option(`${m.name} · ${m.segment}`, m.id));
  }
  sel.addEventListener("change", () => { merchantId = sel.value || null; refresh(); });
  await refresh();
  setInterval(refresh, 5000);
}

init().catch((e) => {
  el("main").prepend(
    el("p", "empty", `Could not reach the API: ${e.message}. Is the console API running?`));
});
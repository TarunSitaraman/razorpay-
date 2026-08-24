/* Yukti console.
 *
 * Reads the JSON endpoints the API already serves rather than rendering
 * server-side, so there is exactly one place where a number is computed. A
 * template that re-derived any of these would be a second implementation free
 * to drift from the first.
 *
 * No framework and no build step, deliberately: `make demo` has to work from a
 * cold clone, and a toolchain that must install before the console renders is
 * the thing that fails in front of an audience.
 */
"use strict";

const $ = (id) => document.getElementById(id);

async function json(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

/* Indian digit grouping, matching domain.money.format_inr. A lakh is not a
   hundred thousand to the person reading this screen. */
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
  const sign = neg ? "-" : (opts.signed ? "+" : "");
  return `${sign}₹${out}`;
}

const num = (n) => (n ?? 0).toLocaleString("en-IN");

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

function tile(label, value, sub) {
  const t = el("div", "tile");
  t.append(el("div", "label", label), el("div", "value", value));
  if (sub) t.append(el("div", "sub", sub));
  return t;
}

/* Horizontal bars. Scaled against the largest magnitude present so a single
   dominant row cannot flatten the rest into invisibility, and every row is
   directly labelled — there is one series, so no legend box earns its place. */
function bars(container, rows, { format = inr, variant = () => "" } = {}) {
  container.innerHTML = "";
  if (!rows.length) {
    container.append(el("p", "empty", "Nothing to show yet."));
    return;
  }
  const max = Math.max(...rows.map((r) => Math.abs(r.value)), 1);
  for (const r of rows) {
    const row = el("div", "bar-row");
    row.append(el("div", "name", r.name));
    const track = el("div", "bar-track");
    const fill = el("div", `bar-fill ${variant(r)}`);
    fill.style.width = `${Math.max(2, (Math.abs(r.value) / max) * 100)}%`;
    track.append(fill);
    row.append(track);
    row.append(el("div", "val", r.display ?? format(r.value)));
    row.title = r.title ?? `${r.name}: ${r.display ?? format(r.value)}`;
    container.append(row);
  }
}

const SURFACE_LABEL = {
  cart: "abandoned cart",
  subscription_cycle: "failed subscription",
  invoice: "overdue invoice",
  order: "failed payment",
};

let merchantId = null;
const q = () => (merchantId ? `?merchant_id=${encodeURIComponent(merchantId)}` : "");

async function loadRisk() {
  const d = await json(`/metrics/revenue-at-risk${q()}`);
  $("risk-tiles").innerHTML = "";
  $("risk-tiles").append(
    tile("At risk", inr(d.total_paise), `${num(d.total_cases)} open obligations`),
  );
  bars(
    $("risk-bars"),
    (d.by_surface || []).map((s) => ({
      name: SURFACE_LABEL[s.kind] || s.kind,
      value: s.amount_paise,
      display: `${inr(s.amount_paise)}  ·  ${num(s.cases)}`,
    })),
  );
}

async function loadNotChased() {
  const d = await json(`/metrics/not-chased${q()}`);
  $("notchased-tiles").innerHTML = "";
  $("notchased-tiles").append(
    tile("Stopped by a named rule", inr(d.stopped_total_paise),
         "a decision that this money is not worth chasing"),
    tile("Considered, not funded", inr(d.considered_not_funded_paise),
         `${num(d.considered_not_funded_cases)} cases still open tomorrow`),
  );
  bars(
    $("stopping-bars"),
    (d.stopped_by_rule || []).map((r) => ({
      name: (r.stop_reason || "").replace(/_/g, " "),
      value: r.amount_paise,
      display: `${inr(r.amount_paise)}  ·  ${num(r.cases)}`,
    })),
  );
}

async function loadBudgets() {
  const rows = await json(`/metrics/budgets${q()}`);
  const box = $("budget-tiles");
  box.innerHTML = "";
  if (!rows.length) {
    box.append(el("p", "empty", "No budget window for today."));
    return;
  }
  for (const r of rows) {
    const pct = r.limit_val ? Math.min(100, (r.consumed_val / r.limit_val) * 100) : 0;
    const t = tile(
      `${r.kind} budget`,
      r.kind === "discount" ? inr(r.consumed_val) : num(r.consumed_val),
      `of ${r.kind === "discount" ? inr(r.limit_val) : num(r.limit_val)} authorised`,
    );
    const track = el("div", "meter-track");
    const fill = el("div", `meter-fill ${pct >= 100 ? "full" : pct >= 80 ? "warn" : ""}`);
    fill.style.width = `${pct}%`;
    track.append(fill);
    t.append(track);
    box.append(t);
  }
}

async function loadPolicy() {
  const rows = await json(`/metrics/policy${q()}`);
  const box = $("policy-body");
  box.innerHTML = "";
  if (!rows.length) {
    box.append(el("p", "empty", "No blocks or escalations recorded yet."));
    return;
  }
  const table = el("table");
  table.innerHTML = `<thead><tr>
    <th>pack</th><th>rule</th><th>verdict</th>
    <th style="text-align:right">cases</th><th style="text-align:right">money</th>
  </tr></thead>`;
  const body = el("tbody");
  for (const r of rows) {
    const tr = el("tr");
    tr.innerHTML = `
      <td>${r.pack}</td>
      <td><code>${r.rule_id}</code></td>
      <td>${verdictPill(r.verdict)}</td>
      <td class="num">${num(r.n)}</td>
      <td class="num">${inr(r.amount_paise)}</td>`;
    body.append(tr);
  }
  table.append(body);
  box.append(table);
}

/* Status carries a glyph and a word, never colour alone. */
function verdictPill(v) {
  const map = {
    allow: ["allow", "✓", "allow"],
    block: ["block", "✕", "blocked"],
    escalate: ["escalate", "▲", "escalated"],
  };
  const [cls, glyph, label] = map[v] || ["stop", "■", v];
  return `<span class="pill ${cls}">${glyph} ${label}</span>`;
}

async function loadDecisions() {
  const rows = await json(`/decisions${q()}&limit=25`.replace("?&", "?"));
  const box = $("decisions-body");
  box.innerHTML = "";
  if (!rows.length) {
    box.append(el("p", "empty", "No decisions yet — run `make plan`."));
    return;
  }
  const table = el("table");
  table.innerHTML = `<thead><tr>
    <th>action</th><th>verdict</th><th style="text-align:right">expected margin</th>
    <th style="text-align:right">amount</th><th>why, and what was turned down</th>
  </tr></thead>`;
  const body = el("tbody");
  for (const d of rows) {
    const alts = (d.alternatives_rejected || []).slice(0, 3).map((a) => {
      const by = a.blocked_by ? `blocked by <code>${a.blocked_by}</code>` : "not funded";
      return `<div class="rejected">✕ ${a.action}${a.channel && a.channel !== "none" ? "/" + a.channel : ""} — ${by}: ${a.reason ?? ""}</div>`;
    }).join("");
    const tr = el("tr");
    tr.innerHTML = `
      <td><strong>${d.action_kind}</strong>${d.channel !== "none" ? ` <span class="tag">${d.channel}</span>` : ""}
          ${d.arm === "holdout" ? '<span class="pill neutral">held out</span>' : ""}</td>
      <td>${verdictPill(d.policy_verdict)}</td>
      <td class="num">${d.margin_display ?? ""}</td>
      <td class="num">${d.display ?? ""}</td>
      <td>${d.reason ?? ""}${alts}</td>`;
    body.append(tr);
  }
  table.append(body);
  box.append(table);
}

async function loadLift() {
  const box = $("lift-body");
  let d;
  try {
    d = await json("/metrics/lift");
  } catch {
    box.innerHTML = '<p class="empty">Run <code>make eval</code> to produce this.</p>';
    return;
  }
  box.innerHTML = "";

  const arms = d.arms || [];
  const net = arms.find((a) => a.key === d.winner_by_net);
  const gross = arms.find((a) => a.key === d.winner_by_gross);

  /* The receipt: the sentence no competitor in this market prints. */
  const y = arms.find((a) => a.key === "Y");
  if (y && y.recovered_cases) {
    const caused = y.recovered_cases - y.would_have_recovered_anyway;
    const tiles = el("div", "tiles");
    tiles.append(
      tile("Billed for", num(y.recovered_cases) + " recoveries", "gross, as everyone reports it"),
      tile("Would have happened anyway", num(y.would_have_recovered_anyway),
           "organic — measured against a holdout"),
      tile("We actually caused", num(caused),
           inr(y.net_incremental_paise) + " net of MDR, discounts and channel cost"),
    );
    box.append(tiles);
  }

  if (gross && net && gross.key !== net.key) {
    box.append(el("p", "note",
      `<strong>${gross.label}</strong> recovered the most money. ` +
      `<strong>${net.label}</strong> earned the most. That gap is the product.`));
  }

  /* Two measures of different scale get two charts — never one dual axis. */
  box.append(el("h3", null, "Gross recovered"));
  const g = el("div", "bars");
  box.append(g);
  bars(g, arms.map((a) => ({
    name: `${a.key} · ${a.label}`, value: a.gross_recovered_paise,
  })), { variant: (r) => (r.name.startsWith(d.winner_by_gross + " ") ? "rival" : "muted") });

  box.append(el("h3", null, "Net incremental margin (what the merchant keeps)"));
  const n = el("div", "bars");
  box.append(n);
  bars(n, arms.map((a) => ({
    name: `${a.key} · ${a.label}`,
    value: a.net_incremental_paise,
    display: a.per_1k
      ? `${inr(a.net_incremental_paise)}  ·  ${inr(a.per_1k.point, { signed: true })}/1k`
      : inr(a.net_incremental_paise),
    title: a.per_1k
      ? `95% CI per 1k opportunities: ${inr(a.per_1k.low, { signed: true })} to ${inr(a.per_1k.high, { signed: true })}`
      : undefined,
  })), { variant: (r) => (r.name.startsWith(d.winner_by_net + " ") ? "" : "muted") });

  box.append(el("p", "ci",
    "Bars are totals; the second figure is per 1,000 opportunities. " +
    "Hover for the 95% confidence interval, bootstrapped over customers."));

  /* Does a 10% holdout recover the truth? The measurement claim, checkable. */
  const acting = arms.filter((a) => a.acts && a.holdout_estimate);
  if (acting.length) {
    box.append(el("h3", null, "Can a 10% holdout recover the true causal number?"));
    const table = el("table");
    table.innerHTML = `<thead><tr><th>arm</th>
      <th style="text-align:right">oracle truth</th>
      <th style="text-align:right">holdout estimate (95% CI)</th>
      <th style="text-align:right">error</th><th>covers?</th></tr></thead>`;
    const tb = el("tbody");
    for (const a of acting) {
      const tr = el("tr");
      tr.innerHTML = `
        <td>${a.key}</td>
        <td class="num">${inr(a.net_incremental_paise)}</td>
        <td class="num">${inr(a.holdout_estimate.point)}
            <span class="ci">[${inr(a.holdout_estimate.low)}, ${inr(a.holdout_estimate.high)}]</span></td>
        <td class="num">${(a.holdout_error * 100).toFixed(1)}%</td>
        <td>${a.holdout_brackets_truth
              ? '<span class="pill allow">✓ yes</span>'
              : '<span class="pill block">✕ no</span>'}</td>`;
      tb.append(tr);
    }
    table.append(tb);
    box.append(table);
    box.append(el("p", "note",
      "Oracle truth is available only in simulation. The holdout estimate is what " +
      "a real deployment could actually compute — a wide honest interval that covers " +
      "the truth is a working estimator; a tight one that misses is a broken one."));
  }
}

async function refresh() {
  await Promise.allSettled([
    loadRisk(), loadNotChased(), loadBudgets(),
    loadPolicy(), loadDecisions(), loadLift(),
  ]);
}

async function init() {
  const merchants = await json("/merchants");
  const sel = $("merchant");
  sel.append(new Option("all merchants", ""));
  for (const m of merchants) sel.append(new Option(`${m.name} · ${m.segment}`, m.id));
  sel.addEventListener("change", () => { merchantId = sel.value || null; refresh(); });
  await refresh();
  // Polled rather than pushed: the console is read-only and a few seconds of
  // staleness costs nothing, where an SSE channel would add a failure mode to
  // the one thing that has to work during a demo.
  setInterval(refresh, 5000);
}

init().catch((e) => {
  document.querySelector("main").prepend(
    el("p", "empty", `Could not reach the API: ${e.message}. Is \`make services\` running?`));
});

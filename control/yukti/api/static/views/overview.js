/* Overview panels — extracted from app.js verbatim.
 * Depends on globals defined in app.js: $, inr, num, el, tile, bars,
 * divergingBars, SURFACE_NAME, STOP_NAME, RULE_NAME, PACK_NAME,
 * ACTION_NAME, CHANNEL_NAME, ARM_NAME, name, pill, merchantId,
 * merchantNames, liftCache, q, renderStamp, syncNav.
 */

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

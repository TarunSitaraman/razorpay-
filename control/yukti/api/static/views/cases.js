/* Cases view — depends on globals from app.js: $, json, inr, num, el, name,
 * STOP_NAME, ARM_NAME, SURFACE_NAME, CHANNEL_NAME, ACTION_NAME, RULE_NAME,
 * merchantNames, q, showView, navigate, consentLine.
 */

/* Facets the list is currently narrowed by. Held here rather than in the URL
 * because the merchant selector already owns the query string, and a case list
 * filtered two different ways by two different mechanisms is a list nobody can
 * reason about. */
const caseFilter = { state: "", arm: "", stop_reason: "" };
let caseFacets = null;

// The four questions worth one click. Each is a state a merchant asks about by
// name — "what did we walk away from", "what are we holding out" — rather than
// a schema column they would have to translate first.
const CASE_PRESETS = [
  { key: "all",      label: "Everything",     filter: {} },
  { key: "stopped",  label: "Walked away",    filter: { stop_reason: "stopped" } },
  { key: "open",     label: "Still open",     filter: { state: "open" } },
  { key: "heldout",  label: "Held out",       filter: { arm: "holdout" } },
  { key: "escalated",label: "Sent to a human",filter: { state: "escalated" } },
];

async function showCasesList() {
  showView("cases");
  const box = $("cases-body");
  if (!box) return;
  box.innerHTML = '<p class="empty">Loading cases…</p>';
  // Facet counts are a property of the book, not of the current narrowing, so
  // they are fetched once per merchant rather than on every filter change.
  if (!caseFacets) caseFacets = await json(`/cases/facets${q()}`).catch(() => ({}));
  redrawCases(box);
}

const prunedFilter = () =>
  Object.fromEntries(Object.entries(caseFilter).filter(([, v]) => v));

function caseControls(box) {
  const wrap = el("div", "case-controls");

  const chips = el("div", "chips");
  const activePreset = CASE_PRESETS.find((p) =>
    JSON.stringify({ state: "", arm: "", stop_reason: "", ...p.filter }) === JSON.stringify(caseFilter));
  for (const p of CASE_PRESETS) {
    const b = el("button", "chip" + (activePreset && activePreset.key === p.key ? " on" : ""), p.label);
    b.type = "button";
    b.onclick = () => {
      Object.assign(caseFilter, { state: "", arm: "", stop_reason: "" }, p.filter);
      redrawCases(box);
    };
    chips.append(b);
  }
  wrap.append(chips);

  const row = el("div", "case-facets");
  row.append(facetSelect("state", "State", STATE_NAME));
  row.append(facetSelect("arm", "Arm", { treatment: "Worked", holdout: "Held out" }));
  row.append(facetSelect("stop_reason", "Stop reason", STOP_NAME));
  wrap.append(row);
  return wrap;

  function facetSelect(key, label, dict) {
    const opts = (caseFacets && caseFacets[key]) || [];
    const lab = el("label", "field");
    lab.append(el("span", null, label));
    const sel = el("select");
    sel.innerHTML =
      `<option value="">Any</option>` +
      opts.map((o) => `<option value="${o.value}"${caseFilter[key] === o.value ? " selected" : ""}>${name(dict, o.value)} (${num(o.n)})</option>`).join("");
    sel.onchange = () => { caseFilter[key] = sel.value; redrawCases(box); };
    lab.append(sel);
    return lab;
  }
}

function redrawCases(box) {
  box.innerHTML = "";
  box.append(caseControls(box));
  const rows = el("div");
  rows.id = "cases-rows";
  rows.innerHTML = '<p class="empty">Loading cases…</p>';
  box.append(rows);
  json(`/cases${q({ limit: 200, ...prunedFilter() })}`)
    .then((d) => renderCaseList(rows, d))
    .catch((e) => { rows.innerHTML = `<p class="empty">Could not load cases: ${e.message}</p>`; });
}

function renderCaseList(box, cases) {
  box.innerHTML = "";
  if (!cases.length) { box.append(el("p", "empty", "No cases found.")); return; }
  const table = el("table");
  table.innerHTML = `<thead><tr><th>Case</th><th>State</th><th>Arm</th><th class="num">Amount</th><th>Why it failed</th><th>Rail</th><th>Stop reason</th></tr></thead>`;
  const tb = el("tbody");
  for (const c of cases.slice(0, 100)) {
    tb.append(el("tr", null, `<td><a href="#/case/${c.id}"><code>${c.id}</code></a></td>` +
      `<td>${name(STATE_NAME, c.state)}</td>` +
      `<td>${c.arm === "holdout" ? "Held out" : c.arm ? "Worked" : "—"}</td>` +
      `<td class="num">${inr(c.amount_paise)}</td>` +
      `<td>${c.decline_label || (c.decline_code || "—")}</td>` +
      `<td>${[c.rail, c.issuer].filter(Boolean).join(" · ") || "—"}</td>` +
      `<td>${c.stop_reason ? name(STOP_NAME, c.stop_reason) : "—"}</td>`));
  }
  table.append(tb); box.append(table);
  const shown = Math.min(cases.length, 100);
  box.append(el("div", "caption",
    `${num(shown)} of ${num(cases.length)} matching cases — click one to open its file`));
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
  const { case: c, attempts, decisions, actions, outcomes, audit, ground_truth, collisions } = data;

  const header = el("div", "panel dossier-header");
  header.innerHTML = `<div class="dossier-title"><strong>${c.id}</strong> · ${name(STATE_NAME, c.state)} · ${c.arm === "holdout" ? "Held out" : "Worked"} · ${merchantNames[c.merchant_id] || c.merchant_id || ""}</div><div class="dossier-sub">${name(SURFACE_NAME, c.obligation_kind)} · ${inr(c.amount_paise)} · ${c.stop_reason ? "stopped: " + name(STOP_NAME, c.stop_reason) : "never stopped"}</div>`;
  box.append(header);

  const moneyPanel = el("div", "panel");
  moneyPanel.append(el("h3", null, "The money <span class=\"caption\">payment_attempt</span>"));
  moneyPanel.append(el("div", "sub", `Obligation ${c.obligation_id} · ${inr(c.amount_paise)}`));
  if (attempts.length) {
    const table = el("table");
    table.innerHTML = `<thead><tr><th>Rail</th><th>Issuer</th><th>PSP</th><th>Why it failed</th><th>Kind</th><th>Amount</th><th>Ours</th></tr></thead>`;
    const tb = el("tbody");
    for (const a of attempts) tb.append(el("tr", null, `<td>${a.rail || "—"}</td><td>${a.issuer || "—"}</td><td>${a.psp || "—"}</td><td>${(a.decline && a.decline.label) || a.decline_code || "—"}</td><td class="sub">${a.decline ? String(a.decline.transience).replace(/_/g, " ") : ""}</td><td class="num">${inr(a.amount_paise)}</td><td>${a.ours ? "✓" : "—"}</td>`));
    table.append(tb); moneyPanel.append(table);
  }
  box.append(moneyPanel);

  const custPanel = el("div", "panel");
  custPanel.append(el("h3", null, "The customer <span class=\"caption\">customer</span>"));
  custPanel.append(el("div", "sub", `Consent: ${consentLine(c.consent)}${c.opted_out_at ? " · opted out " + String(c.opted_out_at).slice(0, 10) : ""} · LTV: ${c.ltv_band || "—"} · Tenure: ${c.tenure_days || "—"}d · Prefers: ${name(CHANNEL_NAME, c.preferred_channel) || "—"}`));
  box.append(custPanel);

  if (collisions && collisions.length) {
    const colPanel = el("div", "panel");
    colPanel.append(el("h3", null, "Collisions <span class=\"caption\">obligation</span>"));
    colPanel.append(el("div", "sub", "Other open obligations on other surfaces"));
    const table = el("table");
    table.innerHTML = `<thead><tr><th>Obligation</th><th>Kind</th><th>Amount</th><th>Case state</th></tr></thead>`;
    const tb = el("tbody");
    for (const o of collisions.slice(0, 10)) tb.append(el("tr", null, `<td>${o.id}</td><td>${name(SURFACE_NAME, o.kind) || o.kind}</td><td class="num">${inr(o.amount_paise)}</td><td>${o.case_state ? name(STATE_NAME, o.case_state) : "—"}</td>`));
    table.append(tb); colPanel.append(table); box.append(colPanel);
  }

  if (decisions.length) {
    const decPanel = el("div", "panel");
    decPanel.append(el("h3", null, "Everything considered <span class=\"caption\">agent_decision.alternatives_rejected</span>"));
    for (const d of decisions) {
      const chosen = d.action_kind !== "suppress";
      const tagCls = chosen ? "pill allow" : "pill neutral";
      const tag = chosen ? "CHOSEN" : "SUPPRESSED";
      let altsHtml = "";
      const alts = d.alternatives_rejected || [];
      if (alts.length) altsHtml = '<div class="rejected-list">' + alts.map(a => { const byRule = a.rejected_by && a.rejected_by !== "ALLOCATOR"; const why = byRule ? `<strong>blocked by ${name(RULE_NAME, a.blocked_by || a.rejected_by)}</strong>` : "not chosen — lower margin"; const via = a.channel && a.channel !== "none" ? ` via ${name(CHANNEL_NAME, a.channel)}` : ""; return `<div class="rejected">✕ ${name(ACTION_NAME, a.action)}${via} — ${why}</div>`; }).join("") + '</div>';
      decPanel.append(el("div", null, `<div><strong>${name(ACTION_NAME, d.action_kind)}</strong> <span class="${tagCls}">${tag}</span> ${name(CHANNEL_NAME, d.channel) || ""} — ${inr(d.expected_incr_margin_paise || 0)} expected margin</div>${altsHtml}${d.rules_not_applicable ? `<div class="caption">n/a: ${d.rules_not_applicable.join(", ")}</div>` : ""}`));
    }
    if (decisions.length > 6) box.append(el("div", "caption", `Showing 6 of ${decisions.length} decisions`));
    box.append(decPanel);
  }

  if (decisions.length) {
    const rulePanel = el("div", "panel");
    rulePanel.append(el("h3", null, "Every rule that ran <span class=\"caption\">policy_evaluation · engine.all_rule_ids()</span>"));
    const packs = {};
    for (const d of decisions) for (const e of (d.policy_evaluations || [])) { packs[e.pack] = packs[e.pack] || {}; packs[e.pack][e.rule_id] = packs[e.pack][e.rule_id] || { allow: 0, other: 0 }; if (e.verdict === "allow") packs[e.pack][e.rule_id].allow++; else packs[e.pack][e.rule_id].other++; }
    for (const [pack, rules] of Object.entries(packs)) { rulePanel.append(el("div", "sub", `${PACK_NAME[pack] || pack}`)); for (const [rid, counts] of Object.entries(rules)) rulePanel.append(el("div", null, `${name(RULE_NAME, rid) || rid}: ${counts.allow} allow, ${counts.other} blocked/escalated`)); }
    box.append(rulePanel);
  }

  const allocPanel = el("div", "panel");
  allocPanel.append(el("h3", null, "The allocator <span class=\"caption\">pipeline.py · Allocation.lambda_contact</span>"));
  allocPanel.append(el("div", "sub", `Funded: ${actions.filter(a => a.status === "dispatched" || !a.status).length} · Suppressed: ${actions.filter(a => a.status === "suppressed").length}`));
  box.append(allocPanel);

  if (actions.length) {
    const dispPanel = el("div", "panel");
    dispPanel.append(el("h3", null, "Dispatch <span class=\"caption\">dispatcher.fingerprint</span>"));
    for (const a of actions) dispPanel.append(el("div", "sub", `idem_<hash> · ${name(CHANNEL_NAME, a.channel) || a.kind} · ${a.status || "pending"}`));
    box.append(dispPanel);
  }

  if (outcomes.length) {
    const outPanel = el("div", "panel");
    outPanel.append(el("h3", null, "Outcome <span class=\"caption\">recovery_outcome</span>"));
    for (const o of outcomes) outPanel.append(el("div", "sub", `${o.outcome} · ${inr(o.recovered_paise || 0)}${o.action_id ? "" : " (organic)"}`));
    box.append(outPanel);
  }

  if (audit.length) {
    const audPanel = el("div", "panel");
    audPanel.append(el("h3", null, "Audit <span class=\"caption\">audit_event</span>"));
    for (const a of audit.slice(0, 20)) { const prev = (a.prev_hash || "").slice(0, 12); const h = (a.hash || "").slice(0, 12); audPanel.append(el("div", "sub", `#${a.id} ${a.action} · ${prev}→${h} · ${a.actor} · ${a.created_at?.slice(0, 10) || ""}`)); }
    if (audit.length > 20) audPanel.append(el("div", "caption", `${audit.length} audit rows — see #/evidence`));
    box.append(audPanel);
  }

  if (ground_truth && ground_truth.archetype) {
    const gtPanel = el("div", "panel");
    gtPanel.style.border = "2px dashed var(--ink-mut)";
    gtPanel.append(el("h3", null, 'not visible to any model <span class="caption">features.FORBIDDEN</span>'));
    gtPanel.append(el("div", "sub", `Archetype: ${ground_truth.archetype} · Forbidden: ${(ground_truth.withheld_from_models || []).join(", ")}`));
    gtPanel.append(el("div", "caption", "enforced by yukti.intelligence.features.FeatureLeakage — see tests/integration/test_feature_leakage.py"));
    box.append(gtPanel);
  }
}

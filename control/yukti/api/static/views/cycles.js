/* Cycles view — depends on globals from app.js: $, json, inr, num, el,
 * merchantNames, esc, showView, q.
 *
 * A planning run is not a table. `plan_cycle` writes its result into the audit
 * chain and that is the only record, so this reads the chain back. The columns
 * follow the pipeline in order — considered → stopped → suppressed → dispatched
 * — because the interesting number is rarely the last one: a run that dispatched
 * 127 of 2,293 considered is a run where the stopping rules did most of the work.
 */

async function showCycles() {
  showView("cycles");
  const box = $("cycles-body");
  if (!box) return;
  box.innerHTML = '<p class="empty">Loading planning runs…</p>';
  try {
    renderCycles(box, await json("/cycles" + q({ limit: 50 })));
  } catch (e) {
    box.innerHTML = `<p class="empty">Could not load planning runs: ${e.message}</p>`;
  }
}

// 0 is a result, not a gap. `x || "—"` reported "this run dispatched nothing"
// and "this run recorded no such field" with the same glyph, which are opposite
// claims — the first is the stopping rules working, the second is missing data.
const cell = (v) => (v === null || v === undefined ? "—" : num(v));

function renderCycles(box, runs) {
  box.innerHTML = "";
  if (!runs.length) {
    box.append(el("p", "empty", "No planning runs recorded yet — run `yukti plan` to produce one."));
    return;
  }

  const table = el("table");
  table.innerHTML =
    '<thead><tr><th>Run</th><th>Merchant</th><th>As of</th>' +
    '<th class="num">Considered</th><th class="num">Stopped</th>' +
    '<th class="num">Suppressed</th><th class="num">Dispatched</th>' +
    '<th class="num">Escalated</th><th class="num">λ contact</th>' +
    '<th class="num">Dual bound</th><th class="num">Ratio</th></tr></thead>';
  const tb = el("tbody");

  let missingDuals = 0;
  for (const r of runs) {
    if (r.lambda_contact === null || r.lambda_contact === undefined) missingDuals++;
    const merchant = merchantNames[r.merchant_id] || (r.merchant_id || "—").slice(-8);
    tb.append(el("tr", null,
      `<td><code>${esc(r.id || "—")}</code></td>` +
      `<td>${esc(merchant)}</td>` +
      `<td>${esc((r.as_of || "").slice(0, 10) || "—")}</td>` +
      `<td class="num">${cell(r.considered)}</td>` +
      `<td class="num">${cell(r.stopped)}</td>` +
      `<td class="num">${cell(r.suppressed)}</td>` +
      `<td class="num">${cell(r.dispatched)}</td>` +
      `<td class="num">${cell(r.escalated)}</td>` +
      `<td class="num">${r.lambda_contact != null ? "₹" + (r.lambda_contact / 100).toFixed(2) : "—"}</td>` +
      `<td class="num">${r.dual_bound_paise != null ? inr(r.dual_bound_paise) : "—"}</td>` +
      `<td class="num">${r.optimality_ratio != null ? Number(r.optimality_ratio).toFixed(4) : "—"}</td>`));
  }
  table.append(tb);
  box.append(table);

  box.append(el("div", "caption",
    "λ contact is the shadow price of one more contact: what an action would have " +
    "to be worth before the allocator would fund it. Ratio is the achieved margin " +
    "over the LP dual bound — 1.0000 is optimal."));

  // Said plainly rather than left as a column of dashes. These runs were
  // recorded before the solver's duals were carried out of `allocate()` into
  // the audit detail; the field is written now, but not retroactively.
  if (missingDuals) {
    box.append(el("p", "empty",
      `${missingDuals} of ${runs.length} runs predate the shadow prices being ` +
      `written to the audit detail, and report “—” rather than a guess. ` +
      `A fresh <code>yukti plan</code> records them.`));
  }
}

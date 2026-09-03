/* Evidence view — depends on globals from app.js: $, json, el, num, q,
 * merchantNames, showView.
 *
 * The claim this view has to support is narrow and it should not overreach:
 * every decision the system took was written into a hash chain, and that chain
 * still verifies right now. So the chain is re-walked on request rather than
 * read from a cached verdict, and the panel reports how many rows were checked
 * and per merchant — a bare "✓ intact" with no denominator is indistinguishable
 * from a chain with nothing in it.
 */

async function showEvidence() {
  showView("evidence");
  const box = $("evidence-body");
  if (!box) return;
  box.innerHTML = '<p class="empty">Re-walking the audit chain…</p>';
  const [verify, chain] = await Promise.all([
    json("/audit/verify").catch((e) => ({ error: e.message })),
    json(`/audit/chain${q({ limit: 12 })}`).catch((e) => ({ error: e.message })),
  ]);
  renderEvidence(box, verify, chain);
}

function renderEvidence(box, verify, chain) {
  box.innerHTML = "";

  // --- 1. The verdict, and what it was taken over.
  const v = el("div", "panel");
  v.append(el("h3", null, 'Audit chain <span class="caption">audit.verify_all()</span>'));
  if (!verify || verify.error) {
    v.append(el("p", "empty", `Could not verify the chain: ${verify ? verify.error : "no response"}`));
  } else {
    const intact = verify.intact === true;
    const head = el("p", "verdict " + (intact ? "ok" : "bad"));
    head.innerHTML = intact
      ? `✓ <b>${num(verify.rows)}</b> events across <b>${num(verify.merchants)}</b> merchants — every row hashes to its recorded hash, and every link matches the row before it.`
      : `✕ The chain does not verify. See the affected merchant below.`;
    v.append(head);
    v.append(el("p", "sub", "Re-computed on this request. A cached verdict would be an assertion about a past state of the table, which is the one thing this panel exists not to do."));

    const t = el("table");
    t.innerHTML = '<thead><tr><th>Merchant</th><th class="num">Events</th><th>Verdict</th><th>Detail</th></tr></thead>';
    const tb = el("tbody");
    for (const c of verify.chains || []) {
      const label = merchantNames[c.merchant_id] || c.merchant_id;
      tb.append(el("tr", null,
        `<td>${label}</td><td class="num">${num(c.rows)}</td>` +
        `<td>${c.intact ? '<span class="pill allow">✓ intact</span>' : '<span class="pill block">✕ broken</span>'}</td>` +
        `<td class="sub">${c.intact ? "—" : `row ${c.broken_at}: ${c.reason}`}</td>`));
    }
    t.append(tb);
    v.append(t);
  }
  box.append(v);

  // --- 2. The tip of the chain, so the links are visible rather than asserted.
  const c = el("div", "panel");
  c.append(el("h3", null, 'Chain tip <span class="caption">audit_event</span>'));
  if (!chain || chain.error || !chain.length) {
    c.append(el("p", "empty", chain && chain.error ? `Could not read the chain: ${chain.error}` : "No audit events yet."));
  } else {
    c.append(el("p", "sub", "Newest first — which is the reverse of the direction the hashes link, so each row's <code>prev</code> is the <code>hash</code> of the row below it."));
    const t = el("table");
    t.innerHTML = '<thead><tr><th>#</th><th>When</th><th>Actor</th><th>Action</th><th>prev →</th><th>hash</th></tr></thead>';
    const tb = el("tbody");
    for (const r of chain) {
      tb.append(el("tr", null,
        `<td class="num">${r.id}</td>` +
        `<td class="sub">${(r.created_at || "").slice(0, 19).replace("T", " ")}</td>` +
        `<td>${r.actor}</td><td>${r.action}</td>` +
        `<td><code>${(r.prev_hash || "—").slice(0, 10)}</code></td>` +
        `<td><code>${(r.hash || "").slice(0, 10)}</code></td>`));
    }
    t.append(tb);
    c.append(t);
  }
  box.append(c);
}

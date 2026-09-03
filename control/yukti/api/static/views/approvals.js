/* Approvals — depends on globals from app.js: $, json, inr, num, el, name,
 * ACTION_NAME, CHANNEL_NAME, RULE_NAME, esc, rupeeify, q, showView,
 * refreshApprovalBadge.
 *
 * The one surface in the console that does something. Every other view reports;
 * this one asks a person to decide, and records the decision against their name.
 *
 * Each card leads with the money, then the action waiting to go out, then what
 * the allocator rejected to arrive at it. The rejected alternatives are the
 * point: a reviewer approving a WhatsApp message should be able to see that a
 * voice call was considered and declined on margin, not left out by accident.
 */

let approvalRows = [];

async function showApprovals() {
  showView("approvals");
  const box = $("approvals-body");
  if (!box) return;
  box.innerHTML = '<p class="empty">Loading the queue…</p>';
  try {
    approvalRows = await json(`/approvals${q()}`);
    renderApprovals(box);
  } catch (e) {
    box.innerHTML = `<p class="empty">Could not load the queue: ${e.message}</p>`;
  }
}

function approverName() {
  // Persisted so a reviewer names themselves once per browser rather than on
  // every card. It is sent with every decision and written to the audit chain.
  let who = localStorage.getItem("niyama-approver") || "";
  if (!who) {
    who = "reviewer@merchant";
    try { localStorage.setItem("niyama-approver", who); } catch { /* private mode */ }
  }
  return who;
}

function renderApprovals(box) {
  box.innerHTML = "";

  if (!approvalRows.length) {
    box.append(el("p", "empty", "Nothing is waiting on a human right now."));
    return;
  }

  const total = approvalRows.reduce((a, r) => a + (r.amount_paise || 0), 0);
  const margin = approvalRows.reduce((a, r) => a + (r.expected_incr_margin_paise || 0), 0);

  const head = el("div", "queue-head");
  head.append(tile("Waiting on you", num(approvalRows.length), "cases held for review"));
  head.append(tile("Money held", inr(total), "not moving until someone decides"));
  head.append(tile("Margin at stake", inr(margin, { signed: true }),
                   "if every proposal were approved", "pos"));
  box.append(head);

  const who = el("label", "field approver");
  who.append(el("span", null, "Approving as"));
  const input = el("input");
  input.type = "text";
  input.value = approverName();
  input.setAttribute("aria-label", "Your name, recorded with every decision");
  input.onchange = () => {
    try { localStorage.setItem("niyama-approver", input.value.trim()); } catch { /* ignore */ }
  };
  who.append(input);
  who.append(el("span", "approver-note", "recorded in the audit chain with every decision"));
  box.append(who);

  for (const row of approvalRows) box.append(approvalCard(row, box));
}

function approvalCard(row, box) {
  const card = el("article", "approval");
  card.dataset.case = row.case_id;

  const top = el("div", "approval-top");
  top.innerHTML =
    `<div class="approval-money">${inr(row.amount_paise)}</div>` +
    `<div class="approval-who"><b>${esc(row.merchant_name)}</b>` +
    `<span class="sub">${esc(name(SURFACE_NAME, row.obligation_kind))} · ` +
    `${row.decline_code ? esc(String(row.decline_code).replace(/_/g, " ").toLowerCase()) : "—"}` +
    `${row.rail ? " · " + esc(row.rail) : ""}</span></div>`;
  card.append(top);

  const prop = el("div", "approval-prop");
  prop.innerHTML =
    `<span class="lbl">Proposed</span> ` +
    `<b>${esc(name(ACTION_NAME, row.action_kind))}</b>` +
    `${row.channel && row.channel !== "none" ? ` via ${esc(name(CHANNEL_NAME, row.channel))}` : ""}` +
    `${row.scheduled_for ? ` · ${esc(String(row.scheduled_for).slice(0, 10))}` : ""}` +
    ` · expected margin <b class="pos">${esc(row.margin_display)}</b>`;
  card.append(prop);

  if (row.reason) {
    const reason = el("p", "approval-reason");
    reason.textContent = row.reason;   // text, never markup
    card.append(reason);
  }

  for (const r of row.rules || []) {
    card.append(el("div", "approval-rule",
      `<span class="pill escalate">held</span> <code>${esc(r.rule_id)}</code> ${rupeeify(esc(r.reason))}`));
  }

  const alts = row.alternatives_rejected || [];
  if (alts.length) {
    const d = el("details", "approval-alts");
    d.append(el("summary", null, `${alts.length} alternatives rejected`));
    for (const a of alts) {
      const why = a.blocked_by
        ? `blocked by <code>${esc(a.blocked_by)}</code> — ${esc(a.reason || "")}`
        : `${a.rejected_by ? esc(a.rejected_by.toLowerCase()) : "not chosen"}${a.reason ? " — " + esc(a.reason) : ""}`;
      d.append(el("div", "rejected",
        `✕ ${esc(name(ACTION_NAME, a.action))}${a.channel && a.channel !== "none" ? " via " + esc(name(CHANNEL_NAME, a.channel)) : ""} — ${why}`));
    }
    card.append(d);
  }

  const actions = el("div", "approval-actions");
  const status = el("span", "approval-status");
  const approve = el("button", "btn primary", "Approve");
  const reject = el("button", "btn", "Reject");
  approve.type = reject.type = "button";
  approve.onclick = () => decide(row, "approve", card, status, [approve, reject], box);
  reject.onclick = () => decide(row, "reject", card, status, [approve, reject], box);
  actions.append(approve, reject, status);
  card.append(actions);
  return card;
}

async function decide(row, verdict, card, status, buttons, box) {
  const actor = (localStorage.getItem("niyama-approver") || "").trim();
  if (!actor) {
    status.className = "approval-status bad";
    status.textContent = "Name yourself first — every decision is recorded against someone.";
    return;
  }
  buttons.forEach((b) => (b.disabled = true));
  status.className = "approval-status";
  status.textContent = verdict === "approve" ? "Re-checking the rules…" : "Recording…";

  let resp;
  try {
    resp = await fetch(`/approvals/${encodeURIComponent(row.case_id)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verdict, actor, note: "" }),
    });
  } catch (e) {
    buttons.forEach((b) => (b.disabled = false));
    status.className = "approval-status bad";
    status.textContent = `Could not reach the server: ${e.message}`;
    return;
  }

  const body = await resp.json().catch(() => ({}));

  if (!resp.ok) {
    // The interesting case, and the reason this is not a fire-and-forget
    // button: the reviewer approved and a rule still said no. Show which.
    buttons.forEach((b) => (b.disabled = false));
    const detail = body.detail || {};
    card.classList.add("refused");
    status.className = "approval-status bad";
    status.innerHTML =
      `<b>Refused.</b> ${esc(detail.message || "the policy engine did not permit this")}`;
    return;
  }

  card.classList.add("done");
  status.className = "approval-status ok";
  status.innerHTML = body.verdict === "approved"
    ? `<b>Approved</b> — ${body.dispatched ? "sent" : "recorded, not dispatched"}, signed by ${esc(actor)}`
    : `<b>Rejected</b> — case stopped, nothing sent`;

  approvalRows = approvalRows.filter((r) => r.case_id !== row.case_id);
  refreshApprovalBadge(approvalRows.length);
  // The card stays on screen with its outcome rather than vanishing: a queue
  // that silently removes the row you just acted on gives no confirmation that
  // the thing you intended is the thing that happened.
}

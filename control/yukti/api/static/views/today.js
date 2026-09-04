/* Today — depends on globals from app.js: $, json, inr, num, el, esc, q,
 * merchantNames, merchantId, liftCache.
 *
 * The landing surface. Everything else in this console reports; this answers
 * the two questions somebody actually arrives with — what did you do for me,
 * and what needs me — and it answers them above the fold, in four lines.
 *
 * The order is deliberate. The result first, because the whole product claim is
 * that recovery should be measured net of what would have happened anyway. Then
 * the queue, because it is the only thing here that is waiting on a person.
 * Then what we declined to chase, which is the part no other recovery tool can
 * show. Money still at risk comes last: it is context, not a task.
 */

async function loadToday() {
  const box = $("today-body");
  if (!box) return;

  const [risk, notChased, approvals] = await Promise.all([
    json(`/metrics/revenue-at-risk${q()}`).catch(() => null),
    json(`/metrics/not-chased${q()}`).catch(() => null),
    json(`/approvals${q()}`).catch(() => []),
  ]);
  renderToday(box, risk, notChased, approvals || []);
}

function todayScope() {
  const el_ = $("today-scope");
  if (!el_) return;
  // The selector's scope, not the evaluation's. Three of the four rows are
  // whatever the selector says; only the receipt is tied to the merchant the
  // last evaluation ran on, and that row names its own scope. Showing the
  // evaluation's merchant up here made the whole screen look like one book.
  const asOf = (liftCache?.as_of || "").slice(0, 10);
  const who = merchantId ? merchantNames[merchantId] : "All merchants";
  el_.textContent = [who, asOf].filter(Boolean).join(" · ");
}

/* The evaluation runs on one merchant's book at a time. When the rest of the
 * screen is showing something wider, or something else, say so on the row that
 * is affected rather than letting the reader assume one scope for all four. */
function evalScopeNote() {
  if (!liftCache || liftCache.source === "demo-light") return "";
  const on = merchantNames[liftCache.merchant_id];
  if (!on) return "";
  if (!merchantId) return ` · measured on ${on}, not the whole book`;
  if (merchantId !== liftCache.merchant_id) return ` · measured on ${on}, not this merchant`;
  return "";
}

/* One statement, one number. `tone` colours the number, never the label —
 * money created is good news, money at risk is not news at all. */
function todayRow({ label, sub, value, tone = "", action = null }) {
  const row = el("div", "today-row" + (action ? " actionable" : ""));
  const left = el("div", "today-left");
  left.append(el("div", "today-label", label));
  if (sub) left.append(el("div", "today-sub", sub));
  row.append(left);

  const right = el("div", "today-right");
  right.append(el("div", "today-value" + (tone ? " " + tone : ""), value));
  if (action) {
    const a = el("a", "today-cta", action.label);
    a.href = action.href;
    right.append(a);
  }
  row.append(right);
  return row;
}

function renderToday(box, risk, notChased, approvals) {
  box.innerHTML = "";
  todayScope();

  // --- 1. The receipt.
  const y = liftCache?.arms?.find((a) => a.key === "Y");
  if (y) {
    const light = liftCache.source === "demo-light";
    const caused = y.recovered_cases - y.would_have_recovered_anyway;
    box.append(todayRow({
      label: light
        ? "Earned from contacting, above retrying alone"
        : `We caused ${num(caused)} recoveries`,
      sub: light
        ? `${num(y.contacts)} contacts, measured in the service-free demo world`
        : `${num(y.would_have_recovered_anyway)} of ${num(y.recovered_cases)} would have paid anyway`
          + evalScopeNote(),
      value: inr(light ? y.contact_incremental_paise : y.net_incremental_paise, { signed: true }),
      tone: "good",
    }));
  } else {
    box.append(todayRow({
      label: "No evaluation yet",
      sub: "Run `make eval` to measure what the system actually caused",
      value: "—",
    }));
  }

  // --- 2. The only thing waiting on a person.
  const held = approvals.reduce((a, r) => a + (r.amount_paise || 0), 0);
  box.append(todayRow({
    label: approvals.length
      ? `${num(approvals.length)} need your approval`
      : "Nothing needs your approval",
    sub: approvals.length
      ? `above the amount ${merchantId ? "this merchant" : "these merchants"} let us act on alone`
      : "every action was inside the limits you set",
    value: approvals.length ? inr(held) : "—",
    tone: approvals.length ? "warn" : "",
    action: approvals.length ? { label: "Review", href: "#/approvals" } : null,
  }));

  // --- 3. The claim no other recovery tool makes.
  if (notChased) {
    const cases = (notChased.stopped_by_rule || []).reduce((s, r) => s + r.cases, 0);
    box.append(todayRow({
      label: `Walked away from ${num(cases)} cases`,
      sub: "each stopped by a named rule, not by running out of budget",
      value: inr(notChased.stopped_total_paise),
    }));
  }

  // --- 4. Context, not a task.
  if (risk) {
    box.append(todayRow({
      label: "Money still at risk",
      sub: `${num(risk.total_cases)} open obligations across every surface`,
      value: inr(risk.total_paise),
    }));
  }
}

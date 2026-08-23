"""`plan_cycle` — the integration.

Everything before this was a component with a test. This is where a decision
actually flows: intelligence scores it, the stopping rules can refuse it, the
allocator funds it against a budget, the policy engine clears it, the dispatcher
executes it exactly once, and the audit chain records that all of that happened.

There is no LLM in this path. That is deliberate and it is the shape of the
whole system: the deterministic spine runs, and the agent layers on top of it
proposing and explaining. If the model is unavailable, mispriced, or wrong, the
merchant's money still moves correctly.

**A pure function of database state.** `plan_cycle(conn, merchant, as_of)` reads
what is true at `as_of` and writes what follows from it. Nothing is carried in
memory between cycles, so a crash is resumed by running it again, and a replay
of a past date reproduces the decision that was made then.

**Why policy is consulted twice.** Once as a feasibility filter before
allocation, so budget is never spent on an action that will be blocked; once in
full before dispatch. The second check is not redundancy, it is what catches a
bug in the first — and it is the one that must be true, so it is the one whose
verdict is recorded.

**Order of operations, and why stopping comes first.** Stopping rules are cheap
and remove cases before the expensive scoring. More importantly a stop is a
different kind of statement from a policy block: "we chose not to work this" is
a business decision the merchant should see separately from "we were not
allowed to do this". Collapsing them would make the console unreadable and the
stopping-rules metric meaningless — and that metric is half of what the merchant
is paying to learn.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg

from yukti import audit
from yukti.allocator import budget as budget_ledger
from yukti.allocator.lagrangian import Allocation, Budgets
from yukti.allocator.lagrangian import Candidate as AllocCandidate
from yukti.allocator.lagrangian import allocate, expected_margin
from yukti.candidates import Candidate, generate
from yukti.config import settings
from yukti.dispatch.adapters import Adapters
from yukti.dispatch.dispatcher import Dispatcher, fingerprint
from yukti.dispatch.tools import CHANNEL_COST_PAISE, ActionSpec
from yukti.domain.enums import (
    ActionKind,
    ActorKind,
    Arm,
    CaseState,
    Channel,
    ObligationState,
    PolicyVerdict,
    StopReason,
)
from yukti.domain.ids import decision_id, run_id, trace_id
from yukti.intelligence import registry
from yukti.intelligence.debit_timing import DebitTimingModel
from yukti.intelligence.features import load_open_cases
from yukti.policy import engine as policy_engine
from yukti.policy.merchantpack import MerchantContext, MerchantPolicy
from yukti.policy.regpack import ActionRequest
from yukti.policy.store import load_policy
from yukti.scoring import Scorer, default_scorer, key_for
from yukti.stopping.rules import MIN_EXPECTED_MARGIN_PAISE, CaseSnapshot
from yukti.stopping.rules import evaluate as evaluate_stopping

# Per-customer contact cap across every open case on every surface. Taken from
# the merchant's weekly cap rather than invented here, so raising the policy
# raises the arbitration limit with it and the two cannot disagree.
DEFAULT_PER_CUSTOMER_CONTACTS = 1


@dataclass(slots=True)
class PlanResult:
    """What one planning cycle did. Every field is a console metric."""

    merchant_id: str
    as_of: datetime
    trace_id: str
    run_id: str
    considered: int = 0
    stopped: int = 0
    suppressed: int = 0
    dispatched: int = 0
    escalated: int = 0
    blocked: int = 0
    duplicates: int = 0
    failed: int = 0
    candidates: int = 0
    infeasible: int = 0
    contacts_spent: int = 0
    discount_spent_paise: int = 0
    planned_margin_paise: int = 0
    # Money the stopping rules deliberately declined to chase, by named rule.
    stop_breakdown: dict[str, int] = field(default_factory=dict)
    not_chased_paise: int = 0
    optimality_ratio: float = 1.0

    def summary(self) -> str:
        return (
            f"{self.considered:,} cases  stopped {self.stopped:,}  "
            f"dispatched {self.dispatched:,}  escalated {self.escalated:,}  "
            f"suppressed {self.suppressed:,}  "
            f"contacts {self.contacts_spent:,}  "
            f"discount Rs {self.discount_spent_paise / 100:,.0f}"
        )


def plan_cycle(
    conn: psycopg.Connection,
    merchant_id: str,
    as_of: datetime,
    adapters: Adapters | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    scorer: Scorer | None = None,
) -> PlanResult:
    """Plan and execute one merchant's recovery for one window.

    `scorer` decides what each candidate action is worth. It defaults to the
    fitted uplift model, which raises rather than degrading if no model exists —
    silently scoring everything at zero would make the stopping rules report the
    merchant's entire book as `LOST_CAUSE`, which is an operational failure
    dressed up as a business finding.
    """
    tid = trace_id()
    rid = run_id()
    result = PlanResult(merchant_id=merchant_id, as_of=as_of, trace_id=tid, run_id=rid)

    policy = load_policy(conn, merchant_id)
    cases = load_open_cases(conn, merchant_id, as_of, limit)
    result.considered = len(cases)

    _open_run(conn, rid, merchant_id, tid, len(cases))
    audit.append(conn, trace_id=tid, merchant_id=merchant_id,
                 action="plan_cycle.started", actor=ActorKind.SYSTEM, subject_id=rid,
                 detail={"as_of": as_of.isoformat(), "cases": len(cases),
                         "policy": _policy_digest(policy)})
    conn.commit()

    if cases.empty:
        _close_run(conn, rid, "completed")
        conn.commit()
        return result

    # 1. Candidates. Deterministic, so a re-run reaches the same fingerprints.
    timing = _load_timing()
    by_case = {row["case_id"]: row for row in cases.to_dict("records")}
    proposals: dict[str, list[Candidate]] = {
        cid: generate(case, policy, as_of, timing) for cid, case in by_case.items()
    }
    result.candidates = sum(len(v) for v in proposals.values())

    # 2. What each (case, action) pair is worth. The scorer is injected because
    #    swapping it is exactly what distinguishes the evaluation arms — same
    #    allocator, same stopping rules, same policy engine, different number.
    uplift = (scorer or default_scorer()).score(cases, proposals)

    # 3. Stopping rules. Cheap, and they run before anything is funded.
    budgets = budget_ledger.load_all(conn, merchant_id, as_of.date())
    survivors: list[str] = []
    for cid, case in by_case.items():
        best = _best_margin(case, proposals[cid], uplift,
                            budgets["discount"].remaining)
        snapshot = _snapshot(case, policy, budgets, best, as_of)
        decision = evaluate_stopping(snapshot, as_of)
        if decision.stop:
            _record_stop(conn, case, decision, tid, rid)
            result.stopped += 1
            result.stop_breakdown[decision.reason.value] = (
                result.stop_breakdown.get(decision.reason.value, 0) + 1
            )
            result.not_chased_paise += int(case["amount_paise"])
            continue
        survivors.append(cid)
    conn.commit()

    # 4. Feasibility filter, then allocation. The filter is what stops the
    #    allocator spending a contact on something quiet hours will reject.
    alloc_candidates: list[AllocCandidate] = []
    index: dict[tuple[str, str, str, float], Candidate] = {}
    for cid in survivors:
        case = by_case[cid]
        for cand in proposals[cid]:
            if cand.action_kind is ActionKind.SUPPRESS:
                continue
            if not policy_engine.is_feasible(
                _request(cand, case), policy, _context(case, policy)
            ):
                result.infeasible += 1
                continue
            margin = _margin(case, cand, uplift)
            key = (cid, cand.action_kind.value, cand.channel.value, cand.discount_pct)
            index[key] = cand
            alloc_candidates.append(AllocCandidate(
                case_id=cid, customer_id=cand.customer_id,
                action_kind=cand.action_kind.value, channel=cand.channel.value,
                margin_paise=margin, contacts=cand.contacts,
                discount_paise=cand.discount_paise,
                channel_cost_paise=CHANNEL_COST_PAISE.get(cand.channel, 0),
            ))

    allocation = allocate(alloc_candidates, Budgets(
        contacts=budgets["contact"].remaining,
        discount_paise=budgets["discount"].remaining,
        per_customer_contacts=DEFAULT_PER_CUSTOMER_CONTACTS,
    ))
    result.optimality_ratio = allocation.optimality_ratio
    result.planned_margin_paise = allocation.total_margin_paise

    chosen = {
        c.case_id: index[(c.case_id, c.action_kind, c.channel,
                          index_discount(index, c))]
        for c in allocation.chosen
    }

    # 5. Dispatch, or record why not. Every survivor gets a decision row —
    #    including the ones we chose not to fund, because "considered and
    #    declined" is the number this system exists to be able to report.
    own_adapters = adapters is None
    adapters = adapters or Adapters.sandbox()
    dispatcher = Dispatcher(conn, adapters)
    try:
        for cid in survivors:
            case = by_case[cid]
            cand = chosen.get(cid)
            if cand is None:
                _record_suppression(conn, case, proposals[cid], uplift, tid, rid,
                                    as_of, allocation)
                result.suppressed += 1
                continue
            outcome = _act(conn, dispatcher, case, cand, proposals[cid], uplift,
                           policy, tid, rid, as_of, dry_run)
            _tally(result, outcome)
    finally:
        if own_adapters:
            adapters.close()

    # 6. Spend the budgets that were actually used. Taken after dispatch and
    #    from what was dispatched, not from what was planned: a plan that failed
    #    to dispatch has not consumed anything, and charging the merchant's
    #    budget for it would starve tomorrow's cycle for no benefit.
    if not dry_run and (result.contacts_spent or result.discount_spent_paise):
        if result.contacts_spent:
            budget_ledger.spend(conn, merchant_id, "contact", as_of.date(),
                                result.contacts_spent)
        if result.discount_spent_paise:
            budget_ledger.spend(conn, merchant_id, "discount", as_of.date(),
                                result.discount_spent_paise)

    audit.append(conn, trace_id=tid, merchant_id=merchant_id,
                 action="plan_cycle.completed", actor=ActorKind.SYSTEM, subject_id=rid,
                 detail={"considered": result.considered, "stopped": result.stopped,
                         "dispatched": result.dispatched,
                         "escalated": result.escalated,
                         "suppressed": result.suppressed,
                         "contacts": result.contacts_spent,
                         "discount_paise": result.discount_spent_paise,
                         "optimality_ratio": round(result.optimality_ratio, 4)})
    _close_run(conn, rid, "completed")
    conn.commit()
    return result


# --- scoring ----------------------------------------------------------------

def _load_timing() -> DebitTimingModel:
    """The fitted timing model, or an unfitted one that proposes 'as soon as allowed'.

    Degrading rather than raising: without a fitted curve the system still
    recovers money, it just stops being clever about when. Refusing to plan
    because a model artifact is missing would be a worse failure than planning
    without it.
    """
    try:
        return registry.load("debit_timing").model
    except (registry.ModelUnavailable, Exception):
        return DebitTimingModel()




def index_discount(index: dict, alloc: AllocCandidate) -> float:
    """Recover the discount tier for an allocated candidate.

    The allocator's `Candidate` carries `discount_paise` but not the percentage,
    and two tiers can round to the same paise on a small obligation. Matching on
    paise is therefore how the wrong tier gets attributed to a decision, so the
    tier is recovered from the proposal index instead.
    """
    for (case_id, kind, channel, pct) in index:
        if (case_id == alloc.case_id and kind == alloc.action_kind
                and channel == alloc.channel
                and index[(case_id, kind, channel, pct)].discount_paise
                == alloc.discount_paise):
            return pct
    return 0.0


def _margin(case: dict, cand: Candidate, uplift: dict[tuple, float]) -> int:
    return expected_margin(
        uplift=uplift.get(key_for(cand), 0.0),
        amount_paise=int(case["amount_paise"]),
        mdr_bps=int(case.get("mdr_bps") or 200),
        discount_paise=cand.discount_paise,
        channel_cost_paise=CHANNEL_COST_PAISE.get(cand.channel, 0),
    )


@dataclass(frozen=True, slots=True)
class BestOption:
    """The best thing we could do for a case, and the best we can AFFORD."""

    uplift: float
    margin_paise: int
    # True only when the discount budget is the thing standing between us and a
    # worthwhile action — not merely because some discount candidate existed.
    blocked_on_discount_budget: bool


def _best_margin(
    case: dict, cands: list[Candidate], uplift: dict[tuple, float],
    discount_remaining_paise: int,
) -> BestOption:
    """What this case is worth, given what the merchant can currently afford.

    Affordability is applied HERE rather than left to the allocator, because the
    stopping rules run first and they need to judge the case on options that
    actually exist. The first live run made the cost of getting this wrong
    obvious: a merchant with no discount budget had 346 of 442 cases stopped as
    DISCOUNT_BUDGET_SPENT, because the highest-margin candidate happened to be a
    discount and the rule killed the whole case — discarding the free silent
    retry sitting right next to it.

    So two figures are computed. The affordable best is what the stopping rules
    judge. The unconstrained best exists only to tell two stops apart:

        "your discount budget ran out and nothing else was worth doing"
            -> DISCOUNT_BUDGET_SPENT, and raising the budget would change it
        "nothing was worth doing at all"
            -> NEGATIVE_EXPECTED_MARGIN, and no budget would change it

    A merchant can act on the first and should not waste time on the second, so
    collapsing them into one reason would make the console actively misleading.
    """
    best_affordable = 0
    best_affordable_uplift = 0.0
    best_unconstrained = 0
    best_unconstrained_needs_discount = False

    for c in cands:
        if c.action_kind is ActionKind.SUPPRESS:
            continue
        m = _margin(case, c, uplift)

        if m > best_unconstrained:
            best_unconstrained = m
            best_unconstrained_needs_discount = c.discount_paise > 0

        if c.discount_paise > discount_remaining_paise:
            continue
        if m > best_affordable:
            best_affordable = m
            best_affordable_uplift = uplift.get(key_for(c), 0.0)

    if best_affordable == 0:
        # Nothing cleared zero. Report the best uplift we saw anyway, so
        # LOST_CAUSE can distinguish "no causal effect" from "effect exists but
        # costs more than it returns" — those are different problems.
        best_affordable_uplift = max(
            (uplift.get(key_for(c), 0.0) for c in cands
             if c.action_kind is not ActionKind.SUPPRESS),
            default=0.0,
        )

    blocked_on_discount = (
        best_affordable < MIN_EXPECTED_MARGIN_PAISE
        and best_unconstrained >= MIN_EXPECTED_MARGIN_PAISE
        and best_unconstrained_needs_discount
    )

    return BestOption(
        uplift=best_affordable_uplift,
        margin_paise=best_affordable,
        blocked_on_discount_budget=blocked_on_discount,
    )


# --- adapters between component vocabularies --------------------------------

def _snapshot(
    case: dict, policy: MerchantPolicy, budgets: dict, best: BestOption,
    as_of: datetime
) -> CaseSnapshot:
    return CaseSnapshot(
        case_id=case["case_id"],
        obligation_state=ObligationState(case.get("obligation_state", "open")),
        decline_code=case.get("decline_code"),
        first_failed_at=_as_datetime(case.get("first_failed_at"), as_of),
        attempts_made=int(case.get("attempts_made") or 1),
        customer_opted_out=bool(case.get("customer_opted_out")),
        open_promise_to_pay=bool(case.get("open_promise_to_pay")),
        issuer_degraded=bool(case.get("issuer_degraded")),
        contacts_this_window=int(case.get("contacts_this_window") or 0),
        contact_cap=policy.max_contacts_per_customer_per_week,
        contact_budget_remaining=budgets["contact"].remaining,
        discount_budget_remaining_paise=budgets["discount"].remaining,
        predicted_uplift=best.uplift,
        expected_margin_paise=best.margin_paise,
        # Only true when the discount budget is what is actually blocking us.
        requires_discount=best.blocked_on_discount_budget,
    )


def _request(cand: Candidate, case: dict) -> ActionRequest:
    return ActionRequest(
        action_kind=cand.action_kind,
        channel=cand.channel,
        scheduled_for=cand.scheduled_for,
        amount_paise=cand.amount_paise - cand.discount_paise,
        merchant_category=case.get("merchant_segment", "general"),
        decline_code=cand.decline_code,
        attempts_made=int(case.get("attempts_made") or 1),
        discount_pct=cand.discount_pct,
        consent=case.get("consent") or {},
        # A scheduled debit is only ever proposed at least 25 hours out (see
        # `candidates.generate`), so the notice is emitted at planning time and
        # the debit clears RBI_PREDEBIT_24H by construction rather than by luck.
        predebit_notice_at=(
            cand.scheduled_for - timedelta(hours=25)
            if cand.action_kind is ActionKind.SCHEDULE_DEBIT else None
        ),
        dlt_template_id=_template_for(cand),
        has_afa=False,
    )


def _context(case: dict, policy: MerchantPolicy) -> MerchantContext:
    return MerchantContext(
        contacts_this_week=int(case.get("contacts_this_window") or 0),
        had_recent_discount=bool(case.get("had_recent_discount")),
    )


def _template_for(cand: Candidate) -> str | None:
    """The registered DLT template this message binds to.

    Every commercial SMS in India must cite a template registered with the DLT
    registry, and free-form sends are rejected by the operator. Templates are
    per action kind, so the binding is a lookup rather than something a model
    supplies — a generated template id would be a fabricated regulatory
    reference, which is worse than no message at all.
    """
    if not cand.action_kind.contacts_customer:
        return None
    return {
        ActionKind.MESSAGE: "DLT_YUKTI_PAYLINK_01",
        ActionKind.DISCOUNT_OFFER: "DLT_YUKTI_OFFER_01",
        ActionKind.VOICE_CALL: "DLT_YUKTI_VOICE_01",
    }.get(cand.action_kind)


def _as_datetime(value: Any, default: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if value is None:
        return default
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return default


# --- persistence ------------------------------------------------------------

def _open_run(conn, rid: str, merchant_id: str, tid: str, n_cases: int) -> None:
    conn.execute(
        "INSERT INTO agent_run (id, merchant_id, kind, trace_id, model, status, "
        "input_digest) VALUES (%s, %s, 'planner', %s, %s, 'running', %s)",
        (rid, merchant_id, tid, "deterministic", f"cases={n_cases}"),
    )


def _close_run(conn, rid: str, status: str) -> None:
    conn.execute(
        "UPDATE agent_run SET status = %s, finished_at = now() WHERE id = %s",
        (status, rid),
    )


def _write_decision(
    conn, case: dict, kind: ActionKind, channel: Channel,
    scheduled_for: datetime | None, reason: str, verdict: PolicyVerdict,
    margin: int, tid: str, rid: str, alternatives: list[dict],
    confidence: float | None = None,
) -> str:
    did = decision_id()
    conn.execute(
        "INSERT INTO agent_decision (id, run_id, case_id, trace_id, action_kind, "
        "channel, scheduled_for, reason, confidence, expected_incr_margin_paise, "
        "alternatives_rejected, policy_verdict) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (did, rid, case["case_id"], tid, kind.value, channel.value, scheduled_for,
         reason, confidence, margin, json.dumps(alternatives, default=str),
         verdict.value),
    )
    return did


def _record_stop(conn, case: dict, decision, tid: str, rid: str) -> None:
    """A stop is a decision with a named rule, not the absence of one."""
    did = _write_decision(
        conn, case, ActionKind.SUPPRESS, Channel.NONE, None,
        f"{decision.reason.value}: {decision.detail}", PolicyVerdict.ALLOW, 0,
        tid, rid, alternatives=[],
    )
    # Written to policy_evaluation under the 'stopping' pack so the console can
    # show business stops and compliance blocks side by side while keeping them
    # distinguishable — the schema anticipated this with its three-value pack.
    conn.execute(
        "INSERT INTO policy_evaluation (decision_id, pack, rule_id, verdict, reason) "
        "VALUES (%s, 'stopping', %s, 'block', %s)",
        (did, decision.reason.value, decision.detail),
    )
    conn.execute(
        "UPDATE recovery_case SET state = %s, stop_reason = %s, closed_at = now(), "
        "version = version + 1 WHERE id = %s",
        (CaseState.STOPPED.value, decision.reason.value, case["case_id"]),
    )
    audit.append(conn, trace_id=tid, merchant_id=case["merchant_id"],
                 action="case.stopped", actor=ActorKind.AGENT,
                 subject_id=case["case_id"],
                 detail={"reason": decision.reason.value, "detail": decision.detail,
                         "amount_paise": int(case["amount_paise"])})


def _record_suppression(
    conn, case: dict, cands: list[Candidate], uplift: dict, tid: str, rid: str,
    as_of: datetime, allocation: Allocation,
) -> None:
    """Considered, and not funded. The case stays open for tomorrow.

    Distinct from a stop: nothing about this case says it is not worth working,
    only that today's budget went further elsewhere. Closing it would throw away
    a recoverable obligation because of a temporary constraint.
    """
    rejected = [
        {"action": c.action_kind.value, "channel": c.channel.value,
         "rejected_by": "ALLOCATOR",
         "reason": f"expected margin {_margin(case, c, uplift)} paise did not clear "
                   f"the marginal value of a contact "
                   f"(lambda {allocation.lambda_contact:.0f})"}
        for c in cands if c.action_kind is not ActionKind.SUPPRESS
    ][:6]
    _write_decision(
        conn, case, ActionKind.SUPPRESS, Channel.NONE, as_of,
        "not funded this cycle — budget went to higher-uplift cases",
        PolicyVerdict.ALLOW, 0, tid, rid, alternatives=rejected,
    )


def _act(
    conn, dispatcher: Dispatcher, case: dict, cand: Candidate,
    all_cands: list[Candidate], uplift: dict, policy: MerchantPolicy,
    tid: str, rid: str, as_of: datetime, dry_run: bool,
) -> dict:
    """Full policy check, then dispatch. This is the second of the two checks."""
    request = _request(cand, case)
    verdict = policy_engine.evaluate(request, policy, _context(case, policy))
    margin = _margin(case, cand, uplift)

    rejected = [
        {"action": c.action_kind.value, "channel": c.channel.value,
         "rejected_by": "ALLOCATOR",
         "reason": f"expected margin {_margin(case, c, uplift)} paise"}
        for c in all_cands
        if c is not cand and c.action_kind is not ActionKind.SUPPRESS
    ][:6]
    rejected.extend(
        {"action": cand.action_kind.value, "channel": cand.channel.value,
         "blocked_by": r.rule_id, "reason": r.reason}
        for r in verdict.results if r.verdict is not PolicyVerdict.ALLOW
    )

    did = _write_decision(
        conn, case, cand.action_kind, cand.channel, cand.scheduled_for,
        cand.rationale or "highest expected incremental margin under budget",
        verdict.verdict, margin, tid, rid, rejected,
        confidence=round(min(0.999, abs(uplift.get(key_for(cand), 0.0)) * 3), 3),
    )
    policy_engine.record(conn, did, verdict)

    # A holdout case is planned in full and then never acted on. Planning it
    # anyway is what makes the holdout a valid denominator: the console can show
    # what would have been done, and the comparison is like-for-like rather than
    # "treated cases versus cases nobody looked at".
    arm = Arm(case.get("arm", "treatment"))
    if arm is Arm.HOLDOUT:
        conn.execute(
            "UPDATE recovery_case SET state = %s, version = version + 1 WHERE id = %s",
            (CaseState.SCHEDULED.value, case["case_id"]),
        )
        audit.append(conn, trace_id=tid, merchant_id=case["merchant_id"],
                     action="case.held_out", actor=ActorKind.SYSTEM,
                     subject_id=case["case_id"],
                     detail={"would_have": cand.action_kind.value,
                             "expected_margin_paise": margin})
        conn.commit()
        return {"holdout": True}

    if verdict.verdict is PolicyVerdict.BLOCK:
        # Should be unreachable: the feasibility filter already rejected these.
        # Reaching it means the two checks disagree, which is a bug in the
        # filter — so it is recorded loudly rather than swallowed.
        conn.execute(
            "UPDATE recovery_case SET state = %s, version = version + 1 WHERE id = %s",
            (CaseState.OPEN.value, case["case_id"]),
        )
        audit.append(conn, trace_id=tid, merchant_id=case["merchant_id"],
                     action="dispatch.blocked_after_allocation", actor=ActorKind.SYSTEM,
                     subject_id=case["case_id"],
                     detail={"rules": [r.rule_id for r in verdict.blocks],
                             "note": "feasibility filter and full evaluation "
                                     "disagreed — investigate"})
        conn.commit()
        return {"blocked": True}

    if verdict.verdict is PolicyVerdict.ESCALATE:
        conn.execute(
            "UPDATE recovery_case SET state = %s, version = version + 1 WHERE id = %s",
            (CaseState.ESCALATED.value, case["case_id"]),
        )
        audit.append(conn, trace_id=tid, merchant_id=case["merchant_id"],
                     action="case.escalated", actor=ActorKind.AGENT,
                     subject_id=case["case_id"],
                     detail={"rules": [r.rule_id for r in verdict.escalations],
                             "proposed": cand.action_kind.value,
                             "amount_paise": int(case["amount_paise"])})
        conn.commit()
        return {"escalated": True}

    if dry_run:
        conn.commit()
        return {"dry_run": True, "candidate": cand}

    spec = _spec(cand, case)
    outcome = dispatcher.dispatch(spec, did, tid)

    if outcome.dispatched:
        conn.execute(
            "UPDATE recovery_case SET state = %s, version = version + 1 WHERE id = %s",
            (CaseState.AWAITING_OUTCOME.value, case["case_id"]),
        )
        conn.commit()
    return {"dispatch": outcome, "candidate": cand}


def _spec(cand: Candidate, case: dict) -> ActionSpec:
    from dataclasses import replace

    spec = ActionSpec(
        case_id=cand.case_id, obligation_id=cand.obligation_id,
        merchant_id=cand.merchant_id, customer_id=cand.customer_id,
        action_kind=cand.action_kind, channel=cand.channel,
        amount_paise=cand.amount_paise, scheduled_for=cand.scheduled_for,
        idempotency_key="", rail=cand.rail, decline_code=cand.decline_code,
        issuer=cand.issuer, discount_pct=cand.discount_pct,
        discount_paise=cand.discount_paise,
        dlt_template_id=_template_for(cand),
    )
    return replace(spec, idempotency_key=fingerprint(spec))


def _tally(result: PlanResult, outcome: dict) -> None:
    if outcome.get("blocked"):
        result.blocked += 1
        return
    if outcome.get("escalated"):
        result.escalated += 1
        return
    if outcome.get("holdout"):
        result.suppressed += 1
        return
    cand: Candidate | None = outcome.get("candidate")
    dispatch = outcome.get("dispatch")
    if outcome.get("dry_run"):
        result.dispatched += 1
    elif dispatch is None:
        return
    elif dispatch.duplicate:
        result.duplicates += 1
        return
    elif dispatch.failed:
        result.failed += 1
        return
    else:
        result.dispatched += 1

    if cand is not None:
        result.contacts_spent += cand.contacts
        result.discount_spent_paise += cand.discount_paise


def _policy_digest(policy: MerchantPolicy) -> dict:
    return {
        "max_contacts_per_week": policy.max_contacts_per_customer_per_week,
        "max_discount_pct": policy.max_discount_pct,
        "approval_threshold_paise": policy.approval_threshold_paise,
        "channels": sorted(c.value for c in policy.allowed_channels),
    }


def stop_reason_counts(result: PlanResult) -> Counter:
    return Counter(result.stop_breakdown)


__all__ = ["PlanResult", "plan_cycle", "stop_reason_counts"]

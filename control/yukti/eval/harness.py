"""Running all five arms over identical cases.

The sequence per arm is: reset the planning state, run one planning cycle in
dry-run, read back what it decided, and ask the oracle what each decision would
have produced.

**Dry-run matters.** It persists a decision for every case and touches no
adapter, no budget ledger and no external system. So the arms are compared on
what they DECIDED, without five arms racing to dispatch to the same sandbox.

**Reset between arms is not optional.** A planning cycle moves cases to
`stopped`, `escalated` or `awaiting_outcome`, and `load_open_cases` only returns
open ones — so without a reset the second arm would see whatever the first arm
left behind and the comparison would be meaningless. `reset-planning` is safe to
use here specifically because of the two guards added on day 5: it never touches
a case with a recovery_outcome, and never a decision without a planner run.

**No LLM anywhere in this path.** Every arm is deterministic, so `make eval`
runs identically with no API key configured — which is what makes the headline
result reproducible by anyone who clones the repository.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import psycopg

from yukti.config import settings
from yukti.eval import estimator, oracle_bridge
from yukti.eval.arms import ARMS, REFERENCE, Arm
from yukti.eval.estimator import ArmMetrics
from yukti.eval.oracle_bridge import ArmOutcome, CaseFacts
from yukti.pipeline import plan_cycle

log = logging.getLogger(__name__)

DECISIONS_SQL = """
SELECT d.case_id, d.action_kind, d.channel, d.scheduled_for,
       coalesce((d.alternatives_rejected #>> '{}')::text, '') AS alts,
       coalesce(act.discount_paise, 0) AS discount_paise,
       o.amount_paise
  FROM agent_decision d
  JOIN recovery_case c ON c.id = d.case_id
  JOIN obligation o    ON o.id = c.obligation_id
  LEFT JOIN recovery_action act ON act.decision_id = d.id
 WHERE d.trace_id = %s
"""


@dataclass(slots=True)
class EvalResult:
    merchant_id: str
    as_of: datetime
    metrics: dict[str, ArmMetrics] = field(default_factory=dict)
    cases: int = 0
    customers: int = 0
    holdout_cases: int = 0

    def winner_by_net(self) -> str:
        return max(self.metrics,
                   key=lambda k: self.metrics[k].true_incremental_margin_paise)

    def winner_by_gross(self) -> str:
        return max(self.metrics, key=lambda k: self.metrics[k].gross_recovered_paise)


def run(
    conn: psycopg.Connection, merchant_id: str, as_of: datetime,
    limit: int | None = None, seed: int | None = None,
    contact_budget: int | None = None,
) -> EvalResult:
    """Score every arm on the same cases and return the comparison.

    `contact_budget` overrides the merchant's authorised contact limit for the
    duration of the run. The arms only diverge on how they spend contacts, so
    the size of that budget is the parameter the whole comparison is sensitive
    to — sweeping it is what turns a single contested number into a curve.
    """
    seed = seed if seed is not None else settings().seed

    if contact_budget is not None:
        _set_contact_budget(conn, merchant_id, as_of, contact_budget)

    # Reset BEFORE loading the facts, not just between arms. `load_case_facts`
    # only sees `open` cases, so whatever the previous run left stopped or
    # held-out would silently drop out of the case set — making the evaluation
    # depend on what happened to run before it. It did: two consecutive runs
    # picked different merchants and different case counts off the same data.
    reset_planning(conn, merchant_id)

    facts = oracle_bridge.load_case_facts(conn, merchant_id, as_of, limit)
    if not facts:
        raise SystemExit(
            f"no open cases for {merchant_id} at {as_of:%Y-%m-%d} — "
            "run `make replay-fast && make consume` first"
        )

    result = EvalResult(merchant_id=merchant_id, as_of=as_of, cases=len(facts))
    result.customers = len({f.customer_id for f in facts.values()})
    assigned = {cid: f.assigned_arm for cid, f in facts.items()}
    result.holdout_cases = sum(1 for a in assigned.values() if a == "holdout")
    archetypes = {cid: f.archetype for cid, f in facts.items()}

    # The denominator, computed once. Every arm's incremental margin is measured
    # against these same cases under the same draw.
    baseline = {cid: oracle_bridge.score_no_action(f, seed) for cid, f in facts.items()}

    by_arm: dict[str, list[ArmOutcome]] = {}
    for arm in ARMS:
        log.info("evaluating arm %s", arm.key)
        outcomes = _run_arm(conn, arm, merchant_id, as_of, facts, baseline, limit, seed)
        by_arm[arm.key] = outcomes

        metrics = estimator.summarise(arm.key, outcomes, baseline, archetypes, seed)
        # Scaled by the TREATED case count, not by every case. A holdout case is
        # never acted on, so it contributes exactly zero to the true incremental
        # margin; scaling the estimate by `len(facts)` extrapolated the effect
        # onto cases the truth had already counted as zero and inflated every
        # arm's estimate by the holdout share.
        metrics.holdout_incremental = estimator.holdout_estimate(
            outcomes, baseline, assigned, len(facts) - result.holdout_cases, seed=seed)
        metrics.per_case_sd_paise, metrics.cases_needed_for_power = (
            estimator.power_requirement(
                baseline, metrics.true_incremental_margin_paise,
                len(facts) - result.holdout_cases,
                result.holdout_cases / len(facts) if facts else 0.0,
            )
        )
        result.metrics[arm.key] = metrics

    # Contact-attributable margin, once the reference arm has been scored.
    # Measured against B4 rather than B0 so the shared free-retry mass cancels
    # and what is left is only how each arm spent the contact budget.
    reference = {o.case_id: o for o in by_arm[REFERENCE.key]}
    for key, metrics in result.metrics.items():
        if key == REFERENCE.key:
            continue
        total, interval = estimator.against_reference(by_arm[key], reference, seed)
        metrics.contact_incremental_margin_paise = total
        metrics.contact_incremental_per_1k = interval

    return result


def _run_arm(
    conn: psycopg.Connection, arm: Arm, merchant_id: str, as_of: datetime,
    facts: dict[str, CaseFacts], baseline: dict[str, ArmOutcome],
    limit: int | None, seed: int,
) -> list[ArmOutcome]:
    if not arm.acts:
        # The holdout does not plan. Reusing the precomputed baseline rather
        # than re-deriving it also guarantees B0 is scored on exactly the same
        # object every other arm is compared against.
        return list(baseline.values())

    reset_planning(conn, merchant_id)

    plan = plan_cycle(
        conn, merchant_id, as_of, limit=limit, dry_run=True, scorer=arm.scorer(),
    )
    decided = {
        row["case_id"]: row
        for row in conn.execute(DECISIONS_SQL, (plan.trace_id,)).fetchall()
    }

    outcomes: list[ArmOutcome] = []
    for case_id, fact in facts.items():
        if fact.assigned_arm == "holdout":
            # A holdout case's outcome IS the baseline, by construction, and
            # that must be read off the ASSIGNMENT rather than off the decision.
            # `_act` writes a decision naming the action it would have taken and
            # then declines to take it; scoring that row treats the recorded
            # intent as a treatment and contaminates the denominator. Every lift
            # number rests on this line.
            outcomes.append(baseline[case_id])
            continue

        row = decided.get(case_id)
        if row is None:
            # Not reached this cycle — a stopped case, or one outside the limit.
            # Scored as no action, which is what actually happened to it.
            outcomes.append(baseline[case_id])
            continue

        discount_pct = _discount_pct(row["discount_paise"], row["amount_paise"])
        outcomes.append(oracle_bridge.score(
            fact, row["action_kind"], row["channel"], row["scheduled_for"],
            discount_pct, seed,
        ))
    return outcomes


def _set_contact_budget(
    conn: psycopg.Connection, merchant_id: str, as_of: datetime, limit: int
) -> None:
    """Force the contact budget for a sweep. Writes the ledger the allocator
    actually reads, rather than a parallel setting the run might diverge from."""
    conn.execute(
        "INSERT INTO budget_ledger (merchant_id, kind, window_start, limit_val) "
        "VALUES (%s, 'contact', %s, %s) "
        "ON CONFLICT (merchant_id, kind, window_start) "
        "DO UPDATE SET limit_val = EXCLUDED.limit_val",
        (merchant_id, as_of.date(), limit),
    )
    conn.commit()


def _discount_pct(discount_paise: int, amount_paise: int) -> float:
    if not discount_paise or not amount_paise:
        return 0.0
    return 100.0 * discount_paise / amount_paise


def reset_planning(conn: psycopg.Connection, merchant_id: str) -> None:
    """Undo the previous arm's planning output, and nothing else.

    Deliberately the same two guards as the CLI command: only cases planned by a
    planner run, and never one carrying a recovery_outcome. The exploration
    history lives in these same tables and holds the randomised treatment
    assignment that identifies uplift — an earlier version of this cleanup
    reopened 2,156 of those cases and silently turned treated rows into control
    rows.
    """
    conn.execute(
        """
        CREATE TEMP TABLE _arm_reset ON COMMIT DROP AS
        SELECT DISTINCT c.id
          FROM recovery_case c
          JOIN agent_decision d ON d.case_id = c.id
          JOIN agent_run r      ON r.id = d.run_id AND r.kind = 'planner'
         WHERE c.merchant_id = %s
           AND NOT EXISTS (SELECT 1 FROM recovery_outcome o WHERE o.case_id = c.id)
        """,
        (merchant_id,),
    )
    conn.execute(
        "DELETE FROM recovery_action WHERE decision_id IN "
        "(SELECT id FROM agent_decision WHERE run_id IS NOT NULL "
        "   AND case_id IN (SELECT id FROM _arm_reset))")
    conn.execute(
        "DELETE FROM policy_evaluation WHERE decision_id IN "
        "(SELECT id FROM agent_decision WHERE run_id IS NOT NULL "
        "   AND case_id IN (SELECT id FROM _arm_reset))")
    conn.execute(
        "DELETE FROM agent_decision WHERE run_id IS NOT NULL "
        "  AND case_id IN (SELECT id FROM _arm_reset)")
    conn.execute(
        "UPDATE recovery_case SET state = 'open', stop_reason = NULL, "
        "       closed_at = NULL "
        " WHERE id IN (SELECT id FROM _arm_reset) AND state <> 'open'")
    conn.execute(
        "DELETE FROM agent_run WHERE merchant_id = %s AND kind = 'planner'",
        (merchant_id,))
    conn.execute("DELETE FROM audit_event WHERE merchant_id = %s", (merchant_id,))
    conn.execute(
        "UPDATE budget_ledger SET consumed_val = 0 WHERE merchant_id = %s",
        (merchant_id,))
    conn.commit()

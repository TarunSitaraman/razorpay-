"""The policy engine — the gate every action passes through.

Composes the regulatory and merchant packs and reduces their results to a single
verdict, with every rule outcome recorded. Two properties matter more than
anything else here:

**BLOCK is absorbing.** Any single BLOCK, from either pack, blocks the action.
No amount of merchant configuration and no model confidence overrides it.

**RegPack outranks MerchantPack.** A merchant may be more restrictive than the
regulator, never less. This is enforced structurally — the merchant pack's
results are only consulted for BLOCK and ESCALATE, never to lift a regulatory
block — rather than by convention.

The engine is a library rather than a service, called in-process on the path of
every action. A network hop here would introduce a failure mode where the
guardrails are unavailable but the actions are not, and there is no acceptable
behaviour in that state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from yukti.domain.enums import PolicyVerdict
from yukti.policy import merchantpack, regpack
from yukti.policy.merchantpack import MerchantContext, MerchantPolicy
from yukti.policy.regpack import ActionRequest, RuleResult


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The verdict, plus every rule that produced it."""

    verdict: PolicyVerdict
    results: list[RuleResult] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.verdict is PolicyVerdict.ALLOW

    @property
    def blocks(self) -> list[RuleResult]:
        return [r for r in self.results if r.verdict is PolicyVerdict.BLOCK]

    @property
    def escalations(self) -> list[RuleResult]:
        return [r for r in self.results if r.verdict is PolicyVerdict.ESCALATE]

    @property
    def reasons(self) -> list[str]:
        """Human-readable reasons, for the console and the audit trail."""
        return [f"{r.rule_id}: {r.reason}"
                for r in self.results if r.verdict is not PolicyVerdict.ALLOW]

    def explain(self) -> str:
        if self.allowed:
            return "allowed"
        head = "blocked by" if self.blocks else "escalated by"
        return f"{head} " + "; ".join(self.reasons)


def evaluate(
    request: ActionRequest,
    policy: MerchantPolicy,
    context: MerchantContext | None = None,
) -> PolicyDecision:
    """Evaluate an action against both packs.

    Every rule runs, even after the first block. A merchant investigating why an
    action did not happen should see every reason at once rather than fixing one
    and rediscovering the next.
    """
    regulatory = regpack.evaluate(request)
    merchant = merchantpack.evaluate(request, policy, context)
    results = regulatory + merchant

    # BLOCK is absorbing and outranks ESCALATE: an action that is both
    # above-threshold and illegal must not be presented to a human for approval,
    # because approving it would still be illegal.
    if any(r.verdict is PolicyVerdict.BLOCK for r in results):
        verdict = PolicyVerdict.BLOCK
    elif any(r.verdict is PolicyVerdict.ESCALATE for r in results):
        verdict = PolicyVerdict.ESCALATE
    else:
        verdict = PolicyVerdict.ALLOW

    return PolicyDecision(verdict=verdict, results=results)


def is_feasible(
    request: ActionRequest,
    policy: MerchantPolicy,
    context: MerchantContext | None = None,
) -> bool:
    """Cheap feasibility check for the allocator.

    The allocator calls this while choosing, so it never spends budget on an
    action that will be blocked at dispatch. The full `evaluate` still runs
    before dispatch — this is a filter, not a substitute. Two checks against one
    rule set is defence in depth: the second catches a bug in the first.
    """
    return evaluate(request, policy, context).verdict is not PolicyVerdict.BLOCK


def record(
    conn: psycopg.Connection, decision_id: str, decision: PolicyDecision
) -> int:
    """Persist every rule outcome against a decision.

    Written for all results, not just failures. "This rule ran and passed" is
    what makes the audit trail evidence rather than an anecdote — a merchant or
    a regulator can see that a check happened, not merely that nothing complained.
    """
    reg_ids = set(regpack.rule_ids())
    rows = [
        (decision_id,
         "regulatory" if r.rule_id in reg_ids else "merchant",
         r.rule_id, r.verdict.value, r.reason or "passed")
        for r in decision.results
    ]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO policy_evaluation (decision_id, pack, rule_id, verdict, reason) "
            "VALUES (%s, %s, %s, %s, %s)",
            rows,
        )
    return len(rows)


def all_rule_ids() -> dict[str, tuple[str, ...]]:
    """Every rule this engine can apply, by pack. Used by the console legend."""
    from datetime import datetime

    from yukti.domain.enums import ActionKind, Channel

    probe = ActionRequest(
        action_kind=ActionKind.SUPPRESS, channel=Channel.NONE,
        scheduled_for=datetime(2026, 6, 1, 12, 0), amount_paise=1,
    )
    blank = MerchantPolicy(merchant_id="probe")
    return {
        "regulatory": regpack.rule_ids(),
        "merchant": tuple(
            r.rule_id for r in merchantpack.evaluate(probe, blank, MerchantContext())
        ),
    }

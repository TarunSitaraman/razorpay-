"""Turning a persisted decision into a counterfactual outcome.

The evaluation asks a question no production system can answer: *what would have
happened if this arm had acted on this customer?* In simulation we can ask it,
because `datagen/yukti_datagen/response.py` is a full outcome oracle rather than
a sampler.

One property of that oracle does the heavy lifting, and it was built on day 1
for exactly this moment: **the recovery draw is keyed on the case, not on the
intervention.** So the same customer "would have paid at threshold u" no matter
which arm is being scored. Arms therefore differ only through the probability
they induce, never through luck — a paired comparison, which is what makes a
few points of lift measurable without enormous samples.

This module is the only place in the evaluation that reads `customer.archetype`.
That is allowed and is the whole reason the column exists: archetype is ground
truth for SCORING. `features.py` structurally forbids it from ever reaching a
model, and nothing here feeds back into one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from yukti.domain.enums import ActionKind, Channel, Rail, UpliftArchetype
from yukti_datagen.response import CaseContext, Intervention, Outcome, evaluate

# What each contact channel costs the merchant, in paise. Shared with the tool
# layer and the generator so the margin the evaluation reports is the margin the
# allocator optimised against.
from yukti.dispatch.tools import CHANNEL_COST_PAISE

# The counterfactual context for every case in the planning window, plus the
# ground truth the oracle needs. Read once per evaluation run rather than per
# arm: it does not change between arms, and re-reading it five times would be
# the slowest part of the harness.
CONTEXT_SQL = """
SELECT c.id                            AS case_id,
       c.customer_id,
       c.merchant_id,
       c.arm                           AS assigned_arm,
       o.amount_paise,
       o.kind                          AS obligation_kind,
       m.mdr_bps,
       cu.archetype,
       cu.preferred_channel,
       a.decline_code,
       a.rail,
       a.issuer,
       (ptp.id IS NOT NULL)            AS open_promise,
       coalesce(ct.n, 0)               AS prior_contacts_7d,
       (deg.id IS NOT NULL)            AS in_downtime
  FROM recovery_case c
  JOIN obligation o  ON o.id  = c.obligation_id
  JOIN merchant   m  ON m.id  = c.merchant_id
  JOIN customer   cu ON cu.id = c.customer_id
  JOIN LATERAL (
      SELECT decline_code, rail, issuer, attempted_at
        FROM payment_attempt
       WHERE obligation_id = o.id AND status = 'failed'
       ORDER BY attempted_at DESC LIMIT 1
  ) a ON true
  LEFT JOIN promise_to_pay ptp
         ON ptp.obligation_id = o.id
        AND ptp.created_at <= %(as_of)s AND ptp.promised_for > %(as_of)s
        AND ptp.state <> 'broken'
  -- Ground-truth downtime, not the detector's estimate. The detector's accuracy
  -- is scored elsewhere; here we need to know what was actually true, because
  -- that is what determined the outcome.
  LEFT JOIN degradation_signal deg
         ON deg.state = 'ground_truth' AND deg.dimension = 'issuer'
        AND deg.dimension_value = a.issuer
        AND deg.window_start <= %(as_of)s AND deg.window_end > %(as_of)s
  LEFT JOIN LATERAL (
      SELECT count(*) AS n
        FROM recovery_action ra
        JOIN recovery_case rc ON rc.id = ra.case_id
       WHERE rc.customer_id = c.customer_id
         AND ra.dispatched_at >= %(as_of)s - interval '7 days'
         AND ra.dispatched_at <  %(as_of)s
         AND ra.kind IN ('message', 'voice_call', 'discount_offer')
  ) ct ON true
 WHERE c.merchant_id = %(merchant_id)s
   AND c.opened_at <= %(as_of)s
   AND NOT EXISTS (SELECT 1 FROM recovery_outcome ro WHERE ro.case_id = c.id)
 ORDER BY c.id
"""


@dataclass(frozen=True, slots=True)
class CaseFacts:
    """Everything the harness needs about one case, arm-independent."""

    case_id: str
    customer_id: str
    merchant_id: str
    assigned_arm: str
    amount_paise: int
    obligation_kind: str
    mdr_bps: int
    archetype: str
    context: CaseContext

    def margin_of(self, recovered_paise: int, discount_paise: int,
                  channel_cost_paise: int) -> int:
        """Net margin in paise: what the merchant actually keeps.

        MDR is subtracted because a recovered rupee is not a kept rupee, and
        discount and channel cost are subtracted in full because they are paid
        whether or not the recovery lands. Any arm that ignored either would
        report a larger number than the merchant's bank balance shows.
        """
        gross = recovered_paise * (1 - self.mdr_bps / 10_000)
        return int(round(gross - discount_paise - channel_cost_paise))


@dataclass(frozen=True, slots=True)
class ArmOutcome:
    """What one arm did to one case, and what it produced."""

    case_id: str
    customer_id: str
    action_kind: str
    channel: str
    recovered: bool
    opted_out: bool
    recovered_paise: int
    discount_paise: int
    channel_cost_paise: int
    net_margin_paise: int
    true_uplift: float

    @property
    def contacted(self) -> bool:
        return self.channel not in ("", "none")


def load_case_facts(
    conn: psycopg.Connection, merchant_id: str, as_of: datetime,
    limit: int | None = None,
) -> dict[str, CaseFacts]:
    """Arm-independent facts for every case in the planning window."""
    sql = CONTEXT_SQL + (f" LIMIT {int(limit)}" if limit else "")
    rows = conn.execute(
        sql, {"merchant_id": merchant_id, "as_of": as_of}
    ).fetchall()

    facts: dict[str, CaseFacts] = {}
    for r in rows:
        archetype = r["archetype"] or UpliftArchetype.PERSUADABLE.value
        facts[r["case_id"]] = CaseFacts(
            case_id=r["case_id"],
            customer_id=r["customer_id"],
            merchant_id=r["merchant_id"],
            assigned_arm=r["assigned_arm"],
            amount_paise=int(r["amount_paise"]),
            obligation_kind=r["obligation_kind"],
            mdr_bps=int(r["mdr_bps"] or 200),
            archetype=archetype,
            context=CaseContext(
                case_id=r["case_id"],
                archetype=UpliftArchetype(archetype),
                amount_paise=int(r["amount_paise"]),
                decline_code=r["decline_code"] or "UNKNOWN",
                rail_is_mandate=_is_mandate(r["rail"]),
                preferred_channel=_channel(r["preferred_channel"]),
                prior_contacts_7d=int(r["prior_contacts_7d"] or 0),
                open_promise=bool(r["open_promise"]),
                in_downtime=bool(r["in_downtime"]),
            ),
        )
    return facts


def score(
    facts: CaseFacts, action_kind: str, channel: str, scheduled_for: datetime | None,
    discount_pct: float, seed: int,
) -> ArmOutcome:
    """Ask the oracle what this action would have produced on this case."""
    kind = _action(action_kind)
    chan = _channel(channel)

    intervention = Intervention(
        kind=kind, channel=chan, at=scheduled_for, discount_pct=discount_pct,
    )
    outcome: Outcome = evaluate(facts.context, intervention, seed)

    # Discount is committed when the offer is made, not when it converts. The
    # allocator prices it that way — it has to spend before knowing the result —
    # so the evaluation must charge it the same way, or Yukti would be scored
    # against a cost model it never optimised against.
    discount_paise = (
        int(round(facts.amount_paise * discount_pct / 100.0)) if discount_pct else 0
    )
    channel_cost = CHANNEL_COST_PAISE.get(chan, 0) if kind.contacts_customer else 0

    return ArmOutcome(
        case_id=facts.case_id,
        customer_id=facts.customer_id,
        action_kind=kind.value,
        channel=chan.value,
        recovered=outcome.recovered,
        opted_out=outcome.opted_out,
        recovered_paise=outcome.recovered_paise,
        discount_paise=discount_paise,
        channel_cost_paise=channel_cost,
        net_margin_paise=facts.margin_of(
            outcome.recovered_paise, discount_paise, channel_cost),
        true_uplift=outcome.uplift,
    )


def score_no_action(facts: CaseFacts, seed: int) -> ArmOutcome:
    """The holdout: what happens when nothing is done.

    Not "the arm chose to suppress" — genuinely no intervention, which is what
    makes it the denominator. Every other arm's incremental margin is measured
    against this same case under the same draw.
    """
    return score(facts, ActionKind.SUPPRESS.value, Channel.NONE.value, None, 0.0, seed)


def _action(value: str) -> ActionKind:
    try:
        return ActionKind(value)
    except ValueError:
        return ActionKind.SUPPRESS


def _channel(value: Any) -> Channel:
    try:
        return Channel(value) if value else Channel.NONE
    except ValueError:
        return Channel.NONE


def _is_mandate(rail: Any) -> bool:
    try:
        return Rail(rail).is_mandate
    except (ValueError, TypeError):
        return False

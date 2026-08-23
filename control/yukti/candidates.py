"""Generating the action set a case could receive.

The allocator chooses between candidates; this decides what the candidates are.
That makes it a quietly important module — an action never proposed can never be
chosen, so an omission here is invisible in every downstream metric.

Two properties it must have.

**Deterministic.** The same case at the same planning moment produces the same
candidate list, in the same order. Without that, re-running a cycle would
produce different proposals, different fingerprints, and duplicate dispatches —
so the idempotency guarantee downstream rests on this being a pure function.

**Domain-filtered, not policy-filtered.** Candidates are excluded here only when
the action is *incoherent*: a silent retry on a rail the customer has to
initiate, a messaging channel the customer never consented to, a discount above
what the merchant permits. Whether an action is *allowed right now* — quiet
hours, pre-debit notice, budget — belongs to the policy engine and the
allocator. Filtering it here too would put the same rule in two places and
guarantee they eventually disagree.

The debit-timing model chooses *when* a mandate retry is proposed. It is
consulted rather than hardcoded because it rediscovered the salary-cycle peaks
from data, and hardcoding "retry on the 1st" would throw that away and be wrong
for merchants whose customers are not salaried.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from yukti.domain.decline import lookup
from yukti.domain.enums import ActionKind, Channel, Rail, Transience
from yukti.intelligence.debit_timing import DebitTimingModel
from yukti.policy.merchantpack import MerchantPolicy

# Discount tiers offered, as percentages. Mirrors the exploration grid in
# `datagen.history.EXPLORE_DISCOUNTS` minus the zero, so every tier the
# allocator can pick is one the uplift model has seen treated examples of.
DISCOUNT_TIERS: tuple[float, ...] = (5.0, 10.0, 15.0, 25.0)

# Hours at which a contact is proposed. Inside TRAI's 09:00-21:00 window by
# construction, but the policy engine still checks — this is a preference, not
# the enforcement, and treating it as enforcement is how a constraint quietly
# stops being tested.
CONTACT_HOURS: tuple[int, ...] = (10, 13, 16, 19)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One thing we could do about one case."""

    case_id: str
    customer_id: str
    obligation_id: str
    merchant_id: str
    action_kind: ActionKind
    channel: Channel
    scheduled_for: datetime
    amount_paise: int
    discount_pct: float = 0.0
    discount_paise: int = 0
    rail: str = "upi_autopay"
    decline_code: str = "UNKNOWN"
    issuer: str | None = None
    rationale: str = ""

    @property
    def contacts(self) -> int:
        return 1 if self.action_kind.contacts_customer else 0


def _consented(consent: dict | None, channel: Channel) -> bool:
    """DPDP consent is per channel, and absence means no.

    An empty consent map is the safe default rather than an empty permission
    set — the schema default is `{}` for exactly this reason.
    """
    return bool((consent or {}).get(channel.value))


def _contact_hour(as_of: datetime) -> datetime:
    """The next preferred contact slot at or after `as_of`."""
    for hour in CONTACT_HOURS:
        if as_of.hour < hour:
            return as_of.replace(hour=hour, minute=0, second=0, microsecond=0)
    nxt = as_of + timedelta(days=1)
    return nxt.replace(hour=CONTACT_HOURS[0], minute=0, second=0, microsecond=0)


def generate(
    case: dict, policy: MerchantPolicy, as_of: datetime,
    timing: DebitTimingModel | None = None,
) -> list[Candidate]:
    """Every coherent action for one case, cheapest first.

    Ordering is by resource draw rather than by expected value: the allocator
    ranks on value itself, and presenting a stable cost order makes the list
    readable in the audit trail and reproducible across runs.
    """
    spec = lookup(case.get("decline_code"))
    rail = case.get("rail") or "upi_autopay"
    is_mandate = _rail_is_mandate(rail)
    amount = int(case["amount_paise"])
    consent = case.get("consent")

    out: list[Candidate] = []

    def add(kind: ActionKind, channel: Channel, when: datetime,
            discount_pct: float = 0.0, rationale: str = "") -> None:
        discount_paise = int(round(amount * discount_pct / 100.0)) if discount_pct else 0
        out.append(Candidate(
            case_id=case["case_id"], customer_id=case["customer_id"],
            obligation_id=case["obligation_id"], merchant_id=case["merchant_id"],
            action_kind=kind, channel=channel, scheduled_for=when,
            amount_paise=amount, discount_pct=discount_pct,
            discount_paise=discount_paise, rail=rail,
            decline_code=case.get("decline_code") or "UNKNOWN",
            issuer=case.get("issuer"), rationale=rationale,
        ))

    # Always available, always free. Its presence is what lets the allocator
    # decline to act without that being an absence of any candidate at all —
    # "we considered this case and chose nothing" is a decision, and it needs a
    # row to attach to.
    add(ActionKind.SUPPRESS, Channel.NONE, as_of,
        rationale="do nothing and reconsider next cycle")

    # Free money-moving actions, where the rail permits them.
    if is_mandate and spec.retryable_silently and spec.transience is not Transience.PERMANENT:
        slot = (timing or DebitTimingModel()).best_slot(as_of, case.get("decline_code"))
        add(ActionKind.SILENT_RETRY, Channel.NONE, slot.at, rationale=slot.reason)

        # A scheduled debit is the same attempt with a pre-debit notice in front
        # of it, so it is only proposed far enough out for that notice to be
        # legal. Proposing it sooner would guarantee an RBI_PREDEBIT_24H block
        # and waste an allocator slot on something that cannot happen.
        notice_safe = max(slot.at, as_of + timedelta(hours=25))
        if notice_safe != slot.at:
            add(ActionKind.SCHEDULE_DEBIT, Channel.NONE, notice_safe,
                rationale="debit after the 24h pre-debit notification window")
        else:
            add(ActionKind.SCHEDULE_DEBIT, Channel.NONE, slot.at,
                rationale=f"{slot.reason}; clears the 24h notice window")

    # Contact actions. Excluded here only where the channel is incoherent for
    # this customer or this merchant — never on timing or budget.
    when = _contact_hour(as_of)
    for channel in (Channel.EMAIL, Channel.SMS, Channel.WHATSAPP):
        if channel not in policy.allowed_channels or not _consented(consent, channel):
            continue
        add(ActionKind.MESSAGE, channel, when,
            rationale=f"payment link over {channel.value}")

    # Discounts on the customer's preferred channel only. Offering the same
    # incentive over three channels would let the allocator fund three copies of
    # one concession, and the per-case cap would be the only thing stopping it.
    preferred = _preferred_channel(case, policy, consent)
    if preferred is not None:
        for tier in DISCOUNT_TIERS:
            if tier > policy.max_discount_pct:
                continue
            add(ActionKind.DISCOUNT_OFFER, preferred, when, discount_pct=tier,
                rationale=f"{tier:.0f}% incentive over {preferred.value}")

    if Channel.VOICE in policy.allowed_channels and _consented(consent, Channel.VOICE):
        add(ActionKind.VOICE_CALL, Channel.VOICE, when,
            rationale="outbound call — the expensive option, funded only on high value")

    return out


def _preferred_channel(
    case: dict, policy: MerchantPolicy, consent: dict | None
) -> Channel | None:
    """The customer's own preference, if it is usable; otherwise the cheapest."""
    stated = case.get("preferred_channel")
    if stated:
        try:
            channel = Channel(stated)
        except ValueError:
            channel = None
        if channel and channel in policy.allowed_channels and _consented(consent, channel):
            return channel
    for channel in (Channel.EMAIL, Channel.SMS, Channel.WHATSAPP):
        if channel in policy.allowed_channels and _consented(consent, channel):
            return channel
    return None


def _rail_is_mandate(rail: str) -> bool:
    try:
        return Rail(rail).is_mandate
    except ValueError:
        return False

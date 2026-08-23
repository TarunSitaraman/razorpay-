"""Merchant-configurable policy.

These are the merchant's own commercial limits — how often their customers may
be contacted, how much margin they will give away, when they want a human in
the loop. They sit on top of the regulatory pack and can only ever be *more*
restrictive: `PolicyEngine` runs RegPack first and a merchant setting cannot
un-block a regulatory block.

The approval threshold is what produces `ESCALATE` rather than `ALLOW`. Above
it, the agent has done all the work and holds the action for a human — which is
the posture the track brief calls "compliant escalation", and the one Razorpay's
own guardrails documentation describes as review-first mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from yukti.domain.enums import ActionKind, Channel, PolicyVerdict
from yukti.policy.regpack import ActionRequest, RuleResult, _allow


@dataclass(frozen=True, slots=True)
class MerchantPolicy:
    """One merchant's configuration.

    Defaults are deliberately conservative: a merchant who configures nothing
    gets cautious behaviour, not unlimited behaviour.
    """

    merchant_id: str
    max_contacts_per_customer_per_week: int = 3
    max_discount_pct: float = 15.0
    # Above this, the agent proposes and a human decides.
    approval_threshold_paise: int = 25_000_00
    allowed_channels: frozenset[Channel] = field(
        default_factory=lambda: frozenset({Channel.WHATSAPP, Channel.SMS, Channel.EMAIL})
    )
    # Dates on which no outbound contact happens at all — a product incident, a
    # festival, a PR situation.
    blackout_dates: frozenset[date] = field(default_factory=frozenset)
    # Below this, chasing costs more than it recovers for this merchant.
    min_obligation_paise: int = 100_00
    # Whether a discount may be offered to a customer who already had one
    # recently. Off by default: stacking is how discount farming starts.
    allow_discount_stacking: bool = False


@dataclass(frozen=True, slots=True)
class MerchantContext:
    """Runtime facts the merchant rules need."""

    contacts_this_week: int = 0
    had_recent_discount: bool = False


def contact_cap(r: ActionRequest, p: MerchantPolicy, ctx: MerchantContext) -> RuleResult:
    rule = "MERCHANT_CONTACT_CAP"
    if not r.action_kind.contacts_customer:
        return _allow(rule)
    if ctx.contacts_this_week >= p.max_contacts_per_customer_per_week:
        return RuleResult(
            rule, PolicyVerdict.BLOCK,
            f"customer has had {ctx.contacts_this_week} contacts this week; "
            f"merchant cap is {p.max_contacts_per_customer_per_week}",
        )
    return _allow(rule)


def discount_ceiling(r: ActionRequest, p: MerchantPolicy, ctx: MerchantContext) -> RuleResult:
    rule = "MERCHANT_DISCOUNT_CEILING"
    if r.discount_pct > p.max_discount_pct:
        return RuleResult(
            rule, PolicyVerdict.BLOCK,
            f"discount {r.discount_pct:.1f}% exceeds merchant ceiling "
            f"{p.max_discount_pct:.1f}%",
        )
    return _allow(rule)


def discount_stacking(r: ActionRequest, p: MerchantPolicy, ctx: MerchantContext) -> RuleResult:
    """Repeated discounts to the same customer.

    Not merely a cost control: a customer who learns that abandoning produces a
    discount will abandon deliberately, so stacking actively manufactures the
    behaviour the system exists to recover from.
    """
    rule = "MERCHANT_DISCOUNT_STACKING"
    if r.discount_pct <= 0 or p.allow_discount_stacking:
        return _allow(rule)
    if ctx.had_recent_discount:
        return RuleResult(rule, PolicyVerdict.BLOCK,
                          "customer already received a discount recently; stacking is off")
    return _allow(rule)


def allowed_channel(r: ActionRequest, p: MerchantPolicy, ctx: MerchantContext) -> RuleResult:
    rule = "MERCHANT_ALLOWED_CHANNEL"
    if not r.action_kind.contacts_customer or r.channel is Channel.NONE:
        return _allow(rule)
    if r.channel not in p.allowed_channels:
        return RuleResult(rule, PolicyVerdict.BLOCK,
                          f"merchant has not enabled {r.channel.value}")
    return _allow(rule)


def blackout(r: ActionRequest, p: MerchantPolicy, ctx: MerchantContext) -> RuleResult:
    rule = "MERCHANT_BLACKOUT"
    if not r.action_kind.contacts_customer:
        return _allow(rule)
    if r.scheduled_for.date() in p.blackout_dates:
        return RuleResult(rule, PolicyVerdict.BLOCK,
                          f"{r.scheduled_for:%Y-%m-%d} is a merchant blackout date")
    return _allow(rule)


def minimum_value(r: ActionRequest, p: MerchantPolicy, ctx: MerchantContext) -> RuleResult:
    rule = "MERCHANT_MIN_VALUE"
    if r.action_kind is ActionKind.SUPPRESS:
        return _allow(rule)
    if r.amount_paise < p.min_obligation_paise:
        return RuleResult(rule, PolicyVerdict.BLOCK,
                          f"obligation {r.amount_paise} is below the merchant floor "
                          f"{p.min_obligation_paise}")
    return _allow(rule)


def approval_threshold(r: ActionRequest, p: MerchantPolicy, ctx: MerchantContext) -> RuleResult:
    """Above the threshold the agent proposes and a human disposes.

    ESCALATE, deliberately not BLOCK: the work is valid and the action may well
    be right. What is missing is authority, and the distinction matters both to
    the merchant reading the console and to the metrics.
    """
    rule = "MERCHANT_APPROVAL_THRESHOLD"
    if r.action_kind is ActionKind.SUPPRESS:
        return _allow(rule)
    if r.amount_paise >= p.approval_threshold_paise:
        return RuleResult(
            rule, PolicyVerdict.ESCALATE,
            f"amount {r.amount_paise} is at or above the merchant approval "
            f"threshold {p.approval_threshold_paise}; holding for review",
        )
    return _allow(rule)


RULES = (
    contact_cap,
    discount_ceiling,
    discount_stacking,
    allowed_channel,
    blackout,
    minimum_value,
    approval_threshold,
)


def evaluate(
    request: ActionRequest, policy: MerchantPolicy, ctx: MerchantContext | None = None
) -> list[RuleResult]:
    context = ctx or MerchantContext()
    return [rule(request, policy, context) for rule in RULES]


def compile_from_settings(merchant_id: str, settings: dict) -> MerchantPolicy:
    """Build a policy from a merchant's stored configuration.

    Unknown keys are ignored rather than raising, and every value is clamped to
    a safe range. A configuration bug should degrade a merchant's policy toward
    caution, never widen it past what they meant.
    """
    def clamp(value, lo, hi, default):
        try:
            return max(lo, min(hi, type(default)(value)))
        except (TypeError, ValueError):
            return default

    channels = settings.get("allowed_channels")
    parsed_channels = frozenset(
        Channel(c) for c in channels if c in {ch.value for ch in Channel}
    ) if channels else MerchantPolicy(merchant_id).allowed_channels

    return MerchantPolicy(
        merchant_id=merchant_id,
        max_contacts_per_customer_per_week=clamp(
            settings.get("max_contacts_per_customer_per_week", 3), 0, 14, 3),
        max_discount_pct=clamp(settings.get("max_discount_pct", 15.0), 0.0, 50.0, 15.0),
        approval_threshold_paise=clamp(
            settings.get("approval_threshold_paise", 25_000_00), 0, 10_00_00_000,
            25_000_00),
        allowed_channels=parsed_channels,
        blackout_dates=frozenset(settings.get("blackout_dates", ())),
        min_obligation_paise=clamp(settings.get("min_obligation_paise", 100_00),
                                   0, 10_00_000, 100_00),
        allow_discount_stacking=bool(settings.get("allow_discount_stacking", False)),
    )

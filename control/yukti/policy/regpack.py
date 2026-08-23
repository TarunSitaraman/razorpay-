"""Regulatory rules. Merchants cannot disable these.

Indian payments regulation constrains *when* and *how* a recovery action may
happen, independently of whether it would make money. An LLM planner asked to
sequence recoveries will violate these — not maliciously, but because "message
her now, she's most likely to respond at 22:00" is a locally correct answer to
the wrong question.

So the constraints live here, as deterministic functions with named rule ids,
evaluated on every action before dispatch. Each cites the rule it encodes; the
citations are in docs/RESEARCH.md.

Nothing in `MerchantPack` can switch any of these off. A merchant may be more
restrictive than the regulator, never less.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from yukti.domain.decline import lookup
from yukti.domain.enums import ActionKind, Channel, PolicyVerdict

# RBI's consolidated e-mandate framework requires a pre-debit notification at
# least 24 hours before each debit, with an opt-out path.
PREDEBIT_NOTICE_HOURS = 24

# AFA-free ceiling per transaction, and the raised ceiling for NPCI-approved
# categories (mutual funds, insurance, credit-card bills).
AFA_FREE_LIMIT_PAISE = 15_000_00
AFA_FREE_LIMIT_EXEMPT_PAISE = 1_00_000_00
EXEMPT_CATEGORIES = frozenset({"mutual_fund", "insurance", "credit_card_bill"})

# TRAI restricts commercial communication to daytime hours.
QUIET_HOURS_START = 21   # 21:00 IST onwards is prohibited
QUIET_HOURS_END = 9      # before 09:00 IST is prohibited


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """A proposed action, as the policy engine sees it."""

    action_kind: ActionKind
    channel: Channel
    scheduled_for: datetime          # IST
    amount_paise: int
    merchant_category: str = "general"
    decline_code: str | None = None
    attempts_made: int = 0
    discount_pct: float = 0.0
    # Consent per channel, as recorded under DPDP.
    consent: dict[str, bool] | None = None
    # When the RBI pre-debit notice was sent, if it has been.
    predebit_notice_at: datetime | None = None
    dlt_template_id: str | None = None
    has_afa: bool = False


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    verdict: PolicyVerdict
    reason: str

    @property
    def blocked(self) -> bool:
        return self.verdict is PolicyVerdict.BLOCK


def _allow(rule_id: str) -> RuleResult:
    return RuleResult(rule_id, PolicyVerdict.ALLOW, "")


# --- individual regulatory rules -------------------------------------------

def rbi_predebit_24h(r: ActionRequest) -> RuleResult:
    """No mandate debit inside 24h of its pre-debit notification."""
    rule = "RBI_PREDEBIT_24H"
    if r.action_kind is not ActionKind.SCHEDULE_DEBIT:
        return _allow(rule)
    if r.predebit_notice_at is None:
        return RuleResult(rule, PolicyVerdict.BLOCK,
                          "mandate debit scheduled with no pre-debit notification sent")
    gap = r.scheduled_for - r.predebit_notice_at
    if gap < timedelta(hours=PREDEBIT_NOTICE_HOURS):
        return RuleResult(
            rule, PolicyVerdict.BLOCK,
            f"debit is {gap.total_seconds() / 3600:.1f}h after notice; "
            f"{PREDEBIT_NOTICE_HOURS}h required",
        )
    return _allow(rule)


def rbi_afa_limit(r: ActionRequest) -> RuleResult:
    """Above the AFA-free ceiling, a debit needs additional authentication.

    The agent may not schedule one autonomously — that is a customer
    interaction, not a decision we get to make on their behalf.
    """
    rule = "RBI_AFA_LIMIT"
    if not r.action_kind.moves_money:
        return _allow(rule)
    limit = (AFA_FREE_LIMIT_EXEMPT_PAISE if r.merchant_category in EXEMPT_CATEGORIES
             else AFA_FREE_LIMIT_PAISE)
    if r.amount_paise > limit and not r.has_afa:
        return RuleResult(
            rule, PolicyVerdict.BLOCK,
            f"amount {r.amount_paise} exceeds the AFA-free limit {limit} "
            f"for category '{r.merchant_category}' and no AFA is present",
        )
    return _allow(rule)


def npci_represent_cap(r: ActionRequest) -> RuleResult:
    """Per-reason re-presentation ceiling, from the shared decline table."""
    rule = "NPCI_REPRESENT_CAP"
    if not r.action_kind.moves_money:
        return _allow(rule)
    spec = lookup(r.decline_code)
    if r.attempts_made >= spec.max_attempts:
        return RuleResult(
            rule, PolicyVerdict.BLOCK,
            f"{r.attempts_made} attempts already made; {spec.code} permits "
            f"{spec.max_attempts}",
        )
    return _allow(rule)


def trai_quiet_hours(r: ActionRequest) -> RuleResult:
    """No commercial communication outside 09:00-21:00 IST."""
    rule = "TRAI_QUIET_HOURS"
    if not r.action_kind.contacts_customer:
        return _allow(rule)
    hour = r.scheduled_for.hour
    if hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END:
        return RuleResult(
            rule, PolicyVerdict.BLOCK,
            f"scheduled {r.scheduled_for:%H:%M} IST; commercial contact is "
            f"permitted only {QUIET_HOURS_END:02d}:00-{QUIET_HOURS_START:02d}:00",
        )
    return _allow(rule)


def trai_dlt_template(r: ActionRequest) -> RuleResult:
    """SMS and WhatsApp must bind to a registered DLT template.

    Free-form commercial messaging on those channels is not permitted, so a
    generated message with no template binding is rejected before it is sent
    rather than bounced by the operator afterwards.
    """
    rule = "TRAI_DLT_TEMPLATE"
    if r.channel not in (Channel.SMS, Channel.WHATSAPP):
        return _allow(rule)
    if not r.dlt_template_id:
        return RuleResult(rule, PolicyVerdict.BLOCK,
                          f"{r.channel.value} requires a registered DLT template id")
    return _allow(rule)


def dpdp_consent(r: ActionRequest) -> RuleResult:
    """Channel-level consent. Absence of a grant is a refusal, not a default."""
    rule = "DPDP_CONSENT"
    if not r.action_kind.contacts_customer or r.channel is Channel.NONE:
        return _allow(rule)
    consent = r.consent or {}
    if not consent.get(r.channel.value, False):
        return RuleResult(rule, PolicyVerdict.BLOCK,
                          f"no recorded consent for {r.channel.value}")
    return _allow(rule)


RULES = (
    rbi_predebit_24h,
    rbi_afa_limit,
    npci_represent_cap,
    trai_quiet_hours,
    trai_dlt_template,
    dpdp_consent,
)


def evaluate(request: ActionRequest) -> list[RuleResult]:
    """Run every regulatory rule.

    All of them, not first-match. A merchant investigating a block should see
    every reason it failed, not just the first one — fixing one and rediscovering
    the next is a bad loop to put someone in.
    """
    return [rule(request) for rule in RULES]


def rule_ids() -> tuple[str, ...]:
    return tuple(rule(ActionRequest(
        action_kind=ActionKind.SUPPRESS, channel=Channel.NONE,
        scheduled_for=datetime(2026, 6, 1, 12, 0), amount_paise=1,
    )).rule_id for rule in RULES)

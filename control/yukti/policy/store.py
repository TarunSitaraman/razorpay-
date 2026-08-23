"""Loading merchant policy from the database.

`merchantpack.compile_from_settings` clamps every value and ignores unknown
keys, so a corrupt or malicious row degrades a merchant toward caution rather
than widening what may be spent on their behalf. That property is the reason
this module is thin: it fetches JSON and hands it to the compiler, and does no
interpretation of its own.

A merchant with no active pack gets `MerchantPolicy`'s defaults, which are also
conservative. The failure mode to avoid is a missing configuration reading as
"no limits" — so the absence of a row is treated as the strictest case, never
the loosest.
"""

from __future__ import annotations

from datetime import date

import psycopg

from yukti.domain.ids import new_id
from yukti.policy.merchantpack import MerchantPolicy, compile_from_settings

# Per-segment starting configuration. Segments differ in what is reasonable: an
# NBFC chasing an EMI has a legitimate reason to contact more often than a D2C
# brand chasing a cart, and a B2B invoice is large enough that a human should
# see it before an agent acts.
SEGMENT_DEFAULTS: dict[str, dict] = {
    "d2c_subscription": {
        "max_contacts_per_customer_per_week": 3, "max_discount_pct": 15.0,
        "approval_threshold_paise": 25_000_00, "min_obligation_paise": 100_00,
        "allowed_channels": ["whatsapp", "sms", "email"],
    },
    "edtech": {
        "max_contacts_per_customer_per_week": 4, "max_discount_pct": 20.0,
        "approval_threshold_paise": 50_000_00, "min_obligation_paise": 500_00,
        "allowed_channels": ["whatsapp", "sms", "email", "voice"],
    },
    "saas": {
        "max_contacts_per_customer_per_week": 2, "max_discount_pct": 10.0,
        "approval_threshold_paise": 1_00_000_00, "min_obligation_paise": 1_000_00,
        "allowed_channels": ["email", "whatsapp"],
    },
    "nbfc_lending": {
        # Higher cap because a missed EMI has consequences for the borrower too,
        # and lower discount because lending margins do not absorb one.
        "max_contacts_per_customer_per_week": 5, "max_discount_pct": 2.0,
        "approval_threshold_paise": 75_000_00, "min_obligation_paise": 500_00,
        "allowed_channels": ["whatsapp", "sms", "voice"],
    },
    "marketplace": {
        "max_contacts_per_customer_per_week": 2, "max_discount_pct": 12.0,
        "approval_threshold_paise": 15_000_00, "min_obligation_paise": 50_00,
        "allowed_channels": ["whatsapp", "sms", "email"],
    },
    "b2b_services": {
        # Two contacts a week to an accounts-payable desk is already a lot, and
        # invoice values mean a human approves most of what the agent proposes.
        "max_contacts_per_customer_per_week": 2, "max_discount_pct": 5.0,
        "approval_threshold_paise": 25_000_00, "min_obligation_paise": 5_000_00,
        "allowed_channels": ["email", "whatsapp", "voice"],
    },
}


def _most_restrictive() -> dict:
    """The tightest value of each setting across every known segment."""
    return {
        "max_contacts_per_customer_per_week": min(
            s["max_contacts_per_customer_per_week"] for s in SEGMENT_DEFAULTS.values()),
        "max_discount_pct": min(
            s["max_discount_pct"] for s in SEGMENT_DEFAULTS.values()),
        "approval_threshold_paise": min(
            s["approval_threshold_paise"] for s in SEGMENT_DEFAULTS.values()),
        "min_obligation_paise": max(
            s["min_obligation_paise"] for s in SEGMENT_DEFAULTS.values()),
        "allowed_channels": ["email"],
    }


def load_policy(conn: psycopg.Connection, merchant_id: str) -> MerchantPolicy:
    """The merchant's active policy, or conservative defaults if none exists."""
    row = conn.execute(
        "SELECT compiled FROM policy_pack "
        " WHERE merchant_id = %s AND kind = 'merchant' AND active "
        " ORDER BY version DESC LIMIT 1",
        (merchant_id,),
    ).fetchone()
    if row is None:
        return MerchantPolicy(merchant_id=merchant_id)

    settings = dict(row["compiled"] or {})
    blackout = settings.pop("blackout_dates", ())
    policy = compile_from_settings(merchant_id, settings)
    if blackout:
        # Dates arrive as ISO strings from JSONB and have to become `date` before
        # the rule can compare them; `compile_from_settings` passes them through
        # untouched, so parsing belongs here rather than there.
        parsed = frozenset(
            date.fromisoformat(d) for d in blackout if isinstance(d, str)
        )
        object.__setattr__(policy, "blackout_dates", parsed)
    return policy


def seed_defaults(conn: psycopg.Connection, approved_by: str = "seed") -> int:
    """Give every merchant an active pack from its segment defaults.

    Idempotent: a merchant that already has an active pack is left alone, so
    re-running this cannot silently reset a policy someone has since tuned.
    """
    import json
    from datetime import UTC, datetime

    merchants = conn.execute(
        "SELECT m.id, m.segment FROM merchant m "
        " WHERE NOT EXISTS (SELECT 1 FROM policy_pack p "
        "                    WHERE p.merchant_id = m.id AND p.kind = 'merchant' "
        "                      AND p.active)"
    ).fetchall()

    for m in merchants:
        # An unrecognised segment gets the most restrictive configuration on
        # offer, not a middle-of-the-road one. Adding a segment and forgetting
        # its policy should cost that merchant some recovery, never let the
        # agent spend more on their behalf than anyone intended.
        settings = SEGMENT_DEFAULTS.get(m["segment"]) or _most_restrictive()
        conn.execute(
            "INSERT INTO policy_pack "
            "(id, merchant_id, kind, version, compiled, approved_by, approved_at, active) "
            "VALUES (%s, %s, 'merchant', 1, %s, %s, %s, true)",
            (new_id("pol"), m["id"], json.dumps(settings), approved_by,
             datetime.now(UTC)),
        )
    return len(merchants)

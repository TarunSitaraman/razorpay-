"""Money as integer paise.

Every monetary value in Yukti is an ``int`` count of paise, never a float and
never a rupee-denominated decimal. Floats cannot represent 0.1 exactly, and a
recovery system that sums millions of small discounts and channel costs will
drift. Razorpay's own APIs take amounts in paise for the same reason, so this
also keeps the adapter boundary honest — no unit conversion at the edge.

Rupee values exist only for display and for parsing human input.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

PAISE_PER_RUPEE: Final[int] = 100


def rupees_to_paise(rupees: str | int | float | Decimal) -> int:
    """Convert a rupee value to integer paise, rounding half-up at the paisa.

    Accepts float only as a convenience for test fixtures; it is routed through
    ``Decimal(str(...))`` so that 0.1 parses as one-tenth rather than as the
    nearest binary double.
    """
    d = Decimal(str(rupees)) if not isinstance(rupees, Decimal) else rupees
    return int((d * PAISE_PER_RUPEE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def paise_to_rupees(paise: int) -> Decimal:
    """Exact rupee value of a paise amount, for display only."""
    return (Decimal(paise) / PAISE_PER_RUPEE).quantize(Decimal("0.01"))


def format_inr(paise: int) -> str:
    """Format paise as Indian-grouped rupees, e.g. 1234567800 -> '₹1,23,45,678.00'.

    Indian digit grouping is 3 then 2s (lakh/crore), not 3s, and merchant-facing
    numbers in the console are read by people who will notice if it is wrong.
    """
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), PAISE_PER_RUPEE)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join([*parts, tail])
    return f"{sign}₹{s}.{frac:02d}"


def apply_discount_pct(amount_paise: int, pct: Decimal | float | int) -> int:
    """Discount amount in paise for a percentage, rounded half-up.

    Returned as the *discount*, not the net, so callers must subtract explicitly.
    Making the caller do the subtraction has caught sign errors in review.
    """
    p = Decimal(str(pct))
    if not (Decimal(0) <= p <= Decimal(100)):
        raise ValueError(f"discount pct out of range: {pct}")
    return int((Decimal(amount_paise) * p / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

"""Typed, prefixed identifiers.

Prefixes follow Razorpay's own convention (``pay_``, ``order_``, ``sub_``) so
that IDs are self-describing in logs and in the audit trail. A bare UUID in a
stack trace tells you nothing; ``case_01JBX...`` tells you which table to open.

The suffix is a ULID-style value: lexicographically sortable by creation time,
which makes ``ORDER BY id`` a valid chronological ordering and keeps B-tree
inserts append-mostly rather than scattering across the index.
"""

from __future__ import annotations

import os
import time
from typing import Final

_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # excludes I, L, O, U


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def ulid() -> str:
    """48-bit millisecond timestamp + 80 bits of randomness, Crockford base32."""
    return _encode(int(time.time() * 1000), 10) + _encode(
        int.from_bytes(os.urandom(10), "big"), 16
    )


def new_id(prefix: str) -> str:
    return f"{prefix}_{ulid()}"


def merchant_id() -> str:   return new_id("mrc")
def customer_id() -> str:   return new_id("cus")
def obligation_id() -> str: return new_id("obl")
def attempt_id() -> str:    return new_id("att")
def case_id() -> str:       return new_id("case")
def decision_id() -> str:   return new_id("dec")
def action_id() -> str:     return new_id("act")
def run_id() -> str:        return new_id("run")
def trace_id() -> str:      return new_id("yk")
def promise_id() -> str:    return new_id("ptp")
def event_id() -> str:      return new_id("evt")
def experiment_id() -> str: return new_id("exp")
def degradation_id() -> str: return new_id("deg")

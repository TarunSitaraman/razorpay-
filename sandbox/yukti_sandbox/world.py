"""Ground truth, resolved by the simulator rather than supplied by its caller.

Why this module exists at all.

The outcome oracle needs facts the control plane must never know: which uplift
archetype a customer really is, whether their issuer is genuinely down right
now, whether they really have an unbroken promise outstanding. The sandbox
previously took these in the request body — `ChargeRequest.archetype`, default
`"persuadable"`.

That is a back door. For the dispatcher to fill that field, the control plane
would have to `SELECT archetype FROM customer`, and ground truth would enter the
decision path through the one component nobody re-reads. The whole day-3 design
turns on archetype never being readable by Yukti; enforcing it in `features.py`
and then handing it over the wire in `dispatch/` would be enforcement in name
only.

So the direction is inverted. The simulator resolves what the simulator is
entitled to know, and the adapter sends only what a real Razorpay call carries:
amount, identifiers, rail, idempotency key. That is also what makes the "swap in
live keys and it works" claim true rather than aspirational — a request that
carries a field Razorpay has never heard of would not survive the swap.

This reads the generator's tables directly, which is the one place the sandbox
is allowed to: those columns describe the world, not Yukti's beliefs about it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

# Used when the world cannot be consulted — an unseeded database, or a unit test
# exercising the HTTP contract rather than the simulation. Deliberately the
# middle archetype: an unknown customer should behave averagely rather than
# conveniently.
FALLBACK_ARCHETYPE = "persuadable"


@dataclass(frozen=True, slots=True)
class GroundTruth:
    archetype: str
    open_promise: bool
    in_downtime: bool
    prior_contacts_7d: int
    resolved: bool          # False when we fell back to defaults


_conn: psycopg.Connection | None = None
# Archetype never changes for a customer, so it is cached for the process. The
# time-varying facts are not cached — caching "is the issuer down" would make
# the simulator disagree with itself across a replay.
_archetype_cache: dict[str, str] = {}


def _connection() -> psycopg.Connection | None:
    global _conn
    if _conn is not None and not _conn.closed:
        return _conn
    url = os.environ.get("YUKTI_DATABASE_URL",
                         "postgresql://yukti:yukti@localhost:5432/yukti")
    try:
        _conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    except psycopg.Error:
        # The sandbox must stay up without a database. It degrades to default
        # behaviour rather than failing the merchant's API call, which is how a
        # real PSP behaves when a downstream of its own is unavailable.
        _conn = None
    return _conn


def reset_cache() -> None:
    _archetype_cache.clear()


def resolve(customer_id: str, obligation_id: str, issuer: str | None,
            at: datetime) -> GroundTruth:
    """Look up the true state of the world for this customer and obligation."""
    conn = _connection()
    if conn is None:
        return GroundTruth(FALLBACK_ARCHETYPE, False, False, 0, resolved=False)

    try:
        archetype = _archetype_cache.get(customer_id)
        if archetype is None:
            row = conn.execute(
                "SELECT archetype FROM customer WHERE id = %s", (customer_id,)
            ).fetchone()
            archetype = (row or {}).get("archetype") or FALLBACK_ARCHETYPE
            _archetype_cache[customer_id] = archetype

        promise = conn.execute(
            "SELECT 1 FROM promise_to_pay "
            " WHERE obligation_id = %s AND state = 'open' AND promised_for >= %s::date",
            (obligation_id, at),
        ).fetchone()

        downtime = False
        if issuer:
            # Injected episodes are the ground truth. The control plane has to
            # *detect* degradation statistically; the simulator simply knows.
            downtime = conn.execute(
                """
                SELECT 1 FROM degradation_signal
                 WHERE state = 'ground_truth' AND dimension = 'issuer'
                   AND dimension_value = %s
                   AND window_start <= %s AND window_end > %s
                """,
                (issuer, at, at),
            ).fetchone() is not None

        contacts = conn.execute(
            """
            SELECT count(*) AS n
              FROM recovery_action a
              JOIN recovery_case c ON c.id = a.case_id
             WHERE c.customer_id = %s
               AND a.dispatched_at >= %s - interval '7 days'
               AND a.kind IN ('message', 'voice_call', 'discount_offer')
            """,
            (customer_id, at),
        ).fetchone()["n"]

        return GroundTruth(
            archetype=archetype,
            open_promise=promise is not None,
            in_downtime=downtime,
            prior_contacts_7d=int(contacts),
            resolved=True,
        )
    except psycopg.Error:
        return GroundTruth(FALLBACK_ARCHETYPE, False, False, 0, resolved=False)

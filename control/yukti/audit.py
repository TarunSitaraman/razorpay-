"""Append-only, hash-chained audit trail.

The track brief asks for an audit trail, and the distinction that matters is
between a log and evidence. A log records what a system says it did. Evidence is
a log you cannot quietly edit afterwards.

Each row commits to the one before it for the same merchant:

    hash_n = BLAKE2b( hash_{n-1} || canonical(row_n) )

Deleting a row, reordering two, or changing one rupee in a `detail` payload
breaks every hash from that point on, and `verify` reports the first row where
the chain parts. That is a much stronger claim than "we have logs", and it costs
one hash per decision.

Chained per merchant rather than globally for a practical reason: a global chain
serialises every write in the system behind one row, which would make the audit
log the throughput ceiling for the whole control plane. Per-merchant chains are
independent, and a merchant only ever needs to verify their own.

Canonicalisation is where this kind of scheme usually fails. JSON key order,
whitespace and float formatting all change the bytes without changing the
meaning, so a chain that verified at write time stops verifying after a
round-trip through the database. `_canonical` sorts keys, fixes separators and
serialises through the same path on both sides.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import psycopg

from yukti.domain.enums import ActorKind

GENESIS = "0" * 64


def _canonical(
    trace_id: str, merchant_id: str, actor: str, action: str,
    subject_id: str | None, detail: dict[str, Any],
) -> bytes:
    """Stable bytes for a row. Same input, same bytes, on any machine."""
    return json.dumps(
        {
            "trace_id": trace_id,
            "merchant_id": merchant_id,
            "actor": actor,
            "action": action,
            "subject_id": subject_id,
            "detail": detail,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()


def chain_hash(prev_hash: str | None, payload: bytes) -> str:
    return hashlib.blake2b(
        (prev_hash or GENESIS).encode() + payload, digest_size=32
    ).hexdigest()


def append(
    conn: psycopg.Connection,
    *,
    trace_id: str,
    merchant_id: str,
    action: str,
    actor: ActorKind = ActorKind.SYSTEM,
    subject_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> str:
    """Append one audit row and return its hash.

    MUST be called inside the caller's transaction, alongside the thing being
    audited. An audit row that commits separately from the decision it describes
    can disagree with it, and the disagreement is unrecoverable — you cannot
    tell afterwards which one was true.

    The tip is read `FOR UPDATE` so two concurrent appends for the same merchant
    cannot both chain off the same predecessor and fork the chain. Contention is
    per merchant, which is the point of chaining per merchant.
    """
    detail = detail or {}

    tip = conn.execute(
        "SELECT hash FROM audit_event WHERE merchant_id = %s "
        "ORDER BY id DESC LIMIT 1 FOR UPDATE",
        (merchant_id,),
    ).fetchone()
    prev_hash = tip["hash"] if tip else None

    payload = _canonical(trace_id, merchant_id, actor.value, action, subject_id, detail)
    digest = chain_hash(prev_hash, payload)

    conn.execute(
        "INSERT INTO audit_event "
        "(trace_id, merchant_id, actor, action, subject_id, detail, prev_hash, hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (trace_id, merchant_id, actor.value, action, subject_id,
         json.dumps(detail, default=str), prev_hash, digest),
    )
    return digest


@dataclass(frozen=True, slots=True)
class ChainStatus:
    merchant_id: str
    rows: int
    intact: bool
    broken_at: int | None = None
    reason: str = ""

    def __str__(self) -> str:
        if self.intact:
            return f"{self.merchant_id}: {self.rows} rows, chain intact"
        return (f"{self.merchant_id}: chain BROKEN at row {self.broken_at} "
                f"({self.reason})")


def verify(conn: psycopg.Connection, merchant_id: str) -> ChainStatus:
    """Walk a merchant's chain and report the first row that does not verify."""
    rows = conn.execute(
        "SELECT id, trace_id, merchant_id, actor, action, subject_id, detail, "
        "       prev_hash, hash "
        "  FROM audit_event WHERE merchant_id = %s ORDER BY id",
        (merchant_id,),
    ).fetchall()

    expected_prev: str | None = None
    for row in rows:
        # Checked separately from the hash so the failure is diagnosable: a
        # wrong link means a row was removed, a wrong hash means one was edited.
        if row["prev_hash"] != expected_prev:
            return ChainStatus(
                merchant_id, len(rows), False, row["id"],
                f"prev_hash {row['prev_hash']!r} does not match the preceding "
                f"row's hash {expected_prev!r} — a row was removed or reordered",
            )

        payload = _canonical(row["trace_id"], row["merchant_id"], row["actor"],
                             row["action"], row["subject_id"], row["detail"])
        if chain_hash(row["prev_hash"], payload) != row["hash"]:
            return ChainStatus(
                merchant_id, len(rows), False, row["id"],
                "row content does not hash to its recorded hash — it was edited "
                "after it was written",
            )
        expected_prev = row["hash"]

    return ChainStatus(merchant_id, len(rows), True)


def verify_all(conn: psycopg.Connection) -> list[ChainStatus]:
    merchants = conn.execute(
        "SELECT DISTINCT merchant_id FROM audit_event ORDER BY merchant_id"
    ).fetchall()
    return [verify(conn, m["merchant_id"]) for m in merchants]

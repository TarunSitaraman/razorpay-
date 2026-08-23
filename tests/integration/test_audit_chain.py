"""The audit chain must detect edits, deletions and reordering.

A hash chain that is never adversarially tested is decoration. These tests
tamper with committed rows the way someone covering their tracks would, and
assert the chain notices.
"""

from __future__ import annotations

import json

from yukti import audit
from yukti.domain.enums import ActorKind


def _append(conn, mid: str, n: int) -> list[str]:
    return [
        audit.append(conn, trace_id=f"yk_{i}", merchant_id=mid,
                     action="test.event", actor=ActorKind.SYSTEM,
                     subject_id=f"subj_{i}", detail={"i": i, "amount_paise": 1000 + i})
        for i in range(n)
    ]


def test_intact_chain_verifies(conn, merchant):
    _append(conn, merchant, 5)
    status = audit.verify(conn, merchant)
    assert status.intact
    assert status.rows == 5


def test_first_row_links_to_genesis(conn, merchant):
    _append(conn, merchant, 1)
    row = conn.execute(
        "SELECT prev_hash FROM audit_event WHERE merchant_id = %s", (merchant,)
    ).fetchone()
    # Null rather than a literal genesis string: the first row has no
    # predecessor, and saying so is more honest than inventing one.
    assert row["prev_hash"] is None


def test_editing_a_detail_breaks_the_chain(conn, merchant):
    _append(conn, merchant, 4)
    target = conn.execute(
        "SELECT id FROM audit_event WHERE merchant_id = %s ORDER BY id OFFSET 2 LIMIT 1",
        (merchant,),
    ).fetchone()["id"]

    # The edit someone would actually make: change the amount, leave everything
    # else alone so the row still looks plausible.
    conn.execute("UPDATE audit_event SET detail = %s WHERE id = %s",
               (json.dumps({"i": 2, "amount_paise": 999_999}), target))

    status = audit.verify(conn, merchant)
    assert not status.intact
    assert status.broken_at == target
    assert "hash" in status.reason


def test_deleting_a_row_breaks_the_chain(conn, merchant):
    _append(conn, merchant, 4)
    target = conn.execute(
        "SELECT id FROM audit_event WHERE merchant_id = %s ORDER BY id OFFSET 1 LIMIT 1",
        (merchant,),
    ).fetchone()["id"]
    conn.execute("DELETE FROM audit_event WHERE id = %s", (target,))

    status = audit.verify(conn, merchant)
    assert not status.intact
    assert "removed or reordered" in status.reason


def test_chains_are_independent_per_merchant(conn, merchant):
    """One merchant's tampering must not invalidate another's chain."""
    from yukti.domain.ids import merchant_id
    other = merchant_id()
    conn.execute("INSERT INTO merchant (id, name, segment) VALUES (%s, 'Other', 'saas')",
               (other,))
    _append(conn, merchant, 3)
    _append(conn, other, 3)

    victim = conn.execute(
        "SELECT id FROM audit_event WHERE merchant_id = %s ORDER BY id LIMIT 1",
        (merchant,),
    ).fetchone()["id"]
    conn.execute("UPDATE audit_event SET action = 'tampered' WHERE id = %s", (victim,))

    assert not audit.verify(conn, merchant).intact
    assert audit.verify(conn, other).intact


def test_detail_key_order_does_not_affect_the_hash(conn, merchant):
    """Canonicalisation, not luck.

    JSONB does not preserve key order, so a chain that hashed raw JSON text
    would verify at write time and fail after a round trip through Postgres.
    This is the property that makes the scheme survive its own storage.
    """
    a = audit._canonical("t", "m", "system", "act", "s", {"b": 2, "a": 1})
    b = audit._canonical("t", "m", "system", "act", "s", {"a": 1, "b": 2})
    assert a == b


def test_append_is_ordered_under_the_same_merchant(conn, merchant):
    hashes = _append(conn, merchant, 6)
    rows = conn.execute(
        "SELECT hash, prev_hash FROM audit_event WHERE merchant_id = %s ORDER BY id",
        (merchant,),
    ).fetchall()
    assert [r["hash"] for r in rows] == hashes
    # Each row commits to its predecessor, which is what makes a deletion
    # anywhere in the chain detectable from the end.
    assert all(rows[i]["prev_hash"] == rows[i - 1]["hash"] for i in range(1, len(rows)))

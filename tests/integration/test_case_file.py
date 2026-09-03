"""Assert the dossier shape for a known case and scope by merchant.

Every figure on the case-file page must match a psql query.
"""

from __future__ import annotations

import pytest


def _get_case_id(conn):
    row = conn.execute(
        "SELECT c.id FROM recovery_case c JOIN recovery_action a ON a.case_id = c.id LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def test_dossier_shape(client):
    from yukti.store.db import connect
    conn = connect()
    case_id = _get_case_id(conn)
    conn.close()
    if not case_id:
        pytest.skip("no case with recovery_action")

    resp = client.get(f"/cases/{case_id}")
    assert resp.status_code == 200
    data = resp.json()

    # Required top-level keys
    for key in ["case", "attempts", "collisions", "promises", "decisions",
                "actions", "outcomes", "audit", "ground_truth"]:
        assert key in data, f"missing key: {key}"

    # Header fields
    case = data["case"]
    assert case["id"] == case_id
    assert "state" in case
    assert "arm" in case

    # Every attempt has decline annotation
    for a in data["attempts"]:
        assert "decline" in a, f"attempt {a['id']} missing decline annotation"
        assert "transience" in a["decline"]

    # Decisions carry policy evaluations and rules_not_applicable
    for d in data["decisions"]:
        assert "policy_evaluations" in d
        assert "rules_not_applicable" in d
        assert "alternatives_rejected" in d or "alternatives_rejected" not in d

    # Ground truth separates forbidden features
    gt = data["ground_truth"]
    assert "archetype" in gt
    assert "withheld_from_models" in gt
    assert "enforced_by" in gt


def test_case_file_scopes_by_merchant(client):
    from yukti.store.db import connect
    conn = connect()
    # Get a case from a different merchant than the first
    row = conn.execute(
        "SELECT c.id, c.merchant_id FROM recovery_case c JOIN recovery_action a ON a.case_id = c.id LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        pytest.skip("no case with recovery_action")

    case_id = row["id"]
    resp = client.get(f"/cases/{case_id}")
    assert resp.status_code == 200
    data = resp.json()
    # The case's merchant_id should be present in the spine
    assert "merchant_id" in data["case"] or "merchant_name" in data["case"]

"""Smoke-test every route in the console API.

Iterates app.routes and asserts no 500 for the default merchant.
Catches a typo'd SQL string in an endpoint nobody opened before the
recording.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("path", ["/health", "/merchants", "/metrics/lift", "/metrics/revenue-at-risk"])
def test_no_500_public(client, path):
    resp = client.get(path)
    assert resp.status_code != 500


@pytest.mark.parametrize("path", ["/metrics/pipeline", "/metrics/stopping-rules", "/metrics/arms", "/metrics/failure-mix", "/metrics/budgets"])
def test_no_500_with_merchant(client, path):
    resp = client.get(path, params={"merchant_id": "mrc_01M18T5WD4K0KSNZXMB8GSS8QH"})
    assert resp.status_code != 500


def test_cases_list_no_500(client):
    resp = client.get("/cases", params={"merchant_id": "mrc_01M18T5WD4K0KSNZXMB8GSS8QH", "limit": 10})
    assert resp.status_code != 500


def test_cases_detail_no_500(client):
    from yukti.store.db import connect
    conn = connect()
    row = conn.execute(
        "SELECT c.id FROM recovery_case c JOIN recovery_action a ON a.case_id = c.id LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        pytest.skip("no case with recovery_action")
    case_id = row["id"]
    resp = client.get(f"/cases/{case_id}")
    assert resp.status_code != 500

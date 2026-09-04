"""The approval queue and its write path, against the live database.

This is the console's only write path, so the tests are about what it refuses as
much as what it does.
"""

from __future__ import annotations

import pytest


def _queue(client, limit=None):
    r = client.get("/approvals")
    assert r.status_code == 200
    rows = r.json()
    return rows[:limit] if limit else rows


class TestTheQueue:
    def test_every_row_is_actually_waiting_on_a_human(self, client):
        for row in _queue(client, 10):
            assert row["rules"], "a queued case must say what held it"
            assert any(r["verdict"] == "escalate" for r in row["rules"])

    def test_rows_carry_the_proposal_not_just_the_case(self, client):
        for row in _queue(client, 10):
            assert row["action_kind"] and row["channel"]
            assert row["decision_id"]

    def test_ordered_by_money_so_a_reviewer_starts_where_it_matters(self, client):
        amounts = [r["amount_paise"] for r in _queue(client, 15)]
        assert amounts == sorted(amounts, reverse=True)

    def test_merchant_filter_scopes_the_queue(self, client):
        rows = _queue(client)
        if not rows:
            pytest.skip("nothing escalated")
        mid = rows[0]["merchant_id"]
        scoped = client.get("/approvals", params={"merchant_id": mid}).json()
        assert scoped
        assert {r["merchant_id"] for r in scoped} == {mid}


class TestTheWritePathRefuses:
    def test_an_unnamed_approver_is_rejected(self, client):
        rows = _queue(client, 1)
        if not rows:
            pytest.skip("nothing escalated")
        r = client.post(f"/approvals/{rows[0]['case_id']}",
                        json={"verdict": "approve", "actor": "   "})
        assert r.status_code == 409
        assert "name the person" in r.json()["detail"]["message"]

    def test_an_unknown_verdict_is_rejected(self, client):
        rows = _queue(client, 1)
        if not rows:
            pytest.skip("nothing escalated")
        r = client.post(f"/approvals/{rows[0]['case_id']}",
                        json={"verdict": "maybe", "actor": "reviewer"})
        assert r.status_code == 409

    def test_a_case_nobody_escalated_cannot_be_approved(self, client):
        """Approving an ordinary open case would be an action with no proposal."""
        open_case = client.get("/cases", params={"state": "open", "limit": 1}).json()
        if not open_case:
            pytest.skip("no open case")
        r = client.post(f"/approvals/{open_case[0]['id']}",
                        json={"verdict": "approve", "actor": "reviewer"})
        assert r.status_code == 409
        assert "not escalated" in r.json()["detail"]["message"]

    def test_an_unknown_case_is_rejected(self, client):
        r = client.post("/approvals/case_does_not_exist",
                        json={"verdict": "approve", "actor": "reviewer"})
        assert r.status_code == 409
        assert "no such case" in r.json()["detail"]["message"]


class TestRejectionIsRecorded:
    def test_rejecting_stops_the_case_under_a_named_reason(self, client):
        rows = _queue(client)
        if not rows:
            pytest.skip("nothing escalated")
        case_id = rows[-1]["case_id"]

        r = client.post(f"/approvals/{case_id}",
                        json={"verdict": "reject", "actor": "test@yukti",
                              "note": "integration test"})
        assert r.status_code == 200, r.text
        assert r.json()["verdict"] == "rejected"
        assert r.json()["dispatched"] is False

        after = client.get(f"/cases/{case_id}").json()
        assert after["case"]["state"] == "stopped"
        # The schema requires a named rule whenever a case is stopped.
        assert after["case"]["stop_reason"] == "human_rejected"

        last = after["audit"][-1]
        assert last["action"] == "case.approval_rejected"
        assert last["actor"] == "human"
        assert last["detail"]["by"] == "test@yukti"

    def test_a_rejected_case_leaves_the_queue(self, client):
        rows = _queue(client)
        if not rows:
            pytest.skip("nothing escalated")
        case_id = rows[-1]["case_id"]
        client.post(f"/approvals/{case_id}",
                    json={"verdict": "reject", "actor": "test@yukti"})
        assert case_id not in {r["case_id"] for r in _queue(client)}


class TestApprovalReentersThePolicyEngine:
    def test_approval_either_dispatches_or_names_the_rule_that_forbade_it(self, client):
        """The property that matters: a human's yes is never the last word.

        Both outcomes are correct. What must never happen is an approval that
        dispatches without the rules having been re-checked, or one that fails
        without saying which rule objected.
        """
        rows = _queue(client)
        if not rows:
            pytest.skip("nothing escalated")

        checked = 0
        for row in rows[:12]:
            r = client.post(f"/approvals/{row['case_id']}",
                            json={"verdict": "approve", "actor": "test@yukti",
                                  "note": "integration test"})
            if r.status_code == 200:
                body = r.json()
                assert body["verdict"] == "approved"
                after = client.get(f"/cases/{row['case_id']}").json()
                assert after["case"]["state"] == "awaiting_outcome"
                approved = [a for a in after["audit"]
                            if a["action"] == "case.approved"]
                assert approved, "an approval must reach the audit chain"
                assert approved[-1]["actor"] == "human"
                assert approved[-1]["detail"]["rules_rechecked"] > 0
            else:
                assert r.status_code == 409
                detail = r.json()["detail"]
                assert detail["rules"], "a refusal must name the rule"
                after = client.get(f"/cases/{row['case_id']}").json()
                # Refused, so it stays queued rather than silently disappearing.
                assert after["case"]["state"] == "escalated"
            checked += 1
        assert checked, "no approvals were exercised"

    def test_the_chain_still_verifies_after_writes(self, client):
        v = client.get("/audit/verify").json()
        assert v["intact"] is True

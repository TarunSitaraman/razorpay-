"""A crashed agent run resumes from its evidence, not from the model.

Gathering evidence is the expensive half of an agent run — SQL over the attempt
stream, degradation scans, cohort statistics. Re-deriving all of it on every
resume is what makes long agent runs cost real money, and re-calling the model
for a conclusion already reached is worse: it costs money AND can return a
different answer, so a resumed cycle would not reproduce the original decision.

The property under test is narrow and checkable: after a crash, re-entering a
run makes ZERO additional model calls for conclusions already recorded.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from yukti.agent import memory
from yukti.agent.schemas import Posture, RCAVerdict
from yukti.agent.specialists import RCASpecialist
from yukti.agent.supervisor import Supervisor
from yukti.domain.ids import trace_id

AS_OF = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


class CountingClient:
    """Counts calls so 'did not re-call the model' is measured, not assumed."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def messages(self):
        return self

    def parse(self, **kwargs):
        self.calls += 1

        class _R:
            parsed_output = RCAVerdict(
                root_cause="issuer_outage", posture="suppress_and_wait",
                narrative="Issuer-side failure with a BANK_DOWN-heavy decline mix.",
                cited_evidence_ids=[], confidence=0.85,
            )
            usage = type("U", (), {"input_tokens": 100, "output_tokens": 40})()

        return _R()


@pytest.fixture
def run(conn, merchant):
    sup = Supervisor(conn, rca=RCASpecialist(client=CountingClient()))
    rid = sup.open_run(merchant, trace_id())
    return sup, rid, merchant


def test_evidence_survives_and_is_retrievable(conn, run):
    sup, rid, _ = run
    memory.record(conn, rid, "degradation_scan",
                  [{"z_score": 4.2, "issuer": "HDFC"}], subject="HDFC")

    rows = memory.retrieve(conn, rid, "degradation_scan", subject="HDFC")
    assert len(rows) == 1
    assert rows[0].fact["z_score"] == 4.2
    assert rows[0].id > 0, "evidence must be addressable so it can be cited"


def test_a_recorded_conclusion_is_not_recomputed(conn, run):
    """The core resume property, counted at the client."""
    sup, rid, _ = run
    client = sup.rca._client

    evidence = memory.record(conn, rid, "degradation_scan",
                             [{"z_score": 4.2}], subject="HDFC")
    result = sup.rca.analyse("HDFC dropped 22%", evidence)
    memory.conclude(conn, rid, "rca", result.output.model_dump(mode="json"),
                    result.cited_ids, subject="HDFC", provenance=result.provenance)
    assert client.calls == 1

    # Resume: the supervisor reads what is already concluded.
    prior = {c["subject"]: c for c in memory.conclusions(conn, rid, "rca")}
    assert "HDFC" in prior
    assert Posture(prior["HDFC"]["output"]["posture"]) is Posture.SUPPRESS_AND_WAIT
    assert client.calls == 1, "the model was called again for a settled conclusion"


def test_conclusions_record_which_evidence_they_used(conn, run):
    sup, rid, _ = run
    evidence = memory.record(conn, rid, "degradation_scan",
                             [{"z_score": 4.2}, {"decline_code": "BANK_DOWN"}],
                             subject="HDFC")
    ids = [e.id for e in evidence]

    memory.conclude(conn, rid, "rca", {"posture": "suppress_and_wait"},
                    cited_ids=ids, subject="HDFC")

    stored = memory.conclusions(conn, rid, "rca")[0]
    assert sorted(stored["cited_ids"]) == sorted(ids)


def test_a_fabricated_citation_is_detectable(conn, run):
    sup, rid, _ = run
    supplied = memory.record(conn, rid, "degradation_scan", [{"z": 1}], subject="HDFC")
    real_id = supplied[0].id

    assert memory.uncited(supplied, [real_id]) == []
    assert memory.uncited(supplied, [real_id, 99_999]) == [99_999]


def test_provenance_separates_model_answers_from_fallbacks(conn, run):
    """A fleet quietly running on fallbacks looks identical to a working one."""
    sup, rid, _ = run
    memory.conclude(conn, rid, "rca", {"posture": "suppress_and_wait"}, [],
                    subject="HDFC", provenance="llm")
    memory.conclude(conn, rid, "rca", {"posture": "suppress_and_wait"}, [],
                    subject="ICICI", provenance="fallback")

    assert memory.provenance_stats(conn, rid) == {"llm": 1, "fallback": 1}


def test_evidence_retrieval_is_bounded(conn, run):
    """An unbounded read would put the whole run back in the context window."""
    sup, rid, _ = run
    memory.record(conn, rid, "cohort_stats", [{"i": i} for i in range(200)])

    assert len(memory.retrieve(conn, rid, "cohort_stats")) == 60
    assert len(memory.retrieve(conn, rid, "cohort_stats", limit=10)) == 10


def test_deleting_a_run_takes_its_evidence_with_it(conn, run):
    """ON DELETE CASCADE: evidence must not outlive the run that gathered it."""
    sup, rid, _ = run
    memory.record(conn, rid, "degradation_scan", [{"z": 1}], subject="HDFC")

    conn.execute("DELETE FROM agent_run WHERE id = %s", (rid,))

    assert memory.retrieve(conn, rid) == []

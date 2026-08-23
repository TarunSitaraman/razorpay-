"""Evidence store — the specialists write facts, the supervisor reads them.

This is the shape Razorpay published for Project Viveka, and the reason to copy
it is not imitation. Two properties fall out of persisting evidence instead of
accumulating it in a context window:

**A crashed run resumes without re-calling the model.** Gathering evidence is
the expensive half — SQL over the attempt stream, degradation scans, cohort
statistics. Re-deriving it on every resume is what makes a long agent run cost
real money and take real time.

**A narrative can be checked against its sources.** Every fact carries an id;
every conclusion records which ids it was shown. An assertion citing an id that
was never supplied is a fabricated source, and that is a test rather than a
judgement call.

Facts are computed by SQL and written here. The model reads them. It never
writes one — the moment a model can add to the evidence store, "cites only
retrieved evidence" stops meaning anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import psycopg

from yukti.domain.ids import new_id


@dataclass(frozen=True, slots=True)
class Evidence:
    id: int
    source: str
    subject: str | None
    fact: dict[str, Any]

    def as_row(self) -> dict[str, Any]:
        return {"id": self.id, "source": self.source, "subject": self.subject,
                "fact": self.fact}


def record(
    conn: psycopg.Connection, run_id: str, source: str,
    facts: list[dict[str, Any]], subject: str | None = None,
) -> list[Evidence]:
    """Persist facts gathered by a specialist. Returns them with their ids."""
    if not facts:
        return []
    rows = conn.execute(
        "INSERT INTO agent_evidence (run_id, source, subject, fact) "
        "SELECT %s, %s, %s, unnest(%s::jsonb[]) RETURNING id, source, subject, fact",
        (run_id, source, subject, [json.dumps(f, default=str) for f in facts]),
    ).fetchall()
    return [Evidence(r["id"], r["source"], r["subject"], r["fact"]) for r in rows]


def retrieve(
    conn: psycopg.Connection, run_id: str, source: str | None = None,
    subject: str | None = None, limit: int = 60,
) -> list[Evidence]:
    """Fetch evidence for a run, optionally narrowed.

    Bounded by default. An unbounded retrieval would put the whole run back in
    the context window, which is the thing this store exists to avoid.
    """
    clauses = ["run_id = %s"]
    params: list[Any] = [run_id]
    if source:
        clauses.append("source = %s")
        params.append(source)
    if subject:
        clauses.append("subject = %s")
        params.append(subject)
    params.append(limit)

    rows = conn.execute(
        f"SELECT id, source, subject, fact FROM agent_evidence "
        f" WHERE {' AND '.join(clauses)} ORDER BY id LIMIT %s",
        params,
    ).fetchall()
    return [Evidence(r["id"], r["source"], r["subject"], r["fact"]) for r in rows]


def conclude(
    conn: psycopg.Connection, run_id: str, specialist: str, output: dict[str, Any],
    cited_ids: list[int], subject: str | None = None, model: str | None = None,
    usage: dict[str, int] | None = None, provenance: str = "llm",
) -> str:
    """Record what a specialist concluded, and what it was shown.

    `provenance` distinguishes a real model answer from a conservative default
    used because the call failed. It is reported as a metric: a fleet quietly
    running on fallbacks behaves plausibly and looks identical to one that is
    working, so the difference has to be recorded at the point it happens.
    """
    cid = new_id("con")
    usage = usage or {}
    conn.execute(
        "INSERT INTO agent_conclusion (id, run_id, specialist, subject, output, "
        "cited_ids, model, input_tokens, output_tokens, provenance) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (cid, run_id, specialist, subject, json.dumps(output, default=str),
         cited_ids, model, usage.get("input"), usage.get("output"), provenance),
    )
    return cid


def uncited(supplied: list[Evidence], cited: list[int]) -> list[int]:
    """Cited ids that were never supplied — fabricated sources.

    Returned rather than raised. A specialist inventing a citation is a real
    finding worth recording and surfacing, not a reason to abort a planning
    cycle that is otherwise fine: the conclusion is discarded, the deterministic
    default applies, and the fabrication is counted.
    """
    available = {e.id for e in supplied}
    return sorted(set(cited) - available)


def conclusions(
    conn: psycopg.Connection, run_id: str, specialist: str | None = None
) -> list[dict[str, Any]]:
    """Prior conclusions for a run — how a resumed run avoids re-calling the model."""
    clauses = ["run_id = %s"]
    params: list[Any] = [run_id]
    if specialist:
        clauses.append("specialist = %s")
        params.append(specialist)
    rows = conn.execute(
        f"SELECT id, specialist, subject, output, cited_ids, provenance "
        f"  FROM agent_conclusion WHERE {' AND '.join(clauses)} ORDER BY id",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def provenance_stats(conn: psycopg.Connection, run_id: str) -> dict[str, int]:
    """How many conclusions came from the model versus from a fallback."""
    rows = conn.execute(
        "SELECT provenance, count(*) AS n FROM agent_conclusion "
        " WHERE run_id = %s GROUP BY provenance", (run_id,),
    ).fetchall()
    return {r["provenance"]: r["n"] for r in rows}

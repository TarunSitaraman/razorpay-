"""Merchant console API.

Read-only for now. Every write path in Yukti goes through the dispatcher and
the policy engine, never through an HTTP handler, so there is deliberately no
"trigger recovery" endpoint here — an action that skipped the policy engine
would be exactly the failure mode the whole design exists to prevent.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import psycopg_pool
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from yukti.api import queries
from yukti.config import settings
from yukti.domain.money import format_inr

pool: psycopg_pool.ConnectionPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    from psycopg.rows import dict_row

    pool = psycopg_pool.ConnectionPool(
        settings().database_url, min_size=1, max_size=8, open=True,
        kwargs={"row_factory": dict_row},
    )
    yield
    pool.close()


app = FastAPI(title="Yukti", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def _q(fn, *args, **kwargs) -> Any:
    assert pool is not None
    with pool.connection() as conn:
        return fn(conn, *args, **kwargs)


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        _q(lambda c: c.execute("SELECT 1").fetchone())
        return {"status": "ok", "database": "up"}
    except Exception as exc:  # noqa: BLE001 - health must report, not raise
        return {"status": "degraded", "database": "down", "detail": str(exc)}


@app.get("/merchants")
def list_merchants() -> list[dict]:
    return _q(queries.merchants)


@app.get("/metrics/revenue-at-risk")
def revenue_at_risk(merchant_id: str | None = Query(None)) -> dict[str, Any]:
    data = _q(queries.revenue_at_risk, merchant_id)
    data["total_display"] = format_inr(data["total_paise"])
    for row in data["by_surface"]:
        row["display"] = format_inr(row["amount_paise"])
    return data


@app.get("/metrics/pipeline")
def pipeline(merchant_id: str | None = Query(None)) -> dict[str, int]:
    return _q(queries.pipeline_counts, merchant_id)


@app.get("/metrics/stopping-rules")
def stopping_rules(merchant_id: str | None = Query(None)) -> dict[str, Any]:
    rows = _q(queries.stop_reason_breakdown, merchant_id)
    for r in rows:
        r["display"] = format_inr(r["amount_paise"])
    return {
        "rules": rows,
        "total_not_chased_paise": sum(r["amount_paise"] for r in rows),
        "total_not_chased_display": format_inr(sum(r["amount_paise"] for r in rows)),
    }


@app.get("/metrics/arms")
def arms(merchant_id: str | None = Query(None)) -> list[dict]:
    rows = _q(queries.arm_split, merchant_id)
    for r in rows:
        r["display"] = format_inr(r["recovered_paise"])
        r["recovery_rate"] = round(r["recovered"] / r["cases"], 4) if r["cases"] else 0.0
    return rows


@app.get("/metrics/failure-mix")
def failure_mix(merchant_id: str | None = Query(None)) -> list[dict]:
    rows = _q(queries.failure_mix, merchant_id)
    for r in rows:
        r["display"] = format_inr(r["amount_paise"])
    return rows


@app.get("/decisions")
def decisions(
    merchant_id: str | None = Query(None), limit: int = Query(50, le=500)
) -> list[dict]:
    """The live decision feed: what was chosen, and what was turned down."""
    rows = _q(queries.recent_decisions, merchant_id, limit)
    for r in rows:
        r["display"] = format_inr(r["amount_paise"])
        r["margin_display"] = format_inr(r["expected_incr_margin_paise"] or 0)
    return rows


@app.get("/metrics/policy")
def policy_metrics(merchant_id: str | None = Query(None)) -> list[dict]:
    """Blocks and escalations by named rule — "the agent wanted this; this stopped it"."""
    rows = _q(queries.policy_breakdown, merchant_id)
    for r in rows:
        r["display"] = format_inr(r["amount_paise"])
    return rows


@app.get("/metrics/budgets")
def budget_metrics(
    merchant_id: str | None = Query(None), window: str | None = Query(None)
) -> list[dict]:
    from datetime import date as _date

    parsed = _date.fromisoformat(window) if window else None
    rows = _q(queries.budget_state, merchant_id, parsed)
    for r in rows:
        if r["kind"] == "discount":
            r["display"] = format_inr(r["consumed_val"])
            r["limit_display"] = format_inr(r["limit_val"])
        else:
            r["display"] = f"{r['consumed_val']:,}"
            r["limit_display"] = f"{r['limit_val']:,}"
    return rows


@app.get("/metrics/not-chased")
def not_chased(merchant_id: str | None = Query(None)) -> dict:
    """Money deliberately not pursued, split by why.

    Stopped and not-funded are reported apart because they mean different
    things: one is a decision that this money is not worth chasing, the other is
    a budget that ran out with the case still open.
    """
    result = _q(queries.money_not_chased, merchant_id)
    for r in result["stopped_by_rule"]:
        r["display"] = format_inr(r["amount_paise"])
    result["stopped_total_display"] = format_inr(result["stopped_total_paise"])
    result["considered_not_funded_display"] = format_inr(
        result["considered_not_funded_paise"]
    )
    return result


@app.get("/cases")
def cases(
    merchant_id: str | None = Query(None), limit: int = Query(50, le=500)
) -> list[dict]:
    rows = _q(queries.recent_cases, merchant_id, limit)
    for r in rows:
        r["display"] = format_inr(r["amount_paise"])
    return rows

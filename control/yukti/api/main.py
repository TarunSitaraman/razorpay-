"""Merchant console API.

Read-only for now. Every write path in Yukti goes through the dispatcher and
the policy engine, never through an HTTP handler, so there is deliberately no
"trigger recovery" endpoint here — an action that skipped the policy engine
would be exactly the failure mode the whole design exists to prevent.
"""

from __future__ import annotations

import json
import pathlib
from contextlib import asynccontextmanager
from typing import Any

import psycopg_pool
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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

# The console is a static page that reads these same JSON endpoints. No template
# engine and no build step: `make demo` has to work from a cold clone, and a
# toolchain that must install before anything renders is the thing that fails in
# front of an audience. It also means a number is computed in exactly one place —
# a server-side template would be free to drift from the API serving it.
STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def console() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/metrics/lift")
def lift() -> dict:
    """The five-arm comparison, read from the last `make eval`.

    Served from disk rather than computed on request: a five-arm run takes
    minutes, and an HTTP handler that could take minutes is not a handler.
    Reading the file the CLI wrote also guarantees the console shows the same
    number the CLI printed, rather than a second computation free to disagree.
    """
    from yukti.eval.report import EXPORT_PATH

    if not EXPORT_PATH.exists():
        raise HTTPException(
            404, "no evaluation on disk yet — run `make eval` to produce one")
    return json.loads(EXPORT_PATH.read_text())


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

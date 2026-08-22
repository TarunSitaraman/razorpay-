"""Write a generated dataset to Postgres and to a replayable Parquet event log."""

from __future__ import annotations

import pathlib
from dataclasses import asdict

import pyarrow as pa
import pyarrow.parquet as pq
from yukti.store.db import connect

from yukti_datagen.generate import Dataset

DATA_DIR = pathlib.Path("data/generated")

# Postgres COPY is roughly an order of magnitude faster than executemany for the
# hundreds of thousands of rows a 90-day run produces, and this runs on every
# `make seed`.
def _copy(cur, table: str, columns: list[str], rows: list[tuple]) -> None:
    if not rows:
        return
    cols = ", ".join(columns)
    with cur.copy(f"COPY {table} ({cols}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)


def to_postgres(ds: Dataset) -> dict[str, int]:
    counts: dict[str, int] = {}
    with connect() as conn, conn.cursor() as cur:
        # Truncate in FK-dependency order. RESTART IDENTITY resets the
        # BIGSERIAL sequences so a reseed produces identical audit IDs.
        cur.execute(
            """
            TRUNCATE payment_attempt, obligation, customer, merchant,
                     recovery_case, recovery_action, recovery_outcome,
                     agent_decision, agent_run, policy_evaluation,
                     promise_to_pay, degradation_signal, budget_ledger,
                     audit_event, outbox, processed_event, experiment
            RESTART IDENTITY CASCADE
            """
        )

        _copy(cur, "merchant", ["id", "name", "segment", "mdr_bps"],
              [(m.id, m.spec.name, m.spec.segment, m.spec.mdr_bps) for m in ds.merchants])
        counts["merchant"] = len(ds.merchants)

        customers = [
            (c.id, c.merchant_id, c.ltv_band, c.tenure_days,
             __import__("json").dumps(c.consent), c.archetype.value,
             c.history.prior_payments, c.history.prior_failures,
             c.history.prior_contacts, c.history.prior_contact_responses,
             c.history.prior_optouts, c.history.days_since_last_payment,
             c.preferred_channel.value,
             c.history.prior_unprompted_payments, c.history.prior_prompted_payments)
            for m in ds.merchants for c in m.customers
        ]
        _copy(cur, "customer",
              ["id", "merchant_id", "ltv_band", "tenure_days", "consent", "archetype",
               "prior_payments", "prior_failures", "prior_contacts",
               "prior_contact_responses", "prior_optouts", "days_since_last_payment",
               "preferred_channel", "prior_unprompted_payments",
               "prior_prompted_payments"],
              customers)
        counts["customer"] = len(customers)

        _copy(cur, "obligation",
              ["id", "merchant_id", "customer_id", "kind", "amount_paise",
               "currency", "due_at", "state", "version"],
              [(o["id"], o["merchant_id"], o["customer_id"], o["kind"],
                o["amount_paise"], o["currency"], o["due_at"], o["state"], o["version"])
               for o in ds.obligations])
        counts["obligation"] = len(ds.obligations)

        _copy(cur, "payment_attempt",
              ["id", "obligation_id", "rail", "issuer", "psp", "status",
               "decline_code", "decline_text", "amount_paise", "attempted_at"],
              [(a["id"], a["obligation_id"], a["rail"], a["issuer"], a["psp"],
                a["status"], a["decline_code"], a["decline_text"],
                a["amount_paise"], a["attempted_at"]) for a in ds.attempts])
        counts["payment_attempt"] = len(ds.attempts)

        # Daily budget ledgers, one row per merchant per kind per day the
        # dataset covers. The allocator spends against these.
        days = sorted({o["due_at"].date() for o in ds.obligations})
        ledger = [
            (m.id, kind, d, limit)
            for m in ds.merchants
            for kind, limit in (
                ("contact", m.spec.contact_budget_per_day),
                ("discount", m.spec.discount_budget_paise_per_day),
            )
            for d in days
        ]
        _copy(cur, "budget_ledger",
              ["merchant_id", "kind", "window_start", "limit_val"], ledger)
        counts["budget_ledger"] = len(ledger)

        # Ground-truth degradation episodes, so the detector can be scored.
        import json

        from yukti.domain.ids import degradation_id
        _copy(cur, "degradation_signal",
              ["id", "dimension", "dimension_value", "baseline_sr", "observed_sr",
               "z_score", "sample_size", "window_start", "window_end", "state",
               "injected_truth"],
              [(degradation_id(), e.dimension, e.value, 0.0, 0.0, 0.0, 0,
                e.start, e.end, "ground_truth",
                json.dumps({"true_cause": e.true_cause,
                            "dominant_code": e.dominant_code,
                            "sr_drop": round(e.sr_drop, 4)}))
               for e in ds.degradations])
        counts["degradation_signal"] = len(ds.degradations)

        conn.commit()
    return counts


def to_parquet(ds: Dataset, out_dir: pathlib.Path | None = None) -> pathlib.Path:
    """Write the event log for Kafka replay.

    Parquet rather than JSON because the eval harness scans this file repeatedly
    (once per arm) and columnar reads make that cheap.
    """
    out_dir = out_dir or DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "events.parquet"
    rows = [asdict(e) for e in ds.events]
    pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
    return path

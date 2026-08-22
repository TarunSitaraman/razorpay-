"""Build the randomised exploration history from the seeded database."""

from __future__ import annotations

import json

from rich.console import Console
from yukti.config import settings
from yukti.domain.enums import Rail
from yukti.domain.ids import action_id, case_id, decision_id, new_id
from yukti.store.db import connect

from yukti_datagen.calendar import generate_downtime_windows
from yukti_datagen.history import run_exploration
from yukti_datagen.world import ISSUERS

console = Console()


def build(days: int = 14) -> dict[str, int]:
    """Generate the exploration period and write it to the recovery tables."""
    cfg = settings()

    with connect() as conn:
        customers = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, archetype, preferred_channel, issuer_hint AS issuer "
                "FROM (SELECT c.id, c.archetype, c.preferred_channel, "
                "             (SELECT a.issuer FROM payment_attempt a "
                "                JOIN obligation o2 ON o2.id = a.obligation_id "
                "               WHERE o2.customer_id = c.id AND a.issuer IS NOT NULL "
                "               LIMIT 1) AS issuer_hint "
                "        FROM customer c) x"
            ).fetchall()
        }

        # One failed obligation = one case the exploration policy can act on.
        rows = conn.execute(
            """
            SELECT o.id            AS obligation_id,
                   o.merchant_id, o.customer_id, o.amount_paise,
                   a.decline_code, a.rail, a.attempted_at AS ts
              FROM obligation o
              JOIN LATERAL (
                  SELECT decline_code, rail, attempted_at
                    FROM payment_attempt
                   WHERE obligation_id = o.id AND status = 'failed'
                   ORDER BY attempted_at DESC LIMIT 1
              ) a ON true
             WHERE o.state = 'open'
            """
        ).fetchall()

        cases = [
            {
                "case_id": case_id(),
                "obligation_id": r["obligation_id"],
                "merchant_id": r["merchant_id"],
                "customer_id": r["customer_id"],
                "amount_paise": r["amount_paise"],
                "decline_code": r["decline_code"],
                "rail_is_mandate": Rail(r["rail"]).is_mandate,
                "ts": r["ts"],
            }
            for r in rows
        ]
        if not cases:
            raise SystemExit("no open obligations — run `make seed` first")

        start = min(c["ts"] for c in cases)
        downtime = generate_downtime_windows(cfg.seed, ISSUERS, start, days + 1)

        treatments = run_exploration(cases, customers, downtime, cfg.seed)

        # Persist as real recovery_case / decision / action / outcome rows, so
        # the feature frame reads the same tables in training and in serving.
        # Anything else is training/serving skew waiting to happen.
        conn.execute("TRUNCATE recovery_outcome, recovery_action, policy_evaluation, "
                     "agent_decision, agent_run, recovery_case RESTART IDENTITY CASCADE")

        for t in treatments:
            conn.execute(
                "INSERT INTO recovery_case (id, obligation_id, merchant_id, customer_id, "
                "state, arm, opened_at) VALUES (%s,%s,%s,%s,'awaiting_outcome',%s,%s)",
                (t.case_id, t.obligation_id, t.merchant_id, t.customer_id,
                 "holdout" if t.action_kind == "suppress" else "treatment",
                 t.scheduled_for),
            )
            did = decision_id()
            conn.execute(
                "INSERT INTO agent_decision (id, case_id, trace_id, action_kind, channel, "
                "scheduled_for, reason, policy_verdict) VALUES (%s,%s,%s,%s,%s,%s,%s,'allow')",
                (did, t.case_id, new_id("yk"), t.action_kind, t.channel,
                 t.scheduled_for, "randomised exploration"),
            )
            if t.action_kind != "suppress":
                conn.execute(
                    "INSERT INTO recovery_action (id, decision_id, case_id, kind, channel, "
                    "idempotency_key, scheduled_for, dispatched_at, status, cost_paise, "
                    "discount_paise, payload) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'dispatched',%s,%s,%s)",
                    (action_id(), did, t.case_id, t.action_kind, t.channel,
                     new_id("idem"), t.scheduled_for, t.scheduled_for,
                     t.cost_paise, t.discount_paise,
                     json.dumps({"discount_pct": t.discount_pct})),
                )
            conn.execute(
                "INSERT INTO recovery_outcome (id, case_id, outcome, recovered_paise, "
                "attribution_window_h) VALUES (%s,%s,%s,%s,72)",
                (new_id("out"), t.case_id,
                 "opted_out" if t.opted_out else
                 ("recovered" if t.recovered else "not_recovered"),
                 t.recovered_paise),
            )
        conn.commit()

    treated = sum(1 for t in treatments if t.action_kind != "suppress")
    control = len(treatments) - treated
    recovered = sum(1 for t in treatments if t.recovered)
    return {
        "cases": len(treatments), "treated": treated, "control": control,
        "recovered": recovered,
        "opted_out": sum(1 for t in treatments if t.opted_out),
    }

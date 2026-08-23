"""Build the randomised exploration history from the seeded database.

The exploration period is a PREFIX of the simulated timeline, not the whole of
it. Obligations whose last failure falls before the cutoff are consumed into
randomised exploration cases and scored by the oracle; everything after the
cutoff is left `open`, and that remainder is what the planner works on.

Without the split there is nothing to plan. This module previously took every
open failed obligation, which meant `plan_cycle` ran cleanly over an empty
input set and reported success — the same silent-success failure the dedup
namespace collision produced on day 2.

It is also how this has to work in production. Uplift is identified from a
deliberate exploration period; you train on that period and you plan on today.
Training on cases the current policy chose would relearn the policy rather than
the effect.
"""

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


# Share of the simulated failure timeline given to exploration. The remainder is
# the planning window. 0.75 leaves roughly a fortnight of open cases at the
# default 60-day generation span — enough for the allocator to have real
# choices to make, while keeping the training set large enough that the day-3
# gate still passes on it.
EXPLORATION_SHARE = 0.75


def build(days: int = 14, exploration_share: float = EXPLORATION_SHARE) -> dict[str, int]:
    """Generate the exploration period and write it to the recovery tables.

    Returns the counts plus the cutoff, so the caller can print where the
    training period ends and the planning period begins.
    """
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

        # The cutoff is derived from the data rather than hardcoded, so
        # regenerating with a different span moves it automatically instead of
        # silently emptying one side of the split.
        span = conn.execute(
            "SELECT min(attempted_at) AS lo, max(attempted_at) AS hi "
            "FROM payment_attempt WHERE status = 'failed'"
        ).fetchone()
        if span["lo"] is None:
            raise SystemExit("no failed attempts — run `make seed` first")
        share = min(max(exploration_share, 0.05), 0.95)
        cutoff = span["lo"] + (span["hi"] - span["lo"]) * share

        # One failed obligation = one case the exploration policy can act on,
        # but only inside the exploration period. Obligations that failed after
        # the cutoff stay open and untouched — they are the planner's input, and
        # the planner must never see a case an exploration policy already acted
        # on, or the two policies would be mixed in one measurement.
        rows = conn.execute(
            """
            SELECT o.id            AS obligation_id,
                   o.merchant_id, o.customer_id, o.amount_paise,
                   a.decline_code, a.rail, a.attempted_at AS ts,
                   (ptp.id IS NOT NULL) AS open_promise
              FROM obligation o
              JOIN LATERAL (
                  SELECT decline_code, rail, attempted_at
                    FROM payment_attempt
                   WHERE obligation_id = o.id AND status = 'failed'
                   ORDER BY attempted_at DESC LIMIT 1
              ) a ON true
              -- Was a promise open AT THE MOMENT this case is worked, which is
              -- not the same question as its final state. A promise that has
              -- since been kept was still open then, and the intervention was
              -- still made through it. Joining on state = 'open' would ask
              -- "is it open now", which is the wrong clock entirely.
              LEFT JOIN promise_to_pay ptp
                     ON ptp.obligation_id = o.id
                    AND ptp.created_at <= a.attempted_at
                    AND ptp.promised_for > a.attempted_at
             WHERE o.state = 'open'
               AND a.attempted_at < %s
            """,
            (cutoff,),
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
                # Passed through to the oracle. The oracle already models this
                # properly — an open promise floors organic recovery and chasing
                # through one is net-negative — but it was previously hardcoded
                # to False, which made the effect unobservable and the
                # OPEN_PROMISE_TO_PAY stopping rule untrainable.
                "open_promise": bool(r["open_promise"]),
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
        # What the planner will see. Counted here rather than inferred, because
        # "the split left something on both sides" is the property that matters
        # and it should fail loudly if it ever stops holding.
        left_open = conn.execute(
            """
            SELECT count(*) AS n
              FROM obligation o
             WHERE o.state = 'open'
               AND NOT EXISTS (SELECT 1 FROM recovery_case rc
                                WHERE rc.obligation_id = o.id)
            """
        ).fetchone()["n"]
        conn.commit()

    if left_open == 0:
        raise SystemExit(
            "the exploration cutoff consumed every open obligation — the planner "
            "would have no input. Lower exploration_share."
        )

    treated = sum(1 for t in treatments if t.action_kind != "suppress")
    control = len(treatments) - treated
    recovered = sum(1 for t in treatments if t.recovered)
    return {
        "cases": len(treatments), "treated": treated, "control": control,
        "recovered": recovered,
        "opted_out": sum(1 for t in treatments if t.opted_out),
        "promised": sum(1 for c in cases if c["open_promise"]),
        "cutoff": cutoff,
        "left_open": left_open,
    }

"""Feature frame for the intelligence layer.

One rule dominates this module: **`customer.archetype` is ground truth for
scoring and must never become a feature.** A model that reads it would score
beautifully and mean nothing, and the failure would be invisible — every metric
would look excellent.

So leakage is enforced structurally rather than by discipline. `FORBIDDEN`
names the banned columns, `build_frame` asserts they are absent from what it
returns, and a test builds a frame through this exact code path and fails if
any of them appear. The assertion lives in the function rather than in the
caller because the caller is the thing most likely to be rewritten in a hurry.

Everything the frame reads comes from the database, not from the generator's
in-memory objects, so training and serving read the same shape. Otherwise
training/serving skew stays invisible until it costs money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd
import psycopg

from yukti.domain.decline import lookup
from yukti.domain.enums import Rail

# Columns that must never reach a model. `archetype` is the latent label;
# `true_p_recover` and outcome columns are the answer itself.
FORBIDDEN: frozenset[str] = frozenset({
    "archetype", "true_p_recover", "uplift", "recovered",
    "recovered_paise", "outcome", "opted_out",
})

# Categorical features, declared so encoding is consistent between train and score.
CATEGORICAL: tuple[str, ...] = (
    "obligation_kind", "rail", "issuer", "psp", "decline_code",
    "transience", "ltv_band", "preferred_channel", "merchant_segment",
    "action_kind", "action_channel",
)


class FeatureLeakage(AssertionError):
    """A forbidden column reached the feature frame."""


@dataclass(frozen=True, slots=True)
class Frame:
    """A feature matrix plus the labels held alongside it, never inside it."""

    X: pd.DataFrame          # features only
    treated: pd.Series       # 1 = an action was dispatched, 0 = control
    outcome: pd.Series       # 1 = recovered
    archetype: pd.Series     # ground truth, for SCORING ONLY
    case_id: pd.Series
    amount_paise: pd.Series
    cost_paise: pd.Series

    def __len__(self) -> int:
        return len(self.X)


TRAINING_SQL = """
SELECT c.id                          AS case_id,
       c.arm,
       o.kind                        AS obligation_kind,
       o.amount_paise,
       m.segment                     AS merchant_segment,
       m.mdr_bps,
       a.rail, a.issuer, a.psp, a.decline_code,
       cu.ltv_band, cu.tenure_days, cu.preferred_channel,
       cu.prior_payments, cu.prior_failures, cu.prior_contacts,
       cu.prior_contact_responses, cu.prior_optouts, cu.days_since_last_payment,
       cu.prior_unprompted_payments, cu.prior_prompted_payments,
       cu.consent,
       cu.archetype,                                   -- label, split out below
       d.action_kind, d.channel                        AS action_channel,
       d.scheduled_for,
       coalesce(act.cost_paise, 0)                     AS cost_paise,
       coalesce(act.discount_paise, 0)                 AS discount_paise,
       coalesce((act.payload->>'discount_pct')::float, 0) AS discount_pct,
       (act.id IS NOT NULL)                            AS treated,
       (out.outcome = 'recovered')                     AS recovered
  FROM recovery_case c
  JOIN obligation  o  ON o.id  = c.obligation_id
  JOIN merchant    m  ON m.id  = c.merchant_id
  JOIN customer    cu ON cu.id = c.customer_id
  JOIN recovery_outcome out ON out.case_id = c.id
  LEFT JOIN agent_decision  d   ON d.case_id = c.id
  LEFT JOIN recovery_action act ON act.decision_id = d.id
  JOIN LATERAL (
      SELECT rail, issuer, psp, decline_code
        FROM payment_attempt
       WHERE obligation_id = o.id AND status = 'failed'
       ORDER BY attempted_at DESC LIMIT 1
  ) a ON true
 -- Deterministic order. Without it a LIMITed frame is an arbitrary sample, and
 -- arbitrary here is not merely unstable: it returned 2,000 treated rows and
 -- ZERO control rows, which would train an uplift model with no counterfactual
 -- arm at all. Case ids are ULIDs, so this is chronological and both arms
 -- interleave.
 ORDER BY c.id
"""


def _derive(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived features. Each one has a reason to exist."""
    spec = df["decline_code"].map(lookup)

    # The decline taxonomy is shared with the policy engine via domain.decline,
    # so a code cannot mean one thing to the model and another to the guardrails.
    df["transience"] = spec.map(lambda s: s.transience.value)
    df["retryable_silently"] = spec.map(lambda s: s.retryable_silently).astype(int)
    df["customer_actionable"] = spec.map(lambda s: s.customer_actionable).astype(int)
    df["max_attempts"] = spec.map(lambda s: s.max_attempts)

    df["rail_is_mandate"] = df["rail"].map(lambda r: Rail(r).is_mandate).astype(int)

    # Calendar position. The salary-day effect is the strongest timing signal in
    # Indian recurring payments, but the model is given only the raw position —
    # it has to discover the curve itself.
    sched = pd.to_datetime(df["scheduled_for"], utc=True, errors="coerce")
    df["day_of_month"] = sched.dt.day.fillna(15).astype(int)
    df["hour"] = sched.dt.hour.fillna(12).astype(int)
    df["weekday"] = sched.dt.weekday.fillna(2).astype(int)
    df["is_weekend"] = (df["weekday"] >= 5).astype(int)

    # Engagement history, as ratios rather than raw counts so they transfer
    # across customers with very different contact volumes.
    df["response_rate"] = (
        df["prior_contact_responses"] / df["prior_contacts"].clip(lower=1)
    )
    df["failure_ratio"] = (
        df["prior_failures"] / (df["prior_payments"] + df["prior_failures"]).clip(lower=1)
    )
    df["has_opted_out"] = (df["prior_optouts"] > 0).astype(int)

    # The discriminator: what share of past payments needed a nudge. A customer
    # who always pays unprompted is worth little to contact however reliable
    # they look; one who only pays after being asked is where the money is.
    total_paid = (df["prior_unprompted_payments"] + df["prior_prompted_payments"])
    df["prompted_share"] = df["prior_prompted_payments"] / total_paid.clip(lower=1)
    df["ever_paid_unprompted"] = (df["prior_unprompted_payments"] > 0).astype(int)
    df["nudge_conversion"] = (
        df["prior_prompted_payments"] / df["prior_contacts"].clip(lower=1)
    )

    consent = df["consent"].apply(lambda c: c if isinstance(c, dict) else {})
    for ch in ("whatsapp", "sms", "email", "voice"):
        df[f"consent_{ch}"] = consent.map(lambda c, ch=ch: int(bool(c.get(ch)))).astype(int)

    df["log_amount"] = (df["amount_paise"].clip(lower=1)).apply(float).pow(0.5)
    return df


def build_frame(conn: psycopg.Connection, limit: int | None = None) -> Frame:
    """Build a training/scoring frame from the database."""
    sql = TRAINING_SQL + (f" LIMIT {int(limit)}" if limit else "")
    rows = conn.execute(sql).fetchall()
    if not rows:
        raise SystemExit("no labelled cases — run `make seed && make history` first")

    df = pd.DataFrame(rows)
    df = _derive(df)

    labels = {
        "archetype": df["archetype"],
        "treated": df["treated"].astype(int),
        "recovered": df["recovered"].astype(int),
        "case_id": df["case_id"],
        "amount_paise": df["amount_paise"],
        "cost_paise": df["cost_paise"],
    }

    drop = FORBIDDEN | {"case_id", "arm", "scheduled_for", "consent", "treated"}
    X = df.drop(columns=[c for c in drop if c in df.columns])

    for col in CATEGORICAL:
        if col in X.columns:
            X[col] = X[col].fillna("unknown").astype("category")

    # Structural guard. Enforced here, not in the caller, because the caller is
    # what gets rewritten in a hurry the night before a demo.
    leaked = FORBIDDEN & set(X.columns)
    if leaked:
        raise FeatureLeakage(f"forbidden column(s) reached the feature frame: {sorted(leaked)}")

    return Frame(
        X=X,
        treated=labels["treated"],
        outcome=labels["recovered"],
        archetype=labels["archetype"],
        case_id=labels["case_id"],
        amount_paise=labels["amount_paise"],
        cost_paise=labels["cost_paise"],
    )


def feature_names(frame: Frame) -> list[str]:
    return list(frame.X.columns)


# ---------------------------------------------------------------------------
# Serving: open cases and counterfactual candidate scoring
#
# `build_frame` above is training-only by construction — it INNER JOINs
# recovery_outcome, which an open case does not have, and reads the action
# columns from a decision that has not been made yet. Serving needs the mirror
# image: cases with no outcome, scored under actions we are *considering*.
#
# Both paths run through the same `_derive`, and the scoring path asserts the
# same FORBIDDEN guard. If they diverged, training/serving skew would show up as
# a model that looks excellent offline and picks badly in production, which is
# the failure mode that is hardest to notice and most expensive to have.
# ---------------------------------------------------------------------------

OPEN_CASE_SQL = """
SELECT c.id                          AS case_id,
       c.arm, c.merchant_id, c.customer_id, c.opened_at,
       o.id                          AS obligation_id,
       o.kind                        AS obligation_kind,
       o.amount_paise,
       o.state                       AS obligation_state,
       m.segment                     AS merchant_segment,
       m.mdr_bps,
       a.rail, a.issuer, a.psp, a.decline_code,
       a.first_failed_at, a.attempts_made,
       cu.ltv_band, cu.tenure_days, cu.preferred_channel,
       cu.prior_payments, cu.prior_failures, cu.prior_contacts,
       cu.prior_contact_responses, cu.prior_optouts, cu.days_since_last_payment,
       cu.prior_unprompted_payments, cu.prior_prompted_payments,
       cu.consent,
       (cu.opted_out_at IS NOT NULL)  AS customer_opted_out,
       (ptp.id IS NOT NULL)           AS open_promise_to_pay,
       coalesce(ct.n, 0)              AS contacts_this_window
  FROM recovery_case c
  JOIN obligation  o  ON o.id  = c.obligation_id
  JOIN merchant    m  ON m.id  = c.merchant_id
  JOIN customer    cu ON cu.id = c.customer_id
  JOIN LATERAL (
      SELECT rail, issuer, psp, decline_code,
             min(attempted_at) OVER ()   AS first_failed_at,
             count(*)          OVER ()   AS attempts_made
        FROM payment_attempt
       WHERE obligation_id = o.id AND status = 'failed'
       ORDER BY attempted_at DESC LIMIT 1
  ) a ON true
  -- Open AS OF the planning moment, not as of now. A planner replaying an
  -- earlier date must see the promises that were live then.
  LEFT JOIN promise_to_pay ptp
         ON ptp.obligation_id = o.id
        AND ptp.created_at <= %(as_of)s
        AND ptp.promised_for > %(as_of)s
        AND ptp.state <> 'broken'
  -- Contacts across EVERY case for this customer, on every surface. This is the
  -- cross-agent arbitration signal: a per-case count cannot see it.
  LEFT JOIN LATERAL (
      SELECT count(*) AS n
        FROM recovery_action ra
        JOIN recovery_case rc ON rc.id = ra.case_id
       WHERE rc.customer_id = c.customer_id
         AND ra.dispatched_at >= %(as_of)s - interval '7 days'
         AND ra.dispatched_at <  %(as_of)s
         AND ra.kind IN ('message', 'voice_call', 'discount_offer')
  ) ct ON true
 WHERE c.state IN ('open', 'planning')
   AND c.merchant_id = %(merchant_id)s
   AND c.opened_at <= %(as_of)s
 -- Deterministic order, which a LIMIT makes load-bearing rather than cosmetic.
 -- Without it Postgres returns an arbitrary subset and two runs at the same
 -- planning moment consider DIFFERENT cases — so `plan_cycle` stops being the
 -- pure function of database state the whole crash-resume and replay story
 -- depends on. It showed up as a re-run "dispatching 601 more actions", which
 -- looks like an idempotency failure and is not one.
 -- Case ids are ULIDs, so this is also chronological.
 ORDER BY c.id
"""


def load_open_cases(
    conn: psycopg.Connection, merchant_id: str, as_of: datetime,
    limit: int | None = None,
) -> pd.DataFrame:
    """Every case this merchant could work at `as_of`, with its decision context.

    One query rather than one per concern. The stopping rules, the candidate
    generator and the feature frame all read the same rows, so they cannot
    disagree about what was true at planning time — which they would if each
    fetched its own snapshot a few milliseconds apart.
    """
    sql = OPEN_CASE_SQL + (f" LIMIT {int(limit)}" if limit else "")
    rows = conn.execute(sql, {"merchant_id": merchant_id, "as_of": as_of}).fetchall()
    return pd.DataFrame(rows)


def build_candidate_frame(base: pd.DataFrame, candidates: pd.DataFrame) -> Frame:
    """Score one row per (case, proposed action).

    `candidates` carries `case_id` plus the action columns —`action_kind`,
    `action_channel`, `scheduled_for`, `cost_paise`, `discount_paise`,
    `discount_pct`. Those are the columns that describe the intervention, and
    substituting a proposed action for an observed one is exactly the
    counterfactual `ActionConditionalUplift` is built to answer.

    The returned `Frame` carries no outcome — nothing has happened yet — so the
    label fields are zero-filled placeholders. They exist only because `Frame`
    is shared with training; reading them at serving time would be a bug and
    they are deliberately constant so such a bug is obvious rather than subtle.
    """
    if base.empty or candidates.empty:
        return Frame(
            X=pd.DataFrame(), treated=pd.Series(dtype=int), outcome=pd.Series(dtype=int),
            archetype=pd.Series(dtype=object), case_id=pd.Series(dtype=object),
            amount_paise=pd.Series(dtype=int), cost_paise=pd.Series(dtype=int),
        )

    context_cols = [c for c in base.columns if c not in candidates.columns or c == "case_id"]
    df = candidates.merge(base[context_cols], on="case_id", how="inner")
    if df.empty:
        raise ValueError("no candidate matched a loaded case")

    df = _derive(df)

    keep_case = df["case_id"]
    keep_amount = df["amount_paise"]
    keep_cost = df["cost_paise"]

    drop = FORBIDDEN | {
        "case_id", "arm", "scheduled_for", "consent", "treated",
        # Serving-only context. Read by the stopping rules and the policy engine
        # from `base`, never by the model — several of them are decisions the
        # system already made, and feeding a decision back in as a feature is
        # how a model learns to predict its own policy.
        "merchant_id", "customer_id", "obligation_id", "opened_at",
        "obligation_state", "first_failed_at", "attempts_made",
        "customer_opted_out", "open_promise_to_pay", "contacts_this_window",
    }
    X = df.drop(columns=[c for c in drop if c in df.columns])

    for col in CATEGORICAL:
        if col in X.columns:
            X[col] = X[col].fillna("unknown").astype("category")

    leaked = FORBIDDEN & set(X.columns)
    if leaked:
        raise FeatureLeakage(
            f"forbidden column(s) reached the candidate frame: {sorted(leaked)}"
        )

    zeros = pd.Series(0, index=range(len(df)))
    return Frame(
        X=X.reset_index(drop=True),
        treated=zeros, outcome=zeros,
        archetype=pd.Series(["__unavailable__"] * len(df)),
        case_id=keep_case.reset_index(drop=True),
        amount_paise=keep_amount.reset_index(drop=True),
        cost_paise=keep_cost.reset_index(drop=True),
    )

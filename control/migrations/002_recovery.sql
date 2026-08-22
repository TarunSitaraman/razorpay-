-- Recovery: cases, promises, decisions, actions, outcomes, audit, budgets.

BEGIN;

CREATE TABLE experiment (
    id              TEXT PRIMARY KEY,
    merchant_id     TEXT        NOT NULL REFERENCES merchant(id),
    name            TEXT        NOT NULL,
    hypothesis      TEXT,
    -- Assignment is a pure function of (salt, customer_id), so an arm can be
    -- recomputed at any time without storing a per-customer row, and a replay
    -- reproduces exactly the same split.
    salt            TEXT        NOT NULL,
    holdout_pct     NUMERIC(5,2) NOT NULL DEFAULT 10.00
        CHECK (holdout_pct >= 0 AND holdout_pct <= 100),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ
);

-- The unit of recovery work. One obligation may have at most one live case.
CREATE TABLE recovery_case (
    id              TEXT PRIMARY KEY,
    obligation_id   TEXT        NOT NULL REFERENCES obligation(id),
    merchant_id     TEXT        NOT NULL REFERENCES merchant(id),
    customer_id     TEXT        NOT NULL REFERENCES customer(id),
    state           TEXT        NOT NULL DEFAULT 'open',
    arm             TEXT        NOT NULL DEFAULT 'treatment',
    experiment_id   TEXT        REFERENCES experiment(id),
    stop_reason     TEXT,       -- set iff state = 'stopped'; named rule ID
    version         INT         NOT NULL DEFAULT 0,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ,
    CONSTRAINT stop_reason_iff_stopped
        CHECK ((state = 'stopped') = (stop_reason IS NOT NULL))
);
-- At most one open case per obligation. Without this, a duplicated
-- payment.failed webhook opens a second case and the customer is worked twice.
CREATE UNIQUE INDEX case_one_live_per_obligation
    ON recovery_case (obligation_id)
    WHERE state NOT IN ('stopped', 'recovered', 'lost');
CREATE INDEX case_merchant_state_idx ON recovery_case (merchant_id, state);
CREATE INDEX case_customer_idx       ON recovery_case (customer_id);

-- "B2B receivables chaser" + "promise-to-pay tracker" from the track brief.
-- An open promise is a hard stopping rule: chasing through one measurably
-- lowers recovery, so this table is read by the stopping engine, not just shown.
CREATE TABLE promise_to_pay (
    id                    TEXT PRIMARY KEY,
    obligation_id         TEXT        NOT NULL REFERENCES obligation(id),
    promised_amount_paise BIGINT      NOT NULL CHECK (promised_amount_paise > 0),
    promised_for          DATE        NOT NULL,
    source                TEXT        NOT NULL,  -- customer_reply|voice_call|merchant_entry|inferred
    state                 TEXT        NOT NULL DEFAULT 'open',
    confidence            NUMERIC(4,3),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at           TIMESTAMPTZ
);
CREATE UNIQUE INDEX ptp_one_open_per_obligation
    ON promise_to_pay (obligation_id) WHERE state = 'open';

-- "Payment degradation -> root cause -> recovery action", the first example
-- direction on the track page. Aggregate-level, not per-transaction.
CREATE TABLE degradation_signal (
    id              TEXT PRIMARY KEY,
    merchant_id     TEXT        REFERENCES merchant(id),   -- null = platform-wide
    dimension       TEXT        NOT NULL,   -- issuer | psp | method | bin | gateway
    dimension_value TEXT        NOT NULL,
    baseline_sr     NUMERIC(6,4) NOT NULL,
    observed_sr     NUMERIC(6,4) NOT NULL,
    z_score         NUMERIC(8,3) NOT NULL,
    sample_size     INT         NOT NULL,
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    state           TEXT        NOT NULL DEFAULT 'firing',
    rca_run_id      TEXT,
    -- Ground truth from the generator, for scoring the detector. Null in
    -- anything that is not a synthetic run.
    injected_truth  JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX degradation_open_idx ON degradation_signal (dimension, dimension_value)
    WHERE state = 'firing';

CREATE TABLE agent_run (
    id              TEXT PRIMARY KEY,
    case_id         TEXT        REFERENCES recovery_case(id),
    merchant_id     TEXT        NOT NULL REFERENCES merchant(id),
    kind            TEXT        NOT NULL,   -- rca | planner | composer
    trace_id        TEXT        NOT NULL,
    model           TEXT,
    status          TEXT        NOT NULL DEFAULT 'running',
    input_digest    TEXT,       -- hash of retrieved evidence; makes runs replay-comparable
    tokens_in       INT         NOT NULL DEFAULT 0,
    tokens_out      INT         NOT NULL DEFAULT 0,
    cost_paise      BIGINT      NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX agent_run_trace_idx ON agent_run (trace_id);

-- Immutable. A decision is a proposal plus the verdicts that were applied to it.
CREATE TABLE agent_decision (
    id                    TEXT PRIMARY KEY,
    run_id                TEXT        REFERENCES agent_run(id),
    case_id               TEXT        NOT NULL REFERENCES recovery_case(id),
    trace_id              TEXT        NOT NULL,
    action_kind           TEXT        NOT NULL,
    channel               TEXT        NOT NULL DEFAULT 'none',
    scheduled_for         TIMESTAMPTZ,
    reason                TEXT        NOT NULL,
    confidence            NUMERIC(4,3),
    expected_incr_margin_paise BIGINT NOT NULL DEFAULT 0,
    -- What the allocator/policy rejected and why. This is what the console shows
    -- in the "alternatives rejected" panel and what makes a decision auditable.
    alternatives_rejected JSONB       NOT NULL DEFAULT '[]'::jsonb,
    policy_verdict        TEXT        NOT NULL,
    risk                  TEXT        NOT NULL DEFAULT 'low',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX decision_case_idx  ON agent_decision (case_id, created_at);
CREATE INDEX decision_trace_idx ON agent_decision (trace_id);

CREATE TABLE policy_evaluation (
    id              BIGSERIAL PRIMARY KEY,
    decision_id     TEXT        NOT NULL REFERENCES agent_decision(id),
    pack            TEXT        NOT NULL,   -- regulatory | merchant | stopping
    rule_id         TEXT        NOT NULL,
    verdict         TEXT        NOT NULL,
    reason          TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX policy_eval_decision_idx ON policy_evaluation (decision_id);
CREATE INDEX policy_eval_rule_idx     ON policy_evaluation (rule_id, verdict);

CREATE TABLE recovery_action (
    id              TEXT PRIMARY KEY,
    decision_id     TEXT        NOT NULL REFERENCES agent_decision(id),
    case_id         TEXT        NOT NULL REFERENCES recovery_case(id),
    kind            TEXT        NOT NULL,
    channel         TEXT        NOT NULL DEFAULT 'none',
    -- The idempotency guarantee for money. Fingerprint is derived from
    -- (merchant, obligation, action kind, semantic payload) so that a replayed
    -- webhook producing an identical intent collides here instead of dispatching.
    idempotency_key TEXT        NOT NULL UNIQUE,
    scheduled_for   TIMESTAMPTZ,
    dispatched_at   TIMESTAMPTZ,
    status          TEXT        NOT NULL DEFAULT 'pending',
    cost_paise      BIGINT      NOT NULL DEFAULT 0,   -- channel cost we paid
    discount_paise  BIGINT      NOT NULL DEFAULT 0,   -- incentive we gave away
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX action_case_idx      ON recovery_action (case_id, created_at);
CREATE INDEX action_scheduled_idx ON recovery_action (scheduled_for)
    WHERE status = 'pending';

CREATE TABLE recovery_outcome (
    id                  TEXT PRIMARY KEY,
    case_id             TEXT        NOT NULL REFERENCES recovery_case(id),
    action_id           TEXT        REFERENCES recovery_action(id),  -- null = organic
    outcome             TEXT        NOT NULL,  -- recovered|not_recovered|opted_out|churned
    recovered_paise     BIGINT      NOT NULL DEFAULT 0,
    attribution_window_h INT        NOT NULL DEFAULT 72,
    attributed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX outcome_case_idx ON recovery_outcome (case_id);

-- Budgets are ledgers, not counters, so that consumption is auditable and a
-- replay can reconstruct exactly what was affordable at each point in time.
CREATE TABLE budget_ledger (
    id              BIGSERIAL PRIMARY KEY,
    merchant_id     TEXT        NOT NULL REFERENCES merchant(id),
    kind            TEXT        NOT NULL,   -- contact | discount | channel_spend
    window_start    DATE        NOT NULL,
    limit_val       BIGINT      NOT NULL,
    consumed_val    BIGINT      NOT NULL DEFAULT 0,
    UNIQUE (merchant_id, kind, window_start)
);

-- Append-only, hash-chained. prev_hash links each row to the one before it for
-- the same merchant, so a deleted or edited row breaks the chain verifiably.
CREATE TABLE audit_event (
    id              BIGSERIAL PRIMARY KEY,
    trace_id        TEXT        NOT NULL,
    merchant_id     TEXT        NOT NULL REFERENCES merchant(id),
    actor           TEXT        NOT NULL,   -- agent | human | system
    action          TEXT        NOT NULL,
    subject_id      TEXT,
    detail          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    prev_hash       TEXT,
    hash            TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_merchant_idx ON audit_event (merchant_id, id);
CREATE INDEX audit_trace_idx    ON audit_event (trace_id);

-- Transactional outbox. A decision and its intent to publish are committed in
-- one transaction; a relay drains this table to Kafka. This is what removes the
-- dual-write between Postgres and Kafka.
CREATE TABLE outbox (
    id              BIGSERIAL PRIMARY KEY,
    topic           TEXT        NOT NULL,
    partition_key   TEXT        NOT NULL,   -- merchant_id, to preserve per-merchant order
    payload         JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at    TIMESTAMPTZ
);
CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;

-- Backstop for webhook dedup. Redis is the fast path; this is the durable one
-- that survives a Redis flush.
CREATE TABLE processed_event (
    event_id        TEXT PRIMARY KEY,
    source          TEXT        NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;

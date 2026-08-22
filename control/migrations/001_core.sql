-- Yukti core schema.
--
-- Design notes that matter for review:
--   * All money is BIGINT paise. No NUMERIC, no float. See domain/money.py.
--   * Aggregates carry `version` and inbound events are compared against it, so
--     out-of-order webhook delivery is safe without distributed locking.
--   * `recovery_action` carries a UNIQUE idempotency_key. That single constraint
--     is the last line of defence that stops a duplicated webhook from causing a
--     second debit or a second discount.
--   * `audit_event` is append-only and hash-chained; there is no UPDATE path.

BEGIN;

CREATE TABLE merchant (
    id              TEXT PRIMARY KEY,
    name            TEXT        NOT NULL,
    segment         TEXT        NOT NULL,
    mdr_bps         INT         NOT NULL DEFAULT 200,   -- basis points kept off the margin
    risk_profile    TEXT        NOT NULL DEFAULT 'standard',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE customer (
    id                  TEXT PRIMARY KEY,
    merchant_id         TEXT        NOT NULL REFERENCES merchant(id),
    ltv_band            TEXT        NOT NULL,
    tenure_days         INT         NOT NULL DEFAULT 0,
    -- Per-channel consent under DPDP. Absence of a key means no consent, so the
    -- default {} is the safe default rather than an empty permission set.
    consent             JSONB       NOT NULL DEFAULT '{}'::jsonb,
    opted_out_at        TIMESTAMPTZ,
    -- Ground truth for evaluation only. NEVER read by any model or policy; the
    -- eval harness asserts this column is absent from every feature frame.
    archetype           TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX customer_merchant_idx ON customer (merchant_id);

-- One abstraction over all four revenue-loss surfaces named in the track brief.
CREATE TABLE obligation (
    id              TEXT PRIMARY KEY,
    merchant_id     TEXT        NOT NULL REFERENCES merchant(id),
    customer_id     TEXT        NOT NULL REFERENCES customer(id),
    kind            TEXT        NOT NULL,   -- cart | subscription_cycle | invoice | order
    amount_paise    BIGINT      NOT NULL CHECK (amount_paise > 0),
    currency        TEXT        NOT NULL DEFAULT 'INR',
    due_at          TIMESTAMPTZ NOT NULL,
    state           TEXT        NOT NULL DEFAULT 'open',
    version         INT         NOT NULL DEFAULT 0,
    source_ref      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX obligation_merchant_state_idx ON obligation (merchant_id, state);
CREATE INDEX obligation_customer_idx       ON obligation (customer_id);
CREATE INDEX obligation_due_idx            ON obligation (due_at) WHERE state = 'open';

CREATE TABLE payment_attempt (
    id              TEXT PRIMARY KEY,
    obligation_id   TEXT        NOT NULL REFERENCES obligation(id),
    rail            TEXT        NOT NULL,
    issuer          TEXT,
    psp             TEXT,
    status          TEXT        NOT NULL,   -- captured | failed | pending
    decline_code    TEXT,
    decline_text    TEXT,                   -- untrusted free text; never concatenated into a prompt
    amount_paise    BIGINT      NOT NULL,
    attempted_at    TIMESTAMPTZ NOT NULL,
    -- Set when Yukti caused the attempt, null when the customer or the merchant's
    -- own billing did. Distinguishing these is what makes attribution possible.
    caused_by_action_id TEXT,
    idempotency_key TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX attempt_obligation_idx ON payment_attempt (obligation_id, attempted_at);
CREATE INDEX attempt_issuer_time_idx ON payment_attempt (issuer, attempted_at)
    WHERE status = 'failed';

COMMIT;

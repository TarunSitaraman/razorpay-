-- Merchant policy configuration.
--
-- `merchantpack.compile_from_settings` has always taken a settings dict, but
-- nothing stored one, so every planning run would have fallen back to the
-- library defaults and the "merchants configure their own limits" claim would
-- have been true only in the type signature.
--
-- Shaped for the natural-language compiler that comes later: `dsl_source` holds
-- what the merchant wrote, `compiled` holds what it compiled to, and the two are
-- versioned together with who approved them. Until the compiler exists,
-- `dsl_source` is null and `compiled` is written directly — which is why they
-- are separate columns rather than one.

BEGIN;

CREATE TABLE policy_pack (
    id              TEXT PRIMARY KEY,
    -- NULL merchant_id = a pack that applies to everyone. That is how the
    -- regulatory pack would be represented if it were ever stored; it is not,
    -- deliberately, because RegPack lives in code where it cannot be edited by
    -- a database write.
    merchant_id     TEXT        REFERENCES merchant(id),
    kind            TEXT        NOT NULL CHECK (kind IN ('regulatory', 'merchant')),
    version         INT         NOT NULL DEFAULT 1,
    dsl_source      TEXT,
    compiled        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,
    active          BOOLEAN     NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- An unapproved pack must never be active. Merchant policy governs what
    -- gets spent on their behalf, so activation requires a named approver
    -- rather than merely a successful insert.
    CONSTRAINT active_requires_approval
        CHECK (NOT active OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);

-- One active pack per merchant per kind. Two active packs would mean the engine
-- picks one arbitrarily, and which limits applied would depend on row order.
CREATE UNIQUE INDEX policy_pack_one_active
    ON policy_pack (merchant_id, kind) WHERE active;

CREATE INDEX policy_pack_merchant_idx ON policy_pack (merchant_id, kind, version DESC);

COMMIT;

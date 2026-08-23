-- Agent evidence store.
--
-- Project Viveka's published shape: specialists write what they FOUND into a
-- store, and the supervisor reasons over retrieved rows rather than over an
-- ever-growing context window. Two things follow from that, and both are the
-- reason this is a table rather than a Python list:
--
--   * A run that crashes resumes by re-reading its evidence instead of
--     re-calling the model. Evidence is the expensive part; re-deriving it is
--     what makes a resumed run cost real money.
--   * A narrative can be checked against the rows it was given. An RCA that
--     cites a fact with no evidence row behind it is a hallucination, and this
--     table is what makes that statement testable rather than aspirational.

BEGIN;

CREATE TABLE agent_evidence (
    id            BIGSERIAL PRIMARY KEY,
    run_id        TEXT        NOT NULL REFERENCES agent_run(id) ON DELETE CASCADE,
    -- Which specialist gathered it, and what kind of fact it is.
    source        TEXT        NOT NULL,   -- degradation_scan | decline_mix | cohort_stats | case_detail
    -- What it is about: an issuer, a PSP, a case id. Lets the supervisor
    -- retrieve by subject rather than reading everything.
    subject       TEXT,
    -- The fact itself. Computed by SQL, never by a model — the LLM reads these
    -- rows, it does not produce them.
    fact          JSONB       NOT NULL,
    gathered_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX agent_evidence_run_idx     ON agent_evidence (run_id, source);
CREATE INDEX agent_evidence_subject_idx ON agent_evidence (subject);

-- What a specialist concluded, kept apart from what it was given.
--
-- Separate tables on purpose. Evidence is fact and conclusions are
-- interpretation, and a merchant auditing a decision needs to see which is
-- which. Storing a narrative in the same table as the numbers it describes
-- would blur exactly the line this system exists to hold.
CREATE TABLE agent_conclusion (
    id            TEXT        PRIMARY KEY,
    run_id        TEXT        NOT NULL REFERENCES agent_run(id) ON DELETE CASCADE,
    specialist    TEXT        NOT NULL,   -- rca | planner | composer
    subject       TEXT,
    -- Structured output, already clamped to a closed enum where one applies.
    output        JSONB       NOT NULL,
    -- Evidence ids the specialist was shown. An assertion that cites nothing
    -- here is unsupported by construction.
    cited_ids     BIGINT[]    NOT NULL DEFAULT '{}',
    model         TEXT,
    input_tokens  INT,
    output_tokens INT,
    -- 'llm' when a model produced it, 'fallback' when the call failed and the
    -- conservative default was used. Reported as a metric: a fleet quietly
    -- running on fallbacks looks identical to one that is working.
    provenance    TEXT        NOT NULL DEFAULT 'llm',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX agent_conclusion_run_idx ON agent_conclusion (run_id, specialist);

COMMIT;

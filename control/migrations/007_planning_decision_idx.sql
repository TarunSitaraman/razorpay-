-- The decision feed's index.
--
-- The console's activity feed reads planning decisions newest-first, with
-- suppressions demoted below funded actions:
--
--     WHERE d.run_id IS NOT NULL
--     ORDER BY (d.action_kind = 'suppress'), d.created_at DESC, d.id DESC
--
-- With 82,498 decisions and no supporting index that is a full scan plus a
-- sort, measured at 163 ms — on a panel the console refreshes on a timer. The
-- partial predicate matters as much as the ordering: only 16,961 of those rows
-- are planning decisions, the rest are the randomised exploration history, so
-- indexing the whole table would be four times the pages for the same answer.
--
-- The `action_kind` expression is deliberately NOT in the index. Postgres can
-- read this index in order and sort the two suppression groups cheaply once the
-- limit is applied; adding the boolean would help only the feed's exact
-- ordering and nothing else that reads planning decisions by recency.
CREATE INDEX IF NOT EXISTS decision_planning_recent_idx
    ON agent_decision (created_at DESC, id DESC)
    WHERE run_id IS NOT NULL;

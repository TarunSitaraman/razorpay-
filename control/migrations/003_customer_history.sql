-- Observable customer history.
--
-- These are legitimate model features, unlike customer.archetype which is
-- ground truth for scoring only. They are persisted because the feature frame
-- must be buildable from the database alone — a model that depended on the
-- generator's in-memory objects could not be served in production, and the
-- training/serving skew would be invisible until it mattered.

BEGIN;

ALTER TABLE customer
    ADD COLUMN prior_payments           INT NOT NULL DEFAULT 0,
    ADD COLUMN prior_failures           INT NOT NULL DEFAULT 0,
    ADD COLUMN prior_contacts           INT NOT NULL DEFAULT 0,
    ADD COLUMN prior_contact_responses  INT NOT NULL DEFAULT 0,
    ADD COLUMN prior_optouts            INT NOT NULL DEFAULT 0,
    ADD COLUMN days_since_last_payment  INT NOT NULL DEFAULT 0,
    -- Preferred channel is observable in reality (inferred from engagement),
    -- so it is a feature. Stored explicitly rather than derived at query time.
    ADD COLUMN preferred_channel        TEXT NOT NULL DEFAULT 'whatsapp';

-- The uplift model scores open cases by (customer, recency), so the frame build
-- reads this shape directly.
CREATE INDEX customer_history_idx ON customer (merchant_id, prior_optouts, tenure_days);

COMMIT;

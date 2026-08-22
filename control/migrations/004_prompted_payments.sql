-- The discriminator between a sure thing and a persuadable.
--
-- "Paid on their own" and "paid after we asked" mean very different things
-- about what an intervention is worth, and collapsing them into a single
-- prior_payments count is what left those two archetypes observationally
-- indistinguishable (identical profiles, 0.707 vs 0.089 recovery rates).
--
-- A production system genuinely has this: a payment landing inside the
-- attribution window of a dunning contact is prompted, otherwise it is not. So
-- this is a legitimate feature, not a smuggled label.

BEGIN;

ALTER TABLE customer
    ADD COLUMN prior_unprompted_payments INT NOT NULL DEFAULT 0,
    ADD COLUMN prior_prompted_payments   INT NOT NULL DEFAULT 0;

COMMIT;

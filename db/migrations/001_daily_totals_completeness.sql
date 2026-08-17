-- Coverage weight divides incident articles by an outlet's total output that
-- day. That total is only meaningful for days we actually crawled end to end.
--
-- The first crawl backfills whatever happens to still be in each feed, which
-- produced rows like "cna, 2026-08-11, 6 articles" -- CNA publishes on the order
-- of 200 a day. Dividing by 6 would report a coverage weight of several hundred
-- percent and look like a spectacular finding rather than a bug.
--
-- MIN_DAILY_DENOMINATOR alone does not catch this: the same first crawl produced
-- 84 articles for 2026-08-15, which clears any sane threshold while still being
-- less than half the real total.

ALTER TABLE outlet_daily_totals
    ADD COLUMN IF NOT EXISTS complete BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN outlet_daily_totals.complete IS
    'True only when crawl_runs shows gap-free successful coverage of the whole '
    'Asia/Taipei day. Coverage weight must be suppressed when false.';

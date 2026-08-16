# T-004 — Daily rollup and denominator completeness

*Written retroactively after review: `rollup_daily.py` was landed without a
ticket authorizing it, and it materially changes metric correctness.*

## Goal

Maintain `outlet_daily_totals` — the denominator for coverage weight (Q2) — and
mark each outlet-day as usable or not.

## Files in scope

- `src/parallax/jobs/rollup_daily.py`
- `db/migrations/001_daily_totals_completeness.sql`
- `outlet_daily_totals` in `db/schema.sql`
- `tests/test_rollup.py`

## Do not touch

`src/parallax/crawl/**` — T-003. `src/parallax/metrics/**` — T-010.

## Why `complete` exists

A raw count is not a denominator. The first crawl backfilled whatever was still
in each feed and recorded **6 articles** for a day 中央社 actually published
~200; dividing by that reports a coverage weight of several hundred percent.

A minimum-denominator threshold does not catch this. The same crawl produced
**84** for the previous day — clearing any sane floor while still under half the
true total. Only evidence of gap-free crawl coverage across a whole Taipei day
can justify using it, so completeness is computed from `crawl_runs`, not guessed
from the count.

## Acceptance criteria

- Recomputes a trailing window, not just yesterday: feeds surface late items and
  `effective_at` can place an article in an earlier day than we crawled it.
- Days are `Asia/Taipei`.
- `complete = true` only when the day is over **and** successful runs bracket it
  **and** no gap exceeds `--max-gap-minutes` (default 90; the crawl runs every
  20, so this tolerates a couple of missed cycles but not an outage).
- **Gaps must be computed partitioned by outlet only, never by day.** Partitioning
  by day resets `lag()` at midnight, so an outage from 23:40 to 01:20 exists in
  neither day's partition while both still pass their bracket checks — and both
  get marked complete despite a 100-minute hole.
- Runs with `ok = false` leave a gap rather than filling one, so a degraded or
  empty crawl cannot manufacture completeness.
- Idempotent: re-running yields the same flags.

## Verify

```bash
make db.migrate && make rollup
uv run pytest tests/test_rollup.py -q
```

`tests/test_rollup.py` seeds synthetic runs in a transaction that is always
rolled back, so it never touches real crawl data. The midnight case is verified
to fail when the partition bug is reintroduced.

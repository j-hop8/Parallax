# T-003 — Tier-1 listing crawl (CRITICAL PATH)

## Why this is first

`article_index` is the coverage-weight denominator. RSS feeds expose only hours
of history, so every hour this is not running is an hour of denominator that can
never be reconstructed. It ships before the metric that consumes it, and before
the UI that displays it.

## Goal

All 8 outlets polled every 20 minutes, metadata upserted idempotently, every run
recorded per outlet whether it succeeded or failed.

## Files in scope

- `src/parallax/crawl/{listing,http}.py`, `src/parallax/crawl/adapters/**`
- `src/parallax/jobs/crawl_listing.py`, `src/parallax/{db,urls,config,models}.py`
- `tests/test_urls.py`, `tests/fixtures/**`

## Do not touch

- `src/parallax/nlp/{stance,dedup,framing}.py` — T-007/T-008/T-009
- `src/parallax/ui/**`

## Acceptance criteria

- One outlet raising does not stop the others; the failure lands in `crawl_runs`
  with its error text and the run continues.
- Re-running immediately inserts **zero** new rows (upsert is idempotent across
  polls: scheme, `www.`, trailing slash, fragment and tracking params all
  collapse to one key).
- `published_at` missing or later than `seen_at` falls back to `seen_at` via
  `effective_at`.
- Each adapter has a test against a **saved fixture** in `tests/fixtures/`, not
  the live network — network tests hide exactly the breakage we care about.
- Cron installed for the 20-minute crawl and the 00:20 rollup.

## Verify

```bash
make crawl.one OUTLET=cna     # then re-run: items_new must be 0
make crawl
make health                   # all 8 outlets present, ok, non-zero
make test
```

Then leave it running an hour and confirm every outlet has non-zero `new_24h`.

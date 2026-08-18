# T-006b — Hardening from self-review

Follow-up to [T-006](done/T-006-crawl-resilience.md), plus one fix to
[T-005](done/T-005-body-fetch-and-timestamps.md). Numbered `006b` rather than
`007` deliberately: the plan reserves T-007 for stance, and this is not that.

## Why

Codex quota is exhausted until 2026-09-10, so `main` got a self-review pass
instead of an independent one. It found three defects and one deferred risk.
Recording them as a ticket after the fact, because the work is real regardless of
how it was found.

## Files in scope

- `src/parallax/models.py`, `src/parallax/db.py`, `src/parallax/config.py`
- `src/parallax/crawl/adapters/base.py`
- `src/parallax/jobs/enrich.py`
- `config/outlets.yaml`
- `tests/test_crawl_health.py`

## Do not touch

`src/parallax/nlp/**` — T-007/T-008. `src/parallax/metrics/**` — T-010.

## The defects

1. **`crawl_runs.started_at` was really `finished_at`.** It took the column's
   `DEFAULT now()`, so it was stamped at INSERT — the same instant as
   `finished_at`. Every run recorded a 0.0s duration, but the real damage is that
   `started_at` was the *end* of the crawl. The rollup measures coverage gaps
   between `started_at` values to decide whether a day may serve as a
   coverage-weight denominator, so every timestamp sat later than the truth by
   that outlet's runtime.

2. **A comment stated a measurement nobody had taken.** `_MIN_BODY_CHARS` claimed
   the shortest legitimate body was 562 chars; measured across the corpus it is
   447 (p05 487). The threshold of 80 was already safe, but an unchecked number
   in a comment is how a later reader justifies raising it too far.

3. **`enrich` counted one article twice.** A fetch that succeeded and then failed
   extraction incremented both `fetched` and `failed`, so the totals exceeded
   `matched` and overstated how much work had succeeded.

4. **One outlet could consume the whole crawl cycle.** Deferred in the first pass
   on the grounds that real runs take ~23s. That reasoning only holds while hosts
   fail *fast*: one that accepts a connection and then hangs costs
   `timeout x retries` per request, and 中央社 is polled across 11 feeds —
   enough on its own to delay or skip every outlet queued behind it.

## Acceptance criteria

- `crawl_runs` records a real, non-zero duration; `started_at` is when the
  outlet's work began.
- Each outlet has a wall-clock budget; feeds not reached are reported as errors,
  never silently dropped, so the run stays honestly degraded.
- The budget does not engage on healthy outlets (headroom: 中央社 23.3s against
  180s; every other outlet under 0.5s).
- `fetched + cached + failed` never exceeds `matched`.
- Comments state only measurements that were actually taken.

## Knowingly not done

- **Historical `crawl_runs` rows keep the old `started_at`.** Real durations are
  0.1–23s against a 90-minute completeness tolerance, so the distortion cannot
  change a verdict. Rewriting ~900 historical rows would risk more than it fixes.
- **The denominator-bias finding belongs to T-010.** Enrich backfills accurate
  timestamps only for articles matching a *searched* keyword, so the
  coverage-weight numerator grows more accurate than its denominator, and the mix
  shifts as more keywords are searched. Systematic, not random. Measured blast
  radius today: no article has changed Taipei day, and 5 dateless articles sit
  within 30 minutes of midnight. It is a metric-design question, not a crawler
  patch.

## Verify

```bash
uv run pytest tests/test_crawl_health.py -q
make crawl && make health
```

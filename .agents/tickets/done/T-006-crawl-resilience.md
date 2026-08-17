# T-006 — Crawl resilience on a sleeping laptop

## Why

In the first 24 hours of live crawling, **229 of 458 runs failed** and three hours
produced no runs at all. Two distinct causes, both confirmed from `pmset -g log`:

| Window (Taipei) | Symptom | Cause |
|---|---|---|
| 02:00–06:00 | runs fired, every outlet `ConnectionError` | `DarkWake ... due to RTC/Maintenance` — cron fired during a brief dark wake before Wi-Fi re-associated |
| 16:00–18:00 | no `crawl_runs` rows at all | `Entering Sleep state due to 'Clamshell Sleep'` — lid closed, `powernap 0`, so nothing scheduled ran |

All eight outlets failed simultaneously against eight different hosts, including
feedburner. Eight independent sites do not fail in lockstep; the host lost
network.

This breaks invariant 1 — the crawl must never stop — and the loss is permanent.
It is also why `make rollup` reports **0 outlet-days usable as a denominator**:
the completeness guard is correctly refusing every half-covered day, so coverage
weight (Q2) cannot be computed at all.

## Honest scope limit

**No code change fixes this properly.** A laptop that sleeps cannot host a
crawler whose source data expires in hours. Articles published during a long
sleep scroll out of the feeds and are gone regardless of how well the job
retries. The real fix is an always-on host (VPS, Pi, or a cloud scheduler), and
that decision belongs to the human.

This ticket reduces the loss on the current host and makes the remaining loss
visible rather than silent.

## Files in scope

- `src/parallax/crawl/listing.py`, `src/parallax/crawl/http.py`
- `ops/` (new): launchd plists
- `Makefile`, `CLAUDE.md`
- `tests/test_crawl_health.py`

## Do not touch

`src/parallax/nlp/**`, `src/parallax/jobs/rollup_daily.py`.

## Work

1. **Fix the write-path bug** (Codex round 2, `listing.py:115`). A database error
   inside the `PartialFetchError` handler escapes the per-outlet `try`, aborting
   the whole crawl, skipping every remaining outlet, and recording no
   `crawl_runs` row. Every DB write in the loop must be individually guarded so
   one outlet can never take down the run.

2. **Retry transient network failures.** `ConnectionError` and `Timeout` get a
   couple of backed-off retries; HTTP status errors do not, since a 404 will not
   heal. This directly addresses the dark-wake window, where the network is
   seconds away from being ready.

3. **Replace cron with launchd.** This is the substantive win: a launchd
   `StartInterval` job that was missed while asleep runs **immediately on wake**,
   whereas cron simply skips it and waits for the next slot. Combined with (2),
   a short sleep stops costing a full cycle.

4. **Report gaps, don't just count failures.** `make health` should surface the
   largest coverage gap per outlet, so a night like this is one obvious number
   rather than something to infer from 229 scattered rows.

## Acceptance criteria

- A DB error while persisting one outlet leaves the other seven crawling and
  still records that outlet's failure in `crawl_runs`.
- Transient connection errors retry; HTTP errors do not.
- launchd plists installed and loaded; cron entries removed so the two cannot
  double-run.
- `make health` shows the largest gap per outlet over the last 24h.
- `CLAUDE.md` states plainly that a sleeping laptop loses tier-1 data
  permanently, so the limitation is not rediscovered later.

## Verify

```bash
uv run pytest tests/test_crawl_health.py -q
make health
launchctl list | grep parallax
```

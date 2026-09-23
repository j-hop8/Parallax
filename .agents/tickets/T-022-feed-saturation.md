# T-022 — Feed saturation: detect when the poll interval is losing articles

**Owner:** codex (delegable — pure arithmetic over `crawl_runs`, no network, and
it mirrors the `metrics/coverage.py` + job + db-read shape exactly).
**Blocked by:** nothing.

## Why

Invariant 1 says tier-1 loss is permanent and unrecoverable. Invariant 2 gives
one detector: `items_seen = 0` means a broken selector. Nothing detects the
other, quieter failure — **the feed turning over faster than we poll it**.

RSS exposes a fixed window, typically 20–30 items. If an outlet publishes
enough between two polls to replace that whole window, the articles in the
middle are gone: never seen, never counted, and not recoverable. The crawl
looks perfectly healthy while it happens — every run succeeds, `items_seen` is
a normal number, and `make health` reports no gaps. Worse, those missing
articles are the **Q2 denominator**, so a short capture silently *inflates*
every coverage weight on the page.

`crawl_runs` already holds everything needed to measure this: `items_seen`,
`items_new` and `started_at` per outlet per run. No new crawling, no extra load
on the outlets — this is arithmetic over data we already have.

Proposal §9 row 4 wants a true capture rate from a sampled manual count; that
needs a human and is a later ticket. This is the part a machine can do alone,
and it covers the failure mode that would otherwise go unnoticed for weeks.

## Design — decided, not open

**Saturation** of one successful run = `items_new / items_seen`. At 1.0 the
entire feed window was new since the last poll, which means the window may also
have dropped items we never saw. At 0.1 there is comfortable headroom.

**Turnover time** is the number that makes it actionable. For a run with
saturation `s` and `gap` minutes since that outlet's previous successful run,
the feed consumed `s` of its window in `gap` minutes, so a full window turns
over in about `gap / s` minutes. Report **the fastest observed turnover per
outlet**: if that is not comfortably above the poll interval, the interval is
too long for that outlet and articles are being lost now.

**Runs that must be excluded, and why** — each one would otherwise read as
saturated for a legitimate reason and bury the real signal:

- an outlet's **first ever run**: it backfills whatever the feed holds, so
  saturation is 1.0 by construction and there is no previous run to measure a
  gap against;
- runs whose preceding gap is **more than 3× the median gap for that outlet**:
  these follow an outage, where a full window legitimately turned over. Outages
  are `make health`'s job, not this one, and double-reporting them here would
  make the saturation number useless exactly when it matters;
- runs with `items_seen = 0`: invariant 2's separate signal, already reported;
- `ok = false` runs.

**`items_new > items_seen` must never happen.** If a row shows it, do not
clamp silently — count those rows and print them as a data-integrity warning,
because it means the upsert accounting is wrong and that is worth knowing.

**Threshold:** a run is *at risk* at saturation ≥ 0.8. Reported, never gated —
same rule as every other metric in this project.

**Shape** — follow `metrics/coverage.py`:

- `src/parallax/metrics/saturation.py` (new, **pure**): takes run rows, returns
  a frozen `OutletSaturation` per outlet — `runs`, `at_risk`, `median`, `worst`
  (saturation and its `started_at`), `fastest_turnover_minutes | None`,
  `excluded` counts by reason, `anomalies`. Suppression/exclusion logic lives
  here, not in SQL.
- `src/parallax/db.py`: one read, `crawl_runs_window(conn, since)`, returning
  `outlet, started_at, items_seen, items_new, ok` ordered by outlet then time.
  Day bucketing is not needed; this is about intervals, not days.
- `src/parallax/jobs/saturation.py`: argv `--days N` (default 7), `--verbose`;
  prints one line per outlet plus a legend, and a non-zero exit **only** on the
  `items_new > items_seen` integrity anomaly, never on saturation itself.
- `Makefile`: `make saturation` (and `ARGS="--days 30"`), listed in `help` and
  `.PHONY`.

Tests are the point of the pure module: build run rows as plain dicts and assert
each exclusion rule, the turnover arithmetic, an at-risk outlet and a healthy
one. Add a DB-backed test in the style of `tests/test_crawl_health.py` if one
fits; skip-on-no-Postgres like the others.

## Do not touch

`src/parallax/crawl/**` and the adapters — this ticket adds no crawling and
changes no selector. Also `db/schema.sql` (`crawl_runs` already has every
column needed), `config/outlets.yaml`, and the `health` target, which keeps
reporting gaps as it does today.

## Acceptance criteria

- `make saturation` prints, per outlet: runs considered, at-risk count, median
  and worst saturation with the worst run's Taipei timestamp, and the fastest
  observed full-window turnover in minutes.
- An outlet's first run, post-outage runs (gap > 3× that outlet's median),
  `items_seen = 0` runs and `ok = false` runs are excluded, and the output says
  how many were excluded for each reason rather than hiding them.
- Turnover arithmetic is covered by a test with hand-computed numbers.
- `items_new > items_seen` is reported as an integrity anomaly and is the only
  condition that makes the job exit non-zero.
- An outlet with too few usable runs reports that instead of a number — the
  same refusal-to-guess rule as `MIN_DAILY_DENOMINATOR`.
- Timestamps are `Asia/Taipei` (invariant 3).

## Verify

```bash
uv run pytest -q tests/test_saturation.py && uv run ruff check . && uv run pytest -q
```

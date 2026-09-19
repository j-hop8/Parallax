# T-010 — Incident metrics: coverage weight (Q2), originality, propagation, behind one report seam

## Why

Every question now has its data: T-004 keeps the denominator, T-007 the stance
rows, T-008/T-009 the clusters and their deltas. Nothing yet turns them into
the page in `Design.pdf` -- one keyword in, per-outlet stance + 當日涵蓋權重,
the header counts (篇文章 / 家媒體 / 個抄襲群 / 天), the 群組 blocks. Coverage
weight in particular has never been computed; `metrics/` has been an empty
package reserved by every ticket since T-004. The Streamlit UI (T-011) should
render a report object, not run SQL, so this ticket builds the report and a
text readout of it; T-011 is then a rendering exercise.

Invariants 3, 4, 5 and 7 all bind here, and 7 is not enough on its own (T-004:
a raw count is not a denominator -- `complete` is the gate, the floor is
secondary).

## What the corpus said first (read-only, 2026-09-19, 31,395 tier-1 rows)

- **Coverage weights are small.** On the two complete days (08-20, 08-21),
  沈伯洋 runs 0.2–1.1% per outlet-day (ltn 9/817, setn 5/507, udn 1/623);
  颱風 peaks at tvbs 1.5%. The design's 2–10% is a mock. One decimal place,
  and the bar scale must come from the data, not a fixed 10%.
- **Only 16 outlet-days are complete, all on 08-20/21.** Everything since
  09-13 is incomplete: the crawl on this laptop has multi-hour gaps every
  night (invariant 1a). So for any keyword today, the exact weight rests on
  two days out of a 35-day span, and the report has to say so per outlet
  ("2 of 16 active days"). A pooled figure over the usable days is the only
  defensible single number; a mean of daily ratios would let a 1/327 day
  weigh as much as a 9/817 day.
- **Four outlets have no feed timestamps at all** -- chinatimes 99.0%,
  ettoday 99.5%, setn 98.3%, udn 98.4% of tier-1 rows have
  `published_at IS NULL`, so their `effective_at` is our poll time. T-006b's
  denominator-bias finding is about exactly these: enrich backfills real
  publish times for matched articles only. Measured on the complete days: of
  133 enriched articles, **2 changed Taipei day** after backfill (one cna, one
  ftv -- both outlets that already ship timestamps; the four dateless outlets
  moved 0). Median crawl lag for the dateless outlets is 12–16 min, p90
  21–39 min, so on a complete day (gap ≤ 90 min) only articles inside that
  lag of midnight can be misfiled. Decision: bucket numerator and denominator
  by the same `effective_at` (invariant 4), accept the bounded bias, and have
  the readout print the day-shift count so it stays measured.
- **Per-outlet baseline (proposal §5: "normalization once ~10 days exist")
  cannot switch on yet.** Medians over 2 complete days (ltn 883, cna 326)
  are not baselines. Implemented behind `MIN_BASELINE_DAYS = 7`; dormant
  until the crawl moves to an always-on host.
- **`too_short` is not stored.** dedup decides it in memory from bigram
  count; the two known cases (ltn video pages) will count as "alone" in a
  stored-column originality rate. Noted, not fixed here.
- Stored clusters: 14, 35 members; 沈伯洋 touches 4 (one indeterminate:
  udn/udn), 關稅 4, 颱風 5. Cluster membership is global (dedup runs over
  the whole enriched corpus), so a keyword's report shows every member of any
  cluster that has at least one member matching the keyword.

## Design

**Matched set.** `search.match_ids(conn, keyword)` -- the same title FTS as
`find_articles`, segmented (invariant 6), no LIMIT -- returns
`(id, outlet, effective_at)` for every tier-1 row. Everything downstream joins
on those ids. That is deliberately the shape an Elasticsearch swap returns.
Window `--since/--until` are Taipei dates, inclusive; default = every matched
day. **Active days** = Taipei days on which any outlet matched.

**Coverage (`metrics/coverage.py`, pure).** Inputs: per outlet-day
`(n, total, complete)` and the outlet's baseline. Per outlet-day
`usable = complete AND total >= MIN_DAILY_DENOMINATOR`.
- `basis = "exact"`: weight = Σn / Σtotal over the outlet's usable *active*
  days -- a usable day on which the outlet ran nothing counts 0/total, which
  is the signal. `days_used` / `days_active` reported.
- `basis = "estimated"` when no usable day exists but the outlet has
  ≥ `MIN_BASELINE_DAYS` complete days with total ≥ floor: weight =
  Σn / (median complete-day total × active days). A lower bound, flagged.
- `basis = "none"`: `weight = None`; `n` is still shown. Never a number.
- Per-day series `(day, n, total, complete, weight|None)` kept for T-011.

**Stance (Q1).** `stance_by_outlet` restricted to the matched ids, at
`(STANCE_MODEL, PROMPT_VERSION)`; beside neg/neu/pos each outlet carries
`matched`, `enriched` (has body) and `classified`, so the UI can write
"45 of 90 classified" instead of implying a full distribution.

**Originality (`metrics/originality.py`, pure).** Over matched articles with
a body, from stored columns: alone / first / follow / unresolved (member of a
cluster with `origin_confident = false`), `original = (alone + first) / n`,
`strict = alone / n` -- the two definitions T-008's readout already prints.

**Propagation (`metrics/propagation.py`, pure).** Cluster rows →
`ClusterView`: members in `effective_at` order with `rank` **only when
`origin_confident`**, `origin` = rank-1 member only then, else `origin=None`
and `reason` rebuilt with `nlp.dedup.build_cluster`. Deltas and
`delta_summary` pass through; on an indeterminate cluster `delta_removed` is
forced empty on the way out (T-009 writes it so; a stale row must not leak a
direction into the UI).

**Report (`metrics/report.py`).** `build_report(conn, keyword, since, until)
-> IncidentReport`: header (`articles`, `outlets`, `clusters`, `span_days`,
`active_days`), one `OutletRow` per outlet in `config/outlets.yaml` order
(stance + coverage + originality), `clusters`, `denominator_as_of`
(`max(computed_at)` of the rollup) and `day_shift` (matched articles whose
backfilled publish day differs from their poll day). SQL lives in `db.py`
as everywhere else; the `metrics/` modules take rows and return dataclasses.

**Readout (`jobs/report.py`, `make report KEYWORD=…`).** Prints the design
page as text: header line, the Q1+Q2 table with `basis` and
`days_used/days_active` beside every weight, `—` where basis is none, the
originality columns, then each 群組 block (核心稿源 excerpt, `#n outlet
HH:MM summary` or `順序不明 (reason)` with no `#`). Exit 0 with a plain
"no articles match" for an unknown keyword.

## Files in scope

- `src/parallax/metrics/{coverage,originality,propagation,report}.py`,
  `src/parallax/metrics/__init__.py`
- `src/parallax/search.py` (`match_ids`)
- `src/parallax/db.py` (read helpers: outlet-day counts for ids, daily totals
  in a window, baselines, stance for ids, cluster roles for ids, clusters
  touching ids, rollup `computed_at`)
- `src/parallax/jobs/report.py`, `Makefile` (`report`), `settings.py`
  (`MIN_BASELINE_DAYS`)
- `tests/test_metrics.py` (pure), `tests/test_report_jobs.py` (renderer,
  fake rows), `tests/test_report_db.py` (DB-gated, seeds in a rolled-back
  transaction like `test_rollup.py`)
- this ticket (archived to `done/` at ship)

## Do not touch

`jobs/rollup_daily.py` and `outlet_daily_totals` semantics (T-004),
`nlp/**` thresholds and prompts, `crawl/**`, `db/schema.sql` (no schema
change: every column exists), `src/parallax/ui/**` (T-011), `article_stance`
writes.

## Acceptance criteria

- A weight is emitted **only** for an outlet-day with `complete = true` and
  `total >= MIN_DAILY_DENOMINATOR`; an incomplete day with 84 articles yields
  none (T-004's case). The pure test covers exact / estimated / none, and
  that a usable day with `n = 0` lowers the pooled weight.
- Pooled, not averaged: `(1/327 + 9/817)` days report `10/1144`, not the mean.
- Baseline never engages below `MIN_BASELINE_DAYS` complete days.
- `--since/--until` bucket by Taipei dates; a UTC-evening article lands on
  the next Taipei day.
- An indeterminate cluster has `origin is None`, no member ranks, non-empty
  `reason`, and every `delta_removed` empty even when the stored row carries
  one; the readout prints 順序不明 and never `#1` or `－`.
- Stance counts for a keyword equal `stance_by_outlet` restricted to the
  matched ids; `classified <= enriched <= matched` per outlet.
- Originality per outlet matches `make dedup`'s table for the same articles
  (up to the `too_short` cases, which this ticket counts as alone).
- `make report KEYWORD=沈伯洋` and `KEYWORD=沈伯洋 ARGS="--since 2026-08-20
  --until 2026-08-21"` both run; the second reports `basis=exact` for the
  outlets with matches on those days and the numbers in the prototype above.
- `make report KEYWORD=不存在的字` exits 0 with a one-line message.
- `uv run pytest -q` passes offline; DB tests skip without a database.

## Live (2026-09-19, 31,395 tier-1 rows, 478 bodies)

`make report KEYWORD=沈伯洋`: 333 篇文章, 8 家媒體, 4 個抄襲群, 35 天 (16
active). Every outlet reports `basis=exact` on **2/16 days**; weights 0.1%
(ettoday: 1 match against 748 articles across the two complete days) to 1.0%
(setn). cna 0.2% not the prototype's 0.3% because 08-20 -- a complete day on
which cna ran nothing about 沈伯洋 -- now counts 0/325. 17 matched articles
sit on a different Taipei day than they were polled: the overnight sleep gaps
made visible, and all on incomplete days. Stance: `classified == enriched`
for every outlet (44/44 ltn, 45/45 ftv). Originality matches `make dedup`'s
table. Cluster 34939 (udn/udn) prints `順序不明: gap 0s inside the 300s noise
floor` with no ranks.

`--since 2026-08-20 --until 2026-08-21`: 43 篇文章, 1 個抄襲群, weights
identical to the prototype's usable-day figures (ltn 0.8%, setn 1.0%, udn
0.2%).

`KEYWORD=關稅`: 6 clusters touched (the prototype counted 4 by first-member
title; two more have a member whose headline carries the word). ettoday
**0.0% exact** -- it matched nothing on either complete day, which is the
measurement, not a gap. No stance rows at the configured model: the Q1
columns are `—`.

## Knowingly not done

- No per-day series in the readout; `OutletCoverage.days` carries it for the
  UI's timeline.
- `too_short` still counts as alone (see above).
- The baseline tier has never engaged on real data and cannot until seven
  complete days exist for an outlet; it is covered by the pure tests only.

## Verify

```bash
uv run pytest -q && uv run ruff check .
make rollup
make report KEYWORD=沈伯洋
make report KEYWORD=沈伯洋 ARGS="--since 2026-08-20 --until 2026-08-21"
make report KEYWORD=關稅
make report KEYWORD=不存在的字
```

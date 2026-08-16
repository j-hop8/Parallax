# Parallax 視差

Measures how Taiwanese outlets diverge covering the same incident: stance (Q1),
share of daily output (Q2), who copied whom and what each one changed (Q3), and
social-platform lean (Q4). See [parallax-proposal.md](parallax-proposal.md) for
the full rationale and `Design.pdf` for the target UI.

## Invariants — break these and the data is wrong, often silently

1. **The tier-1 listing crawl must never stop.** `article_index` is the
   denominator for coverage weight. RSS exposes only hours of history, so any
   window where the crawl is down is permanently unrecoverable — no backfill
   exists. Check `make health` before assuming things are fine.

2. **One outlet failing must not abort the crawl.** Every adapter runs isolated
   and records to `crawl_runs` either way. `items_seen = 0` on a healthy run is
   the signature of a broken selector, not a quiet day.

3. **Days are `Asia/Taipei`, never UTC.** Bucketing by UTC misfiles everything
   published after 08:00 local and corrupts the denominator.

4. **Order by `effective_at`, not `published_at`.** Outlet-reported timestamps
   are missing, backdated, and occasionally in the future. `effective_at` is a
   generated column encoding the fallback rule; use it for all ordering and
   bucketing.

5. **Never claim who copied whom when `dup_clusters.origin_confident` is false.**
   Sub-five-minute gaps are inside the noise floor of feed timestamps. Say the
   order is indeterminate instead.

6. **Segment before you search.** Postgres ships no Chinese parser. Text is
   indexed as `to_tsvector('simple', <jieba-segmented>)`, so query strings must
   go through `parallax.nlp.segment.segment_text` too or they simply will not
   match.

7. **Suppress coverage weight below `MIN_DAILY_DENOMINATOR`.** A percentage off
   a denominator of six is noise dressed as a measurement.

## Layout

- `src/parallax/crawl/` — tier 1 (listing, metadata only) and tier 2 (body fetch)
- `src/parallax/nlp/` — segmentation, stance, SimHash dedup, framing diff
- `src/parallax/metrics/` — coverage weight, originality, propagation
- `src/parallax/jobs/` — argv-driven entry points; Airflow wraps these unchanged
- `config/outlets.yaml` — the 8 outlets. Fixed in week 1; changing it resets the
  `outlet_daily_totals` baseline.
- `db/schema.sql` — idempotent, re-runnable

## Common commands

```bash
make setup && make db.up && make db.create && make db.migrate
make audit                    # probe feeds + robots.txt
make crawl.one OUTLET=cna     # single adapter
make crawl                    # all outlets
make health                   # per-outlet crawl health, last 24h
make test
```

## Costs

Tier 1 is metadata only and never calls a model. Model spend happens in tier 2,
which only runs on articles matching a keyword someone actually searched — and
LLM calls are reserved for summarising cluster diffs, cached per cluster member.
Keep it that way: labelling every article daily does not scale.

## Crawling conduct

Prefer RSS. Respect `robots.txt` (recorded per outlet in `outlets.yaml`).
Identify the bot honestly in the User-Agent with a contact address, rate-limit
conservatively, and cache raw HTML so a parser fix never re-hits the outlet.

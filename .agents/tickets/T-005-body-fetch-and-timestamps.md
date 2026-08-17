# T-005 — Body fetch, raw HTML cache, and publish-timestamp recovery

**Status: complete.** Timestamp extraction, the raw HTML cache, the enrich job,
and the write-back to `article_index.published_at` are all in place.

## Why

Four outlets (聯合報, 中國時報, 三立, ETtoday) publish no timestamp in their
listings, so `effective_at` fell back to `seen_at` — precision no better than the
20-minute poll interval, an order of magnitude coarser than the ~5-minute noise
floor at which origin ordering is already untrustworthy. That directly caps Q3,
the project's differentiator.

## Files in scope

`src/parallax/crawl/{extract,body}.py`, `src/parallax/jobs/enrich.py`,
`tests/test_extract.py`, `tests/fixtures/articles/**`

## Do not touch

`src/parallax/nlp/{stance,dedup,framing}.py` — T-007/T-008/T-009.

## Done

- `extract.py` recovers publish times from article pages: JSON-LD, then
  OpenGraph, then `<time>`, in descending order of trustworthiness. All 8
  outlets now resolve a timestamp.
- Two live-site bugs handled, both of which fail silently:
  - **三立 stamps Taipei wall-clock time as `+00:00`.** Taken at face value its
    articles publish ~8 hours *after* we crawled them, inverting any cluster they
    appear in. Corrected per-outlet, never globally — the other seven declare
    correct offsets.
  - **民視 wraps the whole page in one ASP.NET `<form runat="server">`.**
    Stripping chrome before locating the article deleted the article too. The
    container is now found first, cleaned second.
- The plausibility guard is one-sided: an article older than the crawl is
  ordinary and its real date is exactly what we want; one published *after* we
  saw it is impossible. The earlier two-sided check discarded a legitimately
  6-day-old 三立 article still sitting on the listing.

## Also done

- [x] Gzipped content-addressed cache at `raw/{outlet}/{day}/{sha256}.html.gz`,
      keyed on canonical URL and foldered by the article's own day so the path is
      derivable from the database row alone. Written temp-then-rename, because
      interrupted runs are routine here and a half-written `.gz` must not pass for
      a good one. Verified: a re-run made **zero** outlet requests.
- [x] `jobs/enrich.py` — bodies fetched only for keyword matches, each article
      isolated and committed on its own so the run is resumable. Measured cost
      model: **4,456 indexed vs 7 enriched**.
- [x] Recovered timestamps written back to `article_index.published_at`, guarded
      by `published_at IS NULL` so a feed-supplied timestamp is never replaced by
      a scraped one. `effective_at` is generated, so it recomputes automatically
      and the article immediately sorts by real publish time.

## Measured result

2,017 of 4,456 articles had no timestamp (setn 557, udn 533, chinatimes 489,
ettoday 438, ftv 15). On the first live run, 聯合報 articles seen at 11:40
resolved to a real 11:27, and 中國時報 seen at 13:00 to 12:44 — minute precision
in place of a ±20-minute poll approximation. That is what Q3 ordering needs.

Two bugs found while building this, both caught by their own tests:

- `read_cached` caught `OSError`/`EOFError` but **not `zlib.error`**, which is not
  an OSError. A valid gzip header with a corrupt deflate stream -- exactly what a
  killed write leaves -- would have taken down the enrich batch instead of being
  re-fetched.
- The search query did not return `url_canonical`, `seen_at` or `published_at`,
  all of which enrichment needs to key the cache and decide whether to backfill.

## Still outstanding elsewhere

Clusters containing an outlet whose timestamp is still unrecovered must be
reported `origin_confident = false`. That belongs to T-008/T-009, which own
clustering; enrichment now supplies the timestamps those tickets need.

## Verify

```bash
uv run pytest tests/test_extract.py -q
make crawl && make health
```

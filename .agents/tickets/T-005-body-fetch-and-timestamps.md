# T-005 — Body fetch, raw HTML cache, and publish-timestamp recovery

**Status: partially delivered.** Timestamp extraction is built and tested; the
cache and the enrich job are not.

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

## Remaining

- [ ] Gzipped content-addressed raw HTML cache at `raw/{outlet}/{date}/{sha}.html.gz`,
      so a parser fix re-runs locally and never re-hits the outlet.
- [ ] `jobs/enrich.py` — fetch bodies **only** for articles matching a searched
      keyword. The gap between `article_index` and `articles` is the cost model.
- [ ] Write recovered timestamps back into `article_index.published_at`.
      Until this lands, the extractor's output reaches nothing.
- [ ] Until then, treat the 4 dateless outlets as `origin_confident = false` in
      any cluster.

## Verify

```bash
uv run pytest tests/test_extract.py -q
make crawl && make health
```

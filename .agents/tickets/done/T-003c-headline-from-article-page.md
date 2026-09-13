# T-003c — Recover the real headline from the cached article page

Follow-up to [T-003b](done/T-003b-udn-ftv-headline.md), found while preparing
T-007's live run.

## Why

T-003b fixed the listing adapter but left history alone: the upsert is
first-sight-wins, and ~6,800 udn/ftv rows from August keep lede text as
`title`. That was acceptable for rows nobody looks at. It is not acceptable
for rows T-007 classifies: of the 200 enriched 沈伯洋 articles, **68 (53 ftv,
15 udn) carry the wrong headline**, the headline is the classifier's
most-weighted input, and the stance cache key does not include it — a verdict
made on the wrong headline would persist.

Every enriched article has its page cached in `raw/` (T-005). The page's
`<h1>` is the headline as the outlet displayed it, so the fix needs no
network, no quota, and no guessing.

## Files in scope

- `src/parallax/crawl/extract.py` — `extract_headline(html)`
- `scripts/retitle_from_article_page.py` — backfill for enriched udn/ftv rows
- `tests/test_extract.py` (existing article fixtures; no refresh)
- this ticket (archived to `done/` at ship)

## Do not touch

`src/parallax/db.py` upsert (first-sight-wins stays), listing adapters,
`nlp/**`, `metrics/**`. `enrich` is not changed: with T-003b in place new rows
arrive with the right title, so re-deriving it on every fetch is not needed.

## Acceptance criteria

- `extract_headline` returns the displayed headline for all 8 article
  fixtures: non-empty, ≤ 60 chars, a substring of the page `<title>`, and for
  ettoday not the logo `<h1>` that precedes the real one.
- Falls back to `og:title` then `<title>` with the site suffix removed when
  there is no `<h1>`; returns `None` rather than guessing when there is nothing.
- The backfill touches only `title`/`title_seg`, only udn and ftv, only where
  the stored title is longer than the recovered headline (the bug's signature).
  `seen_at`/`effective_at` untouched. Second run reports 0 changes.
- After the run, enriched 沈伯洋 rows with `length(title) > 60`: 0.

## Knowingly not done

- Non-enriched history (no cached page) stays as T-003b documented.
- Other outlets: their listing titles already equal the page `<h1>`; the
  script does not touch them.

## Verify

```bash
uv run pytest -q tests/test_extract.py
uv run python scripts/retitle_from_article_page.py     # then again: 0 retitled
```
```sql
select outlet, count(*) filter (where length(ai.title)>60) from articles a
join article_index ai on ai.id=a.id where ai.outlet in ('udn','ftv') group by 1;
```

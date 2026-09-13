# T-003b — udn and ftv record the lede as the title

Defect in [T-003](done/T-003-listing-crawl.md)'s pattern adapter. Numbered
`003b` following the `006b` precedent: it is a fix to the listing crawl, not
the next step in the plan (T-007 stance is reserved).

## Why

Measured 2026-09-13 over `article_index`: 73–99% of udn titles and 90–99% of
ftv titles are longer than 60 characters, every week since the crawl began on
2026-08-10. The other six outlets sit at 0%. The stored "title" is the lede or
summary paragraph, not the headline — about 7,000 rows.

Root cause is the adapter's tie-break, *"longest non-empty text wins"* among
anchors sharing a URL. Two markup shapes defeat it:

- **ftv** wraps `div.time` + `h2.title` + `div.desc` in one anchor, so the
  anchor's text is the datetime, the headline and the lede glued together.
- **udn** links each story three times — image (empty), `<h2><a>headline</a></h2>`
  (~27 chars) and `<p><a>summary</a></p>` (~250 chars). The summary always wins.

It matters because `find_articles` searches titles. udn and ftv therefore match
on lede text while the other six match on headlines only, inflating those two
in every keyword's coverage-share numerator (Q2). T-007 is planned to weight
headline and lede above body, so wrong titles would corrupt stance too.

## Fix

A heading element is the outlet saying "this is the title"; trust it over
length. If an anchor contains an `h1–h6`, use the heading's text; if the anchor
sits inside one, the anchor's own text is the headline. Rank heading-backed
candidates above plain ones, longest within rank. No class names, so it stays
inside the adapter's URL-pattern-over-CSS-paths philosophy and keeps working
across any redesign that preserves semantic headings.

## Files in scope

- `src/parallax/crawl/adapters/html_listing.py`
- `tests/test_adapters.py` (existing fixtures; no refresh needed — the bug
  reproduces against them)
- `scripts/retitle_from_listing.py` — one-off: re-run the fixed adapters and
  correct `title`/`title_seg` for rows still in the listing window. Touches
  nothing else; `seen_at` and therefore `effective_at` stay as first seen.
- `config/outlets.yaml` — comment only, recording the affected date range.
- `tests/fixtures/expected/*_listing.json` — golden output per pattern outlet
- this ticket file (archived to `done/` at ship)

## Do not touch

`src/parallax/db.py` — "first sight wins" in the upsert is deliberate (it
protects `seen_at`); the correction is a separate targeted UPDATE, not a change
to the upsert. `src/parallax/nlp/**`, `src/parallax/metrics/**`.

## Acceptance criteria

- On the saved udn and ftv fixtures, no title exceeds 60 characters and known
  headlines come out verbatim (`美職聯／多倫多FC 2比1擊退新英格蘭革命　終結13場不勝`,
  `韓美明大型軍演觸及台灣？美駐韓第8軍團首提「第一島鏈投射戰力」`).
- setn and chinatimes output is byte-identical before and after — the change
  must not alter outlets that were already correct. Pinned by golden files:
  the full `(url, title, published_at)` output of every pattern outlet on its
  fixture, compared exactly.
- ftv still recovers its listing timestamps (the datetime lives in a sibling of
  the heading, not inside it).
- udn's trailing-time strip still applies to the headline it now selects.
- After `make crawl.one OUTLET=udn` and `=ftv`, new rows have ~0% long titles.
- The retitle script is idempotent: a second run reports 0 changes.

## Knowingly not done

- **~6,800 udn/ftv rows from 2026-08-10 → 08-23 keep lede text as title.**
  The headline is not in the stored data (udn: absent; ftv: an unknowable
  prefix of the glued text) and the pages have scrolled off the listings, so
  recovery would mean one tier-2 fetch per row for articles nobody has searched.
  Not worth 4 hours of polite crawling. Recorded in `outlets.yaml` next to each
  outlet so anyone reading a title-based result over that window knows.

## Verify

```bash
uv run pytest -q tests/test_adapters.py
make crawl.one OUTLET=udn && make crawl.one OUTLET=ftv
uv run python scripts/retitle_from_listing.py          # then again: 0 changes
```
```sql
select outlet, round(100.0*count(*) filter (where length(title)>60)/count(*),1)
from article_index where seen_at > now()-interval '1 hour' group by 1;
```

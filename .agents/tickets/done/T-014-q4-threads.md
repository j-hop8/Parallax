# T-014 — Q4 platform decision: Threads now, Facebook parked, PTT/Dcard dropped

**Owner:** claude. Decision recorded 2026-09-20; doc + placeholder change only.

## Why

The proposal named PTT and Dcard as the phase-2 social platforms. Both are
losing users, and the political discussion Q4 is meant to measure has moved
to Threads. Facebook is still the largest platform in Taiwan, but there is no
read path that respects this project's crawling conduct: `facebook.com/robots.txt`
disallows crawling, CrowdTangle closed in Aug 2024, Meta Content Library is
application-gated to institutional researchers, and Graph API's Page Public
Content Access needs business verification. Threads, by contrast, has an
official keyword-search endpoint (`threads_keyword_search`: 2,200 queries per
rolling 24 h per user, ≤100 results per page, `since`/`until`, public posts
from any account once App Review passes).

Route chosen: **Threads now, Facebook parked** as a visible slot in the Q4
panel so the design does not move if a compliant path opens later.

## Files in scope

- `parallax-proposal.md` — §5 Phase 2 bullet, §11 week-6 milestone
- `db/schema.sql` — the `social_posts` header comment only (no DDL change;
  `platform` is free `TEXT`)
- `src/parallax/ui/render.py` — `q4()` placeholder labels
- `tests/test_ui_render.py` — the matching assertion
- `.agents/tickets/T-015-threads-ingestion.md`, `T-016-q4-platform-lean.md`
  — the work this decision unblocks, written here so they are ready to
  delegate after the VPS cutover
- this ticket

## Do not touch

Everything under `src/parallax/crawl/`, `nlp/`, `metrics/`, `jobs/`;
`db/migrations/`; `Design.pdf` (target UI still shows PTT/Dcard; the slot
layout is unchanged, only the labels).

## Acceptance criteria

- `PTT`/`Dcard` survive only as history: the decision sentence in the
  proposal, the `board` column note in `schema.sql`, and the negative
  assertion in the UI test. Nothing names them as a planned platform.
- Q4 panel renders `Threads · 第二階段` and `Facebook · 暫緩` with no digits.
- Full suite green, ruff clean.

## Verify

```bash
uv run pytest -q tests/test_ui_render.py && uv run ruff check . && ! grep -nE 'PTT|Dcard' src/parallax/ui/render.py
```

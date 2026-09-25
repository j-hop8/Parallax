# T-025 — Re-attach the stance gold set to a rebuilt database, by URL

**Owner:** claude — what happens to a row that cannot be matched is a judgement
about an irreplaceable file, not a mechanical edit. **Blocked by:** nothing.

## Why

The Mac Mini rebuild on 2026-09-25 found no database and no backup: the
`parallax_pgdata` volume was created fresh, and `article_index`, `articles` and
`crawl_runs` are all empty. The historical tier-1 index is gone and cannot be
rebuilt — RSS exposes hours (invariant 1).

What did **not** have to be lost is `eval/stance_gold.csv`: 184 hand-checked
labels, described in `eval/README.md` as hand-made and irreplaceable, and the
only thing standing between this project and §9's sentiment row. It is
committed, so the labels survived. But it keys on `article_id`, and ids belong
to the database — after a re-crawl every one of them points at nothing, and
`validation_sample`, `pending` and `evaluate` all join on that column.

The file also stores `url`, populated on all 184 rows, and `article_index` is
UNIQUE on `(outlet, url_canonical)`. So the labels can be re-attached to a
re-crawled index **exactly**, not approximately.

## Design — decided

1. **Match on `(outlet, canonicalize(url))`**, through the same
   `parallax.urls.canonicalize` the crawler used to write `url_canonical`. The
   gold file stores `url_original` (`https://www.setn.com/...`), so matching raw
   strings would miss every row. Outlet is part of the key because the index's
   UNIQUE constraint is — two outlets can serve the same path.

2. **An unmatched row keeps its id and is reported as pending.** A re-crawl
   fills in over days, so the tool is re-runnable by design. Dropping or
   zeroing unmatched rows would destroy the labels it exists to save.

3. **Two rows resolving to one id stops the whole file**, exit 2. That means the
   mapping is not a bijection; writing it would make two articles share an id
   and corrupt every downstream join. Judged on the *resulting* file — an
   unchanged row already holding the id a remapped row wants is equally broken.

4. **The same article under two targets is not a collision.** One article
   legitimately carries a label per target, and a second row per annotator once
   T-020's validation runs. The check is on distinct originating ids.

5. **Only `article_id` is ever rewritten.** Every other column is the
   annotator's work and is carried through byte-for-byte.

6. **Dry run by default**; `--write` rewrites atomically (temp + `os.replace`).
   The CSV is committed, so `git diff` is the review and `git checkout` the undo.

## `dup_gold.csv` cannot be saved

Its columns are `article_a, article_b, is_duplicate, annotator, labeled_at,
note` — ids and nothing else. There is no URL to match on, so those **118
labelled pairs are lost** with the database. Recorded in `eval/README.md` with
the fix for next time: add `url_a`/`url_b` before labeling more. T-017 already
keys posts on the permalink for exactly this reason; the pair set predates that
lesson.

## Files in scope

`src/parallax/nlp/remap.py` (new, pure), `src/parallax/db.py`
(`article_ids_by_canonical`), `scripts/remap_gold.py`, `Makefile`
(`gold.remap`), `eval/README.md`, `tests/test_remap.py`.

## Do not touch

`eval/stance_gold.csv` itself — this ticket ships the tool, not a rewrite of
the data. The prompts, `db/schema.sql`, `src/parallax/crawl/**`.

## Acceptance criteria

- Canonicalisation, tracking-param stripping and `www.` are handled, and the
  lookup keys on outlet too.
- An unmatched row survives with its label and is listed as pending.
- A collision refuses the whole file and exits 2.
- The same article under two targets, or two annotators, is not a collision.
- Only `article_id` differs between input and output rows.
- `make gold.remap` against a database with no matches reports 184 pending and
  writes nothing.

## Verify

```bash
uv run pytest -q tests/test_remap.py && uv run ruff check . && uv run pytest -q
```

## Shipped

Verified on the rebuilt Mini: 401 tests pass locally, ruff clean, and
`make gold.remap` against the empty index reports `remapped 0 / unchanged 0 /
pending 184` and writes nothing.

# T-030 — The public demo page: news only, a live-data strip, nothing leaked

**Owner:** codex. **Blocked by:** nothing (T-029 packages the host; no file overlap).

## Why

The Streamlit page is about to be served publicly (T-029: Caddy on the crawl
host, `https://<ip>.sslip.io`, no login). Three things are wrong with it for
that audience:

1. **Q4 is out of scope.** Social media (Threads, Q4) moved to version 2.0.0 on
   2026-09-25 -- the Threads API is currently broken and `social_posts` is
   empty. The page still draws a Q4 panel of empty slots beside the table.
2. **An empty-looking page says nothing about progress.** Today one keyword has
   stance rows and there are no clusters, yet the crawl has been indexing every
   article from 8 outlets every 10 minutes. A visitor should see that.
3. **Failures print internals.** `st.error(f"無法產生報告：{e}")` (app.py)
   shows a psycopg exception verbatim -- host, port, role -- on a public page.

## Design — decided, not open

**1. Gate Q4 at the presentation layer; delete nothing.** Add to
`src/parallax/settings.py`:

```python
# Q4 (social platforms) ships in v2.0.0. Off by default so the news-only line
# never shows an empty panel; the code, tables and tests stay for 2.0.0.
SOCIAL_ENABLED = os.environ.get("PARALLAX_SOCIAL", "0") == "1"
```

- `render.page(r, *, social: bool = SOCIAL_ENABLED)`: when off, no `q4(r)`
  column -- `table(r)` alone, full width (do not leave an empty grid cell).
- `jobs/report.py` `render(r, *, social: bool = SOCIAL_ENABLED)`: when off,
  `render_platform_lean` is not in the output.
- `build_report` is **not** touched: it still computes `platform_lean` (free on
  an empty table), so flipping the flag back on needs no metrics change.
- Existing Q4 tests keep their assertions. The two places that reach Q4
  through an entry point pass `social=True` explicitly and change nothing else:
  `test_q4_panel_is_reachable_from_the_page` (test_ui_render.py) and the `_q4`
  helper in `test_report_jobs.py`, which slices `render(r)` between the Q4 and
  Q3 headings. Tests calling `q4(...)` directly are untouched.

**2. A status strip, from queries that already exist where possible.**

- New `db.crawl_health(conn) -> list[dict]` in `src/parallax/db.py`: per
  outlet, over the last 24h of **ok** runs, `last_ok`, `ok_runs`,
  `largest_gap` -- the same SQL as `make health` (Makefile, `health:` target).
  Do not edit the Makefile.
- New `db.index_extent(conn) -> dict`: `count(*)` and `min(effective_at)` over
  `article_index` (order/bucket by `effective_at`, invariant 4).
- Reuse `db.complete_day_totals` (complete days per outlet = `len` of each
  list) and `db.rollup_as_of`. Reuse the keyword count `load_targets()` already
  fetches.
- `ui/app.py`: `load_status()` with `@st.cache_data(ttl=60)`; it swallows DB
  errors and returns `None` like `load_targets` does (the strip is a hint, not
  the page). Shown **on the landing view** (no keyword) under the prompt, and
  as **one line in the sidebar** under the pills.
- `ui/render.py`: `status_strip(status)` -- pure, escaped, no streamlit import
  (`test_render_module_does_not_import_streamlit` pins that). Shows: articles
  indexed and since when (Taipei date), last crawl time (Taipei, `HH:MM`),
  complete days per outlet (or min–max across outlets), number of analysed
  keywords. When **any** outlet's `last_ok` is older than **30 minutes** (three
  missed 10-minute slots), the strip carries a visible `爬蟲延遲` flag naming
  the outlets. `None` status renders nothing.
- Times are displayed in `Asia/Taipei` (invariant 3) -- use `render.TZ`.

**3. Say less on failure.** `app.py`: on a `build_report` exception, log it
with `logging.exception` and show `無法產生報告，請稍後再試。` -- no exception
text. Update `test_build_failure_is_an_error_box` to assert the new message
**and** that `connection refused` is absent from the page.

**4. A Q1 caveat for a public audience.** The article gold set
(`eval/stance_gold.csv`) is 184 rows, all annotated by `claude-opus-5`: the
0.733 macro-F1 is model-vs-model agreement, not human validation. Add
`STANCE_VALIDATION_NOTE` in `ui/render.py`, shown next to `STANCE_NOTE` under
the table:

> 文章立場目前由模型標註，評估用的黃金標準亦為模型標註，尚待人工驗證；請視為訊號而非量測值。

Do not edit `STANCE_NOTE` or `POST_STANCE_NOTE`.

## Files in scope

- `src/parallax/settings.py` -- `SOCIAL_ENABLED` only
- `src/parallax/db.py` -- `crawl_health`, `index_extent` only (append; no edits
  to existing functions)
- `src/parallax/ui/app.py`, `src/parallax/ui/render.py`
- `src/parallax/jobs/report.py` -- the `render(..., social=...)` gate only
- `tests/test_ui_app.py`, `tests/test_ui_render.py`, `tests/test_report_jobs.py`,
  and a new DB test for the two queries if you follow `tests/test_report_db.py`'s
  pattern (skips without a database)

## Do not touch

`src/parallax/metrics/**`, `src/parallax/social/**`, `src/parallax/jobs/social.py`,
`src/parallax/jobs/stance_social.py`, `src/parallax/crawl/**`, the `db/` directory
(schema and migrations), `Makefile`, `ops/**`, `CLAUDE.md`, `README.md`,
`parallax-proposal.md` (T-029 and T-031 own those), the text of `STANCE_NOTE`,
`POST_STANCE_NOTE` and `POST_STANCE_CAVEAT`.

## Acceptance criteria

- With `PARALLAX_SOCIAL` unset: `render.page(r)` contains no `px-q4`, and
  `jobs.report.render(r)` contains no `Q4 社群平台傾向`. With `social=True`,
  both are byte-identical to today's output (pin one of each).
- Landing view (no keyword) shows the prompt **and** the status strip; the
  sidebar shows the one-line status. Covered in `test_ui_app.py` with
  `crawl_health` / `index_extent` / `complete_day_totals` / `rollup_as_of`
  faked the way the fixture fakes `stance_targets`.
- Stale crawl: a fake `last_ok` 31 minutes old renders `爬蟲延遲` with that
  outlet's name; 29 minutes does not.
- Status DB failure: the page still renders the prompt, no error box.
- Build failure: generic message, no exception text anywhere on the page.
- `STANCE_VALIDATION_NOTE` is on the page whenever the table is.
- `render.py` still imports no streamlit; every interpolated value goes through
  `esc`.
- Ticket file moved to `.agents/tickets/done/`.

## Verify

```bash
uv run --extra ui pytest -q tests/test_ui_app.py tests/test_ui_render.py tests/test_report_jobs.py && uv run ruff check . && uv run --extra ui pytest -q
```

# T-018 — `make health` covers Threads: budget, last run per keyword, last error

**Owner:** codex (delegable — read-only status over tables T-015 created,
inside the job that already knows the budget rule). **Blocked by:** nothing.

## Why

CLAUDE.md says "check `make health` before assuming things are fine", and
today `health` reads only `crawl_runs`. Every Threads failure mode T-015
designed for ends as a `social_runs` row with `ok=false` and an `error`, and
nothing surfaces those rows except the stdout of the job that wrote them:

- the **own-posts guard** (`OWN_POSTS_ERROR`) — the only signal an unapproved
  app gives; a human who does not read it keeps burning budget on nothing;
- a **dead token** — the long-lived token lives 60 days with no refresh past
  expiry; from then on every run is a 401 recorded as `ok=false`;
- **budget refusals** — `used + estimate > THREADS_DAILY_QUERY_BUDGET`.

The rolling-24 h budget ceiling is a Python setting, so the block belongs in
`jobs/social.py` (which already computes `used`), not in raw psql in the
Makefile — a second copy of the constant would drift.

## Design — decided

`python -m parallax.jobs.social --status` prints a status block and exits 0.
It **must run without `THREADS_ACCESS_TOKEN`** (the always-on host has no
token by design — T-015 "laptop only — tier 2 secret" — and `make health` runs
there), so the `--status` branch is taken **before** the token check in
`main()`. It makes **no API call**, constructs **no `ThreadsClient`**, and
**writes no `social_runs` row** (unlike `--dry-run`, which records one). It
does not take the advisory lock.

Output, exactly this shape (values illustrative), Taipei times, `--` prefix
like the existing health commentary:

```
threads  budget used 24h: 37 / 1000   posts: 412 (2 keywords)   runs 24h: 3 ok, 1 failed
  keyword     last run (Taipei)   ok   seen  new   error
  沈伯洋      09-20 05:59         no   0     0     dry run
  萬安        09-19 22:10         yes  200   58
-- a run with error 'keyword_search returned only own posts …' means the app is not
-- approved for threads_keyword_search; a run of 401s past ~60 days means the token is dead:
-- make threads.refresh
```

- `budget used 24h` = the same query `ingest()` runs
  (`sum(queries)` over `social_runs` where `platform='threads'` and
  `started_at > now() - interval '24 hours'`) against
  `settings.THREADS_DAILY_QUERY_BUDGET`.
- `posts` = `count(*)` and `count(DISTINCT fetched_for)` from `social_posts`
  where `platform='threads'`.
- `runs 24h` = ok / not-ok counts in the same 24 h window.
- One row per distinct `keyword` in `social_runs` (all time), showing its
  **most recent** run: `started_at` as `MM-DD HH24:MI` in `Asia/Taipei`,
  `ok`, `items_seen`, `items_new`, `error` truncated to 60 chars. Order by
  most recent first. With no runs at all print `threads  no runs recorded`
  and the commentary lines, still exit 0.
- Write to stdout; exit 0 on success, 1 only if the database is unreachable
  (print the sanitized error — never a credential; `client.redact` is not
  available without a client, so just print `str(exc)`; there is no token in
  a DB error).

`make health` appends `@uv run python -m parallax.jobs.social --status` as its
last line. Pure `psql` output above it is unchanged.

## Files in scope

- `src/parallax/jobs/social.py` — `status(conn) -> dict` (the numbers) and
  `render_status(report) -> str` (the text), both pure so they are testable
  without Postgres given a fake `conn`; the `--status` argv branch in
  `main()`. Do not change `ingest()`, `taipei_window()` or the token /
  keyword checks for the other modes beyond reordering `--status` ahead of
  them.
- `Makefile` — the `health` recipe only (append one line). Touch nothing else
  in the Makefile: another ticket adds a `label.posts` target after `label:`
  in parallel; keep the hunks apart.
- `tests/test_social_job.py` — add tests (see below); do not modify existing
  ones.
- `ops/README.md` — if it has a "what to check" / health paragraph, add one
  sentence that `make health` now includes the Threads block and that the
  token-less host will simply show runs made from the laptop after a DB
  restore. If no such paragraph exists, skip this file.
- this ticket

## Do not touch

`src/parallax/social/**`, `src/parallax/db.py`, `src/parallax/settings.py`,
`src/parallax/nlp/**`, `src/parallax/metrics/**`, `src/parallax/ui/**`,
`src/parallax/jobs/{crawl_listing,rollup_daily,enrich,dedup,framing,stance,report,eval_*}.py`,
`db/**`, `config/**`, `scripts/**`, `eval/**`, `.venv/`.

## Implementation notes for Codex

- No new dependencies; the worktree `.venv` is pre-built and
  editable-installed against this worktree's `src/`. Do not delete or
  recreate it. The sandbox has no network.
- `settings.THREADS_ACCESS_TOKEN` is unset in the worktree (no `.env`), which
  is exactly the environment `--status` must work in.
- DB-backed tests use the `conn` fixture already in `tests/test_social_job.py`
  (transaction + rollback, skips with "Postgres unavailable"); in the sandbox
  they will skip, CI runs them for real — write them as if they run.
- Truncate `error` with the same `left(error, 60)`-style rule in Python, not
  SQL, so `status()` returns full strings and `render_status` decides width.
- If the sandbox refuses a write under `.agents/`, leave the ticket where it
  is and say so in your final message.

## Acceptance criteria

- `THREADS_ACCESS_TOKEN` unset → `python -m parallax.jobs.social --status`
  exits 0 and prints the block (test: monkeypatch the token to `None`, fake
  `db.connect`, assert exit 0 and that no `ThreadsClient` is constructed —
  monkeypatch the class to raise).
- `--status` writes no `social_runs` row and makes no HTTP request (DB test:
  count rows before/after; unit test: `requests.Session.get` monkeypatched to
  raise).
- `render_status` on a report with one ok and one failed run shows both
  keywords, the failed one's `error` truncated to 60 chars, budget as
  `used / THREADS_DAILY_QUERY_BUDGET` with the monkeypatched setting value.
- Empty tables → `threads  no runs recorded`, exit 0.
- `make health` runs end-to-end on a DB with the T-015 tables (the existing
  crawl block, then the Threads block).
- Unit tests pass without Postgres; full suite green, ruff clean, CI green.
- Ticket file moved to `.agents/tickets/done/` (if the sandbox allows; else
  say so).

## Knowingly out of scope

Alerting (T-013 follow-up), token-expiry tracking (the API exposes no issue
date; the 60-day rule stays a `ops/env.example` comment), changing what
`--dry-run` records, any change to the crawl half of `health`.

## Verify

```bash
uv run pytest -q tests/test_social_job.py && uv run ruff check . && uv run pytest -q
```

## Shipped

PR #19, squash-merged as `3c4ef06` on 2026-09-20. Codex run: 32k tokens.
Lead review: approve; verified on a scratch Postgres (284 passed, ruff clean),
`make health` end-to-end, and in CI — including a re-run after GitHub's
"update branch" merged main into the head, which branch protection required
because #18 landed first. Non-blocking nits left as PR comments: CJK keyword
width misaligns the table, `ZoneInfo("Asia/Taipei")` instead of
`settings.TIMEZONE`, ops/README sentence glued onto §7's paragraph.

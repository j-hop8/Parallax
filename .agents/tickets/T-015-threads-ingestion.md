# T-015 — Threads ingestion: keyword-driven fetch of public posts into `social_posts`

**Owner:** codex (delegable — self-contained, external API with fixtures).
**Blocked by:** (1) the VPS cutover (T-013 §5–§9) — nothing new ships to this
laptop's checkout while it is production; (2) a Meta app approved for
`threads_basic` + `threads_keyword_search`, with a long-lived user token in
`.env` as `THREADS_ACCESS_TOKEN`. Both are the human's tasks.

**Blocker status (2026-09-20, delegated):** (2) `THREADS_ACCESS_TOKEN` is now
in the laptop's `.env`; whether the app is *approved* is unknown, which is
exactly what the own-posts guard below exists for. (1) is still open -- the
crawl still runs from this checkout -- but this ticket touches nothing on the
tier-1 path and adds no dependency, so implementing it in `.worktrees/T-015`
and reviewing the PR is unblocked; whether to merge before the cutover is the
human's call at `/ship`.

## Why

Q4 (platform lean) is the last unanswered question. Decision T-014: Threads
first. This ticket is ingestion only — the lean metric, stance on posts and
the UI are T-016 — so that this half is fully specified and Codex-shaped.

Same cost model as tier 2: **fetch only for a keyword someone searched**,
never a standing crawl. Same conduct as tier 1: official API, honest
identification, conservative budget, raw responses cached so a parser fix
never re-hits the API.

## API facts (developers.facebook.com/docs/threads/keyword-search, checked 2026-09-20)

- `GET https://graph.threads.net/v1.0/keyword_search?q=<kw>&search_type=RECENT&since=<unix>&until=<unix>&fields=...&limit=100&access_token=...`
  (Meta's docs use both `graph.threads.net` and `graph.threads.com`; both
  are live. Default to `.net`, keep it in `THREADS_API_BASE`.)
- Budget: **2,200 queries per rolling 24 h per user.** Page size ≤ 100
  (default 25). `since` ≥ 1688540400, `until` ≤ now. Cursor pagination
  (`paging.cursors.after`).
- **Before App Review approval the same call silently returns only the
  token owner's own posts.** No error, no flag. See the guard below.

## Design

**Module:** `src/parallax/social/__init__.py`, `src/parallax/social/threads.py`.

- `class ThreadsClient` — `keyword_search(q, since, until, *, search_type="RECENT", page_size=100) -> Iterator[dict]` following cursors; one HTTP request = one budget unit. `User-Agent` as in `crawl/http.py` (bot name + contact). Retries only on 429/5xx with backoff; 4xx other than 429 raises. Fields: `id,text,username,permalink,timestamp,media_type,is_quote_post,has_replies` (verify against the docs' field list before coding; drop any that 400).
- `me() -> dict` — `GET /me?fields=id,username`, once per job run, for the guard.
- Raw cache: every response body gzipped to `raw/threads/<YYYY-MM-DD>/<sha1(url-without-token)>.json.gz`, mirroring `crawl/body.py:cache_path`. Never write the token to disk or logs.

**Job:** `src/parallax/jobs/social.py` (argv: `--keyword` required, `--since`, `--until` as Taipei dates defaulting to the last 7 days, `--limit` posts, `--dry-run`, `-v`). Makefile target `social` mirroring `enrich`.

1. Budget check first: `SELECT coalesce(sum(queries),0) FROM social_runs WHERE platform='threads' AND started_at > now() - interval '24 hours'`; refuse to start if `+ estimated pages > THREADS_DAILY_QUERY_BUDGET` (settings, default **1000** — half the ceiling, leaving room for T-016 backfills and a human at the console).
2. Record a `social_runs` row at start; update `queries`, `items_seen`, `items_new`, `ok`, `error` at end — **on every path**, like `crawl_runs` (invariant 2).
3. Fetch, then upsert into `social_posts` on `post_url` (= `permalink`): `platform='threads'`, `author=username`, `posted_at=timestamp`, `text`, `text_seg=segment_text(text)` (invariant 6), `fetched_for=<keyword>`, `raw_path`. Re-fetch of a known post updates nothing but `seen_at`.
4. **Own-posts guard:** if `items_seen > 0` and every returned `username` equals `me().username`, mark the run `ok=false, error='keyword_search returned only own posts — app not approved for threads_keyword_search?'` and write nothing. This is the only signal an unapproved app gives.
5. Time windows are Taipei days converted to UTC epoch at the boundary (invariant 3); `posted_at` stored as `timestamptz`, unchanged.

**Migration** `db/migrations/003_social_posts_threads.sql` (idempotent):

```sql
ALTER TABLE social_posts
    ADD COLUMN IF NOT EXISTS author        TEXT,
    ADD COLUMN IF NOT EXISTS fetched_for   TEXT,
    ADD COLUMN IF NOT EXISTS raw_path      TEXT,
    ADD COLUMN IF NOT EXISTS stance_model  TEXT,
    ADD COLUMN IF NOT EXISTS prompt_version TEXT;
CREATE INDEX IF NOT EXISTS social_posts_text_seg_fts_idx
    ON social_posts USING GIN (to_tsvector('simple', text_seg));
CREATE INDEX IF NOT EXISTS social_posts_platform_posted_idx
    ON social_posts (platform, posted_at DESC);
CREATE TABLE IF NOT EXISTS social_runs (
    run_id      BIGSERIAL PRIMARY KEY,
    platform    TEXT NOT NULL,
    keyword     TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    queries     INT NOT NULL DEFAULT 0,
    items_seen  INT NOT NULL DEFAULT 0,
    items_new   INT NOT NULL DEFAULT 0,
    ok          BOOLEAN NOT NULL DEFAULT FALSE,
    error       TEXT
);
```

`stance_model`/`prompt_version` are added here (not used until T-016) so the
verdict-comparability rule from `nlp/stance.py` holds from the first row.
Mirror the new columns in `db/schema.sql` so a fresh `make db.migrate` and an
upgraded DB end identical.

**Token lifecycle.** The long-lived token lives **60 days** and there is no
refresh token: `GET /refresh_access_token?grant_type=th_refresh_token&access_token=…`
returns a new 60-day token (allowed once the current one is ≥ 24 h old; an
expired token is dead for good and the human redoes the dashboard flow).
`jobs/social.py --refresh-token` calls it and **prints** the new token +
`expires_in`; it never writes `.env`. Makefile `threads.refresh`. `ops/env.example`
says: refresh monthly.

**Settings:** `THREADS_ACCESS_TOKEN` (env, no default; job exits 2 with a
one-line message when unset), `THREADS_DAILY_QUERY_BUDGET=1000`,
`THREADS_API_BASE="https://graph.threads.net/v1.0"`. Add the token line to
`ops/env.example` commented out, with "laptop only — tier 2 secret".

**`db.py`:** `find_social_posts(conn, keyword, platform, since, until)` — FTS on
`text_seg` with the segmented query (invariant 6) **OR** `fetched_for = keyword`,
so a post Threads matched but jieba split differently is still found.

## Implementation notes for Codex

- HTTP: use `requests`, as `crawl/http.py` does. **No new dependencies** --
  the sandbox has no network, so `uv sync` cannot fetch anything. The
  worktree's `.venv` is pre-built and editable-installed against this
  worktree's `src/`; do not delete or recreate it.
- No `.env` exists in the worktree, so `THREADS_ACCESS_TOKEN` is unset here:
  the exit-2 path is what you will see if you run the job by hand. Tests use
  fixtures only (mock `requests.Session.get` or inject a fake session).
- DB-backed tests: follow the transaction-and-rollback fixture in
  `tests/test_report_db.py` (skips with "Postgres unavailable"). In the
  sandbox they will probably skip; CI runs them against a real Postgres and
  fails the build on that skip, so write them as if they run.
- `text_seg`: `parallax.nlp.segment.segment_text` is read-only for you; import
  it, do not copy it.
- If the sandbox refuses a write under `.agents/` (it did in T-012), leave the
  ticket where it is and say so in your final message.

## Files in scope

- `src/parallax/social/__init__.py`, `src/parallax/social/threads.py` (new)
- `src/parallax/jobs/social.py` (new)
- `src/parallax/db.py` — the one new query + upsert helper
- `src/parallax/settings.py` — three settings
- `db/migrations/003_social_posts_threads.sql` (new), `db/schema.sql` (mirror)
- `Makefile` — `social` target; `ops/env.example`; `README.md` one line in **Run**
- `tests/test_threads_client.py`, `tests/test_social_job.py` (new) with recorded
  JSON fixtures under `tests/fixtures/threads/` — no live calls in tests
- this ticket

## Do not touch

`src/parallax/crawl/**`, `src/parallax/nlp/**`, `src/parallax/metrics/**`,
`src/parallax/ui/**`, `src/parallax/jobs/{crawl_listing,rollup_daily,enrich,dedup,framing,stance,report}.py`,
`config/outlets.yaml`, `ops/systemd/**`, existing migrations.

## Acceptance criteria

- `make social KEYWORD=沈伯洋` with a valid approved token writes rows with
  `platform='threads'`, non-null `text_seg`, `fetched_for`, `raw_path`; a
  second run within the window inserts 0 new rows and re-uses no API budget
  beyond the pages it actually fetched.
- With `THREADS_ACCESS_TOKEN` unset: exit code 2, one-line message, no DB write,
  no `social_runs` row.
- Budget: a fixture-driven test shows the job refusing to start when the last
  24 h of `social_runs.queries` + estimated pages exceeds the setting, and a
  `social_runs` row is written with `ok=false` explaining why.
- Own-posts guard fires on a fixture where all `username` == `me().username`;
  writes nothing; run recorded `ok=false`.
- Raw cache written per response; token appears nowhere under `raw/` or `logs/`
  (`! grep -r "$THREADS_ACCESS_TOKEN" raw logs`).
- 429 → backoff and retry; 400/401/403 → run recorded with the API's error
  string, job exits non-zero, no partial rows.
- Taipei-day `--since/--until` map to the right UTC epochs across the 08:00
  boundary (test the 2026-09-19 → 09-20 edge).
- `find_social_posts` returns a post whose `text_seg` does not contain the
  segmented keyword but whose `fetched_for` does.
- Schema mirror: `make db.migrate` on a fresh DB and on a DB carrying 001–002
  produce identical `\d social_posts` / `\d social_runs`.
- `--refresh-token` prints a token and expiry from a fixture response and
  leaves `.env` untouched.
- Full suite green, ruff clean, CI green.
- Ticket file moved to `.agents/tickets/done/`.

## Knowingly out of scope (T-016)

Stance on posts, the lean metric, `IncidentReport.platform_lean`, the Q4
panel going live, and a hand-labeled post gold set.

## Verify

```bash
uv run pytest -q tests/test_threads_client.py tests/test_social_job.py && uv run ruff check . && uv run pytest -q
```

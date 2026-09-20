# T-017 — Post stance gold set: blind labeling tool for Threads posts

**Owner:** codex (delegable — a sibling of `scripts/label_stance.py`, fully
specified by it). **Blocked by:** nothing. Real posts in `social_posts` are
needed to *use* the tool, not to build it; tests run on fixtures.

## Why

T-016 turns Threads posts into the Q4 number, and its acceptance criteria say
the panel keeps a "model-labeled, unvalidated" caveat until
`eval/post_stance_gold.csv` has ≥ 100 human rows. No tool exists to collect
them. This ticket is that tool and nothing else: the post stance *prompt*, the
eval job for posts, and the lean metric stay in T-016. (T-016 listed
`scripts/label_posts.py` and the CSV in its own scope; this ticket takes them
over so T-016 is smaller.)

The article tool's design carries over unchanged — see the docstring of
`scripts/label_stance.py` and the module docstring of `src/parallax/nlp/gold.py`:
blind (never shows a model verdict), writes every label immediately, resumes
without re-asking, shuffled so one author's run of posts does not prime the
annotator, loop logic in `nlp/gold.py` with I/O injected so it is tested.

## Design — decided, not open

**Gold file:** `eval/post_stance_gold.csv`, columns
`post_id, platform, post_url, author, target, label, annotator, labeled_at, note`.
**Identity for resume is `(post_url, target)`, not `post_id`:** the database is
about to move hosts (T-013) and a re-fetch assigns new ids; `post_url` is the
API permalink and the table's UNIQUE key. `post_id` is kept as a convenience
column only. Do **not** commit a CSV — the human creates it by labeling, and
`load_post_gold` treats a missing file as an empty set exactly like `load_gold`.

**Selection:** `db.find_social_posts(conn, keyword, "threads", since, until)`
(read-only for you; it already ORs FTS with `fetched_for`). `since` =
`datetime(2023, 7, 6, tzinfo=UTC)` (Threads launch), `until` = now, both
timezone-aware — see `tests/test_social_job.py::test_upsert_and_find` for the
call shape. Add `--since/--until` as inclusive/exclusive Taipei dates only if it
costs nothing; the default is "everything".

**Offered to the annotator:** posts whose `text` is non-empty after `.strip()`
and whose `(post_url, target)` is not in the gold file. Media-only posts are
dropped before shuffling and counted in the "pending" line. The table has no
`is_quote_post` column (that flag lives only in the raw cache), so nothing is
filtered on it.

**Shown per post:** `[i/n] @author · <posted_at as Asia/Taipei YYYY-MM-DD HH:MM>`
then the full `text` (posts are ≤ 500 chars; there is no lede/body split).
**Never shown:** `stance_label`, `stance_score`, `stance_model`,
`prompt_version`, `aspect_label`, `text_seg`. Add a test that runs a session
over a row carrying all of those and asserts none of the values appear in
anything written.

**Keys:** `n` neg, `e` neu, `p` pos, `s` skip, `o` print the permalink,
`q` quit (everything labeled so far is already on disk). `o` replaces the
article tool's `b`: a post has no "more body", but a reaction to something not
shown may need the link to make sense of. The guide (below) tells the human
that if they needed the link, `s` or a `context` note is the right answer,
because the model will only ever see `text`.

## Files in scope

- `src/parallax/nlp/gold.py` — **append a new section** `# ---- post gold set
  (T-017): eval/post_stance_gold.csv` below the pair section, mirroring the
  article set: `POST_GOLD_PATH`, `POST_COLUMNS`, `PostGoldRow` (frozen
  dataclass, fields = columns, `note: str = ""`), `load_post_gold(path)`
  (raises `ValueError` on a bad label, like `load_gold`), `append_post_gold`
  (header once, flush per row), `pending_posts(posts, gold, target, seed)`,
  `post_label_session(posts, *, target, annotator, gold_path, limit, read,
  write, now)` returning the same counts dict as `label_session`. Reuse `KEYS`
  and `_summary`; change nothing above the new section.
- `scripts/label_posts.py` (new) — argv `--keyword` (required), `--n`
  (default 50), `--annotator` (default `getpass.getuser()`), `--seed`. Exit 1
  with `no threads posts match {keyword!r}; run: make social KEYWORD={keyword}`
  when the query returns nothing. Same "N posts, M already labeled, K pending
  -> eval/post_stance_gold.csv" line as the article tool, plus
  `(dropped D media-only)` when D > 0.
- `Makefile` — one target directly after `label:`:
  `label.posts:` → `uv run python scripts/label_posts.py --keyword "$(KEYWORD)" $(ARGS)`.
  Touch nothing else in the Makefile (another ticket edits `health` in
  parallel; keep the hunks apart).
- `eval/README.md` — a new top-level section **after** the duplicate-pair
  section: `# Post stance gold set — annotation guide (T-017)`. Contents, in
  this order: what the file is and the ≥ 100-row threshold T-016 waits on;
  the same provenance rule (annotator column, human vs model); **the task**:
  same three labels and the same "stance toward the TARGET, not mood, not
  agreement" rule as articles, judged on the author's own words only; what
  differs on Threads — sarcasm is common: label the *intended* stance and note
  `sarcasm`; a bare reaction whose meaning depends on a post not shown
  (quote-post, reply) → `s` skip, or if you pressed `o` to decide, label and
  note `context`; hashtag-only or emoji-only text → `s`; a post about the
  target only in passing → `s` unless the mention is itself pointed;
  **keys** incl. `o`; **workflow** (`make social KEYWORD=…` first, then
  `make label.posts KEYWORD=… ARGS="--n 30 --annotator <you> --seed 1"`);
  **CSV columns**. Do not edit the two existing sections.
- `tests/test_label_posts.py` (new) — mirror `tests/test_label_stance.py`:
  round-trip + header written once; bad label raises; `pending_posts` skips
  labeled `(post_url, target)` pairs, keeps the same `post_url` under a
  different target, drops empty/whitespace `text`, and is reproducible with a
  seed; session appends each label immediately (assert file contents after the
  first key, not at the end); `q` returns the counts and keeps rows; `o`
  writes the permalink; unknown key re-prompts; the blindness test above;
  `scripts/label_posts.py::main` with `db.connect` and `db.find_social_posts`
  monkeypatched — exit 1 + hint on no rows, exit 0 and a session on rows.
  No live Postgres needed anywhere in this file.
- this ticket

## Do not touch

`src/parallax/nlp/stance.py`, `src/parallax/nlp/eval.py`, `src/parallax/db.py`,
`src/parallax/jobs/**`, `src/parallax/social/**`, `src/parallax/metrics/**`,
`src/parallax/ui/**`, `db/**`, `config/**`, `scripts/label_stance.py`,
`scripts/label_pairs.py`, `eval/stance_gold.csv`, `eval/dup_gold.csv`, the
existing article and pair sections of `nlp/gold.py` and `eval/README.md`,
`.venv/`.

## Implementation notes for Codex

- No new dependencies; the sandbox has no network and the worktree `.venv` is
  pre-built and editable-installed against this worktree's `src/`. Do not
  delete or recreate it.
- Taipei formatting: `posted_at.astimezone(ZoneInfo(settings.TIMEZONE))`;
  `settings.TIMEZONE` already exists. `posted_at` may be NULL in the schema —
  print `(no time)` rather than crash.
- If the sandbox refuses a write under `.agents/`, leave the ticket where it
  is and say so in your final message.

## Acceptance criteria

- `make label.posts KEYWORD=沈伯洋` on a DB with Threads rows offers non-empty
  posts newest-shuffled, shows author/time/text only, and appends to
  `eval/post_stance_gold.csv` after every key; re-running offers only the
  unlabeled remainder.
- With no matching rows: exit 1 and the `make social` hint; no file created.
- The gold file never contains a row whose `label` is outside `neg/neu/pos`;
  `load_post_gold` rejects one.
- A session over a row with `stance_label='pos'` and `stance_model='x'` never
  writes `pos`/`x` to the annotator (blindness test).
- `eval/README.md` post section present with the rules above; the two
  existing sections are byte-identical to `origin/main`.
- Full suite green, ruff clean, CI green.
- Ticket file moved to `.agents/tickets/done/` (if the sandbox allows; else
  say so).

## Knowingly out of scope

The post stance prompt (`post-v1`), verdict storage, the post eval job
(`jobs/eval_stance.py --platform threads` or sibling), `MIN_PLATFORM_POSTS`,
the lean metric and the Q4 panel — all T-016. Facebook.

## Verify

```bash
uv run pytest -q tests/test_label_posts.py tests/test_label_stance.py && uv run ruff check . && uv run pytest -q
```

## Shipped

PR #18, squash-merged as `e7bb193` on 2026-09-20. Codex run: 40k tokens.
Lead review: approve, scope clean, all acceptance criteria test-backed;
verified locally on a scratch Postgres (289 passed, ruff clean) and in CI.
Non-blocking nits left as PR comments: two function-local imports in
`post_label_session`, `@None` for a NULL author. The ticket file lands here
rather than in the PR because the Codex sandbox cannot write under `.agents/`.

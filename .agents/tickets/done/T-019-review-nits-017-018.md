# T-019 — Review nits from #18 (T-017) and #19 (T-018)

**Owner:** codex. **Blocked by:** nothing. Five small, fully specified fixes
left as non-blocking comments on the two PRs; bundled so they cost one review
round instead of two.

## Why

Both PRs were approved and shipped with the nits noted for a later sweep. Two
of them are in text a human reads at the console (`make health`, the labeling
prompt); the rest are consistency. None changes behaviour beyond what is
listed here.

## The fixes — exact

### A. `src/parallax/nlp/gold.py` (from #18)

1. **Lines 405–407** — `post_label_session` imports `zoneinfo` and `settings`
   inside the function body. Move them to module level: extend line 22 to
   `from ..settings import EVAL_DIR, TIMEZONE` and add
   `from zoneinfo import ZoneInfo` to the stdlib imports at the top; use
   `ZoneInfo(TIMEZONE)` in the function. There is no circular-import reason
   for the local imports (`gold.py` already imports from `..settings` at the
   top).
2. **Line 423** — `@{post['author']}` renders `@None` when `author` is NULL
   (the column is nullable). Render `@?` instead:
   `@{post.get('author') or '?'}`.

### B. `src/parallax/jobs/social.py` (from #19)

3. **Line 166** — `ZoneInfo("Asia/Taipei")` → `ZoneInfo(settings.TIMEZONE)`,
   matching lines 22 and 27 of the same module.
4. **Line 170** — `{run['keyword']:<10}` pads by code points, so a CJK keyword
   (`沈伯洋`, display width 6) lands 3–4 cells left of the `last run` header.
   Add two small helpers next to `render_status`:

   ```python
   def _display_width(s: str) -> int:
       """Terminal cells: East Asian Wide/Fullwidth characters take two."""
       return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)

   def _pad(s: str, width: int) -> str:
       return s + " " * max(0, width - _display_width(s))
   ```

   and use `_pad(run['keyword'], 10)` for the keyword column. Nothing else
   in the row format changes. `import unicodedata` at the top.

### C. `ops/README.md` (from #19)

5. **Line 121** — the sentence is glued onto the end of §7's "Expected:"
   paragraph, so it reads as part of the 40-minute check. Give it its own
   paragraph (blank line before) and reword to:

   > `make health` also prints a Threads block (T-018). On the token-less
   > host it shows the runs made from the laptop, as of the last restore.

## Files in scope

- `src/parallax/nlp/gold.py` — items 1–2 only
- `src/parallax/jobs/social.py` — items 3–4 only
- `ops/README.md` — item 5 only
- `tests/test_label_posts.py` — one test: a post with `author=None` renders
  `@?` in the header line and never the string `None`
- `tests/test_social_job.py` — tests: `_display_width("沈伯洋") == 6`,
  `_display_width("abc") == 3`, `_pad("沈伯洋", 10)` has display width 10;
  and in `test_status_render` (or a sibling) assert the CJK row's `last run`
  value begins at the same display-cell offset as the header's `last run`
  (compute with `_display_width` on the prefix before it). Existing tests
  must not be modified except to extend them.
- this ticket

## Do not touch

Everything else. In particular: `render_status`'s wording and line order,
the crawl half of `Makefile health`, `src/parallax/social/**`,
`scripts/**`, `eval/**`, `db/**`, `.venv/`.

## Implementation notes for Codex

- No new dependencies; the worktree `.venv` is pre-built and
  editable-installed against this worktree's `src/`. Do not delete or
  recreate it. The sandbox has no network; DB tests will skip here and run
  in CI.
- If the sandbox refuses a write under `.agents/`, leave the ticket where it
  is and say so in your final message.

## Acceptance criteria

- `grep -n "from zoneinfo\|from .. import settings" src/parallax/nlp/gold.py`
  shows only module-level imports.
- `python -m parallax.jobs.social --status` on a DB whose latest run keyword
  is `沈伯洋` prints the `last run` value directly under the header's
  `last run` (verified by the display-width test).
- `ZoneInfo("Asia/Taipei")` no longer appears anywhere under `src/`.
- `ops/README.md` §7 ends with the reworded Threads paragraph, separated by a
  blank line.
- Full suite green, ruff clean, CI green.
- Ticket file moved to `.agents/tickets/done/` (if the sandbox allows; else
  say so).

## Verify

```bash
uv run pytest -q tests/test_label_posts.py tests/test_social_job.py && uv run ruff check . && ! grep -rn 'ZoneInfo("Asia/Taipei")' src/ && uv run pytest -q
```

## Shipped

PR #21, squash-merged as `4df7164` on 2026-09-21. Codex run: 36k tokens.
Lead review: approve — scope clean, each source file carrying only its
numbered items. Verified on a scratch Postgres (296 passed, ruff clean,
`ZoneInfo("Asia/Taipei")` gone from `src/`) and in CI. The column-alignment
fix is pinned by a test asserting the CJK row's `last run` starts at the same
display offset as the header, which is the check the original nit lacked.

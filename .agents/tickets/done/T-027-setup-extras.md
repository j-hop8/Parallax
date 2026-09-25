# T-027 — `make setup` leaves a workstation unable to classify or search properly

**Owner:** claude. **Blocked by:** nothing.

## Why

Two failures on the rebuilt Mini, both from the same root: `make setup` was
never run, and nothing else makes up for it.

1. `make stance` died six times with
   `cannot import name 'genai' from 'google'`. The `llm` extra is optional and
   `uv run` syncs base dependencies only, so the classifier backend was simply
   absent.
2. Quieter and worse: `config/dict.txt.big` was missing, so jieba fell back to
   its **simplified-Chinese** dictionary. All 486 crawled titles had
   `title_seg` built with the wrong dictionary — and that column *is* the
   search index. Nothing errors; you just silently match fewer articles and
   dedup worse. After `make dict` + `make resegment` (532 titles, 6 bodies),
   the same keyword matched **6 → 7** articles.

`make setup` depends on `dict` and runs `uv sync`, so running it would have
fixed (2) — but not (1), because it never installed the extras. Following
`CLAUDE.md` verbatim still left stance broken.

## Design — decided

Two targets, because the two hosts genuinely differ:

- **`make setup`** — the workstation: `uv sync --extra llm --extra ui` plus the
  dictionary. `nlp` stays out; it drags in torch and nothing in the current
  pipeline uses it.
- **`make setup.crawl`** — the always-on crawl host (`ops/README.md`): base deps
  plus the dictionary, nothing else. That box runs tier 1 only, holds no model
  key and serves no page, and the existing comment in `pyproject.toml` is
  explicit that the crawler must install without the heavy extras.

**Both depend on `dict`.** The listing crawl segments every title it stores, so
a crawl host with the wrong dictionary corrupts the search index for everything
it ingests — the exact failure above, on the machine where it matters most.

`ops/README.md` switched to `setup.crawl`; `CLAUDE.md` now says what `setup`
installs and why plain `uv run` is not equivalent.

## Files in scope

`Makefile`, `ops/README.md`, `CLAUDE.md`, this ticket.

## Do not touch

`pyproject.toml` — the extras are correctly defined; only the install path was
wrong. CI pins its own `uv sync --extra ui` deliberately, proving the `llm`
extra stays optional (`test_importing_the_module_does_not_require_the_llm_extra`).

## Acceptance criteria

- `make -n setup` shows `uv sync --extra llm --extra ui`.
- `make -n setup.crawl` shows a bare `uv sync`.
- Both still depend on `dict`.
- `ops/README.md` points the crawl host at `setup.crawl`.
- `.PHONY` gains exactly `setup.crawl`.

## Verify

```bash
make -n setup | grep 'uv sync' && make -n setup.crawl | grep 'uv sync' && uv run ruff check .
```

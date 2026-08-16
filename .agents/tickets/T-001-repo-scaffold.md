# T-001 — Repo scaffold, schema, and dev tooling

*Written retroactively; the work is already on `claude/T-001-005-tier1-crawl`.*

## Goal

A runnable skeleton: Python package, Postgres, migrations, and the make targets
everything else is driven through.

## Files in scope

`pyproject.toml`, `Makefile`, `docker-compose.yml`, `db/schema.sql`,
`.gitignore`, `CLAUDE.md`, `src/parallax/{settings,models,db,config,urls}.py`

## Acceptance criteria

- `make db.up && make db.migrate` brings up Postgres 16 and applies the schema.
- `db/schema.sql` is idempotent and re-runnable (`CREATE TABLE IF NOT EXISTS`).
- `make setup` installs into `.venv`; `make test` runs pytest.
- `effective_at` is a generated column encoding the timestamp-trust rule once, in
  the schema, so no query has to remember it.
- `crawl_runs` exists from the start — tier-1 loss is unrecoverable, so a
  silently dead adapter is the highest-severity failure and per-run counts are
  the only way to see one.
- Day bucketing is `Asia/Taipei` everywhere, never UTC.

## Notes / deviations

- **Homebrew was unusable.** It ships no bottles for macOS 13 / x86_64, so every
  formula would have been a source build (installing `uv` began compiling Rust
  and LLVM). `uv` came from Astral's prebuilt binary instead.
- Postgres therefore runs in Docker on host port **5433**, not 5432, so a native
  Postgres installed later cannot collide with it.

## Verify

```bash
make db.up && make db.migrate && make test
```

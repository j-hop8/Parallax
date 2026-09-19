# T-012 — CI: run lint + the full test suite (DB tests included) on every PR

**Owner:** codex — shipped as #13 (`62d5fba`). Archived from the `claude/T-013`
branch: Codex's sandbox refused writes under `.agents/`, so the ticket never
made it into its commit.

## Why

The repo has never had CI. `.github/` does not exist, PRs #1–#12 show zero
checks, and `main` has no protection -- every "green" so far has been a
local `uv run pytest` on one laptop. The ship gate (`/ship`) reads
`statusCheckRollup` and finds nothing to read. One workflow closes that:
lint + tests on every pull request and on `main`, with a Postgres service so
the DB-gated tests (`test_report_db`, `test_dedup_db`, `test_framing_db`,
`test_rollup`, …) actually run instead of skipping -- a green run that skipped
the denominator tests would be a false signal.

## Design

One workflow, `.github/workflows/test.yml`, one job `test` on
`ubuntu-latest`:

- **Triggers:** `pull_request` (any branch) and `push` to `main`.
- **Postgres service** matching `docker-compose.yml` exactly: image
  `postgres:16`, user/password/db all `parallax`, published on host port
  **5433** (the project default -- `settings.DATABASE_URL` falls back to
  `postgresql://parallax:parallax@localhost:5433/parallax`, so no env var is
  needed if the port matches), with a `pg_isready` health check so the job
  waits for it.
- **Steps, in order:** checkout → `astral-sh/setup-uv` (cached) + Python
  3.12 → `uv sync --extra ui` (the UI tests `importorskip` streamlit; a
  skipped UI suite is the same false green as skipped DB tests) → `make
  dict`, cached on the URL in the Makefile → schema + migrations via `psql`
  in the Makefile's order with `ON_ERROR_STOP=1` → `uv run ruff check .`
  (not `ruff format --check`; nine files on `main` are unformatted and that
  is a separate format-only commit) → `uv run pytest -q -rs | tee
  pytest.log` then `! grep -q "Postgres unavailable" pytest.log`, turning a
  silent DB skip into a red run.

## Files in scope

- `.github/workflows/test.yml` (new)
- `README.md` -- one line under **Run** about what CI checks
- this ticket

## Do not touch

`Makefile`, `docker-compose.yml`, `db/**`, `src/**`, `tests/**`,
`pyproject.toml`, `uv.lock`.

## Acceptance criteria

- YAML parses; triggers, service and step order as above. ✓
- `PARALLAX_DATABASE_URL` never overridden; service on 5433. ✓
- Schema then migrations in sorted order, `ON_ERROR_STOP=1`. ✓
- Test step fails on a "Postgres unavailable" skip. ✓ (the `! grep` form is
  exempt from `errexit` and works only because it is the last command in
  the step; switch to `if grep -q …; then exit 1; fi` if anything is ever
  appended)
- `ruff format` not run. ✓
- README one-liner. ✓
- Ticket in `done/` — done here, not in #13.

## Live (2026-09-19)

Run 35426940252 on #13, the first CI run this repo ever had: every step
green, **259 passed, 0 skipped** -- the 17 DB-gated tests ran against the
service. Post-merge run on `main` (`62d5fba`) green. Branch protection then
applied: required check `test`, `strict: true`, `enforce_admins: false`.

## Verify

```bash
uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/test.yml'))" && uv run ruff check . && uv run pytest -q
```

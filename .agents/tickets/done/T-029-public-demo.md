# T-029 — Public demo page on the crawl host, without risking the crawl

**Owner:** claude — it puts a public web server on the host whose uptime is
invariant 1, so the isolation design is the ticket. **Blocked by:** #33 (T-028)
for the Makefile `ops.check` hunk; stacked on it and rebased before push.

## Why

People should be able to see progress at a URL. The crawl is moving to an
always-on VM anyway (invariant 1a, T-013's runbook); serving the existing
Streamlit page from the same VM costs one more unit and a proxy. But the page
takes free-text input from the public, and the crawl must never stop. So the
page gets its own unit and its own database role, and a bug or a crowd on the
page must be unable to take the crawl down with it.

Decided with the user 2026-09-25: fully public (no login), `<ip>.sslip.io`
until there is a domain, the page is news only (Q4 is v2.0.0 -- T-030/T-031).

## Design — decided

- **Own unit, outside `ops/systemd/`:** `ops/demo/parallax-ui.service`.
  `sched.install` installs every unit in `ops/systemd/`, and scheduling the
  crawl must never install a web server. Loopback only (`127.0.0.1:8501`),
  `MemoryMax=768M` (~300 MB RSS measured with a report loaded),
  `OOMScoreAdjust=500`, `Nice=10`.
- **Own role:** `db/migrations/004_ui_readonly_role.sql` creates `parallax_ro`:
  SELECT only, `default_transaction_read_only`, `statement_timeout = 15s`,
  `CONNECTION LIMIT 20`, `idle_in_transaction_session_timeout = 60s`. LOGIN but
  no password, so on a laptop it grants nothing usable.
- **No code change for the URL:** the unit's `EnvironmentFile=.env.ui` exports
  `PARALLAX_DATABASE_URL` for `parallax_ro`; `settings._load_dotenv` never
  overrides an exported variable, so the page never sees the owner URL.
- **Caddy** in front for automatic HTTPS (`ops/demo/Caddyfile`), proxying only
  to `127.0.0.1:8501`.
- **`make demo.install`** is re-runnable: it migrates, rotates the
  `parallax_ro` password, writes `.env.ui` (mode 600, gitignored), renders the
  unit and the Caddyfile, validates, restarts. `demo.uninstall` removes the
  unit and nulls the password.
- `ops.check` verifies the demo unit with the six crawl units and validates
  the rendered Caddyfile in `caddy:2`.

## Files in scope

`ops/demo/parallax-ui.service`, `ops/demo/Caddyfile`,
`db/migrations/004_ui_readonly_role.sql`, `Makefile` (help, `setup.crawl`
comment, `setup.demo` / `demo.install` / `demo.uninstall`, `ops.check`),
`ops/README.md` (sizing, §3 all-outlet dry run, §10, Operating), `CLAUDE.md`
(invariant 1, one sentence), `.gitignore` (`.env.ui`), `tests/test_demo_ops.py`,
this ticket.

## Do not touch

`src/**` (the page itself is T-030), `ops/systemd/**`, `docker-compose.yml`,
`db/schema.sql`, `CLAUDE.md` intro (T-031).

## Acceptance criteria

- `make ops.check` verifies 7 units and reports the Caddyfile valid.
- The migration is idempotent (applied twice cleanly) and `parallax_ro` can
  SELECT every public table and INSERT/UPDATE/DELETE/TRUNCATE none
  (`tests/test_demo_ops.py`, run against CI's migrated Postgres).
- As `parallax_ro`, an INSERT fails with `ReadOnlySqlTransaction`, and the
  full Streamlit page for 沈伯洋 renders with no errors (checked locally
  2026-09-25: 41 articles, 7 outlets, ~300 MB RSS).
- No unit under `ops/systemd/` mentions streamlit.

## Verify

```bash
uv run pytest -q tests/test_demo_ops.py && make ops.check && uv run ruff check . && uv run pytest -q
```

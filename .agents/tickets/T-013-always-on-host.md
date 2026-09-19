# T-013 — Package tier 1 for an always-on Linux host, and the cutover runbook

## Why

Invariant 1a in one number: **0 complete outlet-days in the last 7 days, 2
ever** (08-20, 08-21), out of 116 outlet-days recorded since 08-11. Last
night's largest gap was 6h57m on seven of eight outlets. Every weight the UI
shipped in T-011 says `2 / 16 天`, and no code can change that while the
crawl runs on a laptop that sleeps -- T-006 measured it, T-006b hardened
around it, T-010 and T-011 reported around it. The decision is now made: a
Linux VPS (x86_64). This ticket packages what already runs (the two launchd
jobs and the compose Postgres) for that host and writes the cutover so the
move is a checklist, not a project.

What moves: **tier 1 (`crawl_listing`, every 20 min), the daily rollup, and
Postgres.** What stays on the laptop: tier 2 (`enrich`, needs the Gemini
key and writes the raw-HTML cache), `dedup`, `framing`, `stance`, the UI --
all keyword-driven and interactive, all reaching the database through an
SSH tunnel that lands on the same `localhost:5433` the code already
defaults to. Nothing in `src/` changes.

## What the current setup said first (read-only, 2026-09-19)

- **The scheduler rationale transfers verbatim.** launchd was chosen over
  cron because it runs a missed job on wake. systemd timers do the same with
  `Persistent=true` -- a rollup missed during a reboot runs at boot -- and a
  oneshot service cannot overlap itself, which is the plist's
  `AbandonProcessGroup=false` for free.
- **No image needed.** Both jobs are `cd ROOT && uv run python -m
  parallax.jobs.<x>` from the working checkout; that is also how the VPS
  will run them (`git pull && uv sync` is the deploy). A crawler Docker image
  would add a build/push step and remove nothing. Compose stays for Postgres
  only.
- **Compose publishes Postgres on all interfaces with password `parallax`.**
  Harmless on a laptop, a public database on a VPS. Bind `127.0.0.1` and take
  the password from the environment with the old default, so local dev is
  unchanged.
- **Timezone is not a host property here.** Every day bucket is `AT TIME ZONE
  'Asia/Taipei'` in SQL; no `datetime.now()` in `src/`; the rollup timer
  states its zone explicitly. A UTC host produces identical rows.
- **The data is irreplaceable and 48 MB.** `pg_dump -Fc` is seconds; the VPS
  needs a daily dump with retention, because a single VPS disk is the only
  copy once the laptop's database is retired.
- **`make sched.install` is macOS-only** (launchctl, plutil, crontab). It
  needs a Linux branch, not a second target the runbook has to explain.
- Worst-case crawl cycle is proven to fit 20 minutes
  (`test_worst_case_crawl_cycle_fits_the_launchd_interval`), so
  `TimeoutStartSec=19min` on the crawl service is a backstop, not a budget.

## Design

**Units (`ops/systemd/`).** Four files, `@@ROOT@@` / `@@UV@@` / `@@USER@@`
substituted at install like the plists:
- `parallax-crawl.service` -- `Type=oneshot`, `WorkingDirectory=@@ROOT@@`,
  `ExecStart=@@UV@@ run python -m parallax.jobs.crawl_listing`,
  `TimeoutStartSec=19min`, `User=@@USER@@`. Its `.timer`: `OnCalendar=*:0/20`,
  `Persistent=true`, `OnBootSec=1min` (the plist's `RunAtLoad`).
- `parallax-rollup.service` / `.timer` -- `OnCalendar=*-*-* 00:20:00
  Asia/Taipei`, `Persistent=true`.
- `parallax-backup.service` / `.timer` -- daily 03:00 Asia/Taipei, `pg_dump
  -Fc` through `docker compose exec -T db` into `@@ROOT@@/backups/`, delete
  dumps older than 14 days. Same shape as `make db.dump`.
Logs go to the journal; `journalctl -u parallax-crawl` replaces
`logs/crawl.log`.

**`make sched.install` / `sched.uninstall`** dispatch on `uname -s`: Darwin
keeps today's launchd path untouched; Linux renders the units into
`/etc/systemd/system/` (sudo), `daemon-reload`, `enable --now` the timers,
and prints `systemctl list-timers 'parallax-*'`.

**`docker-compose.yml`:** `ports: "127.0.0.1:5433:5432"`; `POSTGRES_PASSWORD:
${PARALLAX_DB_PASSWORD:-parallax}`. `ops/env.example` documents the two
variables the VPS `.env` needs (`PARALLAX_DB_PASSWORD`,
`PARALLAX_DATABASE_URL`); `settings.py` already reads `.env`.

**`make db.dump` / `make db.restore FILE=…`** -- `pg_dump -Fc` to
`backups/parallax-<UTC stamp>.dump`; restore is `pg_restore --clean
--if-exists` into the compose database. These are the migration and the
restore drill.

**`make ops.check`** -- what can be verified on this Mac without a VPS:
render the units with dummy paths and run `systemd-analyze verify` on them
in an `ubuntu:24.04` container; then in a `python:3.12-slim` container with
uv, `uv sync` the project and run `crawl_listing --outlet cna --dry-run`
against the real feeds, proving the Linux wheels (psycopg-binary, lxml) and
the dictionary fetch work on the target platform. Docker is already a
requirement.

**Runbook (`ops/README.md`).** Provision (Ubuntu 24.04, `ufw` allowing only
22, unattended-upgrades) → install docker + uv → clone → `.env` → `make
setup db.up db.migrate` → **stop the laptop crawl** (`make sched.uninstall`)
→ `make db.dump` on the laptop, `scp`, `make db.restore` on the VPS → `make
sched.install` → `make health` on the VPS after 40 minutes (expect 2 ok runs
per outlet, gap ≤ 20 min) → laptop: `make db.down`, `ssh -N -L
5433:127.0.0.1:5433 <vps>` in a terminal or autossh, then `make report` /
`make ui` work unchanged → after 24h, `make health` shows every outlet's
largest gap under an hour, and the first complete outlet-days appear in
`outlet_daily_totals` the morning after. Plus: how to read the journal, how
to restore a backup, how to deploy a code change (`git pull && uv sync`;
timers pick up the next run).

## Files in scope

- `ops/systemd/parallax-{crawl,rollup,backup}.{service,timer}` (new)
- `ops/README.md` (new runbook), `ops/env.example` (new)
- `Makefile` (`sched.install` / `sched.uninstall` Linux branch, `db.dump`,
  `db.restore`, `ops.check`, help lines)
- `docker-compose.yml` (loopback bind, password from env)
- `.gitignore` (`backups/`)
- `README.md` (one line pointing at the runbook), `CLAUDE.md` (invariant 1a:
  one sentence that the host move is T-013 and how to check it)
- `.agents/tickets/done/T-012-ci-workflow.md` (recreated: Codex's sandbox
  could not write `.agents/`, so #13 merged without it)
- this ticket (archived to `done/` at ship)

## Do not touch

`src/**` (nothing here needs a code change; if the runbook seems to need
one, that is a finding for the ticket, not an edit), `db/**`, `tests/**`,
`ops/com.parallax.*.plist.template` (the macOS path keeps working as is),
`.github/**`.

## Acceptance criteria

- `make ops.check` passes on this Mac: `systemd-analyze verify` reports
  nothing for all six rendered units, and the dry-run crawl in the Linux
  container reports items for cna.
- Crawl timer fires every 20 minutes with `Persistent=true`; crawl service is
  `oneshot` with `TimeoutStartSec` ≤ 19 min; rollup timer is
  `00:20 Asia/Taipei`; backup timer daily with 14-day retention.
- `docker compose config` shows the Postgres port bound to `127.0.0.1` only;
  with no env set the password is still `parallax` and `uv run pytest -q`
  passes against the local database unchanged.
- `make db.dump` writes a `.dump` under `backups/`; `make db.restore
  FILE=<it>` into a throwaway compose project reproduces `select count(*)
  from article_index` and the 16 complete outlet-days.
- `make sched.install` on macOS is byte-for-byte the same behaviour as
  before (launchd path); on Linux it installs and enables the three timers.
- `TZ=UTC uv run pytest -q tests/test_rollup.py tests/test_report_db.py`
  passes (a UTC host buckets the same days).
- Runbook has the cutover as an ordered checklist with the expected output
  at each verification step, and states the one irreversible moment (laptop
  crawl stopped before the VPS crawl is confirmed → that window is lost).
- `T-012-ci-workflow.md` is in `done/`.
- `uv run pytest -q` and `uv run ruff check .` clean.

## Verify

```bash
uv run pytest -q && uv run ruff check . && make ops.check && docker compose config | grep -q '127.0.0.1:5433' && echo OK
```

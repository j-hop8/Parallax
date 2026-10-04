# T-036 — The Mac as the chinatimes + ftv crawl host, feeding the VM's database

**Owner:** claude -- cross-host ordering and the cutover's one irreversible
moment. **Blocked by:** T-035 (the per-host outlet selection).

## Why

chinatimes and ftv challenge datacenter IPs (T-035 has the evidence); the Mac
mini on a Taiwanese home connection is the only host that can crawl them. The
VM (Oracle A1, Tokyo) takes the database, the demo page, backups and the other
six outlets. The Mac must therefore (a) reach the VM's Postgres reliably and
unattended, (b) crawl only its two outlets, and (c) not run the rollup the VM
already runs. The runbook's cutover (§5–§8) assumed one crawl host; it has to
describe two.

The Mac mini is a valid host for this, unlike the laptop invariant 1a was
written about: measured 2026-10-01, `pmset` shows `sleep 0` and
`autorestart 1`, uptime 3 days with no sleep events. Its one gap is FileVault:
after a restart nothing runs until someone logs in.

## Design — decided

- **Tunnel as a launchd agent** (`ops/com.parallax.tunnel.plist.template`):
  `/usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3
  -L 5433:127.0.0.1:5433 <vm>`, `KeepAlive` + `RunAtLoad` + a throttle, so a
  dropped tunnel is back within seconds. It formalises runbook §8's manual
  `ssh -N -L`. Installed by `make tunnel.install` (macOS), separate from
  `sched.install`, like `backup.pull.install`.
- **One name for the VM:** `PARALLAX_VM_HOST=parallax@<ip>` in the Mac's
  `.env`, used by the tunnel and by `backup.pull` (which keeps accepting
  `PARALLAX_BACKUP_HOST` for anyone who already set it).
- **`make sched.install SCHED_JOBS=crawl`** on the Mac: the launchd loops take
  their job list from `SCHED_JOBS` (default `crawl rollup`, unchanged), so the
  rollup runs only on the VM. Two rollups against one database at 00:20 would
  race on `outlet_daily_totals`.
- **Mac `.env`:** `PARALLAX_CRAWL_OUTLETS=chinatimes,ftv`,
  `PARALLAX_DATABASE_URL` with the VM's password on `127.0.0.1:5433` (the
  tunnel), `PARALLAX_HEARTBEAT_URL` = a **second** healthchecks.io check (each
  host proves its own crawl). **VM `.env`:** `PARALLAX_CRAWL_EXCLUDE=chinatimes,ftv`
  and its own heartbeat check.
- **Runbook:** a new §12 "Split crawl" and a rewritten cutover order: dump the
  Mac DB → restore on the VM → start the VM's crawl (6 outlets) → on the Mac,
  `db.down`, `tunnel.install`, switch `.env`, `sched.install SCHED_JOBS=crawl`.
  The window between the Mac's dump and its first crawl into the VM must stay
  under 10 minutes: articles published inside it are still in the feeds at
  the next crawl, but udn turns its feed over in 13.6 min (T-022).
- **Mac restart hardening** (the user chose this 2026-09-26): keep FileVault,
  turn off automatic *installation* of macOS updates (System Settings →
  General → Software Update → Automatic Updates), so restarts only happen when
  someone is there to log in. A power cut still needs a login; the Mac's
  heartbeat check emails within ~40 minutes.
- **CLAUDE.md invariant 1a:** one paragraph: which host crawls what, and why
  the Mac mini qualifies where the laptop did not.
- **While here, fix the first-boot race seen 2026-10-04:** `db.wait` ran
  `pg_isready` over the Unix socket, which answers during the postgres image's
  temporary init server; `db.migrate` then hit "the database system is
  shutting down". Wait on TCP (`pg_isready -h 127.0.0.1`), which the init
  server does not listen on.

## Files in scope

`ops/com.parallax.tunnel.plist.template`, `Makefile` (`SCHED_JOBS`,
`tunnel.install` / `tunnel.uninstall`, `PARALLAX_VM_HOST` in `backup.pull`,
`db.wait`, help), `ops/README.md` (§5–§8 order, §11 host var, new §12),
`.env.example`, `CLAUDE.md` (invariant 1a), `tests/test_oracle_ops.py`, this
ticket.

## Do not touch

`src/**` (T-035), `ops/env.example` (T-035), `ops/systemd/**`, `ops/demo/**`,
`db/**`, `config/outlets.yaml`.

## Acceptance criteria

- The tunnel plist lints (`plutil -lint`), keeps alive, forwards only
  `127.0.0.1:5433`, and is installed by neither `sched.install` nor
  `backup.pull.install`.
- `make sched.install` with no `SCHED_JOBS` still installs crawl + rollup;
  `SCHED_JOBS=crawl` installs only the crawl, and `sched.uninstall` removes
  whatever is installed.
- `backup.pull` works with `PARALLAX_VM_HOST` or the old
  `PARALLAX_BACKUP_HOST`.
- `db.wait` waits on TCP; a fresh `make db.up db.migrate` on an empty volume
  succeeds first time (rehearsed with the `pxdrill` project).
- `make ops.check` still passes; the runbook's new order is one copy-pasteable
  sequence per host.

## Verify

```bash
uv run pytest -q tests/test_oracle_ops.py && make ops.check && uv run ruff check . && uv run pytest -q
```

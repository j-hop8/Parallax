# T-032 — Oracle Always Free host: the runbook deltas, its firewall, off-host backups

**Owner:** claude — it decides how a reclaimable free VM is allowed to hold
the one dataset that cannot be re-fetched. **Blocked by:** nothing.

## Why

Decided with the user 2026-09-26, after comparing providers: tier 1 and the
demo page go to **Oracle Cloud Always Free, Tokyo** (Ampere A1 arm64, 2 OCPU /
12 GB, $0). The runbook (`ops/README.md`) was written for a generic Ubuntu VPS,
and three things differ on Oracle:

1. **Idle reclamation.** Always Free instances idle for 7 days (CPU p95 < 20%
   and network < 20% and, on A1, memory < 20%) are reclaimed. A metadata crawl
   is idle by the first two tests.
2. **The VM can vanish**, and with it the only copy of the daily dumps
   (`parallax-backup.timer` writes them to the VM's own disk).
3. **Its Ubuntu image ships its own iptables rules** that reject everything but
   SSH; the runbook's `ufw` lines open nothing there.

## Design — decided

- **Runbook:** an "Oracle Cloud Always Free" block before §1 covering the
  risk and its four guards (PAYG + budget alert; watch the memory metric and
  shrink to 1 OCPU / 6 GB if under 20% on a non-PAYG account, since reclamation
  needs all three tests; T-033's heartbeat; §11's pulled backups), creating
  the instance (home region Tokyo, the shape, the security list, the IP's
  lifetime), and how §1/§10 differ on that image (the `ubuntu` user, a
  password for `parallax`, no ufw, restarts need nobody, keep
  unattended-upgrades' `Automatic-Reboot "false"`). Target line: arm64 is fine
  and `ops.check` on an Apple Silicon Mac already verifies it.
- **`make oci.firewall`:** one idempotent `iptables -I` ACCEPT for 80/443 ahead
  of the image's REJECT, persisted with `netfilter-persistent save`. Refuses to
  run where netfilter-persistent is absent (not Oracle's image -- use ufw).
- **§11, off-host backups:** `make backup.pull` rsyncs the VM's `backups/`
  into the workstation's `backups/vm/` (`--ignore-existing`, keeps the newest
  30) and exits non-zero with `STALE` when the newest dump is over 36h old.
  `make backup.pull.install` schedules it daily at 04:00 (an hour after the
  VM's 03:00 dump) with its own launchd agent, **not** part of
  `sched.install`, so the cutover's `sched.uninstall` leaves it running.
  Host from `HOST=` or `PARALLAX_BACKUP_HOST` in `.env`.

## Files in scope

`ops/README.md`, `Makefile` (help, `oci.firewall`, `backup.pull*`),
`ops/com.parallax.backup-pull.plist.template`, `.env.example`,
`tests/test_oracle_ops.py`, this ticket.

## Do not touch

`src/**`, `ops/systemd/**`, `ops/demo/**` (T-034), `db/**`, `CLAUDE.md`,
`ops/env.example` (T-033 edits it).

## Acceptance criteria

- The pull plist lints (`plutil -lint`) and runs `make backup.pull` at 04:00.
- Both launchd loops in the Makefile still name exactly `crawl rollup`.
- `OCI_RULE` opens exactly ports 80 and 443.
- With `rsync` stubbed: 32 remote dumps → the newest 30 kept, exit 0; a newest
  dump 40h old → non-zero with `STALE`; no host → refuses naming
  `PARALLAX_BACKUP_HOST`.

## Verify

```bash
uv run pytest -q tests/test_oracle_ops.py && uv run ruff check . && uv run pytest -q
```

# T-034 — Sandbox the public page: hide .env and the Docker socket from it

**Owner:** claude — security fix found while planning T-032, before the page
is exposed publicly. **Blocked by:** nothing.

## Why

T-029's `parallax-ui.service` reads Postgres as the read-only `parallax_ro`
role, and its *environment* never carries the owner URL. But it runs as the
crawl's user (`User=@@USER@@`), which:

- owns `.env`, holding `PARALLAX_DATABASE_URL` for the owner role, and
- is in the `docker` group, and the Docker socket is root on the host.

So any code-execution bug in a public Streamlit page could read the owner
password off disk, or take the host through the socket. The read-only role
only bounded what the page's *configuration* could do, not the process.

## Design — decided

Hide both from the unit's mount namespace rather than add a second user:
`InaccessiblePaths=-@@ROOT@@/.env -/run/docker.sock -/var/run/docker.sock`.
`EnvironmentFile=.env.ui` still works, because systemd reads it before the
namespace exists. `settings._load_dotenv` already treats an unreadable `.env`
as absent (`except OSError`), so nothing in `src/` changes. Plus cheap
hardening that costs the page nothing: `ProtectSystem=full`,
`ProtectKernelTunables`, `ProtectKernelModules`, `ProtectControlGroups`,
`RestrictSUIDSGID` (NoNewPrivileges and PrivateTmp were already set).

## Files in scope

`ops/demo/parallax-ui.service`, `ops/README.md` (§10 description + an
nsenter check), `CLAUDE.md` (invariant 1, one clause), `tests/test_demo_ops.py`,
this ticket.

## Acceptance criteria

- `make ops.check` verifies the hardened unit (systemd-analyze).
- Tests pin both hidden paths, and that `_load_dotenv` survives a
  PermissionError.
- On the VM (§10): `nsenter` into the page's namespace cannot read `.env` or
  the Docker socket, and the page still renders.

## Verify

```bash
uv run pytest -q tests/test_demo_ops.py && make ops.check && uv run ruff check . && uv run pytest -q
```

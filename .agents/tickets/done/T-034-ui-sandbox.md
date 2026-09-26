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

## Design — decided (revised after the Codex review on #39)

First version: hide `.env` and the socket from the unit's mount namespace
(`InaccessiblePaths`) while still running as the crawl's user. Codex showed
it leaks: any process of that user can open `/proc/<crawl-pid>/root/...`,
the crawl's unmasked view. Reproduced in systemd 255 (Ubuntu 24.04's): the
same-user sandbox read `.env` straight through `/proc`.

So the page gets **an identity of its own**:

- `DynamicUser=yes` + `SupplementaryGroups=@@GROUP@@`: a transient user that
  reads the code through the crawl user's group (home `0750`, files `0644`)
  but cannot read `.env` (`0600`, which `demo.install` now enforces), cannot
  touch the Docker socket (`root:docker 0660`), and is refused on another
  user's `/proc/<pid>/root`. DynamicUser also implies `ProtectSystem=strict`,
  `ProtectHome=read-only`, `PrivateTmp`, `NoNewPrivileges`, `RestrictSUIDSGID`.
- `ExecStart` is the venv's interpreter, not `uv run` -- that user cannot
  write the venv or uv's cache; deploys already `uv sync --extra ui`.
- `InaccessiblePaths` stays as a second layer. `EnvironmentFile=.env.ui` is
  read by systemd as root before the drop, so it still works at `0600`.
- The runbook's check no longer uses root `nsenter` (root passes any
  permission test). It runs probes under the same identity rules with
  `systemd-run -p DynamicUser=yes -p SupplementaryGroups=…`: a control that
  must succeed, and `.env`, the socket and `/proc/<pid>/root` that must fail.

Verified in a throwaway systemd 255 container: control readable, venv python
through the home runs, `.env` / socket / `/proc` route all refused, while the
old same-user design leaked the secret through `/proc`; the rendered unit
starts as `parallax-ui` with groups `parallax,parallax-ui`, the RO URL, 768M
cap and `ProtectSystem=strict`.

## Files in scope

`ops/demo/parallax-ui.service`, `ops/README.md` (§10 description + the
identity check), `CLAUDE.md` (invariant 1, one clause), `Makefile`
(`demo.install`: `chmod 600 .env`, `@@GROUP@@`, the streamlit-in-venv check;
`ops.check`: `@@GROUP@@` and a stub venv interpreter), `tests/test_demo_ops.py`,
this ticket.

## Acceptance criteria

- `make ops.check` verifies the hardened unit (systemd-analyze).
- Tests pin both hidden paths, and that `_load_dotenv` survives a
  PermissionError.
- On the VM (§10): the control probe reads the code; `.env`, the socket and
  the `/proc/<pid>/root` route are refused; the page still renders.

## Verify

```bash
uv run pytest -q tests/test_demo_ops.py && make ops.check && uv run ruff check . && uv run pytest -q
```

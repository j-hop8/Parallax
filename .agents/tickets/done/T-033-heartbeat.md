# T-033 — Crawl heartbeat: a free dead-man's switch for tier 1

**Owner:** codex. **Blocked by:** nothing. (T-032 runs in parallel on
`ops/README.md`, the Makefile and a new plist; this ticket touches none of those.)

## Why

Tier 1 is moving to an Oracle Always Free VM in Tokyo. That tier can take the
crawl away without telling anyone: Oracle reclaims instances idle for 7 days,
and a metadata crawl looks idle. The crawl can also die on its own (a DB that
won't start, a bad deploy). Invariant 1 says any window the crawl is down is
permanently lost, so someone must find out within minutes, not at the next
`make health`.

A dead-man's switch catches every one of those cases, including "the VM is
gone": the crawl pings a URL after every scheduled run, and an external
service emails when the pings stop. healthchecks.io's free tier does exactly
this; the user creates the check (10-minute period, 30-minute grace) and puts
its ping URL in `.env`.

## Design — decided, not open

1. **`PARALLAX_HEARTBEAT_URL`** in `src/parallax/settings.py`, default unset
   (`None`). Unset means no pings at all -- laptops and CI are unaffected.
2. **When to ping:** only on a scheduled full run, i.e. in
   `jobs/crawl_listing.main()` when **not** `--dry-run` and **no** `--outlet`.
   A manual `make crawl.one` must not report the whole crawl healthy.
   - Every outlet ok → `GET <url>`.
   - Any outlet failed (the path that returns 1) → `GET <url>/fail`
     (healthchecks.io's failure endpoint), so a persistently failing outlet
     alerts too.
   - If the process crashes before pinging, nothing is sent -- the missing
     ping *is* the alert. Do not add a `finally:` success ping.
3. **The ping can never hurt the crawl** (invariant 2): at most 2 attempts,
   `timeout=5` seconds each, via plain `requests.get`. Any exception is caught
   and logged at WARNING as `heartbeat ping failed: <ExceptionClassName>` --
   **never log the URL** (anyone holding it can forge pings). The return code
   of `main()` is exactly what it would have been without the heartbeat.
   The ping runs after the DB writes, i.e. after `crawl_all()` returns.
4. **The cycle must still fit.** A heartbeat adds up to ~10s to a cycle whose
   worst case is 534s, and `ops/systemd/parallax-crawl.service` has
   `TimeoutStartSec=9min` (540s) -- a hung cycle plus a hung ping would now be
   killed mid-ping. Raise it to `TimeoutStartSec=570` (still under the 600s
   slot) and update that file's comment. Then extend
   `tests/test_crawl_health.py::test_worst_case_crawl_cycle_fits_the_launchd_interval`
   so it adds the heartbeat's maximum (attempts x timeout, read from the code's
   constants, not hard-coded in the test) and asserts
   `worst_case + heartbeat < TimeoutStartSec < interval`, parsing
   `TimeoutStartSec` from the unit file the way it already parses the plist.
5. `ops/env.example` documents the variable, with the healthchecks.io
   settings above, next to the database lines (the crawl host needs it).

## Files in scope

- `src/parallax/settings.py` -- `HEARTBEAT_URL` only
- `src/parallax/jobs/crawl_listing.py`
- `ops/systemd/parallax-crawl.service` -- `TimeoutStartSec` and its comment only
- `ops/env.example`
- `tests/test_crawl_health.py` -- the worst-case test only
- a new `tests/test_heartbeat.py`
- this ticket

## Do not touch

`src/parallax/crawl/**` (adapters, listing, http), `db/**`, `Makefile`,
`ops/README.md`, `ops/demo/**`, `ops/*.plist.template`, `CLAUDE.md`,
`README.md`, `src/parallax/ui/**`, `src/parallax/metrics/**`.

## Acceptance criteria

- Unset URL: no HTTP call is made (assert with a fake `requests.get` that fails
  the test if called).
- All outlets ok → one `GET <url>`; one failed → one `GET <url>/fail`; exit
  codes 0 and 1 respectively, unchanged.
- `--dry-run` and `--outlet cna` make no heartbeat call.
- `requests.get` raising (timeout, connection error) twice: `main()` returns
  the same code it would have, the WARNING names the exception class, and the
  URL string appears nowhere in the captured logs.
- The worst-case test fails if `TimeoutStartSec` is set back to `9min`, and
  passes at `570`.
- Tests fake `crawl_all` / `wait_for_network`; no network, no database needed.
- Ticket file moved to `.agents/tickets/done/`.

## Verify

```bash
uv run pytest -q tests/test_heartbeat.py tests/test_crawl_health.py && uv run ruff check . && uv run pytest -q
```

## Completion

- Added the optional full-run heartbeat with two 5-second attempts, failure
  pings, unchanged crawl exit codes, and URL-safe logging (including verbose
  transport logs).
- Documented the crawl-host setting and raised the service timeout to 570s.
- Verify passed: 30 heartbeat/crawl-health tests, Ruff clean, full suite
  439 passed and 1 skipped (3 third-party jieba warnings).
- Confirmed the timing regression rejects `9min` (544s exceeds 540s) and
  passes at 570s, substituting the old timeout in memory only.

# T-035 — Host-level outlet selection: each crawl host crawls its own subset

**Owner:** codex. **Blocked by:** nothing. (T-036 builds the Mac side on top of
this; it touches no file listed here.)

**Status:** done (2026-10-04).

Host selection, validation, manual overrides, dry runs, and per-run INFO counts
are implemented. The subset heartbeat regression extends T-033 coverage in the
new in-scope `tests/test_crawl_subset.py`; `tests/test_heartbeat.py` is unchanged.
The Verify command passed: 81 targeted tests, Ruff clean, and 463 full-suite
tests passed with 38 skipped (three jieba dependency warnings). Verification used
`UV_CACHE_DIR=/private/tmp/parallax-T-035-uv-cache` to keep the cache writable
inside the sandbox. `config/dict.txt.big` and its target were not modified.

## Why

Measured 2026-10-04 from the Oracle VM (Tokyo) that is about to host tier 1:
the same crawler, same honest User-Agent, gets **403 + a Cloudflare bot
challenge** (`cf-mitigated: challenge`, "Just a moment...") from
**chinatimes** (listing page *and* the sitemaps its robots.txt advertises) and
**ftv** (listing page; it publishes no sitemap or feed). From the Mac mini on a
Taiwanese home connection both return items (137/137 ok runs in 24h). The
other six outlets work from the VM. Routing around a bot challenge (proxies,
browser impersonation, solving it) is out of bounds under CLAUDE.md's crawling
conduct, and those two outlets are the poles of the stance spectrum, so they
cannot be dropped either.

Decision (user, 2026-10-04): the VM crawls six outlets; the Mac crawls only
chinatimes + ftv, writing into the VM's database (T-036). Today a scheduled
crawl always takes every verified outlet; it needs a per-host selection.

## Design — decided, not open

1. **Two env vars, read in `src/parallax/settings.py`:**
   `PARALLAX_CRAWL_OUTLETS` (comma-separated codes: crawl only these) and
   `PARALLAX_CRAWL_EXCLUDE` (comma-separated codes: crawl every verified
   outlet except these). Whitespace around codes is ignored; empty/unset means
   "not set". Both set at once is a configuration error.
   - The VM will set `PARALLAX_CRAWL_EXCLUDE=chinatimes,ftv` (an exclude list,
     so it never silently stops crawling an outlet added later).
   - The Mac will set `PARALLAX_CRAWL_OUTLETS=chinatimes,ftv`.
2. **Applies to scheduled runs and dry runs** (`crawl_listing.main()` without
   `--outlet`). An explicit `--outlet X` overrides both vars (manual runs).
3. **Misconfiguration fails loudly, never by skipping** -- the whole point of
   invariant 1 is that a silently uncrawled outlet is permanent data loss:
   - both vars set → log an ERROR and `main()` returns 2;
   - any code in either list not in `config/outlets.yaml` → same, naming it;
   - an unverified outlet named in `PARALLAX_CRAWL_OUTLETS` → the same
     `UnverifiedOutletError` an explicit `--outlet` gets (naming it is an
     explicit act; see `_check_explicit_target`).
4. **`crawl_all` / `crawl_dry_run` take a selection, not just one code:**
   generalise `only: str | None` to accept `str | Collection[str] | None`
   (a single code keeps working unchanged for `--outlet`), and extend
   `_check_explicit_target` to validate each named code. The bulk path's rule
   is unchanged: unverified outlets *not* named are skipped as today.
5. **Say what this host crawls**, once per run at INFO before crawling:
   `crawling 6 of 8 verified outlets on this host (PARALLAX_CRAWL_EXCLUDE: chinatimes, ftv)`
   -- `journalctl` and the launchd log then show the split at a glance.
6. **Heartbeat unchanged:** a scheduled subset run is still a scheduled run, so
   it pings (T-033's `args.outlet is None` rule already covers it). Each host
   will have its own healthchecks.io check (T-036).
7. `ops/env.example` documents `PARALLAX_CRAWL_EXCLUDE=chinatimes,ftv` for the
   VM with the one-line reason and a pointer to T-036 for the Mac side.

## Files in scope

- `src/parallax/settings.py` -- the two settings only
- `src/parallax/crawl/listing.py` -- the selection parameter and validation
- `src/parallax/jobs/crawl_listing.py`
- `ops/env.example`
- tests: extend `tests/test_crawl_health.py` where it already tests
  `_check_explicit_target`, plus a new `tests/test_crawl_subset.py`
- this ticket

## Do not touch

`Makefile`, `ops/README.md`, `ops/*.plist.template`, `ops/systemd/**`,
`ops/demo/**`, `CLAUDE.md`, `config/outlets.yaml`, `db/**`,
`src/parallax/crawl/adapters/**`, `src/parallax/crawl/http.py`,
`src/parallax/ui/**`, `src/parallax/metrics/**`.

## Acceptance criteria

- `PARALLAX_CRAWL_OUTLETS=chinatimes,ftv` → only those two are crawled (fake
  adapters/`crawl_all` internals; no network, no DB).
- `PARALLAX_CRAWL_EXCLUDE=chinatimes,ftv` → the other verified outlets, and an
  unverified outlet is still skipped as before.
- Both set → return 2 with an ERROR naming both vars; nothing crawled.
- Unknown code in either → return 2 naming it; nothing crawled.
- Unverified code in `PARALLAX_CRAWL_OUTLETS` → `UnverifiedOutletError`.
- `--outlet cna` with `PARALLAX_CRAWL_OUTLETS=ftv` set → crawls cna only.
- `--dry-run` honours the selection.
- The INFO line names the count and the source var.
- Heartbeat still pings on a subset scheduled run (extend one T-033 test).
- `test_worst_case_crawl_cycle_fits_the_launchd_interval` still passes
  unchanged (a subset is never slower than all outlets).
- Ticket file moved to `.agents/tickets/done/`.

## Verify

```bash
uv run pytest -q tests/test_crawl_subset.py tests/test_crawl_health.py tests/test_heartbeat.py && uv run ruff check . && uv run pytest -q
```

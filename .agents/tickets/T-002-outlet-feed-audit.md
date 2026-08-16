# T-002 — Outlet feed audit

## Goal

Resolve, for each of the 8 outlets, whether it has a usable RSS feed, whether
that feed carries publish dates, and whether `robots.txt` permits our bot. Record
the answers in `config/outlets.yaml` so T-003 can crawl against verified config
instead of guessed URLs.

## Files in scope

- `scripts/audit_feeds.py` (written)
- `config/outlets.yaml`

## Do not touch

- `src/parallax/crawl/**` — that is T-003
- `db/schema.sql`

## Method

`make audit` probes every `feed_candidates` entry, then prints a report and a
YAML fragment. Update `outlets.yaml` by hand from that fragment (the script does
not rewrite the file, so its comments survive).

## Acceptance criteria

- Every outlet has `parser`, `robots_ok`, `has_dates` and `verified` set.
- Outlets with no usable feed are marked `parser: html` and carry a
  `listing_url` + selectors, verified against a saved fixture.
- Any outlet whose `robots.txt` disallows our path is either dropped and replaced
  **this week** (changing the list later resets the daily-totals baseline) or
  escalated to the human. Do not crawl a disallowed path.
- `load_outlets()` raises `UnverifiedOutletError` for anything left unverified —
  confirm this actually fires.

## Verify

```bash
make audit
uv run python -c "from parallax.config import load_outlets; print(len(load_outlets()[1]), 'outlets verified')"
```

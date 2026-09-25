# T-026 — ftv is unreachable: a malformed root in certifi, not a broken selector

**Owner:** claude — it is a change to what the crawler trusts, which is a
security decision and not a mechanical fix. **Blocked by:** nothing.

## Why

The first crawl on the rebuilt Mini took seven of eight outlets. `ftv` failed
every attempt:

```
SSLError: certificate verify failed: Missing Subject Key Identifier
```

Invariant 2 says a failing outlet must not abort the crawl, and it did not --
the other seven recorded normally and `ftv` recorded its error. But invariant 1
says tier-1 loss is permanent, and an outlet that fails *every* cycle is a
standing 12.5% hole in the Q2 denominator that no backfill can repair.

**It is not a broken selector and not ftv's fault.** All three certificates
ftvnews.com.tw sends carry a Subject Key Identifier. The defect is in
`certifi` 2026.07.22, which ships the `TWCA Global Root CA` **trust anchor**
without one. RFC 5280 requires an SKI on a CA certificate and OpenSSL 3.5
(bundled with Python 3.14) enforces it, so every path through that anchor is
rejected. It is the only root of the 121 in the bundle with this defect, and it
happens to sit in ftv's chain.

`curl` reaches the site (403 on user-agent, so TLS itself succeeded) because
macOS verifies through its own store. Python does not.

## Design — decided

**Remove that one anchor from the bundle the crawler verifies against.** The
same chain then validates against `TWCA Root Certification Authority`, which is
also in certifi, does carry an SKI, and is the issuer the server already sends
the cross-signed intermediate for.

This is **not** a relaxation of TLS. Verification stays on, every chain must
still reach a trusted root, and `verify=False` appears nowhere. The bundle loses
an anchor OpenSSL was refusing to use anyway.

1. **Pinned by SHA-256 of the DER, never by subject.** A root can only be
   removed if its bytes are exactly the ones that were inspected. When certifi
   ships a conforming copy the fingerprint stops matching, the filter becomes a
   no-op and nobody has to remember to revert this.
2. **Fails open to stock certifi.** If the filtered bundle cannot be built for
   any reason, the crawler uses certifi untouched: verifying against the stock
   bundle loses one outlet, while failing to start loses all eight.
3. **The bundle is a derived cache** at `config/ca-bundle.pem`, gitignored and
   rewritten whenever certifi changes. Nothing about trust is committed except
   the fingerprint and the reason.

## Files in scope

`src/parallax/crawl/tls.py` (new), `src/parallax/crawl/http.py` (one line:
`session.verify`), `.gitignore`, `tests/test_tls.py`.

## Do not touch

The adapters, `config/outlets.yaml`, the retry and rate-limit logic in
`http.py`. This ticket changes which roots are trusted and nothing else.

## Acceptance criteria

- Only the pinned fingerprint is ever dropped; a near-miss survives.
- The pin matches at most one root in the real certifi bundle.
- Trust setup failing falls back to certifi rather than stopping the crawl.
- The session still verifies — `verify` is a bundle path, never `False`.
- `make crawl.one OUTLET=ftv` succeeds.

## Verify

```bash
uv run pytest -q tests/test_tls.py && uv run ruff check . && make crawl.one OUTLET=ftv
```

## Shipped

Verified on the Mini: `make crawl.one OUTLET=ftv` → `seen=34 new=34`, restoring
the eighth outlet. 408 tests pass, ruff clean.

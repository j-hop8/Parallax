"""The CA bundle the crawler verifies against: certifi, minus known-broken roots.

Not a relaxation of TLS. Verification stays fully on and every chain must still
reach a trusted root; this only removes anchors that OpenSSL refuses to use, so
that a *different*, conforming root in the same bundle can anchor the path
instead.

Why it exists: certifi 2026.07.22 ships `TWCA Global Root CA` without an
X509v3 Subject Key Identifier. RFC 5280 requires one on a CA certificate and
OpenSSL 3.5 enforces it, so every path through that anchor fails with
"Missing Subject Key Identifier". ftvnews.com.tw chains through it, which took
one of the eight outlets off the air entirely -- a permanent 12.5% hole in the
Q2 denominator, and invariant 1 says that loss is unrecoverable.

Dropping that one anchor lets the same chain validate against `TWCA Root
Certification Authority`, which is also in certifi and does carry an SKI. The
server already sends the cross-signed intermediate needed to get there.

Pinned by fingerprint rather than by subject so this can only ever remove the
exact bytes that were inspected. When certifi ships a conforming copy the
fingerprint stops matching, the filter becomes a no-op, and nothing here needs
touching.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from functools import lru_cache
from pathlib import Path

import certifi

from ..settings import ROOT

log = logging.getLogger(__name__)

# SHA-256 of the DER body. Verified 2026-09-25 against certifi 2026.07.22:
# C=TW, O=TAIWAN-CA, OU=Root CA, CN=TWCA Global Root CA -- no SKI, expires 2030.
BROKEN_ROOTS: frozenset[str] = frozenset(
    {"59769007f7685d0fcd50872f9f95d5755a5b2b457d81f3692b610a98672f0e1b"}
)

_PEM = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL)
_CACHE = ROOT / "config" / "ca-bundle.pem"


def _fingerprint(pem: str) -> str:
    body = "".join(line for line in pem.splitlines() if "-----" not in line)
    return hashlib.sha256(base64.b64decode(body)).hexdigest()


def filter_bundle(source: str) -> tuple[str, int]:
    """Return (bundle, dropped). Pure, so the filtering itself is testable."""
    kept, dropped = [], 0
    for pem in _PEM.findall(source):
        if _fingerprint(pem) in BROKEN_ROOTS:
            dropped += 1
            continue
        kept.append(pem)
    return "\n".join(kept) + "\n", dropped


@lru_cache(maxsize=1)
def ca_bundle() -> str:
    """Path to the bundle `requests` should verify against.

    Falls back to certifi untouched if anything goes wrong: a crawl that
    verifies against the stock bundle loses one outlet, while a crawl that
    cannot start loses all eight.
    """
    try:
        bundle, dropped = filter_bundle(Path(certifi.where()).read_text(encoding="utf-8"))
        if not dropped:
            return certifi.where()  # nothing to strip; use certifi directly
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        if not _CACHE.exists() or _CACHE.read_text(encoding="utf-8") != bundle:
            _CACHE.write_text(bundle, encoding="utf-8")
            log.info("CA bundle: dropped %d broken root(s) -> %s", dropped, _CACHE)
        return str(_CACHE)
    except Exception as exc:  # noqa: BLE001 -- never let trust setup stop the crawl
        log.warning("could not build the filtered CA bundle (%s); using certifi", exc)
        return certifi.where()

"""The crawler's CA bundle (T-026).

This is trust configuration, so the tests are about what it must never do:
weaken verification, drop a root it did not inspect, or fail closed and take
the whole crawl down.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import certifi

from parallax.crawl import tls


def _pem(payload: bytes) -> str:
    body = base64.b64encode(payload).decode()
    return "-----BEGIN CERTIFICATE-----\n" + body + "\n-----END CERTIFICATE-----"


GOOD_A, GOOD_B, BAD = _pem(b"good-a"), _pem(b"good-b"), _pem(b"broken-root")
BAD_FP = hashlib.sha256(b"broken-root").hexdigest()


def test_only_the_pinned_fingerprint_is_dropped(monkeypatch):
    monkeypatch.setattr(tls, "BROKEN_ROOTS", frozenset({BAD_FP}))
    bundle, dropped = tls.filter_bundle(f"{GOOD_A}\n{BAD}\n{GOOD_B}")
    assert dropped == 1
    assert GOOD_A in bundle and GOOD_B in bundle
    assert BAD not in bundle


def test_a_bundle_with_nothing_pinned_is_returned_whole(monkeypatch):
    monkeypatch.setattr(tls, "BROKEN_ROOTS", frozenset())
    source = f"{GOOD_A}\n{GOOD_B}"
    bundle, dropped = tls.filter_bundle(source)
    assert dropped == 0
    assert GOOD_A in bundle and GOOD_B in bundle


def test_the_pin_matches_der_bytes_not_the_subject(monkeypatch):
    """Fingerprinting the decoded DER is what makes this auditable: a root can
    only be removed if its bytes are exactly the ones that were inspected."""
    monkeypatch.setattr(tls, "BROKEN_ROOTS", frozenset({BAD_FP}))
    _, dropped = tls.filter_bundle(_pem(b"broken-root-but-different"))
    assert dropped == 0, "a near-miss must survive"


def test_the_real_pin_matches_exactly_one_root_in_certifi():
    """If certifi ships a conforming copy this drops to zero and the filter
    becomes a no-op -- which is the intended end state, not a failure."""
    source = Path(certifi.where()).read_text(encoding="utf-8")
    _, dropped = tls.filter_bundle(source)
    assert dropped <= 1, "the pin must never match more than the one root it names"


def test_trust_setup_never_takes_the_crawl_down(monkeypatch, tmp_path):
    """A crawl verifying against stock certifi loses one outlet; a crawl that
    cannot start loses all eight. This must fail open, to certifi."""

    def boom(*a, **k):
        raise OSError("unreadable")

    monkeypatch.setattr(tls.Path, "read_text", boom)
    tls.ca_bundle.cache_clear()
    assert tls.ca_bundle() == certifi.where()
    tls.ca_bundle.cache_clear()


def test_the_bundle_is_a_real_readable_file_of_certificates():
    tls.ca_bundle.cache_clear()
    path = tls.ca_bundle()
    text = Path(path).read_text(encoding="utf-8")
    assert text.count("BEGIN CERTIFICATE") > 100, "a truncated trust store would be worse than none"
    assert "PRIVATE KEY" not in text
    tls.ca_bundle.cache_clear()


def test_the_session_verifies_against_that_bundle():
    """The whole point: requests must still verify, just against our bundle."""
    from parallax.crawl.http import Fetcher

    f = Fetcher(user_agent="test")
    assert f._session.verify == tls.ca_bundle()
    assert f._session.verify is not False, "verification must never be disabled"

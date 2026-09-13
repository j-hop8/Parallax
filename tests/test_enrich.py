"""Tier-2 enrich accounting.

The stats enrich reports are how an operator judges whether a keyword's
articles were actually fetched. Each article must resolve to exactly one
outcome, so fetched + cached + failed can never exceed matched -- a double
count overstates how much work succeeded, and it has happened twice: once by
counting at fetch time, before extraction could fail, and once by counting
before the commit, which could fail too.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import pytest

from parallax.jobs import enrich as mod

SEEN = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


class _Conn:
    def __init__(self):
        self.fail_commits = 0
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        if self.fail_commits > 0:
            self.fail_commits -= 1
            raise RuntimeError("commit failed")
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _row(i: int) -> dict:
    return {
        "id": i,
        "outlet": "cna",
        "url_original": f"https://example.com/{i}",
        "url_canonical": f"https://example.com/{i}",
        "seen_at": SEEN,
        "published_at": None,
    }


@pytest.fixture
def conn(monkeypatch):
    """Stub every collaborator so enrich_keyword runs with no network or DB."""
    conn = _Conn()
    monkeypatch.setattr(mod, "load_outlets", lambda: ({}, []))
    monkeypatch.setattr(mod, "_build_fetcher", lambda defaults: object())
    monkeypatch.setattr(mod.db, "connect", lambda: contextlib.nullcontext(conn))
    monkeypatch.setattr(mod.db, "save_enriched", lambda *a, **k: None)
    monkeypatch.setattr(mod.db, "backfill_published_at", lambda *a, **k: None)
    monkeypatch.setattr(mod.db, "mark_enrich_failed", lambda *a, **k: None)
    monkeypatch.setattr(mod, "fetch_html", lambda *a, **k: ("<html/>", "/cache/x", False))
    monkeypatch.setattr(mod, "extract_body", lambda html: "x" * 500)
    monkeypatch.setattr(mod, "extract_published_at", lambda *a, **k: None)
    monkeypatch.setattr(mod, "segment_text", lambda s: s)
    return conn


def _run(monkeypatch, rows):
    monkeypatch.setattr(mod, "find_articles", lambda conn, keyword, limit: rows)
    return mod.enrich_keyword("kw")


def test_each_article_resolves_to_exactly_one_outcome(monkeypatch, conn):
    stats = _run(monkeypatch, [_row(1), _row(2), _row(3)])
    assert stats["matched"] == 3
    assert stats["fetched"] + stats["cached"] + stats["failed"] == 3
    assert conn.commits == 3


def test_extraction_failure_is_not_also_counted_as_fetched(monkeypatch, conn):
    monkeypatch.setattr(mod, "extract_body", lambda html: "")
    stats = _run(monkeypatch, [_row(1)])
    assert stats == {"matched": 1, "fetched": 0, "cached": 0, "dated": 0, "failed": 1}


def test_commit_failure_is_not_also_counted_as_fetched(monkeypatch, conn):
    """Counting before the commit left the double count open one step later.

    The article's own commit fails; the failure-marking commit afterwards
    succeeds. Nothing about the article persisted, so neither fetched nor dated
    may be credited -- only failed.
    """
    conn.fail_commits = 1
    monkeypatch.setattr(mod, "extract_published_at", lambda *a, **k: SEEN)
    stats = _run(monkeypatch, [_row(1)])
    assert stats == {"matched": 1, "fetched": 0, "cached": 0, "dated": 0, "failed": 1}
    assert conn.rollbacks == 1

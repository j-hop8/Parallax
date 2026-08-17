"""Raw HTML cache tests.

The cache exists so a body is fetched from an outlet exactly once. Every parser
fix in this project re-runs over articles already fetched, and re-requesting
thousands of pages from eight news sites to correct our own selector bug would be
both slow and rude. These tests pin that guarantee.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime

import pytest

from parallax.crawl import body as mod


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "RAW_DIR", tmp_path)
    return tmp_path


DAY = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


class _Fetcher:
    def __init__(self, html="<html><body>fresh</body></html>"):
        self.html = html
        self.calls = 0

    def get_text(self, url):
        self.calls += 1
        return self.html


def test_cache_path_is_stable_and_keyed_on_canonical_url(raw_dir):
    a = mod.cache_path("udn", "https://udn.com/news/story/1/2", DAY)
    b = mod.cache_path("udn", "https://udn.com/news/story/1/2", DAY)
    c = mod.cache_path("udn", "https://udn.com/news/story/1/3", DAY)

    assert a == b, "same article must resolve to the same file across runs"
    assert a != c
    # Foldered by the article's own day so the path is derivable from the DB row
    # alone -- no stored path needed to find it again after a crash.
    assert a.parent.name == "2026-08-17"
    assert a.parent.parent.name == "udn"


def test_round_trip(raw_dir):
    path = mod.cache_path("cna", "https://cna.com.tw/news/1", DAY)
    mod.write_cache(path, "<html>內容</html>")
    assert mod.read_cached(path) == "<html>內容</html>"
    # Stored gzipped, not plain.
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        assert fh.read() == "<html>內容</html>"


def test_a_truncated_cache_entry_is_a_miss_not_a_crash(raw_dir):
    """Interrupted runs are routine on this host.

    A half-written .gz must not be indistinguishable from a good one, and must
    not fail the whole enrich batch either.
    """
    path = mod.cache_path("setn", "https://setn.com/news/1", DAY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x1f\x8b\x08 truncated garbage")

    assert mod.read_cached(path) is None

    # And the fetcher is used to recover, rather than the error propagating.
    fetcher = _Fetcher()
    html, _, from_cache = mod.fetch_html(
        fetcher, "setn", "https://setn.com/news/1", "https://setn.com/news/1", DAY
    )
    assert from_cache is False
    assert "fresh" in html
    assert fetcher.calls == 1


def test_write_is_atomic_leaving_no_tmp_behind(raw_dir):
    path = mod.cache_path("ltn", "https://ltn.com.tw/news/1", DAY)
    mod.write_cache(path, "<html>x</html>")
    assert list(path.parent.glob("*.tmp")) == []


def test_second_fetch_never_hits_the_outlet(raw_dir):
    """The cost-and-courtesy guarantee: one fetch per article, ever."""
    fetcher = _Fetcher()
    args = ("ettoday", "https://ettoday.net/news/1", "https://ettoday.net/news/1", DAY)

    html1, path1, cached1 = mod.fetch_html(fetcher, *args)
    html2, path2, cached2 = mod.fetch_html(fetcher, *args)

    assert (cached1, cached2) == (False, True)
    assert fetcher.calls == 1, "second call must be served from disk"
    assert html1 == html2
    assert path1 == path2


def test_refetch_bypasses_the_cache_deliberately(raw_dir):
    fetcher = _Fetcher()
    args = ("tvbs", "https://news.tvbs.com.tw/news/1", "https://news.tvbs.com.tw/news/1", DAY)

    mod.fetch_html(fetcher, *args)
    _, _, from_cache = mod.fetch_html(fetcher, *args, refetch=True)

    assert from_cache is False
    assert fetcher.calls == 2

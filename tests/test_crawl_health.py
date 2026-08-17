"""Regression tests for the ways a crawl can look healthy while losing data.

Every case here was a real bug caught in review, and each shipped precisely
because nothing asserted on it. They share one shape: the crawl completes, the
run is recorded ok, and articles are silently missing -- which then lets the
rollup mark the day complete and use a short count as a coverage denominator.
"""

from __future__ import annotations

import pytest

from parallax.config import UnverifiedOutletError
from parallax.crawl.adapters.base import PartialFetchError, RSSAdapter
from parallax.crawl.listing import _check_explicit_target
from parallax.models import ArticleStub, OutletConfig


def _outlet(code="x", n_feeds=3, verified=True) -> OutletConfig:
    return OutletConfig(
        code=code,
        name_zh=code,
        home_url="https://example.com/",
        feed_urls=tuple(f"https://example.com/feed{i}.xml" for i in range(n_feeds)),
        parser="rss",
        rate_limit_seconds=0,
        verified=verified,
    )


class _Fetcher:
    """Serves one working feed and fails the rest, mimicking a partial outage."""

    def __init__(self, working: set[int]):
        self.working = working

    def get(self, url: str):
        index = int(url.rstrip(".xml")[-1])
        if index not in self.working:
            raise ConnectionError(f"feed {index} down")

        class _Resp:
            content = b"""<?xml version="1.0"?><rss version="2.0"><channel>
              <item><title>Real headline</title><link>https://example.com/a1</link>
              <pubDate>Sat, 16 Aug 2026 09:00:00 +0800</pubDate></item>
            </channel></rss>"""

        return _Resp()


def test_partial_feed_failure_is_not_a_healthy_run():
    """1 of 3 sections working must not be recorded as a good crawl.

    中央社 is polled across 12 section feeds. If 11 fail, the survivor still
    returns articles; calling that ok would let a day missing ~90% of its output
    stand as a denominator.
    """
    adapter = RSSAdapter(_outlet(n_feeds=3), _Fetcher(working={0}))
    with pytest.raises(PartialFetchError) as excinfo:
        adapter.fetch()

    # The articles that did arrive must survive -- they cannot be re-fetched.
    assert len(excinfo.value.stubs) == 1
    assert excinfo.value.stubs[0].title == "Real headline"
    assert len(excinfo.value.errors) == 2


def test_all_feeds_working_returns_cleanly():
    stubs = RSSAdapter(_outlet(n_feeds=3), _Fetcher(working={0, 1, 2})).fetch()
    # One article, deduplicated across the three identical feeds.
    assert len(stubs) == 1
    assert isinstance(stubs[0], ArticleStub)


def test_naming_an_unverified_outlet_raises_rather_than_skipping():
    """A silent skip looks identical to a successful crawl of that outlet."""
    outlets = [_outlet("cna"), _outlet("setn", verified=False)]

    with pytest.raises(UnverifiedOutletError, match="setn"):
        _check_explicit_target(outlets, "setn")

    with pytest.raises(ValueError, match="unknown outlet"):
        _check_explicit_target(outlets, "nosuchoutlet")

    # The bulk crawl must still be allowed to run and skip it.
    _check_explicit_target(outlets, None)
    _check_explicit_target(outlets, "cna")

"""Adapter tests run against saved fixtures, never the live network.

A test that fetches the real listing passes for the wrong reason on a good day
and fails for an unrelated reason on a bad one. Worse, it cannot detect the
failure that actually matters here: a redesign that makes the parser match zero
articles. Pinning known HTML means a selector regression is a red test, not a
silently shrinking denominator.

Refresh with scripts/fetch_fixtures.py when an outlet redesigns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from parallax.config import load_outlets
from parallax.crawl.adapters.html_listing import PatternListingAdapter
from parallax.crawl.adapters.tvbs import TVBSAdapter

FIXTURES = Path(__file__).parent / "fixtures"

# Floor, not a target: these listings carry far more than this. A parser that
# silently degrades to a handful of links should fail here.
MIN_ARTICLES = 10


def _config(code: str):
    _, outlets = load_outlets()
    return next(o for o in outlets if o.code == code)


def _fixture(code: str) -> str:
    path = FIXTURES / f"{code}_listing.html"
    if not path.exists():
        pytest.skip(f"missing fixture {path.name}")
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("code", ["udn", "chinatimes", "setn", "ftv"])
def test_pattern_adapter_extracts_articles(code: str):
    stubs = PatternListingAdapter(_config(code), fetcher=None).parse(_fixture(code))

    assert len(stubs) >= MIN_ARTICLES, f"{code}: only {len(stubs)} articles"
    assert all(s.title.strip() for s in stubs), f"{code}: some titles empty"
    assert all(s.url_original.startswith("http") for s in stubs), f"{code}: relative URL leaked"
    # Deduplication: the same article appears as both an image and a headline link.
    assert len({s.url_original for s in stubs}) == len(stubs), f"{code}: duplicate URLs"


def test_udn_strips_the_time_appended_to_headlines():
    stubs = PatternListingAdapter(_config("udn"), fetcher=None).parse(_fixture("udn"))
    # UDN renders "...第一島鏈投射戰力」14:15"; leaving that in pollutes both the
    # search index and the token set SimHash compares.
    offenders = [s.title for s in stubs if __import__("re").search(r"\d{1,2}:\d{2}$", s.title)]
    assert not offenders, f"trailing time left in {len(offenders)} titles: {offenders[:3]}"


def test_ftv_recovers_timestamps_from_the_listing():
    stubs = PatternListingAdapter(_config("ftv"), fetcher=None).parse(_fixture("ftv"))
    dated = [s for s in stubs if s.published_at]
    assert len(dated) >= MIN_ARTICLES, f"only {len(dated)} FTV articles carried a timestamp"
    # The datetime prefix must be consumed, not left glued to the headline.
    assert not any(s.title.startswith("20") and "/" in s.title[:11] for s in dated)


def test_tvbs_decodes_articles_from_the_astro_island():
    stubs = TVBSAdapter(_config("tvbs"), fetcher=None).parse(_fixture("tvbs"))
    assert len(stubs) >= MIN_ARTICLES, f"only {len(stubs)} TVBS articles"
    assert all(s.title.strip() for s in stubs)

    # TVBS is the only non-RSS outlet with real publish times; that is what makes
    # it usable in a propagation chain, so the parse must preserve them.
    dated = [s for s in stubs if s.published_at]
    assert len(dated) == len(stubs), "TVBS timestamps lost"
    assert all(s.published_at.tzinfo is not None for s in dated), "naive datetime"
    assert all(
        datetime(2020, 1, 1, tzinfo=UTC) < s.published_at < datetime(2100, 1, 1, tzinfo=UTC)
        for s in dated
    ), "timestamp outside a sane range -- unit confusion (ms vs s)?"


def test_a_redesign_yields_zero_rather_than_garbage():
    """A pattern that no longer matches must return nothing, not partial junk."""
    stubs = PatternListingAdapter(_config("setn"), fetcher=None).parse(
        "<html><body><a href='/totally/different/9'>x</a></body></html>"
    )
    assert stubs == []

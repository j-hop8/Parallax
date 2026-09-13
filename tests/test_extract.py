"""Extractor tests against saved article pages, one per outlet.

Both bugs pinned here were found on live pages and would have been invisible in
the data: one produced empty bodies, the other timestamps eight hours in the
future. Neither raises an exception, so only an assertion catches them.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from parallax.config import load_outlets
from parallax.crawl.extract import extract_body, extract_headline, extract_published_at

FIXTURES = Path(__file__).parent / "fixtures" / "articles"
OUTLETS = ["cna", "ltn", "ettoday", "udn", "chinatimes", "setn", "tvbs", "ftv"]


def _meta() -> dict:
    path = FIXTURES / "meta.json"
    if not path.exists():
        pytest.skip("article fixtures not fetched")
    return json.loads(path.read_text(encoding="utf-8"))


def _html(code: str) -> str:
    path = FIXTURES / f"{code}_article.html"
    if not path.exists():
        pytest.skip(f"missing fixture {path.name}")
    return path.read_text(encoding="utf-8")


def _config(code: str):
    _, outlets = load_outlets()
    return next(o for o in outlets if o.code == code)


@pytest.mark.parametrize("code", OUTLETS)
def test_every_outlet_yields_a_timestamp(code: str):
    """All eight must resolve a publish time.

    Four of them supply none in their listing, so this is the only thing that
    makes them orderable in a propagation chain -- which is Q3, the whole point.
    """
    seen_at = dateparser.parse(_meta()[code]["seen_at"])
    published = extract_published_at(
        _html(code),
        seen_at=seen_at,
        offset_is_wrong=_config(code).timestamp_offset_is_wrong,
    )
    assert published is not None, f"{code}: no timestamp recovered"
    assert published.tzinfo is not None, f"{code}: naive datetime"


@pytest.mark.parametrize("code", OUTLETS)
def test_no_outlet_claims_to_publish_after_we_crawled_it(code: str):
    seen_at = dateparser.parse(_meta()[code]["seen_at"])
    published = extract_published_at(
        _html(code),
        seen_at=seen_at,
        offset_is_wrong=_config(code).timestamp_offset_is_wrong,
    )
    # Both are timezone-aware, so they compare directly across zones. Calling
    # .replace(tzinfo=...) here would reinterpret the UTC wall-clock as Taipei
    # and shift seen_at eight hours -- the very error being tested for.
    #
    # 三立 stamps Taipei wall-clock as +00:00; taken at face value its articles
    # publish 8 hours after we fetched them, which would reverse cluster order.
    assert published <= seen_at + timedelta(hours=2), (
        f"{code}: published_at {published} is after seen_at {seen_at}"
    )


def test_setn_offset_is_reinterpreted_as_taipei():
    """Guard the quirk explicitly, so removing the flag fails loudly."""
    html = _html("setn")
    seen_at = dateparser.parse(_meta()["setn"]["seen_at"])
    naive = extract_published_at(html, seen_at=None, offset_is_wrong=False)
    fixed = extract_published_at(html, seen_at=seen_at, offset_is_wrong=True)
    assert naive is not None and fixed is not None
    # Same wall-clock reading, different instant: exactly the 8-hour Taipei offset.
    assert (naive - fixed).total_seconds() == pytest.approx(8 * 3600, abs=1)


@pytest.mark.parametrize("code", OUTLETS)
def test_body_extraction_returns_real_prose(code: str):
    body = extract_body(_html(code))
    # 民視 wraps its entire page in one ASP.NET <form runat="server">. Stripping
    # chrome before locating the article deleted the article too and returned "".
    assert len(body) > 200, f"{code}: body only {len(body)} chars"
    assert any("一" <= ch <= "鿿" for ch in body), f"{code}: no CJK in body"


def test_body_excludes_script_and_style_content():
    body = extract_body(
        "<html><body><article><script>var x=1;</script>"
        "<style>.a{color:red}</style><p>實際內容段落文字</p></article></body></html>"
    )
    assert "var x" not in body and "color:red" not in body
    assert "實際內容段落文字" in body


def test_missing_timestamp_returns_none_rather_than_now():
    """No timestamp must stay absent so effective_at can fall back to seen_at."""
    assert extract_published_at("<html><body><p>x</p></body></html>") is None


def test_naive_timestamp_is_read_as_taipei_not_utc():
    html = '<html><head><meta property="article:published_time" content="2026-08-16 09:00:00">'
    parsed = extract_published_at(html)
    assert parsed is not None
    assert parsed.utcoffset().total_seconds() == 8 * 3600, "bare clock must be Taipei, not UTC"
    assert parsed.hour == 9


def test_datetime_type_is_returned():
    assert isinstance(extract_published_at(_html("cna")), datetime)


# ---- headline ---------------------------------------------------------------


@pytest.mark.parametrize("code", OUTLETS)
def test_headline_is_the_displayed_h1_not_a_logo_or_a_lede(code: str):
    """T-003c: the page's own headline, for the rows T-007 classifies.

    Every outlet's article page carries the displayed headline in an <h1>; the
    page <title> is that headline plus a site suffix, which makes "substring of
    <title>" a fixture-independent check that we picked the right element.
    """
    html = _html(code)
    headline = extract_headline(html)
    assert headline, f"{code}: no headline"
    assert len(headline) <= 60, f"{code}: {len(headline)} chars looks like a lede: {headline!r}"
    page_title = BeautifulSoup(html, "lxml").title.get_text(" ", strip=True)
    assert headline in page_title, f"{code}: {headline!r} not in <title> {page_title!r}"


def test_ettoday_headline_skips_the_logo_h1():
    assert extract_headline(_html("ettoday")) != "探索LOGO"
    assert "游泳" in extract_headline(_html("ettoday"))


def test_headline_falls_back_to_og_title_then_title_with_site_suffix_removed():
    og_only = '<html><head><meta property="og:title" content="標題在這裡 | 聯合新聞網"></head><body></body></html>'
    assert extract_headline(og_only) == "標題在這裡"
    title_only = "<html><head><title>標題在這裡 | 政治 | 要聞 | 聯合新聞網</title></head><body></body></html>"
    assert extract_headline(title_only) == "標題在這裡"
    dash = "<html><head><title>標題在這裡 - 民視新聞網</title></head><body></body></html>"
    assert extract_headline(dash) == "標題在這裡"


def test_headline_returns_none_rather_than_guessing():
    assert extract_headline("<html><body><p>no headline anywhere</p></body></html>") is None
    assert extract_headline("") is None

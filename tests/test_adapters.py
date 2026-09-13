"""Adapter tests run against saved fixtures, never the live network.

A test that fetches the real listing passes for the wrong reason on a good day
and fails for an unrelated reason on a bad one. Worse, it cannot detect the
failure that actually matters here: a redesign that makes the parser match zero
articles. Pinning known HTML means a selector regression is a red test, not a
silently shrinking denominator.

Refresh with scripts/fetch_fixtures.py when an outlet redesigns.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from parallax.config import load_outlets
from parallax.crawl.adapters.html_listing import PatternListingAdapter
from parallax.crawl.adapters.tvbs import TVBSAdapter

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED = FIXTURES / "expected"

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


# Headlines that appear verbatim in the saved fixtures. Pinned so the tie-break
# between anchors sharing a URL cannot drift: for udn/ftv these are what the old
# "longest text wins" rule replaced with the lede; for setn/chinatimes they are
# what it already got right and must keep getting right.
KNOWN_HEADLINES = {
    "udn": "韓美明大型軍演觸及台灣？美駐韓第8軍團首提「第一島鏈投射戰力」",
    "ftv": "美職聯／多倫多FC 2比1擊退新英格蘭革命\u3000終結13場不勝",
    "setn": "淺堤圓山12場寫新紀錄！12組夢幻嘉賓曝光",
    "chinatimes": "神祕扣款全是「4078元」！ 詐團鎖定學生盜買PS高級會員",
}

# Chinese headlines run 15-40 characters. Anything past this is a lede or a
# headline with the lede glued on, which is exactly what T-003b found stored as
# the title for 73-99% of udn and ftv rows.
MAX_HEADLINE_CHARS = 60


@pytest.mark.parametrize("code", ["udn", "chinatimes", "setn", "ftv"])
def test_pattern_adapter_picks_the_headline_not_the_lede(code: str):
    stubs = PatternListingAdapter(_config(code), fetcher=None).parse(_fixture(code))
    titles = [s.title for s in stubs]

    assert KNOWN_HEADLINES[code] in titles, f"{code}: known headline not extracted verbatim"
    too_long = [t for t in titles if len(t) > MAX_HEADLINE_CHARS]
    assert not too_long, f"{code}: {len(too_long)} titles look like ledes, e.g. {too_long[0]!r}"


@pytest.mark.parametrize("code", ["udn", "chinatimes", "setn", "ftv"])
def test_pattern_adapter_output_matches_golden(code: str):
    """The complete parse of each fixture, compared exactly.

    T-003b changed the tie-break between anchors sharing a URL. A rule change
    like that must be provably inert on the outlets it was not aimed at: setn
    and chinatimes were byte-identical before and after, and this is what keeps
    them so. For udn and ftv the golden is the corrected output, reviewed once.

    When a fixture is refreshed (scripts/fetch_fixtures.py), regenerate with
    PARALLAX_UPDATE_GOLDEN=1 and review the diff of the JSON like any other
    change -- a shrinking file is the redesign these tests exist to catch.
    """
    stubs = PatternListingAdapter(_config(code), fetcher=None).parse(_fixture(code))
    actual = [
        {
            "url": s.url_original,
            "title": s.title,
            "published_at": s.published_at.isoformat() if s.published_at else None,
        }
        for s in stubs
    ]

    golden = EXPECTED / f"{code}_listing.json"
    if os.environ.get("PARALLAX_UPDATE_GOLDEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(json.dumps(actual, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if not golden.exists():
        pytest.fail(
            f"missing {golden.relative_to(FIXTURES.parent)}; run with PARALLAX_UPDATE_GOLDEN=1"
        )

    expected = json.loads(golden.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"{code}: parse differs from golden ({len(actual)} vs {len(expected)} rows)"
    )


def test_heading_beats_length_when_anchors_share_a_url():
    """The two shapes that defeated "longest text wins", plus the fallback.

    ftv: one anchor wraps time + <h2> + summary. udn: separate anchors, the
    headline's inside an <h2>, the summary's inside a <p>. A page with no
    headings at all must behave exactly as before -- longest text wins.
    """
    html = """
    <a href="/news/story/1/111"><div>2026/08/16 14:12:40</div>
       <h2>ftv shape headline</h2><div>a much longer summary paragraph than the headline</div></a>

    <h2><a href="/news/story/1/222">udn shape headline</a></h2>
    <p><a href="/news/story/1/222">a much longer summary paragraph than the headline</a></p>

    <a href="/news/story/1/333">short</a>
    <a href="/news/story/1/333">no headings anywhere so longest still wins</a>
    """
    stubs = PatternListingAdapter(_config("udn"), fetcher=None).parse(html)
    by_url = {s.url_original.rsplit("/", 1)[-1]: s.title for s in stubs}

    assert by_url["111"] == "ftv shape headline"
    assert by_url["222"] == "udn shape headline"
    assert by_url["333"] == "no headings anywhere so longest still wins"


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

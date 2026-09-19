from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from ..settings import TIMEZONE

log = logging.getLogger(__name__)

TAIPEI = ZoneInfo(TIMEZONE)

# The check is deliberately one-sided. An article genuinely older than the crawl
# is ordinary -- listings carry pieces from days back, and their real publish
# date is exactly what we want to record. An article published *after* we saw it
# is impossible, and is the signature of the timezone bug we actually hit: a
# Taipei wall-clock labelled +00:00 lands 8 hours in the future. The tolerance
# only absorbs modest clock skew between us and the outlet.
_MAX_FUTURE = timedelta(hours=2)


def extract_published_at(
    html: str,
    *,
    seen_at: datetime | None = None,
    offset_is_wrong: bool = False,
) -> datetime | None:
    """Recover an article's publish time from the page itself.

    Four of the eight outlets publish no timestamp in their listing, which caps
    ordering precision at the poll interval and makes them unusable in a
    propagation chain. Every one of them does expose a real timestamp on the
    article page, so this is what makes Q3 answerable for them.

    Tried in descending order of trustworthiness: JSON-LD (a declared, typed
    field), then OpenGraph, then a <time> element (often only a display string).
    """
    soup = BeautifulSoup(html, "lxml")

    for value in _candidates(soup):
        parsed = _parse(value, offset_is_wrong=offset_is_wrong)
        if parsed is None:
            continue
        if seen_at and parsed > seen_at + _MAX_FUTURE:
            # Do not silently accept it: a wrong timestamp is worse than none,
            # because effective_at would file the article into the wrong day and
            # put it in the wrong place in a propagation order.
            log.warning(
                "%s claims to publish after we crawled it (seen %s) -- ignoring",
                parsed.isoformat(),
                seen_at.isoformat(),
            )
            continue
        return parsed
    return None


def _candidates(soup: BeautifulSoup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (ValueError, TypeError):
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("datePublished"):
                yield obj["datePublished"]

    for prop in ("article:published_time", "og:article:published_time"):
        tag = soup.find("meta", property=prop)
        if tag and tag.get("content"):
            yield tag["content"]

    tag = soup.find("meta", attrs={"name": "pubdate"})
    if tag and tag.get("content"):
        yield tag["content"]

    for time_tag in soup.find_all("time"):
        if time_tag.get("datetime"):
            yield time_tag["datetime"]


def _parse(value: str, *, offset_is_wrong: bool = False) -> datetime | None:
    try:
        parsed = dateparser.parse(value)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed is None:
        return None

    if offset_is_wrong:
        # 三立 stamps its JSON-LD "2026-08-16 12:54 +00:00" while the wall-clock
        # is plainly Taipei local: taken at face value that article publishes at
        # 20:54, an hour and a half AFTER we crawled it. Keep the clock, discard
        # the offset. Applied per outlet, never globally -- the other seven
        # declare correct offsets and rewriting those would break them.
        return parsed.replace(tzinfo=TAIPEI)

    if parsed.tzinfo is None:
        # A bare wall-clock time from a Taiwanese outlet is Taipei local. Reading
        # it as UTC would shift every article eight hours and reorder clusters.
        parsed = parsed.replace(tzinfo=TAIPEI)
    return parsed


def extract_body(html: str) -> str:
    """Pull the article text, dropping chrome that would pollute dedup.

    Navigation, related-article rails and boilerplate are near-identical across
    every article on a site. Left in, they inflate SimHash similarity between
    unrelated pieces from the same outlet and manufacture false copy clusters.
    """
    soup = BeautifulSoup(html, "lxml")

    # Locate the article BEFORE removing anything. Stripping chrome first looks
    # equivalent and is not: 民視 wraps its whole page in a single ASP.NET
    # <form runat="server">, so decomposing forms up front deletes the article
    # along with the navigation and yields an empty body.
    node = soup.find("article") or soup.find(attrs={"itemprop": "articleBody"})
    if node is None:
        # Fall back to the densest block, ignoring containers that are mostly
        # markup rather than prose.
        candidates = soup.find_all(["div", "section"])
        node = max(candidates, key=lambda n: len(n.get_text(strip=True)), default=None)
    if node is None:
        return ""

    # Now strip chrome, scoped to inside the article. Navigation and
    # related-article rails are near-identical across every article on a site;
    # left in, they inflate SimHash similarity between unrelated pieces and
    # manufacture false copy clusters.
    for tag in node(["script", "style", "nav", "header", "footer", "aside", "iframe", "figure"]):
        tag.decompose()

    paragraphs = [p.get_text(strip=True) for p in node.find_all("p") if not _is_link_block(p)]
    text = "\n".join(p for p in paragraphs if p)
    return text or node.get_text("\n", strip=True)


# A paragraph that is link text is navigation, not prose. The related-story
# rails that the chrome strip above cannot reach live inside the article node
# and are DIFFERENT on every page, so no per-outlet frequency rule catches
# them either: tvbs wraps each rail item's <p> in an <a>; udn fills one <p> of
# its 【全球熱話題】 box with <a>s. Measured over the cached corpus (2026-09-19):
# this rule removes 68% of tvbs "body" (the rails), udn's box and a few
# 更多鏡週刊報導 lines, and changes nothing at cna, ftv or chinatimes. A
# paragraph with one inline link is far below the bar and is kept.
_LINK_TEXT_FRACTION = 0.8


def _is_link_block(p) -> bool:
    if p.find_parent("a") is not None:
        return True
    text_len = len(p.get_text(strip=True))
    if text_len == 0:
        return False
    link_len = sum(len(a.get_text(strip=True)) for a in p.find_all("a"))
    return link_len >= _LINK_TEXT_FRACTION * text_len


# Site names outlets append to og:title / <title>. Only consulted when a page
# has no <h1>; the h1 is what the outlet displayed and needs no cleaning.
_TITLE_SUFFIX = re.compile(r"\s+[|\-–—]\s+[^|\-–—]{1,40}$")
_MAX_HEADLINE_CHARS = 120


def extract_headline(html: str) -> str | None:
    """The headline as the outlet displayed it, from the article page itself.

    Exists because the listing crawl cannot always be trusted for it: T-003b
    stored the lede as the title for udn and ftv for five weeks, and the rows
    that matter most -- the enriched ones T-007 classifies, where the headline
    is the most-weighted input -- all have their page cached.

    <h1> first: on every outlet in the fixture set it is the exact displayed
    headline. Longest wins because ettoday puts a logo in an <h1> ahead of the
    real one. og:title is second choice (ftv rewrites it for social sharing, so
    it is not always the displayed headline), <title> third; both carry a site
    suffix that is stripped. Nothing found returns None rather than a guess.
    """
    soup = BeautifulSoup(html, "lxml")

    h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    h1s = [h for h in h1s if h and len(h) <= _MAX_HEADLINE_CHARS]
    if h1s:
        return max(h1s, key=len)

    og = soup.find("meta", property="og:title")
    if og and og.get("content", "").strip():
        return _TITLE_SUFFIX.sub("", og["content"].strip()).strip() or None

    if soup.title and soup.title.get_text(strip=True):
        text = soup.title.get_text(" ", strip=True)
        # Strip repeatedly: "<headline> | 政治 | 要聞 | 聯合新聞網".
        previous = None
        while text != previous:
            previous, text = text, _TITLE_SUFFIX.sub("", text).strip()
        return text or None

    return None

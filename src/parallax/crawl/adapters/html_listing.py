from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from ...models import ArticleStub, OutletConfig
from ...settings import TIMEZONE
from ..http import Fetcher

log = logging.getLogger(__name__)

TAIPEI = ZoneInfo(TIMEZONE)


class PatternListingAdapter:
    """Listing parser driven by an article-URL pattern rather than CSS paths.

    Nested selectors like `div.news_info > div.title_pc > a.smart-link` break on
    any redesign and, worse, break *silently* -- they simply match nothing. A URL
    shape (`/news/<digits>`) is far more stable than the markup wrapped around
    it, and it also captures every article link on the page regardless of which
    of the several layouts (carousel, list, sidebar) it sits in. That matters
    here because this listing is the coverage-weight denominator: missing a
    layout means undercounting the outlet's daily output.

    The same article usually appears more than once -- once as an image link with
    no text, once as a headline link, sometimes once more as a summary link.
    Links are grouped by resolved URL; a candidate backed by a heading element
    beats one that is not, and the longest text wins within that rank. See
    _headline() for why length alone picked the lede at two outlets.
    """

    def __init__(self, config: OutletConfig, fetcher: Fetcher) -> None:
        self.code = config.code
        self.config = config
        self.fetcher = fetcher

    def fetch(self) -> list[ArticleStub]:
        cfg = self.config
        if not cfg.listing_url or not cfg.article_url_pattern:
            raise ValueError(
                f"{self.code}: PatternListingAdapter needs listing_url and article_url_pattern"
            )
        return self.parse(self.fetcher.get_text(cfg.listing_url))

    def parse(self, html: str) -> list[ArticleStub]:
        """Split out from fetch() so tests can run against saved fixtures."""
        cfg = self.config
        pattern = re.compile(cfg.article_url_pattern)
        title_dt = re.compile(cfg.title_datetime_pattern) if cfg.title_datetime_pattern else None

        soup = BeautifulSoup(html, "lxml")
        # url -> (rank, title, published). rank 1 = heading-backed, 0 = plain.
        best: dict[str, tuple[int, str, datetime | None]] = {}

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if not pattern.search(href):
                continue
            url = urljoin(cfg.listing_url, href)
            text = anchor.get_text(strip=True)
            published = None

            # The datetime is matched against the anchor's full text because at
            # ftv it sits in a sibling of the heading, not inside it.
            if text and title_dt:
                match = title_dt.match(text)
                if match:
                    published = _parse_listing_datetime(match.group(1))
                    text = text[match.end() :].strip()

            headline = _headline(anchor)
            title = headline if headline is not None else text
            rank = 1 if headline is not None else 0

            if cfg.strip_trailing_time:
                # UDN appends the publish time to the headline text ("...戰力」14:15").
                # Left in place it pollutes both the search index and the token
                # set SimHash compares.
                title = re.sub(r"\s*\d{1,2}:\d{2}$", "", title).strip()

            previous = best.get(url)
            if previous is None or (rank, len(title)) > (previous[0], len(previous[1])):
                best[url] = (rank, title, published or (previous[2] if previous else None))
            elif published and previous[2] is None:
                best[url] = (previous[0], previous[1], published)

        stubs = [
            ArticleStub(outlet=self.code, url_original=url, title=title, published_at=published)
            for url, (_rank, title, published) in best.items()
            if title
        ]
        if not stubs:
            # A pattern that matches nothing is a redesign, not a quiet hour.
            log.warning(
                "%s: listing matched 0 articles for pattern %r -- selector likely stale",
                self.code,
                cfg.article_url_pattern,
            )
        return stubs


_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def _headline(anchor) -> str | None:
    """The headline this anchor carries if the markup marks one, else None.

    "Longest text wins" stored the lede as the title at two outlets for the
    first five weeks of the crawl -- 73-99% of udn and ftv rows -- and was
    invisible until someone read the search results:

    - ftv wraps time + <h2> + summary in ONE anchor, so the anchor's text is
      the headline glued to the lede.
    - udn links each story three times: image, <h2><a>headline</a></h2> and
      <p><a>summary</a></p>. The summary is always the longest.

    A heading element is the outlet saying "this is the title". Trusting it
    over length fixes both shapes without naming a single CSS class, so a
    redesign that keeps semantic headings keeps working. Anchors with no
    heading anywhere near them fall back to the old rule unchanged.
    """
    inner = anchor.find(_HEADINGS)
    if inner is not None:
        text = inner.get_text(strip=True)
        if text:
            return text
    if anchor.find_parent(_HEADINGS) is not None:
        text = anchor.get_text(strip=True)
        if text:
            return text
    return None


def _parse_listing_datetime(text: str) -> datetime | None:
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            # Listing pages print local wall-clock time with no offset.
            return datetime.strptime(text, fmt).replace(tzinfo=TAIPEI)
        except ValueError:
            continue
    return None

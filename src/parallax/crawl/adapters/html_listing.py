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
    no text, once as a headline link. Links are grouped by resolved URL and the
    longest non-empty text wins.
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
        best: dict[str, tuple[str, datetime | None]] = {}

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if not pattern.search(href):
                continue
            url = urljoin(cfg.listing_url, href)
            title = anchor.get_text(strip=True)
            published = None

            if title and title_dt:
                match = title_dt.match(title)
                if match:
                    published = _parse_listing_datetime(match.group(1))
                    title = title[match.end() :].strip()

            if cfg.strip_trailing_time:
                # UDN appends the publish time to the headline text ("...戰力」14:15").
                # Left in place it pollutes both the search index and the token
                # set SimHash compares.
                title = re.sub(r"\s*\d{1,2}:\d{2}$", "", title).strip()

            previous = best.get(url)
            if previous is None or len(title) > len(previous[0]):
                best[url] = (title, published or (previous[1] if previous else None))

        stubs = [
            ArticleStub(outlet=self.code, url_original=url, title=title, published_at=published)
            for url, (title, published) in best.items()
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


def _parse_listing_datetime(text: str) -> datetime | None:
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            # Listing pages print local wall-clock time with no offset.
            return datetime.strptime(text, fmt).replace(tzinfo=TAIPEI)
        except ValueError:
            continue
    return None

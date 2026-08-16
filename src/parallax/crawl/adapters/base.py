from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

import feedparser

from ...models import ArticleStub, OutletConfig
from ..http import Fetcher

log = logging.getLogger(__name__)

# A feed whose newest item is older than this has stopped publishing. Set well
# above the slowest legitimate section (CNA's 文化 runs ~17h behind, 科技 ~24h)
# so low-volume sections are not flagged as broken.
_STALE_AFTER = timedelta(days=4)


class ListingAdapter(Protocol):
    """Tier-1 contract: return the outlet's current listing as metadata stubs.

    Implementations never fetch article bodies. Body fetching is tier 2 and only
    happens for articles that matched a keyword someone actually searched.
    """

    code: str

    def fetch(self) -> list[ArticleStub]: ...


def _from_struct_time(value) -> datetime | None:
    if not value:
        return None
    # feedparser normalises parsed dates to UTC.
    return datetime(*value[:6], tzinfo=UTC)


class RSSAdapter:
    """Preferred adapter: parses a declared feed.

    Feeds are cheaper, more stable across redesigns, and explicitly published for
    consumption, which is why every outlet that offers one uses this path.
    """

    def __init__(self, config: OutletConfig, fetcher: Fetcher) -> None:
        self.code = config.code
        self.config = config
        self.fetcher = fetcher

    def fetch(self) -> list[ArticleStub]:
        if not self.config.feed_urls:
            raise ValueError(f"{self.code}: RSSAdapter requires at least one feed_url")

        stubs: list[ArticleStub] = []
        seen: set[str] = set()
        errors: list[str] = []
        stale: list[str] = []

        for feed_url in self.config.feed_urls:
            newest: datetime | None = None
            try:
                # Fetch through Fetcher rather than letting feedparser make its own
                # request, so the rate limit and User-Agent apply here too.
                raw = self.fetcher.get(feed_url).content
            except Exception as exc:  # noqa: BLE001
                # One dead section must not cost us the outlet's other sections
                # this cycle -- that data cannot be re-fetched later.
                errors.append(f"{feed_url}: {type(exc).__name__}")
                continue

            for entry in feedparser.parse(raw).entries:
                link = entry.get("link")
                title = (entry.get("title") or "").strip()
                # An entry with no link or title is a hollow placeholder, not an
                # article. UDN serves 20 of them per channel; counting entries
                # rather than usable ones is how a dead feed passes for healthy.
                if not link or not title or link in seen:
                    continue
                seen.add(link)
                published = _from_struct_time(
                    entry.get("published_parsed") or entry.get("updated_parsed")
                )
                if published and (newest is None or published > newest):
                    newest = published
                stubs.append(
                    ArticleStub(
                        outlet=self.code,
                        url_original=link,
                        title=title,
                        published_at=published,
                    )
                )

            if newest is not None and newest < datetime.now(UTC) - _STALE_AFTER:
                stale.append(f"{feed_url} (newest {newest:%Y-%m-%d})")

        # A feed frozen in the past keeps returning well-formed items forever, so
        # it never looks like an error -- it just quietly files old articles into
        # historical days and distorts the coverage-weight denominator.
        if stale:
            log.warning("%s: stale feed(s), consider dropping: %s", self.code, "; ".join(stale))

        # Every section failing is a real breakage, not a quiet news cycle.
        if errors and not stubs:
            raise RuntimeError(f"all {len(self.config.feed_urls)} feeds failed: {'; '.join(errors)}")
        return stubs


def build_adapter(config: OutletConfig, fetcher: Fetcher) -> ListingAdapter:
    # Imported here to keep this module free of a circular import back through
    # the adapters package.
    from .html_listing import PatternListingAdapter
    from .tvbs import TVBSAdapter

    if config.parser == "rss":
        return RSSAdapter(config, fetcher)
    if config.parser == "pattern":
        return PatternListingAdapter(config, fetcher)
    if config.parser == "tvbs":
        return TVBSAdapter(config, fetcher)
    raise ValueError(f"{config.code}: unknown parser {config.parser!r}")

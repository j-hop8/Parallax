from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ...models import ArticleStub, OutletConfig
from ..http import Fetcher

log = logging.getLogger(__name__)

_ISLAND = "RealTimeList"


def _unwrap(value):
    """Undo Astro's island serialisation.

    Astro tags every value as [typeTag, payload]: 0 is a scalar or nested object,
    1 is an array. Decoding it is much cheaper and more stable than driving a
    headless browser, and it yields fields the rendered DOM never exposes.
    """
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        tag, payload = value
        if tag == 0:
            return _unwrap(payload)
        if tag == 1:
            return [_unwrap(v) for v in payload]
        return payload
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


class TVBSAdapter:
    """TVBS renders its listing client-side, so there is nothing to scrape in the DOM.

    The articles are still present, serialised into the props of the
    `RealTimeList` Astro island. That payload carries a real `publishedAt` Unix
    timestamp -- better provenance than any other non-RSS outlet here, and enough
    to place TVBS in a propagation chain.
    """

    def __init__(self, config: OutletConfig, fetcher: Fetcher) -> None:
        self.code = config.code
        self.config = config
        self.fetcher = fetcher

    def fetch(self) -> list[ArticleStub]:
        if not self.config.listing_url:
            raise ValueError(f"{self.code}: TVBSAdapter requires listing_url")
        return self.parse(self.fetcher.get_text(self.config.listing_url))

    def parse(self, html: str) -> list[ArticleStub]:
        soup = BeautifulSoup(html, "lxml")
        islands = [
            i for i in soup.find_all("astro-island") if _ISLAND in (i.get("component-url") or "")
        ]
        if not islands:
            raise RuntimeError(
                f"{self.code}: no {_ISLAND} island found -- the page structure changed"
            )

        payload = _unwrap(json.loads(islands[0]["props"]))
        articles = (payload.get("initialArticles") or {}).get("data") or []

        stubs: list[ArticleStub] = []
        for art in articles:
            title = (art.get("title") or "").strip()
            article_id = art.get("articleId")
            if not title or not article_id:
                continue

            url = art.get("articleUrl") or f"/news/{article_id}"
            published = None
            if art.get("publishedAt"):
                published = datetime.fromtimestamp(int(art["publishedAt"]), tz=UTC)

            stubs.append(
                ArticleStub(
                    outlet=self.code,
                    url_original=urljoin(self.config.listing_url, str(url)),
                    title=title,
                    published_at=published,
                )
            )

        if not stubs:
            log.warning("%s: island present but yielded 0 articles", self.code)
        return stubs

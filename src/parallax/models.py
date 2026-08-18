from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class ArticleStub:
    """One item from a listing crawl. Metadata only -- no body is fetched here."""

    outlet: str
    url_original: str
    title: str
    published_at: datetime | None


@dataclass
class CrawlResult:
    """Outcome of one outlet's crawl, written to crawl_runs whether it succeeded or not."""

    outlet: str
    items_seen: int = 0
    items_new: int = 0
    ok: bool = False
    error: str | None = None

    # Captured when the outlet's fetch begins, NOT when the row is written.
    # crawl_runs.started_at previously took its column default, so it was set at
    # INSERT time -- the same instant as finished_at. Every run therefore
    # recorded a 0.0s duration, and, more importantly, started_at was really the
    # END of the crawl. The rollup measures coverage gaps between started_at
    # values, so a slow run shifted its own timestamp later and distorted the
    # completeness calculation that decides whether a day may be a denominator.
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class OutletConfig:
    code: str
    name_zh: str
    home_url: str

    # A list, not one URL. CNA and UDN publish no whole-site feed -- only
    # per-section ones -- and coverage weight divides by an outlet's *total*
    # daily output. Crawling one section would shrink the denominator and
    # overstate that outlet's editorial priority on every incident.
    feed_urls: tuple[str, ...]

    parser: str            # "rss" | "html"
    rate_limit_seconds: float
    verified: bool

    # Upper bound on wall-clock time spent on this one outlet per run. Feeds not
    # reached within it are reported as errors, so the run is honestly degraded
    # rather than quietly truncated.
    budget_seconds: float = 180.0

    # True when the feed carries publish timestamps. False means effective_at
    # falls back to seen_at, which is only as precise as the poll interval --
    # too coarse to order a propagation chain from listing data alone.
    has_dates: bool = True

    listing_url: str | None = None

    # Article-URL shape, e.g. r"/news/\d+". Preferred over CSS selectors: markup
    # changes constantly and fails silently, URL structure rarely does.
    article_url_pattern: str | None = None
    # Some listings prefix the headline with a timestamp; capture group 1 is it.
    title_datetime_pattern: str | None = None
    # UDN appends "14:15" to headline text.
    strip_trailing_time: bool = False
    # 三立 declares "+00:00" on a timestamp that is really Taipei local time.
    timestamp_offset_is_wrong: bool = False

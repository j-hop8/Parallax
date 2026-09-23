"""One keyword in, the design's page out -- as data.

`build_report` is the seam the UI (T-011) renders and the `report` job prints.
It runs the reads in db.py / search.py and hands the rows to the pure metric
modules; nothing above this function should need SQL.

Windowing and day bucketing are Taipei dates (invariant 3) over `effective_at`
(invariant 4), for the numerator and the denominator alike -- T-006b's
denominator-bias finding is bounded by keeping both on the same clock, and
`day_shift` reports how many matched articles that clock moved.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg

from .. import db, search
from ..config import load_outlets
from ..nlp.stance import POST_PROMPT_VERSION, PROMPT_VERSION
from ..settings import (
    MIN_BASELINE_DAYS,
    MIN_DAILY_DENOMINATOR,
    MIN_PLATFORM_POSTS,
    STANCE_MODEL,
    TIMEZONE,
)
from .coverage import OutletCoverage, baseline_of, coverage
from .lean import PLATFORMS, PlatformLean, platform_lean
from .originality import OutletOriginality, originality
from .propagation import ClusterView, cluster_view

TZ = ZoneInfo(TIMEZONE)


def taipei_day(dt: datetime) -> date:
    return dt.astimezone(TZ).date()


@dataclass(frozen=True)
class OutletRow:
    outlet: str
    name_zh: str
    matched: int  # tier-1 title matches in the window
    enriched: int  # of those, with a body
    classified: int  # of those, with a stance verdict at the configured model/prompt
    neg: int
    neu: int
    pos: int
    coverage: OutletCoverage
    originality: OutletOriginality | None  # None when nothing is enriched


@dataclass(frozen=True)
class IncidentReport:
    keyword: str
    since: date | None
    until: date | None
    articles: int
    outlets: int  # outlets with at least one match
    clusters: int
    first_day: date | None
    last_day: date | None
    active_days: int  # days on which any outlet matched
    span_days: int  # last - first + 1
    rows: tuple[OutletRow, ...]
    cluster_views: tuple[ClusterView, ...]
    platform_lean: tuple[PlatformLean, ...]  # Q4, one row per live platform
    denominator_as_of: datetime | None
    day_shift: int  # matched articles whose effective day differs from their poll day
    stance_model: str
    prompt_version: str
    post_prompt_version: str

    @property
    def empty(self) -> bool:
        return self.articles == 0


def _in_window(day: date, since: date | None, until: date | None) -> bool:
    return (since is None or day >= since) and (until is None or day <= until)


def build_report(
    conn: psycopg.Connection,
    keyword: str,
    since: date | None = None,
    until: date | None = None,
    *,
    stance_model: str = STANCE_MODEL,
    prompt_version: str = PROMPT_VERSION,
    min_denominator: int = MIN_DAILY_DENOMINATOR,
    min_baseline_days: int = MIN_BASELINE_DAYS,
    post_prompt_version: str = POST_PROMPT_VERSION,
    min_platform_posts: int = MIN_PLATFORM_POSTS,
) -> IncidentReport:
    _, configured = load_outlets()
    names = {o.code: o.name_zh for o in configured}
    order = [o.code for o in configured]

    matched = [
        r
        for r in search.match_all(conn, keyword)
        if _in_window(taipei_day(r["effective_at"]), since, until)
    ]
    ids = [r["id"] for r in matched]
    id_set = frozenset(ids)

    # Outlets in config order, then any outlet that matched but is not configured
    # (a test outlet, or one added after the baseline was fixed).
    seen_outlets = {r["outlet"] for r in matched}
    outlets = order + sorted(seen_outlets - set(order))

    counts: dict[str, Counter] = defaultdict(Counter)
    day_shift = 0
    for r in matched:
        day = taipei_day(r["effective_at"])
        counts[r["outlet"]][day] += 1
        if day != taipei_day(r["seen_at"]):
            day_shift += 1
    active = sorted({d for c in counts.values() for d in c})

    totals: dict[str, dict[date, tuple[int, bool]]] = defaultdict(dict)
    if active:
        for t in db.daily_totals(conn, outlets, active[0], active[-1]):
            totals[t["outlet"]][t["day"]] = (t["total_articles"], t["complete"])
    baselines = {
        o: baseline_of(ts, min_days=min_baseline_days, min_denominator=min_denominator)
        for o, ts in db.complete_day_totals(conn).items()
    }

    stance = {
        s["outlet"]: s
        for s in (
            db.stance_for_ids(conn, ids, keyword, stance_model, prompt_version) if ids else []
        )
    }
    roles = db.cluster_roles(conn, ids) if ids else []
    enriched = Counter(r["outlet"] for r in roles)
    orig = originality(roles)

    rows = []
    for o in outlets:
        s = stance.get(o)
        rows.append(
            OutletRow(
                outlet=o,
                name_zh=names.get(o, o),
                matched=sum(counts[o].values()),
                enriched=enriched.get(o, 0),
                classified=s["n"] if s else 0,
                neg=s["neg"] if s else 0,
                neu=s["neu"] if s else 0,
                pos=s["pos"] if s else 0,
                coverage=coverage(
                    o,
                    counts[o],
                    totals[o],
                    active,
                    baseline=baselines.get(o),
                    min_denominator=min_denominator,
                ),
                originality=orig.get(o),
            )
        )

    views = tuple(cluster_view(c, id_set) for c in (db.clusters_touching(conn, ids) if ids else []))

    # Q4 reads the same window the article side just used, so the panel answers
    # "during this incident" rather than "ever". Posts are timestamped by the
    # platform in UTC; the window edges are Taipei midnights (invariant 3),
    # half-open so a post at 23:59:59 on the last day is in and the next is out.
    leans: tuple[PlatformLean, ...] = ()
    if active:
        start = since or active[0]
        end = until or active[-1]
        lo = datetime.combine(start, time.min, tzinfo=TZ)
        hi = datetime.combine(end + timedelta(days=1), time.min, tzinfo=TZ)
        leans = tuple(
            platform_lean(
                p,
                db.post_stance_counts(
                    conn, keyword, p, lo, hi, stance_model, post_prompt_version
                ),
                min_posts=min_platform_posts,
            )
            for p in PLATFORMS
        )

    return IncidentReport(
        keyword=keyword,
        since=since,
        until=until,
        articles=len(matched),
        outlets=len(seen_outlets),
        clusters=len(views),
        first_day=active[0] if active else None,
        last_day=active[-1] if active else None,
        active_days=len(active),
        span_days=(active[-1] - active[0]).days + 1 if active else 0,
        rows=tuple(rows),
        cluster_views=views,
        platform_lean=leans,
        denominator_as_of=db.rollup_as_of(conn),
        day_shift=day_shift,
        stance_model=stance_model,
        prompt_version=prompt_version,
        post_prompt_version=post_prompt_version,
    )

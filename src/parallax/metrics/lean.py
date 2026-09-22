"""Q4: a platform's stance distribution toward the incident's target.

Pure. The caller hands over the counts db.post_stance_counts returned; this
module decides whether they may be shown at all, and nothing here knows SQL.

The one rule, and it is invariant 7 transposed from articles to posts: a
neg/neu/pos split over a handful of posts is noise wearing the costume of a
measurement. Threads keyword search returns far fewer posts for an incident
than the news index returns articles, so the floor bites more often here than
the coverage denominator does -- which is the point. `suppressed_reason` is a
stable code, never display text: the text report and the panel each say it in
their own register, and a test can pin the reason without pinning a string of
Chinese.

Platforms are listed here rather than discovered from the rows, so a platform
with no posts still appears and says so. Facebook is absent on purpose: there
is no compliant read path, so it is a parked slot in the design, not a row with
a zero in it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

PLATFORMS: tuple[str, ...] = ("threads",)

# no_posts:     nothing matched the keyword on this platform in the window
# unclassified: posts exist but none carry a verdict at this model/prompt
# below_floor:  fewer classified posts than the floor -- a percentage off 6 is noise
Reason = Literal["no_posts", "unclassified", "below_floor"]


@dataclass(frozen=True)
class PlatformLean:
    platform: str
    posts: int  # matching posts in the window, classified or not
    classified: int  # of those, with a verdict at the configured model/prompt
    neg: int
    neu: int
    pos: int
    min_posts: int  # the floor this row was judged against
    suppressed_reason: Reason | None  # None: the distribution may be shown

    @property
    def suppressed(self) -> bool:
        return self.suppressed_reason is not None


def platform_lean(platform: str, counts: Mapping[str, int], *, min_posts: int) -> PlatformLean:
    """One platform's row. Counts missing a key are read as zero."""
    posts = int(counts.get("posts") or 0)
    classified = int(counts.get("classified") or 0)
    neg = int(counts.get("neg") or 0)
    neu = int(counts.get("neu") or 0)
    pos = int(counts.get("pos") or 0)

    reason: Reason | None
    if posts == 0:
        reason = "no_posts"
    elif classified == 0:
        reason = "unclassified"
    elif classified < min_posts:
        reason = "below_floor"
    else:
        reason = None

    return PlatformLean(
        platform=platform,
        posts=posts,
        classified=classified,
        neg=neg,
        neu=neu,
        pos=pos,
        min_posts=min_posts,
        suppressed_reason=reason,
    )

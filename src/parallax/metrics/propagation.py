"""Q3 read model: a stored cluster as the UI may show it.

Pure. The one job of this module is invariant 5: a cluster whose
`origin_confident` is false has no origin, no ranks and no "removed" lines,
whatever the caller does with it afterwards. Members still come out in time
order -- the times are real, the *claim* that one copied another is what the
noise floor cannot support.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..nlp.dedup import Member, build_cluster


@dataclass(frozen=True)
class MemberView:
    article_id: int
    outlet: str
    title: str
    effective_at: datetime
    rank: int | None  # None on an indeterminate cluster
    gap: timedelta | None  # since the origin; None for the origin and when indeterminate
    delta_added: tuple[str, ...]
    delta_removed: tuple[str, ...]
    delta_summary: str | None
    matched: bool  # this member itself matched the keyword


@dataclass(frozen=True)
class ClusterView:
    cluster_id: int
    origin_confident: bool
    reason: str  # why the order is indeterminate; "" when confident
    origin: MemberView | None
    members: tuple[MemberView, ...]  # time order
    shared_core_text: str | None

    @property
    def first_at(self) -> datetime:
        return self.members[0].effective_at


def cluster_view(cluster: dict, matched_ids: set[int] | frozenset[int]) -> ClusterView:
    """Build the view from one db.clusters_touching row group.

    Members need id, outlet, title, effective_at, published_at, delta_added,
    delta_removed, delta_summary. The stored `origin_confident` is the verdict;
    the reason is rebuilt with the same rule dedup applied, because it is not
    stored.
    """
    raw = sorted(cluster["members"], key=lambda m: (m["effective_at"], m["id"]))
    confident = bool(cluster["origin_confident"])
    reason = ""
    if not confident and len(raw) >= 2:
        reason = (
            build_cluster(
                Member(m["id"], m["outlet"], m["effective_at"], m["published_at"]) for m in raw
            ).reason
            or "stored as indeterminate"
        )

    first_at = raw[0]["effective_at"]
    members = []
    for i, m in enumerate(raw):
        removed = tuple(m.get("delta_removed") or ())
        if not confident:
            # T-009 writes no direction for an indeterminate cluster. Enforce it
            # on the way out too, so a stale row can never print "removed".
            removed = ()
        members.append(
            MemberView(
                article_id=m["id"],
                outlet=m["outlet"],
                title=m["title"],
                effective_at=m["effective_at"],
                rank=i + 1 if confident else None,
                gap=(m["effective_at"] - first_at) if (confident and i > 0) else None,
                delta_added=tuple(m.get("delta_added") or ()),
                delta_removed=removed,
                delta_summary=m.get("delta_summary"),
                matched=m["id"] in matched_ids,
            )
        )
    return ClusterView(
        cluster_id=cluster["cluster_id"],
        origin_confident=confident,
        reason=reason,
        origin=members[0] if confident else None,
        members=tuple(members),
        shared_core_text=cluster.get("shared_core_text"),
    )

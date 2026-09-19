"""Originality rate, from the cluster columns dedup stored.

Pure. Two rates, the same two `make dedup` prints: `original` treats an
article as original when it stands alone or is the confident first of its
cluster (the origin did the reporting); `strict` is the proposal's literal
"not in any near-duplicate cluster". A member of an indeterminate cluster is
`unresolved` and original under neither -- we do not know who wrote it first,
so we do not credit anyone.

Known gap: dedup decides `too_short` in memory and stores nothing, so a body
too short to fingerprint counts as `alone` here. Two ltn video pages at the
time of writing.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class OutletOriginality:
    outlet: str
    n: int
    alone: int
    first: int
    follow: int
    unresolved: int

    @property
    def original(self) -> float | None:
        return (self.alone + self.first) / self.n if self.n else None

    @property
    def strict(self) -> float | None:
        return self.alone / self.n if self.n else None


def role_of(row: Mapping) -> str:
    """alone / first / follow / unresolved for one enriched article.

    `dup_cluster_id` None means alone. Otherwise the cluster's
    `origin_confident` decides whether the rank means anything (invariant 5).
    """
    if row.get("dup_cluster_id") is None:
        return "alone"
    if not row.get("origin_confident"):
        return "unresolved"
    return "first" if row.get("is_cluster_origin") else "follow"


def originality(rows: Iterable[Mapping]) -> dict[str, OutletOriginality]:
    """Per outlet, over rows shaped like db.cluster_roles: outlet, dup_cluster_id,
    is_cluster_origin, origin_confident."""
    per: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        per[r["outlet"]][role_of(r)] += 1
    return {
        o: OutletOriginality(
            outlet=o,
            n=sum(c.values()),
            alone=c["alone"],
            first=c["first"],
            follow=c["follow"],
            unresolved=c["unresolved"],
        )
        for o, c in sorted(per.items())
    }

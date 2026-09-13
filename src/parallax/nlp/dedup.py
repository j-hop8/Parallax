"""Q3: near-duplicate clusters and who published first.

Deterministic, no model. The design follows what the enriched corpus showed
before any of this was written (T-008 ticket):

- SimHash-64 over jieba token bigrams is the *prefilter*. The canonical wire
  copy (cna -> ltn, 2h50m apart) sits at Hamming 9, so the textbook "<= 3"
  rule -- and the 4x16-bit band blocking the schema anticipates, which can only
  guarantee <= 3 -- would miss exactly the case this project exists to find.
- **Containment** of bigram sets is the verdict: true copies score 0.90-1.00,
  then a gap, then a 0.6-0.8 tail of "same press release quoted at length".
- Per-outlet **boilerplate** must be stripped first. tvbs appends related-story
  rails to every body; ltn/udn/chinatimes append app promos, member-centre
  text and comment policy. Left in, an outlet's own articles cluster with each
  other on their furniture.

Everything here is pure: fingerprints in, clusters out. Persistence is in db.py
and jobs/dedup.py.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

MIN_FEATURES = 30  # fewer bigrams than this after boilerplate removal: not comparable
BOILERPLATE_MIN_DOCS = 3
BOILERPLATE_FRACTION = 0.2
HAMMING_PREFILTER = 12
CONTAINMENT_THRESHOLD = 0.85
JACCARD_THRESHOLD = 0.5
SHARES_SOURCE_THRESHOLD = 0.6  # containment tier reported, never stored
NOISE_FLOOR = timedelta(minutes=5)  # invariant 5

_TWO63 = 1 << 63
_TWO64 = 1 << 64


# ---- storage encoding ------------------------------------------------------


def to_signed(v: int) -> int:
    """64-bit unsigned -> Postgres BIGINT. Values >= 2^63 wrap negative."""
    if not 0 <= v < _TWO64:
        raise ValueError(f"not a 64-bit unsigned value: {v}")
    return v - _TWO64 if v >= _TWO63 else v


def from_signed(v: int) -> int:
    if not -_TWO63 <= v < _TWO63:
        raise ValueError(f"not a 64-bit signed value: {v}")
    return v + _TWO64 if v < 0 else v


# ---- features --------------------------------------------------------------

Bigram = tuple[str, str]


def bigrams(body_seg: str | None) -> Counter[Bigram]:
    """Counted token bigrams from a jieba-segmented body.

    Single-character tokens are dropped unless numeric: 的/了/在 carry no
    identity and would make every article look like every other. Numbers
    stay because "181家、2406人" is exactly the kind of detail copies share.
    """
    if not body_seg:
        return Counter()
    toks = [t for t in body_seg.split() if len(t) > 1 or t.isdigit()]
    return Counter(pairwise(toks))


def boilerplate(
    per_outlet_docs: dict[str, list[Counter[Bigram]]],
    *,
    min_docs: int = BOILERPLATE_MIN_DOCS,
    fraction: float = BOILERPLATE_FRACTION,
) -> dict[str, set[Bigram]]:
    """Bigrams that recur across an outlet's own articles: its furniture, not its news.

    A bigram in >= max(min_docs, fraction * n) of an outlet's enriched articles
    is dropped for that outlet only. Measured 2026-09-13: tvbs 1,213 such
    bigrams (related-story rails), cna 55 (dateline and credit lines), setn 9.
    Computed per run over whatever is enriched, so it adapts as sites change.
    """
    out: dict[str, set[Bigram]] = {}
    for outlet, docs in per_outlet_docs.items():
        df: Counter[Bigram] = Counter()
        for feats in docs:
            df.update(feats.keys())
        cut = max(min_docs, fraction * len(docs))
        out[outlet] = {g for g, c in df.items() if c >= cut}
    return out


def strip(feats: Counter[Bigram], drop: set[Bigram]) -> Counter[Bigram]:
    return Counter({g: w for g, w in feats.items() if g not in drop})


# ---- fingerprint -----------------------------------------------------------


def _feature_hash(g: Bigram) -> int:
    return int.from_bytes(hashlib.blake2b("|".join(g).encode(), digest_size=8).digest(), "big")


def simhash(feats: Counter[Bigram]) -> int:
    """Charikar SimHash-64 over weighted bigrams; 0 for an empty feature set."""
    if not feats:
        return 0
    v = [0] * 64
    for g, w in feats.items():
        h = _feature_hash(g)
        for i in range(64):
            v[i] += w if (h >> i) & 1 else -w
    return sum(1 << i for i in range(64) if v[i] > 0)


def bands(h: int) -> tuple[int, int, int, int]:
    """The four 16-bit slices. Two hashes within Hamming 3 share at least one
    band (pigeonhole) -- an index for the exact/near-exact fast path only."""
    return (h & 0xFFFF, (h >> 16) & 0xFFFF, (h >> 32) & 0xFFFF, (h >> 48) & 0xFFFF)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


@dataclass(frozen=True)
class Fingerprint:
    article_id: int
    outlet: str
    simhash: int  # unsigned
    bands: tuple[int, int, int, int]
    features: frozenset[Bigram]
    n_features: int

    @property
    def too_short(self) -> bool:
        return self.n_features < MIN_FEATURES


def fingerprint(
    article_id: int, outlet: str, body_seg: str | None, drop: set[Bigram]
) -> Fingerprint:
    feats = strip(bigrams(body_seg), drop)
    h = simhash(feats)
    return Fingerprint(
        article_id=article_id,
        outlet=outlet,
        simhash=h,
        bands=bands(h),
        features=frozenset(feats),
        n_features=sum(feats.values()),
    )


# ---- pairs -----------------------------------------------------------------


@dataclass(frozen=True)
class PairScore:
    a: int
    b: int
    hamming: int
    containment: float  # |A∩B| / min(|A|,|B|)
    jaccard: float  # |A∩B| / |A∪B|


def score_pair(fa: Fingerprint, fb: Fingerprint) -> PairScore:
    inter = len(fa.features & fb.features)
    smaller = min(len(fa.features), len(fb.features)) or 1
    union = len(fa.features | fb.features) or 1
    return PairScore(
        fa.article_id,
        fb.article_id,
        hamming(fa.simhash, fb.simhash),
        inter / smaller,
        inter / union,
    )


def is_duplicate(
    s: PairScore,
    *,
    containment: float = CONTAINMENT_THRESHOLD,
    jaccard: float = JACCARD_THRESHOLD,
) -> bool:
    """Both thresholds: containment catches wire-plus-appended-paragraph, the
    Jaccard floor stops a teaser buried inside a long article from counting."""
    return s.containment >= containment and s.jaccard >= jaccard


def candidate_pairs(
    fps: Iterable[Fingerprint], *, hamming_max: int = HAMMING_PREFILTER
) -> Iterator[PairScore]:
    """All pairs within scope that pass the Hamming prefilter, scored.

    All pairs, not band-blocked: the scope is tier-2 (hundreds to low
    thousands, keyword-driven), and bands cannot recall a Hamming-9 copy.
    Too-short fingerprints are skipped -- comparing furniture to furniture is
    how the two ltn video teasers would have become a "cluster".
    """
    usable = [f for f in fps if not f.too_short]
    for i, fa in enumerate(usable):
        for fb in usable[i + 1 :]:
            if hamming(fa.simhash, fb.simhash) <= hamming_max:
                yield score_pair(fa, fb)


# ---- clusters --------------------------------------------------------------


class UnionFind:
    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)

    def components(self) -> list[set[int]]:
        groups: dict[int, set[int]] = defaultdict(set)
        for x in list(self._parent):
            groups[self.find(x)].add(x)
        return [g for g in groups.values() if len(g) >= 2]


def components(edges: Iterable[tuple[int, int]]) -> list[set[int]]:
    uf = UnionFind()
    for a, b in edges:
        uf.union(a, b)
    return uf.components()


@dataclass(frozen=True)
class Member:
    article_id: int
    outlet: str
    effective_at: datetime
    published_at: datetime | None  # None = effective_at is a poll time (T-005)


@dataclass(frozen=True)
class Cluster:
    cluster_id: int  # == min(member article_id): stable across full rebuilds
    members: tuple[Member, ...]  # ranked by effective_at; rank = index + 1
    origin_confident: bool
    reason: str  # why not confident; "" when confident
    shares_source: tuple[int, ...] = field(default=())

    @property
    def origin(self) -> Member:
        return self.members[0]

    @property
    def first_published_at(self) -> datetime:
        return self.origin.effective_at


def build_cluster(members: Iterable[Member]) -> Cluster:
    """Rank by effective_at and decide whether the order may be claimed.

    Not confident when (invariant 5 and T-005):
    - the gap between first and second is inside the feed-timestamp noise floor;
    - any member's timestamp is a poll time, not a publish time;
    - the first two are the same outlet -- nothing propagated anywhere.
    """
    ranked = tuple(sorted(members, key=lambda m: (m.effective_at, m.article_id)))
    if len(ranked) < 2:
        raise ValueError("a cluster needs at least two members")
    first, second = ranked[0], ranked[1]

    reason = ""
    unstamped = [m.article_id for m in ranked if m.published_at is None]
    gap = second.effective_at - first.effective_at
    if unstamped:
        reason = f"no publish time for {', '.join(map(str, unstamped))}; poll time only"
    elif gap < NOISE_FLOOR:
        reason = f"gap {int(gap.total_seconds())}s inside the {int(NOISE_FLOOR.total_seconds())}s noise floor"
    elif first.outlet == second.outlet:
        reason = f"first two both {first.outlet}"

    return Cluster(
        cluster_id=min(m.article_id for m in ranked),
        members=ranked,
        origin_confident=reason == "",
        reason=reason,
    )

"""Q3, second half: within a near-duplicate cluster, what did each member change?

The unit is the sentence. Every member's body is split into sentences, credits
and per-outlet furniture are dropped, and sentences are matched across
members by normalised text (or a 0.9 similarity, which absorbs a typo or a
dropped space; a rewording scores 0.74-0.89 on this corpus and stays visible
as a −/＋ pair, which the readout folds into a ～ line).

What the deltas are relative to depends on whether the order may be claimed
(invariant 5), and that choice is baked into the stored data, not left to
the UI:

- **origin confident**: the origin is the reference. It has no deltas; each
  follower's `added` is what it ran that the origin did not, `removed` is
  what the origin ran that it dropped.
- **order indeterminate**: the shared core is the reference and no member is
  first. Each member's `added` is what only it ran; `removed` is always empty,
  because "dropped" presumes it once had the text.

The **core** is every sentence that at least max(2, ⌈n/2⌉) members share, in
the origin's order: the design's 共同核心稿源.

Datelines and credit lines -- `（中央社芝加哥22日綜合外電報導）` in front of
CNA's lede, `（編譯：陳昱婷）1150823` after its last sentence, `（路透）` on a
caption -- are bracketed at sentence edges and are stripped before matching.
Without that the lede of every CNA reprint reads as a false delta.

Pure: member texts in, deltas out. Persistence is in db.py and jobs/framing.py;
the LLM summary is in nlp/summary.py.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .dedup import BOILERPLATE_FRACTION, BOILERPLATE_MIN_DOCS

MIN_HAN_CHARS = 4  # fewer than this after stripping: a credit, an id, a stray mark
SAME_RATIO = 0.9  # >= this: the same sentence (typo, spacing, punctuation width)
EDIT_RATIO = 0.6  # >= this between an added and a removed sentence: a rewording
CORE_MIN_MEMBERS = 2

# A sentence ends at 。！？, and any closing quote or bracket that follows
# belongs to it. Paragraph breaks always split. The second alternative keeps
# a trailing fragment with no terminator (a credit line, a sub-head).
_SENTENCE = re.compile(r"[^。！？\n]*[。！？]+[」』）】\]]*|[^。！？\n]+")
_WS = re.compile(r"\s+")
# After NFKC, （ is ( and ／ is /; 〔【 are not folded and are listed as such.
_LEADING_BRACKETS = re.compile(r"^(?:[(\[〔【][^)\]〕】]{0,40}[)\]〕】])+")
_TRAILING_BRACKETS = re.compile(r"(?:[(\[〔【][^)\]〕】]{0,40}[)\]〕】])+$")
_TERMINATORS = "。！？!?.…"
# The same edge rule on the original text, for what people see: a core
# sentence is shown without the dateline only one member carried.
_SHOWN_LEADING = re.compile(r"^(?:[（(〔\[【][^）)〕\]】]{0,40}[）)〕\]】])+")
_SHOWN_TRAILING = re.compile(r"(?:[（(〔\[【][^）)〕\]】]{0,40}[）)〕\]】])+\s*$")
_BYLINE = re.compile(r"^[^,;:]{0,14}/[^,;:]{0,14}報導$")  # 記者X/台北報導, 即時中心/X報導


def sentences(body: str | None) -> list[str]:
    """Sentences in reading order. extract_body joins paragraphs with \\n."""
    if not body:
        return []
    out = []
    for para in body.split("\n"):
        for s in _SENTENCE.findall(para):
            s = s.strip()
            if s:
                out.append(s)
    return out


def normalize(sentence: str) -> str:
    """The matching key: no whitespace, no edge brackets, folded punctuation.

    udn drops the space in "Mark Carney"; setn's sub-heads carry a ● that the
    Han count still passes; a full-width comma and a half-width one must be
    the same comma. The brackets rule removes datelines and credits at either
    edge, and a sentence that was nothing but a credit comes out empty.
    """
    s = unicodedata.normalize("NFKC", sentence)
    s = _WS.sub("", s)
    s = s.replace("、", ",")
    s = _LEADING_BRACKETS.sub("", s)
    s = _TRAILING_BRACKETS.sub("", s)
    return s.strip(_TERMINATORS)


def display(sentence: str) -> str:
    """The sentence as shown: original wording, edge brackets (datelines,
    credits, agency tags) removed, so a core sentence does not carry the one
    member's dateline and a caption does not end in （路透）."""
    return _SHOWN_TRAILING.sub("", _SHOWN_LEADING.sub("", sentence)).strip()


def han_count(s: str) -> int:
    return sum(1 for ch in s if "一" <= ch <= "鿿")


def is_credit(key: str) -> bool:
    """A normalised sentence that carries no reporting: an id, a byline, a mark."""
    return han_count(key) < MIN_HAN_CHARS or bool(_BYLINE.match(key))


def same(a: str, b: str) -> bool:
    """Same sentence? Exact after normalisation, or nearly so.

    The length pre-check is exact: ratio() is 2M / (|a|+|b|) with M <= |shorter|,
    so it cannot reach t unless |shorter| / |longer| >= t / (2 - t) -- 0.82 at 0.9.
    """
    if a == b:
        return True
    shorter, longer = sorted((len(a), len(b)))
    if shorter == 0 or shorter / longer < SAME_RATIO / (2 - SAME_RATIO) - 1e-9:
        return False
    m = SequenceMatcher(None, a, b)
    return m.quick_ratio() >= SAME_RATIO and m.ratio() >= SAME_RATIO


def boilerplate(
    per_outlet_bodies: dict[str, list[str | None]],
    *,
    min_docs: int = BOILERPLATE_MIN_DOCS,
    fraction: float = BOILERPLATE_FRACTION,
) -> dict[str, set[str]]:
    """Sentences (by key) that recur across an outlet's own articles.

    Same rule as dedup's bigram boilerplate, at the sentence level: present in
    >= max(min_docs, fraction * n) of the outlet's enriched articles. Measured
    2026-09-19: cna's three promo lines, ltn's 請繼續往下閱讀, udn's member-centre
    lines and its 禁止酒駕 reminder, chinatimes' comment policy.
    """
    out: dict[str, set[str]] = {}
    for outlet, bodies in per_outlet_bodies.items():
        df: Counter[str] = Counter()
        for body in bodies:
            df.update({normalize(s) for s in sentences(body)})
        cut = max(min_docs, fraction * len(bodies))
        out[outlet] = {k for k, c in df.items() if c >= cut}
    return out


@dataclass(frozen=True)
class Unit:
    key: str
    text: str  # the member's own wording, shown to people


def units(body: str | None, drop: set[str] = frozenset()) -> list[Unit]:
    """The comparable sentences of one body: credits, furniture and repeats out."""
    out: list[Unit] = []
    seen: set[str] = set()
    for s in sentences(body):
        k = normalize(s)
        if not k or k in drop or k in seen or is_credit(k):
            continue
        seen.add(k)
        out.append(Unit(k, display(s)))
    return out


@dataclass(frozen=True)
class MemberText:
    article_id: int
    outlet: str
    title: str
    units: tuple[Unit, ...]


@dataclass(frozen=True)
class MemberDelta:
    article_id: int
    added: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.added and not self.removed


@dataclass(frozen=True)
class Framing:
    cluster_id: int
    directional: bool  # == origin_confident of the cluster
    core: tuple[str, ...]  # origin's wording, origin's order
    deltas: tuple[MemberDelta, ...]  # same order as the members given
    shared_by: dict[int, int] = field(default_factory=dict, repr=False)  # unit index -> members

    @property
    def core_text(self) -> str:
        return "\n".join(self.core)


def diff_cluster(members: list[MemberText], *, origin_confident: bool) -> Framing:
    """Members ranked (rank 1 first). See the module docstring for the rules."""
    if len(members) < 2:
        raise ValueError("a cluster needs at least two members")

    # Canonical sentence pool: first-seen wording wins, so a core sentence is
    # shown in the rank-1 member's words and in its order.
    pool: list[Unit] = []
    has: list[dict[int, str]] = []  # per member: pool index -> own wording
    for m in members:
        mine: dict[int, str] = {}
        for u in m.units:
            for pi, pu in enumerate(pool):
                if same(u.key, pu.key):
                    mine.setdefault(pi, u.text)
                    break
            else:
                pool.append(u)
                mine[len(pool) - 1] = u.text
        has.append(mine)

    n = len(members)
    need = max(CORE_MIN_MEMBERS, -(-n // 2))
    counts = {pi: sum(pi in h for h in has) for pi in range(len(pool))}
    core = [pi for pi in range(len(pool)) if counts[pi] >= need]
    reference = set(has[0]) if origin_confident else set(core)

    deltas = []
    for i, m in enumerate(members):
        if origin_confident and i == 0:
            deltas.append(MemberDelta(m.article_id, (), ()))
            continue
        added = tuple(has[i][pi] for pi in sorted(set(has[i]) - reference))
        removed = (
            tuple(pool[pi].text for pi in sorted(reference - set(has[i])))
            if origin_confident
            else ()
        )
        deltas.append(MemberDelta(m.article_id, added, removed))

    return Framing(
        cluster_id=min(m.article_id for m in members),
        directional=origin_confident,
        core=tuple(pool[pi].text for pi in core),
        deltas=tuple(deltas),
        shared_by=counts,
    )


def pair_edits(
    added: tuple[str, ...], removed: tuple[str, ...]
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Fold a rewording into one ～ line: (edits as (old, new), added-only, removed-only).

    Greedy by similarity, each sentence used once. Readout and summary prompt
    only -- the stored columns keep the plain sets.
    """
    scored = []
    for r in removed:
        rk = normalize(r)
        for a in added:
            ratio = SequenceMatcher(None, rk, normalize(a)).ratio()
            if ratio >= EDIT_RATIO:
                scored.append((ratio, r, a))
    scored.sort(key=lambda t: -t[0])
    used_r: set[str] = set()
    used_a: set[str] = set()
    edits: list[tuple[str, str]] = []
    for _, r, a in scored:
        if r in used_r or a in used_a:
            continue
        used_r.add(r)
        used_a.add(a)
        edits.append((r, a))
    return (
        edits,
        [a for a in added if a not in used_a],
        [r for r in removed if r not in used_r],
    )

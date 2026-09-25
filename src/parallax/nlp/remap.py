"""Re-key the stance gold set to a rebuilt database, by URL (T-025).

Pure. The caller supplies the gold rows and a {(outlet, canonical): id} map
read from `article_index`; this module decides what each row becomes and what
may be written at all.

Why it exists: `eval/stance_gold.csv` stores `article_id`, but ids belong to the
database, not to the article. Lose the database and every one of those 184
hand-made labels points at nothing. The URL the annotator actually looked at is
in the file too, and `article_index` is UNIQUE on (outlet, url_canonical), so
the labels can be re-attached to a re-crawled index exactly rather than
approximately.

Three rules, all of them about not making the file worse than losing it:

- A row whose article has not been re-crawled yet keeps its old id and is
  reported. It is pending, not wrong -- articles arrive over days, so the remap
  is meant to be re-run, and dropping unmatched rows would quietly destroy the
  labels this exists to save.
- Two rows resolving to one id is a hard stop for the whole file. It means the
  URL mapping is not a bijection, and writing a file where two different
  articles claim the same id would corrupt every join downstream.
- Nothing is written when nothing changed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from ..urls import canonicalize
from .gold import GoldRow

Outcome = Literal["remapped", "unchanged", "unmatched"]


@dataclass(frozen=True)
class RowPlan:
    row: GoldRow
    outcome: Outcome
    new_id: int  # the old id when unmatched, so the row survives untouched
    canonical: str


@dataclass(frozen=True)
class Plan:
    rows: tuple[RowPlan, ...]
    collisions: dict[int, tuple[int, ...]]  # new id -> the old ids fighting over it

    @property
    def remapped(self) -> int:
        return sum(p.outcome == "remapped" for p in self.rows)

    @property
    def unchanged(self) -> int:
        return sum(p.outcome == "unchanged" for p in self.rows)

    @property
    def unmatched(self) -> tuple[RowPlan, ...]:
        return tuple(p for p in self.rows if p.outcome == "unmatched")

    @property
    def safe(self) -> bool:
        """False when any id is claimed twice: the file must not be written."""
        return not self.collisions

    def applied(self) -> tuple[GoldRow, ...]:
        """The rewritten rows. Only the id changes; every other column is the
        annotator's work and is carried through untouched."""
        from dataclasses import replace

        return tuple(
            replace(p.row, article_id=p.new_id) if p.outcome == "remapped" else p.row
            for p in self.rows
        )


def canonical_keys(gold: Sequence[GoldRow]) -> list[tuple[str, str]]:
    """The (outlet, url_canonical) lookups this gold set needs, de-duplicated."""
    seen = {(g.outlet, canonicalize(g.url)) for g in gold}
    return sorted(seen)


def plan(gold: Sequence[GoldRow], found: Mapping[tuple[str, str], int]) -> Plan:
    """Decide each row's fate and detect id collisions across the whole file."""
    plans = []
    for g in gold:
        canon = canonicalize(g.url)
        hit = found.get((g.outlet, canon))
        if hit is None:
            plans.append(RowPlan(g, "unmatched", g.article_id, canon))
        elif hit == g.article_id:
            plans.append(RowPlan(g, "unchanged", hit, canon))
        else:
            plans.append(RowPlan(g, "remapped", hit, canon))

    # Collisions are judged on the resulting file, not on the changed rows
    # alone: an unchanged row already holding the id a remapped row wants is
    # exactly as broken. Distinct (article, target) pairs are the unit -- the
    # same article legitimately appears under two targets, and twice more once
    # a second annotator labels it (T-020).
    claims: dict[int, set[int]] = defaultdict(set)
    for p in plans:
        if p.outcome != "unmatched":
            claims[p.new_id].add(p.row.article_id)
    collisions = {new: tuple(sorted(olds)) for new, olds in claims.items() if len(olds) > 1}
    return Plan(tuple(plans), collisions)

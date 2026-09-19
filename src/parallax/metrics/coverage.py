"""Q2: coverage weight -- incident articles as a share of an outlet's daily output.

Pure. The caller supplies per outlet-day counts and denominators; this module
decides which days may be divided and how the days combine. Three rules from
the tickets that fed it:

- A raw count is not a denominator (T-004). Only an outlet-day whose crawl was
  gap-free (`complete`) AND whose total clears MIN_DAILY_DENOMINATOR
  (invariant 7) yields a weight. An incomplete day with 84 articles yields none.
- Pool, never average. Over a multi-day window the weight is sum(n) / sum(total)
  across the usable days, so a 1/327 day cannot weigh as much as a 9/817 day.
  A usable day on which the outlet ran nothing counts 0/total: that silence is
  the signal the metric exists to show.
- When no day is usable, the proposal's fallback is the outlet's own baseline
  (median complete-day total), and only once enough complete days exist for a
  median to mean something. The result is a lower bound and is flagged as such.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Literal

Basis = Literal["exact", "estimated", "none"]


@dataclass(frozen=True)
class DayCoverage:
    day: date
    n: int
    total: int | None  # None: no rollup row for this outlet-day
    complete: bool
    weight: float | None  # n / total, only when the day is usable


@dataclass(frozen=True)
class OutletCoverage:
    outlet: str
    n: int  # matched articles across the window
    weight: float | None
    basis: Basis
    days_used: int  # usable active days behind an exact weight
    days_active: int  # days on which any outlet matched
    days: tuple[DayCoverage, ...]


def baseline_of(
    complete_totals: Iterable[int], *, min_days: int, min_denominator: int
) -> float | None:
    """Median complete-day total, or None until there are enough days to trust it.

    Two complete days (all this laptop has produced) are not a baseline; the
    proposal says roughly ten. Days under the denominator floor are excluded
    first so a near-empty complete day cannot drag the median down.
    """
    usable = sorted(t for t in complete_totals if t >= min_denominator)
    if len(usable) < min_days:
        return None
    return float(median(usable))


def coverage(
    outlet: str,
    counts: Mapping[date, int],
    totals: Mapping[date, tuple[int, bool]],
    active_days: Sequence[date],
    *,
    baseline: float | None,
    min_denominator: int,
) -> OutletCoverage:
    """Combine one outlet's days into a weight, or decline to.

    `counts`: matched articles per Taipei day (days without a match may be
    absent). `totals`: (total_articles, complete) per day from the rollup.
    `active_days`: every day in the window on which any outlet matched; the
    outlet's usable days among these are pooled, including its zero days.
    """
    days: list[DayCoverage] = []
    used_n = used_total = 0
    for day in sorted(active_days):
        n = counts.get(day, 0)
        row = totals.get(day)
        total, complete = row if row is not None else (None, False)
        usable = complete and total is not None and total >= min_denominator
        weight = n / total if usable else None
        if usable:
            used_n += n
            used_total += total
        days.append(DayCoverage(day, n, total, complete, weight))

    n_all = sum(d.n for d in days)
    days_used = sum(1 for d in days if d.weight is not None)
    if days_used:
        return OutletCoverage(
            outlet, n_all, used_n / used_total, "exact", days_used, len(days), tuple(days)
        )
    if baseline and days:
        return OutletCoverage(
            outlet, n_all, n_all / (baseline * len(days)), "estimated", 0, len(days), tuple(days)
        )
    return OutletCoverage(outlet, n_all, None, "none", 0, len(days), tuple(days))

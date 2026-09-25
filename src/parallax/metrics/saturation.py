"""Feed-window pressure, computed without I/O from crawl-run dictionaries."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from types import MappingProxyType
from typing import Any

AT_RISK = 0.8
MIN_USABLE_RUNS = 2


@dataclass(frozen=True)
class OutletSaturation:
    outlet: str
    runs: int
    at_risk: int
    median: float | None
    worst: tuple[float, datetime] | None
    fastest_turnover_minutes: float | None
    excluded: Mapping[str, int]
    anomalies: int


def saturation(
    rows: Iterable[Mapping[str, Any]], *, since: datetime | None = None
) -> tuple[OutletSaturation, ...]:
    """Summarize each outlet; pre-window rows supply predecessor context only.

    Gaps run between successful polls (including zero-item or anomalous polls).
    The outage baseline is the median of positive gaps ending in the window.
    Exclusions are exclusive, in the order below; anomalies count independently
    even on failed/first/zero-item runs and are never divided or clamped.
    Two usable runs are the minimum for reporting estimates. Tied timestamps
    cannot measure a polling interval and are excluded explicitly.
    """
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["outlet"]].append(row)
    results = []
    for outlet, group in sorted(grouped.items()):
        previous = None
        window = []
        for row in sorted(group, key=lambda r: r["started_at"]):
            ts = row["started_at"]
            gap = (ts - previous).total_seconds() / 60 if previous is not None else None
            if since is None or ts >= since:
                window.append((row, gap))
            if row["ok"]:
                previous = ts
        if not window:
            continue
        gaps = [gap for row, gap in window if row["ok"] and gap is not None and gap > 0]
        typical_gap = median(gaps) if gaps else None
        excluded = dict.fromkeys(
            ("failed", "first", "zero_seen", "anomaly", "nonpositive_gap", "outage"), 0
        )
        usable = []
        turnovers = []
        anomalies = 0
        for row, gap in window:
            anomalous = row["items_new"] > row["items_seen"]
            anomalies += anomalous
            if not row["ok"]:
                reason = "failed"
            elif gap is None:
                reason = "first"
            elif row["items_seen"] == 0:
                reason = "zero_seen"
            elif anomalous:
                reason = "anomaly"
            elif gap <= 0:
                reason = "nonpositive_gap"
            elif typical_gap is not None and gap > 3 * typical_gap:
                reason = "outage"
            else:
                s = row["items_new"] / row["items_seen"]
                usable.append((s, row["started_at"]))
                if s > 0:
                    turnovers.append(gap / s)
                continue
            excluded[reason] += 1
        enough = len(usable) >= MIN_USABLE_RUNS
        results.append(OutletSaturation(
            outlet=outlet,
            runs=len(usable),
            at_risk=sum(s >= AT_RISK for s, _ in usable),
            median=float(median(s for s, _ in usable)) if enough else None,
            worst=max(usable, key=lambda sample: sample[0]) if enough else None,
            fastest_turnover_minutes=min(turnovers) if enough and turnovers else None,
            excluded=MappingProxyType(excluded),
            anomalies=anomalies,
        ))
    return tuple(results)

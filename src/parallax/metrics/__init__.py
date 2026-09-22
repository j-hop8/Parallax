"""Metrics over stored data: coverage weight (Q2), originality and propagation
(Q3), platform lean (Q4), composed into one IncidentReport per keyword. Pure
modules take rows; report.py is the only one that touches the database."""

from .coverage import DayCoverage, OutletCoverage, baseline_of, coverage
from .lean import PlatformLean, platform_lean
from .originality import OutletOriginality, originality
from .propagation import ClusterView, MemberView, cluster_view
from .report import IncidentReport, OutletRow, build_report, taipei_day

__all__ = [
    "ClusterView",
    "DayCoverage",
    "IncidentReport",
    "MemberView",
    "OutletCoverage",
    "OutletOriginality",
    "OutletRow",
    "PlatformLean",
    "baseline_of",
    "build_report",
    "cluster_view",
    "coverage",
    "originality",
    "platform_lean",
    "taipei_day",
]

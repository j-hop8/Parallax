"""The text readout prints only what the report supports: no weight without a
basis, no ranks or "removed" lines on an indeterminate cluster."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from parallax.jobs.report import main, render
from parallax.metrics.coverage import DayCoverage, OutletCoverage
from parallax.metrics.originality import OutletOriginality
from parallax.metrics.propagation import ClusterView, MemberView
from parallax.metrics.report import IncidentReport, OutletRow

T0 = datetime(2030, 1, 1, 1, 12, tzinfo=UTC)  # 09:12 Taipei
D1 = date(2030, 1, 1)


def _cov(outlet, n, weight, basis, used, active):
    return OutletCoverage(
        outlet, n, weight, basis, used, active, (DayCoverage(D1, n, 500, basis == "exact", weight),)
    )


def _mv(aid, outlet, minutes, rank, added=(), removed=(), summary=None, matched=True):
    at = T0 + timedelta(minutes=minutes)
    return MemberView(
        aid,
        outlet,
        f"t{aid}",
        at,
        rank,
        timedelta(minutes=minutes) if rank not in (None, 1) else None,
        tuple(added),
        tuple(removed),
        summary,
        matched,
    )


def _report(**over) -> IncidentReport:
    confident = ClusterView(
        cluster_id=1,
        origin_confident=True,
        reason="",
        origin=_mv(1, "cna", 0, 1, summary="原始稿源。"),
        members=(
            _mv(1, "cna", 0, 1, summary="原始稿源。"),
            _mv(2, "ltn", 12, 2, added=["加入鄰居受訪"], summary="＋ 鄰居受訪", matched=False),
        ),
        shared_core_text="新北市一名看護遭雇主家屬指控。",
    )
    indeterminate = ClusterView(
        cluster_id=3,
        origin_confident=False,
        reason="first two both udn",
        origin=None,
        members=(_mv(3, "udn", 0, None, added=["本版才有"]), _mv(4, "udn", 1, None)),
        shared_core_text=None,
    )
    rows = (
        OutletRow(
            "cna",
            "中央社",
            10,
            8,
            8,
            1,
            5,
            2,
            _cov("cna", 10, 0.021, "exact", 2, 3),
            OutletOriginality("cna", 8, 8, 0, 0, 0),
        ),
        OutletRow("ltn", "自由時報", 4, 0, 0, 0, 0, 0, _cov("ltn", 4, None, "none", 0, 3), None),
        OutletRow(
            "udn",
            "聯合報",
            6,
            6,
            0,
            0,
            0,
            0,
            _cov("udn", 6, 0.004, "estimated", 0, 3),
            OutletOriginality("udn", 6, 4, 0, 0, 2),
        ),
    )
    base = {
        "keyword": "看護",
        "since": None,
        "until": None,
        "articles": 20,
        "outlets": 3,
        "clusters": 2,
        "first_day": D1,
        "last_day": date(2030, 1, 3),
        "active_days": 3,
        "span_days": 3,
        "rows": rows,
        "cluster_views": (confident, indeterminate),
        "denominator_as_of": T0,
        "day_shift": 1,
        "stance_model": "m",
        "prompt_version": "v1",
    }
    base.update(over)
    return IncidentReport(**base)


def test_header_and_table():
    out = render(_report())
    assert "看護  20 篇文章  3 家媒體  2 個抄襲群  3 天" in out
    cna = next(line for line in out.splitlines() if line.startswith("cna"))
    assert "2.1%" in cna and "exact" in cna and "2/3d" in cna and "100%" in cna
    ltn = next(line for line in out.splitlines() if line.startswith("ltn"))
    # No denominator and no stance: dashes, never a number.
    assert "%" not in ltn
    assert ltn.count("—") >= 5
    udn = next(line for line in out.splitlines() if line.startswith("udn"))
    assert "0.4%" in udn and "est." in udn and "0/3d" in udn
    assert "67%" in udn  # original = 4/6; the two unresolved are credited to nobody


def test_confident_cluster_prints_origin_and_ranks():
    out = render(_report())
    assert "起源: cna (01-01 09:12)" in out
    assert "#1 01-01 09:12  cna" in out
    assert "#2 01-01 09:24  ltn" in out and "+12m" in out
    assert "＋ 加入鄰居受訪" in out
    assert "(title did not match)" in out


def test_indeterminate_cluster_prints_no_rank_and_no_direction():
    out = render(_report())
    block = out[out.index("群組 3") :]
    assert "順序不明: first two both udn" in block
    assert "#1" not in block and "#2" not in block
    assert "起源" not in block
    assert "－" not in block
    assert "本版獨有 本版才有" in block


def test_window_is_shown_when_given():
    out = render(_report(since=D1, until=date(2030, 1, 2)))
    assert "(window 2030-01-01 → 2030-01-02)" in out


def test_empty_report_is_one_line():
    out = render(_report(articles=0, outlets=0, clusters=0, rows=(), cluster_views=()))
    assert out == "no articles match '看護'"


def test_main_rejects_inverted_window():
    with pytest.raises(SystemExit):
        main(["--keyword", "x", "--since", "2030-01-02", "--until", "2030-01-01"])

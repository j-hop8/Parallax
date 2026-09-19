"""Pure metric rules: which days may be divided, how they pool, what a cluster
may claim. No database."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from parallax.metrics.coverage import baseline_of, coverage
from parallax.metrics.originality import originality, role_of
from parallax.metrics.propagation import cluster_view
from parallax.metrics.report import taipei_day

D1, D2, D3 = date(2030, 1, 1), date(2030, 1, 2), date(2030, 1, 3)
FLOOR = 20


# ---- coverage ------------------------------------------------------------------


def test_incomplete_day_never_yields_a_weight_even_with_a_big_total():
    # T-004's case: 84 articles on an incomplete day clears any floor and is
    # still under half the true total. The gate is `complete`, not the count.
    c = coverage("x", {D1: 5}, {D1: (84, False)}, [D1], baseline=None, min_denominator=FLOOR)
    assert c.basis == "none"
    assert c.weight is None
    assert c.n == 5  # the count is still reported
    assert c.days[0].weight is None
    assert c.days_used == 0 and c.days_active == 1


def test_complete_day_under_the_floor_is_not_usable():
    c = coverage("x", {D1: 1}, {D1: (6, True)}, [D1], baseline=None, min_denominator=FLOOR)
    assert c.basis == "none" and c.weight is None


def test_missing_rollup_row_is_not_usable():
    c = coverage("x", {D1: 1}, {}, [D1], baseline=None, min_denominator=FLOOR)
    assert c.basis == "none"
    assert c.days[0].total is None and c.days[0].complete is False


def test_pooled_not_averaged():
    # 1/327 and 9/817 must report 10/1144, not the mean of the two ratios.
    c = coverage(
        "x",
        {D1: 1, D2: 9},
        {D1: (327, True), D2: (817, True)},
        [D1, D2],
        baseline=None,
        min_denominator=FLOOR,
    )
    assert c.basis == "exact"
    assert c.weight == pytest.approx(10 / 1144)
    assert c.weight != pytest.approx((1 / 327 + 9 / 817) / 2)
    assert c.days_used == 2 and c.days_active == 2
    assert [d.weight for d in c.days] == [pytest.approx(1 / 327), pytest.approx(9 / 817)]


def test_usable_zero_day_lowers_the_pooled_weight():
    # An outlet that ran nothing on a complete active day is measured, not skipped.
    with_zero = coverage(
        "x",
        {D1: 4},
        {D1: (400, True), D2: (400, True)},
        [D1, D2],
        baseline=None,
        min_denominator=FLOOR,
    )
    without = coverage("x", {D1: 4}, {D1: (400, True)}, [D1], baseline=None, min_denominator=FLOOR)
    assert without.weight == pytest.approx(0.01)
    assert with_zero.weight == pytest.approx(0.005)
    assert with_zero.days_used == 2
    assert with_zero.days[1].n == 0 and with_zero.days[1].weight == 0.0


def test_only_usable_days_pool_and_the_ratio_is_reported():
    c = coverage(
        "x",
        {D1: 2, D2: 50, D3: 3},
        {D1: (200, True), D2: (60, False), D3: (300, True)},
        [D1, D2, D3],
        baseline=500.0,  # present, but exact days exist so it must not engage
        min_denominator=FLOOR,
    )
    assert c.basis == "exact"
    assert c.weight == pytest.approx(5 / 500)
    assert c.days_used == 2 and c.days_active == 3
    assert c.n == 55


def test_estimated_only_when_no_day_is_usable_and_a_baseline_exists():
    c = coverage(
        "x",
        {D1: 4, D2: 2},
        {D1: (100, False), D2: (90, False)},
        [D1, D2],
        baseline=300.0,
        min_denominator=FLOOR,
    )
    assert c.basis == "estimated"
    assert c.weight == pytest.approx(6 / (300 * 2))
    assert c.days_used == 0 and c.days_active == 2


def test_no_active_days_means_nothing_to_estimate():
    c = coverage("x", {}, {}, [], baseline=300.0, min_denominator=FLOOR)
    assert c.basis == "none" and c.weight is None and c.days == ()


def test_baseline_needs_enough_complete_days():
    assert baseline_of([326, 327], min_days=7, min_denominator=FLOOR) is None
    assert baseline_of(range(300, 307), min_days=7, min_denominator=FLOOR) == 303.0
    # Days under the floor do not count toward the seven and cannot drag the median.
    assert (
        baseline_of([5, 6, 300, 301, 302, 303, 304, 305], min_days=7, min_denominator=FLOOR) is None
    )
    assert (
        baseline_of([5, 300, 301, 302, 303, 304, 305, 306], min_days=7, min_denominator=FLOOR)
        == 303.0
    )


# ---- originality ---------------------------------------------------------------


def test_roles_follow_invariant_5():
    assert role_of({"dup_cluster_id": None}) == "alone"
    assert (
        role_of({"dup_cluster_id": 1, "is_cluster_origin": True, "origin_confident": True})
        == "first"
    )
    assert (
        role_of({"dup_cluster_id": 1, "is_cluster_origin": False, "origin_confident": True})
        == "follow"
    )
    # The origin of an indeterminate cluster gets no credit: nobody is "first".
    assert (
        role_of({"dup_cluster_id": 1, "is_cluster_origin": True, "origin_confident": False})
        == "unresolved"
    )


def test_two_originality_rates():
    rows = [
        {"outlet": "a", "dup_cluster_id": None},
        {"outlet": "a", "dup_cluster_id": 1, "is_cluster_origin": True, "origin_confident": True},
        {"outlet": "a", "dup_cluster_id": 2, "is_cluster_origin": False, "origin_confident": True},
        {"outlet": "a", "dup_cluster_id": 3, "is_cluster_origin": True, "origin_confident": False},
        {"outlet": "b", "dup_cluster_id": None},
    ]
    o = originality(rows)
    a = o["a"]
    assert (a.n, a.alone, a.first, a.follow, a.unresolved) == (4, 1, 1, 1, 1)
    assert a.original == pytest.approx(2 / 4)
    assert a.strict == pytest.approx(1 / 4)
    assert o["b"].original == 1.0 and o["b"].strict == 1.0
    assert originality([]) == {}


# ---- propagation -----------------------------------------------------------------

T0 = datetime(2030, 1, 1, 9, 12, tzinfo=UTC)


def _member(aid, outlet, minutes, published=True, **kw):
    at = T0 + timedelta(minutes=minutes)
    m = {
        "id": aid,
        "outlet": outlet,
        "title": f"t{aid}",
        "effective_at": at,
        "published_at": at if published else None,
        "cluster_rank": None,
        "delta_added": [],
        "delta_removed": [],
        "delta_summary": None,
    }
    m.update(kw)
    return m


def test_confident_cluster_has_origin_ranks_and_gaps():
    c = {
        "cluster_id": 1,
        "origin_confident": True,
        "shared_core_text": "core",
        "members": [
            _member(2, "ltn", 30, delta_added=["圖說"], delta_summary="＋ 圖說"),
            _member(1, "cna", 0),
        ],
    }
    v = cluster_view(c, {1})
    assert v.origin is not None and v.origin.outlet == "cna"
    assert [m.rank for m in v.members] == [1, 2]
    assert v.members[0].gap is None and v.members[1].gap == timedelta(minutes=30)
    assert v.members[1].delta_added == ("圖說",)
    assert v.members[0].matched is True and v.members[1].matched is False
    assert v.reason == ""
    assert v.first_at == T0


def test_indeterminate_cluster_claims_nothing():
    c = {
        "cluster_id": 3,
        "origin_confident": False,
        "shared_core_text": None,
        "members": [
            _member(3, "udn", 0, delta_removed=["stale directional row"]),
            _member(4, "udn", 1),
        ],
    }
    v = cluster_view(c, {3, 4})
    assert v.origin is None
    assert all(m.rank is None and m.gap is None for m in v.members)
    assert v.reason  # rebuilt from the members with dedup's rule
    assert "noise floor" in v.reason
    # T-009 stores no direction for these; a stale row must not leak one out.
    assert all(m.delta_removed == () for m in v.members)
    # Time order is still real information.
    assert [m.article_id for m in v.members] == [3, 4]


def test_indeterminate_reason_names_the_missing_publish_time():
    c = {
        "cluster_id": 5,
        "origin_confident": False,
        "shared_core_text": None,
        "members": [_member(5, "cna", 0), _member(6, "setn", 120, published=False)],
    }
    assert "no publish time for 6" in cluster_view(c, set()).reason


# ---- day bucketing -----------------------------------------------------------------


def test_days_are_taipei_not_utc():
    # 17:00Z is 01:00 the next day in Taipei.
    assert taipei_day(datetime(2030, 1, 1, 17, 0, tzinfo=UTC)) == date(2030, 1, 2)
    assert taipei_day(datetime(2030, 1, 1, 15, 59, tzinfo=UTC)) == date(2030, 1, 1)

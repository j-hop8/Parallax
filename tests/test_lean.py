"""Q4's suppression rule -- the only judgement metrics.lean makes.

The floor is invariant 7 moved from articles to posts, so the tests that matter
are the ones at its edge: n-1 suppresses, n does not, and neither case loses
the honest denominator a reader needs to see how much of the platform was read.
"""

from __future__ import annotations

import pytest

from parallax.metrics.lean import PLATFORMS, platform_lean


def _counts(posts=100, classified=40, neg=20, neu=10, pos=10):
    return {"posts": posts, "classified": classified, "neg": neg, "neu": neu, "pos": pos}


def test_distribution_is_shown_once_the_floor_is_reached():
    p = platform_lean("threads", _counts(classified=30, neg=15, neu=10, pos=5), min_posts=30)
    assert p.suppressed_reason is None
    assert not p.suppressed
    assert (p.neg, p.neu, p.pos) == (15, 10, 5)
    assert p.classified == 30
    assert p.min_posts == 30


def test_one_short_of_the_floor_is_suppressed():
    p = platform_lean("threads", _counts(classified=29, neg=15, neu=10, pos=4), min_posts=30)
    assert p.suppressed_reason == "below_floor"
    assert p.suppressed
    # The counts survive suppression: the renderers decide not to print them,
    # and a test of the renderer needs them present to prove that it did.
    assert (p.neg, p.neu, p.pos) == (15, 10, 4)
    assert p.posts == 100, "the denominator is still reported when the split is not"


@pytest.mark.parametrize("floor", [1, 2, 30, 500])
def test_the_edge_is_the_floor_wherever_it_is_set(floor):
    assert platform_lean("threads", _counts(classified=floor - 1), min_posts=floor).suppressed
    assert not platform_lean("threads", _counts(classified=floor), min_posts=floor).suppressed


def test_no_posts_and_unclassified_are_different_reasons():
    """A platform nobody fetched and a platform nobody classified are different
    failures with different fixes, and the panel must not say one for the other."""
    none_at_all = platform_lean("threads", _counts(posts=0, classified=0), min_posts=30)
    assert none_at_all.suppressed_reason == "no_posts"

    fetched = platform_lean("threads", _counts(posts=250, classified=0), min_posts=30)
    assert fetched.suppressed_reason == "unclassified"
    assert fetched.posts == 250


def test_no_posts_wins_over_the_floor():
    """posts == 0 also satisfies classified < floor; the reason must be the
    specific one, or the panel tells the reader to raise a sample that is not there."""
    assert platform_lean("threads", {}, min_posts=30).suppressed_reason == "no_posts"


def test_missing_and_null_counts_read_as_zero():
    """psycopg hands back whatever the aggregate produced; a None must not
    become a TypeError halfway through building a report."""
    p = platform_lean("threads", {"posts": None, "classified": None}, min_posts=30)
    assert (p.posts, p.classified, p.neg, p.neu, p.pos) == (0, 0, 0, 0, 0)
    assert p.suppressed_reason == "no_posts"


def test_facebook_is_not_a_live_platform():
    """It is a parked slot in the design, not a row of zeroes: there is no
    compliant read path, and 'no posts matched' would misdescribe that."""
    assert PLATFORMS == ("threads",)

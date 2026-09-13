"""Fingerprints, verdicts, clusters and the origin rules -- all offline.

The numbers in the verdict tests come from the corpus prototype recorded in
the T-008 ticket: true copies have containment >= 0.9, "same press release
quoted at length" sits around 0.7 with low Jaccard, and a teaser buried in a
long article has containment 1.0 but tiny Jaccard.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from parallax.nlp import dedup as d
from parallax.nlp.dedup import (
    Cluster,
    Member,
    PairScore,
    bands,
    bigrams,
    boilerplate,
    build_cluster,
    candidate_pairs,
    components,
    fingerprint,
    from_signed,
    hamming,
    is_duplicate,
    score_pair,
    simhash,
    strip,
    to_signed,
)

T0 = datetime(2026, 8, 16, 2, 14, tzinfo=UTC)


def _seg(words: list[str]) -> str:
    return " ".join(words)


def _words(n: int, prefix: str) -> list[str]:
    return [f"{prefix}{i:03d}" for i in range(n)]


# ---- storage encoding ------------------------------------------------------


@pytest.mark.parametrize("v", [0, 1, (1 << 63) - 1, 1 << 63, (1 << 64) - 1])
def test_signed_round_trip_including_the_2_63_boundary(v):
    s = to_signed(v)
    assert -(1 << 63) <= s < (1 << 63), "must fit a BIGINT"
    assert from_signed(s) == v


def test_signed_rejects_out_of_range():
    with pytest.raises(ValueError):
        to_signed(1 << 64)
    with pytest.raises(ValueError):
        from_signed(1 << 63)


# ---- features and fingerprint ---------------------------------------------


def test_bigrams_drop_single_char_tokens_but_keep_numbers():
    feats = bigrams("勞動部 今天 公布 最新 減班 休息 共 181 家 、 2406 人")
    assert ("勞動部", "今天") in feats
    assert ("減班", "休息") in feats
    assert ("休息", "181") in feats, "single-char 共 dropped, number kept"
    assert not any("、" in g for g in feats)
    assert bigrams("") == Counter() and bigrams(None) == Counter()


def test_boilerplate_is_per_outlet_and_needs_min_docs():
    promo = Counter({("不用", "現在"): 1, ("現在", "APP"): 1})

    def story(i):
        return Counter({(f"w{i}", f"x{i}"): 1})

    ltn = [story(i) + promo for i in range(10)]
    udn = [story(i) + promo for i in range(2)]  # only 2 docs: below min_docs
    drop = boilerplate({"ltn": ltn, "udn": udn})
    assert drop["ltn"] == {("不用", "現在"), ("現在", "APP")}
    assert drop["udn"] == set(), "two docs cannot establish boilerplate"
    assert strip(ltn[0], drop["ltn"]) == story(0)


def test_simhash_is_stable_close_for_a_small_edit_and_far_for_unrelated():
    a = _words(120, "a")
    b = a[:]
    b[60] = "changed"
    c = _words(120, "c")
    ha, hb, hc = (simhash(bigrams(_seg(x))) for x in (a, b, c))
    assert ha == simhash(bigrams(_seg(a)))
    assert hamming(ha, hb) <= 6, "one changed token moves at most a few bits"
    assert hamming(ha, hc) >= 20, "unrelated text is far"
    assert simhash(Counter()) == 0


def test_bands_are_the_four_16_bit_slices():
    h = 0x1234_5678_9ABC_DEF0
    assert bands(h) == (0xDEF0, 0x9ABC, 0x5678, 0x1234)
    assert all(0 <= b <= 0xFFFF for b in bands((1 << 64) - 1))


def test_fingerprint_marks_too_short_after_boilerplate_removal():
    promo = {("不用", "現在"), ("現在", "APP")}
    fp = fingerprint(1, "ltn", "不用 現在 APP 標題 這裡", promo)
    assert fp.too_short and fp.n_features < d.MIN_FEATURES
    long_fp = fingerprint(2, "ltn", _seg(_words(80, "w")), promo)
    assert not long_fp.too_short and long_fp.bands == bands(long_fp.simhash)


# ---- verdict ---------------------------------------------------------------


def _fp(i, outlet, words):
    return fingerprint(i, outlet, _seg(words), set())


def test_wire_copy_with_an_appended_paragraph_is_a_duplicate():
    wire = _words(100, "w")
    reprint = wire + _words(25, "extra")  # ltn added a paragraph
    s = score_pair(_fp(1, "cna", wire), _fp(2, "ltn", reprint))
    assert s.containment >= 0.95 and s.jaccard >= 0.75
    assert is_duplicate(s)


def test_same_quote_in_two_different_articles_is_not_a_duplicate():
    quote = _words(40, "q")
    a = _words(60, "a") + quote
    b = _words(70, "b") + quote
    s = score_pair(_fp(1, "ftv", a), _fp(2, "udn", b))
    assert 0.3 < s.containment < 0.85 and s.jaccard < 0.5
    assert not is_duplicate(s)


def test_teaser_contained_in_a_long_article_is_not_a_duplicate():
    long_article = _words(400, "w")
    teaser = long_article[:40]  # containment 1.0, tiny jaccard
    s = score_pair(_fp(1, "ltn", long_article), _fp(2, "ltn", teaser))
    assert s.containment == pytest.approx(1.0) and s.jaccard < 0.15
    assert not is_duplicate(s), "the Jaccard floor must stop this"


def test_thresholds_are_parameters():
    s = PairScore(1, 2, hamming=5, containment=0.7, jaccard=0.45)
    assert not is_duplicate(s)
    assert is_duplicate(s, containment=0.6, jaccard=0.4)


def test_candidate_pairs_apply_the_hamming_prefilter_and_skip_too_short():
    base = _words(150, "w")
    fps = [
        _fp(1, "cna", base),
        _fp(2, "ltn", base + _words(20, "x")),  # near
        _fp(3, "udn", _words(150, "z")),  # far
        fingerprint(4, "ltn", "只有 一點 文字", set()),  # too short
    ]
    scored = list(candidate_pairs(fps, hamming_max=12))
    ids = {(s.a, s.b) for s in scored}
    assert (1, 2) in ids
    assert all(4 not in p for p in ids), "too-short fingerprints never pair"
    assert all(s.hamming <= 12 for s in scored)
    assert (1, 3) not in ids


# ---- clusters --------------------------------------------------------------


def test_components_are_unions_of_edges_of_size_at_least_two():
    comps = components([(1, 2), (2, 3), (10, 11)])
    assert sorted(sorted(c) for c in comps) == [[1, 2, 3], [10, 11]]
    assert components([]) == []


def _m(i, outlet, minutes, stamped=True):
    t = T0 + timedelta(minutes=minutes)
    return Member(i, outlet, t, t if stamped else None)


def test_cluster_id_is_the_min_member_and_rank_follows_time():
    c = build_cluster([_m(200, "ltn", 170), _m(57, "cna", 0), _m(300, "setn", 40)])
    assert c.cluster_id == 57
    assert [m.article_id for m in c.members] == [57, 300, 200]
    assert c.origin.outlet == "cna" and c.first_published_at == T0
    assert c.origin_confident and c.reason == ""


def test_gap_inside_the_noise_floor_is_not_confident():
    c = build_cluster([_m(1, "cna", 0), _m(2, "ltn", 4)])
    assert not c.origin_confident and "noise floor" in c.reason
    assert build_cluster([_m(1, "cna", 0), _m(2, "ltn", 5)]).origin_confident


def test_a_member_without_a_publish_time_is_not_confident():
    """T-005: an unrecovered timestamp is a 20-minute poll time, not an order."""
    c = build_cluster([_m(1, "cna", 0), _m(2, "ltn", 170), _m(3, "udn", 400, stamped=False)])
    assert not c.origin_confident and "3" in c.reason and "poll" in c.reason


def test_first_two_from_the_same_outlet_is_not_confident():
    c = build_cluster([_m(1, "udn", 0), _m(2, "udn", 21), _m(3, "ltn", 90)])
    assert not c.origin_confident and "both udn" in c.reason


def test_a_cluster_needs_two_members():
    with pytest.raises(ValueError):
        build_cluster([_m(1, "cna", 0)])


def test_cluster_is_frozen_and_reports_members_in_order():
    c = build_cluster([_m(2, "ltn", 10), _m(1, "cna", 0)])
    assert isinstance(c, Cluster)
    with pytest.raises(AttributeError):
        c.cluster_id = 9  # type: ignore[misc]

"""nlp.framing: sentence units, matching, core and deltas -- all pure.

The shapes here are the live ones from the 2026-09-19 prototype: a CNA
dateline in front of the lede, a credit line after the last sentence, an ltn
caption ending in （路透）, setn's ● sub-heads, a udn copy that drops the space
in an English name.
"""

from __future__ import annotations

import pytest

from parallax.nlp.framing import (
    MemberText,
    Unit,
    boilerplate,
    diff_cluster,
    display,
    is_credit,
    normalize,
    pair_edits,
    same,
    sentences,
    units,
)

LEDE = "在貿易談判於最後一刻破裂後，美國總統川普對數十種加拿大進口商品徵收的50%關稅於今天正式生效。"
S2 = "美聯社報導，新關稅措施預計將影響加拿大每年輸往美國出口額約5，涉及商品金額約200億美元。"
S3 = "加拿大總理卡尼（Mark Carney）今天迅速承諾，將自9月8日起實施「等額對等」的反制措施。"
S4 = "這項關稅無需經過任何調查即可開徵，且無實施期限上限。"
PROMO = "下載中央社「一手新聞」APP，即時掌握最新消息"

CNA = f"（中央社芝加哥22日綜合外電報導）{LEDE}\n{S2}\n{S3}\n{S4}（編譯：陳昱婷）1150823\n{PROMO}"
UDN = f"{LEDE}\n{S2}\n{S3.replace('Mark Carney', 'MarkCarney')}\n{S4}"
SETN = f"{LEDE}\n{S2}\n● 加拿大是否正在採取報復行動？\n{S3}\n{S4}（中央社芝加哥22日電）"
LTN = f"{LEDE}（路透）\n{S2}\n請繼續往下閱讀...\n{S3}"  # dropped S4, added a caption tag


# ---- sentences and keys ----------------------------------------------------


def test_sentences_split_on_terminators_and_paragraphs_and_keep_closing_quotes():
    body = "他說：「我們需要重建家園。」記者追問。\n第二段沒有句號"
    assert sentences(body) == ["他說：「我們需要重建家園。」", "記者追問。", "第二段沒有句號"]
    assert sentences("") == [] and sentences(None) == []


def test_normalize_strips_datelines_credits_whitespace_and_punctuation_width():
    assert normalize(f"（中央社芝加哥22日綜合外電報導）{LEDE}") == normalize(LEDE)
    assert normalize(f"{S4}（編譯：陳昱婷）") == normalize(S4)
    assert normalize("卡尼（Mark Carney）今天") == normalize("卡尼（MarkCarney）今天")
    assert normalize("Ａ，Ｂ") == normalize("A,B")  # NFKC folds width


def test_display_keeps_wording_but_drops_edge_brackets_only():
    assert display(f"（中央社芝加哥22日綜合外電報導）{LEDE}") == LEDE
    assert display(f"{LEDE}（路透）") == LEDE
    assert display(S3) == S3  # an inner bracket is content


@pytest.mark.parametrize(
    "key,credit",
    [
        ("1150823", True),  # a CNA story id
        ("記者詹詠淇/台北報導", True),
        ("即時中心/巫彥輝報導", True),
        ("●加拿大是否正在採取報復行動", False),
        ("美加談判破局", False),
    ],
)
def test_is_credit(key, credit):
    assert is_credit(normalize(key)) is credit


def test_same_is_exact_or_near_and_the_length_precheck_is_safe():
    a = normalize(S2)
    assert same(a, a)
    assert same(a, a.replace("200億", "２００億"))  # width only
    assert same(a, a[:-3] + "美金")  # a three-character edit on a long sentence
    assert not same(a, normalize(S4))
    assert not same(a, a[: len(a) // 2])  # a teaser that is half the sentence
    assert not same("", a)


def test_units_drop_credits_boilerplate_and_repeats_and_show_clean_text():
    got = units(CNA, drop={normalize(PROMO)})
    assert [u.text for u in got] == [LEDE, S2, S3, S4]
    assert units(f"{LEDE}\n{LEDE}") == [Unit(normalize(LEDE), LEDE)]


def test_boilerplate_is_per_outlet_document_frequency():
    a = [f"{PROMO}\n{S2}", f"{PROMO}\n{S3}", f"{PROMO}\n{S4}", S2]
    b = [f"{PROMO}\n{S4}"]
    drop = boilerplate({"cna": a, "udn": b})
    assert drop["cna"] == {normalize(PROMO)}  # 3 of 4; S2 at 2 of 4 is news
    assert drop["udn"] == set()  # one article cannot make furniture


# ---- diff ------------------------------------------------------------------


def _member(aid, outlet, body, drop=frozenset()):
    return MemberText(aid, outlet, f"title {aid}", tuple(units(body, drop)))


def test_confident_cluster_diffs_every_follower_against_the_origin():
    f = diff_cluster(
        [
            _member(1, "cna", CNA, {normalize(PROMO)}),
            _member(2, "udn", UDN),
            _member(3, "setn", SETN),
            _member(4, "ltn", LTN, {normalize("請繼續往下閱讀...")}),
        ],
        origin_confident=True,
    )
    assert f.cluster_id == 1 and f.directional
    # Core: what >= 2 of 4 share, in the origin's wording and order, no dateline.
    assert f.core == (LEDE, S2, S3, S4)
    origin, udn, setn, ltn = f.deltas
    assert origin.empty
    assert udn.empty  # MarkCarney without the space is the same sentence
    assert setn.added == ("● 加拿大是否正在採取報復行動？",) and setn.removed == ()
    assert ltn.added == () and ltn.removed == (S4,)  # the （路透） tag is not an addition


def test_indeterminate_cluster_is_symmetric_and_never_says_removed():
    f = diff_cluster(
        [_member(7, "udn", UDN), _member(8, "udn", SETN)],
        origin_confident=False,
    )
    assert f.cluster_id == 7 and not f.directional
    assert f.core == (LEDE, S2, S3.replace("Mark Carney", "MarkCarney"), S4)  # rank-1 wording
    a, b = f.deltas
    assert a.added == () and a.removed == ()
    assert b.added == ("● 加拿大是否正在採取報復行動？",) and b.removed == ()


def test_core_needs_a_majority_so_a_sentence_two_of_five_share_is_not_core():
    body_a = f"{LEDE}\n{S2}"
    body_b = f"{LEDE}\n{S3}"
    members = [_member(i, "x", body_a if i % 2 else body_b) for i in range(1, 6)]
    f = diff_cluster(members, origin_confident=False)
    # LEDE in 5/5; S2 in members 1,3,5 (3 >= ceil(5/2)); S3 in 2,4 (2 < 3).
    assert f.core == (LEDE, S2)
    assert f.deltas[1].added == (S3,)


def test_empty_bodies_do_not_crash_and_a_singleton_is_rejected():
    f = diff_cluster([_member(1, "a", ""), _member(2, "b", LEDE)], origin_confident=True)
    assert f.core == () and f.deltas[1].added == (LEDE,)
    with pytest.raises(ValueError):
        diff_cluster([_member(1, "a", LEDE)], origin_confident=True)


def test_pair_edits_folds_a_rewording_and_leaves_the_rest():
    old = "賴清德總統昨天宣布「AI紅利、全民共享」，明年普發一萬元。"
    new = "總統賴清德昨（17）日宣布「AI紅利，全民共享」，明年普發1萬元。"
    edits, added, removed = pair_edits((new, "完全不同的一句新聞。"), (old, S4))
    assert edits == [(old, new)]
    assert added == ["完全不同的一句新聞。"] and removed == [S4]

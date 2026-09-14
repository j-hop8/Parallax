"""Pair gold set, the blind pair-labeling loop, the stratified sampler, and the eval."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parallax.jobs import eval_dedup as ev
from parallax.jobs.dedup import run_dedup
from parallax.nlp.gold import (
    PairGoldRow,
    append_pair_gold,
    load_pair_gold,
    pair_label_session,
    sentences,
)

T0 = datetime(2026, 8, 16, 2, 14, tzinfo=UTC)


def _words(n, prefix):
    return " ".join(f"{prefix}{i:03d}" for i in range(n))


def _row(i, outlet, seg, minutes=0, body=None):
    t = T0 + timedelta(minutes=minutes)
    return {
        "id": i,
        "outlet": outlet,
        "title": f"title {i}",
        "effective_at": t,
        "published_at": t,
        "body_seg": seg,
        "body": body or seg.replace(" ", ""),
    }


def _corpus():
    wire = _words(120, "w")
    quote = _words(40, "q")
    return [
        _row(1, "cna", wire),
        _row(2, "ltn", wire + " " + _words(20, "x"), 170),  # copy of 1
        _row(3, "udn", _words(60, "a") + " " + quote),  # shares a quote with 4
        _row(4, "ftv", _words(70, "b") + " " + quote),
        _row(5, "setn", _words(150, "z")),  # unrelated
        _row(6, "tvbs", _words(150, "y")),  # unrelated
        _row(7, "ltn", "只有 一點 文字"),  # too short
    ]


# ---- gold I/O ----------------------------------------------------------------


def test_pair_gold_round_trips_and_normalises_key_order(tmp_path):
    p = tmp_path / "dup_gold.csv"
    append_pair_gold(p, PairGoldRow(9, 3, True, "t", "now"))
    append_pair_gold(p, PairGoldRow(1, 2, False, "t", "now", "same quote only"))
    rows = load_pair_gold(p)
    assert [(r.key, r.is_duplicate) for r in rows] == [((3, 9), True), ((1, 2), False)]
    assert rows[1].note == "same quote only"
    assert (
        p.read_text(encoding="utf-8").splitlines()[0].startswith("article_a,article_b,is_duplicate")
    )


def test_pair_gold_rejects_a_bad_flag(tmp_path):
    p = tmp_path / "dup_gold.csv"
    p.write_text("article_a,article_b,is_duplicate,annotator,labeled_at,note\n1,2,maybe,t,now,\n")
    with pytest.raises(ValueError):
        load_pair_gold(p)


def test_sentences_split_on_cjk_punctuation_and_drop_fragments():
    s = sentences("第一句話夠長了吧。短。第二句話也夠長了！第三句同樣夠長嗎？")
    assert "第一句話夠長了吧" in s and "第二句話也夠長了" in s and "第三句同樣夠長嗎" in s
    assert "短" not in s, "fragments under 8 chars are not sentences"


# ---- labeling loop -----------------------------------------------------------


def test_pair_session_writes_immediately_shows_both_sides_and_never_a_score(tmp_path):
    rows = {r["id"]: r for r in _corpus()}
    out: list[str] = []
    keys = iter(["b", "y", "n", "q"])
    counts = pair_label_session(
        [(rows[1], rows[2]), (rows[3], rows[4]), (rows[5], rows[6])],
        annotator="t",
        gold_path=tmp_path / "g.csv",
        read=lambda _: next(keys),
        write=out.append,
        now=lambda: T0,
    )
    assert counts == {"dup": 1, "not": 1, "skipped": 0}
    gold = load_pair_gold(tmp_path / "g.csv")
    assert [(g.key, g.is_duplicate) for g in gold] == [((1, 2), True), ((3, 4), False)]
    text = "\n".join(out)
    assert "A. cna" in text and "B. ltn" in text and "shared sentences" in text
    assert "--- A ---" in text, "b must show more body"
    for word in ("containment", "jaccard", "hamming", "simhash", "0.8"):
        assert word not in text.lower()


# ---- sampler -----------------------------------------------------------------


def test_stratified_sample_covers_strata_and_skips_labeled_pairs():
    run = run_dedup(_corpus())
    sample = ev.stratified_sample(run, labeled={(1, 2)}, seed=1)
    keys = {(a, b) for a, b, _ in sample}
    assert (1, 2) not in keys, "already labeled"
    assert (3, 4) in keys, "the shares-a-quote pair sits in a middle stratum"
    assert all(a < b for a, b, _ in sample)
    assert all(7 not in (a, b) for a, b, _ in sample), "too-short never offered"
    strata = {s for _, _, s in sample}
    assert any(s.startswith("0.00") for s in strata), "random low stratum is filled"
    assert ev.stratified_sample(run, labeled=keys | {(1, 2)}, seed=1) == []


# ---- eval --------------------------------------------------------------------


def test_prf_by_hand():
    gold = [
        PairGoldRow(1, 2, True, "t", ""),
        PairGoldRow(3, 4, False, "t", ""),
        PairGoldRow(5, 6, True, "t", ""),
        PairGoldRow(7, 8, False, "t", ""),
    ]
    from parallax.nlp.dedup import PairScore

    s = PairScore(0, 0, 0, 0.0, 0.0)
    preds = {(1, 2): (True, s), (3, 4): (True, s), (5, 6): (False, s)}  # (7,8) unscored
    r = ev.prf(gold, preds)
    assert (r["tp"], r["fp"], r["fn"], r["tn"]) == (1, 1, 1, 0)
    assert r["precision"] == 0.5 and r["recall"] == 0.5 and r["f1"] == 0.5


def test_evaluate_reports_unscorable_and_per_stratum_and_sweep_finds_the_operating_point():
    run = run_dedup(_corpus())
    gold = [
        PairGoldRow(1, 2, True, "claude-opus-5", ""),
        PairGoldRow(3, 4, False, "claude-opus-5", ""),
        PairGoldRow(5, 6, False, "claude-opus-5", ""),
        PairGoldRow(6, 7, False, "claude-opus-5", ""),  # 7 is too short
    ]
    r = ev.evaluate(run, gold, containment=0.85, jaccard=0.5)
    assert r["n"] == 3 and r["unscorable"] == [(6, 7)]
    assert r["precision"] == 1.0 and r["recall"] == 1.0
    assert r["gold_positive"] == 1 and "claude-opus-5" in r["annotators"]
    text = ev.render(r, ev.sweep(run, gold))
    assert "inter-model agreement" in text and "precision 1.000" in text and "best F1" in text
    # A threshold loose enough to call the shared-quote pair a duplicate costs precision.
    loose = ev.evaluate(run, gold, containment=0.3, jaccard=0.2)
    assert loose["fp"] >= 1 and loose["precision"] < 1.0

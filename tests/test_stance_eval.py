"""Metric arithmetic, checked by hand.

Macro-F1 is the number the T-007 milestone is judged on. If it is computed
wrongly, the project either ships an approach that does not work or discards
one that does, so each case below is small enough to verify on paper.
"""

from __future__ import annotations

import pytest

from parallax.nlp.eval import (
    accuracy,
    agreement,
    cohens_kappa,
    confusion,
    macro_f1,
    per_class,
)


def test_perfect_predictions_score_one():
    y = ["neg", "neu", "pos", "neu", "neg"]
    assert macro_f1(y, y) == 1.0
    assert accuracy(y, y) == 1.0
    assert all(s.f1 == 1.0 for s in per_class(y, y))


def test_hand_computed_three_class_case():
    # gold:  neg neg neu neu pos pos
    # pred:  neg neu neu neu pos neg
    y_true = ["neg", "neg", "neu", "neu", "pos", "pos"]
    y_pred = ["neg", "neu", "neu", "neu", "pos", "neg"]
    # neg: tp=1 fp=1 fn=1 -> P=.5 R=.5 F1=.5
    # neu: tp=2 fp=1 fn=0 -> P=2/3 R=1 F1=.8
    # pos: tp=1 fp=0 fn=1 -> P=1 R=.5 F1=2/3
    scores = {s.label: s for s in per_class(y_true, y_pred)}
    assert scores["neg"].f1 == pytest.approx(0.5)
    assert scores["neu"].f1 == pytest.approx(0.8)
    assert scores["pos"].f1 == pytest.approx(2 / 3)
    assert scores["neg"].support == 2 and scores["neu"].support == 2
    assert macro_f1(y_true, y_pred) == pytest.approx((0.5 + 0.8 + 2 / 3) / 3)
    assert accuracy(y_true, y_pred) == pytest.approx(4 / 6)

    c = confusion(y_true, y_pred)
    assert c["neg"] == {"neg": 1, "neu": 1, "pos": 0}
    assert c["pos"] == {"neg": 1, "neu": 0, "pos": 1}


def test_all_neutral_classifier_is_punished_not_rewarded():
    """The failure mode the proposal names: document sentiment says 'neu' to everything.

    Accuracy would flatter it on a mostly-neutral set; macro-F1 must not.
    """
    y_true = ["neu"] * 8 + ["neg", "pos"]
    y_pred = ["neu"] * 10
    assert accuracy(y_true, y_pred) == 0.8
    # neu F1 = 2*(.8*1)/(1.8) = 0.888..; neg and pos never predicted -> 0, not NaN.
    assert macro_f1(y_true, y_pred) == pytest.approx(0.8889 / 3, abs=1e-3)
    scores = {s.label: s for s in per_class(y_true, y_pred)}
    assert scores["neg"].precision == 0.0 and scores["neg"].f1 == 0.0
    assert scores["pos"].precision == 0.0 and scores["pos"].f1 == 0.0


def test_length_mismatch_is_an_error_not_a_silent_truncation():
    with pytest.raises(ValueError):
        confusion(["neg", "pos"], ["neg"])


def test_empty_input_does_not_divide_by_zero():
    assert accuracy([], []) == 0.0
    assert macro_f1([], []) == 0.0


def test_macro_f1_averages_over_labels_present_not_all_three():
    """A gold set with no `pos` rows yet must not carry a phantom zero for pos."""
    assert macro_f1(["neg", "neu"], ["neg", "neu"]) == 1.0
    # ...but a class the model *predicted* wrongly does count, even if gold lacks it.
    assert macro_f1(["neg", "neu"], ["neg", "pos"]) == pytest.approx((1.0 + 0.0 + 0.0) / 3)


# ---- annotator agreement (T-020) -------------------------------------------


def test_cohens_kappa_matches_a_hand_computed_case():
    """The textbook 2x2: 50 items, 35 agreed, marginals .50/.50 and .60/.40.

    observed = 35/50 = .70
    expected = .50*.60 + .50*.40 = .30 + .20 = .50
    kappa    = (.70 - .50) / (1 - .50) = .40
    """
    a = ["pos"] * 20 + ["pos"] * 5 + ["neg"] * 15 + ["neg"] * 10
    b = ["pos"] * 20 + ["neg"] * 5 + ["neg"] * 15 + ["pos"] * 10
    assert cohens_kappa(a, b) == pytest.approx(0.40)


def test_kappa_is_symmetric():
    a = ["neg", "neu", "pos", "neu", "neg"]
    b = ["neg", "neu", "neu", "neu", "pos"]
    assert cohens_kappa(a, b) == pytest.approx(cohens_kappa(b, a))


def test_high_raw_agreement_on_a_skewed_set_is_not_high_kappa():
    """The reason kappa exists here at all.

    Both annotators call 90% of a set neutral, so they agree 80% of the time --
    a number that reads like validation. But they never once agree on which
    items are negative, and chance alone would have produced 82% agreement on
    marginals this skewed. Kappa is slightly negative: worse than guessing.

      observed = 40/50 = .80
      expected = .9*.9 + .1*.1 = .82
      kappa    = (.80 - .82) / (1 - .82) = -0.111...
    """
    a = ["neu"] * 40 + ["neu"] * 5 + ["neg"] * 5
    b = ["neu"] * 40 + ["neg"] * 5 + ["neu"] * 5
    assert accuracy(a, b) == pytest.approx(0.80), "raw agreement looks reassuring"
    assert cohens_kappa(a, b) == pytest.approx(-0.02 / 0.18)
    assert cohens_kappa(a, b) < 0, "and kappa says it is no better than chance"


def test_perfect_agreement_on_one_label_returns_one_not_a_zero_division():
    """Degenerate: both used a single label and it matched, so expected
    agreement is 1 and kappa is 0/0. Reported as 1.0 -- n travels with it."""
    assert cohens_kappa(["neu"] * 10, ["neu"] * 10) == 1.0


def test_total_disagreement_is_negative():
    assert cohens_kappa(["neg"] * 5 + ["pos"] * 5, ["pos"] * 5 + ["neg"] * 5) == pytest.approx(-1.0)


def test_kappa_length_mismatch_is_an_error_not_a_silent_truncation():
    with pytest.raises(ValueError, match="length mismatch"):
        cohens_kappa(["neg"], ["neg", "pos"])


def test_agreement_joins_on_the_item_never_on_csv_order():
    """Two annotators label overlapping but different subsets in whatever order
    they were offered. Zipping the two orderings would compare unrelated
    articles and report a confident, meaningless number."""
    human = {1: "neg", 2: "neu", 3: "pos", 4: "neg"}
    machine = {3: "pos", 2: "neu", 9: "neg"}  # different order, only 2 and 3 shared
    ag = agreement("jimmy", human, "claude-opus-5", machine)
    assert ag.n == 2
    assert ag.observed == 1.0
    assert (ag.a, ag.b) == ("jimmy", "claude-opus-5")
    assert ag.confusion["neu"]["neu"] == 1 and ag.confusion["pos"]["pos"] == 1


def test_agreement_on_an_empty_overlap_does_not_divide_by_zero():
    ag = agreement("jimmy", {1: "neg"}, "claude-opus-5", {2: "neg"})
    assert (ag.n, ag.observed, ag.kappa) == (0, 0.0, 0.0)
    assert ag.confusion == {}

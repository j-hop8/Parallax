"""Metric arithmetic, checked by hand.

Macro-F1 is the number the T-007 milestone is judged on. If it is computed
wrongly, the project either ships an approach that does not work or discards
one that does, so each case below is small enough to verify on paper.
"""

from __future__ import annotations

import pytest

from parallax.nlp.eval import accuracy, confusion, macro_f1, per_class


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

"""Classification metrics for the stance gold set. Pure Python on purpose.

scikit-learn lives in the torch extra, and the eval must run on the crawler's
install. Three classes and a few hundred rows do not need a library; they need
numbers whose derivation a reviewer can check by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

LABELS: tuple[str, ...] = ("neg", "neu", "pos")


@dataclass(frozen=True)
class ClassScore:
    label: str
    precision: float
    recall: float
    f1: float
    support: int  # gold rows with this label


def confusion(
    y_true: Sequence[str], y_pred: Sequence[str], labels: Sequence[str] = LABELS
) -> dict[str, dict[str, int]]:
    """counts[gold][predicted]. Rows are what the human said, columns the model."""
    if len(y_true) != len(y_pred):
        raise ValueError(f"length mismatch: {len(y_true)} gold vs {len(y_pred)} predicted")
    counts = {g: dict.fromkeys(labels, 0) for g in labels}
    for g, p in zip(y_true, y_pred, strict=True):
        counts[g][p] += 1
    return counts


def per_class(
    y_true: Sequence[str], y_pred: Sequence[str], labels: Sequence[str] = LABELS
) -> list[ClassScore]:
    """Precision / recall / F1 per label.

    A label the model never predicted has precision 0, not NaN -- the case a
    flat classifier produces by calling everything "neu", which is exactly the
    failure the proposal warns about and this eval exists to surface.
    """
    counts = confusion(y_true, y_pred, labels)
    scores = []
    for label in labels:
        tp = counts[label][label]
        fn = sum(counts[label].values()) - tp
        fp = sum(counts[g][label] for g in labels) - tp
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        scores.append(ClassScore(label, precision, recall, f1, support=tp + fn))
    return scores


def macro_f1(y_true: Sequence[str], y_pred: Sequence[str], labels: Sequence[str] = LABELS) -> float:
    """Unweighted mean of per-class F1: the proposal's target metric (> 0.75).

    Unweighted so the rare classes count as much as "neu". Accuracy would let a
    classifier that only ever says neutral score well on a mostly-neutral set.
    """
    scores = per_class(y_true, y_pred, labels)
    return sum(s.f1 for s in scores) / len(scores)


def accuracy(y_true: Sequence[str], y_pred: Sequence[str]) -> float:
    if not y_true:
        return 0.0
    return sum(g == p for g, p in zip(y_true, y_pred, strict=True)) / len(y_true)

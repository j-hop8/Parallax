"""Classification metrics for the stance gold set. Pure Python on purpose.

scikit-learn lives in the torch extra, and the eval must run on the crawler's
install. Three classes and a few hundred rows do not need a library; they need
numbers whose derivation a reviewer can check by hand.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
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

    Averaged over the labels that occur in gold or predictions (scikit-learn's
    default). A label that appears in neither -- a gold set with no `pos` rows
    yet -- is not a zero to be averaged in; on the full three-class set the two
    definitions coincide.
    """
    present = [lab for lab in labels if lab in set(y_true) | set(y_pred)]
    if not present:
        return 0.0
    scores = per_class(y_true, y_pred, present)
    return sum(s.f1 for s in scores) / len(scores)


def accuracy(y_true: Sequence[str], y_pred: Sequence[str]) -> float:
    if not y_true:
        return 0.0
    return sum(g == p for g, p in zip(y_true, y_pred, strict=True)) / len(y_true)


# ---- annotator agreement (T-020) -------------------------------------------
#
# The functions above score a model against gold. These score two *annotators*
# against each other, which is a different question with a different statistic:
# raw agreement flatters a skewed set, because two annotators who both call
# almost everything "neu" agree constantly by accident. Cohen's kappa removes
# the agreement chance alone would produce.
#
# Why this exists: every gold row in this repo was written by claude-opus-5, so
# a macro-F1 against it measures two models agreeing, not correctness. Kappa
# between a human and that machine annotator on a shared subset is what says
# whether the machine labels may stand in for human ones (proposal section 9).


@dataclass(frozen=True)
class Agreement:
    a: str  # annotator name
    b: str
    n: int  # rows both labeled
    observed: float  # raw proportion agreed
    expected: float  # agreement chance alone would produce, from the marginals
    kappa: float
    confusion: dict[str, dict[str, int]]  # rows = a's label, cols = b's


def cohens_kappa(a: Sequence[str], b: Sequence[str], labels: Sequence[str] = LABELS) -> float:
    """Chance-corrected agreement between two annotators over the same items.

    (observed - expected) / (1 - expected), with expected from the product of
    the two annotators' marginals. Symmetric in a and b.

    Landis-Koch reads 0.0-0.20 slight, 0.21-0.40 fair, 0.41-0.60 moderate,
    0.61-0.80 substantial, 0.81-1.00 almost perfect. This project's bar is
    0.60, the same one the proposal sets for aspect labeling.

    Degenerate case: when both annotators used exactly one label and it was the
    same one, expected agreement is 1 and the ratio is 0/0. Return 1.0 rather
    than raising -- they agreed on every row, and while kappa genuinely cannot
    tell that apart from chance, reporting "undefined" for a perfect column is
    more misleading than reporting the agreement. `n` and the confusion matrix
    travel with every kappa this module reports, so the degeneracy is visible.
    """
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)} labels")
    if not a:
        return 0.0
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    expected = sum(
        (sum(x == lab for x in a) / n) * (sum(y == lab for y in b) / n) for lab in labels
    )
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1 - expected)


def agreement(
    a_name: str,
    a_labels: Mapping[Hashable, str],
    b_name: str,
    b_labels: Mapping[Hashable, str],
    labels: Sequence[str] = LABELS,
) -> Agreement:
    """Compare two annotators on the items they both labeled.

    Keyed rather than positional on purpose: two annotators label overlapping
    but different subsets, and zipping two CSV orderings would silently compare
    unrelated rows. The caller passes {key: label} and the intersection is what
    gets scored.
    """
    shared = sorted(set(a_labels) & set(b_labels), key=repr)
    ys = [a_labels[k] for k in shared]
    zs = [b_labels[k] for k in shared]
    n = len(shared)
    observed = (sum(y == z for y, z in zip(ys, zs, strict=True)) / n) if n else 0.0
    expected = (
        sum(
            (sum(y == lab for y in ys) / n) * (sum(z == lab for z in zs) / n) for lab in labels
        )
        if n
        else 0.0
    )
    return Agreement(
        a=a_name,
        b=b_name,
        n=n,
        observed=observed,
        expected=expected,
        kappa=cohens_kappa(ys, zs, labels) if n else 0.0,
        confusion=confusion(ys, zs, labels) if n else {},
    )

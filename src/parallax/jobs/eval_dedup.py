"""Score the clusterer against hand-labeled pairs (proposal §9: P > 0.90, R > 0.80).

    make dedup.eval                             # at the configured thresholds, plus a sweep
    make dedup.eval ARGS="--containment 0.8 --jaccard 0.4"

A prediction is what the *system* would do: the Hamming prefilter and both
thresholds together. The sweep varies the two thresholds so the operating
point is chosen from data rather than from the prototype's eyeballing. Pairs
whose bodies are too short to fingerprint are reported, not silently dropped.
Annotator provenance is printed beside the numbers, as in eval_stance.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime

from .. import db
from ..nlp.dedup import (
    CONTAINMENT_THRESHOLD,
    HAMMING_PREFILTER,
    JACCARD_THRESHOLD,
    PairScore,
    is_duplicate,
    score_pair,
)
from ..nlp.gold import PAIR_GOLD_PATH, PairGoldRow, load_pair_gold, pair_key
from ..settings import EVAL_DIR, ROOT
from .dedup import DedupRun, run_dedup

log = logging.getLogger(__name__)

TARGET_PRECISION = 0.90
TARGET_RECALL = 0.80
RUNS_DIR = EVAL_DIR / "runs"

# (containment low, high, how many to label). The boundary is where labels
# earn their keep, so the middle strata are oversampled relative to the corpus.
STRATA: tuple[tuple[float, float, int], ...] = (
    (0.85, 1.01, 50),
    (0.60, 0.85, 60),
    (0.30, 0.60, 50),
    (0.00, 0.30, 40),
)
SWEEP_CONTAINMENT = (0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)
SWEEP_JACCARD = (0.30, 0.40, 0.50, 0.60, 0.70)


def stratum_of(containment: float) -> str:
    for lo, hi, _ in STRATA:
        if lo <= containment < hi:
            return f"{lo:.2f}-{min(hi, 1.0):.2f}"
    return "?"


def all_candidate_scores(run: DedupRun) -> dict[tuple[int, int], PairScore]:
    """Every pair that passed the prefilter, keyed by (min id, max id)."""
    from ..nlp.dedup import candidate_pairs

    return {pair_key(s.a, s.b): s for s in candidate_pairs(run.fingerprints.values())}


def stratified_sample(
    run: DedupRun,
    labeled: set[tuple[int, int]],
    *,
    strata: tuple[tuple[float, float, int], ...] = STRATA,
    seed: int | None = None,
    random_draws: int = 5000,
) -> list[tuple[int, int, str]]:
    """Candidate pairs to label, bucketed by containment, never already labeled.

    The top three strata come from the prefiltered candidates; the bottom one
    is drawn from random pairs (most of the corpus lives there, and the
    prefilter never sees it), scored on the spot.
    """
    rng = random.Random(seed)
    scores = all_candidate_scores(run)
    usable = [f for f in run.fingerprints.values() if not f.too_short]

    # Fill the low stratum with random pairs scored directly.
    lo, hi, _ = strata[-1]
    tried = 0
    while tried < random_draws and len(usable) >= 2:
        fa, fb = rng.sample(usable, 2)
        k = pair_key(fa.article_id, fb.article_id)
        tried += 1
        if k in scores:
            continue
        s = score_pair(fa, fb)
        if lo <= s.containment < hi:
            scores[k] = s

    out: list[tuple[int, int, str]] = []
    for lo, hi, n in strata:
        pool = [k for k, s in scores.items() if lo <= s.containment < hi and k not in labeled]
        rng.shuffle(pool)
        out.extend((a, b, stratum_of(scores[(a, b)].containment)) for a, b in pool[:n])
    rng.shuffle(out)
    return out


def predictions(
    run: DedupRun,
    gold: list[PairGoldRow],
    *,
    containment: float,
    jaccard: float,
    hamming_max: int = HAMMING_PREFILTER,
) -> tuple[dict[tuple[int, int], tuple[bool, PairScore]], list[tuple[int, int]]]:
    """System verdict per gold pair; pairs that cannot be scored are listed."""
    out: dict[tuple[int, int], tuple[bool, PairScore]] = {}
    unscorable: list[tuple[int, int]] = []
    for g in gold:
        fa, fb = run.fingerprints.get(g.article_a), run.fingerprints.get(g.article_b)
        if fa is None or fb is None or fa.too_short or fb.too_short:
            unscorable.append(g.key)
            continue
        s = score_pair(fa, fb)
        verdict = s.hamming <= hamming_max and is_duplicate(
            s, containment=containment, jaccard=jaccard
        )
        out[g.key] = (verdict, s)
    return out, unscorable


def prf(gold: list[PairGoldRow], preds: dict[tuple[int, int], tuple[bool, PairScore]]) -> dict:
    tp = fp = fn = tn = 0
    for g in gold:
        if g.key not in preds:
            continue
        p = preds[g.key][0]
        if g.is_duplicate and p:
            tp += 1
        elif g.is_duplicate:
            fn += 1
        elif p:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def evaluate(run: DedupRun, gold: list[PairGoldRow], *, containment: float, jaccard: float) -> dict:
    preds, unscorable = predictions(run, gold, containment=containment, jaccard=jaccard)
    report = prf(gold, preds)
    per_stratum: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for g in gold:
        if g.key not in preds:
            continue
        verdict, s = preds[g.key]
        st = stratum_of(s.containment)
        per_stratum[st]["n"] += 1
        per_stratum[st]["gold_dup"] += int(g.is_duplicate)
        per_stratum[st]["correct"] += int(verdict == g.is_duplicate)
    report.update(
        {
            "n": len(preds),
            "n_unscorable": len(unscorable),
            "unscorable": unscorable,
            "gold_positive": sum(1 for g in gold if g.is_duplicate),
            "per_stratum": {k: dict(v) for k, v in sorted(per_stratum.items())},
            "annotators": dict(sorted(Counter(g.annotator for g in gold).items())),
            "thresholds": {"containment": containment, "jaccard": jaccard},
        }
    )
    return report


def sweep(run: DedupRun, gold: list[PairGoldRow]) -> list[dict]:
    rows = []
    for c in SWEEP_CONTAINMENT:
        for j in SWEEP_JACCARD:
            preds, _ = predictions(run, gold, containment=c, jaccard=j)
            rows.append({"containment": c, "jaccard": j, **prf(gold, preds)})
    return rows


def render(report: dict, sweep_rows: list[dict]) -> str:
    t = report["thresholds"]
    lines = [
        f"dedup eval  n={report['n']} pairs  gold positives={report['gold_positive']}"
        + (f"  (unscorable {report['n_unscorable']}: too short)" if report["n_unscorable"] else ""),
        "  gold labeled by: "
        + ", ".join(f"{a} ({n})" for a, n in report["annotators"].items())
        + (
            "   <- model-authored gold: this is inter-model agreement, not human validation"
            if any(
                a.startswith(("claude", "gemini", "gpt", "model:")) for a in report["annotators"]
            )
            else ""
        ),
        "",
        f"  at containment>={t['containment']:.2f}, jaccard>={t['jaccard']:.2f}:",
        f"  precision {report['precision']:.3f}  target > {TARGET_PRECISION:.2f}"
        + ("  ✓" if report["precision"] > TARGET_PRECISION else "")
        + f"     recall {report['recall']:.3f}  target > {TARGET_RECALL:.2f}"
        + ("  ✓" if report["recall"] > TARGET_RECALL else "")
        + f"     F1 {report['f1']:.3f}",
        f"  tp {report['tp']}  fp {report['fp']}  fn {report['fn']}  tn {report['tn']}",
        "",
        "  per containment stratum (n, gold dup, accuracy)",
    ]
    for st, v in report["per_stratum"].items():
        acc = v["correct"] / v["n"] if v["n"] else 0.0
        lines.append(f"  {st:<12}{v['n']:>5}{v['gold_dup']:>9}{acc:>10.2f}")
    lines += [
        "",
        "  sweep (P / R / F1)",
        "  cont\\jacc " + "".join(f"{j:>16.2f}" for j in SWEEP_JACCARD),
    ]
    by = {(r["containment"], r["jaccard"]): r for r in sweep_rows}
    for c in SWEEP_CONTAINMENT:
        cells = "".join(
            f"{by[(c, j)]['precision']:>6.2f}/{by[(c, j)]['recall']:.2f}/{by[(c, j)]['f1']:.2f}"
            for j in SWEEP_JACCARD
        )
        lines.append(f"  {c:<10.2f}{cells}")
    best = max(sweep_rows, key=lambda r: (r["f1"], r["precision"]))
    lines.append(
        f"\n  best F1 in sweep: containment>={best['containment']:.2f}, jaccard>={best['jaccard']:.2f}"
        f"  (P {best['precision']:.2f}, R {best['recall']:.2f}, F1 {best['f1']:.2f})"
    )
    if report["n"] < 50 or report["gold_positive"] < 10:
        lines.append(
            f"\n  n={report['n']}, positives={report['gold_positive']}: small; a direction, not a verdict."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score the clusterer against eval/dup_gold.csv.")
    parser.add_argument("--containment", type=float, default=CONTAINMENT_THRESHOLD)
    parser.add_argument("--jaccard", type=float, default=JACCARD_THRESHOLD)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    gold = load_pair_gold(PAIR_GOLD_PATH)
    if not gold:
        print("no gold pairs; run: make label.pairs")
        return 1
    with db.connect() as conn:
        rows = db.enriched_for_dedup(conn)
    run = run_dedup(rows)

    report = evaluate(run, gold, containment=args.containment, jaccard=args.jaccard)
    sweep_rows = sweep(run, gold)
    print(render(report, sweep_rows))

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RUNS_DIR / f"{stamp}-dedup.json"
    out.write_text(
        json.dumps({**report, "sweep": sweep_rows}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"\nwritten {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

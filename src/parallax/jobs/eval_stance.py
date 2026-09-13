"""Score the stance classifier against the hand-labeled gold set.

    make stance.eval                       # uses cached verdicts only; reports what is missing
    make stance.eval ARGS=--classify       # fetch missing verdicts first (spends quota)
    make stance.eval ARGS="--model X --prompt-version v2"

Reports Macro-F1 (the milestone metric, target > 0.75), per-class P/R/F1, the
confusion matrix and per-outlet accuracy, and writes the same to eval/runs/
so runs across models and prompt versions can be compared later. It reports;
it does not gate. Whether 0.75 has been reached is a milestone decision, not
a CI failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import UTC, datetime

from .. import db
from ..nlp.eval import LABELS, accuracy, confusion, macro_f1, per_class
from ..nlp.gold import GOLD_PATH, GoldRow, load_gold
from ..nlp.stance import PROMPT_VERSION, GeminiStance, StanceClassifier, stance_input
from ..settings import EVAL_DIR, ROOT, STANCE_MODEL, STANCE_RPM

log = logging.getLogger(__name__)

TARGET_MACRO_F1 = 0.75
RUNS_DIR = EVAL_DIR / "runs"


def evaluate(gold: list[GoldRow], predictions: dict[tuple[int, str], str]) -> dict:
    """Join gold rows to predictions keyed by (article_id, target) and score.

    Gold rows with no prediction are counted and reported, never silently
    dropped: an eval over the 60% of rows the model happened to answer is a
    different number from an eval over the set.
    """
    scored = [
        (g, predictions[(g.article_id, g.target)])
        for g in gold
        if (g.article_id, g.target) in predictions
    ]
    missing = [g for g in gold if (g.article_id, g.target) not in predictions]
    y_true = [g.label for g, _ in scored]
    y_pred = [p for _, p in scored]

    by_outlet: dict[str, list[bool]] = defaultdict(list)
    by_target: dict[str, int] = defaultdict(int)
    for g, p in scored:
        by_outlet[g.outlet].append(g.label == p)
        by_target[g.target] += 1

    return {
        "n": len(scored),
        "n_missing": len(missing),
        "missing": [(g.article_id, g.target) for g in missing],
        "macro_f1": macro_f1(y_true, y_pred) if scored else 0.0,
        "accuracy": accuracy(y_true, y_pred),
        "per_class": [s.__dict__ for s in per_class(y_true, y_pred)] if scored else [],
        "confusion": confusion(y_true, y_pred) if scored else {},
        "per_outlet": {
            o: {"n": len(v), "accuracy": sum(v) / len(v)} for o, v in sorted(by_outlet.items())
        },
        "per_target": dict(sorted(by_target.items())),
        "gold_distribution": {lab: sum(1 for g in gold if g.label == lab) for lab in LABELS},
    }


def render(report: dict, *, model: str, prompt_version: str) -> str:
    lines = [
        f"stance eval  model={model}  prompt={prompt_version}  n={report['n']}"
        + (
            f"  (missing {report['n_missing']} -- run with --classify)"
            if report["n_missing"]
            else ""
        ),
        "",
        f"  macro-F1  {report['macro_f1']:.3f}   target > {TARGET_MACRO_F1:.2f}"
        + ("  ✓" if report["macro_f1"] > TARGET_MACRO_F1 else "")
        + f"     accuracy {report['accuracy']:.3f}",
        "",
        f"  {'class':<6}{'P':>7}{'R':>7}{'F1':>7}{'n':>6}",
    ]
    for s in report["per_class"]:
        lines.append(
            f"  {s['label']:<6}{s['precision']:>7.2f}{s['recall']:>7.2f}{s['f1']:>7.2f}{s['support']:>6}"
        )
    if report["confusion"]:
        lines += [
            "",
            "  confusion (rows = human, cols = model)",
            f"  {'':<6}" + "".join(f"{lab:>6}" for lab in LABELS),
        ]
        for g in LABELS:
            lines.append(f"  {g:<6}" + "".join(f"{report['confusion'][g][p]:>6}" for p in LABELS))
    if report["per_outlet"]:
        lines += ["", "  per outlet (accuracy, n)"]
        for o, v in report["per_outlet"].items():
            lines.append(f"  {o:<12}{v['accuracy']:>6.2f}{v['n']:>5}")
    if report["n"] < 50:
        lines += [
            "",
            f"  n={report['n']} is small; treat the numbers as a direction, not a verdict.",
        ]
    return "\n".join(lines)


def _fill_missing(conn, gold: list[GoldRow], classifier: StanceClassifier) -> int:
    """Classify gold rows that have no cached verdict for this model/prompt."""
    need = [
        g
        for g in gold
        if db.get_stance(conn, g.article_id, g.target, classifier.model, classifier.prompt_version)
        is None
    ]
    if not need:
        return 0
    rows = {r["id"]: r for r in db.articles_by_ids(conn, {g.article_id for g in need})}
    done = 0
    for g in need:
        row = rows.get(g.article_id)
        if row is None:
            log.warning("gold article %s has no body; enrich first", g.article_id)
            continue
        try:
            result = classifier.classify(stance_input(row, g.target))
            db.save_stance(
                conn,
                article_id=g.article_id,
                target=g.target,
                model=result.model,
                prompt_version=result.prompt_version,
                label=result.label,
                confidence=result.confidence,
                evidence=result.evidence,
            )
            conn.commit()
            done += 1
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            log.warning("could not classify gold article %s: %s", g.article_id, exc)
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score the stance classifier against eval/stance_gold.csv."
    )
    parser.add_argument("--model", default=STANCE_MODEL)
    parser.add_argument("--prompt-version", default=PROMPT_VERSION)
    parser.add_argument("--target", help="restrict to one target keyword")
    parser.add_argument(
        "--classify", action="store_true", help="classify gold rows with no verdict (spends quota)"
    )
    parser.add_argument("--rpm", type=float, default=STANCE_RPM)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    gold = load_gold(GOLD_PATH)
    if args.target:
        gold = [g for g in gold if g.target == args.target]
    if not gold:
        print(
            f"no gold rows{' for ' + args.target if args.target else ''}; run: make label KEYWORD=<target>"
        )
        return 1

    with db.connect() as conn:
        if args.classify:
            classifier = GeminiStance(model=args.model, rpm=args.rpm)
            if classifier.prompt_version != args.prompt_version:
                print(
                    f"--classify uses the current prompt {classifier.prompt_version}, not {args.prompt_version}"
                )
                return 2
            n = _fill_missing(conn, gold, classifier)
            log.info("classified %d gold rows", n)
        predictions = {}
        for g in gold:
            hit = db.get_stance(conn, g.article_id, g.target, args.model, args.prompt_version)
            if hit is not None:
                predictions[(g.article_id, g.target)] = hit["label"]

    report = evaluate(gold, predictions)
    print(render(report, model=args.model, prompt_version=args.prompt_version))

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RUNS_DIR / f"{stamp}-{args.model}-{args.prompt_version}.json"
    out.write_text(
        json.dumps(
            {"model": args.model, "prompt_version": args.prompt_version, **report},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"\nwritten {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

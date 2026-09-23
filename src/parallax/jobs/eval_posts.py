"""Score the stance classifier against the post gold set.

    make posts.eval                       # uses cached verdicts only; reports what is missing
    make posts.eval ARGS=--classify       # fetch missing verdicts first (spends quota)
    make posts.eval ARGS="--model X --prompt-version v2"

Reports Macro-F1 (the milestone metric, target > 0.75), per-class P/R/F1, the
confusion matrix and per-platform accuracy, and writes the same to eval/runs/
so runs across models and prompt versions can be compared later. It reports;
it does not gate. Whether 0.75 has been reached is a milestone decision, not
a CI failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime

from .. import db
from ..nlp.eval import LABELS, accuracy, confusion, macro_f1, per_class
from ..nlp.gold import POST_GOLD_PATH, PostGoldRow, load_post_gold
from ..nlp.stance import (
    POST_PROMPT_VERSION,
    DailyQuotaExhausted,
    GeminiPostStance,
    PostStanceClassifier,
    post_stance_input,
)
from ..settings import EVAL_DIR, ROOT, STANCE_MODEL, STANCE_RPM

log = logging.getLogger(__name__)

TARGET_MACRO_F1 = 0.75
RUNS_DIR = EVAL_DIR / "runs"
MODEL_PREFIXES = ("claude", "gemini", "gpt", "model:")


def evaluate(gold: list[PostGoldRow], predictions: dict[tuple[str, str], str]) -> dict:
    """Join gold rows to predictions keyed by (post_url, target) and score.

    Gold rows with no prediction are counted and reported, never silently
    dropped: an eval over the 60% of rows the model happened to answer is a
    different number from an eval over the set.
    """
    scored = [
        (g, predictions[(g.post_url, g.target)])
        for g in gold
        if (g.post_url, g.target) in predictions
    ]
    missing = [g for g in gold if (g.post_url, g.target) not in predictions]
    y_true = [g.label for g, _ in scored]
    y_pred = [p for _, p in scored]

    by_platform: dict[str, list[bool]] = defaultdict(list)
    by_target: dict[str, int] = defaultdict(int)
    for g, p in scored:
        by_platform[g.platform].append(g.label == p)
        by_target[g.target] += 1

    return {
        "n": len(scored),
        "n_missing": len(missing),
        "missing": [(g.post_url, g.target) for g in missing],
        "macro_f1": macro_f1(y_true, y_pred) if scored else 0.0,
        "accuracy": accuracy(y_true, y_pred),
        "per_class": [s.__dict__ for s in per_class(y_true, y_pred)] if scored else [],
        "confusion": confusion(y_true, y_pred) if scored else {},
        "per_platform": {
            o: {"n": len(v), "accuracy": sum(v) / len(v)} for o, v in sorted(by_platform.items())
        },
        "per_target": dict(sorted(by_target.items())),
        "gold_distribution": {lab: sum(1 for g in gold if g.label == lab) for lab in LABELS},
        # Who wrote the gold. Printed next to the F1 so the number is never read
        # without its provenance: against human labels it is validation, against
        # another model's labels it is inter-model agreement.
        "annotators": dict(sorted(Counter(g.annotator for g in gold).items())),
        "human_rows": sum(not g.annotator.startswith(MODEL_PREFIXES) for g in gold),
    }


def render(report: dict, *, model: str, prompt_version: str) -> str:
    lines = [
        f"post stance eval  model={model}  prompt={prompt_version}  n={report['n']}"
        + (
            f"  (missing {report['n_missing']} -- run with --classify)"
            if report["n_missing"]
            else ""
        ),
        "  gold labeled by: "
        + ", ".join(f"{a} ({n})" for a, n in report["annotators"].items())
        + (
            "   <- model-authored gold: this is inter-model agreement, not human validation"
            if any(a.startswith(MODEL_PREFIXES) for a in report["annotators"])
            else ""
        ),
        f"  human rows: {report['human_rows']} of 100 required before the Q4 caveat can be retired",
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
    if report["per_platform"]:
        lines += ["", "  per platform (accuracy, n)"]
        for o, v in report["per_platform"].items():
            lines.append(f"  {o:<12}{v['accuracy']:>6.2f}{v['n']:>5}")
    if report["missing"]:
        lines += ["", "  missing verdicts (post_url, target)"]
        for url, target in report["missing"]:
            lines.append(f"  {url}  target={target}")
    if report["n"] < 50:
        lines += [
            "",
            f"  n={report['n']} is small; treat the numbers as a direction, not a verdict.",
        ]
    return "\n".join(lines)


def _predictions(conn, gold: list[PostGoldRow], model: str, prompt_version: str) -> dict:
    predictions = {}
    urls_by_target: dict[str, set[str]] = defaultdict(set)
    for g in gold:
        urls_by_target[g.target].add(g.post_url)
    for target, urls in urls_by_target.items():
        for row in db.post_stance_for_urls(conn, urls, target, model, prompt_version):
            predictions[(row["post_url"], target)] = row["label"]
    return predictions


def _fill_missing(conn, gold: list[PostGoldRow], classifier: PostStanceClassifier) -> int:
    """Fill uncached URL/target pairs using current database ids, committing per row."""
    cached = _predictions(conn, gold, classifier.model, classifier.prompt_version)
    need = [g for g in gold if (g.post_url, g.target) not in cached]
    if not need:
        return 0
    rows = {
        r["post_url"]: r
        for r in conn.execute(
            "SELECT * FROM social_posts WHERE post_url = ANY(%s)",
            (sorted({g.post_url for g in need}),),
        ).fetchall()
    }
    done = 0
    for g in need:
        if (g.post_url, g.target) in cached:
            continue
        row = rows.get(g.post_url)
        if row is None or not (row.get("text") or "").strip():
            log.warning("gold post %s has no text; fetch first", g.post_url)
            continue
        try:
            result = classifier.classify(post_stance_input(row, g.target))
            db.save_post_stance(
                conn,
                post_id=row["id"],
                target=g.target,
                model=result.model,
                prompt_version=result.prompt_version,
                label=result.label,
                confidence=result.confidence,
                evidence=result.evidence,
            )
            conn.commit()
            cached[(g.post_url, g.target)] = result.label
            done += 1
        except DailyQuotaExhausted as exc:
            conn.rollback()
            log.error("%s -- scoring what is cached", exc)
            break
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            log.warning("could not classify gold post %s: %s", g.post_url, exc)
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score the stance classifier against eval/post_stance_gold.csv."
    )
    parser.add_argument("--model", default=STANCE_MODEL)
    parser.add_argument("--prompt-version", default=POST_PROMPT_VERSION)
    parser.add_argument("--target", help="restrict to one target keyword")
    parser.add_argument(
        "--classify", action="store_true", help="classify gold rows with no verdict (spends quota)"
    )
    parser.add_argument("--rpm", type=float, default=STANCE_RPM)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    gold = load_post_gold(POST_GOLD_PATH)
    if args.target:
        gold = [g for g in gold if g.target == args.target]
    if not gold:
        print("no post gold yet; run: make label.posts KEYWORD=<keyword>")
        return 1

    with db.connect() as conn:
        if args.classify:
            classifier = GeminiPostStance(model=args.model, rpm=args.rpm)
            if classifier.prompt_version != args.prompt_version:
                print(
                    f"--classify uses the current prompt {classifier.prompt_version}, not {args.prompt_version}"
                )
                return 2
            n = _fill_missing(conn, gold, classifier)
            log.info("classified %d gold rows", n)
        predictions = _predictions(conn, gold, args.model, args.prompt_version)

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

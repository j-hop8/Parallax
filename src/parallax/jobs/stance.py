"""Q1: classify one keyword's enriched articles for stance toward that keyword.

    make stance KEYWORD=沈伯洋 ARGS=--dry-run    # how many API calls it would make
    make stance KEYWORD=沈伯洋                   # make them; re-run makes zero

Keyword-scoped like enrich, and cached harder: one classifier call ever per
(article, target, model, prompt_version). This is where model quota is spent,
so it is a separate, explicit step rather than something enrich does on the
way past.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable

from .. import db
from ..nlp.stance import (
    PROMPT_VERSION,
    DailyQuotaExhausted,
    GeminiStance,
    StanceClassifier,
    stance_input,
)
from ..settings import STANCE_MODEL, STANCE_RPM

log = logging.getLogger(__name__)


def classify_keyword(
    keyword: str,
    classifier: StanceClassifier,
    *,
    limit: int = 500,
    dry_run: bool = False,
    connect: Callable | None = None,
) -> dict[str, int]:
    """Classify every enriched match that has no verdict yet. Returns counts.

    Each article is committed on its own and failures are isolated -- one bad
    response must not cost the batch, and a run interrupted by a rate limit
    resumes from where it stopped because everything before it is already
    saved. Counters move only after the commit (T-006b).
    """
    stats = {
        "matched": 0,
        "cached": 0,
        "planned": 0,
        "classified": 0,
        "failed": 0,
        "evidence_verbatim": 0,
        "quota_exhausted": 0,  # 1 when the run stopped early on a daily quota
    }
    # Resolved here, not in the signature: a default bound at import time would
    # pin the original db.connect and quietly ignore anything patched over it.
    with (connect or db.connect)() as conn:
        rows = db.find_enriched_articles(conn, keyword, limit=limit)
        stats["matched"] = len(rows)

        todo = []
        for row in rows:
            cached = db.get_stance(
                conn, row["id"], keyword, classifier.model, classifier.prompt_version
            )
            if cached is None:
                todo.append(row)
            else:
                stats["cached"] += 1
        stats["planned"] = len(todo)
        if dry_run:
            return stats

        for row in todo:
            try:
                result = classifier.classify(stance_input(row, keyword))
                db.save_stance(
                    conn,
                    article_id=row["id"],
                    target=keyword,
                    model=result.model,
                    prompt_version=result.prompt_version,
                    label=result.label,
                    confidence=result.confidence,
                    evidence=result.evidence,
                )
                conn.commit()
            except DailyQuotaExhausted as exc:
                # Not an article failure: nothing was cached, so the remaining
                # rows are simply still pending. Stop instead of failing each.
                conn.rollback()
                stats["quota_exhausted"] = 1
                log.error(
                    "%s -- %d article(s) left unclassified",
                    exc,
                    stats["planned"] - stats["classified"] - stats["failed"],
                )
                break
            except Exception as exc:  # noqa: BLE001 -- isolation is the point
                conn.rollback()
                stats["failed"] += 1
                log.warning("stance failed for %s %s: %s", row["outlet"], row["id"], exc)
                continue

            stats["classified"] += 1
            # The evidence phrase is what makes a label auditable; a phrase that
            # is not in the article is a hallucinated justification, and the
            # rate of those is worth watching per model.
            text = f"{row.get('title') or ''}\n{row.get('body') or ''}"
            if result.evidence and result.evidence in text:
                stats["evidence_verbatim"] += 1
    return stats


def distribution_table(rows: list[dict]) -> str:
    """Per-outlet neg/neu/pos counts -- the first, plainest answer to Q1."""
    if not rows:
        return "(no verdicts yet)"
    lines = [f"{'outlet':<12}{'neg':>5}{'neu':>5}{'pos':>5}{'n':>5}   lean"]
    for r in rows:
        n = r["n"] or 0
        lean = (r["pos"] - r["neg"]) / n if n else 0.0
        bar = ("+" * round(lean * 5)) if lean > 0 else ("-" * round(-lean * 5))
        lines.append(
            f"{r['outlet']:<12}{r['neg']:>5}{r['neu']:>5}{r['pos']:>5}{n:>5}   {lean:+.2f} {bar}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Q1: stance toward a keyword, per enriched article."
    )
    parser.add_argument("--keyword", required=True, help="the target; stance is judged toward this")
    parser.add_argument("--model", default=STANCE_MODEL)
    parser.add_argument("--rpm", type=float, default=STANCE_RPM, help="client-side pacing")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true", help="count the calls; make none")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    classifier = GeminiStance(model=args.model, rpm=args.rpm)
    stats = classify_keyword(args.keyword, classifier, limit=args.limit, dry_run=args.dry_run)
    log.info(
        "matched=%d cached=%d planned=%d classified=%d failed=%d evidence_verbatim=%d "
        "quota_exhausted=%d model=%s prompt=%s",
        stats["matched"],
        stats["cached"],
        stats["planned"],
        stats["classified"],
        stats["failed"],
        stats["evidence_verbatim"],
        stats["quota_exhausted"],
        args.model,
        PROMPT_VERSION,
    )
    if args.dry_run:
        print(f"dry run: {stats['planned']} API call(s) would be made for {args.keyword!r}")
        return 0

    with db.connect() as conn:
        print(
            distribution_table(db.stance_by_outlet(conn, args.keyword, args.model, PROMPT_VERSION))
        )
    return 1 if stats["failed"] and not stats["classified"] else 0


if __name__ == "__main__":
    sys.exit(main())

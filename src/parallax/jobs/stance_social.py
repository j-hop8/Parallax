"""Q4: classify a keyword's Threads posts for stance toward that keyword.

    make stance.posts KEYWORD=沈伯洋 ARGS=--dry-run   # how many API calls it would make
    make stance.posts KEYWORD=沈伯洋                  # make them; re-run makes zero

A sibling of `jobs.stance` rather than a flag on it: the article job walks
enriched bodies and this one walks a platform and a time window, and folding
two argv shapes into one parser would make both harder to read than the
duplication saves. What the two share lives where it belongs -- the cache-once
discipline, the per-row commit, and the daily-quota stop are the same shape
because the failure modes are the same.

Posts with no text (media-only) are skipped, never sent: there is nothing for
the model to read, and a verdict on an empty string would be a fabricated
data point in the Q4 denominator's numerator.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime

from .. import db
from ..nlp.stance import (
    POST_PROMPT_VERSION,
    DailyQuotaExhausted,
    GeminiPostStance,
    PostStanceClassifier,
    post_stance_input,
)
from ..settings import STANCE_MODEL, STANCE_RPM

log = logging.getLogger(__name__)

# Threads opened to the public 2023-07-05; nothing can predate it. Used as the
# lower bound when no window is given, so "all posts we hold" is expressible
# without a NULL-handling branch in the query.
THREADS_EPOCH = datetime(2023, 7, 6, tzinfo=UTC)


def classify_posts(
    keyword: str,
    classifier: PostStanceClassifier,
    *,
    platform: str = "threads",
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 500,
    dry_run: bool = False,
    connect: Callable | None = None,
) -> dict[str, int]:
    """Classify every matching post that has no verdict yet. Returns counts.

    Each post is committed on its own and failures are isolated, so a run
    stopped by a rate limit resumes from where it left off. Counters move only
    after the commit (T-006b).
    """
    stats = {
        "matched": 0,
        "empty": 0,  # media-only posts, never sent to the model
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
        rows = db.find_social_posts(
            conn, keyword, platform, since or THREADS_EPOCH, until or datetime.now(UTC)
        )
        stats["matched"] = len(rows)

        todo = []
        for row in rows:
            if not (row.get("text") or "").strip():
                stats["empty"] += 1
                continue
            cached = db.get_post_stance(
                conn, row["id"], keyword, classifier.model, classifier.prompt_version
            )
            if cached is None:
                todo.append(row)
            else:
                stats["cached"] += 1
        todo = todo[:limit]
        stats["planned"] = len(todo)
        if dry_run:
            return stats

        for row in todo:
            try:
                result = classifier.classify(post_stance_input(row, keyword))
                db.save_post_stance(
                    conn,
                    post_id=row["id"],
                    target=keyword,
                    model=result.model,
                    prompt_version=result.prompt_version,
                    label=result.label,
                    confidence=result.confidence,
                    evidence=result.evidence,
                )
                conn.commit()
            except DailyQuotaExhausted as exc:
                # Not a post failure: nothing was cached, so the remaining rows
                # are simply still pending. Stop instead of failing each.
                conn.rollback()
                stats["quota_exhausted"] = 1
                log.error(
                    "%s -- %d post(s) left unclassified",
                    exc,
                    stats["planned"] - stats["classified"] - stats["failed"],
                )
                break
            except Exception as exc:  # noqa: BLE001 -- isolation is the point
                conn.rollback()
                stats["failed"] += 1
                log.warning("post stance failed for %s: %s", row.get("post_url"), exc)
                continue

            stats["classified"] += 1
            # An evidence phrase that is not in the post is a hallucinated
            # justification. Posts are short, so the rate here is a sharper
            # signal than it is on articles.
            if result.evidence and result.evidence in (row.get("text") or ""):
                stats["evidence_verbatim"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Q4: stance toward a keyword, per social post."
    )
    parser.add_argument("--keyword", required=True, help="the target; stance is judged toward this")
    parser.add_argument("--platform", default="threads")
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

    classifier = GeminiPostStance(model=args.model, rpm=args.rpm)
    stats = classify_posts(
        args.keyword,
        classifier,
        platform=args.platform,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    log.info(
        "matched=%d empty=%d cached=%d planned=%d classified=%d failed=%d "
        "evidence_verbatim=%d quota_exhausted=%d platform=%s model=%s prompt=%s",
        stats["matched"],
        stats["empty"],
        stats["cached"],
        stats["planned"],
        stats["classified"],
        stats["failed"],
        stats["evidence_verbatim"],
        stats["quota_exhausted"],
        args.platform,
        args.model,
        POST_PROMPT_VERSION,
    )
    if args.dry_run:
        print(f"dry run: {stats['planned']} API call(s) would be made for {args.keyword!r}")
        return 0
    return 1 if stats["failed"] and not stats["classified"] else 0


if __name__ == "__main__":
    sys.exit(main())

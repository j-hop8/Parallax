#!/usr/bin/env python
"""Blindly label Threads stance into eval/post_stance_gold.csv.

    make label.posts KEYWORD=沈伯洋 ARGS="--n 30 --annotator jimmy --seed 1"

Shows author, Taipei time and full text, never model verdicts. Each label is
saved immediately; re-running skips labeled permalink/target pairs. Read the
post annotation guide in eval/README.md before labeling.
"""

from __future__ import annotations

import argparse
import getpass
import pathlib
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from parallax import db
from parallax.nlp.gold import POST_GOLD_PATH, load_post_gold, pending_posts, post_label_session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--keyword", required=True, help="the target to judge stance toward")
    parser.add_argument("--n", type=int, default=50, help="posts to offer this session")
    parser.add_argument("--annotator", default=getpass.getuser())
    parser.add_argument("--seed", type=int, default=None, help="reproducible shuffle")
    args = parser.parse_args(argv)

    with db.connect() as conn:
        posts = db.find_social_posts(
            conn, args.keyword, "threads", datetime(2023, 7, 6, tzinfo=UTC), datetime.now(UTC)
        )
    if not posts:
        print(f"no threads posts match {args.keyword!r}; run: make social KEYWORD={args.keyword}")
        return 1

    gold = load_post_gold(POST_GOLD_PATH)
    todo = pending_posts(posts, gold, args.keyword, seed=args.seed)
    done = {(g.post_url, g.target) for g in gold}
    already = sum((p["post_url"], args.keyword) in done for p in posts)
    dropped = sum(not (p.get("text") or "").strip() for p in posts)
    suffix = f" (dropped {dropped} media-only)" if dropped else ""
    print(
        f"{len(posts)} posts, {already} already labeled, {len(todo)} pending "
        f"-> {POST_GOLD_PATH.relative_to(POST_GOLD_PATH.parents[1])}{suffix}"
    )
    if todo:
        post_label_session(
            todo,
            target=args.keyword,
            annotator=args.annotator,
            gold_path=POST_GOLD_PATH,
            limit=args.n,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

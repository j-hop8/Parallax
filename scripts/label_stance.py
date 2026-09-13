#!/usr/bin/env python
"""Hand-label stance toward a keyword into eval/stance_gold.csv, blind.

    make label KEYWORD=沈伯洋            # 50 unlabeled articles, shuffled
    make label KEYWORD=沈伯洋 ARGS="--n 20 --annotator jimmy --seed 1"

Shows outlet, headline and lede for each enriched article the keyword matches
and that nobody has labeled yet. Never shows what the model said. Resumable:
re-running skips what is already in the CSV. Definitions are in eval/README.md
-- read them first; the model was given the same ones.
"""

from __future__ import annotations

import argparse
import getpass
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from parallax import db
from parallax.nlp.gold import GOLD_PATH, label_session, load_gold, pending


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--keyword", required=True, help="the target; stance is judged toward this")
    parser.add_argument("--n", type=int, default=50, help="articles to offer this session")
    parser.add_argument("--annotator", default=getpass.getuser())
    parser.add_argument(
        "--seed", type=int, default=None, help="fix the shuffle for a reproducible session"
    )
    args = parser.parse_args(argv)

    with db.connect() as conn:
        articles = db.find_enriched_articles(conn, args.keyword, limit=1000)
    if not articles:
        print(
            f"no enriched articles match {args.keyword!r}; run: make enrich KEYWORD={args.keyword}"
        )
        return 1

    gold = load_gold(GOLD_PATH)
    todo = pending(articles, gold, args.keyword, seed=args.seed)
    already = len(articles) - len(todo)
    print(
        f"{len(articles)} enriched, {already} already labeled, {len(todo)} pending -> {GOLD_PATH.relative_to(GOLD_PATH.parents[1])}"
    )
    if not todo:
        return 0

    label_session(
        todo, target=args.keyword, annotator=args.annotator, gold_path=GOLD_PATH, limit=args.n
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

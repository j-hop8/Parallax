#!/usr/bin/env python
"""Hand-label candidate article pairs into eval/dup_gold.csv, blind.

    make label.pairs                              # up to 200 pairs, stratified by similarity
    make label.pairs ARGS="--n 60 --annotator jimmy --seed 1"

The question is the proposal's near-duplicate definition: the same copy,
syndicated or lightly rewritten. Two articles that quote the same press
release at length inside their own reporting are NOT duplicates. The tool
never shows the similarity score or the current verdict.
"""

from __future__ import annotations

import argparse
import getpass
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from parallax import db
from parallax.jobs.dedup import run_dedup
from parallax.jobs.eval_dedup import stratified_sample
from parallax.nlp.gold import PAIR_GOLD_PATH, load_pair_gold, pair_label_session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--n", type=int, default=200, help="pairs to offer this session")
    parser.add_argument("--annotator", default=getpass.getuser())
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    with db.connect() as conn:
        rows = db.enriched_for_dedup(conn)
    if not rows:
        print("no enriched articles; run: make enrich KEYWORD=<keyword>")
        return 1
    by_id = {r["id"]: r for r in rows}
    run = run_dedup(rows)

    labeled = {g.key for g in load_pair_gold(PAIR_GOLD_PATH)}
    sample = stratified_sample(run, labeled, seed=args.seed)
    print(
        f"{len(rows)} articles, {len(labeled)} pairs already labeled, {len(sample)} offered "
        f"-> {PAIR_GOLD_PATH.relative_to(PAIR_GOLD_PATH.parents[1])}"
    )
    if not sample:
        return 0
    pairs = [(by_id[a], by_id[b]) for a, b, _ in sample]
    pair_label_session(pairs, annotator=args.annotator, gold_path=PAIR_GOLD_PATH, limit=args.n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

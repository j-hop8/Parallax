#!/usr/bin/env python
"""Re-attach eval/stance_gold.csv to a rebuilt database, matching on URL.

    make gold.remap                 # dry run: say what would change, touch nothing
    make gold.remap ARGS=--write    # rewrite the CSV in place

`article_id` belongs to the database, not to the article. Restore or re-crawl
into a fresh index and every id in the gold set points at nothing, which would
strand 184 hand-made labels -- the most expensive artifact in this project. The
URL the annotator looked at is in the file too, and article_index is UNIQUE on
(outlet, url_canonical), so the labels re-attach exactly.

Re-runnable on purpose: a re-crawl fills in over days, so rows whose article is
not back yet keep their old id and are listed as pending. Run it again later.

The CSV is committed, so git is the undo: `git diff eval/stance_gold.csv` shows
exactly what moved and `git checkout eval/stance_gold.csv` puts it back.
"""

from __future__ import annotations

import argparse
import csv
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from parallax import db
from parallax.nlp.gold import COLUMNS, GOLD_PATH, load_gold
from parallax.nlp.remap import canonical_keys, plan


def write_gold(path: pathlib.Path, rows) -> None:
    """Atomic replace: a crash mid-write must not leave a half-written gold set."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row.__dict__)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--write", action="store_true", help="rewrite the CSV (default: dry run)")
    parser.add_argument("--path", type=pathlib.Path, default=GOLD_PATH)
    args = parser.parse_args(argv)

    gold = load_gold(args.path)
    if not gold:
        print(f"no gold rows in {args.path}")
        return 1

    with db.connect() as conn:
        found = db.article_ids_by_canonical(conn, canonical_keys(gold))
    p = plan(gold, found)

    print(f"{len(gold)} gold rows, {len(found)} of {len(canonical_keys(gold))} URLs found")
    print(f"  remapped  {p.remapped}\n  unchanged {p.unchanged}\n  pending   {len(p.unmatched)}")

    if p.unmatched:
        print("\npending -- not re-crawled yet; these keep their id, run again later:")
        for rp in p.unmatched[:10]:
            print(f"  {rp.row.outlet:<10} {rp.row.url}")
        if len(p.unmatched) > 10:
            print(f"  … {len(p.unmatched) - 10} more")

    if not p.safe:
        print("\nREFUSING TO WRITE -- one id claimed by more than one article:")
        for new, olds in sorted(p.collisions.items()):
            print(f"  new id {new} <- old ids {', '.join(map(str, olds))}")
        print("The URL mapping is not one-to-one; writing this would corrupt every join.")
        return 2

    if p.remapped == 0:
        print("\nnothing to change.")
        return 0
    if not args.write:
        print(f"\ndry run: {p.remapped} row(s) would be re-keyed. Re-run with --write.")
        return 0

    write_gold(args.path, p.applied())
    print(f"\nwrote {args.path}: {p.remapped} row(s) re-keyed.")
    print("Check it with: git diff eval/stance_gold.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())

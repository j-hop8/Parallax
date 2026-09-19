"""Re-run body extraction over the raw HTML cache. No fetch, ever.

    make reextract                       # every enriched article, from raw/
    make reextract ARGS="--outlet tvbs"   # one outlet
    make reextract ARGS=--dry-run        # count what would change

The cache exists so that a parser fix never re-hits an outlet (CLAUDE.md,
crawling conduct). This is the job that cashes that in: whenever
`extract_body` changes, run it and every stored body reflects the new parser.
Bodies that come out identical are not written, so a second run reports zero
changes; a body that now fails the minimum-length rule is recorded the same way
enrich records it, with the page still in the cache.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

from .. import db
from ..crawl.body import read_cached
from ..crawl.extract import extract_body
from ..nlp.segment import segment_text
from .enrich import _MIN_BODY_CHARS

log = logging.getLogger(__name__)


def reextract_all(
    conn, *, outlets: list[str] | None = None, dry_run: bool = False
) -> dict[str, dict[str, int]]:
    """Per outlet: changed / same / short / missing. Commits per article."""
    stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ai.id, ai.outlet, a.body, a.raw_html_path
            FROM articles a JOIN article_index ai ON ai.id = a.id
            WHERE a.raw_html_path IS NOT NULL
              AND (%(outlets)s::text[] IS NULL OR ai.outlet = ANY(%(outlets)s))
            ORDER BY ai.id
            """,
            {"outlets": outlets},
        )
        rows = cur.fetchall()

    for row in rows:
        per = stats[row["outlet"]]
        html = read_cached(Path(row["raw_html_path"]))
        if html is None:
            per["missing"] += 1
            log.warning(
                "%s %s: cache entry missing at %s", row["outlet"], row["id"], row["raw_html_path"]
            )
            continue
        body = extract_body(html)
        if not body or len(body) < _MIN_BODY_CHARS:
            per["short"] += 1
            log.warning(
                "%s %s: body now %d chars -- recorded as failed, page kept",
                row["outlet"],
                row["id"],
                len(body),
            )
            if not dry_run:
                db.mark_enrich_failed(
                    conn, row["id"], f"reextract: body {len(body)} chars after parser change"
                )
                conn.commit()
            continue
        if body == (row["body"] or ""):
            per["same"] += 1
            continue
        per["changed"] += 1
        if not dry_run:
            db.save_enriched(
                conn,
                article_id=row["id"],
                body=body,
                body_seg=segment_text(body),
                raw_html_path=row["raw_html_path"],
            )
            conn.commit()
    return {o: dict(v) for o, v in sorted(stats.items())}


def render(stats: dict[str, dict[str, int]]) -> str:
    lines = [f"{'outlet':<12}{'changed':>9}{'same':>7}{'short':>7}{'missing':>9}"]
    for outlet, per in stats.items():
        lines.append(
            f"{outlet:<12}{per.get('changed', 0):>9}{per.get('same', 0):>7}"
            f"{per.get('short', 0):>7}{per.get('missing', 0):>9}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-extract every enriched body from the raw cache."
    )
    parser.add_argument("--outlet", action="append", help="limit to an outlet (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="count changes; write nothing")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    with db.connect() as conn:
        stats = reextract_all(conn, outlets=args.outlet, dry_run=args.dry_run)
    print(render(stats))
    if args.dry_run:
        print("dry run: nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())

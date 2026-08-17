from __future__ import annotations

import argparse
import logging
import sys

from .. import db
from ..nlp.segment import segment_text

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Recompute title_seg/body_seg for stored rows.

    Needed whenever the dictionary or config/userdict.txt changes: the FTS index
    is built from these columns, and a query segmented by the new dictionary will
    not match text segmented by the old one. Cheap and idempotent, so re-running
    it after any dictionary edit is the safe default.
    """
    parser = argparse.ArgumentParser(description="Re-segment stored text after a dictionary change.")
    parser.add_argument("--batch", type=int, default=500)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    updated = 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title FROM article_index ORDER BY id")
            rows = cur.fetchall()

        with conn.cursor() as cur:
            for i, row in enumerate(rows, 1):
                cur.execute(
                    "UPDATE article_index SET title_seg = %s WHERE id = %s",
                    (segment_text(row["title"]), row["id"]),
                )
                updated += 1
                if i % args.batch == 0:
                    conn.commit()
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, body FROM articles WHERE body IS NOT NULL AND body <> ''"
            )
            bodies = cur.fetchall()
        with conn.cursor() as cur:
            for i, row in enumerate(bodies, 1):
                cur.execute(
                    "UPDATE articles SET body_seg = %s WHERE id = %s",
                    (segment_text(row["body"]), row["id"]),
                )
                if i % args.batch == 0:
                    conn.commit()
        conn.commit()

    log.info("re-segmented %d titles, %d bodies", updated, len(bodies))
    return 0


if __name__ == "__main__":
    sys.exit(main())

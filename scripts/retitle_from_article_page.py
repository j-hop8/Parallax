#!/usr/bin/env python
"""T-003c: replace lede-as-title rows with the headline from the cached article page.

T-003b's listing-based retitle could only reach rows still on the listing.
Rows that were enriched (T-005) have their article page cached in raw/, and
the page's <h1> is the headline as displayed -- so for exactly the rows that
T-007 classifies, the real headline is recoverable offline, with no request to
the outlet and no model quota.

Touches `title` and `title_seg` only, on udn and ftv only, and only where the
stored title is longer than the recovered headline (the bug's signature).
seen_at and therefore effective_at are untouched. Idempotent: a second run
reports 0 retitled.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from parallax import db
from parallax.crawl.body import read_cached
from parallax.crawl.extract import extract_headline
from parallax.nlp.segment import segment_text

AFFECTED = ("udn", "ftv")

_ENRICHED = """
SELECT ai.id, ai.outlet, ai.title, a.raw_html_path
FROM article_index ai
JOIN articles a ON a.id = ai.id
WHERE ai.outlet = %(outlet)s AND a.raw_html_path IS NOT NULL
ORDER BY ai.id
"""

_RETITLE = """
UPDATE article_index
   SET title = %(title)s, title_seg = %(title_seg)s
 WHERE id = %(id)s
   AND length(title) > length(%(title)s)
"""


def main() -> int:
    targets = set(sys.argv[1:]) or set(AFFECTED)
    unknown = targets - set(AFFECTED)
    if unknown:
        print(f"not affected by T-003b: {sorted(unknown)}; choose from {list(AFFECTED)}")
        return 2

    with db.connect() as conn:
        for outlet in AFFECTED:
            if outlet not in targets:
                continue
            with conn.cursor() as cur:
                cur.execute(_ENRICHED, {"outlet": outlet})
                rows = cur.fetchall()

            retitled = no_page = no_headline = 0
            with conn.cursor() as cur:
                for row in rows:
                    html = read_cached(pathlib.Path(row["raw_html_path"]))
                    if html is None:
                        no_page += 1
                        continue
                    headline = extract_headline(html)
                    if not headline:
                        no_headline += 1
                        continue
                    if headline == row["title"]:
                        continue
                    cur.execute(
                        _RETITLE,
                        {"id": row["id"], "title": headline, "title_seg": segment_text(headline)},
                    )
                    retitled += cur.rowcount
            conn.commit()
            print(
                f"  {outlet:<6} {len(rows):>4} enriched  {retitled:>4} retitled"
                f"  ({no_page} without cached page, {no_headline} without headline)"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

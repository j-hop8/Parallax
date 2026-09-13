#!/usr/bin/env python
"""One-off for T-003b: replace lede-as-title rows with the real headline.

The pattern adapter stored the summary paragraph as `title` for udn and ftv
from 2026-08-10 until the fix. The upsert is first-sight-wins on purpose (it
protects seen_at), so the corrected adapter does not heal rows it has already
seen. This re-runs the fixed adapters against the live listings and rewrites
`title` and `title_seg` -- nothing else -- for rows whose stored title is longer
than the headline the listing now yields. Longer is the bug's signature: both
shapes glued or substituted text that dwarfs the headline.

Only articles still on the listing pages can be corrected. Idempotent: a second
run reports 0 changes.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from parallax import db
from parallax.config import load_outlets
from parallax.crawl.adapters.base import build_adapter
from parallax.crawl.listing import _build_fetcher
from parallax.nlp.segment import segment_text
from parallax.urls import canonicalize

_RETITLE = """
UPDATE article_index
   SET title = %(title)s, title_seg = %(title_seg)s
 WHERE outlet = %(outlet)s
   AND url_canonical = %(url_canonical)s
   AND length(title) > length(%(title)s)
"""


# The defect was measured at exactly these two outlets. setn and chinatimes
# were 0% affected, and a correction script should not touch rows outside the
# defect it exists to correct, even when the WHERE clause would make it a no-op.
AFFECTED = ("udn", "ftv")


def main() -> int:
    targets = set(sys.argv[1:]) or set(AFFECTED)
    unknown = targets - set(AFFECTED)
    if unknown:
        print(f"not affected by T-003b: {sorted(unknown)}; choose from {list(AFFECTED)}")
        return 2

    defaults, outlets = load_outlets()
    fetcher = _build_fetcher(defaults)

    with db.connect() as conn:
        for outlet in outlets:
            if outlet.code not in targets:
                continue
            stubs = build_adapter(outlet, fetcher).fetch()
            changed = 0
            with conn.cursor() as cur:
                for stub in stubs:
                    cur.execute(
                        _RETITLE,
                        {
                            "outlet": outlet.code,
                            "url_canonical": canonicalize(stub.url_original),
                            "title": stub.title,
                            "title_seg": segment_text(stub.title),
                        },
                    )
                    changed += cur.rowcount
            conn.commit()
            print(f"  {outlet.code:<11} {len(stubs):>3} on listing  {changed:>3} retitled")
    return 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import argparse
import logging
import sys

from .. import db
from ..config import load_outlets
from ..crawl.body import fetch_html
from ..crawl.extract import extract_body, extract_published_at
from ..crawl.listing import _build_fetcher
from ..nlp.segment import segment_text
from ..search import find_articles

log = logging.getLogger(__name__)

# Shorter than this is not an article. Measured over the enriched corpus: bodies
# run 447-1356 chars, 5th percentile 487. Set well below the observed minimum so
# a genuinely terse story is not rejected, but far above the 0-1 chars an
# unrecognised layout yields.
_MIN_BODY_CHARS = 80


def enrich_keyword(keyword: str, limit: int = 200, refetch: bool = False) -> dict[str, int]:
    """Tier 2: fetch and enrich only the articles a keyword actually matched.

    This is the whole cost model. article_index grows with everything published;
    articles grows only with what someone searched for. Labelling every article
    daily would not scale, and nothing here calls a model -- body extraction and
    timestamp recovery are both deterministic.
    """
    defaults, outlets = load_outlets()
    offset_is_wrong = {o.code: o.timestamp_offset_is_wrong for o in outlets}
    fetcher = _build_fetcher(defaults)

    stats = {"matched": 0, "fetched": 0, "cached": 0, "dated": 0, "failed": 0}

    with db.connect() as conn:
        matches = find_articles(conn, keyword, limit=limit)
        stats["matched"] = len(matches)
        if not matches:
            log.warning(
                "no articles matched %r -- remember the query is jieba-segmented, "
                "so a term absent from the dictionary may split oddly",
                keyword,
            )
            return stats

        for row in matches:
            # Each article is isolated and committed on its own: a single bad page
            # must not cost the rest of the batch, and the run has to be resumable
            # after an interruption.
            try:
                html, path, from_cache = fetch_html(
                    fetcher,
                    row["outlet"],
                    row["url_original"],
                    row["url_canonical"],
                    # seen_at, not effective_at: backfilling published_at rewrites
                    # effective_at, which would move the cache entry and re-fetch.
                    row["seen_at"],
                    refetch=refetch,
                )
                body = extract_body(html)
                # An empty body is a silent success, which is the failure mode
                # this project least tolerates: enrich_state='fetched' with no
                # text would let dedup and stance run downstream on nothing and
                # report confident results about an article we never read. It
                # means the extractor met a layout it does not know -- a
                # redesign, a paywall interstitial, a bot-block page -- so it is
                # recorded as failed, with the page kept in the cache so the fix
                # re-runs locally.
                if not body or len(body) < _MIN_BODY_CHARS:
                    raise ValueError(
                        f"extracted body too short ({len(body)} chars) -- "
                        f"layout likely unrecognised; cached at {path}"
                    )

                recovered = extract_published_at(
                    html,
                    seen_at=row["seen_at"],
                    offset_is_wrong=offset_is_wrong.get(row["outlet"], False),
                )

                db.save_enriched(
                    conn,
                    article_id=row["id"],
                    body=body,
                    body_seg=segment_text(body),
                    raw_html_path=str(path),
                )

                # Only fill a timestamp we do not already have. An outlet that
                # publishes one in its feed is the better source: it was recorded
                # at publication, not scraped from rendered markup later.
                if recovered and row["published_at"] is None:
                    db.backfill_published_at(conn, row["id"], recovered)
                    stats["dated"] += 1

                # Counted only once the article is fully processed. Incrementing
                # at fetch time meant an article that fetched and then failed
                # extraction counted as BOTH fetched and failed, so the totals
                # exceeded `matched` and overstated how much work succeeded.
                stats["cached" if from_cache else "fetched"] += 1
                conn.commit()
            except Exception as exc:  # noqa: BLE001 -- isolation is the point
                conn.rollback()
                stats["failed"] += 1
                log.warning("enrich failed for %s %s: %s", row["outlet"], row["id"], exc)
                try:
                    db.mark_enrich_failed(conn, row["id"], f"{type(exc).__name__}: {exc}")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    log.exception("could not record enrich failure for %s", row["id"])

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tier-2 enrichment: fetch bodies for keyword matches only."
    )
    parser.add_argument("--keyword", required=True, help="incident keyword, e.g. 看護虐待")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument(
        "--refetch",
        action="store_true",
        help="Ignore the raw HTML cache and re-request from the outlet. Use only "
        "when the cached page itself is suspect, never to fix our own parser.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    stats = enrich_keyword(args.keyword, limit=args.limit, refetch=args.refetch)
    log.info(
        "matched=%d fetched=%d from_cache=%d timestamps_recovered=%d failed=%d",
        stats["matched"],
        stats["fetched"],
        stats["cached"],
        stats["dated"],
        stats["failed"],
    )
    return 1 if stats["failed"] and not (stats["fetched"] or stats["cached"]) else 0


if __name__ == "__main__":
    sys.exit(main())

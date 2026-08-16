from __future__ import annotations

import argparse
import logging
import sys

from ..crawl.listing import crawl_all, crawl_dry_run

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tier-1 listing crawl (metadata only).")
    parser.add_argument("--outlet", help="Run a single outlet by code, e.g. cna")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report counts without writing to the database.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.dry_run:
        results = crawl_dry_run(only=args.outlet)
        for r in results:
            if r.ok:
                log.info(
                    "%-11s fetched=%-4d distinct_urls=%-4d", r.outlet, r.items_seen, r.items_new
                )
            else:
                log.warning("%-11s %s", r.outlet, r.error)
        return 0

    results = crawl_all(only=args.outlet)
    if not results:
        log.error("no outlets matched %r", args.outlet)
        return 2

    for r in results:
        if r.ok:
            log.info("%-11s seen=%-4d new=%-4d", r.outlet, r.items_seen, r.items_new)
        else:
            log.error("%-11s FAILED %s", r.outlet, r.error)

    failed = [r.outlet for r in results if not r.ok]
    empty = [r.outlet for r in results if r.ok and r.items_seen == 0]
    if empty:
        # Not an error exit -- an outlet can legitimately publish nothing in a
        # 20-minute window -- but it is the signature of a broken selector, so
        # it gets said out loud rather than buried in the counts.
        log.warning("returned zero items: %s", ", ".join(empty))

    # Non-zero exit so cron mail / log greps surface a persistent breakage.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import argparse
import logging
import sys

from .. import db

log = logging.getLogger(__name__)

# Recomputed over a trailing window rather than only "yesterday", because feeds
# surface late items and effective_at can land an article in an earlier day than
# the one we crawled it in. Recomputing is cheap; a wrong denominator is not.
_ROLLUP = """
WITH counts AS (
    SELECT outlet,
           (effective_at AT TIME ZONE 'Asia/Taipei')::date AS day,
           count(*) AS total
    FROM article_index
    WHERE effective_at >= now() - make_interval(days => %(days)s)
    GROUP BY 1, 2
),
-- Successful runs, in Taipei local time, with the gap since the previous one.
--
-- Partitioned by outlet ONLY, never by day. Partitioning by day resets lag() at
-- every midnight, so the first run of a day has a NULL gap and an outage that
-- straddles midnight -- 23:40 to 01:20, say -- becomes invisible to both days:
-- the gap never exists in either partition, while each day still passes its
-- first/last-run bracket checks. Both days would then be marked complete despite
-- a 100-minute hole. Spanning the partition across midnight attributes that gap
-- to the day of the run that ends it, which is the day whose coverage is short.
runs AS (
    SELECT outlet,
           (started_at AT TIME ZONE 'Asia/Taipei') AS ts,
           (started_at AT TIME ZONE 'Asia/Taipei')::date AS day,
           (started_at AT TIME ZONE 'Asia/Taipei')
             - lag(started_at AT TIME ZONE 'Asia/Taipei')
               OVER (PARTITION BY outlet ORDER BY started_at) AS gap
    FROM crawl_runs
    WHERE ok
),
coverage AS (
    SELECT outlet, day,
           min(ts) AS first_run,
           max(ts) AS last_run,
           coalesce(max(gap), interval '99 hours') AS max_gap
    FROM runs
    GROUP BY 1, 2
)
INSERT INTO outlet_daily_totals (outlet, day, total_articles, complete, computed_at)
SELECT c.outlet,
       c.day,
       c.total,
       -- Complete only when the day is over AND successful runs bracket it with
       -- no gap wider than %(max_gap_minutes)s minutes. The crawl runs every 20,
       -- so this tolerates a couple of missed cycles but not an outage.
       COALESCE(
           c.day < (now() AT TIME ZONE 'Asia/Taipei')::date
           AND cov.first_run <= c.day + make_interval(mins => %(max_gap_minutes)s)
           AND cov.last_run  >= c.day + interval '1 day'
                                     - make_interval(mins => %(max_gap_minutes)s)
           AND cov.max_gap   <= make_interval(mins => %(max_gap_minutes)s),
           FALSE
       ),
       now()
FROM counts c
LEFT JOIN coverage cov ON cov.outlet = c.outlet AND cov.day = c.day
ON CONFLICT (outlet, day) DO UPDATE
    SET total_articles = EXCLUDED.total_articles,
        complete = EXCLUDED.complete,
        computed_at = now()
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recompute outlet_daily_totals (Asia/Taipei days).")
    parser.add_argument("--days", type=int, default=7, help="trailing window to recompute")
    parser.add_argument(
        "--max-gap-minutes",
        type=int,
        default=90,
        help="widest gap between successful crawls still counted as full coverage",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _ROLLUP, {"days": args.days, "max_gap_minutes": args.max_gap_minutes}
            )
            log.info("rolled up %d outlet-day rows", cur.rowcount)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE complete)     AS complete,
                       count(*) FILTER (WHERE NOT complete) AS incomplete
                FROM outlet_daily_totals
                """
            )
            row = cur.fetchone()
        log.info(
            "%d outlet-days usable as a denominator, %d suppressed as incomplete",
            row["complete"],
            row["incomplete"],
        )
        conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())

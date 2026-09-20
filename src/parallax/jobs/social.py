from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import UTC, date, datetime, time, timedelta
from itertools import islice
from zoneinfo import ZoneInfo

from .. import db, settings
from ..social.threads import ThreadsClient, ThreadsError

OWN_POSTS_ERROR = (
    "keyword_search returned only own posts — app not approved for threads_keyword_search?"
)
# Session lock serializes these jobs while allowing per-request accounting commits.
_BUDGET_LOCK = 150015


def taipei_window(since=None, until=None):
    today = datetime.now(ZoneInfo(settings.TIMEZONE)).date()
    start = date.fromisoformat(since) if since else today - timedelta(days=7)
    end = date.fromisoformat(until) if until else today
    epochs = tuple(
        int(datetime.combine(d, time(), ZoneInfo(settings.TIMEZONE)).timestamp())
        for d in (start, end)
    )
    if not 1688540400 <= epochs[0] < epochs[1] <= datetime.now(UTC).timestamp():
        raise ValueError("Window must be increasing, since >= 2023-07-06, until <= now")
    return epochs


def ingest(conn, keyword, since, until, *, limit=200, dry_run=False, client=None):
    """Record every outcome; stage all results before an atomic post import.

    Dry runs record the budget decision but make no API requests or post writes.
    /me and retries count too; persist each charge before sending its request.
    """
    client = client or ThreadsClient()
    stats = {"queries": 0, "items_seen": 0, "items_new": 0, "ok": False, "error": None}
    conn.execute("SELECT pg_advisory_lock(%s)", (_BUDGET_LOCK,))
    run_id = None
    try:
        used = conn.execute(
            "SELECT coalesce(sum(queries),0) AS used FROM social_runs "
            "WHERE platform='threads' AND started_at > now() - interval '24 hours'"
        ).fetchone()["used"]
        run_id = conn.execute(
            "INSERT INTO social_runs (platform, keyword) VALUES ('threads', %s) RETURNING run_id",
            (keyword,),
        ).fetchone()["run_id"]
        conn.commit()

        def charge():
            if used + stats["queries"] + 1 > settings.THREADS_DAILY_QUERY_BUDGET:
                raise ThreadsError("Threads daily query budget exhausted")
            stats["queries"] += 1
            conn.execute(
                "UPDATE social_runs SET queries=%s WHERE run_id=%s", (stats["queries"], run_id)
            )
            conn.commit()

        client.before_request = charge
        try:
            estimate = math.ceil(limit / 100) + 1  # pages plus the own-posts guard
            if used + estimate > settings.THREADS_DAILY_QUERY_BUDGET:
                raise ThreadsError(
                    f"Threads daily query budget exceeded: {used} + {estimate} > "
                    f"{settings.THREADS_DAILY_QUERY_BUDGET}"
                )
            if not dry_run:
                owner = client.me()["username"]
                if not owner:
                    raise ThreadsError("Threads /me returned no username")
                posts = []
                for post in islice(client.keyword_search(keyword, since, until), limit):
                    stats["items_seen"] += 1
                    posts.append({**post, "raw_path": client.raw_path})
                if posts and all(post["username"] == owner for post in posts):
                    raise ThreadsError(OWN_POSTS_ERROR)
                stats["items_new"] = db.upsert_social_posts(conn, posts, keyword)
            # Commit posts and successful run status together below.
            stats["ok"] = True
        except BaseException as exc:  # audit interruptions too, then propagate them
            conn.rollback()
            stats.update(ok=False, items_new=0, error=client.redact(exc) or type(exc).__name__)
            if not isinstance(exc, Exception):
                raise
        finally:
            conn.execute(
                "UPDATE social_runs SET finished_at=now(), queries=%s, items_seen=%s, "
                "items_new=%s, ok=%s, error=%s WHERE run_id=%s",
                (
                    stats["queries"],
                    stats["items_seen"],
                    stats["items_new"],
                    stats["ok"],
                    stats["error"],
                    run_id,
                ),
            )
            try:
                conn.commit()
            except Exception as exc:  # noqa: BLE001 -- record commit failures too
                conn.rollback()
                stats.update(ok=False, items_new=0, error=client.redact(exc))
                conn.execute(
                    "UPDATE social_runs SET finished_at=now(), items_seen=%s, ok=false, "
                    "items_new=0, error=%s WHERE run_id=%s",
                    (stats["items_seen"], stats["error"], run_id),
                )
                conn.commit()
        return stats
    finally:
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (_BUDGET_LOCK,))
        conn.commit()
        client.before_request = None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Keyword-driven Threads ingestion (tier 2)")
    parser.add_argument("--keyword")
    parser.add_argument("--since", help="inclusive Taipei date (default: seven days ago)")
    parser.add_argument("--until", help="exclusive Taipei date (default: today)")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true", help="budget check; no API/post writes")
    parser.add_argument("--refresh-token", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not settings.THREADS_ACCESS_TOKEN:
        print("THREADS_ACCESS_TOKEN is unset", file=sys.stderr)
        return 2
    if not args.refresh_token and (not args.keyword or not args.keyword.strip()):
        parser.error("--keyword is required")
    if args.limit <= 0:
        parser.error("--limit must be positive")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    client = ThreadsClient()
    try:
        if args.refresh_token:
            result = client.refresh_token()
            print(f"access_token={result['access_token']} expires_in={result['expires_in']}")
            return 0
        since, until = taipei_window(args.since, args.until)
        with db.connect() as conn:
            stats = ingest(
                conn,
                args.keyword,
                since,
                until,
                limit=args.limit,
                dry_run=args.dry_run,
                client=client,
            )
        print(stats)
        return 0 if stats["ok"] else 1
    except Exception as exc:  # noqa: BLE001 -- one sanitized error, never a credential traceback
        print(client.redact(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

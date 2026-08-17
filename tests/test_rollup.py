"""Rollup completeness tests, run against Postgres inside a rolled-back transaction.

The `complete` flag decides whether a day may be used as a coverage-weight
denominator, so a false positive here silently produces wrong percentages
everywhere downstream. The midnight case below was a real bug: lag() partitioned
by local day reset at every midnight, hiding any outage that straddled it.

Everything happens in one transaction that is always rolled back, so real crawl
data is never touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest

from parallax.jobs.rollup_daily import _ROLLUP
from parallax.settings import DATABASE_URL

OUTLET = "__rollup_test__"


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(DATABASE_URL, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable: {exc}")
    try:
        connection.autocommit = False
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO outlets (code, name_zh, home_url) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (OUTLET, "test", "https://example.com/"),
            )
        yield connection
    finally:
        # Never commit: this must leave no trace in the real tables.
        connection.rollback()
        connection.close()


def _seed_runs(cur, times, ok=True):
    """Record crawls at the given Taipei WALL-CLOCK timestamps.

    The ::timestamp cast is load-bearing. Without it Postgres resolves the bound
    string as timestamptz in the server zone (UTC) and AT TIME ZONE then
    *converts* it to Taipei rather than interpreting it as Taipei, shifting every
    seeded time by 8 hours and silently landing rows on the wrong day.
    """
    for ts in times:
        cur.execute(
            "INSERT INTO crawl_runs (outlet, started_at, items_seen, items_new, ok) "
            "VALUES (%s, %s::timestamp AT TIME ZONE 'Asia/Taipei', 10, 1, %s)",
            (OUTLET, ts, ok),
        )


def _seed_article(cur, ts):
    cur.execute(
        "INSERT INTO article_index (outlet, url_canonical, url_original, title, published_at) "
        "VALUES (%s, %s, %s, 'x', %s::timestamp AT TIME ZONE 'Asia/Taipei')",
        (OUTLET, f"https://example.com/{ts}", f"https://example.com/{ts}", ts),
    )


def _run_rollup(cur, days=30, max_gap_minutes=90):
    cur.execute(_ROLLUP, {"days": days, "max_gap_minutes": max_gap_minutes})


def _completeness(cur, day):
    cur.execute(
        "SELECT complete FROM outlet_daily_totals WHERE outlet = %s AND day = %s",
        (OUTLET, day),
    )
    row = cur.fetchone()
    return None if row is None else row[0]


def _days_ago(n: int) -> str:
    """Test dates are relative, not fixed.

    The rollup only recomputes a trailing window, so hard-coded calendar dates
    silently fall out of range as time passes and the assertions then compare
    against a row that was never written.
    """
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    return str(today - timedelta(days=n))


def _every_20_min(date_str, start_hour, end_hour):
    return [
        f"{date_str} {h:02d}:{m:02d}:00"
        for h in range(start_hour, end_hour)
        for m in (0, 20, 40)
    ]


def test_outage_spanning_midnight_marks_the_following_day_incomplete(conn):
    """The bug: a 23:40 -> 01:20 hole was invisible to both adjacent days.

    Partitioning lag() by day reset the gap at midnight, so it existed in neither
    day's partition, while each day still passed its first/last-run bracket
    checks -- and both were marked complete despite a 100-minute outage.
    """
    day1, day2 = _days_ago(10), _days_ago(9)
    with conn.cursor() as cur:
        runs = _every_20_min(day1, 0, 24) + _every_20_min(day2, 0, 24)
        # Punch out 00:00 and 01:00 on day 2, leaving 23:40 -> 01:20.
        runs = [r for r in runs if not (r.startswith(day2) and r < f"{day2} 01:20:00")]
        _seed_runs(cur, runs)
        _seed_article(cur, f"{day1} 12:00:00")
        _seed_article(cur, f"{day2} 12:00:00")
        _run_rollup(cur)

        assert _completeness(cur, day2) is False, (
            "a 100-minute outage straddling midnight was not detected"
        )


def test_a_fully_covered_day_is_complete(conn):
    day, next_day = _days_ago(10), _days_ago(9)
    with conn.cursor() as cur:
        _seed_runs(cur, _every_20_min(day, 0, 24) + _every_20_min(next_day, 0, 2))
        _seed_article(cur, f"{day} 12:00:00")
        _run_rollup(cur)
        assert _completeness(cur, day) is True


def test_todays_partial_day_is_never_complete(conn):
    """A day still in progress cannot be a denominator, however well covered."""
    with conn.cursor() as cur:
        cur.execute("SELECT (now() AT TIME ZONE 'Asia/Taipei')::date")
        today = cur.fetchone()[0]
        _seed_runs(cur, [f"{today} 00:00:00", f"{today} 00:20:00"])
        _seed_article(cur, f"{today} 00:10:00")
        _run_rollup(cur)
        assert _completeness(cur, str(today)) is False


def test_failed_runs_do_not_count_as_coverage(conn):
    """crawl_runs rows with ok = false must leave a gap, not fill one."""
    day, next_day = _days_ago(8), _days_ago(7)
    with conn.cursor() as cur:
        # A whole morning of runs that all failed.
        _seed_runs(cur, _every_20_min(day, 0, 12), ok=False)
        _seed_runs(cur, _every_20_min(day, 12, 24) + _every_20_min(next_day, 0, 2))
        _seed_article(cur, f"{day} 15:00:00")
        _run_rollup(cur)
        assert _completeness(cur, day) is False

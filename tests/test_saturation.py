from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest

from parallax.metrics.saturation import saturation

START = datetime(2026, 9, 1, tzinfo=UTC)


def run(minute, new=2, seen=20, ok=True, outlet="a"):
    return {
        "outlet": outlet, "started_at": START + timedelta(minutes=minute),
        "items_new": new, "items_seen": seen, "ok": ok,
    }


def test_risk_healthy_outlets_and_hand_computed_turnover():
    rows = [run(0, 20), run(20, 16), run(40, 10), run(60, 20)]
    rows += [run(t, outlet="b") for t in (0, 20, 40)]
    a, b = saturation(reversed(rows))
    assert (a.runs, a.at_risk, a.median) == (3, 2, 0.8)
    assert a.worst == (1.0, START + timedelta(minutes=60))
    # Gaps are 20: turnover is 25, 40, and 20 minutes respectively.
    assert a.fastest_turnover_minutes == 20
    assert (b.runs, b.at_risk, b.median, b.fastest_turnover_minutes) == (2, 0, 0.1, 200)
    assert a.excluded["first"] == b.excluded["first"] == 1


def test_failed_zero_seen_and_outage_exclusions():
    result, = saturation([
        run(0, 20), run(10, ok=False), run(20, 0, 0), run(40),
        run(60), run(140, 20), run(160),
    ])
    assert result.excluded == {
        "failed": 1, "first": 1, "zero_seen": 1, "anomaly": 0, "nonpositive_gap": 0, "outage": 1,
    }
    assert result.runs == 3
    assert result.at_risk == 0
    assert result.fastest_turnover_minutes == 200


def test_failure_does_not_reset_successful_gap_and_three_times_is_allowed():
    result, = saturation([
        run(0), run(20), run(40), run(80, ok=False), run(100, 10), run(120),
    ])
    assert result.excluded["outage"] == 0  # 60 == 3 * median gap 20
    assert result.fastest_turnover_minutes == 120  # 60 / .5, not 20 / .5


def test_anomalies_are_counted_even_when_excluded_for_other_reasons():
    result, = saturation([
        run(0, 21), run(10, 21, ok=False), run(20, 1, 0), run(40, 21),
        run(60), run(80),
    ])
    assert result.anomalies == 4
    assert result.excluded["anomaly"] == 1
    assert result.runs == 2
    assert result.worst[0] == 0.1


def test_too_few_runs_and_no_new_items():
    result, = saturation([run(0), run(20)])
    assert result.runs == 1
    assert result.median is result.worst is result.fastest_turnover_minutes is None
    result, = saturation([run(0), run(20, 0), run(40, 0)])
    assert result.median == 0
    assert result.fastest_turnover_minutes is None
    assert saturation([]) == ()


def test_window_context_is_not_counted_or_mistaken_for_first_ever():
    result, = saturation([run(0, 21), run(20, 16), run(40, 16)],
                         since=START + timedelta(minutes=20))
    assert result.runs == 2
    assert result.excluded["first"] == result.anomalies == 0
    assert result.fastest_turnover_minutes == 25
    assert saturation([run(0)], since=START + timedelta(minutes=20)) == ()


def test_failed_first_attempt_and_duplicate_timestamp():
    result, = saturation([run(0, ok=False), run(20), run(20), run(40), run(60)])
    assert result.excluded["failed"] == result.excluded["first"] == 1
    assert result.excluded["nonpositive_gap"] == 1
    assert result.runs == 2


@pytest.mark.parametrize("anomaly, expected", [(False, 0), (True, 1)])
def test_job_output_and_exit(monkeypatch, capsys, anomaly, expected):
    from parallax.jobs import saturation as job

    class Clock:
        @staticmethod
        def now(tz):
            return (START + timedelta(days=1)).astimezone(tz)

    @contextlib.contextmanager
    def connect():
        yield object()

    rows = [run(0, 21 if anomaly else 20), run(20, 16), run(40, 20)]
    monkeypatch.setattr(job, "datetime", Clock)
    monkeypatch.setattr(job.db, "connect", connect)
    monkeypatch.setattr(job.db, "crawl_runs_window", lambda conn, since: rows)
    assert job.main(["--days", "7", "--verbose"]) == expected
    output = capsys.readouterr().out
    assert "runs=2 at_risk=2 median=90.0% worst=100.0%" in output
    assert "2026-09-01 08:40:00" in output
    assert "Asia/Taipei" in output
    assert "fastest_turnover=20.0 min" in output
    assert "excluded[failed=0,first=1,zero_seen=0" in output
    assert ("DATA-INTEGRITY WARNING" in output) == anomaly


def test_job_reports_insufficient_data(monkeypatch, capsys):
    from parallax.jobs import saturation as job

    @contextlib.contextmanager
    def connect():
        yield object()

    monkeypatch.setattr(job.db, "connect", connect)
    now = datetime.now(UTC)
    monkeypatch.setattr(job.db, "crawl_runs_window", lambda conn, since: [
        dict(run(0), started_at=now),
    ])
    assert job.main([]) == 0
    assert "insufficient data" in capsys.readouterr().out
    monkeypatch.setattr(job.db, "crawl_runs_window", lambda conn, since: [])
    assert job.main([]) == 0
    assert "No crawl runs" in capsys.readouterr().out


def test_db_read_preserves_all_window_rows_and_successful_predecessor():
    import psycopg
    from psycopg.rows import dict_row

    from parallax.db import crawl_runs_window
    from parallax.settings import DATABASE_URL

    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres unavailable: {exc}")
    with conn:
        # Temporary table isolates this read contract from any live crawl history.
        conn.execute("""
            CREATE TEMP TABLE crawl_runs (
                run_id serial, outlet text, started_at timestamptz,
                items_seen integer, items_new integer, ok boolean
            )
        """)
        rows = [run(0), run(10), run(15, ok=False), run(20, 0, 0), run(40, 21),
                run(30, ok=False, outlet="b"), run(0, outlet="inactive")]
        for row in reversed(rows):
            conn.execute("""
                INSERT INTO crawl_runs (outlet, started_at, items_seen, items_new, ok)
                VALUES (%(outlet)s, %(started_at)s, %(items_seen)s, %(items_new)s, %(ok)s)
            """, row)
        actual = crawl_runs_window(conn, START + timedelta(minutes=20))
        assert actual == [rows[i] for i in (1, 3, 4, 5)]

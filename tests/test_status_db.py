"""Public status queries against Postgres; synthetic writes are rolled back."""

from datetime import timedelta

from parallax import db
from tests.test_report_db import T_D1, A, B, _article
from tests.test_report_db import conn as conn  # noqa: PLC0414 -- reuse the rollback fixture


def test_crawl_health_filters_failed_and_old_runs_and_measures_gaps(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT now() AS at")
        now = cur.fetchone()["at"]
        for outlet, age, ok in [
            (A, 25 * 60, True), (A, 50, True), (A, 40, False),
            (A, 20, True), (A, 5, False), (B, 10, True),
        ]:
            cur.execute(
                "INSERT INTO crawl_runs (outlet, started_at, ok) VALUES (%s, %s, %s)",
                (outlet, now - timedelta(minutes=age), ok),
            )
    rows = {row["outlet"]: row for row in db.crawl_health(conn)}
    assert rows[A] == {
        "outlet": A, "last_ok": now - timedelta(minutes=20),
        "ok_runs": 2, "largest_gap": timedelta(minutes=30),
    }
    assert rows[B]["ok_runs"] == 1
    assert rows[B]["largest_gap"] == timedelta(0)


def test_index_extent_counts_all_articles_and_uses_effective_timestamp(conn):
    before = db.index_extent(conn)
    # Earlier than existing data; a missing published_at must fall back to seen_at.
    at = min(before["since"], T_D1) if before["since"] else T_D1
    at -= timedelta(days=2)
    with conn.cursor() as cur:
        _article(cur, A, at, published=False)
        _article(cur, B, at + timedelta(days=1))
    extent = db.index_extent(conn)
    assert extent["articles"] == before["articles"] + 2
    assert extent["since"] == at + timedelta(minutes=5)

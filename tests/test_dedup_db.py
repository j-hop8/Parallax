"""replace_clusters against Postgres, inside a transaction that is always rolled back.

The acceptance criterion is that a second `make dedup` changes nothing. That
is only provable at the database: the reconcile must touch rows exactly when
the stored cluster fields differ from what the run computed, and its order
must respect the FK from articles.dup_cluster_id to dup_clusters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from parallax import db
from parallax.nlp.dedup import Member, build_cluster
from parallax.settings import DATABASE_URL

OUTLET = "__dedup_test__"
T0 = datetime(2030, 1, 1, tzinfo=UTC)  # far future: never collides with real rows


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(DATABASE_URL, connect_timeout=3, row_factory=dict_row)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable: {exc}")
    try:
        connection.autocommit = False
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO outlets (code, name_zh, home_url) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (OUTLET, "test", "https://example.com/"),
            )
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _articles(conn, n: int) -> list[int]:
    ids = []
    with conn.cursor() as cur:
        for i in range(n):
            cur.execute(
                """
                INSERT INTO article_index (outlet, url_canonical, url_original, title, published_at, seen_at)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (
                    OUTLET,
                    f"https://example.com/{i}",
                    f"https://example.com/{i}",
                    f"t{i}",
                    T0 + timedelta(minutes=10 * i),
                    T0 + timedelta(minutes=10 * i),
                ),
            )
            aid = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO articles (id, body, body_seg, enrich_state) VALUES (%s, 'b', 'b', 'fetched')",
                (aid,),
            )
            ids.append(aid)
    return ids


def _state(conn, ids):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, dup_cluster_id, is_cluster_origin, cluster_rank FROM articles WHERE id = ANY(%s) ORDER BY id",
            (ids,),
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT cluster_id, member_count, origin_confident FROM dup_clusters WHERE origin_article_id = ANY(%s)",
            (ids,),
        )
        clusters = cur.fetchall()
    return rows, clusters


def _member(aid, minutes):
    t = T0 + timedelta(minutes=minutes)
    return Member(aid, OUTLET, t, t)


def test_second_run_touches_nothing_and_changes_are_reconciled(conn):
    a, b, c = _articles(conn, 3)
    cluster = build_cluster([_member(a, 0), _member(b, 10)])

    first = db.replace_clusters(conn, [cluster], [a, b, c])
    assert first["clusters_upserted"] == 1 and first["members_set"] == 2
    assert first["members_detached"] == 0 and first["clusters_deleted"] == 0
    rows, clusters = _state(conn, [a, b, c])
    assert [(r["dup_cluster_id"], r["is_cluster_origin"], r["cluster_rank"]) for r in rows] == [
        (a, True, 1),
        (a, False, 2),
        (None, False, None),
    ]
    assert clusters[0]["member_count"] == 2

    again = db.replace_clusters(conn, [cluster], [a, b, c])
    assert again == {
        "clusters_upserted": 0,
        "clusters_deleted": 0,
        "members_set": 0,
        "members_reset": 0,
        "members_detached": 0,
    }

    # c joins: only c is written; the cluster row is updated once for the new count.
    grown = build_cluster([_member(a, 0), _member(b, 10), _member(c, 20)])
    third = db.replace_clusters(conn, [grown], [a, b, c])
    assert third["members_set"] == 1 and third["clusters_upserted"] == 1

    # Everything dissolves: members detached, the unreferenced cluster deleted, FK order respected.
    gone = db.replace_clusters(conn, [], [a, b, c])
    assert gone["members_detached"] == 3 and gone["clusters_deleted"] == 1
    rows, clusters = _state(conn, [a, b, c])
    assert all(r["dup_cluster_id"] is None for r in rows) and clusters == []


def test_cluster_id_change_repoints_members_before_deleting_the_old_row(conn):
    """A lower id joining changes cluster_id = min(member): the old row must go
    only after every member points at the new one."""
    a, b, c = _articles(conn, 3)  # ids ascend: a < b < c
    old = build_cluster([_member(b, 0), _member(c, 10)])  # cluster_id == b
    db.replace_clusters(conn, [old], [a, b, c])
    new = build_cluster([_member(a, 0), _member(b, 10), _member(c, 20)])  # cluster_id == a
    counts = db.replace_clusters(conn, [new], [a, b, c])
    assert counts["clusters_upserted"] == 1 and counts["clusters_deleted"] == 1
    assert counts["members_set"] == 3
    rows, clusters = _state(conn, [a, b, c])
    assert {r["dup_cluster_id"] for r in rows} == {a}
    assert [cl["cluster_id"] for cl in clusters] == [a]

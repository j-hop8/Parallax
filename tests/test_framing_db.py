"""save_framing / save_summary / replace_clusters against Postgres, rolled back.

The acceptance criteria only provable at the database: a second `make
framing` changes nothing; a member whose deltas change loses its summary and
one whose deltas do not keeps it (no quota re-spent); a member that dedup
moves to another cluster loses deltas and summary; a rank change alone does
not touch them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from parallax import db
from parallax.nlp.dedup import Member, build_cluster
from parallax.nlp.summary import ORIGIN_SUMMARY, RULE_MODEL, SUMMARY_VERSION
from parallax.settings import DATABASE_URL

OUTLET = "__framing_test__"
OUTLET_B = "__framing_test_b__"  # build_cluster calls a same-outlet pair indeterminate
T0 = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(DATABASE_URL, connect_timeout=3, row_factory=dict_row)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable: {exc}")
    try:
        connection.autocommit = False
        with connection.cursor() as cur:
            for code in (OUTLET, OUTLET_B):
                cur.execute(
                    "INSERT INTO outlets (code, name_zh, home_url) VALUES (%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (code, "test", "https://example.com/"),
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
                    f"https://example.com/f{i}",
                    f"https://example.com/f{i}",
                    f"t{i}",
                    T0 + timedelta(minutes=10 * i),
                    T0 + timedelta(minutes=10 * i),
                ),
            )
            aid = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO articles (id, body, body_seg, enrich_state) VALUES (%s, '甲。', '甲', 'fetched')",
                (aid,),
            )
            ids.append(aid)
    return ids


def _member(aid, minutes, outlet=OUTLET):
    t = T0 + timedelta(minutes=minutes)
    return Member(aid, outlet, t, t)


def _row(conn, aid):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT dup_cluster_id, cluster_rank, delta_added, delta_removed, delta_summary, "
            "delta_summary_model, delta_summary_version FROM articles WHERE id = %s",
            (aid,),
        )
        return cur.fetchone()


def _core(conn, cid):
    with conn.cursor() as cur:
        cur.execute("SELECT shared_core_text FROM dup_clusters WHERE cluster_id = %s", (cid,))
        return cur.fetchone()["shared_core_text"]


def test_save_framing_is_idempotent_and_resets_only_changed_summaries(conn):
    a, b = _articles(conn, 2)
    db.replace_clusters(conn, [build_cluster([_member(a, 0), _member(b, 10)])], [a, b])

    first = db.save_framing(
        conn, a, "甲。", [(a, [], [], ORIGIN_SUMMARY), (b, ["乙。"], ["丙。"], None)]
    )
    assert first == {"cores_set": 1, "deltas_set": 2, "summaries_reset": 2}
    assert _core(conn, a) == "甲。"
    ra, rb = _row(conn, a), _row(conn, b)
    assert (ra["delta_summary"], ra["delta_summary_model"], ra["delta_summary_version"]) == (
        ORIGIN_SUMMARY,
        RULE_MODEL,
        SUMMARY_VERSION,
    )
    assert rb["delta_added"] == ["乙。"] and rb["delta_summary"] is None

    db.save_summary(conn, b, "＋ 加入乙；－ 刪除丙。", "m", SUMMARY_VERSION)

    again = db.save_framing(
        conn, a, "甲。", [(a, [], [], ORIGIN_SUMMARY), (b, ["乙。"], ["丙。"], None)]
    )
    assert again == {"cores_set": 0, "deltas_set": 0, "summaries_reset": 0}
    assert _row(conn, b)["delta_summary"] == "＋ 加入乙；－ 刪除丙。"  # kept: no quota re-spent

    changed = db.save_framing(
        conn, a, "甲。", [(a, [], [], ORIGIN_SUMMARY), (b, ["乙。"], [], None)]
    )
    assert changed == {"cores_set": 0, "deltas_set": 1, "summaries_reset": 1}
    rb = _row(conn, b)
    assert rb["delta_summary"] is None and rb["delta_summary_model"] is None


def test_replace_clusters_clears_framing_when_a_member_moves_but_not_on_a_rank_change(conn):
    a, b, c, d = _articles(conn, 4)
    db.replace_clusters(conn, [build_cluster([_member(b, 0), _member(c, 10)])], [a, b, c, d])
    db.save_framing(conn, b, "甲。", [(b, [], [], ORIGIN_SUMMARY), (c, ["乙。"], [], None)])
    db.save_summary(conn, c, "＋ 加入乙。", "m", SUMMARY_VERSION)

    # d joins after b and c: their ranks and ids are unchanged, so nothing is cleared;
    # the cluster row changed (member_count), so its core is.
    db.replace_clusters(
        conn, [build_cluster([_member(b, 0), _member(c, 10), _member(d, 20)])], [a, b, c, d]
    )
    assert _row(conn, c)["delta_summary"] == "＋ 加入乙。"
    assert _core(conn, b) is None

    # a joins first: cluster_id becomes a, every member is re-pointed -> cleared.
    db.replace_clusters(
        conn,
        [build_cluster([_member(a, 0), _member(b, 10), _member(c, 20), _member(d, 30)])],
        [a, b, c, d],
    )
    rc = _row(conn, c)
    assert rc["dup_cluster_id"] == a and rc["cluster_rank"] == 3
    assert rc["delta_added"] is None and rc["delta_summary"] is None

    # Dissolving detaches and clears.
    db.save_framing(conn, a, "甲。", [(a, [], [], ORIGIN_SUMMARY), (b, ["乙。"], [], None)])
    db.replace_clusters(conn, [], [a, b, c, d])
    rb = _row(conn, b)
    assert rb["dup_cluster_id"] is None and rb["delta_added"] is None
    assert rb["delta_summary"] is None and rb["delta_summary_version"] is None


def test_a_member_inside_the_noise_floor_flips_confidence_and_clears_every_delta(conn):
    """Review finding (PR #10): same cluster_id, but the reference changed.
    b->c was confident with directional deltas; d lands 2 min after b, the
    cluster becomes indeterminate, and c's `delta_removed` would otherwise
    survive until the next `make framing` -- a stored claim invariant 5 forbids."""
    a, b, c, d = _articles(conn, 4)
    confident = build_cluster([_member(b, 0), _member(c, 10, OUTLET_B)])
    assert confident.origin_confident
    db.replace_clusters(conn, [confident], [a, b, c, d])
    db.save_framing(conn, b, "甲。", [(b, [], [], ORIGIN_SUMMARY), (c, ["乙。"], ["丙。"], None)])
    db.save_summary(conn, c, "－ 刪除丙。", "m", SUMMARY_VERSION)

    flipped = build_cluster([_member(b, 0), _member(d, 2, OUTLET_B), _member(c, 10, OUTLET_B)])
    assert not flipped.origin_confident and flipped.cluster_id == b
    counts = db.replace_clusters(conn, [flipped], [a, b, c, d])
    assert counts["members_reset"] == 2  # b and c had framing; d had none yet
    for aid in (b, c):
        r = _row(conn, aid)
        assert r["dup_cluster_id"] == b
        assert r["delta_added"] is None and r["delta_removed"] is None
        assert r["delta_summary"] is None and r["delta_summary_version"] is None

    # A new origin with the same cluster_id clears too (b keeps the min id).
    db.replace_clusters(conn, [confident], [a, b, c, d])
    db.save_framing(conn, b, "甲。", [(b, [], [], ORIGIN_SUMMARY), (c, ["乙。"], [], None)])
    reordered = build_cluster([_member(c, 0, OUTLET_B), _member(b, 10)])  # c now first
    assert reordered.origin_confident and reordered.cluster_id == b
    counts = db.replace_clusters(conn, [reordered], [a, b, c, d])
    assert counts["members_reset"] == 2
    assert _row(conn, b)["delta_summary"] is None

    # The same cluster again: nothing to reset.
    assert db.replace_clusters(conn, [reordered], [a, b, c, d])["members_reset"] == 0


def test_clusters_for_framing_groups_members_in_rank_order(conn):
    a, b = _articles(conn, 2)
    db.replace_clusters(conn, [build_cluster([_member(b, 0), _member(a, 10)])], [a, b])
    got = [c for c in db.clusters_for_framing(conn) if c["cluster_id"] == a]
    assert len(got) == 1
    members = got[0]["members"]
    assert [(m["id"], m["cluster_rank"]) for m in members] == [(b, 1), (a, 2)]
    assert members[0]["body"] == "甲。" and "cluster_id" not in members[0]
    assert OUTLET in db.bodies_by_outlet(conn) and db.bodies_by_outlet(conn)[OUTLET] == [
        "甲。",
        "甲。",
    ]

"""build_report against Postgres, inside a transaction that is always rolled back.

Seeds two synthetic outlets on far-future days with a title token no real
article carries, then checks that the report divides only where the rollup
says it may, buckets by Taipei day, and shows an indeterminate cluster without
a direction. Real rows are never touched.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from parallax.metrics.report import build_report
from parallax.nlp.segment import segment_text
from parallax.settings import DATABASE_URL

A, B = "__metrics_test_a__", "__metrics_test_b__"
TOKEN = "zzmetrictoken"  # segments to itself; matches nothing real
D1, D2 = date(2030, 3, 1), date(2030, 3, 2)
# 01:00 Taipei on D1 == 17:00Z the previous calendar day: the UTC trap.
T_D1 = datetime(2030, 2, 28, 17, 0, tzinfo=UTC)
T_D2 = T_D1 + timedelta(days=1)


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(DATABASE_URL, connect_timeout=3, row_factory=dict_row)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable: {exc}")
    try:
        connection.autocommit = False
        with connection.cursor() as cur:
            for code in (A, B):
                cur.execute(
                    "INSERT INTO outlets (code, name_zh, home_url) VALUES (%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (code, code, "https://example.com/"),
                )
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _article(cur, outlet, at, *, title=f"{TOKEN} 測試", body=None, published=True) -> int:
    cur.execute(
        """
        INSERT INTO article_index (outlet, url_canonical, url_original, title, title_seg,
                                   published_at, seen_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (
            outlet,
            f"https://example.com/{outlet}/{at.timestamp()}",
            f"https://example.com/{outlet}/{at.timestamp()}",
            title,
            segment_text(title),
            at if published else None,
            at + timedelta(minutes=5),
        ),
    )
    aid = cur.fetchone()["id"]
    if body:
        cur.execute(
            "INSERT INTO articles (id, body, body_seg, enrich_state) VALUES (%s, %s, %s, 'fetched')",
            (aid, body, body),
        )
    return aid


def _totals(cur, outlet, day, total, complete):
    cur.execute(
        "INSERT INTO outlet_daily_totals (outlet, day, total_articles, complete) VALUES (%s,%s,%s,%s)",
        (outlet, day, total, complete),
    )


def _row(report, outlet):
    return next(r for r in report.rows if r.outlet == outlet)


def test_weight_only_where_the_rollup_allows(conn):
    with conn.cursor() as cur:
        # A: D1 complete (2/400), D2 incomplete with a big total (T-004's 84 case).
        for i in range(2):
            _article(cur, A, T_D1 + timedelta(hours=i))
        _article(cur, A, T_D2)
        _totals(cur, A, D1, 400, True)
        _totals(cur, A, D2, 84, False)
        # B: matches on D2 only, but D1 is complete for it too -> a usable zero day.
        _article(cur, B, T_D2)
        _totals(cur, B, D1, 300, True)
        _totals(cur, B, D2, 300, True)

    r = build_report(conn, TOKEN)
    assert r.articles == 4 and r.outlets == 2 and r.clusters == 0
    assert (r.first_day, r.last_day, r.active_days, r.span_days) == (D1, D2, 2, 2)

    a = _row(r, A)
    assert a.matched == 3
    assert a.coverage.basis == "exact"
    assert a.coverage.weight == pytest.approx(2 / 400)  # D2 excluded despite 84 articles
    assert a.coverage.days_used == 1 and a.coverage.days_active == 2
    assert a.coverage.days[1].weight is None and a.coverage.days[1].n == 1

    b = _row(r, B)
    assert b.coverage.weight == pytest.approx(1 / 600)  # the zero day counts
    assert b.coverage.days_used == 2
    assert b.enriched == 0 and b.classified == 0 and b.originality is None

    # Configured outlets are present (with nothing) so the page always has 8 rows.
    assert any(row.outlet == "cna" and row.matched == 0 for row in r.rows)
    assert _row(r, "cna").coverage.basis == "none"


def test_window_buckets_by_taipei_day(conn):
    with conn.cursor() as cur:
        _article(cur, A, T_D1)  # 17:00Z Feb 28 == 01:00 Taipei Mar 1
        _article(cur, A, T_D2)
        _totals(cur, A, D1, 400, True)
        _totals(cur, A, D2, 400, True)

    only_d1 = build_report(conn, TOKEN, since=D1, until=D1)
    assert only_d1.articles == 1 and only_d1.first_day == D1 == only_d1.last_day
    assert _row(only_d1, A).coverage.weight == pytest.approx(1 / 400)

    nothing = build_report(conn, TOKEN, since=date(2030, 2, 28), until=date(2030, 2, 28))
    assert nothing.empty and nothing.rows[0].matched == 0


def test_day_shift_counts_backfilled_publish_days(conn):
    with conn.cursor() as cur:
        # Published 23:58 Taipei, polled 00:03 the next day: effective day != poll day.
        _article(cur, A, datetime(2030, 3, 1, 15, 58, tzinfo=UTC))
        _article(cur, A, T_D2)
    r = build_report(conn, TOKEN)
    assert r.day_shift == 1


def test_stance_originality_and_an_indeterminate_cluster(conn):
    with conn.cursor() as cur:
        a1 = _article(cur, A, T_D1, body="x")
        a2 = _article(cur, A, T_D1 + timedelta(minutes=1), body="x")
        a3 = _article(cur, A, T_D1 + timedelta(hours=2), body="x")
        _article(cur, A, T_D1 + timedelta(hours=3))  # matched, no body
        # A member whose title dropped the keyword still belongs to the cluster.
        b1 = _article(cur, B, T_D1 + timedelta(minutes=2), body="x", title="別的標題")
        cur.execute(
            """
            INSERT INTO dup_clusters (cluster_id, member_count, origin_article_id,
                                      first_published_at, origin_confident, shared_core_text)
            VALUES (%s, 3, %s, %s, FALSE, '核心')
            """,
            (a1, a1, T_D1),
        )
        for aid, rank in ((a1, 1), (a2, 2), (b1, 3)):
            cur.execute(
                """
                UPDATE articles SET dup_cluster_id = %s, is_cluster_origin = %s, cluster_rank = %s,
                       delta_added = %s, delta_removed = %s
                WHERE id = %s
                """,
                (a1, rank == 1, rank, ["只有這版"], ["stale"], aid),
            )
        for aid, label in ((a1, "neg"), (a2, "neu"), (a3, "pos")):
            cur.execute(
                """
                INSERT INTO article_stance (article_id, target, model, prompt_version, label,
                                            confidence, evidence)
                VALUES (%s, %s, 'm', 'v9', %s, 0.9, 'e')
                """,
                (aid, TOKEN, label),
            )

    r = build_report(conn, TOKEN, stance_model="m", prompt_version="v9")
    a = _row(r, A)
    assert (a.matched, a.enriched, a.classified) == (4, 3, 3)
    assert (a.neg, a.neu, a.pos) == (1, 1, 1)
    # Two members of an indeterminate cluster are unresolved; a3 stands alone.
    assert a.originality is not None
    assert (a.originality.n, a.originality.alone, a.originality.unresolved) == (3, 1, 2)
    assert a.originality.original == pytest.approx(1 / 3)

    # Another model/prompt sees no verdicts at all.
    assert _row(build_report(conn, TOKEN, stance_model="m", prompt_version="v1"), A).classified == 0

    assert r.clusters == 1
    c = r.cluster_views[0]
    assert c.origin is None and c.origin_confident is False
    assert "noise floor" in c.reason
    assert [m.article_id for m in c.members] == [a1, a2, b1]
    assert all(m.rank is None and m.delta_removed == () for m in c.members)
    assert c.members[0].delta_added == ("只有這版",)
    assert [m.matched for m in c.members] == [True, True, False]
    assert c.shared_core_text == "核心"


# ---- Q4 (T-016) ------------------------------------------------------------


def _post(cur, at, *, text, keyword=TOKEN) -> int:
    """A Threads post that find_social_posts will match on fetched_for."""
    cur.execute(
        """
        INSERT INTO social_posts (platform, post_url, author, posted_at, text, text_seg,
                                  fetched_for, raw_path)
        VALUES ('threads', %s, 'someone', %s, %s, %s, %s, 'raw/none')
        RETURNING id
        """,
        (
            f"https://www.threads.net/@a/post/{at.timestamp()}",
            at,
            text,
            segment_text(text),
            keyword,
        ),
    )
    return cur.fetchone()["id"]


def _post_stance(cur, post_id, label, *, target=TOKEN, model="m", pv="post-v9"):
    cur.execute(
        """
        INSERT INTO social_post_stance (post_id, target, model, prompt_version, label,
                                        confidence, evidence)
        VALUES (%s, %s, %s, %s, %s, 0.9, 'e')
        """,
        (post_id, target, model, pv, label),
    )


def _threads(report):
    return next(p for p in report.platform_lean if p.platform == "threads")


def test_platform_lean_counts_verdicts_and_respects_the_floor(conn):
    with conn.cursor() as cur:
        _article(cur, A, T_D1)
        for i in range(5):
            pid = _post(cur, T_D1 + timedelta(minutes=i), text=f"{TOKEN} 真是好棒棒 {i}")
            _post_stance(cur, pid, ("neg", "neu", "pos")[i % 3])
        _post(cur, T_D1 + timedelta(minutes=9), text=f"{TOKEN} 沒有判決")  # no verdict

    r = build_report(
        conn, TOKEN, stance_model="m", post_prompt_version="post-v9", min_platform_posts=3
    )
    t = _threads(r)
    assert t.posts == 6, "every matching post is in the denominator, classified or not"
    assert t.classified == 5
    assert (t.neg, t.neu, t.pos) == (2, 2, 1)
    assert t.suppressed_reason is None

    # One above the classified count and the same data is withheld.
    high = build_report(
        conn, TOKEN, stance_model="m", post_prompt_version="post-v9", min_platform_posts=6
    )
    assert _threads(high).suppressed_reason == "below_floor"
    assert _threads(high).classified == 5


def test_post_verdicts_are_filtered_by_target_model_and_prompt(conn):
    """The whole reason post stance got its own table: a verdict toward another
    target must never be counted as this incident's lean."""
    with conn.cursor() as cur:
        _article(cur, A, T_D1)
        pid = _post(cur, T_D1, text=f"{TOKEN} 好棒棒")
        _post_stance(cur, pid, "neg", target="別的目標")
        _post_stance(cur, pid, "pos", model="other-model")
        _post_stance(cur, pid, "pos", pv="post-v1")

    r = build_report(conn, TOKEN, stance_model="m", post_prompt_version="post-v9")
    assert _threads(r).posts == 1
    assert _threads(r).classified == 0
    assert _threads(r).suppressed_reason == "unclassified"

    # The same post, read under the version it was actually labeled with.
    other = build_report(conn, TOKEN, stance_model="m", post_prompt_version="post-v1")
    assert _threads(other).classified == 1 and _threads(other).pos == 1


def test_the_lean_window_is_the_incidents_taipei_days(conn):
    """A post from the day before the incident is not this incident's lean, and
    the window edges are Taipei midnights -- not UTC ones (invariant 3)."""
    with conn.cursor() as cur:
        _article(cur, A, T_D2)  # the incident is D2 only
        inside = _post(cur, T_D2, text=f"{TOKEN} 裡面")  # 01:00 Taipei on D2
        before = _post(cur, T_D2 - timedelta(hours=2), text=f"{TOKEN} 前一天")  # 23:00 on D1
        after = _post(cur, T_D2 + timedelta(hours=23, minutes=30), text=f"{TOKEN} 隔天")
        for pid in (inside, before, after):
            _post_stance(cur, pid, "neg")

    r = build_report(conn, TOKEN, stance_model="m", post_prompt_version="post-v9")
    assert (r.first_day, r.last_day) == (D2, D2)
    assert _threads(r).posts == 1, "only the post inside the Taipei day counts"
    assert _threads(r).classified == 1


def test_no_posts_is_distinguishable_from_no_verdicts(conn):
    with conn.cursor() as cur:
        _article(cur, A, T_D1)
    r = build_report(conn, TOKEN, stance_model="m", post_prompt_version="post-v9")
    assert _threads(r).suppressed_reason == "no_posts"
    assert _threads(r).posts == 0


def test_an_empty_report_has_no_lean_rather_than_a_zero(conn):
    """No window means no question to answer; a fabricated 0/0 would read as
    'nobody posted', which is not what the absence of an incident means."""
    r = build_report(conn, "zznosuchtoken")
    assert r.empty and r.platform_lean == ()

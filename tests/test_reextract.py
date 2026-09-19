"""reextract against Postgres, rolled back: reads the cache, never the network,
writes only bodies that changed, records a body that shrank below the minimum."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.rows import dict_row

from parallax.jobs import reextract as job
from parallax.settings import DATABASE_URL

OUTLET = "__reextract_test__"
T0 = datetime(2030, 1, 1, tzinfo=UTC)
PROSE = (
    "這是一段足夠長的正文，用來確保萃取結果超過最低字數門檻，所以再多寫幾個字以策安全，並且加上更多描述性內容讓長度合格。"
    * 2
)


class _NoCommit:
    """The job commits per article; inside a test that must not reach the disk."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        pass


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
        yield _NoCommit(connection)
    finally:
        connection.rollback()
        connection.close()


def _article(conn, i, body, path):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO article_index (outlet, url_canonical, url_original, title, published_at, seen_at)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (OUTLET, f"https://example.com/r{i}", f"https://example.com/r{i}", f"t{i}", T0, T0),
        )
        aid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO articles (id, body, body_seg, raw_html_path, enrich_state) "
            "VALUES (%s, %s, %s, %s, 'fetched')",
            (aid, body, body, str(path) if path else None),
        )
    return aid


def _page(tmp_path, name, inner):
    p = tmp_path / f"{name}.html.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write(f"<html><body><article>{inner}</article></body></html>")
    return p


def test_reextract_writes_only_changed_bodies_and_records_shrunk_ones(conn, tmp_path):
    same = _article(conn, 1, PROSE, _page(tmp_path, "same", f"<p>{PROSE}</p>"))
    rail = _article(
        conn,
        2,
        PROSE + "\n其他文章的標題",
        _page(tmp_path, "rail", f"<p>{PROSE}</p><a href='/x'><p>其他文章的標題</p></a>"),
    )
    short = _article(conn, 3, PROSE, _page(tmp_path, "short", "<p>短</p>"))
    missing = _article(conn, 4, PROSE, tmp_path / "nope.html.gz")

    stats = job.reextract_all(conn, outlets=[OUTLET])
    assert stats[OUTLET] == {"same": 1, "changed": 1, "short": 1, "missing": 1}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, body, enrich_state, enrich_error FROM articles WHERE id = ANY(%s) ORDER BY id",
            ([same, rail, short, missing],),
        )
        rows = {r["id"]: r for r in cur.fetchall()}
    assert rows[same]["body"] == PROSE
    assert rows[rail]["body"] == PROSE and rows[rail]["enrich_state"] == "fetched"
    assert rows[short]["enrich_state"] == "failed" and "reextract" in rows[short]["enrich_error"]
    assert rows[missing]["body"] == PROSE

    # Second pass: nothing left to change (the shrunk one is re-reported, not re-written).
    again = job.reextract_all(conn, outlets=[OUTLET])
    assert again[OUTLET] == {"same": 2, "short": 1, "missing": 1}


def test_dry_run_writes_nothing(conn, tmp_path):
    rail = _article(conn, 5, "舊內容", _page(tmp_path, "dry", f"<p>{PROSE}</p>"))
    stats = job.reextract_all(conn, outlets=[OUTLET], dry_run=True)
    assert stats[OUTLET] == {"changed": 1}
    with conn.cursor() as cur:
        cur.execute("SELECT body FROM articles WHERE id = %s", (rail,))
        assert cur.fetchone()["body"] == "舊內容"


def test_render_is_a_table():
    out = job.render({"tvbs": {"changed": 55}, "cna": {"same": 30, "short": 1}})
    assert out.splitlines()[0].split() == ["outlet", "changed", "same", "short", "missing"]
    assert out.splitlines()[1].split() == ["tvbs", "55", "0", "0", "0"]

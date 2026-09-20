from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import psycopg
import pytest
from psycopg.rows import dict_row

from parallax import db, settings
from parallax.jobs import social
from tests.test_threads_client import SINCE, UNTIL, client, response


def test_missing_token_no_db(monkeypatch, capsys):
    monkeypatch.setattr(settings, "THREADS_ACCESS_TOKEN", None)
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(db, "connect", connect)
    assert social.main(["--keyword", "沈伯洋"]) == 2
    assert capsys.readouterr().err == "THREADS_ACCESS_TOKEN is unset\n"
    connect.assert_not_called()


def test_taipei_dates(monkeypatch):
    now = datetime(2026, 9, 20, 5, 27, 43, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(social, "datetime", FixedDatetime)
    since, until = social.taipei_window("2026-09-19", "2026-09-20")
    assert datetime.fromtimestamp(since, UTC).isoformat() == "2026-09-18T16:00:00+00:00"
    assert datetime.fromtimestamp(until, UTC).isoformat() == "2026-09-19T16:00:00+00:00"

    since, until = social.taipei_window("2026-09-19", "2026-09-22")
    assert until == int(now.timestamp()) // 60 * 60
    default_since, default_until = social.taipei_window()
    assert default_until == until
    assert datetime.fromtimestamp(default_since, UTC).isoformat() == "2026-09-12T16:00:00+00:00"
    with pytest.raises(ValueError, match="increasing"):
        social.taipei_window("2026-09-21", "2026-09-22")


def test_refresh_cli(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("THREADS_ACCESS_TOKEN=secret-token\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "THREADS_ACCESS_TOKEN", "secret-token")
    c = client(tmp_path / "raw", [response("refresh.json")])
    monkeypatch.setattr(social, "ThreadsClient", lambda: c)
    assert social.main(["--refresh-token"]) == 0
    assert capsys.readouterr().out == "access_token=new-secret-token expires_in=5184000\n"
    assert env.read_text() == "THREADS_ACCESS_TOKEN=secret-token\n"


class MemoryConnection:
    """Minimal transaction-aware audit store for offline job failure tests."""

    def __init__(self, used=0):
        self.used = used
        self.run = {}

    def execute(self, sql, params=()):
        if "AS used" in sql:
            return Mock(fetchone=lambda: {"used": self.used})
        if "RETURNING run_id" in sql:
            return Mock(fetchone=lambda: {"run_id": 1})
        if "SET finished_at=now(), queries=" in sql:
            self.run = dict(zip(("queries", "items_seen", "items_new", "ok", "error"), params))
        return Mock()

    def commit(self):
        pass

    def rollback(self):
        pass


def test_budget_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "THREADS_DAILY_QUERY_BUDGET", 1000)
    conn = MemoryConnection(999)
    c = client(tmp_path, [])
    result = social.ingest(conn, "x", SINCE, UNTIL, client=c)
    assert not result["ok"] and "budget exceeded" in result["error"]
    assert conn.run == result
    assert c.queries == 0


@pytest.mark.parametrize("status", [400, 401, 403])
def test_page_failure_no_partial_rows(tmp_path, monkeypatch, status):
    c = client(
        tmp_path, [response("me.json"), response("page1.json"), response("error.json", status)]
    )
    write = Mock(side_effect=AssertionError("no partial writes"))
    monkeypatch.setattr(db, "upsert_social_posts", write)
    conn = MemoryConnection()
    result = social.ingest(conn, "x", SINCE, UNTIL, client=c)
    assert not result["ok"] and result["items_seen"] == 1
    assert result["queries"] == 3 and "Invalid token" in result["error"]
    assert "secret-token" not in result["error"]
    assert conn.run == result
    write.assert_not_called()


def test_own_posts(tmp_path, monkeypatch):
    c = client(tmp_path, [response("me.json"), response("own.json")])
    write = Mock()
    monkeypatch.setattr(db, "upsert_social_posts", write)
    conn = MemoryConnection()
    result = social.ingest(conn, "x", SINCE, UNTIL, client=c)
    assert result["error"] == social.OWN_POSTS_ERROR and not result["ok"]
    assert conn.run == result
    write.assert_not_called()


def test_retry_cannot_exceed_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "THREADS_DAILY_QUERY_BUDGET", 2)
    monkeypatch.setattr("parallax.social.threads.time.sleep", lambda _: None)
    c = client(tmp_path, [response("me.json"), response("error.json", 429)])
    result = social.ingest(MemoryConnection(), "x", SINCE, UNTIL, limit=100, client=c)
    assert not result["ok"] and "exhausted" in result["error"]
    assert result["queries"] == c.queries == 2


def test_dry_run_no_api(tmp_path):
    c = client(tmp_path, [])
    conn = MemoryConnection()
    result = social.ingest(conn, "x", SINCE, UNTIL, dry_run=True, client=c)
    assert result["ok"] and result["queries"] == result["items_new"] == 0
    assert result["items_seen"] == 0 and result["error"] == "dry run"
    assert conn.run == result
    c.session.get.assert_not_called()


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(settings.DATABASE_URL, connect_timeout=3, row_factory=dict_row)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable: {exc}")
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def test_upsert_and_keyword_fallback(conn):
    post = response("page2.json").json()["data"][0]
    post.update(permalink="https://example.com/__social_test__", raw_path="raw/fixture.json.gz")
    assert db.upsert_social_posts(conn, [post], "zzsocialtest") == 1
    first = conn.execute(
        "SELECT * FROM social_posts WHERE post_url=%s", (post["permalink"],)
    ).fetchone()
    post.update(text="replacement", username="replacement")
    assert db.upsert_social_posts(conn, [post], "replacement") == 0
    rows = db.find_social_posts(
        conn,
        "zzsocialtest",
        "threads",
        datetime(2026, 9, 18, tzinfo=UTC),
        datetime(2026, 9, 21, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0]["text_seg"] and rows[0]["raw_path"]
    assert rows[0]["author"] == first["author"]
    assert rows[0]["text"] == first["text"]
    assert rows[0]["seen_at"] >= first["seen_at"]
    assert not db.find_social_posts(
        conn,
        "zzsocialtest",
        "facebook",
        datetime(2026, 9, 18, tzinfo=UTC),
        datetime(2026, 9, 21, tzinfo=UTC),
    )


def test_schema_mirror(conn):
    root = Path(__file__).resolve().parents[1]
    schema = (root / "db/schema.sql").read_text()
    migration = (root / "db/migrations/003_social_posts_threads.sql").read_text()
    # Isolated transactional schemas: compare fresh install to the original table upgraded twice.
    snapshots = []
    for name in ("__threads_fresh", "__threads_upgrade"):
        conn.execute(f"CREATE SCHEMA {name}")
        conn.execute(f"SET LOCAL search_path TO {name}, public")
        if name.endswith("fresh"):
            # Only this ticket's tables; other schema objects have independent migrations.
            conn.execute(schema[schema.index("CREATE TABLE IF NOT EXISTS social_posts") :])
        else:
            old = schema[schema.index("CREATE TABLE IF NOT EXISTS social_posts") :]
            conn.execute(old[: old.index("ALTER TABLE social_posts")])
        conn.execute(migration)
        conn.execute(migration)
        snapshots.append(
            conn.execute(
                "SELECT table_name, column_name, data_type, is_nullable, "
                "regexp_replace(column_default, %s, '', 'g') AS def "
                "FROM information_schema.columns WHERE table_schema=%s ORDER BY table_name, ordinal_position",
                (name + r"\.", name),
            ).fetchall()
        )
        snapshots[-1] = (
            snapshots[-1],
            conn.execute(
                "SELECT tablename, indexname, "
                "regexp_replace(indexdef, %s, '', 'g') AS indexdef "
                "FROM pg_indexes WHERE schemaname=%s ORDER BY tablename, indexname",
                (name + r"\.", name),
            ).fetchall(),
        )
    assert snapshots[0] == snapshots[1]


def test_job_db_records_and_rollback(conn, tmp_path, monkeypatch):
    # Preserve an outer transaction even when the job commits per-request audit charges.
    conn.execute("SAVEPOINT job_commit")

    class TransactionProxy:
        execute = conn.execute

        def commit(self):
            conn.execute("RELEASE SAVEPOINT job_commit")
            conn.execute("SAVEPOINT job_commit")

        def rollback(self):
            conn.execute("ROLLBACK TO SAVEPOINT job_commit")

    used = conn.execute("SELECT coalesce(sum(queries), 0) AS used FROM social_runs").fetchone()["used"]
    monkeypatch.setattr(settings, "THREADS_DAILY_QUERY_BUDGET", used + 1000)
    c = client(tmp_path, [response("me.json"), response("own.json")])
    result = social.ingest(TransactionProxy(), "zzsocialjob", SINCE, UNTIL, client=c)
    row = conn.execute("SELECT * FROM social_runs WHERE keyword='zzsocialjob'").fetchone()
    assert row["finished_at"] is not None
    for key in result:
        assert row[key] == result[key]

    # An approved response commits posts and audit counters together.
    # A repeat run fetches every page again.
    c = client(
        tmp_path / "approved", [response("me.json"), response("page1.json"), response("page2.json")]
    )
    first = social.ingest(TransactionProxy(), "zzsocialapproved", SINCE, UNTIL, client=c)
    assert first["ok"] and first["items_new"] == 2 and first["queries"] == 3
    c = client(
        tmp_path / "approved", [response("me.json"), response("page1.json"), response("page2.json")]
    )
    second = social.ingest(TransactionProxy(), "zzsocialapproved", SINCE, UNTIL, client=c)
    assert second["ok"] and second["items_new"] == 0 and second["queries"] == 3
    rows = conn.execute(
        "SELECT * FROM social_posts WHERE fetched_for='zzsocialapproved'"
    ).fetchall()
    assert len(rows) == 2
    assert all(row["text_seg"] and row["raw_path"] for row in rows)


def test_write_failure_records_zero_new(tmp_path, monkeypatch):
    c = client(tmp_path, [response("me.json"), response("page2.json")])
    monkeypatch.setattr(db, "upsert_social_posts", Mock(side_effect=ValueError("bad timestamp")))
    conn = MemoryConnection()
    result = social.ingest(conn, "x", SINCE, UNTIL, client=c)
    assert not result["ok"] and result["items_new"] == 0
    assert result["items_seen"] == 1 and result["error"] == "bad timestamp"
    assert conn.run == result


def test_cli_api_failure_exit(tmp_path, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def connect():
        yield MemoryConnection()

    c = client(tmp_path, [response("error.json", 403)])
    monkeypatch.setattr(settings, "THREADS_ACCESS_TOKEN", "secret-token")
    monkeypatch.setattr(social, "ThreadsClient", lambda: c)
    monkeypatch.setattr(db, "connect", connect)
    assert social.main(["--keyword", "x"]) == 1


def test_interrupted_import_rolls_back(tmp_path, monkeypatch):
    c = client(tmp_path, [response("me.json"), response("page2.json")])
    monkeypatch.setattr(db, "upsert_social_posts", Mock(side_effect=KeyboardInterrupt))
    conn = MemoryConnection()
    conn.rollback = Mock()
    with pytest.raises(KeyboardInterrupt):
        social.ingest(conn, "x", SINCE, UNTIL, client=c)
    assert not conn.run["ok"] and conn.run["items_new"] == 0
    assert conn.run["error"] == "KeyboardInterrupt"
    assert conn.rollback.call_count >= 1

"""The public demo page shares a host with tier 1 (T-029). These pin the guards
that keep it from hurting the crawl: the page's unit is capped and kept out of
sched.install, the proxy only reaches loopback, and the database role it reads
as can read and do nothing else."""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
import pytest

from parallax.settings import DATABASE_URL

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "ops" / "demo" / "parallax-ui.service"
CADDYFILE = ROOT / "ops" / "demo" / "Caddyfile"
MIGRATION = ROOT / "db" / "migrations" / "004_ui_readonly_role.sql"


def _directives(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line and not line.startswith(("#", "[")) and "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


def test_page_unit_is_capped_and_sacrificed_before_the_crawl():
    d = _directives(UNIT.read_text())
    assert re.fullmatch(r"\d+[KMG]", d["MemoryMax"]), "a memory cap, not infinity"
    assert int(d["OOMScoreAdjust"]) > 0, "the OOM killer must pick the page first"
    assert int(d["Nice"]) > 0


def test_page_reads_the_readonly_url_and_listens_on_loopback_only():
    d = _directives(UNIT.read_text())
    assert d["EnvironmentFile"] == "@@ROOT@@/.env.ui"
    assert "--server.address=127.0.0.1" in d["ExecStart"]
    assert "--server.port=8501" in d["ExecStart"]


def test_sched_install_never_installs_a_web_server():
    """sched.install installs every unit in ops/systemd/; the page is not one."""
    for unit in (ROOT / "ops" / "systemd").iterdir():
        assert "streamlit" not in unit.read_text(), unit.name


def test_proxy_reaches_only_the_loopback_page():
    upstreams = re.findall(r"reverse_proxy\s+(\S+)", CADDYFILE.read_text())
    assert upstreams == ["127.0.0.1:8501"]


def test_readonly_env_file_is_never_committed():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env.ui" in ignored


def test_migration_grants_select_and_nothing_else():
    sql = MIGRATION.read_text()
    flags = re.MULTILINE | re.IGNORECASE
    grants = re.findall(r"^\s*(?:ALTER DEFAULT PRIVILEGES .*?)?GRANT (\w+)", sql, flags)
    assert grants and {g.upper() for g in grants} <= {"USAGE", "SELECT"}, grants


# ---- against Postgres (CI applies every migration before the suite) --------


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(DATABASE_URL, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable: {exc}")
    with connection:
        yield connection


def test_role_is_bounded(conn):
    row = conn.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolconnlimit, rolconfig "
        "FROM pg_roles WHERE rolname = 'parallax_ro'"
    ).fetchone()
    assert row, "migration 004 not applied: run make db.migrate"
    superuser, createdb, createrole, connlimit, config = row
    assert not (superuser or createdb or createrole)
    assert 0 < connlimit <= 20, "a crowd must not use up max_connections"
    settings = dict(c.split("=", 1) for c in config or [])
    assert settings["default_transaction_read_only"] == "on"
    assert settings["statement_timeout"] == "15s"


def test_role_can_read_every_table_and_write_none(conn):
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    ]
    assert "article_index" in tables
    for t in tables:
        for priv, expected in (("SELECT", True), ("INSERT", False), ("UPDATE", False),
                               ("DELETE", False), ("TRUNCATE", False)):
            got = conn.execute(
                "SELECT has_table_privilege('parallax_ro', %s, %s)", (f"public.{t}", priv)
            ).fetchone()[0]
            assert got is expected, f"parallax_ro {priv} on {t}: {got}"

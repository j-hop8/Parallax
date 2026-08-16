from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator

import psycopg
from psycopg.rows import dict_row

from .models import ArticleStub, CrawlResult
from .settings import DATABASE_URL
from .urls import canonicalize


@contextlib.contextmanager
def connect() -> Iterator[psycopg.Connection]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


# Every tier-1 write goes through this one function. When Kafka arrives in
# Phase 3, the producer slots in here and no adapter changes.
_UPSERT = """
INSERT INTO article_index (outlet, url_canonical, url_original, title, title_seg, published_at)
VALUES (%(outlet)s, %(url_canonical)s, %(url_original)s, %(title)s, %(title_seg)s, %(published_at)s)
ON CONFLICT (outlet, url_canonical) DO NOTHING
RETURNING id
"""


def upsert_article_index(
    conn: psycopg.Connection,
    stubs: Iterable[ArticleStub],
    segment: bool = True,
) -> tuple[int, int]:
    """Insert stubs, ignoring ones already seen. Returns (seen, new).

    First sight wins: a conflicting row is left untouched so seen_at -- and
    therefore effective_at -- keeps recording when we genuinely first saw the
    article, not when we last re-polled it.
    """
    from .nlp.segment import segment_text

    seen = 0
    new = 0
    with conn.cursor() as cur:
        for stub in stubs:
            seen += 1
            cur.execute(
                _UPSERT,
                {
                    "outlet": stub.outlet,
                    "url_canonical": canonicalize(stub.url_original),
                    "url_original": stub.url_original,
                    "title": stub.title,
                    "title_seg": segment_text(stub.title) if segment else None,
                    "published_at": stub.published_at,
                },
            )
            if cur.fetchone() is not None:
                new += 1
    return seen, new


def record_crawl_run(conn: psycopg.Connection, result: CrawlResult) -> None:
    """Persist one outlet's crawl outcome, success or failure.

    Failures matter more than successes here: a silently dead adapter loses
    tier-1 data permanently, and this row is the only way to notice.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawl_runs (outlet, finished_at, items_seen, items_new, ok, error)
            VALUES (%s, now(), %s, %s, %s, %s)
            """,
            (result.outlet, result.items_seen, result.items_new, result.ok, result.error),
        )


def ensure_outlets(conn: psycopg.Connection, outlets: Iterable) -> None:
    """Seed the outlets table from config. Idempotent."""
    with conn.cursor() as cur:
        for o in outlets:
            cur.execute(
                """
                INSERT INTO outlets (code, name_zh, home_url)
                VALUES (%s, %s, %s)
                ON CONFLICT (code) DO UPDATE
                    SET name_zh = EXCLUDED.name_zh, home_url = EXCLUDED.home_url
                """,
                (o.code, o.name_zh, o.home_url),
            )

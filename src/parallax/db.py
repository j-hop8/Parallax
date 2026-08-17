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


def save_enriched(
    conn: psycopg.Connection,
    *,
    article_id: int,
    body: str | None,
    body_seg: str | None,
    raw_html_path: str,
) -> None:
    """Upsert the tier-2 row and flag the index entry as fetched.

    Idempotent so an interrupted enrich run can simply be re-run: the same
    article re-processed overwrites its own row rather than erroring or
    duplicating.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO articles (id, body, body_seg, raw_html_path, enrich_state, updated_at)
            VALUES (%s, %s, %s, %s, 'fetched', now())
            ON CONFLICT (id) DO UPDATE
                SET body = EXCLUDED.body,
                    body_seg = EXCLUDED.body_seg,
                    raw_html_path = EXCLUDED.raw_html_path,
                    enrich_state = 'fetched',
                    enrich_error = NULL,
                    updated_at = now()
            """,
            (article_id, body, body_seg, raw_html_path),
        )
        cur.execute("UPDATE article_index SET body_fetched = TRUE WHERE id = %s", (article_id,))


def backfill_published_at(conn: psycopg.Connection, article_id: int, published_at) -> None:
    """Write a timestamp recovered from the article page into article_index.

    This is what makes the four dateless outlets usable in a propagation chain.
    effective_at is a generated column, so it recomputes automatically and the
    article immediately sorts by its real publish time instead of by when we
    happened to crawl it.

    Guarded with `published_at IS NULL` so a feed-supplied timestamp is never
    overwritten by a scraped one, even if this runs twice.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE article_index SET published_at = %s WHERE id = %s AND published_at IS NULL",
            (published_at, article_id),
        )


def mark_enrich_failed(conn: psycopg.Connection, article_id: int, error: str) -> None:
    """Record that one article could not be enriched, without losing the reason."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO articles (id, enrich_state, enrich_error, updated_at)
            VALUES (%s, 'failed', %s, now())
            ON CONFLICT (id) DO UPDATE
                SET enrich_state = 'failed',
                    enrich_error = EXCLUDED.enrich_error,
                    updated_at = now()
            """,
            (article_id, error[:2000]),
        )

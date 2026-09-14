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
            INSERT INTO crawl_runs
                (outlet, started_at, finished_at, items_seen, items_new, ok, error)
            VALUES (%s, %s, now(), %s, %s, %s, %s)
            """,
            (
                result.outlet,
                result.started_at,
                result.items_seen,
                result.items_new,
                result.ok,
                result.error,
            ),
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


# ---- Q1: stance -----------------------------------------------------------

_ENRICHED_FOR_KEYWORD = """
SELECT ai.id, ai.outlet, ai.title, ai.url_original, ai.effective_at, a.body
FROM article_index ai
JOIN articles a ON a.id = ai.id
   , plainto_tsquery('simple', %(q)s) query
WHERE to_tsvector('simple', ai.title_seg) @@ query
  AND a.body IS NOT NULL AND a.body <> ''
ORDER BY ai.effective_at DESC
LIMIT %(limit)s
"""


def find_enriched_articles(conn: psycopg.Connection, keyword: str, limit: int = 500) -> list[dict]:
    """Keyword matches that have a body -- the only ones stance can be judged on.

    Same title match as search.find_articles (and the same rule: the keyword
    must be segmented before it reaches Postgres, invariant 6) joined to the
    tier-2 row. An article that matched but was never enriched is not an
    error here; it is simply not yet classifiable, and `make enrich` is the fix.
    """
    from .nlp.segment import segment_text

    segmented = segment_text(keyword)
    if not segmented:
        return []
    with conn.cursor() as cur:
        cur.execute(_ENRICHED_FOR_KEYWORD, {"q": segmented, "limit": limit})
        return cur.fetchall()


def get_stance(
    conn: psycopg.Connection, article_id: int, target: str, model: str, prompt_version: str
) -> dict | None:
    """The cached verdict for this exact (article, target, model, prompt), if any."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT label, confidence, evidence, created_at
            FROM article_stance
            WHERE article_id = %s AND target = %s AND model = %s AND prompt_version = %s
            """,
            (article_id, target, model, prompt_version),
        )
        return cur.fetchone()


def save_stance(
    conn: psycopg.Connection,
    *,
    article_id: int,
    target: str,
    model: str,
    prompt_version: str,
    label: str,
    confidence: float,
    evidence: str,
) -> None:
    """Upsert one verdict. The key is the cache key, so a re-run overwrites itself."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO article_stance
                (article_id, target, model, prompt_version, label, confidence, evidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (article_id, target, model, prompt_version) DO UPDATE
                SET label = EXCLUDED.label,
                    confidence = EXCLUDED.confidence,
                    evidence = EXCLUDED.evidence,
                    created_at = now()
            """,
            (article_id, target, model, prompt_version, label, confidence, evidence),
        )


def stance_by_outlet(
    conn: psycopg.Connection, target: str, model: str, prompt_version: str
) -> list[dict]:
    """Q1 in one query: per outlet, how many neg / neu / pos toward the target."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ai.outlet,
                   count(*) FILTER (WHERE s.label = 'neg') AS neg,
                   count(*) FILTER (WHERE s.label = 'neu') AS neu,
                   count(*) FILTER (WHERE s.label = 'pos') AS pos,
                   count(*) AS n
            FROM article_stance s
            JOIN article_index ai ON ai.id = s.article_id
            WHERE s.target = %s AND s.model = %s AND s.prompt_version = %s
            GROUP BY ai.outlet
            ORDER BY ai.outlet
            """,
            (target, model, prompt_version),
        )
        return cur.fetchall()


def articles_by_ids(conn: psycopg.Connection, ids: Iterable[int]) -> list[dict]:
    """Enriched rows for specific ids -- what the eval needs to classify gold rows
    that have no cached verdict yet. Rows without a body are omitted."""
    ids = list(ids)
    if not ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ai.id, ai.outlet, ai.title, ai.url_original, ai.effective_at, a.body
            FROM article_index ai
            JOIN articles a ON a.id = ai.id
            WHERE ai.id = ANY(%s) AND a.body IS NOT NULL AND a.body <> ''
            """,
            (ids,),
        )
        return cur.fetchall()


# ---- Q3: near-duplicate clusters -------------------------------------------


def enriched_for_dedup(conn: psycopg.Connection) -> list[dict]:
    """Every article with a segmented body: the whole tier-2 corpus.

    Clusters are corpus-wide, not keyword-wide -- a wire copy matches whatever
    keywords it matches, and an article enriched for one keyword can be the
    origin of a cluster found under another.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ai.id, ai.outlet, ai.title, ai.url_original, ai.effective_at, ai.published_at,
                   a.body, a.body_seg, a.simhash, a.dup_cluster_id
            FROM articles a
            JOIN article_index ai ON ai.id = a.id
            WHERE a.body_seg IS NOT NULL AND a.body_seg <> ''
            ORDER BY ai.id
            """
        )
        return cur.fetchall()


def save_fingerprint(
    conn: psycopg.Connection, article_id: int, simhash_signed: int, bands: tuple[int, int, int, int]
) -> int:
    """Write the SimHash and its bands. Returns 1 if anything changed, else 0,
    so a re-run can prove it was a no-op."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE articles
               SET simhash = %(h)s, band0 = %(b0)s, band1 = %(b1)s, band2 = %(b2)s, band3 = %(b3)s
             WHERE id = %(id)s
               AND (simhash IS DISTINCT FROM %(h)s OR band0 IS DISTINCT FROM %(b0)s
                    OR band1 IS DISTINCT FROM %(b1)s OR band2 IS DISTINCT FROM %(b2)s
                    OR band3 IS DISTINCT FROM %(b3)s)
            """,
            {
                "id": article_id,
                "h": simhash_signed,
                "b0": bands[0],
                "b1": bands[1],
                "b2": bands[2],
                "b3": bands[3],
            },
        )
        return cur.rowcount


def replace_clusters(
    conn: psycopg.Connection, clusters: list, scope_ids: list[int]
) -> dict[str, int]:
    """Make the stored clusters equal to `clusters` for the articles in scope.

    A reconcile, not a wipe-and-rewrite: rows are touched only where the
    stored cluster id / origin flag / rank differ from what the run computed,
    so a second identical run reports zeros everywhere -- which is how the
    job proves it is idempotent. Order is FK-safe: clusters are upserted
    first so every id a member will point at exists, members are re-pointed
    or detached next, and only then are clusters nobody references deleted.
    shared_core_text is left alone -- T-009 owns it.
    """
    counts = {
        "clusters_upserted": 0,
        "clusters_deleted": 0,
        "members_set": 0,
        "members_detached": 0,
    }
    desired: dict[int, tuple[int, bool, int]] = {}
    for c in clusters:
        for rank, m in enumerate(c.members, 1):
            desired[m.article_id] = (c.cluster_id, rank == 1, rank)
    keep_ids = [c.cluster_id for c in clusters]

    with conn.cursor() as cur:
        for c in clusters:
            cur.execute(
                """
                INSERT INTO dup_clusters
                    (cluster_id, member_count, origin_article_id, first_published_at,
                     origin_confident, computed_at)
                VALUES (%(id)s, %(n)s, %(origin)s, %(first)s, %(conf)s, now())
                ON CONFLICT (cluster_id) DO UPDATE
                    SET member_count = EXCLUDED.member_count,
                        origin_article_id = EXCLUDED.origin_article_id,
                        first_published_at = EXCLUDED.first_published_at,
                        origin_confident = EXCLUDED.origin_confident,
                        computed_at = now()
                  WHERE dup_clusters.member_count IS DISTINCT FROM EXCLUDED.member_count
                     OR dup_clusters.origin_article_id IS DISTINCT FROM EXCLUDED.origin_article_id
                     OR dup_clusters.first_published_at IS DISTINCT FROM EXCLUDED.first_published_at
                     OR dup_clusters.origin_confident IS DISTINCT FROM EXCLUDED.origin_confident
                """,
                {
                    "id": c.cluster_id,
                    "n": len(c.members),
                    "origin": c.origin.article_id,
                    "first": c.first_published_at,
                    "conf": c.origin_confident,
                },
            )
            counts["clusters_upserted"] += cur.rowcount

        for article_id, (cluster_id, is_origin, rank) in desired.items():
            cur.execute(
                """
                UPDATE articles
                   SET dup_cluster_id = %(c)s, is_cluster_origin = %(o)s, cluster_rank = %(r)s
                 WHERE id = %(id)s
                   AND (dup_cluster_id IS DISTINCT FROM %(c)s
                        OR is_cluster_origin IS DISTINCT FROM %(o)s
                        OR cluster_rank IS DISTINCT FROM %(r)s)
                """,
                {"id": article_id, "c": cluster_id, "o": is_origin, "r": rank},
            )
            counts["members_set"] += cur.rowcount

        cur.execute(
            """
            UPDATE articles
               SET dup_cluster_id = NULL, is_cluster_origin = FALSE, cluster_rank = NULL
             WHERE id = ANY(%s) AND NOT (id = ANY(%s)) AND dup_cluster_id IS NOT NULL
            """,
            (scope_ids, list(desired) or [0]),
        )
        counts["members_detached"] = cur.rowcount

        cur.execute(
            """
            DELETE FROM dup_clusters
             WHERE NOT (cluster_id = ANY(%s))
               AND NOT EXISTS (SELECT 1 FROM articles WHERE dup_cluster_id = dup_clusters.cluster_id)
            """,
            (keep_ids or [0],),
        )
        counts["clusters_deleted"] = cur.rowcount
        # Keep the sequence ahead of explicit ids so nothing else ever collides.
        cur.execute(
            "SELECT setval('dup_clusters_cluster_id_seq', "
            "GREATEST((SELECT COALESCE(MAX(cluster_id), 0) FROM dup_clusters), 1))"
        )
    return counts

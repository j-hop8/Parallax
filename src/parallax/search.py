from __future__ import annotations

import psycopg

from .nlp.segment import segment_text

# Single seam for keyword lookup. Phase 2 replaces this function body with an
# Elasticsearch query; nothing above it needs to change.
_SEARCH = """
SELECT ai.id, ai.outlet, ai.title, ai.url_original, ai.url_canonical,
       ai.published_at, ai.seen_at, ai.effective_at,
       ts_rank(to_tsvector('simple', ai.title_seg), query) AS rank
FROM article_index ai, plainto_tsquery('simple', %(q)s) query
WHERE to_tsvector('simple', ai.title_seg) @@ query
ORDER BY ai.effective_at DESC
LIMIT %(limit)s
"""


def find_articles(conn: psycopg.Connection, keyword: str, limit: int = 200) -> list[dict]:
    """Find articles whose title matches a keyword.

    The query is segmented with the same dictionary as the indexed text. Skipping
    that is the single easiest way to get zero results from a database that
    plainly contains the article: Postgres has no Chinese parser, so
    '看護虐待' indexed as '看護 虐待' cannot match an unsegmented query term.
    """
    segmented = segment_text(keyword)
    if not segmented:
        return []
    with conn.cursor() as cur:
        cur.execute(_SEARCH, {"q": segmented, "limit": limit})
        return cur.fetchall()


# Everything the incident report needs from tier 1, for every match: no LIMIT,
# metadata only. Downstream metrics join on these ids -- deliberately the shape
# an Elasticsearch swap hands back, so the report code survives the change.
_MATCH_ALL = """
SELECT ai.id, ai.outlet, ai.title, ai.published_at, ai.seen_at, ai.effective_at
FROM article_index ai, plainto_tsquery('simple', %(q)s) query
WHERE to_tsvector('simple', ai.title_seg) @@ query
ORDER BY ai.effective_at, ai.id
"""


def match_all(conn: psycopg.Connection, keyword: str) -> list[dict]:
    """Every tier-1 article whose title matches the keyword, oldest first.

    Same predicate and the same segmentation rule as find_articles; the two
    must never drift, or the report would count articles the search cannot
    show.
    """
    segmented = segment_text(keyword)
    if not segmented:
        return []
    with conn.cursor() as cur:
        cur.execute(_MATCH_ALL, {"q": segmented})
        return cur.fetchall()

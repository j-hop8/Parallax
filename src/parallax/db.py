from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator
from datetime import datetime

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

    T-009's columns are cleared, never computed, here: a member that moves to
    another cluster or leaves one has deltas that compare it to the wrong
    text, and a cluster whose membership changed has a stale core. Deltas are
    relative to a reference -- the origin, or the core when the order is
    indeterminate -- so when a cluster's origin or its `origin_confident`
    flips, every member's deltas and summary go too: a member joining inside
    the noise floor would otherwise leave directional `delta_removed` rows on
    a cluster the UI must now present as unordered (invariant 5). A rank
    change that moves neither keeps them -- `make framing` recomputes and
    clears the summary only if the deltas actually differ, so quota is not
    re-spent on a no-op.
    """
    counts = {
        "clusters_upserted": 0,
        "clusters_deleted": 0,
        "members_set": 0,
        "members_reset": 0,  # framing cleared because the cluster's reference changed
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
                "SELECT origin_article_id, origin_confident FROM dup_clusters WHERE cluster_id = %s",
                (c.cluster_id,),
            )
            stored = cur.fetchone()
            if stored is not None and (
                stored["origin_article_id"] != c.origin.article_id
                or stored["origin_confident"] != c.origin_confident
            ):
                cur.execute(
                    """
                    UPDATE articles
                       SET delta_added = NULL, delta_removed = NULL, delta_summary = NULL,
                           delta_summary_model = NULL, delta_summary_version = NULL
                     WHERE dup_cluster_id = %s
                       AND (delta_added IS NOT NULL OR delta_removed IS NOT NULL
                            OR delta_summary IS NOT NULL)
                    """,
                    (c.cluster_id,),
                )
                counts["members_reset"] += cur.rowcount
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
                        shared_core_text = NULL,
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
                   SET dup_cluster_id = %(c)s, is_cluster_origin = %(o)s, cluster_rank = %(r)s,
                       delta_added = CASE WHEN dup_cluster_id IS DISTINCT FROM %(c)s
                                          THEN NULL ELSE delta_added END,
                       delta_removed = CASE WHEN dup_cluster_id IS DISTINCT FROM %(c)s
                                            THEN NULL ELSE delta_removed END,
                       delta_summary = CASE WHEN dup_cluster_id IS DISTINCT FROM %(c)s
                                            THEN NULL ELSE delta_summary END,
                       delta_summary_model = CASE WHEN dup_cluster_id IS DISTINCT FROM %(c)s
                                                  THEN NULL ELSE delta_summary_model END,
                       delta_summary_version = CASE WHEN dup_cluster_id IS DISTINCT FROM %(c)s
                                                    THEN NULL ELSE delta_summary_version END
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
               SET dup_cluster_id = NULL, is_cluster_origin = FALSE, cluster_rank = NULL,
                   delta_added = NULL, delta_removed = NULL, delta_summary = NULL,
                   delta_summary_model = NULL, delta_summary_version = NULL
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


# ---- Q3: framing delta -----------------------------------------------------


def bodies_by_outlet(conn: psycopg.Connection) -> dict[str, list[str]]:
    """Every enriched body, grouped by outlet: the input to sentence boilerplate."""
    out: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ai.outlet, a.body
            FROM articles a JOIN article_index ai ON ai.id = a.id
            WHERE a.body IS NOT NULL AND a.body <> ''
            ORDER BY ai.outlet, ai.id
            """
        )
        for row in cur.fetchall():
            out.setdefault(row["outlet"], []).append(row["body"])
    return out


def clusters_for_framing(conn: psycopg.Connection) -> list[dict]:
    """Stored clusters with their members ranked, bodies included.

    Members carry published_at so the job can rebuild the cluster's
    order-indeterminate reason with nlp.dedup.build_cluster; the reason is
    not stored. A cluster with fewer than two members with a body is skipped
    by the caller, not here."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.cluster_id, c.origin_confident, c.shared_core_text,
                   ai.id, ai.outlet, ai.title, ai.effective_at, ai.published_at,
                   a.cluster_rank, a.body,
                   a.delta_added, a.delta_removed, a.delta_summary,
                   a.delta_summary_model, a.delta_summary_version
            FROM dup_clusters c
            JOIN articles a ON a.dup_cluster_id = c.cluster_id
            JOIN article_index ai ON ai.id = a.id
            ORDER BY c.first_published_at, c.cluster_id, a.cluster_rank, ai.id
            """
        )
        rows = cur.fetchall()
    clusters: dict[int, dict] = {}
    for r in rows:
        c = clusters.setdefault(
            r["cluster_id"],
            {
                "cluster_id": r["cluster_id"],
                "origin_confident": r["origin_confident"],
                "shared_core_text": r["shared_core_text"],
                "members": [],
            },
        )
        c["members"].append(
            {
                k: v
                for k, v in r.items()
                if k not in ("cluster_id", "origin_confident", "shared_core_text")
            }
        )
    return list(clusters.values())


def save_framing(
    conn: psycopg.Connection,
    cluster_id: int,
    core_text: str,
    members: list[tuple[int, list[str], list[str], str | None]],
) -> dict[str, int]:
    """Write one cluster's core and per-member deltas; returns what changed.

    `members` is (article_id, added, removed, rule_summary). The summary is
    the cache for the LLM step: when a member's deltas change it is reset to
    the rule string (origins, empty deltas) or to NULL, which is what marks
    the row as pending. Unchanged deltas leave an existing summary alone, so
    re-running `make framing` never re-spends quota.
    """
    from .nlp.summary import RULE_MODEL, SUMMARY_VERSION

    counts = {"cores_set": 0, "deltas_set": 0, "summaries_reset": 0}
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE dup_clusters SET shared_core_text = %(t)s
             WHERE cluster_id = %(id)s AND shared_core_text IS DISTINCT FROM %(t)s
            """,
            {"id": cluster_id, "t": core_text},
        )
        counts["cores_set"] += cur.rowcount

        for article_id, added, removed, rule in members:
            cur.execute(
                "SELECT delta_added, delta_removed, delta_summary, delta_summary_version "
                "FROM articles WHERE id = %s",
                (article_id,),
            )
            row = cur.fetchone()
            if row is None:
                continue
            deltas_changed = row["delta_added"] != list(added) or row["delta_removed"] != list(
                removed
            )
            # A rule row is rewritten when its string or its version label is stale,
            # so provenance never says an older version than the LLM rows beside it.
            summary_wrong = rule is not None and (
                row["delta_summary"] != rule or row["delta_summary_version"] != SUMMARY_VERSION
            )
            if not deltas_changed and not summary_wrong:
                continue
            if deltas_changed:
                counts["deltas_set"] += 1
            if deltas_changed or summary_wrong:
                counts["summaries_reset"] += 1
            cur.execute(
                """
                UPDATE articles
                   SET delta_added = %(a)s, delta_removed = %(r)s,
                       delta_summary = %(s)s,
                       delta_summary_model = %(m)s,
                       delta_summary_version = %(v)s,
                       updated_at = now()
                 WHERE id = %(id)s
                """,
                {
                    "id": article_id,
                    "a": list(added),
                    "r": list(removed),
                    "s": rule,
                    "m": RULE_MODEL if rule is not None else None,
                    "v": SUMMARY_VERSION if rule is not None else None,
                },
            )
    return counts


def save_summary(
    conn: psycopg.Connection, article_id: int, summary: str, model: str, version: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE articles
               SET delta_summary = %s, delta_summary_model = %s, delta_summary_version = %s,
                   updated_at = now()
             WHERE id = %s
            """,
            (summary, model, version, article_id),
        )


# ---- T-010: incident report reads --------------------------------------------
# All keyed by the matched article ids from search.match_all. Nothing here
# knows the keyword; that keeps the FTS predicate in one place (search.py).


def daily_totals(
    conn: psycopg.Connection, outlets: Iterable[str], since, until
) -> list[dict]:
    """Rollup rows for these outlets between two Taipei dates, inclusive."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT outlet, day, total_articles, complete
            FROM outlet_daily_totals
            WHERE outlet = ANY(%s) AND day BETWEEN %s AND %s
            """,
            (list(outlets), since, until),
        )
        return cur.fetchall()


def complete_day_totals(conn: psycopg.Connection) -> dict[str, list[int]]:
    """Every complete day's total per outlet -- the raw material for a baseline."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT outlet, total_articles FROM outlet_daily_totals WHERE complete ORDER BY outlet"
        )
        out: dict[str, list[int]] = {}
        for r in cur.fetchall():
            out.setdefault(r["outlet"], []).append(r["total_articles"])
        return out


def rollup_as_of(conn: psycopg.Connection):
    """When the denominator was last recomputed; None before the first rollup."""
    with conn.cursor() as cur:
        cur.execute("SELECT max(computed_at) AS at FROM outlet_daily_totals")
        row = cur.fetchone()
        return row["at"] if row else None


def stance_for_ids(
    conn: psycopg.Connection, ids: Iterable[int], target: str, model: str, prompt_version: str
) -> list[dict]:
    """stance_by_outlet, restricted to the matched articles."""
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
            WHERE s.article_id = ANY(%s)
              AND s.target = %s AND s.model = %s AND s.prompt_version = %s
            GROUP BY ai.outlet
            ORDER BY ai.outlet
            """,
            (list(ids), target, model, prompt_version),
        )
        return cur.fetchall()


def stance_targets(conn: psycopg.Connection, model: str, prompt_version: str) -> list[dict]:
    """Keywords that have stance rows at this model/prompt, most classified first.

    The UI offers these as suggestions: stance is the one metric that needs a
    model run per keyword, so a first-time viewer otherwise lands on a page
    whose Q1 column is all dashes and cannot tell whether that is the data or
    the system."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT target, count(*) AS n
            FROM article_stance
            WHERE model = %s AND prompt_version = %s
            GROUP BY target
            ORDER BY n DESC, target
            """,
            (model, prompt_version),
        )
        return cur.fetchall()


def cluster_roles(conn: psycopg.Connection, ids: Iterable[int]) -> list[dict]:
    """One row per matched article that has a body: its cluster membership and
    whether that cluster's order may be claimed. Feeds originality and the
    per-outlet `enriched` count."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, ai.outlet, a.dup_cluster_id, a.is_cluster_origin, c.origin_confident
            FROM articles a
            JOIN article_index ai ON ai.id = a.id
            LEFT JOIN dup_clusters c ON c.cluster_id = a.dup_cluster_id
            WHERE a.id = ANY(%s) AND a.body IS NOT NULL AND a.body <> ''
            ORDER BY ai.outlet, a.id
            """,
            (list(ids),),
        )
        return cur.fetchall()


def clusters_touching(conn: psycopg.Connection, ids: Iterable[int]) -> list[dict]:
    """Every stored cluster with at least one matched member -- all its members,
    matched or not, because the cluster is about the incident even when one
    copy's headline dropped the keyword. Same shape as clusters_for_framing,
    without bodies."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.cluster_id, c.origin_confident, c.shared_core_text,
                   ai.id, ai.outlet, ai.title, ai.effective_at, ai.published_at,
                   a.cluster_rank, a.delta_added, a.delta_removed, a.delta_summary
            FROM dup_clusters c
            JOIN articles a ON a.dup_cluster_id = c.cluster_id
            JOIN article_index ai ON ai.id = a.id
            WHERE c.cluster_id IN (
                SELECT dup_cluster_id FROM articles
                WHERE id = ANY(%s) AND dup_cluster_id IS NOT NULL
            )
            ORDER BY c.first_published_at, c.cluster_id, a.cluster_rank, ai.id
            """,
            (list(ids),),
        )
        rows = cur.fetchall()
    clusters: dict[int, dict] = {}
    for r in rows:
        c = clusters.setdefault(
            r["cluster_id"],
            {
                "cluster_id": r["cluster_id"],
                "origin_confident": r["origin_confident"],
                "shared_core_text": r["shared_core_text"],
                "members": [],
            },
        )
        c["members"].append(
            {
                k: v
                for k, v in r.items()
                if k not in ("cluster_id", "origin_confident", "shared_core_text")
            }
        )
    return list(clusters.values())


def upsert_social_posts(conn, posts, keyword):
    """First content wins; repeated permalinks update only the observation time."""
    from .nlp.segment import segment_text

    inserted = 0
    for post in posts:
        row = conn.execute(
            """
            INSERT INTO social_posts
                (platform, post_url, author, posted_at, text, text_seg, fetched_for, raw_path)
            VALUES ('threads', %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (post_url) DO UPDATE SET seen_at = clock_timestamp()
            RETURNING (xmax = 0) AS inserted
            """,
            (post["permalink"], post["username"], post["timestamp"], post.get("text", ""),
             segment_text(post.get("text", "")), keyword, post["raw_path"]),
        ).fetchone()
        inserted += int(row["inserted"])
    return inserted


# One definition of "this post is about this keyword", shared by the labeling
# tool, the classifier job and the Q4 lean. If they drifted apart the panel would
# divide a verdict count by a differently-built denominator -- the same class of
# silent error invariant 7 exists to prevent. Params: platform, since, until,
# segmented keyword, keyword.
_SOCIAL_MATCH_SQL = """
    p.platform = %s AND p.posted_at >= %s AND p.posted_at < %s
      AND (to_tsvector('simple', p.text_seg) @@ plainto_tsquery('simple', %s)
           OR p.fetched_for = %s)
"""


def _social_match_params(keyword: str, platform: str, since, until) -> tuple:
    from .nlp.segment import segment_text

    return (platform, since, until, segment_text(keyword), keyword)


def find_social_posts(conn, keyword, platform, since, until):
    """Find segmented text matches or the original API discovery keyword."""
    return conn.execute(
        f"""
        SELECT p.* FROM social_posts p
        WHERE {_SOCIAL_MATCH_SQL}
        ORDER BY p.posted_at DESC, p.id
        """,
        _social_match_params(keyword, platform, since, until),
    ).fetchall()


def get_post_stance(
    conn: psycopg.Connection, post_id: int, target: str, model: str, prompt_version: str
) -> dict | None:
    """The cached verdict for this exact (post, target, model, prompt), if any."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT label, confidence, evidence, created_at
            FROM social_post_stance
            WHERE post_id = %s AND target = %s AND model = %s AND prompt_version = %s
            """,
            (post_id, target, model, prompt_version),
        )
        return cur.fetchone()


def save_post_stance(
    conn: psycopg.Connection,
    *,
    post_id: int,
    target: str,
    model: str,
    prompt_version: str,
    label: str,
    confidence: float,
    evidence: str,
) -> None:
    """Upsert one post verdict. The key is the cache key, so a re-run overwrites itself."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO social_post_stance
                (post_id, target, model, prompt_version, label, confidence, evidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (post_id, target, model, prompt_version) DO UPDATE
                SET label = EXCLUDED.label,
                    confidence = EXCLUDED.confidence,
                    evidence = EXCLUDED.evidence,
                    created_at = now()
            """,
            (post_id, target, model, prompt_version, label, confidence, evidence),
        )


def post_stance_counts(
    conn: psycopg.Connection,
    keyword: str,
    platform: str,
    since,
    until,
    model: str,
    prompt_version: str,
) -> dict:
    """Raw neg/neu/pos counts for Q4. Suppression is metrics.lean's decision, not SQL's.

    LEFT JOIN on purpose: `posts` counts every matching post in the window --
    including the media-only ones the classifier skips -- so the panel's
    "classified / posts" caption cannot overstate how much of the platform was
    actually read, exactly as the outlet stance bar reports its own shortfall.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*) AS posts,
                   count(s.label) AS classified,
                   count(*) FILTER (WHERE s.label = 'neg') AS neg,
                   count(*) FILTER (WHERE s.label = 'neu') AS neu,
                   count(*) FILTER (WHERE s.label = 'pos') AS pos
            FROM social_posts p
            LEFT JOIN social_post_stance s
              ON s.post_id = p.id AND s.target = %s AND s.model = %s AND s.prompt_version = %s
            WHERE {_SOCIAL_MATCH_SQL}
            """,
            (keyword, model, prompt_version)
            + _social_match_params(keyword, platform, since, until),
        )
        return cur.fetchone() or {"posts": 0, "classified": 0, "neg": 0, "neu": 0, "pos": 0}


def crawl_runs_window(conn: psycopg.Connection, since: datetime) -> list[dict]:
    """Read window rows plus each active outlet's preceding successful poll.

    Context prevents the first row in the window being mistaken for a backfill.
    No metric exclusions are applied here; the pure caller receives `since` too.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH window_runs AS (
                SELECT outlet, started_at, items_seen, items_new, ok
                FROM crawl_runs WHERE started_at >= %(since)s
            ), previous AS (
                SELECT DISTINCT ON (r.outlet)
                       r.outlet, r.started_at, r.items_seen, r.items_new, r.ok
                FROM crawl_runs r
                WHERE r.started_at < %(since)s AND r.ok
                  AND r.outlet IN (SELECT outlet FROM window_runs)
                ORDER BY r.outlet, r.started_at DESC, r.run_id DESC
            )
            SELECT * FROM window_runs
            UNION ALL
            SELECT * FROM previous
            ORDER BY outlet, started_at
            """,
            {"since": since},
        )
        return cur.fetchall()

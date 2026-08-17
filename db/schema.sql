-- Parallax schema — Phase 1.
--
-- Creation order matters: outlets -> article_index -> dup_clusters -> articles,
-- because articles references dup_clusters and dup_clusters references article_index.

CREATE TABLE IF NOT EXISTS outlets (
    code     TEXT PRIMARY KEY,
    name_zh  TEXT NOT NULL,
    home_url TEXT NOT NULL,
    active   BOOLEAN NOT NULL DEFAULT TRUE
);


-- Tier 1: every article from every outlet, metadata only.
-- This table is the coverage-weight denominator. Its data is unrecoverable once
-- an item scrolls off the outlet's feed, which is why the crawl ships before the
-- metric that consumes it.
CREATE TABLE IF NOT EXISTS article_index (
    id            BIGSERIAL PRIMARY KEY,
    outlet        TEXT NOT NULL REFERENCES outlets (code),
    url_canonical TEXT NOT NULL,
    url_original  TEXT NOT NULL,
    title         TEXT NOT NULL,
    title_seg     TEXT,                       -- jieba tokens, space-joined, for FTS

    -- published_at is outlet-reported: sometimes absent, sometimes backdated,
    -- occasionally in the future. seen_at is ours and always trustworthy.
    published_at  TIMESTAMPTZ,
    seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The timestamp everything else orders and buckets by. Encoding the
    -- trust rule here means no query has to remember it.
    effective_at  TIMESTAMPTZ GENERATED ALWAYS AS
                  (LEAST(COALESCE(published_at, seen_at), seen_at)) STORED,

    body_fetched  BOOLEAN NOT NULL DEFAULT FALSE,

    -- Makes the 20-minute re-poll idempotent.
    UNIQUE (outlet, url_canonical)
);

CREATE INDEX IF NOT EXISTS article_index_outlet_time_idx
    ON article_index (outlet, effective_at);
CREATE INDEX IF NOT EXISTS article_index_time_idx
    ON article_index (effective_at);

-- Postgres ships no Chinese parser, so 'simple' over pre-segmented text is the
-- portable option. Query text must be segmented the same way before matching.
CREATE INDEX IF NOT EXISTS article_index_title_fts_idx
    ON article_index USING GIN (to_tsvector('simple', title_seg));


CREATE TABLE IF NOT EXISTS dup_clusters (
    cluster_id         BIGSERIAL PRIMARY KEY,
    member_count       INT NOT NULL DEFAULT 0,
    shared_core_text   TEXT,
    origin_article_id  BIGINT REFERENCES article_index (id),
    first_published_at TIMESTAMPTZ,

    -- False when the top two members are within the noise floor of feed
    -- timestamps. The UI must not claim who copied whom when this is false.
    origin_confident   BOOLEAN NOT NULL DEFAULT TRUE,
    computed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- Tier 2: only articles that matched a keyword someone actually searched.
-- Grows far more slowly than article_index; that gap is the cost model.
CREATE TABLE IF NOT EXISTS articles (
    id            BIGINT PRIMARY KEY REFERENCES article_index (id) ON DELETE CASCADE,
    body          TEXT,
    body_seg      TEXT,
    raw_html_path TEXT,                       -- gzipped original, so parser fixes never re-fetch

    enrich_state  TEXT NOT NULL DEFAULT 'pending'
                  CHECK (enrich_state IN ('pending', 'fetched', 'enriched', 'failed')),
    enrich_error  TEXT,

    -- 64-bit SimHash stored signed: values >= 2^63 are written as v - 2^64.
    -- Use parallax.nlp.dedup.to_signed / from_signed on every read and write.
    simhash       BIGINT,
    -- 16-bit bands for candidate blocking. INT, not SMALLINT: a band reaches
    -- 65535 and signed SMALLINT stops at 32767.
    band0 INT, band1 INT, band2 INT, band3 INT,

    dup_cluster_id    BIGINT REFERENCES dup_clusters (cluster_id),
    is_cluster_origin BOOLEAN NOT NULL DEFAULT FALSE,
    cluster_rank      INT,

    delta_added   TEXT[],                     -- sentences this member added to the shared core
    delta_removed TEXT[],                     -- shared-core sentences this member dropped
    delta_summary TEXT,                       -- one-line prose summary (LLM), cached

    stance_label  TEXT CHECK (stance_label IN ('neg', 'neu', 'pos')),
    stance_score  REAL,
    stance_model  TEXT,                       -- keeps eval runs comparable across models
    aspect_label  TEXT,                       -- Phase 2

    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS articles_band0_idx ON articles (band0);
CREATE INDEX IF NOT EXISTS articles_band1_idx ON articles (band1);
CREATE INDEX IF NOT EXISTS articles_band2_idx ON articles (band2);
CREATE INDEX IF NOT EXISTS articles_band3_idx ON articles (band3);
CREATE INDEX IF NOT EXISTS articles_cluster_idx ON articles (dup_cluster_id);
CREATE INDEX IF NOT EXISTS articles_state_idx ON articles (enrich_state);
CREATE INDEX IF NOT EXISTS articles_body_fts_idx
    ON articles USING GIN (to_tsvector('simple', body_seg));


-- Denominator for coverage weight. Day is Asia/Taipei, never UTC: bucketing by
-- UTC misassigns everything published after 08:00 local.
CREATE TABLE IF NOT EXISTS outlet_daily_totals (
    outlet         TEXT NOT NULL REFERENCES outlets (code),
    day            DATE NOT NULL,
    total_articles INT NOT NULL,

    -- True only when crawl_runs shows gap-free successful coverage of the whole
    -- Taipei day. The first crawl backfills whatever is still in each feed --
    -- 6 articles for a day when CNA actually published ~200 -- and dividing by
    -- that would report a coverage weight of several hundred percent. A minimum
    -- denominator does not catch it either: the same crawl produced 84 for the
    -- previous day, which clears any sane threshold and is still less than half
    -- the true total. Suppress coverage weight wherever this is false.
    complete       BOOLEAN NOT NULL DEFAULT FALSE,

    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (outlet, day)
);


-- Per-outlet, per-run crawl outcome. Without this there is no way to notice that
-- one adapter silently stopped returning items four days ago -- the highest
-- severity failure in the system, because tier-1 loss cannot be backfilled.
CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id      BIGSERIAL PRIMARY KEY,
    outlet      TEXT NOT NULL REFERENCES outlets (code),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    items_seen  INT NOT NULL DEFAULT 0,
    items_new   INT NOT NULL DEFAULT 0,
    ok          BOOLEAN NOT NULL DEFAULT FALSE,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS crawl_runs_outlet_time_idx
    ON crawl_runs (outlet, started_at DESC);


-- Phase 2 (PTT, Dcard). Created empty now so the schema is stable.
CREATE TABLE IF NOT EXISTS social_posts (
    id           BIGSERIAL PRIMARY KEY,
    platform     TEXT NOT NULL,
    board        TEXT,
    post_url     TEXT UNIQUE,
    posted_at    TIMESTAMPTZ,
    seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    text         TEXT,
    text_seg     TEXT,
    stance_label TEXT CHECK (stance_label IN ('neg', 'neu', 'pos')),
    stance_score REAL,
    aspect_label TEXT
);

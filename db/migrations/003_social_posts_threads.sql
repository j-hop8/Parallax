ALTER TABLE social_posts
    ADD COLUMN IF NOT EXISTS author TEXT,
    ADD COLUMN IF NOT EXISTS fetched_for TEXT,
    ADD COLUMN IF NOT EXISTS raw_path TEXT,
    ADD COLUMN IF NOT EXISTS stance_model TEXT,
    ADD COLUMN IF NOT EXISTS prompt_version TEXT;
CREATE INDEX IF NOT EXISTS social_posts_text_seg_fts_idx
    ON social_posts USING GIN (to_tsvector('simple', text_seg));
CREATE INDEX IF NOT EXISTS social_posts_platform_posted_idx
    ON social_posts (platform, posted_at DESC);
CREATE TABLE IF NOT EXISTS social_runs (
    run_id BIGSERIAL PRIMARY KEY,
    platform TEXT NOT NULL,
    keyword TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    queries INT NOT NULL DEFAULT 0,
    items_seen INT NOT NULL DEFAULT 0,
    items_new INT NOT NULL DEFAULT 0,
    ok BOOLEAN NOT NULL DEFAULT FALSE,
    error TEXT
);

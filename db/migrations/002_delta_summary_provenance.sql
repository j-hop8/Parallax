-- articles.delta_summary is a model output (T-009) and, like every stored
-- verdict since T-007, must say which model and which prompt produced it:
-- a summary at an older prompt version is redone, one at the current version
-- is never re-requested, and a reader can tell the two fixed strings
-- (原始稿源 / 與核心稿源相同, model 'rule') from the LLM's.

ALTER TABLE articles
    ADD COLUMN IF NOT EXISTS delta_summary_model   TEXT,
    ADD COLUMN IF NOT EXISTS delta_summary_version TEXT;

COMMENT ON COLUMN articles.delta_summary_model IS
    'Model that wrote delta_summary; ''rule'' for the two deterministic strings.';
COMMENT ON COLUMN articles.delta_summary_version IS
    'parallax.nlp.summary.SUMMARY_VERSION at write time.';

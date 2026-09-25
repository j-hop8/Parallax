# T-021 — Q4 eval: score post stance against the post gold set

**Owner:** codex (delegable — a near-mirror of `src/parallax/jobs/eval_stance.py`,
fully specified by it). **Blocked by:** nothing to *build*. A non-empty
`eval/post_stance_gold.csv` is needed to report a real number; the tests run on
fixtures and must not require one.

## Why

T-016 shipped the Q4 panel carrying an unconditional "model-labeled,
unvalidated" caveat, because nothing scores post stance. T-017 built the
labeling tool; the CSV is created by labeling and is deliberately not committed.
This ticket is the scoring job and nothing else — the post prompt, the lean
metric and the two renderers all stay in T-016.

## Design — decided, not open

**New module** `src/parallax/jobs/eval_posts.py`, mirroring `eval_stance.py`
closely enough that a reader of one can review the other. Reuse
`src/parallax/nlp/eval.py` unchanged (`confusion`, `per_class`, `macro_f1`,
`accuracy`) and the same `TARGET_MACRO_F1 = 0.75`.

**Gold:** `nlp.gold.load_post_gold` / `POST_GOLD_PATH`. **The join key is
`(post_url, target)`, never `post_id`** — T-017 decided this because a
re-fetch or a host move reassigns ids while the permalink is stable. A missing
file is not a crash: exit 1 with `no post gold yet; run: make label.posts
KEYWORD=<keyword>`.

**Predictions** come from `social_post_stance` (T-016). That table keys on
`post_id`, so the read joins through `social_posts`. Add **one** function to
`src/parallax/db.py`, beside `post_stance_counts`:

```python
def post_stance_for_urls(conn, urls, target, model, prompt_version) -> list[dict]
# SELECT p.post_url, p.platform, s.label  ... JOIN social_post_stance s ON s.post_id = p.id
# WHERE p.post_url = ANY(%s) AND s.target = %s AND s.model = %s AND s.prompt_version = %s
```

**Report** — same shape and the same rules as `render()` in `eval_stance.py`:
gold rows with no cached verdict are counted and listed, never silently
dropped; the annotator line prints the breakdown and appends the
model-authored-gold warning on the same prefixes (`claude`, `gemini`, `gpt`,
`model:`); macro-F1 against the target; per-class P/R/F1; confusion with rows =
human and cols = model; the `n < 50` caveat line. Two differences from the
article job: report **per_platform** instead of per_outlet, and add one line

```
human rows: <n> of 100 required before the Q4 caveat can be retired
```

counting gold rows whose annotator does *not* start with a model prefix.

**`--classify`** fills missing verdicts with `nlp.stance.GeminiPostStance`,
mirroring `_fill_missing`: one commit per row, isolate per-row failures, stop
cleanly on `DailyQuotaExhausted` and score whatever is cached. **Without
`--classify` the job must make zero API calls** — assert this in a test with a
classifier that raises if called.

Write the JSON report to `eval/runs/` exactly as `stance.eval` does. Add a
`posts.eval` target to the `Makefile` next to `stance.eval`, and list it in
`help` and `.PHONY`.

## Do not touch

- `src/parallax/nlp/stance.py` — the `post-v1` prompt text is frozen; changing
  it means bumping `POST_PROMPT_VERSION`, which is not this ticket.
- `src/parallax/ui/render.py` and `src/parallax/jobs/report.py` — **retiring the
  caveat is deliberately out of scope.** It needs ≥100 human rows *and* a human
  decision about the F1; this ticket only reports how far off that is.
- `src/parallax/metrics/lean.py`, `db/schema.sql`, `src/parallax/crawl/**`,
  `src/parallax/social/**`, `scripts/label_posts.py`.

## Acceptance criteria

- `make posts.eval` over a fixture gold file and cached verdicts prints
  macro-F1, per-class, confusion and per-platform.
- Gold rows with no cached verdict are counted and named in the output.
- The annotator line flags model-authored gold on the same prefixes as
  `stance.eval`.
- The human-row count against the 100-row threshold is printed.
- Without `--classify` the job makes zero API calls (tested with a classifier
  that raises on use).
- A missing `eval/post_stance_gold.csv` exits 1 with the `make label.posts`
  hint, and does not traceback.
- The job joins on `post_url`; a test proves a changed `post_id` does not break
  the join.

- Ticket file moved to `.agents/tickets/done/`.

## Verify

```bash
uv run pytest -q tests/test_post_eval.py && uv run ruff check . && uv run pytest -q
```

## Implementation handoff

Implemented URL/target-based post evaluation, cached-only default, optional
classification with per-row commits and quota handling, JSON/text reports, and
`make posts.eval`. Added fixture-based tests plus a Postgres integration test
covering changed ids and target/model/prompt cache filters.

Verify was attempted but could not execute: `uv` is not installed (exit 127).
This machine also has no Docker or Postgres. No toolchain was installed; CI must
run the full Verify command with Postgres. Python syntax parsing,
`git diff --check`, and `make -n posts.eval` passed locally.

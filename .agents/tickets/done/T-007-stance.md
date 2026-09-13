# T-007 — Target-dependent stance (Q1), validated on a hand-labeled gold set

## Why

Q1 is "what is each outlet's stance toward the incident?" Nothing exists for
it: `nlp/` holds only the segmenter, `eval/` is empty, `articles.stance_*` is
never written. The proposal calls stance the project's largest technical risk —
news is written in neutral register, so document sentiment comes out flat —
and gates everything downstream on **Macro-F1 > 0.75 on 300 hand-labeled
articles**. The mitigation is target-dependent stance: toward the actor or
incident the user searched for, headline and lede weighted over body.

## Decisions (with the user, 2026-09-13)

- **Gemini API, free tier, first; local BERT later (T-007b).** No training
  data exists, the host is a 4-core Intel / 8 GB, and the milestone is
  validating the *approach*. The classifier is a protocol; backends slot in.
- **Free quota only.** Limits are per-account and unpublished; pace
  conservatively, back off on 429, cache every result forever.
- **`GEMINI_API_KEY` in `.env`**; `settings.py` loads it (nothing did before).
- **First incident: 沈伯洋** — 8/8 outlets, contested political actor.

## Goal

`make stance KEYWORD=沈伯洋` classifies every enriched article for the keyword
once, stores it, and prints the per-outlet label distribution. `make label
KEYWORD=沈伯洋` lets a human label the same articles blind. `make stance.eval`
scores the model against the humans and prints Macro-F1 next to the 0.75
target.

## Files in scope

- `db/schema.sql` — `article_stance` (additive)
- `src/parallax/db.py` — `find_enriched_articles`, `get_stance`, `save_stance`,
  `stance_by_outlet`
- `src/parallax/settings.py` — `.env` loader, `STANCE_MODEL`, `STANCE_RPM`
- `src/parallax/nlp/stance.py`, `src/parallax/nlp/eval.py`
- `src/parallax/jobs/stance.py`, `src/parallax/jobs/eval_stance.py`
- `scripts/label_stance.py`, `eval/README.md`, `eval/stance_gold.csv`
- `pyproject.toml` (`llm` extra), `Makefile`, `.env.example`
- `tests/test_stance.py`, `tests/test_stance_eval.py`, `tests/test_label_stance.py`
- this ticket (archived to `done/` at ship)

## Do not touch

`src/parallax/metrics/**` (T-010), `nlp/{dedup,framing}` (T-008/009),
`src/parallax/ui/**`, the tier-1 crawl path, `articles.stance_*` columns.

## Acceptance criteria

- Stance is stored per `(article, target, model, prompt_version)`; re-running
  `stance` for a classified keyword makes **zero** API calls.
- Every stored label carries an `evidence` phrase and a confidence.
- The labeling tool never shows a model prediction, shuffles across outlets,
  and resumes without relabeling.
- `eval_stance` never spends quota unless `--classify` is passed; it reports
  Macro-F1, per-class P/R/F1, confusion matrix, per-outlet accuracy, and `n`.
- Human and model are scored against one written definition
  (`eval/README.md` and the prompt say the same thing).
- All tests run offline against a fake client; the crawler still installs
  without the `llm` extra.
- Live: 沈伯洋 shows a distribution across all 8 outlets.

## Sequencing note (found during live verification)

Of the 200 enriched 沈伯洋 articles, 68 (53 ftv, 15 udn) still carry the
T-003b lede-as-title text -- they were indexed before that fix and the upsert is
first-sight-wins. The headline is the classifier's most-weighted input and the
cache key does not include it, so the **live stance run must follow T-003c**
(recover the headline from the cached article HTML; all 200 pages are in
`raw/`). Everything else in this ticket is independent of that.

## Outcome (2026-09-13)

- Gold set: 184 沈伯洋 articles labeled by `claude-opus-5` at the project
  owner's decision (blind, notes on borderline calls). The eval report prints
  this next to the F1; it is inter-model agreement, not human validation.
- `gemini-3.8-flash` free tier is 20 requests/day -- unusable. Default moved to
  `gemini-3.5-flash-lite`: 184/184 classified, 0 failed, 98% evidence verbatim.
- **Macro-F1 0.733** (target 0.75), accuracy 0.72. neg 0.85 / pos 0.75 /
  **neu 0.60**. Polarity flips: 3 of 184. 43 of 52 disagreements are neu<->pos,
  split both ways -- the two models place the "favorable framing" line
  differently. That is the prompt-v2 / annotation-guide item, not a model swap.
- Per-outlet lean is the same ordering under both models (chinatimes -0.71,
  ftv +0.76, cna/ettoday/udn/tvbs near zero, ltn/setn strongly positive).

## Knowingly not done

- BERT distillation → T-007b once the approach clears 0.75.
- Reaching 300 gold labels — ~10 articles/day for one incident; the harness
  reports at any `n`. Add 萬安 (neutral contrast) and 關稅 as labeling time allows.
- Stance inside `enrich` — kept a separate, explicit step so quota is a decision.
- Aspect taxonomy, social posts, UI.

## Verify

```bash
uv sync --extra llm && uv run pytest -q && uv run ruff check .
make db.migrate
make enrich KEYWORD=沈伯洋
make stance KEYWORD=沈伯洋 ARGS=--dry-run   # N calls planned, 0 made
make stance KEYWORD=沈伯洋                  # then again: 0 calls
make label KEYWORD=沈伯洋
make stance.eval
```

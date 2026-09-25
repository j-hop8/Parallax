# T-020 — Human-validated stance gold: annotator-aware labeling + Cohen's κ

**Owner:** claude — what counts as "validated" is a judgment call, and it
governs how every Q1 and Q4 number in the product may be quoted.
**Blocked by:** nothing.

## Why

Proposal §9 says the project is judged on measured accuracy. Every gold set in
this repo is machine-labeled:

| set | rows | target | annotators |
|---|---|---|---|
| `eval/stance_gold.csv` | 184 | 300 | `claude-opus-5` ×184 |
| `eval/dup_gold.csv` | 118 | 200 | `claude-opus-5` ×118 |
| `eval/post_stance_gold.csv` | — | ≥100 human | file does not exist |

`eval/README.md` already says the resulting F1 "measures agreement between two
models, not human validation", and §8 calls the stance approach the single
largest technical risk, to be settled on hand-labeled data "before the rest is
built on top of it". T-023 (aspect taxonomy) would stack a second model layer
on that unvalidated base.

The expensive fix is hand-labeling 300 articles. The cheap one is finding out
whether the machine labels can stand in: have a human blind-label a stratified
**overlap** with rows Claude already labeled, then report human-vs-Claude
agreement. If κ clears the bar, the 184 existing rows become a defensible proxy
and §9 row 2 is reachable in an evening. If it does not, that is worth knowing
before T-023 rather than after.

Two things in the code block this today:

- `src/parallax/nlp/eval.py` has confusion / per_class / macro_f1 / accuracy and
  **no agreement statistic at all**.
- `src/parallax/nlp/gold.py:94` — `pending()` keys on `(article_id, target)`
  regardless of annotator, so a second annotator can never be offered a row the
  first has already labeled. The tooling actively prevents the overlap.

## Design — decided, not open

1. **κ is Cohen's κ, pure Python**, beside `macro_f1` in `nlp/eval.py` (no
   scikit-learn: it lives in the `torch` extra and the eval must run on the
   crawler's install). `cohens_kappa(a, b, labels=LABELS) -> float`, symmetric,
   `(po - pe) / (1 - pe)` with `pe` from the two marginals. Length mismatch
   raises, like `confusion()`. **Degenerate case decided:** when `pe == 1`
   (both annotators used exactly one label, and the same one) return `1.0`
   rather than dividing by zero — perfect agreement on a single class is
   agreement, even though κ cannot distinguish it from chance.

2. **`pending()` becomes annotator-aware.** New keyword `annotator: str | None
   = None`. `None` keeps today's behaviour exactly — skip every labeled row —
   so all existing callers are untouched. When set, skip only rows already
   labeled *by that annotator*, so a new name is offered the rows Claude did.
   Blindness is unchanged: `label_session` never shows an existing label.

3. **Duplicate keys must never be silently double-counted.** Once two
   annotators label the same row the CSV holds two rows for one
   `(article_id, target)`, and `evaluate()` in `jobs/eval_stance.py` joins gold
   to predictions on exactly that key — it would score the same prediction
   twice. Decided: `stance.eval` grows `--annotator NAME` to restrict the gold
   set, and **with no filter and duplicate keys present it exits non-zero
   naming the annotators**. Loud, not clever.

4. **Stratify the validation sample proportionally to the gold label
   distribution**, not evenly across the three classes. κ is prevalence
   sensitive: an evenly-sampled overlap would estimate κ for a corpus that does
   not exist. Proportional sampling estimates the same quantity the full set
   would. Say this in `eval/README.md` so the next person does not "fix" it.

5. **Bar: κ ≥ 0.6** (substantial agreement) — §9 sets that for aspect labeling
   and there is no reason stance should be looser. **Reported, never gated**,
   exactly like `macro_f1`. Reuse the existing `n < 50` caveat line: below 50
   overlapping rows the report says the number is a direction, not a verdict.
   60–80 rows is the intended session size.

## Files in scope

- `src/parallax/nlp/eval.py` — `cohens_kappa`, and a pure helper that aligns two
  annotators' rows on their shared keys
- `src/parallax/nlp/gold.py` — `pending()` annotator awareness
- `scripts/label_stance.py` — `--validate`: offer only rows already labeled by
  someone else, stratified per decision 4
- `src/parallax/jobs/eval_stance.py` — `--annotator` filter, the duplicate-key
  guard, and an `--agreement` report (pairwise κ, % agreement, confusion for
  every annotator pair with a non-empty overlap)
- `Makefile` — `make label.validate`, `make stance.agreement`
- `eval/README.md` — the procedure and the reason for proportional stratification
- tests for the κ arithmetic, the pending() split, the blindness of `--validate`,
  and the duplicate-key guard

## Do not touch

`eval/dup_gold.csv` and `scripts/label_pairs.py` — validating the dedup gold
reuses this κ and is its own ticket. Also: the stance prompts (`v1`,
`post-v1`), `db/**`, `src/parallax/crawl/**`, `src/parallax/social/**`,
`config/outlets.yaml`.

## Acceptance criteria

- `cohens_kappa` reproduces a worked example computed by hand in the test, and
  returns `1.0` for the `pe == 1` case rather than raising.
- `pending(..., annotator="jimmy")` offers rows `claude-opus-5` already
  labeled; `pending(...)` with no annotator behaves exactly as before.
- `label_stance.py --validate` never prints an existing label — tested over a
  row that carries one.
- `stance.eval` with duplicate `(article_id, target)` across annotators and no
  `--annotator` exits non-zero and names the annotators.
- `make stance.agreement` prints κ, % agreement and the confusion matrix for
  each annotator pair, and says how many rows the overlap holds.
- `eval/README.md` documents the procedure and why the sample is proportional.

## Verify

```bash
uv run pytest -q tests/test_stance_eval.py tests/test_label_stance.py && uv run ruff check . && uv run pytest -q
```

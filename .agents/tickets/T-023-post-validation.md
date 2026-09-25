# T-023 — Post stance validation: mirror T-020 for Threads posts

**Owner:** codex (delegable — T-020 is the worked example line for line, and the
κ arithmetic is already written and tested; this ticket is the posts-side
plumbing around it). **Blocked by:** #26 (T-020) and #25 (T-021) both merged to
`main`. Neither is optional: this ticket calls `nlp.eval.cohens_kappa` /
`agreement` from the first and edits `jobs/eval_posts.py` from the second.

## Why

Found while reviewing #25 (lead review comment on that PR):

`src/parallax/jobs/eval_posts.py` `evaluate()` joins gold rows to predictions on
`(post_url, target)`. The moment the post gold set carries **two annotators on
one row**, the same model verdict is scored once per annotator and the macro-F1
silently weights those rows double. Nobody would see it — the number just moves.

That is exactly the failure T-020 guards against on the article side with
`duplicate_keys()` plus a refusal to score without `--annotator`. `eval_posts`
has no equivalent, because T-020 and T-021 were written in parallel and neither
ticket knew about the other. It is a seam between two tickets, not a defect in
either.

It cannot bite today: `eval/post_stance_gold.csv` does not exist and there is no
post `--validate` mode to create a second opinion. But T-020 exists precisely to
make second opinions routine, and T-016's Q4 panel keeps its "model-labeled,
unvalidated" caveat until the post gold has **≥ 100 human rows** — which is a
standing invitation to put a human annotator alongside the machine one. The
guard should land before the thing it guards against, not after.

## Design — decided, not open

**1. Reuse the κ arithmetic; do not copy it.** `nlp.eval.cohens_kappa` and
`nlp.eval.agreement` are already generic: `agreement` takes
`Mapping[Hashable, str]`, so `(post_url, target)` keys work unchanged. Import
them. A second copy of a prevalence-sensitive statistic is how the two sides
drift apart. **`src/parallax/nlp/eval.py` is in the do-not-touch list for this
reason** — if you believe it needs a change, stop and say so instead.

**2. Identity is `(post_url, target)`, never `post_id`** — T-017's rule, for the
same reason: a re-fetch or a host move reassigns ids while the permalink is the
table's UNIQUE key.

**3. `pending_posts` gains `annotator: str | None = None`,** with semantics
identical to `pending` in T-020: `None` keeps today's behaviour exactly (any
label retires a row, so every existing caller is unaffected); a name means only
that person's labels retire a row, so a second annotator is offered what the
first already did.

**4. Stratify proportionally to the existing label distribution,** not evenly
across the three classes — κ is prevalence-sensitive, so an evenly-sampled
overlap estimates κ for a corpus that does not exist. T-020's
`validation_sample` already implements this with largest-remainder rounding.
**Factor the shared logic into one private helper in `nlp/gold.py`** (something
like `_stratified_sample(pool_by_label, n, rng)`) used by both
`validation_sample` and the new `validation_sample_posts`. Do not change
`validation_sample`'s public signature — T-020's tests pin it.

**5. The duplicate guard is T-020's, verbatim in behaviour:** `eval_posts` grows
`--annotator NAME` to restrict the gold set, and with no filter and duplicate
`(post_url, target)` keys present it **exits 2 naming the annotators**. Loud,
not clever. Media-only posts stay excluded as they are today.

**6. `--agreement` must touch nothing.** It compares annotators, not a model, so
it must not open a database connection and must not construct a classifier —
assert both in a test, the way T-020's
`test_agreement_report_needs_no_database_and_no_quota` does.

**7. Bar is κ ≥ 0.60**, reported and never gated, reusing `KAPPA_TARGET`.
Keep the `n < 50` "direction, not a verdict" line.

## Files in scope

- `src/parallax/nlp/gold.py` — `pending_posts(..., annotator=...)`,
  `validation_sample_posts(...)`, and the shared private stratifier
- `scripts/label_posts.py` — `--validate`, blind exactly as the normal session is
- `src/parallax/jobs/eval_posts.py` — `duplicate_keys`, `--annotator`,
  `--agreement`
- `Makefile` — `label.posts.validate`, `posts.agreement`; add both to `help` and
  `.PHONY`
- `eval/README.md` — the post-gold section gets the same procedure and the same
  note on why the sample is proportional
- tests

## Do not touch

`src/parallax/nlp/eval.py` (see decision 1), `src/parallax/jobs/eval_stance.py`
and `scripts/label_stance.py` (the article side is T-020's and already shipped),
the `post-v1` prompt text in `src/parallax/nlp/stance.py`, `db/**`,
`src/parallax/metrics/**`, `src/parallax/ui/**`, `src/parallax/crawl/**`,
`src/parallax/social/**`, `config/outlets.yaml`.

**Do not retire the Q4 caveat.** `POST_STANCE_NOTE` in `ui/render.py` and
`POST_STANCE_CAVEAT` in `jobs/report.py` stay exactly as they are. Retiring them
needs ≥ 100 human rows *and* a human judgement about the F1; this ticket only
makes collecting those rows possible.

## Acceptance criteria

- `pending_posts(..., annotator="jimmy")` offers rows a model annotator already
  labeled; `pending_posts(...)` with no annotator behaves exactly as before.
- `validation_sample_posts` offers only rows another annotator labeled and this
  one has not, and a 4:2:1 pool yields a 4:2:1 sample (pin it, as T-020 does).
- Both samplers go through one shared stratifier — a reviewer can see there is
  only one implementation of the rounding.
- `label_posts.py --validate` never prints an existing label. Test it over a row
  carrying `stance_label`, `stance_model`, `prompt_version` and `evidence` under
  sentinel values, and exclude the session's own running tally from the
  assertion — the tally names all three labels by design. (T-020 got this wrong
  first time round; the fix is in `tests/test_label_stance.py`.)
- `posts.eval` with duplicate `(post_url, target)` across annotators and no
  `--annotator` exits 2 and names them; with `--annotator` it scores that
  annotator's rows only.
- `make posts.agreement` prints κ, % agreement, the overlap size and the
  confusion matrix per annotator pair, opening no database connection.
- `eval/README.md` documents the procedure and the proportional-sampling reason.
- Ticket file moved to `.agents/tickets/done/`.

## Verify

```bash
uv run pytest -q tests/test_post_eval.py tests/test_label_posts.py && uv run ruff check . && uv run pytest -q
```

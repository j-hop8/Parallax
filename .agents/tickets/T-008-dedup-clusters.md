# T-008 — Near-duplicate clusters and propagation order (Q3)

## Why

Q3 is the differentiator: which outlets ran the same copy, who published first,
and (T-009) what each changed. The schema has carried the slots since T-001 —
`articles.simhash/band0..3/dup_cluster_id/is_cluster_origin/cluster_rank`,
`dup_clusters.origin_confident` — but nothing writes them. Invariant 5 (never
claim who copied whom when `origin_confident` is false) and T-005's rule (a
cluster with an unrecovered timestamp is not confident) are the constraints.
Target (proposal §9): precision > 0.90, recall > 0.80 on 200 labeled pairs.

## What the corpus said first (read-only prototype, 2026-09-13, 208 bodies)

- **SimHash Hamming distance is a prefilter, not the verdict.** The known
  cna→ltn wire copy sits at Hamming 9; other true copies at 6–10; unrelated
  pairs appear at 15–16. A textbook `≤3` — and the 4×16-bit band blocking the
  schema anticipates, which guarantees only `≤3` — would miss the canonical
  case. Token-bigram **containment** separates them: true copies 0.90–1.00
  (Jaccard ≥ 0.79), a gap, then a 0.6–0.8 tail of "same press release quoted
  at length" (Jaccard 0.2–0.5), then noise ≤ 0.3.
- **Per-outlet boilerplate must go first.** tvbs bodies carry appended
  related-article rails (1,213 recurring bigrams); ltn/udn/chinatimes carry
  app promos, member-centre text, comment policy. Without removal tvbs↔tvbs
  pairs score 0.68–0.83. Dropping bigrams present in ≥ max(3, 20%) of an
  outlet's enriched articles removes all of them.
- Two artefacts: udn serves one article under two section paths (canonicalization
  miss); two ltn `自由說新聞》` video pages have no body (promo text only). Dedup
  handles both (exact same-outlet duplicate; `too_short`); root causes are
  follow-ups.

## Design

- Features: jieba token bigrams from `articles.body_seg`; per-outlet
  boilerplate removed; SimHash-64 (pure Python) stored signed via
  `to_signed/from_signed` with the four 16-bit bands; `< 30` bigrams → `too_short`.
- Pairs: all pairs in the enriched corpus, Hamming ≤ 12 prefilter, verdict
  **duplicate iff containment ≥ 0.85 and Jaccard ≥ 0.5** (defaults; the
  gold-pair eval tunes them). 0.6 ≤ containment < 0.85 is reported as "shares
  source text", stored nowhere.
- Clusters: union-find; `cluster_id = min(member article_id)` so ids survive
  full rebuilds; rank by `effective_at`; origin = rank 1.
- `origin_confident` false if: rank-1/rank-2 gap < 5 min; any member has
  `published_at IS NULL`; the top two are the same outlet.
- `shared_core_text` stays NULL — T-009.
- Readout: clusters in time order with `origin` or `order indeterminate (reason)`,
  the shares-source tier count, `too_short`, and a per-outlet originality table
  (original = singleton or confident origin; strict "not in any cluster" beside it).

## Files in scope

- `src/parallax/nlp/dedup.py`, `src/parallax/nlp/gold.py` (pair rows)
- `src/parallax/db.py` (fingerprint / cluster / readout helpers)
- `src/parallax/jobs/dedup.py`, `src/parallax/jobs/eval_dedup.py`
- `scripts/label_pairs.py`, `eval/dup_gold.csv`, `eval/README.md` (appendix)
- `Makefile`; `tests/test_dedup.py`, `tests/test_dedup_jobs.py`
- this ticket (archived to `done/` at ship)

## Do not touch

`nlp/stance.py`, `nlp/framing` (T-009), `metrics/**` (T-010), `urls.py`,
`crawl/**`, `db/schema.sql` (all needed columns exist).

## Acceptance criteria

- cna→ltn 哥倫比亞 is one cluster, origin cna, `origin_confident` true.
- The udn section-path pair is one cluster, `origin_confident` false (same outlet).
- tvbs articles do not cluster with each other on boilerplate.
- `origin_confident` is false whenever a member lacks `published_at`.
- `make dedup` twice: identical output, second run changes nothing.
- P > 0.90 and R > 0.80 on the gold pairs at the chosen thresholds, annotator
  printed beside the numbers (T-007 provenance rule applies).
- No `metrics/` code; no schema change.

## Knowingly not done

- udn canonicalization by article id regardless of section path (`urls.py`).
- ltn video pages: `extract_body` should return empty, not promo text.
- tvbs related-article rail in `extract_body` (dedup masks it; stance/T-009 read raw body).
- `shared_core_text`, deltas, LLM diff summary — T-009.

## Verify

```bash
uv run pytest -q && uv run ruff check .
make dedup && make dedup                 # second run: 0 changes
make dedup ARGS="--keyword 沈伯洋"
make label.pairs ARGS="--n 200"
make dedup.eval
```

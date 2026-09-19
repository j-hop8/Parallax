# T-009 — Framing delta within a cluster: shared core, what each member added or dropped (Q3)

## Why

T-008 finds the clusters and the order. The second half of Q3 is *what each
outlet changed*: the proposal's "framing delta" (text present in one member
but absent from the shared core) and the one-line per-member summary the
design shows under 群組 ("＋ 加入「鄰居受訪」段落 … － 刪除警方尚未證實的但書句").
The schema has carried the slots since T-001 -- `dup_clusters.shared_core_text`,
`articles.delta_added/delta_removed/delta_summary` -- and nothing writes them.

Invariant 5 shapes the data, not just the UI: when `origin_confident` is
false the stored deltas must not encode a direction either.

## What the corpus said first (read-only prototype, 2026-09-19, 14 clusters)

- **Sentence-level set diff, after the same per-outlet boilerplate rule as
  T-008, reads like the design.** cna→ltn: ltn added a caption; cna→setn:
  setn added four `●` sub-heads; ettoday→setn: setn dropped two `▲` photo
  captions; ltn→chinatimes (a rewrite, not a wire copy): chinatimes added the
  青年局 rebuttal. Datelines and credits (`（中央社芝加哥22日綜合外電報導）`,
  `（編譯：陳昱婷）1150823`, `（路透）`) are bracketed at sentence edges and
  strip cleanly; without that the lede of every CNA copy is a false delta.
- **Rewordings score `SequenceMatcher` ratio 0.74–0.89, distinct sentences
  ≤ 0.22.** A 0.9 "same sentence" bar keeps rewordings visible as a −/＋ pair;
  the readout pairs them as `～` when the ratio is ≥ 0.6, so a reworded lede
  is shown as an edit, not an addition plus a deletion.
- **The tvbs and udn related-story rails are the blocker.** Every tvbs copy
  showed 30–40 "added" sentences that are other articles' headlines and
  ledes; udn's `【全球熱話題】` box showed as four. They are per-article (T-008
  measured this: the per-outlet boilerplate rule cannot catch them) and they
  are link text in the DOM -- tvbs wraps each rail item's `<p>` in an `<a>`,
  udn fills one `<p>` with `<a>`s. A paragraph that is link text is
  navigation, not prose: dropping `<p>` inside `<a>` and `<p>` whose text is
  ≥ 80% anchor text removes 68% of tvbs "body" (the rails), the udn box, and
  a handful of `更多鏡週刊報導…` link lines at ltn/ettoday/setn, and changes
  nothing at cna/ftv/chinatimes. That is the root-cause fix T-008 named, and
  the framing delta is the first consumer that cannot work without it, so it
  is in scope here.

## Design

- `crawl/extract.py`: link-paragraph rule above. `jobs/reextract.py`
  (`make reextract`) re-runs `extract_body` over every enriched article from
  the raw HTML cache -- no fetch -- so a parser fix reaches the corpus.
- `nlp/framing.py`, pure: split bodies into sentences (`。！？`, closing
  quotes attached, paragraph breaks respected); normalise (whitespace, edge
  brackets, punctuation width); drop credits (fewer than 4 Han characters after
  stripping, `記者X／台北報導` bylines); drop per-outlet boilerplate sentences
  (≥ max(3, 20%) of the outlet's enriched articles, as in dedup); match
  sentences across members by normalised equality or ratio ≥ 0.9.
- **Core** = sentences in at least max(2, ⌈n/2⌉) members, in the origin's
  order → `shared_core_text`.
- **Reference for deltas**: the origin when `origin_confident`, else the core.
  Confident: origin has no deltas; each follower's `added` = its sentences
  not in the origin, `removed` = origin sentences it lacks. Indeterminate:
  every member's `added` = its sentences not in the core, `removed` = `[]`
  always -- "only in this version", no direction claimed.
- `jobs/framing.py` (`make framing`): compute over stored clusters, persist,
  print the design's block per cluster (core excerpt, then per member
  `原始稿源` / `＋ − ～` lines). `--keyword` narrows the readout.
  `db.replace_clusters` clears deltas and summary on any member whose
  cluster assignment changed, and on every member of a cluster whose origin
  or `origin_confident` changed (the reference the deltas were computed
  against), so `make dedup` cannot leave stale or directional deltas behind.
- `delta_summary`: LLM, one line of Traditional Chinese per follower with a
  non-empty delta, from the headline pair and the ＋/− lists -- never the
  outlet name (same reason as stance). Only on `make framing ARGS=--summarize`;
  `--dry-run` counts first. Cached forever on the row; `save_framing` NULLs
  the summary when the deltas change; provenance in two new columns
  (`delta_summary_model`, `delta_summary_version`, migration 002). Members
  with empty deltas get the deterministic `與核心稿源相同。`, origins
  `原始稿源。`, with no call.
- Gemini plumbing (pacer, daily-quota stop, retry) moves from `nlp/stance.py`
  to `nlp/gemini.py`; stance keeps its interface and re-exports.

## Files in scope

- `src/parallax/crawl/extract.py`, `src/parallax/jobs/reextract.py`
- `src/parallax/nlp/framing.py`, `src/parallax/nlp/summary.py` (the prompt,
  its version and the Gemini backend for `delta_summary` -- kept out of the
  pure module; added to this list in review round 1, it was implied by the
  Design section but not written here), `src/parallax/nlp/gemini.py`,
  `src/parallax/nlp/stance.py` (extract the shared client only)
- `src/parallax/jobs/framing.py`, `src/parallax/db.py`
- `db/schema.sql`, `db/migrations/002_delta_summary_provenance.sql`
- `src/parallax/settings.py` (`FRAMING_MODEL`), `.env.example`, `Makefile`
- `tests/test_framing.py`, `tests/test_framing_jobs.py`, `tests/test_framing_db.py`
  (DB-gated, rolled back), `tests/test_extract.py`, `tests/test_reextract.py`
- this ticket (archived to `done/` at ship)

## Do not touch

`nlp/dedup.py` thresholds, `metrics/**` (T-010), `urls.py`, `crawl/adapters/**`,
`crawl/listing.py`, the tier-1 crawl path, `article_stance`.

## Acceptance criteria

- tvbs fixture body no longer contains the D23 / Westbrook / 南投 rail
  paragraphs; a `<p>` full of `<a>` is dropped; a paragraph with one inline
  link is kept. cna/ftv/chinatimes bodies are byte-identical before and after.
- `make reextract` makes zero HTTP requests and reports per-outlet changed counts.
- cna→setn 4大重點: setn `added` is exactly the four `●` sub-heads; tvbs
  `added` contains no other article's headline.
- cna→ltn 哥倫比亞: ltn `added` = the caption sentence; `removed` = `[]`;
  the credit line appears in neither.
- udn/udn (indeterminate): both members have `removed = []`; the readout
  prints "only in this version", never "removed".
- `make framing` twice: second run changes nothing; a member whose deltas
  change has its `delta_summary` cleared.
- `make framing ARGS=--summarize` twice: second run makes zero API calls.
  Every summary row carries model and version.
- `make dedup.eval` still passes P > 0.90, R > 0.80 on the re-extracted corpus.
- `uv run pytest -q` passes offline; the crawler still installs without `llm`.

## Live (2026-09-19, 478 enriched articles)

- Re-extraction: tvbs bodies 122k → 39k chars (rails), udn box gone, cna /
  ftv / chinatimes unchanged. Note: the corpus was re-extracted by the first
  run of `tests/test_reextract.py` before its fixture stopped the job's
  per-article commits from reaching the disk -- same code path and result as
  `make reextract`, which now reports 0 changes; the fixture is fixed and the
  test rows were removed by hand.
- `make dedup` on clean bodies: still 14 clusters, 35 members (was 33); tvbs
  now joins the 沈伯洋 青年政策 press-release cluster. `make dedup.eval`:
  **P 0.96, R 1.00** -- the one false positive is that setn/tvbs pair, gold
  says "same press release; tvbs adds own reporting", containment 0.89 now
  that the rail no longer dilutes it. Thresholds are T-008's and untouched.
- `make framing`: 13 members with a delta, 22 on a rule string. Second run:
  0 changes. Readout matches the design: cna→setn = four `●` sub-heads;
  cna→ltn = one caption, nothing removed; ettoday→setn = two `▲` captions
  dropped; ltn→chinatimes = 青年局 rebuttal added; udn/udn indeterminate =
  both 與核心稿源相同. Remaining noise: one udn `※ 提醒您：禁止酒駕` line --
  present in 2 of 65 udn bodies, under the boilerplate floor of 3.
- `make framing ARGS=--summarize`: 13 calls, 0 failed, second pass 0 calls.
  Prompt v2 lists caption-like additions separately after v1 called every
  tvbs photo caption "background"; v2 says `＋ 圖說`. Summaries read like the
  design ("～ 標題改為質疑財源；＋ 賴總統普發1萬元之相關報導；～ 改寫青年局回應…").

## Knowingly not done

- No gold set for the deltas: the proposal budgets human labels for dedup
  and stance, not for framing. The readout is the check for now.
- `caption_like` is substring containment; a caption that also drops one word
  mid-sentence (one tvbs case) is still listed as an addition.
- The summary prompt still names a rewording as "改寫" without saying what
  moved -- the ～ pairs are in the prompt, the model is not always specific.
- Sub-sentence deltas (a clause cut from an otherwise identical sentence) are
  invisible at the 0.9 same-sentence bar.

## Verify

```bash
uv run pytest -q && uv run ruff check .
make db.migrate
make reextract                       # 0 fetches
make dedup && make dedup.eval
make framing && make framing         # second run: 0 changes
make framing ARGS="--keyword 關稅"
make framing ARGS="--summarize --dry-run"
make framing ARGS=--summarize        # then again: 0 calls
```

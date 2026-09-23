# Stance gold set — annotation guide

`stance_gold.csv` is the hand-labeled set the T-007 classifier is judged
against (target: **Macro-F1 > 0.75**). It is committed because it is hand-made
and irreplaceable. `runs/` holds eval outputs and is gitignored.

These definitions are **the same ones given to the model** (see
`SYSTEM_INSTRUCTION` in `src/parallax/nlp/stance.py`). If you change one,
change the other and bump `PROMPT_VERSION`. Human and model must be scored
against one definition of the task, or F1 measures disagreement about what
"stance" means instead of how good the classifier is.

## Provenance — read this before quoting an F1

The `annotator` column says who wrote each label. **As of 2026-09-13 every
沈伯洋 row is labeled by `claude-opus-5`**, at the project owner's decision,
working blind from `articles` (never from `article_stance`) under the rules
below, with a `note` on borderline calls. Two rows (156116, 156523) are flagged
in `note`: Claude had seen Gemini's verdict on them during a smoke test.

Consequence: a macro-F1 against this set measures **agreement between two
models**, not human validation. Two LLMs can share blind spots, so treat the
number as an upper-bound sanity check, not the proposal's milestone. The eval
report prints the annotator breakdown next to the F1 for this reason. To make
the milestone claim, a human labels (a sample of) the same rows under a
different annotator name and the eval is run against those.

## The task

You are labeling how **the article positions the TARGET** — the person,
organisation or policy that was searched for. You judge the article's
*selection, framing, whose voice gets the headline, and word choice*.

You are **not** judging:

- whether the events are good or bad news for the target
- the general mood of the prose
- whether *you* agree with the target

**Weighting: headline and lede first.** Reporters put the framing where readers
stop reading. The body is context; a balanced paragraph nine does not undo a
slanted headline.

## Labels

| key | label | meaning |
|---|---|---|
| `n` | **neg** | Selection, framing or word choice casts the target unfavorably. Critics get the headline with no response from the target. An accusation is amplified uncritically. |
| `e` | **neu** | A plain report with no evaluative framing — or a balanced one where the target and its critics both get comparable voice. |
| `p` | **pos** | Casts the target favorably: praise, achievement framing, the target's own framing adopted as the article's, critics absent or dismissed. |

Also: `s` skip (not about the target, wire copy you already labeled, broken
body), `b` show more body, `q` quit — everything labeled so far is already
saved.

## What is *not* decisive by itself

- **Quoting a critic.** Only `neg` if the article adopts or foregrounds the
  criticism (headline, lede, no reply). A critic quoted in paragraph six with
  the target's reply beside it is `neu`.
- **A bad outcome reported flatly.** "沈伯洋遭檢方約談" in plain language with
  no loaded verbs is `neu`. "沈伯洋涉案 遭檢方約談 拒絕回應" is leaning `neg`.
- **A good outcome reported flatly** is likewise `neu`, not `pos`.

## What *is* signal in Traditional Chinese news

- Epithets and honorifics attached to the target (「綠委」 vs 「立委」,
  「抗中保台大將」).
- Scare quotes around the target's own words.
- Loaded verbs: 遭批 / 挨轟 / 被打臉 / 跳針 (neg-leaning) vs 強調 / 澄清 /
  獲肯定 / 力挺 (pos-leaning).
- Whose framing the headline adopts: the accuser's words, or the target's.

## Edge cases

- **Article is about the target only in passing** → `s` skip, unless the one
  mention is itself pointed.
- **Wire copy** (中央社) reprinted verbatim by another outlet → label it once
  per outlet anyway; propagation is measured elsewhere and the reprint *is*
  that outlet's editorial choice.
- **Headline and body disagree** → headline and lede win.
- **Satire / opinion column** → label the stance it takes, note `opinion` in
  the `note` column.
- **Genuinely torn** → pick the closer label, add a short `note`. Notes are
  how we find where the definitions need sharpening.

## Workflow

```bash
make enrich KEYWORD=沈伯洋          # bodies must exist first
make label  KEYWORD=沈伯洋          # 50 unlabeled articles, shuffled across outlets
make label  KEYWORD=沈伯洋 ARGS="--n 20 --annotator <you> --seed 1"
```

The tool never shows the model's verdict. Label in sessions of 20–50; the
first F1 is worth looking at from ~50 labels, the milestone needs 300 across
several incidents (add 萬安 as a mostly-neutral contrast set, 關稅 as a
policy target). Commit the CSV with the label session's branch.

## CSV columns

`article_id, outlet, url, target, label, annotator, labeled_at, note` —
one row per (article, target). The same article may appear under two targets
with different labels; that is correct, not a duplicate.

---

# Duplicate-pair gold set — annotation guide (T-008)

`dup_gold.csv` holds labeled article pairs for the clusterer (proposal §9:
**precision > 0.90, recall > 0.80** on 200 pairs). Same provenance rule as the
stance set: the `annotator` column says who labeled, and the eval prints it.

## The question

**Is B the same copy as A — syndicated, reprinted, or lightly rewritten?**

`y` (duplicate) when:
- one is a reprint of the other, or both reprint the same source (CNA wire,
  鏡週刊, a press release run verbatim), with at most a paragraph added or
  dropped, the byline/dateline changed, or a few words edited;
- the same article served twice by one outlet (udn's section-path duplicates).

`n` (not) when:
- both articles quote the same statement or press release at length but are
  otherwise their own reporting — different headline, lede, structure;
- same event, same facts, independently written;
- one is a short teaser or listing blurb for the other (too little to be a copy).

`s` skip when either side is not a real article body (promo text only).

## What the tool shows

Both outlets, headlines, ledes, and a **shared-sentence count**. It never
shows the similarity score or the current cluster verdict. `b` prints more of
both bodies. Pairs are offered **stratified by similarity**, so most of what
you see sits near the boundary — that is deliberate; do not expect the mix to
look like the corpus.

## Workflow

```bash
make dedup                     # clusters must exist; this also fingerprints
make label.pairs ARGS="--n 100 --annotator <you> --seed 1"
make dedup.eval                # P/R at the defaults + a threshold sweep
```

---

# Post stance gold set — annotation guide (T-017)

`post_stance_gold.csv` holds human stance labels for Threads posts. T-016
keeps the Q4 panel's **model-labeled, unvalidated** caveat until this file has
**≥ 100 human rows**. The file is created by labeling; a missing file means
no labels yet.

The `annotator` column records who labeled each row. Use your own name for
human labels and identify any model annotator explicitly. Agreement with
model labels is not human validation and does not count toward that threshold.

## The task

Judge **stance toward the TARGET**, not mood and not your agreement with the
post. Use the author's own words only.

| Label | Meaning |
|---|---|
| `neg` | Casts the target unfavorably: criticism, blame, ridicule, or attack framing. |
| `neu` | Reports or mentions the target without a clear favorable or unfavorable stance; balanced or factual wording. |
| `pos` | Casts the target favorably: praise, support, achievement framing, or endorsement. |

On Threads:

- Sarcasm is common: label the *intended* stance and add `sarcasm` in `note`.
- A bare reaction depending on an unseen quote-post or reply → `s` skip.
  If you press `o` and use the link to decide, label and add `context` in
  `note`: the model will only ever see `text`.
- Hashtag-only or emoji-only text → `s` skip.
- A post about the target only in passing → `s`, unless the mention itself
  is pointed.

The model gets these same definitions (`POST_SYSTEM_INSTRUCTION` in
`src/parallax/nlp/stance.py`); change one and change the other, and bump
`POST_PROMPT_VERSION`. One place they deliberately differ: you can press `s`
and the model cannot, so where you would skip for missing context it is told
to answer `neu` and say so in its evidence. Skipped rows are simply absent from
this file, so they never reach the F1 — but they do still land in the Q4
panel's denominator as classified posts, which is why the floor exists.

## Keys

`n` = neg, `e` = neu, `p` = pos, `s` = skip, `o` = print the permalink,
`q` = quit. Every label is saved immediately. Add notes in the CSV's `note`
column after the session, preserving the other columns.

## Workflow

```bash
make social KEYWORD=沈伯洋
make label.posts KEYWORD=沈伯洋 ARGS="--n 30 --annotator <you> --seed 1"
make stance.posts KEYWORD=沈伯洋 ARGS=--dry-run   # what the model would cost
```

Label before you classify where you can: the tool never shows a model verdict,
but knowing one exists is its own anchor.

Posts are shuffled across authors, media-only posts are dropped, and previously
labeled permalink/target pairs are skipped on subsequent runs. The display
shows author, Taipei time and full text, never the model's verdict.

## CSV columns

`post_id, platform, post_url, author, target, label, annotator, labeled_at, note`

One row per `(post_url, target)`. The same permalink can have different labels
for different targets. `post_id` is only a convenience: database migration or
re-fetching can change it, so resume identity uses the permalink.

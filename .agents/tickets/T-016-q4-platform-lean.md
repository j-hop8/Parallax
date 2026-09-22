# T-016 — Q4: platform lean for Threads (stance on posts → distribution → panel)

**Owner:** claude — touches the stance prompt, the report seam and the UI, all
design-judgment surfaces. **Blocked by:** T-015 merged and ≥ 1 keyword with
real Threads rows.

## Why

T-015 puts posts in the database; this ticket turns them into the Q4 number
the design page has carried as an inert panel since T-011: for each platform,
the distribution of stance toward the incident's target.

## Design (to settle in plan mode before coding — these are the questions)

1. **Post stance prompt.** The article prompt (`nlp/stance.py` `v1`) assumes
   headline / lede / body. Posts are one short passage with slang, sarcasm
   and quote-posts. Options: (a) a `post-v1` prompt variant sharing the label
   definition in `eval/README.md` verbatim, stored with
   `prompt_version='post-v1'`; (b) feed the post as `headline` and leave the
   rest empty. Lean (a): sarcasm is common enough on Threads that the prompt
   should name it. Whichever is chosen, `social_posts.stance_model` /
   `prompt_version` (added in T-015) make verdicts comparable, never mixed.
2. **What counts.** Quote-posts (`is_quote_post`) carry the quoted stance
   plus the author's; decide whether to classify the author's text alone or
   skip quote-posts in v1. Replies-only threads and media-only posts
   (`text` empty) are skipped.
3. **Denominator floor.** Reuse the spirit of invariant 7: suppress the lean
   below `MIN_PLATFORM_POSTS` (proposed 30 classified posts per platform per
   window) and say so in the panel, the same way coverage weight does.
4. **Report seam.** `IncidentReport.platform_lean: tuple[PlatformLean, ...]`
   with `platform, posts, classified, neg, neu, pos, suppressed_reason`. The
   text report (`jobs/report.py`) and the UI both read it; nothing else
   queries `social_posts` directly.
5. **Gold set.** No post stance gold exists. Before the number is shown
   without a caveat, the human labels ≥ 100 posts with
   `scripts/label_stance.py` (or a sibling) using the same rules. Until then
   the panel carries the same "model-labeled, unvalidated" caveat as the
   article stance note (`STANCE_NOTE`). See memory: article gold is
   Claude-labeled, so this is the first human-labeled stance set either way.

## Decisions (settled 2026-09-22)

1. **Post prompt:** option (a). `POST_SYSTEM_INSTRUCTION` / `POST_PROMPT_VERSION
   = "post-v1"`, sharing eval/README.md's T-017 label table and naming sarcasm
   explicitly. One divergence from the human guide, documented in both places:
   the annotator can press `s` and the model cannot, so where a human skips for
   missing context the model is told to answer `neu` and say so in its evidence.
   Skipped rows are absent from the gold file and never reach the F1, but they
   do sit in the panel's denominator — which is part of why the floor exists.

2. **What counts:** the author's own text, always. `is_quote_post` turned out
   never to be persisted (`upsert_social_posts` does not store it and the
   quoted passage is not fetched), so there is no column to skip on and nothing
   but the author's words to judge — which is what the annotator is told to use
   too. Media-only posts (empty `text`) are dropped before the model, never sent.

3. **Floor:** `MIN_PLATFORM_POSTS = 30` classified posts per platform per
   window, env-overridable. Suppression carries a *reason code*
   (`no_posts` / `unclassified` / `below_floor`), not display text, so the text
   report and the panel each say it in their own register.

4. **Report seam:** as specified, plus `min_posts` on the row (so a renderer can
   print the floor without importing settings) and `post_prompt_version` on
   `IncidentReport`. The lean window is the incident's own Taipei days.

5. **Storage — deviates from "do not touch `db/**`", approved before coding.**
   The ticket assumed T-015's `social_posts.stance_model` / `prompt_version`
   were enough. They are not: the table has no **target** column, and stance is
   target-dependent by design — the same post is `neg` toward one target and
   `neu` toward another, which is exactly why the gold CSV is keyed on
   `(post_url, target)`. One inline verdict would mean two active keywords
   overwrite each other and re-spend quota on every switch, and there is
   nowhere to put the evidence phrase the article side treats as mandatory.
   New table `social_post_stance`, mirroring `article_stance`. The legacy
   `social_posts.stance_*` columns stay unused. **Needs `make db.migrate`.**

## Live

Not yet: `eval/post_stance_gold.csv` does not exist, so there is no post-stance
F1 and both renderers carry the unvalidated caveat unconditionally. It comes
out when the file holds ≥ 100 human rows and the eval reports a number here.

## Files in scope

- `src/parallax/nlp/stance.py` — post prompt variant + `PostStanceInput`
- `src/parallax/jobs/stance.py` — `--platform threads` mode, or a sibling
  `jobs/stance_social.py` if the argv diverges too much
- `src/parallax/metrics/lean.py` (new), `src/parallax/metrics/report.py`
- `src/parallax/jobs/report.py`, `src/parallax/ui/render.py` (`q4()` goes live
  for Threads; Facebook slot stays `暫緩`)
- `src/parallax/settings.py` — `MIN_PLATFORM_POSTS`
- `scripts/label_stance.py` or `scripts/label_posts.py`; `eval/post_stance_gold.csv`
- tests for the metric, the report field, and the panel

Added to scope during review (Codex flagged the diff leaking past the original
list; these two stay, the third was reverted):

- `Makefile` — `make stance.posts`. The list named `jobs/stance_social.py` but
  not its entry point, and every prior job ticket added its target. Removing it
  would leave the job reachable only as `uv run python -m ...` and would break
  the workflow block this ticket adds to `eval/README.md`.
- `eval/README.md` — decision 1 above requires the post prompt and the
  annotation guide to agree, and the model/human divergence over `s` has to be
  written down in the guide or the next annotator will not know about it.
- `src/parallax/metrics/__init__.py` — **reverted.** Exporting `PlatformLean`
  beside the other metrics was consistency, not necessity; nothing imports from
  the package root. Left for a later tidy-up rather than widening this diff.

## Do not touch

`src/parallax/crawl/**`, `src/parallax/social/**` (T-015's surface — file a
follow-up if the client needs a change), the article prompt `v1` text,
`config/outlets.yaml`, `db/**` beyond what the lean query needs (should be
nothing).

## Acceptance criteria

- `make report KEYWORD=沈伯洋` prints a Q4 block: platform, n classified,
  neg/neu/pos, or the suppression reason.
- `make ui` Q4 panel shows the Threads distribution bar with the same visual
  grammar as the outlet stance bars; Facebook remains the parked slot;
  no digits when suppressed.
- Verdicts for posts carry `stance_model` and `prompt_version`; changing the
  prompt text bumps the version and the report filters on it.
- Suppression below `MIN_PLATFORM_POSTS` tested at n-1 / n.
- Panel caveat text present until `eval/post_stance_gold.csv` has ≥ 100 rows
  and the eval job reports F1 — the eval number goes in this ticket's
  **Live** section when it exists.

## Verify

```bash
uv run pytest -q tests/test_lean.py tests/test_report_db.py tests/test_ui_render.py && uv run ruff check . && uv run pytest -q
```

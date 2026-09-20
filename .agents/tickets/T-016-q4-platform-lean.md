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

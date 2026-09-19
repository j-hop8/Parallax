# T-011 — Streamlit UI: the design page, rendered from `IncidentReport`

## Why

T-010 built the report object and a text readout; the proposal's week-4
exit criterion is "typing a keyword returns outlet stance, coverage counts,
and at least one correctly identified copy cluster with a readable diff" --
in a browser, on the page `Design.pdf` shows. This ticket renders that page.
It is a rendering exercise by design: the UI takes `IncidentReport` and
never runs SQL, so everything T-010 measured (a weight only with a basis,
順序不明 without ranks) is inherited rather than re-implemented -- but a
renderer can still lie by omission or by scale, and those are the
acceptance criteria here.

## What the corpus said first (read-only, 2026-09-19, 31,913 tier-1 rows)

- **The design's bars are mocks at the wrong scale.** Live weights run 0.1%
  (ettoday) to 1.0% (setn) for 沈伯洋; the design draws 2–10%. A bar scaled
  to a fixed 10% is a row of hairlines. The bar scale must be the report's
  max weight (T-010 said so; now it binds).
- **Stance covers about half the matches.** ltn 44/77 classified, ftv 45/94,
  cna 9/19. The design's stance bar implies a full distribution; ours must
  say `44 / 77 已分類` beside every bar and draw an empty track when 0.
- **Only 沈伯洋 has stance rows** at the configured model (184 at
  gemini-3.5-flash-lite v1). 關稅 and 颱風 match hundreds of titles but
  their Q1 columns are `—`. The page needs a visible "no stance yet" state
  that does not look like "neutral".
- **1 of 14 stored clusters is indeterminate** (34939, udn/udn, gap 0s). The
  順序不明 path is exercised live, not only in tests.
- **99 tier-1 titles contain `<`, `>`, `&` or `"`** -- e.g.
  `文化預算遭諷青鳥飼料！<大濛>醫生娘開砲`. Rendered unescaped through
  `st.markdown(unsafe_allow_html=True)` the browser eats `<大濛>` as a tag.
  Every corpus string on the page goes through `html.escape`.
- **Deltas are long.** Cluster 28421's tvbs member carries 19 delta lines;
  `shared_core_text` reaches 1,683 chars; summaries top out at 59. The card
  shows the summary line and a clipped core; full deltas sit behind a
  per-member expander.
- Every outlet reports `exact` on 2/16 active days. The "days used" figure
  is the honest caveat and belongs next to the weight, not in a footnote.

## Design

**Split renderer from shell.** `ui/render.py` is pure: `IncidentReport` in,
HTML strings out, no Streamlit import, tested offline with the same fake
report `test_report_jobs.py` uses. `ui/app.py` is the Streamlit script:
inputs → `build_report` → `st.markdown(html, unsafe_allow_html=True)`. HTML
over native widgets because the design's stacked stance bar, weight bar and
群組 cards have no Streamlit equivalent, and because one CSS block gives the
cream page in `Design.pdf` instead of the default dashboard look.

**Page.** Wordmark row (`Parallax 視差` / 同一事件，不同角度，被測量).
Sidebar: keyword text input, optional 起 / 迄 Taipei dates, and pills for the
keywords that have stance rows (`db.stance_targets`, one new read helper) so
a first-time viewer finds the one incident that is fully populated. Then, in
design order:

- **Header:** 事件關鍵字, the four counters (篇文章 / 家媒體 / 個抄襲群 /
  天 = `span_days`) and a caption `N 個活躍日 first → last`.
- **Q1 + Q2 table:** one row per `OutletRow` in config order. Stance bar =
  neg / neu / pos segments proportional to `classified`, with the counts and
  `classified / matched 已分類`; `classified == 0` draws an empty track and
  `尚未分類`. Weight bar = purple fill scaled to the report's max weight,
  the percentage at one decimal, and `basis` + `days_used / days_active 天`
  in small type. `estimated` draws a hatched fill and `估計`. `none` draws
  no bar and `—`. Originality (`original` / `strict`) in a fourth column.
- **Q4 panel:** the design's PTT / Dcard block rendered as an inert
  placeholder, `社群平台尚未接入（Phase 2）`. Never a number.
- **Q3 cards:** one per `ClusterView`, `first_at` order. Confident: `起源：
  outlet (HH:MM)`, `n 家媒體跟進`, 核心稿源 excerpt (clipped), member rows
  `#rank outlet HH:MM (+gap) summary`. Indeterminate: `順序不明 · reason`,
  no `#`, no `起源`, `＋` lines labelled 本版獨有, and never a `－` line.
  Members that did not match the keyword are marked 標題未含關鍵字.
  Full deltas per member behind `st.expander`, default collapsed.
- **Footer:** 分母更新於 …, day-shift count, stance model / prompt version
  -- the same three facts the text readout prints.
- **Empty:** `找不到含「…」的標題` and nothing else. Errors from `build_report`
  (DB down) surface as `st.error` with the exception text, not a stack trace.

**Caching.** `@st.cache_data(ttl=300)` on the report build keyed by
`(keyword, since, until)`; the report is a frozen dataclass and pickles.
A 重新整理 button clears it.

**Theme.** `.streamlit/config.toml` with the design's cream background,
dark ink, purple primary; wide layout. Committed, because the look is part
of the deliverable.

**Run.** `make ui` → `uv run --extra ui streamlit run src/parallax/ui/app.py`.
`app.py` uses absolute imports (`from parallax...`) because Streamlit and
`AppTest` execute it as a script, not a module.

## Files in scope

- `src/parallax/ui/{app,render}.py`, `src/parallax/ui/__init__.py`
- `src/parallax/db.py` (`stance_targets`)
- `.streamlit/config.toml`, `Makefile` (`ui`), `pyproject.toml` (`ui` extra
  pins only if needed), `README.md` (one run line)
- `tests/test_ui_render.py` (pure), `tests/test_ui_app.py` (`AppTest`,
  skipped without streamlit, `build_report` monkeypatched -- no DB)
- this ticket (archived to `done/` at ship)

## Do not touch

`metrics/**` and `jobs/report.py` (the report is the contract; a rendering
need that requires a new field is a T-010 follow-up, not a quiet edit),
`search.py`, `nlp/**`, `crawl/**`, `db/schema.sql`, every write path.

## Acceptance criteria

- `render.py` imports no Streamlit; `tests/test_ui_render.py` passes with
  streamlit uninstalled.
- A title of `<大濛>醫生娘` renders as text: the HTML contains `&lt;大濛&gt;`
  and no `<大濛>`. Same for summaries, deltas and the shared core.
- Weight bar width is relative to the report's max weight: the outlet at
  max fills 100%; a report whose max is 1.0% still shows a full bar there.
- `basis == "none"` emits no bar element and `—`; `estimated` emits the
  hatched class and 估計; `exact` emits `days_used / days_active`.
- `classified == 0` emits no colored segment, an empty track, and 尚未分類;
  `classified > 0` emits three segments whose widths sum to 100%.
- An indeterminate cluster's card contains 順序不明 and its reason, no `#`,
  no 起源, no `－`, and 本版獨有 for its `＋` lines -- with `delta_removed`
  non-empty on the input to prove the renderer, not just T-010, enforces it.
- Q4 panel contains no digit.
- `AppTest`: an empty keyword renders the prompt and calls nothing; a
  keyword renders the header counters; `build_report` raising surfaces as
  `st.error` with the message.
- `make ui` starts and `http://localhost:8501` shows 沈伯洋 with eight rows,
  four cards, one of them 順序不明. `關稅` shows `—` stance and 尚未分類 on
  every row.
- `uv run pytest -q` passes offline; `uv run ruff check .` clean.

## Verify

```bash
uv run pytest -q && uv run ruff check .
make ui        # then 沈伯洋, 關稅, 不存在的字 in the browser
```

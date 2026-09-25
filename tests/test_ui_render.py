"""The HTML says only what the report supports -- and says it at a scale a
reader can see. Same fake report as the text readout's tests."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import replace

import pytest

from parallax.metrics.propagation import ClusterView
from parallax.ui import render
from tests.test_report_jobs import D1, _cov, _lean, _mv, _report


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _widths(fragment: str) -> list[float]:
    return [float(w) for w in re.findall(r'style="width:([\d.]+)%"', fragment)]


def test_render_module_does_not_import_streamlit():
    code = "import sys; sys.modules['streamlit'] = None; import parallax.ui.render"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_corpus_strings_are_escaped():
    r = _report(keyword="<b>看護</b>")
    c = r.cluster_views[0]
    nasty = replace(
        c,
        shared_core_text="A & B <i>核心</i>",
        origin=replace(c.origin, title="<大濛>醫生娘", delta_summary='<script>alert("x")</script>'),
        members=(
            replace(
                c.members[0], title="<大濛>醫生娘", delta_summary='<script>alert("x")</script>'
            ),
            replace(c.members[1], delta_added=("<img src=x>",)),
        ),
    )
    out = render.page(replace(r, cluster_views=(nasty,)))
    assert "&lt;大濛&gt;醫生娘" in out and "<大濛>" not in out
    assert "&lt;script&gt;" in out and "<script>" not in out
    assert "A &amp; B &lt;i&gt;核心&lt;/i&gt;" in out
    assert "&lt;img src=x&gt;" in out and "<img" not in out
    assert "&lt;b&gt;看護&lt;/b&gt;" in out and "<b>看護</b>" not in out


def test_weight_bar_is_scaled_to_the_reports_maximum():
    out = render.table(_report())
    rows = out.split("<tr>")[2:]  # skip the header row
    cna, ltn, udn = rows
    assert 'class="fill" style="width:100%"' in cna and "2.1%" in cna
    assert "實測 · 2 / 3 天" in cna
    # udn is estimated at 0.4% against a 2.1% maximum: ~19% of the bar, hatched.
    assert 'class="fill est" style="width:19%"' in udn and "估計" in udn
    # No basis: no bar element at all, a dash, and the day count.
    assert "fill" not in ltn and ">—<" in ltn and "無分母 · 0 / 3 天" in ltn


def test_a_tiny_maximum_still_fills_the_bar():
    r = _report()
    rows = tuple(
        replace(row, coverage=_cov(row.outlet, row.matched, 0.001, "exact", 1, 3)) for row in r.rows
    )
    out = render.table(replace(r, rows=rows))
    assert out.count('class="fill" style="width:100%"') == len(rows)
    assert "0.1%" in out


def test_stance_bar_over_classified_or_empty():
    r = _report()
    cna = render.stance_bar(r.rows[0])
    assert cna.count("px-seg") == 3
    assert sum(_widths(cna)) == 100
    assert "8 / 10 已分類" in cna and "負 1 · 中立 5 · 正 2" in cna
    ltn = render.stance_bar(r.rows[1])
    assert "px-seg" not in ltn and "px-stance empty" in ltn
    assert "尚未分類" in ltn and "0 / 4 已分類" in ltn


def test_confident_cluster_card():
    r = _report()
    out = render.clusters(r)
    a = out[out.index("群組 A") : out.index("群組 B")]
    assert "起源：中央社" in a and "(01-01 09:12)" in a and "1 家媒體跟進 · 2 篇" in a
    assert "#1" in a and "#2" in a and "+12分" in a
    assert "＋ 加入鄰居受訪" in a and "完整差異 1 行" in a
    assert "標題未含關鍵字" in a  # the ltn member did not match the keyword
    assert "自由時報" in a  # outlet codes are shown by their configured name


def test_indeterminate_cluster_has_no_rank_origin_or_direction():
    r = _report()
    c = r.cluster_views[1]
    # A stale row carrying delta_removed must not leak a direction here either.
    stale = replace(
        c,
        members=(
            replace(c.members[0], delta_removed=("這句不該出現",)),
            c.members[1],
        ),
    )
    out = render.cluster_card(stale, 1, {"udn": "聯合報"})
    assert "順序不明" in out and "first two both udn" in out
    assert "#1" not in out and "#2" not in out and "起源" not in out
    assert "－" not in out and "這句不該出現" not in out
    assert "本版獨有 本版才有" in out
    # Members still appear, in time order, without ranks.
    assert out.count("px-member") == 2


def test_full_deltas_are_not_clipped_behind_details():
    r = _report()
    c = r.cluster_views[0]
    long = "甲" * 300 + "尾端關鍵差異"
    m = replace(c.members[1], delta_added=(long,))
    out = render.cluster_card(replace(c, members=(c.members[0], m)), 0, {})
    assert "尾端關鍵差異" in out and "…" not in out.split("<details>")[1]


def test_followers_count_outlets_not_articles():
    r = _report()
    c = r.cluster_views[0]
    # A second ltn article and a second cna article: still one outlet followed.
    members = c.members + (
        replace(c.members[1], article_id=7, rank=3),
        replace(c.members[0], article_id=8, rank=4),
    )
    out = render.cluster_card(replace(c, members=members), 0, {})
    assert "1 家媒體跟進 · 4 篇" in out


def test_q4_panel_draws_the_threads_split_in_the_stance_bars_grammar():
    """Same segments, same caption shape as an outlet row -- the two blocks sit
    side by side on the page and must be read on one ruler."""
    panel = render.q4(_report())
    box = panel[panel.index("Threads") :]
    assert box.count("px-seg") == 3
    assert sum(_widths(box)) == 100
    assert "負 70 · 中立 35 · 正 15" in box
    assert "120 / 400 已分類" in box
    # The design's parked slot survives: a platform with no read path is not a zero.
    assert "Facebook" in panel and "暫緩：無合規資料管道" in panel
    assert "PTT" not in panel and "Dcard" not in panel


def test_q4_panel_prints_no_split_digits_when_suppressed():
    """The counts are in the report; below the floor they must not reach the page."""
    r = _report(platform_lean=(_lean(posts=40, classified=29, reason="below_floor"),))
    panel = render.q4(r)
    threads = panel[panel.index("Threads") : panel.index("Facebook")]
    assert "px-stance empty" in threads and "px-seg" not in threads
    assert "樣本不足（29 / 30）" in threads
    assert "29 / 40 已分類" in threads, "the denominator is still honest"
    for n in ("負 70", "中立 35", "正 15"):
        assert n not in threads


@pytest.mark.parametrize(
    ("reason", "expected"), [("no_posts", "查無貼文"), ("unclassified", "尚未分類")]
)
def test_q4_panel_says_which_kind_of_nothing_it_has(reason, expected):
    r = _report(platform_lean=(_lean(posts=0, classified=0, reason=reason),))
    assert expected in _text(render.q4(r))


def test_q4_panel_always_carries_the_unvalidated_caveat():
    """Article stance has a gold set and post stance does not; the panel says so
    until eval/post_stance_gold.csv reaches 100 human rows."""
    assert "尚無人工黃金標準" in _text(render.q4(_report()))
    assert render.POST_STANCE_NOTE != render.STANCE_NOTE


def test_q4_panel_is_reachable_from_the_page():
    out = render.page(_report(), social=True)
    assert "px-q4" in out and "120 / 400 已分類" in out


def test_header_counters_and_footer():
    out = render.page(_report())
    head = out[: out.index("px-section")]
    for n, label in ((20, "篇文章"), (3, "家媒體"), (2, "個抄襲群"), (3, "天期間")):
        assert f'<div class="n mono">{n}</div><div class="l">{label}</div>' in head
    assert "3 個活躍日 · 2030-01-01 → 2030-01-03" in head
    assert "分母更新於 01-01 09:12" in out and "1 篇文章的歸檔日與抓取日不同" in out
    assert "立場模型 m v1" in out


def test_window_is_shown_when_given():
    out = render.header(_report(since=D1))
    assert "（篩選 2030-01-01 → …）" in out


def test_empty_report_is_one_line():
    out = render.page(_report(articles=0, outlets=0, clusters=0, rows=(), cluster_views=()))
    assert "找不到含「看護」的標題" in out
    assert "px-section" not in out


def test_member_on_a_later_day_shows_its_date():
    c = ClusterView(
        cluster_id=9,
        origin_confident=True,
        reason="",
        origin=_mv(1, "cna", 0, 1),
        members=(_mv(1, "cna", 0, 1), _mv(2, "ltn", 26 * 60, 2)),
        shared_core_text=None,
    )
    out = render.cluster_card(c, 0, {})
    assert "<b>01-02 11:12</b>" in out and "+26時00分" in out
    assert "尚未計算共同核心" in out


def test_originality_column():
    out = render.table(_report())
    cna = out.split("<tr>")[2]
    assert ">100%<" in cna
    udn = out.split("<tr>")[4]
    assert ">67%<" in udn  # (4 alone + 0 first) / 6; the two unresolved credit nobody


def test_news_only_default_and_social_output_preserved():
    import hashlib

    out = render.page(_report())
    assert "px-q4" not in out and "px-grid" not in out
    assert render.STANCE_VALIDATION_NOTE in out
    social = render.page(_report(), social=True)
    caveat = f'<div class="muted small">{render.STANCE_VALIDATION_NOTE}</div>'
    assert hashlib.sha256(social.replace(caveat, "").encode()).hexdigest() == (
        "a9f2640d7f030d67aefc9b529f4a9e1355bc6dd6b082cf94f6f3febacd11fa3c"
    )


@pytest.mark.parametrize("minutes,stale", [(29, False), (30, False), (31, True)])
def test_status_strip_taipei_freshness_and_escaping(minutes, stale):
    from datetime import UTC, datetime, timedelta

    now = datetime(2030, 1, 1, 17, 0, tzinfo=UTC)
    out = render.status_strip({
        "extent": {"articles": 1234, "since": now},
        "health": [{"outlet": '<b>中央社</b>', "last_ok": now - timedelta(minutes=minutes)}],
        "complete_days": {"cna": 2, "udn": 4},
        "rollup_as_of": now,
        "keywords": 3,
        "now": now,
    })
    assert "1,234" in out and "2030-01-02" in out
    assert f"00:{60 - minutes:02}" in out
    assert "2–4 天" in out and "3 個關鍵字" in out
    assert ("爬蟲延遲" in out) is stale
    if stale:
        assert "&lt;b&gt;中央社&lt;/b&gt;" in out and "<b>中央社" not in out
    assert render.status_strip(None) == ""


def test_status_strip_empty_index():
    from datetime import datetime

    out = render.status_strip({
        "extent": {"articles": 0, "since": None}, "health": [],
        "complete_days": {}, "rollup_as_of": None, "keywords": 0,
        "now": datetime.now(render.TZ),
    })
    assert "尚無資料" in out and "尚無紀錄" in out and "0 天" in out

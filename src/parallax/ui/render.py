"""The design page as HTML, from an IncidentReport.

Pure: no Streamlit, no SQL. `page(report)` is the whole thing; the smaller
functions exist so the tests can pin what each block may and may not say.
The report already refuses to emit a weight without a basis or a rank
without a confident origin (T-010); what a renderer can still get wrong is
scale and omission, so the rules that live here are:

- the weight bar is scaled to the report's own maximum, because live weights
  are 0.1-1.0% and a bar drawn against the design's 10% is a hairline;
- the stance bar is drawn over the classified articles and says how many
  that is, because it is usually about half of the matches;
- every string that came from an outlet is HTML-escaped -- 99 tier-1 titles
  contain `<`, `>`, `&` or `"`, and a browser eats `<大濛>` as a tag;
- an indeterminate cluster's `delta_removed` is ignored even if the row
  carries one (invariant 5, enforced here as well as upstream).

Output is single-line HTML: Streamlit's markdown path treats an indented
line as a code block, and `st.html` is happier without stray whitespace.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..metrics.lean import PlatformLean
from ..metrics.propagation import ClusterView, MemberView
from ..metrics.report import IncidentReport, OutletRow
from ..settings import SOCIAL_ENABLED, TIMEZONE

TZ = ZoneInfo(TIMEZONE)

CORE_CHARS = 120

STANCE_NOTE = "立場分數來自「目標依存情緒分析」，判斷對事件當事人的態度，而非文句整體語氣。"

STANCE_VALIDATION_NOTE = (
    "文章立場目前由模型標註，評估用的黃金標準亦為模型標註，尚待人工驗證；請視為訊號而非量測值。"
)

# The Q4 counterpart of STANCE_NOTE, plus the thing the article note does not
# have to say: article stance has a gold set, post stance has none yet. This
# line comes out when eval/post_stance_gold.csv reaches 100 human rows and the
# eval job reports an F1 -- not before, and not because the bar looks lonely.
POST_STANCE_NOTE = "社群貼文立場由模型標註，尚無人工黃金標準可驗證，請視為訊號而非量測值。"

# metrics.lean.Reason in the panel's register. A reason with no entry here is
# a KeyError at render time rather than a silently blank bar.
LEAN_REASONS = {
    "no_posts": "查無貼文",
    "unclassified": "尚未分類",
    "below_floor": "樣本不足",
}

# Platform display names and the caption shown where there is no data path.
PLATFORM_NAMES = {"threads": "Threads"}
PARKED = (("Facebook", "暫緩：無合規資料管道"),)

CSS = """<style>
.px{--bg:#F5F0E8;--card:#FAF6EF;--ink:#1D1B18;--muted:#8A8478;--line:#DDD5C6;
--neg:#C43D2B;--neu:#DDD5C3;--pos:#1F6A4E;--weight:#7B4DFF;--track:#E8E1D4;
font-family:-apple-system,"Helvetica Neue","PingFang TC","Noto Sans TC",sans-serif;
color:var(--ink);max-width:1080px;margin:0 auto;line-height:1.45}
.px .mono{font-family:"SF Mono",Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
.px .muted{color:var(--muted)}
.px .small{font-size:12px}
.px-status{color:var(--muted);font-size:12px;margin-top:12px;
border-top:1px solid var(--line);padding-top:8px}
.px-status strong{color:var(--neg)}
.px.px-status-compact{width:100%;max-width:none;margin:0}
.px-wordmark{display:flex;justify-content:space-between;align-items:baseline;
border-bottom:1px solid var(--line);padding:6px 0 14px;margin-bottom:28px}
.px-wordmark .brand{font-family:Georgia,"Noto Serif TC","Songti TC",serif;font-size:26px;
font-weight:600;letter-spacing:.02em}
.px-wordmark .brand span{font-family:inherit;font-size:14px;font-weight:400;margin-left:8px}
.px-head{display:flex;justify-content:space-between;align-items:flex-end;gap:32px;
flex-wrap:wrap;margin-bottom:36px}
.px-head .kw-label{color:var(--weight);font-size:12px;letter-spacing:.08em}
.px-head h1{font-size:34px;margin:2px 0 4px;font-weight:700}
.px-counters{display:flex;gap:28px}
.px-counters .n{font-size:26px;font-weight:600}
.px-counters .l{font-size:12px;color:var(--muted)}
.px-section{margin-bottom:36px}
.px-section h2{font-size:14px;font-weight:600;margin:0 0 2px;letter-spacing:.04em}
.px-section .sub{font-size:12px;color:var(--muted);margin-bottom:14px}
.px-grid{display:grid;grid-template-columns:minmax(0,3fr) minmax(260px,2fr);gap:40px;align-items:start}
@media (max-width:820px){.px-grid{grid-template-columns:1fr}}
.px-table{width:100%;border-collapse:collapse}
.px-table th{font-size:11px;font-weight:500;color:var(--muted);text-align:left;
padding:0 8px 8px 0;border-bottom:1px solid var(--line)}
.px-table td{padding:12px 8px 12px 0;border-bottom:1px solid var(--line);vertical-align:middle}
.px-table td.name{width:88px;font-size:14px;white-space:nowrap}
.px-table td.stance{width:44%}
.px-table td.weight{width:30%}
.px-table td.orig{width:64px;text-align:right}
.px-stance{display:flex;height:16px;background:var(--track);border-radius:2px;overflow:hidden}
.px-stance.empty{background:transparent;border:1px dashed var(--line)}
.px-seg{height:100%}
.px-seg.neg{background:var(--neg)}.px-seg.neu{background:var(--neu)}.px-seg.pos{background:var(--pos)}
.px-caption{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:4px}
.px-weight{display:flex;align-items:center;gap:10px}
.px-weight .bar{flex:1;height:6px;background:var(--track);border-radius:3px;overflow:hidden}
.px-weight .fill{height:100%;background:var(--weight)}
.px-weight .fill.est{background:repeating-linear-gradient(135deg,var(--weight) 0 3px,transparent 3px 6px)}
.px-weight .pct{width:52px;text-align:right;font-size:13px}
.px-weight .pct.none{color:var(--muted)}
.px-basis{font-size:11px;color:var(--muted);margin-top:3px}
.px-q4 .box{background:var(--card);border:1px solid var(--line);border-radius:4px;
padding:16px;margin-bottom:14px}
.px-q4 .box .t{font-family:Georgia,serif;font-size:18px;font-weight:600}
.px-q4 .box .px-stance{margin-top:10px}
.px-card{background:var(--card);border:1px solid var(--line);border-radius:4px;margin-bottom:20px}
.px-card .head{display:flex;gap:14px;align-items:baseline;padding:14px 18px;
border-bottom:1px solid var(--line);flex-wrap:wrap}
.px-card .head .tag{color:var(--weight);font-weight:600;font-size:13px}
.px-card .head .origin{font-weight:600}
.px-card .head .unknown{color:var(--neg);font-weight:600}
.px-card .body{padding:14px 18px 6px}
.px-card .label{font-size:11px;color:var(--muted);margin:6px 0}
.px-core{background:var(--bg);border:1px solid var(--line);border-radius:3px;padding:10px 12px;
font-size:14px;margin-bottom:14px}
.px-member{display:grid;grid-template-columns:32px 96px 128px minmax(0,1fr);gap:8px;
padding:9px 0;border-bottom:1px solid var(--line);font-size:13px;align-items:start}
.px-member:last-child{border-bottom:0}
.px-member .rank{color:var(--muted)}
.px-member .when{color:var(--muted);white-space:nowrap}
.px-member .when b{color:var(--ink);font-weight:500}
.px-member .nomatch{color:var(--muted);font-size:11px}
.px-member details{margin-top:4px}
.px-member summary{cursor:pointer;font-size:11px;color:var(--muted)}
.px-member ul{margin:6px 0 0;padding-left:0;list-style:none}
.px-member li{padding:2px 0;font-size:12px}
.px-foot{border-top:1px solid var(--line);padding-top:12px;font-size:11px;color:var(--muted)}
.px-empty{padding:40px 0;color:var(--muted);font-size:16px}
</style>"""


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _pct1(x: float) -> str:
    return f"{100 * x:.1f}%"


def _pct0(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.0f}%"


def _hhmm(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")


def _mmdd(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%m-%d")


def _gap(td: timedelta) -> str:
    s = int(td.total_seconds())
    return f"+{s // 60}分" if s < 3600 else f"+{s // 3600}時{(s % 3600) // 60:02d}分"


def _letter(i: int) -> str:
    return chr(ord("A") + i) if i < 26 else str(i + 1)


def wordmark() -> str:
    return (
        '<div class="px"><div class="px-wordmark">'
        '<div class="brand">Parallax<span>視差</span></div>'
        '<div class="muted small">同一事件，不同角度，被測量</div>'
        "</div></div>"
    )


def prompt() -> str:
    return '<div class="px"><div class="px-empty">輸入事件關鍵字，看八家媒體怎麼報。</div></div>'


def empty(keyword: str) -> str:
    return f'<div class="px"><div class="px-empty">找不到含「{esc(keyword)}」的標題。</div></div>'


def header(r: IncidentReport) -> str:
    window = ""
    if r.first_day:
        window = f"{r.active_days} 個活躍日 · {r.first_day} → {r.last_day}"
    if r.since or r.until:
        window += f"（篩選 {r.since or '…'} → {r.until or '…'}）"
    counters = "".join(
        f'<div><div class="n mono">{n}</div><div class="l">{label}</div></div>'
        for n, label in (
            (r.articles, "篇文章"),
            (r.outlets, "家媒體"),
            (r.clusters, "個抄襲群"),
            (r.span_days, "天期間"),
        )
    )
    return (
        '<div class="px-head"><div>'
        '<div class="kw-label">事件關鍵字</div>'
        f"<h1>{esc(r.keyword)}</h1>"
        f'<div class="muted small">{window}</div>'
        f'</div><div class="px-counters">{counters}</div></div>'
    )


def stance_bar(row: OutletRow) -> str:
    """Three segments over the classified count, or an empty dashed track."""
    if row.classified == 0:
        return (
            '<div class="px-stance empty"></div>'
            f'<div class="px-caption"><span>尚未分類</span><span>0 / {row.matched} 已分類</span></div>'
        )
    n = row.classified
    neg = round(100 * row.neg / n, 2)
    neu = round(100 * row.neu / n, 2)
    pos = round(100 - neg - neu, 2)
    segs = "".join(
        f'<div class="px-seg {cls}" style="width:{w}%"></div>'
        for cls, w in (("neg", neg), ("neu", neu), ("pos", pos))
    )
    return (
        f'<div class="px-stance">{segs}</div>'
        '<div class="px-caption">'
        f"<span>負 {row.neg} · 中立 {row.neu} · 正 {row.pos}</span>"
        f"<span>{row.classified} / {row.matched} 已分類</span></div>"
    )


def weight_bar(row: OutletRow, max_weight: float) -> str:
    """Fill relative to the report's maximum; nothing at all without a basis."""
    c = row.coverage
    if c.basis == "none" or c.weight is None:
        return (
            '<div class="px-weight"><div class="pct none mono">—</div></div>'
            f'<div class="px-basis">無分母 · 0 / {c.days_active} 天</div>'
        )
    width = f"{100 * c.weight / max_weight:.1f}".rstrip("0").rstrip(".") if max_weight > 0 else "0"
    if c.basis == "estimated":
        fill, basis = "fill est", f"估計 · 對照中位日 · 0 / {c.days_active} 天"
    else:
        fill, basis = "fill", f"實測 · {c.days_used} / {c.days_active} 天"
    return (
        '<div class="px-weight"><div class="bar">'
        f'<div class="{fill}" style="width:{width}%"></div></div>'
        f'<div class="pct mono">{_pct1(c.weight)}</div></div>'
        f'<div class="px-basis">{basis}</div>'
    )


def _max_weight(r: IncidentReport) -> float:
    return max(
        (row.coverage.weight for row in r.rows if row.coverage.weight is not None), default=0.0
    )


def table(r: IncidentReport) -> str:
    mx = _max_weight(r)
    rows = "".join(
        "<tr>"
        f'<td class="name">{esc(row.name_zh)}</td>'
        f'<td class="stance">{stance_bar(row)}</td>'
        f'<td class="weight">{weight_bar(row, mx)}</td>'
        '<td class="orig mono">'
        f"{_pct0(row.originality.original if row.originality else None)}"
        f'<div class="small muted">{_pct0(row.originality.strict if row.originality else None)}</div>'
        "</td></tr>"
        for row in r.rows
    )
    return (
        '<div class="px-section">'
        "<h2>Q1 · 立場傾向 ＋ Q2 · 涵蓋權重</h2>"
        '<div class="sub">每家媒體對此事件的情緒分佈，與其佔當日總發稿量的比例</div>'
        '<table class="px-table"><thead><tr>'
        "<th>媒體</th><th>立場分佈（負面 → 中立 → 正面）</th><th>當日涵蓋權重</th><th>原創率</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        f'<div class="muted small" style="margin-top:10px">{STANCE_NOTE}'
        " 涵蓋權重只在抓取完整且總量足夠的日子計算，並依報告內最大值定比例尺；"
        "原創率下方為嚴格定義（不屬於任何抄襲群）。</div>"
        f'<div class="muted small">{esc(STANCE_VALIDATION_NOTE)}</div>'
        "</div>"
    )


def lean_box(p: PlatformLean) -> str:
    """One platform, in the outlet stance bar's grammar: segments over classified.

    Suppressed rows draw the same empty dashed track a never-classified outlet
    gets and print no digits inside the bar -- the left caption says why, and
    the right one keeps the honest denominator so a reader can see how much of
    the platform was read. Widths are computed the same way as stance_bar so
    the two blocks on the page are measured on one ruler.
    """
    name = PLATFORM_NAMES.get(p.platform, p.platform)
    if p.suppressed_reason is not None:
        why = LEAN_REASONS[p.suppressed_reason]
        if p.suppressed_reason == "below_floor":
            why += f"（{p.classified} / {p.min_posts}）"
        return (
            f'<div class="box"><div class="t">{esc(name)}</div>'
            '<div class="px-stance empty"></div>'
            f'<div class="px-caption"><span>{why}</span>'
            f"<span>{p.classified} / {p.posts} 已分類</span></div></div>"
        )
    n = p.classified
    neg = round(100 * p.neg / n, 2)
    neu = round(100 * p.neu / n, 2)
    pos = round(100 - neg - neu, 2)
    segs = "".join(
        f'<div class="px-seg {cls}" style="width:{w}%"></div>'
        for cls, w in (("neg", neg), ("neu", neu), ("pos", pos))
    )
    return (
        f'<div class="box"><div class="t">{esc(name)}</div>'
        f'<div class="px-stance">{segs}</div>'
        '<div class="px-caption">'
        f"<span>負 {p.neg} · 中立 {p.neu} · 正 {p.pos}</span>"
        f"<span>{n} / {p.posts} 已分類</span></div></div>"
    )


def q4(r: IncidentReport) -> str:
    """Q4 for every live platform, then the parked slots.

    Threads is the phase-2 platform (official keyword-search API). Facebook
    stays as a slot so the design holds if a compliant read path ever opens;
    today there is none, and the caption says so rather than promising a date.
    """
    boxes = "".join(lean_box(p) for p in r.platform_lean)
    boxes += "".join(
        f'<div class="box"><div class="t">{name}</div>'
        '<div class="px-stance empty"></div>'
        f'<div class="px-caption"><span>尚未接入</span><span>{when}</span></div></div>'
        for name, when in PARKED
    )
    return (
        '<div class="px-section px-q4"><h2>Q4 · 社群平台傾向</h2>'
        '<div class="sub">各社群平台對此事件的情緒分佈</div>'
        f"{boxes}"
        f'<div class="muted small">{POST_STANCE_NOTE}</div>'
        "</div>"
    )


def _member_deltas(m: MemberView, confident: bool) -> str:
    """Full delta lines behind a <details>; removed lines only when a direction exists.

    Unclipped: the reader opened this to see the whole sentence, and a clip
    here would hide the tail of exactly the lines long enough to matter."""
    removed = m.delta_removed if confident else ()
    added_mark = "＋" if confident else "本版獨有"
    lines = [f"{added_mark} {' '.join(s.split())}" for s in m.delta_added]
    lines += [f"－ {' '.join(s.split())}" for s in removed]
    if not lines:
        return ""
    items = "".join(f"<li>{esc(line)}</li>" for line in lines)
    return f"<details><summary>完整差異 {len(lines)} 行</summary><ul>{items}</ul></details>"


def _member_row(m: MemberView, c: ClusterView, names: dict[str, str]) -> str:
    when = _hhmm(m.effective_at)
    if _mmdd(m.effective_at) != _mmdd(c.first_at):
        when = f"{_mmdd(m.effective_at)} {when}"
    gap = f" <span>{_gap(m.gap)}</span>" if m.gap is not None else ""
    rank = f"#{m.rank}" if m.rank is not None else ""
    summary = esc(m.delta_summary) if m.delta_summary else '<span class="muted">尚無摘要</span>'
    nomatch = "" if m.matched else ' <span class="nomatch">標題未含關鍵字</span>'
    return (
        '<div class="px-member">'
        f'<div class="rank mono">{rank}</div>'
        f'<div title="{esc(m.title)}">{esc(names.get(m.outlet, m.outlet))}{nomatch}</div>'
        f'<div class="when mono"><b>{when}</b>{gap}</div>'
        f"<div>{summary}{_member_deltas(m, c.origin_confident)}</div>"
        "</div>"
    )


def _followers(c: ClusterView) -> int:
    """Distinct outlets other than the origin's. Members are articles, and one
    outlet can run two of them; the design's "n 家媒體跟進" counts outlets."""
    assert c.origin is not None
    return len({m.outlet for m in c.members} - {c.origin.outlet})


def cluster_card(c: ClusterView, index: int, names: dict[str, str]) -> str:
    if c.origin is not None:
        o = c.origin
        head = (
            f'<span class="origin">起源：{esc(names.get(o.outlet, o.outlet))}'
            f' <span class="mono">({_mmdd(o.effective_at)} {_hhmm(o.effective_at)})</span></span>'
            f'<span class="muted small">{_followers(c)} 家媒體跟進 · {len(c.members)} 篇</span>'
        )
    else:
        head = (
            f'<span class="unknown">順序不明</span>'
            f'<span class="muted small">{esc(c.reason)} · {len(c.members)} 篇</span>'
        )
    core = (
        f'<div class="px-core">{esc(_clip(c.shared_core_text, CORE_CHARS))}</div>'
        if c.shared_core_text
        else '<div class="px-core muted">（尚未計算共同核心）</div>'
    )
    members = "".join(_member_row(m, c, names) for m in c.members)
    return (
        '<div class="px-card"><div class="head">'
        f'<span class="tag">群組 {_letter(index)}</span>{head}'
        f'<span class="muted small mono">#{c.cluster_id}</span>'
        '</div><div class="body">'
        '<div class="label">共同核心稿源（節錄）</div>'
        f"{core}"
        '<div class="label">各媒體傳播順序與框架差異</div>'
        f"{members}</div></div>"
    )


def clusters(r: IncidentReport) -> str:
    names = {row.outlet: row.name_zh for row in r.rows}
    if r.cluster_views:
        body = "".join(
            cluster_card(c, i, names)
            for i, c in enumerate(sorted(r.cluster_views, key=lambda c: c.first_at))
        )
    else:
        body = '<div class="muted">這些文章不屬於任何抄襲群。</div>'
    return (
        '<div class="px-section"><h2>Q3 · 抄襲與框架差異</h2>'
        '<div class="sub">哪些媒體共用同一份稿源，以及各自增減了什麼內容</div>'
        f"{body}</div>"
    )


def footer(r: IncidentReport) -> str:
    denom = (
        r.denominator_as_of.astimezone(TZ).strftime("%m-%d %H:%M")
        if r.denominator_as_of
        else "從未"
    )
    return (
        '<div class="px-foot">'
        f"分母更新於 {denom}（{TIMEZONE}） · "
        f"{r.day_shift} 篇文章的歸檔日與抓取日不同 · "
        f"立場模型 {esc(r.stance_model)} {esc(r.prompt_version)}"
        "</div>"
    )


def page(r: IncidentReport, *, social: bool = SOCIAL_ENABLED) -> str:
    if r.empty:
        return empty(r.keyword)
    content = (
        f'<div class="px-grid"><div>{table(r)}</div><div>{q4(r)}</div></div>'
        if social else table(r)
    )
    return (
        '<div class="px">'
        f"{header(r)}{content}"
        f"{clusters(r)}{footer(r)}"
        "</div>"
    )


def status_strip(status: dict | None, *, compact: bool = False) -> str:
    """A compact public progress hint; all timestamps are displayed in Taipei."""
    if status is None:
        return ""
    extent = status["extent"]
    since = extent["since"]
    since_label = since.astimezone(TZ).strftime("%Y-%m-%d") if since else "尚無資料"
    health = status["health"]
    last = max((r["last_ok"] for r in health if r["last_ok"]), default=None)
    days = list(status["complete_days"].values())
    day_label = str(min(days)) if days and min(days) == max(days) else (
        f"{min(days)}–{max(days)}" if days else "0"
    )
    rollup = status["rollup_as_of"]
    parts = [
        f"已索引 {extent['articles']:,} 篇 · 自 {since_label}",
        f"最近爬取 {_hhmm(last) if last else '尚無紀錄'}（台北）",
        f"每家媒體完整日 {day_label} 天",
        f"已分析 {status['keywords']} 個關鍵字",
        f"分母更新 {_hhmm(rollup) if rollup else '從未'}（台北）",
    ]
    if compact:
        parts = [parts[1]]
    last_ok = {r["outlet"]: r["last_ok"] for r in health}
    stale = [
        outlet for outlet in status["expected_outlets"]
        if last_ok.get(outlet) is None
        or status["now"] - last_ok[outlet] > timedelta(minutes=30)
    ]
    body = esc(" · ".join(parts))
    if stale:
        body += f" · <strong>{esc('爬蟲延遲：' + '、'.join(stale))}</strong>"
    wrapper = "px px-status-compact" if compact else "px"
    return f'<div class="{esc(wrapper)}"><div class="px-status small">{body}</div></div>'

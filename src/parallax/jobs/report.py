"""`make report KEYWORD=…` -- the design page as text.

Prints what build_report returns and nothing it does not: a weight only where
a basis exists, `—` elsewhere, and 順序不明 with no `#` ranks on a cluster
whose order the timestamps cannot support (invariant 5). The Streamlit page
(T-011) renders the same object.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import db
from ..metrics.lean import PlatformLean
from ..metrics.report import IncidentReport, OutletRow, build_report
from ..settings import SOCIAL_ENABLED, TIMEZONE

log = logging.getLogger(__name__)

LINE_CHARS = 72
MAX_LINES_PER_MEMBER = 4


def _local(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(TIMEZONE)).strftime("%m-%d %H:%M")


def _gap(td: timedelta) -> str:
    s = int(td.total_seconds())
    return f"+{s // 60}m" if s < 3600 else f"+{s // 3600}h{(s % 3600) // 60:02d}m"


def _clip(s: str, n: int = LINE_CHARS) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def _pct0(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.0f}%"


def _weight(row: OutletRow) -> str:
    c = row.coverage
    if c.basis == "exact":
        return f"{_pct(c.weight):>7}  exact  {c.days_used}/{c.days_active}d"
    if c.basis == "estimated":
        return f"{_pct(c.weight):>7}  est.   0/{c.days_active}d"
    return f"{'—':>7}  —      0/{c.days_active}d"


def render_header(r: IncidentReport) -> str:
    window = ""
    if r.first_day:
        window = f"{r.first_day} → {r.last_day}"
    if r.since or r.until:
        window += f"  (window {r.since or '…'} → {r.until or '…'})"
    lines = [
        (
            f"{r.keyword}  {r.articles} 篇文章  {r.outlets} 家媒體  {r.clusters} 個抄襲群  "
            f"{r.span_days} 天  ({r.active_days} active: {window})"
        )
    ]
    denom = _local(r.denominator_as_of) if r.denominator_as_of else "never"
    lines.append(
        f"denominator as of {denom} ({TIMEZONE}); "
        f"{r.day_shift} matched article(s) filed on a different Taipei day than polled; "
        f"stance {r.stance_model} {r.prompt_version}"
    )
    return "\n".join(lines)


def render_table(r: IncidentReport) -> str:
    """Q1 (stance) + Q2 (coverage weight) + originality, one line per outlet."""
    head = (
        f"{'outlet':<11}{'matched':>8}{'body':>6}{'neg':>5}{'neu':>5}{'pos':>5}"
        f"{'  weight  basis  days':<24}{'orig%':>7}{'strict%':>9}"
    )
    out = ["Q1 立場傾向 · Q2 涵蓋權重 · originality", head]
    for row in r.rows:
        o = row.originality
        stance = (
            f"{row.neg:>5}{row.neu:>5}{row.pos:>5}"
            if row.classified
            else f"{'—':>5}{'—':>5}{'—':>5}"
        )
        out.append(
            f"{row.outlet:<11}{row.matched:>8}{row.enriched:>6}{stance}"
            f"  {_weight(row):<22}"
            f"{_pct0(o.original if o else None):>7}{_pct0(o.strict if o else None):>9}"
        )
    out.append(
        "weight = matched / outlet total, pooled over complete days with total >= floor "
        "(days = used/active); est. = against the outlet's median complete day; — = no denominator"
    )
    return "\n".join(out)


# The panel and this block say the same thing in different registers; the codes
# they map are metrics.lean.Reason, so a new reason fails loudly here rather
# than printing an empty cell.
LEAN_REASONS = {
    "no_posts": "no posts matched this keyword in the window",
    "unclassified": "no verdicts yet -- run: make stance.posts KEYWORD=<keyword>",
    "below_floor": "below the floor: {classified} classified < {min_posts}",
}

# Until eval/post_stance_gold.csv carries >= 100 human rows and the eval reports
# an F1, every number in this block is a model's opinion nobody has checked.
POST_STANCE_CAVEAT = (
    "post stance is model-labeled and unvalidated: no human gold set exists yet, "
    "so read the split as a signal, not a measurement"
)


def _lean_line(p: PlatformLean) -> str:
    if p.suppressed_reason is None:
        n = p.classified
        lean = (p.pos - p.neg) / n
        counts = f"{p.neg:>6}{p.neu:>6}{p.pos:>6}"
        return f"{p.platform:<11}{p.posts:>7}{n:>12}{counts}   {lean:+.2f}"
    note = LEAN_REASONS[p.suppressed_reason].format(classified=p.classified, min_posts=p.min_posts)
    dashes = f"{'—':>6}{'—':>6}{'—':>6}"
    return f"{p.platform:<11}{p.posts:>7}{p.classified:>12}{dashes}   {note}"


def render_platform_lean(r: IncidentReport) -> str:
    """Q4: one line per live platform, counts or the reason they are withheld."""
    head = f"{'platform':<11}{'posts':>7}{'classified':>12}{'neg':>6}{'neu':>6}{'pos':>6}   lean"
    out = ["Q4 社群平台傾向", head]
    out += [_lean_line(p) for p in r.platform_lean] or ["(no window: nothing matched)"]
    out.append("facebook   parked: no compliant read path")
    out.append(f"{POST_STANCE_CAVEAT}; prompt {r.post_prompt_version}")
    return "\n".join(out)


def render_clusters(r: IncidentReport) -> str:
    if not r.cluster_views:
        return "Q3 抄襲與框架差異\n(no clusters touch these articles)"
    out = ["Q3 抄襲與框架差異"]
    for c in r.cluster_views:
        n_matched = sum(1 for m in c.members if m.matched)
        head = f"群組 {c.cluster_id}  {len(c.members)} members ({n_matched} matched)  "
        if c.origin is not None:
            head += f"起源: {c.origin.outlet} ({_local(c.origin.effective_at)})"
        else:
            head += f"順序不明: {c.reason}"
        out.append(head)
        core = _clip(c.shared_core_text, 80) if c.shared_core_text else "(no shared core stored)"
        out.append(f"  核心: {core}")
        for m in c.members:
            tag = f"#{m.rank}" if m.rank is not None else "  "
            gap = _gap(m.gap) if m.gap is not None else ""
            mark = "" if m.matched else " (title did not match)"
            summary = m.delta_summary or "(no summary yet)"
            out.append(
                f"  {tag:<3}{_local(m.effective_at)}  {m.outlet:<11}{gap:>8}  {summary}{mark}"
            )
            lines = [
                f"{'＋' if c.origin_confident else '本版獨有'} {_clip(s)}" for s in m.delta_added
            ]
            lines += [f"－ {_clip(s)}" for s in m.delta_removed]
            for line in lines[:MAX_LINES_PER_MEMBER]:
                out.append(f"        {line}")
            if len(lines) > MAX_LINES_PER_MEMBER:
                out.append(f"        … {len(lines) - MAX_LINES_PER_MEMBER} more")
        out.append("")
    return "\n".join(out).rstrip()


def render(r: IncidentReport, *, social: bool = SOCIAL_ENABLED) -> str:
    if r.empty:
        return f"no articles match {r.keyword!r}"
    return "\n\n".join(
        [render_header(r), render_table(r)]
        + ([render_platform_lean(r)] if social else [])
        + [render_clusters(r)]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Incident report for one keyword (Q1–Q4).")
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--since", type=date.fromisoformat, help="first Taipei day, inclusive")
    parser.add_argument("--until", type=date.fromisoformat, help="last Taipei day, inclusive")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.since and args.until and args.since > args.until:
        parser.error("--since is after --until")

    with db.connect() as conn:
        report = build_report(conn, args.keyword, args.since, args.until)
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

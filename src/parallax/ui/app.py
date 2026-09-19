"""`make ui` -- the design page in a browser.

Inputs in the sidebar, `build_report` once per (keyword, window), and the
HTML that `ui.render` produces. No SQL and no metric logic live here: the
report is the contract (T-010), the renderer is pure (T-011), and this file
is the seam between them and Streamlit.

Absolute imports on purpose: `streamlit run` and `AppTest` execute this file
as a script, where relative imports have no package to resolve against.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from parallax import db
from parallax.metrics.report import IncidentReport, build_report
from parallax.nlp.stance import PROMPT_VERSION
from parallax.settings import STANCE_MODEL
from parallax.ui import render

CACHE_TTL = 300


@st.cache_data(ttl=CACHE_TTL, show_spinner="計算中…")
def load_report(keyword: str, since: date | None, until: date | None) -> IncidentReport:
    with db.connect() as conn:
        return build_report(conn, keyword, since, until)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_targets() -> list[tuple[str, int]]:
    """Keywords with stance rows, or [] when the database is unreachable --
    the report call below reports that failure; this one stays quiet."""
    try:
        with db.connect() as conn:
            rows = db.stance_targets(conn, STANCE_MODEL, PROMPT_VERSION)
    except Exception:  # noqa: BLE001 -- any failure here only hides the hints
        return []
    return [(r["target"], r["n"]) for r in rows]


def _pick_target() -> None:
    """Copy the clicked pill into the keyword box, then clear the pill: a
    single-select pill that stays lit toggles *off* on its next click, which
    would silently do nothing after the reader has typed something else."""
    if st.session_state.get("pick"):
        st.session_state.keyword = st.session_state.pick
        st.session_state.pick = None


def sidebar() -> tuple[str, date | None, date | None]:
    with st.sidebar:
        st.text_input("事件關鍵字", key="keyword", placeholder="例：沈伯洋")
        targets = load_targets()
        if targets:
            counts = dict(targets)
            st.pills(
                "已有立場資料的關鍵字",
                [t for t, _ in targets],
                format_func=lambda t: f"{t}（{counts[t]}）",
                key="pick",
                on_change=_pick_target,
            )
        since = st.date_input("起（台北日，含）", value=None, format="YYYY-MM-DD")
        until = st.date_input("迄（台北日，含）", value=None, format="YYYY-MM-DD")
        if st.button("重新整理", help=f"報告快取 {CACHE_TTL // 60} 分鐘；分母由 make rollup 更新"):
            st.cache_data.clear()
    keyword = (st.session_state.get("keyword") or "").strip()
    return keyword, since or None, until or None


def main() -> None:
    st.set_page_config(page_title="Parallax 視差", page_icon="◐", layout="wide")
    st.html(render.CSS)
    st.html(render.wordmark())

    keyword, since, until = sidebar()
    if not keyword:
        st.html(render.prompt())
        return
    if since and until and since > until:
        st.error("「起」在「迄」之後。")
        return
    try:
        report = load_report(keyword, since, until)
    except Exception as e:  # noqa: BLE001 -- the page must say why, not trace back
        st.error(f"無法產生報告：{e}")
        return
    st.html(render.page(report))


main()

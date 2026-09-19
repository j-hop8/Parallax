"""The Streamlit shell, driven by AppTest with the database faked out: what it
shows before a keyword, what it does with one, and how a failure surfaces."""

from __future__ import annotations

import contextlib
from datetime import date
from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

import parallax.metrics.report as report_mod
from parallax import db
from tests.test_report_jobs import _report

# AppTest resolves a relative path against this file, not the cwd.
APP = str(Path(__file__).resolve().parents[1] / "src" / "parallax" / "ui" / "app.py")


@pytest.fixture
def app(monkeypatch):
    """Fake the two things the page reads. build_report records its calls."""
    calls: list[tuple] = []

    @contextlib.contextmanager
    def fake_connect():
        yield None

    def fake_build(conn, keyword, since, until):
        calls.append((keyword, since, until))
        return _report(keyword=keyword, since=since, until=until)

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(db, "stance_targets", lambda conn, m, p: [{"target": "看護", "n": 12}])
    monkeypatch.setattr(report_mod, "build_report", fake_build)
    st.cache_data.clear()
    at = AppTest.from_file(APP, default_timeout=30)
    at.calls = calls
    yield at
    st.cache_data.clear()


def _html(at: AppTest) -> str:
    return "\n".join(h.proto.body for h in at.get("html"))


def test_no_keyword_shows_prompt_and_calls_nothing(app):
    app.run()
    assert not app.exception
    assert "輸入事件關鍵字" in _html(app)
    assert app.calls == []
    assert app.sidebar.pills[0].options == ["看護（12）"]


def test_keyword_renders_the_page(app):
    app.run()
    app.sidebar.text_input[0].set_value("看護").run()
    assert not app.exception
    html = _html(app)
    assert "<h1>看護</h1>" in html
    assert '<div class="n mono">20</div><div class="l">篇文章</div>' in html
    assert "順序不明" in html and "起源：中央社" in html
    assert app.calls == [("看護", None, None)]


def test_pill_fills_the_keyword(app):
    app.run()
    app.sidebar.pills[0].set_value("看護").run()
    assert app.sidebar.text_input[0].value == "看護"
    assert app.calls == [("看護", None, None)]


def test_window_is_passed_and_inverted_window_is_refused(app):
    app.run()
    app.sidebar.text_input[0].set_value("看護")
    app.sidebar.date_input[0].set_value(date(2030, 1, 2))
    app.sidebar.date_input[1].set_value(date(2030, 1, 3)).run()
    assert app.calls == [("看護", date(2030, 1, 2), date(2030, 1, 3))]
    app.sidebar.date_input[1].set_value(date(2030, 1, 1)).run()
    assert [e.value for e in app.error] == ["「起」在「迄」之後。"]
    assert len(app.calls) == 1


def test_build_failure_is_an_error_box(app, monkeypatch):
    def boom(conn, keyword, since, until):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(report_mod, "build_report", boom)
    app.run()
    app.sidebar.text_input[0].set_value("看護").run()
    assert not app.exception
    assert [e.value for e in app.error] == ["無法產生報告：connection refused"]
    assert '<div class="px-section">' not in _html(app)


def test_empty_report_is_a_message(app, monkeypatch):
    monkeypatch.setattr(
        report_mod,
        "build_report",
        lambda conn, k, s, u: _report(
            keyword=k, articles=0, outlets=0, clusters=0, rows=(), cluster_views=()
        ),
    )
    app.run()
    app.sidebar.text_input[0].set_value("不存在的字").run()
    assert "找不到含「不存在的字」的標題" in _html(app)

"""Post gold persistence, permalink resume identity, and blind offline sessions."""

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from parallax.nlp.gold import (
    POST_COLUMNS,
    PostGoldRow,
    append_post_gold,
    load_post_gold,
    pending_posts,
    post_label_session,
)
from scripts import label_posts

NOW = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)
TARGET = "沈伯洋"


def post(i, **extra):
    return {
        "id": i,
        "platform": "threads",
        "post_url": f"https://threads.net/@a/post/{i}",
        "author": "author",
        "text": f"完整內容 {i}\n下一行",
        "posted_at": NOW,
    } | extra


def row(i, **extra):
    return replace(
        PostGoldRow(
            i, "threads", post(i)["post_url"], "author", TARGET, "neu", "human", NOW.isoformat()
        ),
        **extra,
    )


def test_round_trip_header_once_and_missing_file(tmp_path):
    path = tmp_path / "nested" / "gold.csv"
    assert load_post_gold(path) == []
    rows = [row(1, label="neg", note='sarcasm, "真的"\n補充'), row(2, label="pos")]
    for r in rows:
        append_post_gold(path, r)
    assert load_post_gold(path) == rows
    assert path.read_text().count(",".join(POST_COLUMNS)) == 1


def test_bad_label_rejected_on_read_and_write(tmp_path):
    path = tmp_path / "gold.csv"
    with pytest.raises(ValueError, match="bad label"):
        append_post_gold(path, row(1, label="maybe"))
    assert not path.exists()
    path.write_text(",".join(POST_COLUMNS) + "\n1,threads,u,a,t,maybe,h,now,\n")
    with pytest.raises(ValueError, match="bad label"):
        load_post_gold(path)


def test_pending_uses_permalink_target_and_drops_media_before_shuffle():
    posts = [post(i) for i in range(1, 11)]
    posts[0]["id"] = 999  # re-fetch changed its database id
    gold = [row(1), row(2, target="別的")]
    todo = pending_posts(posts, gold, TARGET, seed=7)
    assert {p["id"] for p in todo} == set(range(2, 11))
    assert todo != posts[1:]
    media = [post(11, text=""), post(12, text=" \n\t"), post(13, text=None)]
    assert pending_posts(posts + media, gold, TARGET, seed=7) == todo


def test_session_saves_before_next_key_and_quit_keeps_rows(tmp_path):
    path = tmp_path / "gold.csv"
    keys = iter([" N ", "e", "q"])
    calls = 0

    def read(prompt):
        nonlocal calls
        if calls == 1:
            assert load_post_gold(path) == [row(1, label="neg")]
        if calls == 2:
            assert load_post_gold(path) == [row(1, label="neg"), row(2)]
        calls += 1
        return next(keys)

    counts = post_label_session(
        [post(1), post(2), post(3)],
        target=TARGET,
        annotator="human",
        gold_path=path,
        read=read,
        write=lambda _: None,
        now=lambda: NOW,
    )
    assert counts == {"neg": 1, "neu": 1, "pos": 0, "skipped": 0}
    assert [
        p["id"] for p in pending_posts([post(1), post(2), post(3)], load_post_gold(path), TARGET)
    ] == [3]


def test_permalink_unknown_key_skip_and_missing_time(tmp_path):
    path = tmp_path / "gold.csv"
    out = []
    keys = iter(["o", "?", "s"])
    counts = post_label_session(
        [post(1, posted_at=None)],
        target=TARGET,
        annotator="human",
        gold_path=path,
        read=lambda _: next(keys),
        write=out.append,
    )
    assert post(1)["post_url"] in out
    assert "[1/1] @author · (no time)" in out
    assert any("? n=" in line for line in out)
    assert counts == {"neg": 0, "neu": 0, "pos": 0, "skipped": 1}
    assert not path.exists()


def test_session_is_blind_and_formats_taipei_time(tmp_path):
    hidden = {
        name: f"SECRET_{name}"
        for name in (
            "stance_label",
            "stance_score",
            "stance_model",
            "prompt_version",
            "aspect_label",
            "text_seg",
        )
    }

    def run(extra):
        out = []
        post_label_session(
            [post(1, **extra)],
            target=TARGET,
            annotator="human",
            gold_path=tmp_path / "gold.csv",
            read=lambda _: "q",
            write=out.append,
        )
        return out

    output = run(hidden)
    assert all(value not in "\n".join(output) for value in hidden.values())
    # Label names appear in the required summary, but model values never affect output.
    assert run({"stance_label": "pos", "stance_model": "x"}) == run({})
    assert "[1/1] @author · 2026-09-14 04:00" in output
    assert post(1)["text"] in output
    assert post(1)["post_url"] not in output


def test_positive_label_and_limit(tmp_path):
    path = tmp_path / "gold.csv"
    counts = post_label_session(
        [post(1), post(2)],
        target=TARGET,
        annotator="human",
        gold_path=path,
        limit=1,
        read=lambda _: "p",
        write=lambda _: None,
        now=lambda: NOW,
    )
    assert counts == {"neg": 0, "neu": 0, "pos": 1, "skipped": 0}
    assert load_post_gold(path) == [row(1, label="pos")]


def setup_cli(monkeypatch, tmp_path, posts):
    path = tmp_path / "eval" / "post_stance_gold.csv"
    monkeypatch.setattr(label_posts, "POST_GOLD_PATH", path)
    conn = object()
    monkeypatch.setattr(label_posts.db, "connect", lambda: nullcontext(conn))

    def find(actual_conn, keyword, platform, since, until):
        assert actual_conn is conn
        assert keyword == TARGET and platform == "threads"
        assert since == datetime(2023, 7, 6, tzinfo=UTC)
        assert until.tzinfo is UTC and until > since
        return posts

    monkeypatch.setattr(label_posts.db, "find_social_posts", find)
    return path


def test_cli_empty_returns_hint_without_file(monkeypatch, tmp_path, capsys):
    path = setup_cli(monkeypatch, tmp_path, [])
    assert label_posts.main(["--keyword", TARGET]) == 1
    assert capsys.readouterr().out.strip() == (
        f"no threads posts match {TARGET!r}; run: make social KEYWORD={TARGET}"
    )
    assert not path.exists()


def test_cli_rows_runs_session_and_resumes(monkeypatch, tmp_path, capsys):
    posts = [post(1), post(2), post(3, text=" ")]
    path = setup_cli(monkeypatch, tmp_path, posts)
    append_post_gold(path, row(1))
    sessions = []

    def session(todo, **kwargs):
        sessions.append(todo)
        assert kwargs == {"target": TARGET, "annotator": "human", "gold_path": path, "limit": 30}
        return post_label_session(todo, **kwargs, read=lambda _: "p", write=lambda _: None)

    monkeypatch.setattr(label_posts, "post_label_session", session)
    argv = ["--keyword", TARGET, "--n", "30", "--annotator", "human", "--seed", "1"]
    assert label_posts.main(argv) == 0
    assert sessions == [[post(2)]]
    assert (
        "3 posts, 1 already labeled, 1 pending -> eval/post_stance_gold.csv (dropped 1 media-only)"
        in capsys.readouterr().out
    )
    assert [r.label for r in load_post_gold(path)] == ["neu", "pos"]
    assert label_posts.main(argv) == 0
    assert len(sessions) == 1


def test_cli_media_only_creates_no_file(monkeypatch, tmp_path):
    path = setup_cli(monkeypatch, tmp_path, [post(1, text=None)])
    assert label_posts.main(["--keyword", TARGET]) == 0
    assert not path.exists()

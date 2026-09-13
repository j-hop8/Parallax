"""The gold-set file and the blind labeling loop.

An afternoon of hand labels is the most expensive artifact in this project.
These pin that the loop writes every label immediately, resumes without
re-asking, shuffles across outlets, and never shows the annotator a verdict.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from parallax.nlp.gold import COLUMNS, GoldRow, append_gold, label_session, load_gold, pending

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _art(i, outlet="udn"):
    return {
        "id": i,
        "outlet": outlet,
        "title": f"headline {i}",
        "url_original": f"https://example.com/{i}",
        "body": f"lede {i}\nsecond {i}\nthird {i}",
    }


def _row(i, target="沈伯洋", label="neu"):
    return GoldRow(i, "udn", f"https://example.com/{i}", target, label, "t", NOW.isoformat())


def test_append_then_load_round_trips_and_writes_the_header_once(tmp_path):
    path = tmp_path / "gold.csv"
    append_gold(path, _row(1, label="neg"))
    append_gold(path, _row(2, label="pos"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(COLUMNS)
    assert len(lines) == 3
    rows = load_gold(path)
    assert [(r.article_id, r.label) for r in rows] == [(1, "neg"), (2, "pos")]


def test_load_rejects_a_bad_label_rather_than_scoring_garbage(tmp_path):
    path = tmp_path / "gold.csv"
    path.write_text(",".join(COLUMNS) + "\n1,udn,u,t,maybe,a,now,\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_gold(path)


def test_pending_skips_labeled_pairs_and_shuffles_across_outlets():
    arts = [_art(i, outlet="udn") for i in range(1, 6)] + [
        _art(i, outlet="ltn") for i in range(6, 11)
    ]
    gold = [_row(1), _row(6), _row(2, target="別的")]  # 2 is labeled for a different target
    todo = pending(arts, gold, "沈伯洋", seed=7)
    ids = [a["id"] for a in todo]
    assert 1 not in ids and 6 not in ids and 2 in ids
    assert len(ids) == 8
    outlets = [a["outlet"] for a in todo]
    assert outlets != ["udn"] * 4 + ["ltn"] * 4, "must not present one outlet's run in sequence"
    assert pending(arts, gold, "沈伯洋", seed=7) == todo, "seed makes a session reproducible"


def test_session_writes_each_label_immediately_and_resumes(tmp_path):
    path = tmp_path / "gold.csv"
    out: list[str] = []
    keys = iter(["n", "e", "q"])  # label two, then quit mid-session

    counts = label_session(
        [_art(1), _art(2), _art(3)],
        target="沈伯洋",
        annotator="t",
        gold_path=path,
        read=lambda _: next(keys),
        write=out.append,
        now=lambda: NOW,
    )
    assert counts == {"neg": 1, "neu": 1, "pos": 0, "skipped": 0}
    assert [(r.article_id, r.label) for r in load_gold(path)] == [(1, "neg"), (2, "neu")]

    # Next session only offers what is left.
    todo = pending([_art(1), _art(2), _art(3)], load_gold(path), "沈伯洋")
    assert [a["id"] for a in todo] == [3]


def test_session_shows_outlet_headline_lede_and_body_on_request_never_a_verdict(tmp_path):
    out: list[str] = []
    keys = iter(["b", "p"])
    label_session(
        [_art(1)],
        target="沈伯洋",
        annotator="t",
        gold_path=tmp_path / "g.csv",
        read=lambda _: next(keys),
        write=out.append,
        now=lambda: NOW,
    )
    text = "\n".join(out)
    assert "udn" in text and "headline 1" in text and "lede 1" in text
    assert "third 1" in text, "b must reveal more body"
    for word in ("gemini", "model", "predicted", "confidence"):
        assert word not in text.lower()


def test_unknown_key_reprompts_and_skip_writes_nothing(tmp_path):
    path = tmp_path / "gold.csv"
    out: list[str] = []
    keys = iter(["x", "s"])
    counts = label_session(
        [_art(1)],
        target="沈伯洋",
        annotator="t",
        gold_path=path,
        read=lambda _: next(keys),
        write=out.append,
    )
    assert counts["skipped"] == 1
    assert not path.exists()
    assert any("?" in line for line in out)

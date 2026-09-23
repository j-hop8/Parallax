"""The gold-set file and the blind labeling loop.

An afternoon of hand labels is the most expensive artifact in this project.
These pin that the loop writes every label immediately, resumes without
re-asking, shuffles across outlets, and never shows the annotator a verdict.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from parallax.nlp.gold import (
    COLUMNS,
    GoldRow,
    append_gold,
    label_session,
    load_gold,
    pending,
    validation_sample,
)

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


# ---- validation sampling (T-020) -------------------------------------------


def _claude(i, label):
    return GoldRow(i, "udn", f"https://example.com/{i}", "沈伯洋", label, "claude-opus-5", "")


def test_pending_default_is_unchanged_by_the_annotator_parameter():
    """Every caller before T-020 passes nothing; one label by anyone retires a row."""
    arts = [_art(i) for i in range(1, 6)]
    gold = [_claude(1, "neg"), _claude(2, "neu")]
    assert sorted(a["id"] for a in pending(arts, gold, "沈伯洋", seed=1)) == [3, 4, 5]
    assert sorted(a["id"] for a in pending(arts, gold, "沈伯洋", seed=1, annotator=None)) == [3, 4, 5]


def test_pending_scoped_to_an_annotator_offers_what_others_labeled():
    """Without this a second annotator can never see a labeled row, so there is
    no overlap and kappa has nothing to compare."""
    arts = [_art(i) for i in range(1, 6)]
    gold = [_claude(1, "neg"), _claude(2, "neu"), replace(_row(3), annotator="jimmy")]
    todo = pending(arts, gold, "沈伯洋", seed=1, annotator="jimmy")
    assert sorted(a["id"] for a in todo) == [1, 2, 4, 5], "only jimmy's own row is retired"


def test_validation_sample_offers_only_rows_someone_else_labeled():
    arts = [_art(i) for i in range(1, 8)]
    gold = [_claude(i, "neu") for i in (1, 2, 3)]
    picked = validation_sample(arts, gold, "沈伯洋", "jimmy", n=10, seed=1)
    assert sorted(a["id"] for a in picked) == [1, 2, 3], "4-7 are unlabeled, not validation work"


def test_validation_sample_skips_what_this_annotator_already_relabeled():
    arts = [_art(i) for i in range(1, 5)]
    gold = [_claude(1, "neu"), _claude(2, "neg")]
    gold.append(GoldRow(1, "udn", "u", "沈伯洋", "neu", "jimmy", ""))
    assert [a["id"] for a in validation_sample(arts, gold, "沈伯洋", "jimmy", n=10, seed=1)] == [2]


def test_validation_sample_is_stratified_proportionally_not_evenly():
    """Kappa is prevalence-sensitive: expected agreement comes from the
    marginals, so an evenly-sampled overlap would estimate kappa for a corpus
    that does not exist. A 4:2:1 pool must stay roughly 4:2:1."""
    arts = [_art(i) for i in range(1, 8)]
    labels = {1: "neu", 2: "neu", 3: "neu", 4: "neu", 5: "neg", 6: "neg", 7: "pos"}
    gold = [_claude(i, lab) for i, lab in labels.items()]
    picked = validation_sample(arts, gold, "沈伯洋", "jimmy", n=4, seed=3)
    dist = Counter(labels[a["id"]] for a in picked)
    assert sum(dist.values()) == 4, "largest-remainder rounding must hit n exactly"
    assert dist["neu"] == 2 and dist["neg"] == 1 and dist["pos"] == 1


def test_validation_sample_handles_a_pool_smaller_than_n_and_an_empty_one():
    arts = [_art(i) for i in range(1, 5)]
    gold = [_claude(1, "neu"), _claude(2, "neg")]
    assert len(validation_sample(arts, gold, "沈伯洋", "jimmy", n=999, seed=1)) == 2
    assert validation_sample(arts, [], "沈伯洋", "jimmy", n=5, seed=1) == []


def test_validation_sample_is_reproducible_under_a_seed():
    arts = [_art(i) for i in range(1, 12)]
    gold = [_claude(i, "neu") for i in range(1, 9)]
    first = [a["id"] for a in validation_sample(arts, gold, "沈伯洋", "jimmy", n=5, seed=7)]
    again = [a["id"] for a in validation_sample(arts, gold, "沈伯洋", "jimmy", n=5, seed=7)]
    assert first == again


def test_a_validation_session_never_shows_the_existing_label(tmp_path):
    """Blindness is the whole point: knowing claude said `neg` is exactly the
    anchor --validate exists to avoid.

    The article row carries every verdict field a real db row would, under
    sentinel values, so a leak shows up as the sentinel rather than as a word
    that also appears in the running tally. The tally legend
    ("labeled 1: neg 0 / neu 0 / pos 1") names all three labels by design and
    is excluded -- it reports this session's own counts, not a stored verdict.
    """
    path = tmp_path / "gold.csv"
    append_gold(path, _claude(1, "neg"))
    article = _art(1) | {
        "stance_label": "neg",
        "stance_score": 0.97,
        "stance_model": "SENTINEL-MODEL",
        "prompt_version": "SENTINEL-PROMPT",
        "evidence": "SENTINEL-EVIDENCE",
    }
    shown: list[str] = []
    label_session(
        [article],
        target="沈伯洋",
        annotator="jimmy",
        gold_path=path,
        read=lambda _: "p",
        write=shown.append,
        now=lambda: NOW,
    )
    presentation = "\n".join(line for line in shown if "labeled " not in line)
    for leak in ("SENTINEL-MODEL", "SENTINEL-PROMPT", "SENTINEL-EVIDENCE", "0.97", "claude"):
        assert leak not in presentation, f"{leak!r} reached the annotator"
    assert "neg" not in presentation, "the stored verdict must not appear"
    # and the second opinion is recorded beside the first, not over it
    rows = load_gold(path)
    assert len(rows) == 2
    assert {r.annotator: r.label for r in rows} == {"claude-opus-5": "neg", "jimmy": "pos"}

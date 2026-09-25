"""Post evaluation: stable URL joins, honest coverage, and opt-in API spending."""

import json
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from parallax import db
from parallax.jobs import eval_posts as ev
from parallax.nlp.gold import PostGoldRow, append_post_gold
from parallax.nlp.stance import DailyQuotaExhausted, StanceResult
from parallax.settings import DATABASE_URL


def gold(i=1, label="neg", **changes):
    row = PostGoldRow(i, "threads", f"https://example.com/{i}", "author", "政策", label, "human", "")
    return replace(row, **changes)


def test_metrics_missing_rows_and_platforms():
    rows = [gold(1), gold(2, "neu"), gold(3, "pos", platform="ptt"), gold(4)]
    report = ev.evaluate(rows, {
        (rows[0].post_url, "政策"): "neg",
        (rows[1].post_url, "政策"): "neg",
        (rows[2].post_url, "政策"): "pos",
    })
    assert report["n"] == 3
    assert report["n_missing"] == 1
    assert report["missing"] == [(rows[3].post_url, "政策")]
    assert report["macro_f1"] == pytest.approx((2 / 3 + 0 + 1) / 3)
    assert report["accuracy"] == pytest.approx(2 / 3)
    assert report["confusion"]["neu"]["neg"] == 1
    assert report["per_platform"] == {
        "ptt": {"n": 1, "accuracy": 1}, "threads": {"n": 2, "accuracy": 0.5},
    }
    assert report["per_target"] == {"政策": 3}
    assert report["human_rows"] == 4  # Includes human labels still awaiting a verdict.
    text = ev.render(report, model="test", prompt_version="post-v1")
    for fragment in ("macro-F1", "class", "confusion (rows = human, cols = model)",
                     "per platform", "missing 1", rows[3].post_url, "target=政策",
                     "human rows: 4 of 100", "n=3 is small"):
        assert fragment in text


@pytest.mark.parametrize("annotator", ["claude-opus", "gemini-2", "gpt-5", "model:local"])
def test_model_gold_warning_and_human_count(annotator):
    report = ev.evaluate([gold(), gold(2, annotator=annotator)], {})
    text = ev.render(report, model="test", prompt_version="post-v1")
    provenance = next(line for line in text.splitlines() if "gold labeled by:" in line)
    assert f"{annotator} (1)" in provenance
    assert "human (1)" in provenance
    assert "model-authored gold" in provenance
    assert report["human_rows"] == 1
    assert report["n_missing"] == 2
    assert report["macro_f1"] == 0


def test_same_permalink_has_separate_verdicts_for_each_target():
    rows = [gold(), gold(target="other", label="pos", post_id=999)]
    report = ev.evaluate(rows, {(rows[0].post_url, "政策"): "neg"})
    assert report["n"] == 1
    assert report["missing"] == [(rows[1].post_url, "other")]
    complete = ev.evaluate(rows, {
        (rows[0].post_url, "政策"): "neg", (rows[1].post_url, "other"): "pos",
    })
    assert complete["n"] == 2 and complete["macro_f1"] == 1


def test_human_gold_and_empty_report():
    report = ev.evaluate([gold()], {})
    assert "model-authored" not in ev.render(report, model="m", prompt_version="p")
    empty = ev.evaluate([], {})
    assert empty["accuracy"] == 0 and empty["human_rows"] == 0
    assert "n=0 is small" in ev.render(empty, model="m", prompt_version="p")


def setup_main(monkeypatch, tmp_path, rows):
    path = tmp_path / "post_stance_gold.csv"
    for row in rows:
        append_post_gold(path, row)
    monkeypatch.setattr(ev, "POST_GOLD_PATH", path)
    monkeypatch.setattr(ev, "RUNS_DIR", tmp_path / "runs")
    conn = Mock()
    monkeypatch.setattr(db, "connect", lambda: nullcontext(conn))
    return conn


def test_cached_only_main_never_constructs_or_calls_classifier(monkeypatch, tmp_path, capsys):
    rows = [gold(), gold(2, "neu"), gold(3, target="other")]
    conn = setup_main(monkeypatch, tmp_path, rows)

    class ForbiddenClassifier:
        def __init__(self, **kwargs):
            raise AssertionError("cached evaluation must not construct an API client")

        def classify(self, inp):
            raise AssertionError("cached evaluation must not call an API")

    monkeypatch.setattr(ev, "GeminiPostStance", ForbiddenClassifier)
    lookup = Mock(return_value=[{
        "post_url": rows[0].post_url, "platform": "threads", "label": "neg",
    }])
    monkeypatch.setattr(db, "post_stance_for_urls", lookup)
    assert ev.main(["--model", "test", "--prompt-version", "old", "--target", "政策"]) == 0
    lookup.assert_called_once_with(conn, {r.post_url for r in rows[:2]}, "政策", "test", "old")
    text = capsys.readouterr().out
    assert "macro-F1" in text and "per platform" in text and rows[1].post_url in text
    report = json.loads(next((tmp_path / "runs").glob("*.json")).read_text())
    assert report["model"] == "test" and report["prompt_version"] == "old"
    assert report["n"] == 1 and report["n_missing"] == 1
    assert report["human_rows"] == 2


def test_missing_gold_exits_without_database_or_api(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ev, "POST_GOLD_PATH", tmp_path / "absent.csv")
    forbidden = Mock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(db, "connect", forbidden)
    monkeypatch.setattr(ev, "GeminiPostStance", forbidden)
    assert ev.main([]) == 1
    assert capsys.readouterr().out.strip() == (
        "no post gold yet; run: make label.posts KEYWORD=<keyword>"
    )
    forbidden.assert_not_called()


def test_classify_isolates_failures_commits_rows_and_stops_on_quota(monkeypatch, tmp_path):
    rows = [gold(i) for i in range(1, 7)]
    conn = setup_main(monkeypatch, tmp_path, rows)
    # Current database ids differ from all ids in the CSV.
    conn.execute.return_value.fetchall.return_value = [
        {"id": 100 + r.post_id, "post_url": r.post_url, "platform": r.platform, "text": "政策"}
        for r in rows
    ]
    cache = {rows[0].post_url: "neg"}
    monkeypatch.setattr(db, "post_stance_for_urls", lambda *args: [
        {"post_url": url, "platform": "threads", "label": label} for url, label in cache.items()
    ])
    classifier = Mock(model="test", prompt_version=ev.POST_PROMPT_VERSION)
    result = StanceResult("neg", 0.9, "政策", "test", ev.POST_PROMPT_VERSION)
    classifier.classify.side_effect = [
        ValueError("bad response"), result, result, DailyQuotaExhausted("test", "daily", "20"),
    ]
    monkeypatch.setattr(ev, "GeminiPostStance", lambda **kwargs: classifier)

    def save(conn, **kwargs):
        if kwargs["post_id"] == 103:
            raise RuntimeError("write failed")
        assert kwargs["post_id"] == 104
        cache[rows[3].post_url] = kwargs["label"]

    monkeypatch.setattr(db, "save_post_stance", save)
    assert ev.main(["--classify", "--model", "test"]) == 0
    assert [c.args[0].post_id for c in classifier.classify.call_args_list] == [102, 103, 104, 105]
    assert conn.commit.call_count == 1
    assert conn.rollback.call_count == 3
    report = json.loads(next((tmp_path / "runs").glob("*.json")).read_text())
    assert report["n"] == 2 and report["n_missing"] == 4


def test_fill_missing_skips_absent_and_empty_posts(monkeypatch):
    conn = Mock()
    rows = [gold(1), gold(2)]
    conn.execute.return_value.fetchall.return_value = [
        {"id": 99, "post_url": rows[1].post_url, "text": "  "},
    ]
    monkeypatch.setattr(db, "post_stance_for_urls", lambda *args: [])
    classifier = Mock(model="test", prompt_version=ev.POST_PROMPT_VERSION)
    assert ev._fill_missing(conn, rows, classifier) == 0
    classifier.classify.assert_not_called()
    conn.commit.assert_not_called()


def test_classify_rejects_wrong_prompt(monkeypatch, tmp_path, capsys):
    setup_main(monkeypatch, tmp_path, [gold()])
    classifier = Mock(prompt_version=ev.POST_PROMPT_VERSION)
    monkeypatch.setattr(ev, "GeminiPostStance", lambda **kwargs: classifier)
    assert ev.main(["--classify", "--prompt-version", "obsolete"]) == 2
    classifier.classify.assert_not_called()
    assert "--classify uses the current prompt" in capsys.readouterr().out


def test_database_url_join_survives_changed_id_and_filters_cache():
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres unavailable: {exc}")
    try:
        url = f"https://example.com/post-eval-{uuid4()}"
        old_id = conn.execute(
            "INSERT INTO social_posts (platform, post_url) VALUES ('threads', %s) RETURNING id",
            (url,),
        ).fetchone()["id"]
        row = gold(old_id, post_url=url)
        conn.execute("DELETE FROM social_posts WHERE id = %s", (old_id,))
        new_id = conn.execute(
            "INSERT INTO social_posts (platform, post_url) VALUES ('threads', %s) RETURNING id",
            (url,),
        ).fetchone()["id"]
        assert new_id != old_id
        for target, model, prompt, label in [
            ("政策", "test", "post-v1", "neg"),
            ("other", "test", "post-v1", "pos"),
            ("政策", "other", "post-v1", "pos"),
            ("政策", "test", "old", "pos"),
        ]:
            db.save_post_stance(conn, post_id=new_id, target=target, model=model,
                                prompt_version=prompt, label=label, confidence=1, evidence="政策立場")
        assert db.post_stance_for_urls(conn, [url], "政策", "test", "post-v1") == [
            {"post_url": url, "platform": "threads", "label": "neg"},
        ]
        assert db.post_stance_for_urls(conn, [], "政策", "test", "post-v1") == []
        assert db.post_stance_for_urls(conn, [url + "missing"], "政策", "test", "post-v1") == []
        report = ev.evaluate([row], ev._predictions(conn, [row], "test", "post-v1"))
        assert report["n"] == 1 and report["macro_f1"] == 1
    finally:
        conn.rollback()
        conn.close()

"""The stance job and the eval job, with the DB and classifier faked.

What these protect: the cache (quota is spent once per article, ever), the
accounting (counts move after the commit, one outcome per article), and the
eval's refusal to spend quota unless asked.
"""

from __future__ import annotations

import contextlib

import pytest

from parallax.jobs import eval_stance as ev
from parallax.jobs import stance as job
from parallax.nlp.gold import GoldRow
from parallax.nlp.stance import StanceInput, StanceResult


class _Conn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Classifier:
    """Scripted verdicts keyed by article id; raises where the script says so."""

    model = "fake-model"
    prompt_version = "v1"

    def __init__(self, verdicts: dict[int, str | Exception], evidence="lede"):
        self.verdicts = verdicts
        self.evidence = evidence
        self.calls: list[StanceInput] = []

    def classify(self, inp: StanceInput) -> StanceResult:
        self.calls.append(inp)
        v = self.verdicts[inp.article_id]
        if isinstance(v, Exception):
            raise v
        return StanceResult(v, 0.9, self.evidence, self.model, self.prompt_version)


def _row(i, outlet="udn"):
    return {
        "id": i,
        "outlet": outlet,
        "title": f"t{i}",
        "url_original": f"u{i}",
        "body": f"lede {i}\nmore",
    }


@pytest.fixture
def fakedb(monkeypatch):
    """A tiny in-memory stand-in for the three db calls the job makes."""
    store: dict[tuple, dict] = {}
    conn = _Conn()

    def get_stance(c, article_id, target, model, pv):
        return store.get((article_id, target, model, pv))

    def save_stance(c, *, article_id, target, model, prompt_version, label, confidence, evidence):
        store[(article_id, target, model, prompt_version)] = {"label": label, "evidence": evidence}

    monkeypatch.setattr(job.db, "get_stance", get_stance)
    monkeypatch.setattr(job.db, "save_stance", save_stance)
    monkeypatch.setattr(job.db, "connect", lambda: contextlib.nullcontext(conn))
    return store, conn


def test_classifies_only_what_is_not_cached_and_counts_after_commit(fakedb, monkeypatch):
    store, conn = fakedb
    rows = [_row(1), _row(2), _row(3)]
    monkeypatch.setattr(job.db, "find_enriched_articles", lambda c, kw, limit: rows)
    store[(2, "沈伯洋", "fake-model", "v1")] = {"label": "neu", "evidence": None}  # already done

    clf = _Classifier({1: "neg", 3: "pos"}, evidence="lede 1")
    stats = job.classify_keyword("沈伯洋", clf)

    assert [c.article_id for c in clf.calls] == [1, 3], "cached article 2 must not be re-classified"
    assert stats == {
        "matched": 3,
        "cached": 1,
        "planned": 2,
        "classified": 2,
        "failed": 0,
        "evidence_verbatim": 1,  # 'lede 1' is in article 1's text, not article 3's
    }
    assert conn.commits == 2
    assert store[(1, "沈伯洋", "fake-model", "v1")]["label"] == "neg"

    # Second run: everything cached, zero calls.
    clf2 = _Classifier({})
    again = job.classify_keyword("沈伯洋", clf2)
    assert clf2.calls == [] and again["cached"] == 3 and again["planned"] == 0


def test_dry_run_counts_the_calls_and_makes_none(fakedb, monkeypatch):
    monkeypatch.setattr(job.db, "find_enriched_articles", lambda c, kw, limit: [_row(1), _row(2)])
    clf = _Classifier({1: "neg", 2: "pos"})
    stats = job.classify_keyword("沈伯洋", clf, dry_run=True)
    assert clf.calls == [] and stats["planned"] == 2 and stats["classified"] == 0


def test_one_failure_is_isolated_and_the_batch_continues(fakedb, monkeypatch):
    store, conn = fakedb
    monkeypatch.setattr(
        job.db, "find_enriched_articles", lambda c, kw, limit: [_row(1), _row(2), _row(3)]
    )
    clf = _Classifier({1: "neg", 2: RuntimeError("bad json"), 3: "pos"})
    stats = job.classify_keyword("沈伯洋", clf)
    assert (stats["classified"], stats["failed"]) == (2, 1)
    assert stats["classified"] + stats["failed"] == stats["planned"]
    assert conn.rollbacks == 1 and conn.commits == 2
    assert (2, "沈伯洋", "fake-model", "v1") not in store


def test_the_classifier_never_sees_the_outlet(fakedb, monkeypatch):
    monkeypatch.setattr(
        job.db, "find_enriched_articles", lambda c, kw, limit: [_row(1, outlet="ltn")]
    )
    clf = _Classifier({1: "neu"})
    job.classify_keyword("沈伯洋", clf)
    from parallax.nlp.stance import build_prompt

    assert "ltn" not in build_prompt(clf.calls[0])


def test_distribution_table_shows_lean_per_outlet():
    rows = [
        {"outlet": "ltn", "neg": 1, "neu": 2, "pos": 5, "n": 8},
        {"outlet": "udn", "neg": 5, "neu": 2, "pos": 1, "n": 8},
    ]
    text = job.distribution_table(rows)
    assert "ltn" in text and "+0.50" in text
    assert "udn" in text and "-0.50" in text
    assert job.distribution_table([]) == "(no verdicts yet)"


# ---- eval ------------------------------------------------------------------


def _gold(i, label, outlet="udn", target="沈伯洋"):
    return GoldRow(i, outlet, f"u{i}", target, label, "t", "2026-09-13")


def test_evaluate_joins_on_article_and_target_and_reports_missing():
    gold = [
        _gold(1, "neg"),
        _gold(2, "neu", outlet="ltn"),
        _gold(3, "pos"),
        _gold(4, "neg", target="別的"),
    ]
    preds = {(1, "沈伯洋"): "neg", (2, "沈伯洋"): "neu", (3, "沈伯洋"): "neg"}
    r = ev.evaluate(gold, preds)
    assert r["n"] == 3 and r["n_missing"] == 1 and r["missing"] == [(4, "別的")]
    # neg: tp1 fp1 fn0 -> F1 2/3 ; neu: perfect -> 1 ; pos: never predicted -> 0
    assert r["macro_f1"] == pytest.approx((2 / 3 + 1 + 0) / 3)
    assert r["per_outlet"] == {"ltn": {"n": 1, "accuracy": 1.0}, "udn": {"n": 2, "accuracy": 0.5}}
    assert r["gold_distribution"] == {"neg": 2, "neu": 1, "pos": 1}


def test_render_shows_target_and_warns_on_small_n():
    r = ev.evaluate([_gold(1, "neg")], {(1, "沈伯洋"): "neg"})
    text = ev.render(r, model="m", prompt_version="v1")
    assert "macro-F1  1.000" in text and "target > 0.75" in text and "✓" in text
    assert "small" in text


def test_eval_main_never_spends_quota_without_classify(monkeypatch, tmp_path, capsys):
    conn = _Conn()
    monkeypatch.setattr(ev.db, "connect", lambda: contextlib.nullcontext(conn))
    monkeypatch.setattr(ev.db, "get_stance", lambda *a: None)  # nothing cached
    monkeypatch.setattr(ev, "load_gold", lambda p: [_gold(1, "neg"), _gold(2, "pos")])
    monkeypatch.setattr(ev, "RUNS_DIR", tmp_path)

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("classifier constructed without --classify")

    monkeypatch.setattr(ev, "GeminiStance", _Boom)

    assert ev.main(["--model", "m"]) == 0
    out = capsys.readouterr().out
    assert "missing 2" in out and "--classify" in out
    assert list(tmp_path.glob("*.json")), "a run file is written even when nothing could be scored"


def test_eval_main_with_classify_fills_missing_then_scores(monkeypatch, tmp_path, capsys):
    conn = _Conn()
    store: dict[tuple, dict] = {}
    monkeypatch.setattr(ev.db, "connect", lambda: contextlib.nullcontext(conn))
    monkeypatch.setattr(ev.db, "get_stance", lambda c, i, t, m, pv: store.get((i, t, m, pv)))
    monkeypatch.setattr(
        ev.db,
        "save_stance",
        lambda c, **k: store.__setitem__(
            (k["article_id"], k["target"], k["model"], k["prompt_version"]), {"label": k["label"]}
        ),
    )
    monkeypatch.setattr(ev.db, "articles_by_ids", lambda c, ids: [_row(i) for i in ids])
    monkeypatch.setattr(ev, "load_gold", lambda p: [_gold(1, "neg"), _gold(2, "pos")])
    monkeypatch.setattr(ev, "RUNS_DIR", tmp_path)

    clf = _Classifier({1: "neg", 2: "neu"})
    monkeypatch.setattr(ev, "GeminiStance", lambda model, rpm: clf)

    assert ev.main(["--model", "fake-model", "--classify"]) == 0
    assert sorted(c.article_id for c in clf.calls) == [1, 2]
    out = capsys.readouterr().out
    assert "n=2" in out and "missing" not in out
    assert conn.commits == 2

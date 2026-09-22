"""The Q4 post classifier job, with the DB and classifier faked.

Same three things the article job's tests protect -- the cache, the accounting,
and per-row isolation -- plus the one rule that is specific to posts: a
media-only post is never sent to the model, because a verdict on an empty
string would be a fabricated data point in the Q4 numerator.
"""

from __future__ import annotations

import contextlib

import pytest

from parallax.jobs import stance_social as job
from parallax.nlp.stance import DailyQuotaExhausted, PostStanceInput, StanceResult


class _Conn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Classifier:
    """Scripted verdicts keyed by post id; raises where the script says so."""

    model = "fake-model"
    prompt_version = "post-v1"

    def __init__(self, verdicts: dict[int, str | Exception], evidence="好棒棒"):
        self.verdicts = verdicts
        self.evidence = evidence
        self.calls: list[PostStanceInput] = []

    def classify(self, inp: PostStanceInput) -> StanceResult:
        self.calls.append(inp)
        v = self.verdicts[inp.post_id]
        if isinstance(v, Exception):
            raise v
        return StanceResult(v, 0.9, self.evidence, self.model, self.prompt_version)


def _post(i, text="真是好棒棒", author="someone"):
    return {
        "id": i,
        "platform": "threads",
        "post_url": f"https://www.threads.net/@a/post/{i}",
        "author": author,
        "text": text,
    }


@pytest.fixture
def fakedb(monkeypatch):
    store: dict[tuple, dict] = {}
    conn = _Conn()

    def get_post_stance(c, post_id, target, model, pv):
        return store.get((post_id, target, model, pv))

    def save_post_stance(c, *, post_id, target, model, prompt_version, label, confidence, evidence):
        store[(post_id, target, model, prompt_version)] = {"label": label, "evidence": evidence}

    monkeypatch.setattr(job.db, "get_post_stance", get_post_stance)
    monkeypatch.setattr(job.db, "save_post_stance", save_post_stance)
    monkeypatch.setattr(job.db, "connect", lambda: contextlib.nullcontext(conn))
    return store, conn


def _posts(monkeypatch, rows):
    monkeypatch.setattr(job.db, "find_social_posts", lambda c, kw, platform, since, until: rows)


def test_cached_posts_cost_no_quota(fakedb, monkeypatch):
    store, conn = fakedb
    _posts(monkeypatch, [_post(1), _post(2), _post(3)])
    store[(2, "沈伯洋", "fake-model", "post-v1")] = {"label": "neu", "evidence": "x"}

    clf = _Classifier({1: "neg", 3: "pos"})
    stats = job.classify_posts("沈伯洋", clf)

    assert [c.post_id for c in clf.calls] == [1, 3], "cached post 2 must not be re-classified"
    assert (stats["matched"], stats["cached"], stats["planned"]) == (3, 1, 2)
    assert stats["classified"] == 2
    assert conn.commits == 2


def test_media_only_posts_are_never_sent_to_the_model(fakedb, monkeypatch):
    _, conn = fakedb
    _posts(monkeypatch, [_post(1), _post(2, text=""), _post(3, text="   \n "), _post(4)])

    clf = _Classifier({1: "neg", 4: "neu"})
    stats = job.classify_posts("沈伯洋", clf)

    assert [c.post_id for c in clf.calls] == [1, 4]
    assert stats["empty"] == 2
    assert stats["matched"] == 4, "they still count as matched; only the model skips them"
    assert stats["planned"] == 2


def test_dry_run_makes_no_calls_and_no_commits(fakedb, monkeypatch):
    _, conn = fakedb
    _posts(monkeypatch, [_post(1), _post(2, text=""), _post(3)])

    clf = _Classifier({})
    stats = job.classify_posts("沈伯洋", clf, dry_run=True)

    assert clf.calls == []
    assert conn.commits == 0
    assert stats["planned"] == 2
    assert stats["classified"] == 0


def test_one_bad_response_does_not_cost_the_batch(fakedb, monkeypatch):
    store, conn = fakedb
    _posts(monkeypatch, [_post(1), _post(2), _post(3)])

    clf = _Classifier({1: "neg", 2: ValueError("no evidence phrase"), 3: "pos"})
    stats = job.classify_posts("沈伯洋", clf)

    assert stats["classified"] == 2
    assert stats["failed"] == 1
    assert conn.rollbacks == 1
    assert (2, "沈伯洋", "fake-model", "post-v1") not in store, "a failure caches nothing"


def test_daily_quota_stops_the_run_without_failing_the_rest(fakedb, monkeypatch):
    store, conn = fakedb
    _posts(monkeypatch, [_post(1), _post(2), _post(3)])

    clf = _Classifier({1: "neg", 2: DailyQuotaExhausted("m", "PerDay", "20"), 3: "pos"})
    stats = job.classify_posts("沈伯洋", clf)

    assert stats["classified"] == 1
    assert stats["failed"] == 0, "a quota stop is not a post failure"
    assert stats["quota_exhausted"] == 1
    assert [c.post_id for c in clf.calls] == [1, 2], "post 3 is left pending, not attempted"


def test_evidence_is_checked_against_the_post_text(fakedb, monkeypatch):
    _posts(monkeypatch, [_post(1, text="真是好棒棒"), _post(2, text="沈伯洋今天開記者會")])

    clf = _Classifier({1: "neg", 2: "neu"}, evidence="好棒棒")
    stats = job.classify_posts("沈伯洋", clf)

    assert stats["classified"] == 2
    assert stats["evidence_verbatim"] == 1, "post 2's phrase is not in post 2"


def test_limit_caps_the_calls_not_the_matches(fakedb, monkeypatch):
    _posts(monkeypatch, [_post(i) for i in (1, 2, 3, 4)])

    clf = _Classifier({1: "neg", 2: "neu"})
    stats = job.classify_posts("沈伯洋", clf, limit=2)

    assert [c.post_id for c in clf.calls] == [1, 2]
    assert stats["matched"] == 4
    assert stats["planned"] == 2


def test_the_target_is_the_keyword_and_reaches_the_prompt_input(fakedb, monkeypatch):
    store, _ = fakedb
    _posts(monkeypatch, [_post(1)])

    clf = _Classifier({1: "neg"})
    job.classify_posts("沈伯洋", clf)

    assert clf.calls[0].target == "沈伯洋"
    assert (1, "沈伯洋", "fake-model", "post-v1") in store

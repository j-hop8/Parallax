"""The framing job and the summary step, with the DB and the model faked.

What these protect: the summary is spent once per member and never on an
origin or an empty delta; a dry run prints what a real run would; the
readout never says "removed" for an indeterminate cluster; the model never
sees an outlet name.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from parallax.jobs import framing as job
from parallax.nlp import summary as sm
from parallax.nlp.summary import (
    ORIGIN_SUMMARY,
    SAME_SUMMARY,
    SUMMARY_VERSION,
    GeminiSummary,
    SummaryInput,
    SummaryResult,
    build_prompt,
    parse_response,
)

T0 = datetime(2026, 8, 23, 2, 0, tzinfo=UTC)
LEDE = "在貿易談判於最後一刻破裂後，美國總統川普對數十種加拿大進口商品徵收的50%關稅於今天正式生效。"
S2 = "美聯社報導，新關稅措施預計將影響加拿大每年輸往美國出口額約5，涉及商品金額約200億美元。"
S3 = "這項關稅無需經過任何調查即可開徵，且無實施期限上限。"
SUBHEAD = "● 受影響的商品有哪些？"


def _member(aid, outlet, rank, body, minutes, **stored):
    m = {
        "id": aid,
        "outlet": outlet,
        "title": f"標題 {aid}",
        "effective_at": T0 + timedelta(minutes=minutes),
        "published_at": T0 + timedelta(minutes=minutes),
        "cluster_rank": rank,
        "body": body,
        "delta_added": None,
        "delta_removed": None,
        "delta_summary": None,
        "delta_summary_model": None,
        "delta_summary_version": None,
    }
    m.update(stored)
    return m


def _clusters():
    confident = {
        "cluster_id": 1,
        "origin_confident": True,
        "shared_core_text": None,
        "members": [
            _member(1, "cna", 1, f"{LEDE}\n{S2}\n{S3}", 0),
            _member(2, "udn", 2, f"{LEDE}\n{S2}\n{S3}", 30),
            _member(3, "setn", 3, f"{LEDE}\n{SUBHEAD}\n{S2}", 60),
        ],
    }
    indeterminate = {
        "cluster_id": 7,
        "origin_confident": False,
        "shared_core_text": None,
        "members": [
            _member(7, "udn", 1, f"{LEDE}\n{S2}", 0),
            _member(8, "udn", 2, f"{LEDE}\n{S2}\n{S3}", 1),
        ],
    }
    return [confident, indeterminate]


def test_compute_gives_the_design_deltas_and_the_indeterminate_reason():
    computed = job.compute(_clusters(), {})
    conf, indet = computed
    assert conf.framing.core == (LEDE, S2, S3)  # S3 is in 2 of 3: still core
    assert conf.reason == ""
    d1, d2, d3 = conf.framing.deltas
    assert d1.empty and d2.empty
    assert d3.added == (SUBHEAD,) and d3.removed == (S3,)
    assert indet.reason.startswith("gap 60s inside")
    assert [d.removed for d in indet.framing.deltas] == [(), ()]
    assert indet.framing.deltas[1].added == (S3,)


def test_effective_summaries_use_rules_then_stored_then_pending():
    clusters = _clusters()
    # setn already summarised at this version with the same deltas: kept.
    clusters[0]["members"][2].update(
        delta_added=[SUBHEAD],
        delta_removed=[S3],
        delta_summary="＋ 加入小標；－ 刪除法律背景句。",
        delta_summary_model="m",
        delta_summary_version=SUMMARY_VERSION,
    )
    # member 8 summarised at an older prompt version: pending again.
    clusters[1]["members"][1].update(
        delta_added=[S3], delta_removed=[], delta_summary="舊", delta_summary_version="v0"
    )
    computed = job.compute(clusters, {})
    got = job.effective_summaries(computed, SUMMARY_VERSION)
    assert got == {
        1: ORIGIN_SUMMARY,
        2: SAME_SUMMARY,
        3: "＋ 加入小標；－ 刪除法律背景句。",
        7: SAME_SUMMARY,
        8: None,
    }
    inputs = job.summary_inputs(computed, {8})
    assert [i.article_id for i in inputs] == [8]
    assert inputs[0].directional is False and inputs[0].reference_headline == ""


class _Conn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Summarizer:
    model = "fake"
    version = SUMMARY_VERSION

    def __init__(self, script):
        self.script = dict(script)
        self.calls: list[SummaryInput] = []

    def summarize(self, inp):
        self.calls.append(inp)
        v = self.script[inp.article_id]
        if isinstance(v, Exception):
            raise v
        return SummaryResult(v, self.model, self.version)


@pytest.fixture
def fakedb(monkeypatch):
    conn = _Conn()
    saved: list[tuple] = []
    monkeypatch.setattr(
        job.db, "save_summary", lambda c, aid, s, m, v: saved.append((aid, s, m, v))
    )
    return conn, saved


def _inp(aid, **over):
    base = {
        "article_id": aid,
        "directional": True,
        "reference_headline": "美對加開徵50%關稅生效　4大重點一次看",
        "headline": "美國對加拿大「開徵50%關稅」生效！4大重點一次看",
        "added": (SUBHEAD,),
        "removed": (),
    }
    base.update(over)
    return SummaryInput(**base)


def test_summarize_commits_per_member_and_isolates_failures(fakedb):
    conn, saved = fakedb
    s = _Summarizer({3: "＋ 加入小標。", 8: RuntimeError("boom"), 9: "本版獨有：法律背景句。"})
    stats = job.summarize(
        [_inp(3), _inp(8), _inp(9)], s, connect=lambda: contextlib.nullcontext(conn)
    )
    assert stats == {"planned": 3, "summarized": 2, "failed": 1, "quota_exhausted": 0}
    assert [t[0] for t in saved] == [3, 9] and conn.commits == 2 and conn.rollbacks == 1
    assert saved[0] == (3, "＋ 加入小標。", "fake", SUMMARY_VERSION)


def test_daily_quota_stops_the_run_and_leaves_the_rest_pending(fakedb):
    conn, saved = fakedb
    s = _Summarizer({3: sm.DailyQuotaExhausted("m", "PerDay", "20"), 9: "never"})
    stats = job.summarize([_inp(3), _inp(9)], s, connect=lambda: contextlib.nullcontext(conn))
    assert stats["quota_exhausted"] == 1 and stats["summarized"] == 0
    assert saved == [] and len(s.calls) == 1


# ---- prompt and parse ------------------------------------------------------


def test_prompt_shows_headlines_and_lists_and_never_the_outlet():
    p = build_prompt(_inp(3, removed=(S3,)))
    assert "ORIGINAL HEADLINE:\n美對加開徵50%關稅生效" in p
    assert "ADDED:\n- ● 受影響的商品有哪些？" in p
    assert "CAPTIONS: (none)" in p
    assert "REMOVED:\n- 這項關稅無需" in p
    assert "REWROTE: (none)" in p
    for outlet in ("cna", "setn", "中央社", "三立"):
        assert outlet not in p


def test_a_trimmed_lede_under_the_photo_is_listed_as_a_caption_not_an_addition():
    caption = "美國總統川普對數十種加拿大進口商品徵收的50%關稅於今天正式生效。"
    assert sm.caption_like((caption, SUBHEAD), (LEDE, S2)) == (caption,)
    p = build_prompt(_inp(3, added=(caption, SUBHEAD), captions=(caption,)))
    assert f"CAPTIONS:\n- {caption}" in p and f"ADDED:\n- {SUBHEAD}" in p


def test_summary_inputs_carry_captions_from_the_core():
    clusters = _clusters()
    caption = LEDE[10:]  # a trimmed lede
    clusters[0]["members"][2]["body"] = f"{caption}\n{LEDE}\n{SUBHEAD}\n{S2}"
    computed = job.compute(clusters, {})
    (inp,) = job.summary_inputs(computed, {3})
    assert inp.captions == (caption,) and SUBHEAD in inp.added


def test_prompt_for_an_indeterminate_cluster_says_order_unknown_and_has_no_original():
    p = build_prompt(_inp(8, directional=False, reference_headline=""))
    assert p.startswith("Order between the versions is UNKNOWN")
    assert "ORIGINAL HEADLINE" not in p


def test_prompt_folds_a_rewording_and_caps_long_lists():
    old = "賴清德總統昨天宣布「AI紅利、全民共享」，明年普發一萬元。"
    new = "總統賴清德昨（17）日宣布「AI紅利，全民共享」，明年普發1萬元。"
    filler = tuple(f"第{i}句完全不同的新增內容，長度足夠。" for i in range(20))
    p = build_prompt(_inp(3, added=(new,) + filler, removed=(old,)))
    assert f"REWROTE:\n- {old} → {new}" in p
    assert "… and 8 more" in p  # 20 filler beyond MAX_ITEMS = 12


def test_parse_response_normalises_and_rejects_empty():
    assert (
        parse_response('{"summary": " ＋ 加入小標；\\n－ 刪除但書句。 "}')
        == "＋ 加入小標； － 刪除但書句。"
    )
    with pytest.raises(ValueError):
        parse_response('{"summary": ""}')
    with pytest.raises(ValueError):
        parse_response("not json")


class _Fake:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item)


def test_gemini_backend_stamps_model_and_version_and_uses_the_summary_schema():
    fake = _Fake(['{"summary": "＋ 加入四個小標。"}'])
    s = GeminiSummary(model="m", rpm=0, client=fake, sleep=lambda _: None)
    r = s.summarize(_inp(3))
    assert r == SummaryResult("＋ 加入四個小標。", "m", SUMMARY_VERSION)
    cfg = fake.calls[0]["config"]
    assert cfg["system_instruction"] == sm.SYSTEM_INSTRUCTION
    assert cfg["response_json_schema"] == sm.RESPONSE_SCHEMA


def test_importing_summary_does_not_require_the_llm_extra():
    code = "import sys, parallax.nlp.summary; assert 'google.genai' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


# ---- readout ---------------------------------------------------------------


def test_render_prints_the_design_block_and_never_removed_for_indeterminate():
    computed = job.compute(_clusters(), {})
    out = job.render(computed, job.effective_summaries(computed, SUMMARY_VERSION))
    conf, indet = out.split("\n\n")
    assert conf.startswith("cluster 1  3 members  origin: cna (08-23 10:00)")
    assert "  core: 3 sentence(s)  " + LEDE in conf
    assert f"#1 08-23 10:00  cna                  {ORIGIN_SUMMARY}" in conf
    assert f"#2 08-23 10:30  udn            +30m  {SAME_SUMMARY}" in conf
    assert "#3 08-23 11:00  setn         +1h00m  (no summary yet)" in conf
    assert "        ＋ ● 受影響的商品有哪些？" in conf
    assert f"        － {S3}" in conf

    assert indet.startswith("cluster 7  2 members  order indeterminate: gap 60s")
    assert "本版獨有 " + S3 in indet
    assert "－" not in indet and "＋" not in indet


def test_render_narrows_to_a_keyword_scope():
    computed = job.compute(_clusters(), {})
    out = job.render(computed, {}, only={8})
    assert out.startswith("cluster 7") and "cluster 1" not in out
    assert job.render(computed, {}, only={999}) == "(no clusters)"

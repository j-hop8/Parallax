"""Stance classifier contract and Gemini backend, offline.

Nothing here touches the network: the client is a fake. What is pinned is
what the model is asked (the prompt), what we accept back (the parser), and
how the backend behaves under the free tier's rate limits.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from parallax.nlp import stance as mod
from parallax.nlp.stance import (
    LABELS,
    PROMPT_VERSION,
    GeminiStance,
    StanceInput,
    body_excerpt,
    build_prompt,
    lede,
    parse_response,
    stance_input,
)

BODY = "\n".join(
    [
        "第一段：沈伯洋今天在立法院表示，相關指控毫無根據。",
        "",
        "第二段：國民黨團則批評他迴避問題。",
        "第三段：這是第三段的內容。",
        "第四段：" + "很長的內容" * 300,
    ]
)


def _inp(**over) -> StanceInput:
    base = {
        "article_id": 1,
        "outlet": "udn",
        "target": "沈伯洋",
        "headline": "沈伯洋遭質疑 回應：毫無根據",
        "lede": lede(BODY),
        "body_excerpt": body_excerpt(BODY),
    }
    base.update(over)
    return StanceInput(**base)


# ---- text preparation ------------------------------------------------------


def test_lede_is_the_first_two_non_empty_paragraphs():
    assert (
        lede(BODY)
        == "第一段：沈伯洋今天在立法院表示，相關指控毫無根據。\n第二段：國民黨團則批評他迴避問題。"
    )
    assert lede("") == "" and lede(None) == ""


def test_body_excerpt_is_capped_so_cost_cannot_drift_with_article_length():
    excerpt = body_excerpt(BODY, max_chars=100)
    assert len(excerpt) <= 101 and excerpt.endswith("…")
    assert body_excerpt("short") == "short"


def test_stance_input_from_a_db_row():
    row = {"id": 7, "outlet": "ltn", "title": "  標題  ", "body": BODY}
    inp = stance_input(row, "沈伯洋")
    assert inp.article_id == 7 and inp.outlet == "ltn" and inp.headline == "標題"
    assert inp.lede.startswith("第一段") and inp.body_excerpt


# ---- the prompt ------------------------------------------------------------


def test_prompt_carries_target_headline_lede_and_never_the_outlet():
    """The model must judge the text blind. Knowing the outlet lets it lean on
    priors about that outlet's politics -- the thing we are trying to measure."""
    inp = _inp()
    prompt = build_prompt(inp)
    assert inp.target in prompt
    assert inp.headline in prompt
    assert "第二段" in prompt
    assert "udn" not in prompt and "聯合" not in prompt


def test_system_instruction_states_the_weighting_and_the_three_labels():
    text = mod.SYSTEM_INSTRUCTION
    assert "HEADLINE" in text and "LEDE" in text and "most weight" in text
    for label in LABELS:
        assert f"- {label}:" in text
    assert "verbatim" in text


# ---- the parser ------------------------------------------------------------


def test_parse_accepts_the_schema_and_clamps_confidence():
    assert parse_response('{"label":"neg","confidence":0.9,"evidence":"遭質疑"}') == (
        "neg",
        0.9,
        "遭質疑",
    )
    assert parse_response('{"label":"pos","confidence":1.7,"evidence":""}')[1] == 1.0
    assert parse_response('{"label":"neu","confidence":-2,"evidence":"x"}')[1] == 0.0


@pytest.mark.parametrize(
    "bad",
    [
        '{"label":"mixed","confidence":0.5,"evidence":"x"}',
        '{"label":"neg","confidence":"high","evidence":"x"}',
        "not json at all",
    ],
)
def test_parse_rejects_off_schema_output_instead_of_guessing(bad):
    with pytest.raises(ValueError):
        parse_response(bad)


# ---- Gemini backend --------------------------------------------------------


class _Fake:
    """Stands in for google.genai.Client: scripted responses or exceptions."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.models = self  # so fake.models.generate_content works

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item)


class _RateLimited(Exception):
    def __init__(self, retry_delay=None):
        super().__init__("429 RESOURCE_EXHAUSTED")
        self.code = 429
        self.details = (
            {"error": {"details": [{"retryDelay": retry_delay}]}} if retry_delay else None
        )


def test_backend_returns_a_result_stamped_with_model_and_prompt_version():
    fake = _Fake(['{"label":"neg","confidence":0.8,"evidence":"遭質疑"}'])
    clf = GeminiStance(model="fake-model", rpm=0, client=fake)
    result = clf.classify(_inp())

    assert (result.label, result.confidence, result.evidence) == ("neg", 0.8, "遭質疑")
    assert result.model == "fake-model" and result.prompt_version == PROMPT_VERSION
    call = fake.calls[0]
    assert call["model"] == "fake-model"
    assert call["config"]["response_mime_type"] == "application/json"
    assert call["config"]["response_json_schema"]["properties"]["label"]["enum"] == list(LABELS)
    assert call["config"]["temperature"] == 0.0


def test_429_sleeps_the_servers_hint_then_retries():
    slept: list[float] = []
    fake = _Fake([_RateLimited("7s"), '{"label":"neu","confidence":0.6,"evidence":"x"}'])
    clf = GeminiStance(model="m", rpm=0, client=fake, sleep=slept.append)

    assert clf.classify(_inp()).label == "neu"
    assert len(fake.calls) == 2
    assert slept == [8.0], "should sleep retryDelay + 1s, once"


def test_429_without_a_hint_backs_off_exponentially_and_eventually_gives_up():
    slept: list[float] = []
    fake = _Fake([_RateLimited(), _RateLimited(), _RateLimited(), _RateLimited()])
    clf = GeminiStance(model="m", rpm=0, client=fake, sleep=slept.append, max_attempts=3)

    with pytest.raises(_RateLimited):
        clf.classify(_inp())
    assert len(fake.calls) == 3, "max_attempts bounds the calls"
    assert slept == [10.0, 20.0], "two retries, doubling"


def test_non_rate_limit_errors_propagate_without_retry():
    fake = _Fake([RuntimeError("boom")])
    clf = GeminiStance(model="m", rpm=0, client=fake, sleep=lambda s: None)
    with pytest.raises(RuntimeError):
        clf.classify(_inp())
    assert len(fake.calls) == 1


def test_pacer_holds_the_configured_rate():
    slept: list[float] = []
    fake = _Fake(['{"label":"neu","confidence":0.5,"evidence":"a"}'] * 2)
    clf = GeminiStance(model="m", rpm=60, client=fake, sleep=slept.append)  # 1 req/s
    clf.classify(_inp())
    clf.classify(_inp(article_id=2))
    assert len(slept) == 1 and 0.9 < slept[0] <= 1.0


def test_importing_the_module_does_not_require_the_llm_extra():
    """The crawler installs without google-genai; this module must import anyway."""
    code = "import sys, parallax.nlp.stance; assert 'google.genai' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)

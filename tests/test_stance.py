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
    assert parse_response('{"label":"pos","confidence":1.7,"evidence":"y"}')[1] == 1.0
    assert parse_response('{"label":"neu","confidence":-2,"evidence":"x"}')[1] == 0.0


@pytest.mark.parametrize(
    "bad",
    [
        '{"label":"mixed","confidence":0.5,"evidence":"x"}',
        '{"label":"neg","confidence":"high","evidence":"x"}',
        '{"label":"neg","confidence":0.9,"evidence":""}',
        '{"label":"neg","confidence":0.9,"evidence":"   "}',
        '{"label":"neg","confidence":0.9}',
        "not json at all",
    ],
)
def test_parse_rejects_off_schema_output_instead_of_guessing(bad):
    """Includes empty evidence: a verdict nobody can audit must not be cached."""
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


def test_503_is_retried_like_a_rate_limit():
    """The first live run met '503 model is experiencing high demand'."""

    class _Unavailable(Exception):
        code = 503
        details = None

    slept: list[float] = []
    fake = _Fake([_Unavailable(), '{"label":"pos","confidence":0.7,"evidence":"x"}'])
    clf = GeminiStance(model="m", rpm=0, client=fake, sleep=slept.append)
    assert clf.classify(_inp()).label == "pos"
    assert len(fake.calls) == 2 and slept == [10.0]


def test_daily_quota_429_raises_immediately_without_retry():
    """Per-day quota: waiting a minute does not help. Learned live at 20/day."""
    from parallax.nlp.stance import DailyQuotaExhausted

    class _Daily(Exception):
        code = 429

        def __init__(self):
            super().__init__("429")
            self.details = {
                "error": {
                    "details": [
                        {
                            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                            "quotaValue": "20",
                        },
                        {"retryDelay": "59s"},
                    ]
                }
            }

    slept: list[float] = []
    fake = _Fake([_Daily()])
    clf = GeminiStance(model="m", rpm=0, client=fake, sleep=slept.append)
    with pytest.raises(DailyQuotaExhausted) as excinfo:
        clf.classify(_inp())
    assert slept == [] and len(fake.calls) == 1
    assert excinfo.value.quota_value == "20" and "m" in str(excinfo.value)


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


# ---- posts (T-016) ---------------------------------------------------------


def _post_row(**over):
    row = {
        "id": 7,
        "platform": "threads",
        "author": "some_handle",
        "text": "真是好棒棒，沈伯洋又上電視了",
    }
    row.update(over)
    return row


def test_post_stance_input_from_a_db_row():
    inp = mod.post_stance_input(_post_row(), "沈伯洋")
    assert (inp.post_id, inp.platform, inp.target) == (7, "threads", "沈伯洋")
    assert inp.author == "some_handle"
    assert inp.text == "真是好棒棒，沈伯洋又上電視了"


def test_post_text_is_capped_so_one_essay_cannot_drift_the_cost():
    inp = mod.post_stance_input(_post_row(text="字" * 5000), "沈伯洋")
    assert len(inp.text) == mod.POST_TEXT_CHARS + 1  # the ellipsis
    assert inp.text.endswith("…")


def test_post_prompt_carries_target_and_text_and_never_the_author():
    """Same reason the article prompt withholds the outlet: a model that
    recognises the account can grade the account instead of the post."""
    prompt = mod.build_post_prompt(mod.post_stance_input(_post_row(), "沈伯洋"))
    assert "TARGET: 沈伯洋" in prompt
    assert "真是好棒棒" in prompt
    assert "some_handle" not in prompt
    assert "threads" not in prompt.lower()


def test_post_prompt_says_none_rather_than_sending_an_empty_section():
    assert "(none)" in mod.build_post_prompt(mod.post_stance_input(_post_row(text=""), "沈伯洋"))


def test_post_system_instruction_names_sarcasm_and_the_three_labels():
    """Sarcasm is the one thing the article prompt never has to handle and the
    post prompt always does; the annotation guide names it, so must this."""
    text = mod.POST_SYSTEM_INSTRUCTION
    for label in LABELS:
        assert f"- {label}:" in text
    assert "Sarcasm" in text
    assert "JSON only" in text
    # The post is all there is: no quoted post, no replies, no images.
    assert "quoted post" in text and "replies" in text


def test_post_and_article_prompt_versions_never_collide():
    """Verdicts from the two prompts sit in different tables but are filtered by
    version; equal versions would make an article verdict look like a post one."""
    assert mod.POST_PROMPT_VERSION != PROMPT_VERSION
    assert mod.POST_PROMPT_VERSION.startswith("post-")


def test_post_backend_stamps_the_post_prompt_version():
    fake = _Fake(['{"label":"neg","confidence":0.8,"evidence":"好棒棒"}'])
    clf = mod.GeminiPostStance(model="fake-model", rpm=0, client=fake)
    result = clf.classify(mod.post_stance_input(_post_row(), "沈伯洋"))

    assert (result.label, result.evidence) == ("neg", "好棒棒")
    assert result.model == "fake-model"
    assert result.prompt_version == mod.POST_PROMPT_VERSION
    assert clf.prompt_version == mod.POST_PROMPT_VERSION
    # Same JSON contract as articles -- one parser, one schema, two prompts.
    assert fake.calls[0]["config"]["response_json_schema"]["properties"]["label"]["enum"] == list(
        LABELS
    )


def test_post_backend_rejects_a_label_with_no_evidence_phrase():
    """Unauditable on a post as much as on an article: nothing is cached, so
    the post is simply retried next run."""
    fake = _Fake(['{"label":"neg","confidence":0.8,"evidence":"  "}'])
    clf = mod.GeminiPostStance(model="m", rpm=0, client=fake)
    with pytest.raises(ValueError, match="evidence"):
        clf.classify(mod.post_stance_input(_post_row(), "沈伯洋"))

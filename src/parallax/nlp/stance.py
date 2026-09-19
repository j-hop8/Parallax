"""Q1: an article's stance toward a target, judged from headline, lede and body.

Target-dependent on purpose. News is written in neutral register, so asking
"is this article positive or negative?" returns "neutral" for almost everything
and measures nothing. Asking "how does this article position 沈伯洋?" is
answerable, and the headline and lede carry most of the answer -- reporters
put the framing where readers stop reading.

The classifier is a protocol. The first backend is Gemini on the free tier;
a fine-tuned local model (T-007b) replaces it behind the same interface once
the approach has cleared the gold-set bar. The prompt is versioned: changing a
word changes what the model was asked, and old verdicts must stay comparable
rather than silently mixing with new ones.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..settings import STANCE_MODEL, STANCE_RPM
from .gemini import DailyQuotaExhausted, GeminiJSON, Pacer

__all__ = [
    "LABELS",
    "PROMPT_VERSION",
    "DailyQuotaExhausted",  # re-exported: raised out of classify(), callers catch it here
    "GeminiStance",
    "Pacer",
    "StanceClassifier",
    "StanceInput",
    "StanceResult",
    "body_excerpt",
    "build_prompt",
    "lede",
    "parse_response",
    "stance_input",
]

log = logging.getLogger(__name__)

LABELS: tuple[str, ...] = ("neg", "neu", "pos")

# Bump whenever SYSTEM_INSTRUCTION or build_prompt changes in a way that could
# move a verdict. Stored on every row; the eval reports per version.
PROMPT_VERSION = "v1"

LEDE_PARAGRAPHS = 2
BODY_EXCERPT_CHARS = 1200


@dataclass(frozen=True)
class StanceInput:
    article_id: int
    outlet: str  # bookkeeping only -- deliberately NOT shown to the model
    target: str
    headline: str
    lede: str
    body_excerpt: str


@dataclass(frozen=True)
class StanceResult:
    label: str
    confidence: float
    evidence: str
    model: str
    prompt_version: str


class StanceClassifier(Protocol):
    model: str
    prompt_version: str

    def classify(self, inp: StanceInput) -> StanceResult: ...


# ---- text preparation ------------------------------------------------------


def lede(body: str | None, n: int = LEDE_PARAGRAPHS) -> str:
    """The first n non-empty paragraphs. extract_body joins paragraphs with \\n."""
    if not body:
        return ""
    paragraphs = [p.strip() for p in body.split("\n") if p.strip()]
    return "\n".join(paragraphs[:n])


def body_excerpt(body: str | None, max_chars: int = BODY_EXCERPT_CHARS) -> str:
    """Bounded context after the lede, so cost and prompt size cannot drift with article length."""
    if not body:
        return ""
    text = body.strip()
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"


def stance_input(row: dict, target: str) -> StanceInput:
    """Build the classifier input from a db.find_enriched_articles row."""
    body = row.get("body") or ""
    return StanceInput(
        article_id=row["id"],
        outlet=row["outlet"],
        target=target,
        headline=(row.get("title") or "").strip(),
        lede=lede(body),
        body_excerpt=body_excerpt(body),
    )


# ---- the prompt ------------------------------------------------------------
#
# eval/README.md gives human annotators these same rules in the same words.
# Human and model must be scored against one definition of the task, or the
# F1 measures disagreement about what "stance" means instead of the classifier.

SYSTEM_INSTRUCTION = """\
You label the STANCE of one Taiwanese news article toward a named TARGET.

You are judging how the ARTICLE positions the target -- through what it chose
to report, how it frames it, whose voice gets the headline, and its word
choice. You are NOT judging whether the events are good or bad news, and NOT
the general sentiment of the prose.

Weighting: the HEADLINE and LEDE carry the most weight. The body excerpt is
context; it does not override a clearly slanted headline.

Labels:
- neg: selection, framing or word choice casts the target unfavorably. Giving
  critics the headline with no response from the target is neg. Uncritically
  amplifying an accusation is neg.
- pos: casts the target favorably -- praise, achievement framing, the target's
  own framing adopted as the article's, critics absent or dismissed.
- neu: a plain report with no evaluative framing, or a balanced one where the
  target and its critics both get comparable voice.

Not by itself decisive: quoting a critic (only neg if the article adopts or
foregrounds it); reporting a bad outcome for the target in flat language
(neu). Traditional Chinese epithets, honorifics, scare quotes and loaded verbs
are signal.

Respond with JSON only: {"label": "neg"|"neu"|"pos", "confidence": 0..1,
"evidence": "<the single most decisive phrase, copied verbatim from the text>"}.
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(LABELS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "string", "minLength": 1},
    },
    "required": ["label", "confidence", "evidence"],
}


def build_prompt(inp: StanceInput) -> str:
    """What the model sees. Pure so tests can pin it.

    The outlet is left out on purpose: a model that knows the byline can lean
    on priors about that outlet's politics, which is precisely the thing this
    project is trying to measure rather than assume.
    """
    return (
        f"TARGET: {inp.target}\n\n"
        f"HEADLINE:\n{inp.headline}\n\n"
        f"LEDE:\n{inp.lede or '(none)'}\n\n"
        f"BODY (excerpt):\n{inp.body_excerpt or '(none)'}\n"
    )


def parse_response(text: str) -> tuple[str, float, str]:
    """Validate the model's JSON. Anything off-schema is an error, not a guess."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stance response is not JSON: {text[:200]!r}") from exc
    label = data.get("label")
    if label not in LABELS:
        raise ValueError(f"stance label {label!r} not in {LABELS}")
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"stance confidence unusable: {data.get('confidence')!r}") from exc
    confidence = min(1.0, max(0.0, confidence))
    evidence = str(data.get("evidence") or "").strip()
    if not evidence:
        # A label with no cited phrase is unauditable. Rejecting it here means
        # nothing is cached, so the article is simply retried next run instead
        # of carrying an unexplained verdict forever.
        raise ValueError(f"stance response has no evidence phrase for label {label!r}")
    return label, confidence, evidence


# ---- Gemini backend --------------------------------------------------------


class GeminiStance:
    """Stance via the Gemini API, JSON-schema constrained, paced for the free tier.

    The pacing, retry and daily-quota handling live in nlp.gemini; this class
    owns only what is stance-specific: the system instruction, the schema, the
    prompt and the parse. `client` and `sleep` pass straight through so the
    tests here stay offline.
    """

    prompt_version = PROMPT_VERSION

    def __init__(
        self,
        model: str = STANCE_MODEL,
        rpm: float = STANCE_RPM,
        client: Any | None = None,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self._llm = GeminiJSON(
            model,
            rpm,
            system_instruction=SYSTEM_INSTRUCTION,
            schema=RESPONSE_SCHEMA,
            client=client,
            max_attempts=max_attempts,
            sleep=sleep,
            purpose="stance",
        )

    def classify(self, inp: StanceInput) -> StanceResult:
        label, confidence, evidence = parse_response(self._llm.generate(build_prompt(inp)))
        return StanceResult(label, confidence, evidence, self.model, PROMPT_VERSION)

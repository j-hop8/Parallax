"""The one-line framing summary per cluster member: the LLM step of Q3.

This is the only place in Q3 that calls a model, and it runs only on
`make framing ARGS=--summarize`, only for members whose deltas are non-empty,
and only once per (member, delta text, prompt version) -- the row is the cache
and db.save_framing clears it when the deltas change. The origin of a
confident cluster and a member with no deltas get a fixed string with no call.

The model sees the headline pair and the ＋ − ～ lists, never the outlet: a
model that knows the byline can lean on priors about that outlet, which is
what this project measures rather than assumes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..settings import FRAMING_MODEL, STANCE_RPM
from .framing import MemberDelta, normalize, pair_edits
from .gemini import DailyQuotaExhausted, GeminiJSON

__all__ = [
    "ORIGIN_SUMMARY",
    "RULE_MODEL",
    "SAME_SUMMARY",
    "SUMMARY_VERSION",
    "DailyQuotaExhausted",
    "GeminiSummary",
    "Summarizer",
    "SummaryInput",
    "build_prompt",
    "caption_like",
    "parse_response",
    "rule_summary",
]

# Bump whenever SYSTEM_INSTRUCTION or build_prompt changes in a way that could
# change a summary. Stored on every row; rows at another version are redone.
SUMMARY_VERSION = "v2"  # v2: caption-like additions are listed apart from the rest

# Deterministic summaries carry this as their model, so provenance is never blank.
RULE_MODEL = "rule"
ORIGIN_SUMMARY = "原始稿源。"
SAME_SUMMARY = "與核心稿源相同。"

MAX_ITEMS = 12  # per list in the prompt; the rest is counted
MAX_ITEM_CHARS = 160
MAX_SUMMARY_CHARS = 80


def rule_summary(delta: MemberDelta, *, is_origin: bool) -> str | None:
    """The fixed string for a member that needs no model call, else None."""
    if is_origin:
        return ORIGIN_SUMMARY
    if delta.empty:
        return SAME_SUMMARY
    return None


@dataclass(frozen=True)
class SummaryInput:
    article_id: int
    directional: bool  # False: describe what is only in this version, claim no order
    reference_headline: str  # the origin's, or "" when indeterminate
    headline: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    captions: tuple[str, ...] = ()  # subset of `added` that repeats shared text: photo captions


def caption_like(added: tuple[str, ...], core: tuple[str, ...]) -> tuple[str, ...]:
    """Added sentences that are a piece of a shared sentence, or contain one.

    tvbs and ltn put a trimmed copy of the lede under the photo; the model
    called those "background" until they were named for what they are.
    """
    core_keys = [normalize(c) for c in core]
    out = []
    for a in added:
        k = normalize(a)
        if k and any(ck and (k in ck or ck in k) for ck in core_keys):
            out.append(a)
    return tuple(out)


@dataclass(frozen=True)
class SummaryResult:
    summary: str
    model: str
    version: str


class Summarizer(Protocol):
    model: str
    version: str

    def summarize(self, inp: SummaryInput) -> SummaryResult: ...


SYSTEM_INSTRUCTION = """\
You compare two versions of the same Taiwanese news story that share most of
their text, and describe in ONE line of Traditional Chinese what the editors of
THIS version changed.

You will be given the two headlines and four lists: sentences this version
ADDED, CAPTIONS it added (short lines repeating shared text under a photo),
sentences it REMOVED, and sentences it REWROTE (old → new).

Write one line, at most 60 characters, made of short clauses separated by "；":
- start an addition with "＋", a deletion with "－", a rewrite with "～";
- say WHAT was changed in editorial terms: a quote from whom, a rebuttal, a
  sub-head, a background paragraph, a hedge, a loaded word; a caption is
  just "＋ 圖說", never "background";
- mention the headline first if it changed in meaning or tone, e.g.
  "～ 標題改為質問語氣" -- ignore punctuation-only headline changes;
- never name or guess the outlet, never evaluate whether the change is good.

If the lists say the order between versions is unknown, describe the listed
sentences as what is only in this version ("本版獨有：…") and claim nothing
about who changed what.

Respond with JSON only: {"summary": "<the line>"}.
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string", "minLength": 1}},
    "required": ["summary"],
}


def _clip(s: str) -> str:
    return s if len(s) <= MAX_ITEM_CHARS else s[:MAX_ITEM_CHARS].rstrip() + "…"


def _list(label: str, items: list[str]) -> str:
    if not items:
        return f"{label}: (none)\n"
    shown = "\n".join(f"- {_clip(s)}" for s in items[:MAX_ITEMS])
    more = f"\n- … and {len(items) - MAX_ITEMS} more" if len(items) > MAX_ITEMS else ""
    return f"{label}:\n{shown}{more}\n"


def build_prompt(inp: SummaryInput) -> str:
    """What the model sees. Pure so tests can pin it."""
    edits, added, removed = pair_edits(inp.added, inp.removed)
    if inp.directional:
        head = (
            f"ORIGINAL HEADLINE:\n{inp.reference_headline or '(none)'}\n\n"
            f"THIS VERSION'S HEADLINE:\n{inp.headline or '(none)'}\n\n"
        )
    else:
        head = (
            "Order between the versions is UNKNOWN: describe only what is in this "
            "version and not the others.\n\n"
            f"THIS VERSION'S HEADLINE:\n{inp.headline or '(none)'}\n\n"
        )
    rewrote = [f"{_clip(old)} → {_clip(new)}" for old, new in edits]
    captions = set(inp.captions)
    return (
        head
        + _list("ADDED", [a for a in added if a not in captions])
        + "\n"
        + _list("CAPTIONS", [a for a in added if a in captions])
        + "\n"
        + _list("REMOVED", removed)
        + "\n"
        + _list("REWROTE", rewrote)
    )


def parse_response(text: str) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"summary response is not JSON: {text[:200]!r}") from exc
    summary = " ".join(str(data.get("summary") or "").split())
    if not summary:
        raise ValueError("summary response is empty")
    return summary[:MAX_SUMMARY_CHARS]


class GeminiSummary:
    version = SUMMARY_VERSION

    def __init__(
        self,
        model: str = FRAMING_MODEL,
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
            purpose="framing",
        )

    def summarize(self, inp: SummaryInput) -> SummaryResult:
        return SummaryResult(
            parse_response(self._llm.generate(build_prompt(inp))), self.model, SUMMARY_VERSION
        )

"""The one Gemini caller: paced for the free tier, stops on a daily quota.

Shared by stance (T-007) and the framing summary (T-009) so the lessons the
first live run paid for -- a per-day quota that no backoff can wait out, a
503 that heals by waiting, the server's own RetryInfo hint -- are learned
once. Callers hand in a system instruction and a JSON schema and get the
response text back; parsing and prompt versioning stay with the caller,
because that is what makes their results comparable.

`client` is injectable so tests never touch the network; the real client is
built lazily so importing this module never requires the `llm` extra -- the
crawler must keep installing without it.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class DailyQuotaExhausted(RuntimeError):
    """The model's per-day free-tier quota is spent. Waiting a minute will not
    help and neither will the next article; callers should stop the run.

    Learned live: gemini-3.8-flash allows 20 requests/day on the free tier
    (quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier). Without this
    the job retried each remaining article through its full backoff and
    recorded 170+ failures for nothing.
    """

    def __init__(self, model: str, quota_id: str, quota_value: str | None) -> None:
        self.model, self.quota_id, self.quota_value = model, quota_id, quota_value
        limit = f" (limit {quota_value}/day)" if quota_value else ""
        super().__init__(
            f"{model}: daily quota exhausted{limit}; stop and resume tomorrow or switch model"
        )


class Pacer:
    """Hold a request rate. Same idea as crawl.http.Fetcher._wait, one host."""

    def __init__(self, rpm: float, sleep: Callable[[float], None] = time.sleep) -> None:
        self.min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self._last is not None:
            gap = self.min_interval - (now - self._last)
            if gap > 0:
                self._sleep(gap)
        self._last = time.monotonic()


_RETRY_DELAY = re.compile(r"(\d+(?:\.\d+)?)s")


def _daily_quota(exc: Exception) -> tuple[str, str | None] | None:
    """(quotaId, quotaValue) when a 429 is a per-day quota, else None."""
    if getattr(exc, "code", None) != 429:
        return None
    for entry in _walk(getattr(exc, "details", None)):
        if isinstance(entry, dict) and "PerDay" in str(entry.get("quotaId", "")):
            return str(entry["quotaId"]), (
                str(entry["quotaValue"]) if "quotaValue" in entry else None
            )
    return None


def _is_transient(exc: Exception) -> bool:
    """429 and 5xx: the free tier rate-limits, and the first live run met a
    503 "model is experiencing high demand" -- both heal by waiting."""
    code = getattr(exc, "code", None)
    return code == 429 or (isinstance(code, int) and 500 <= code < 600)


def _retry_delay(exc: Exception, attempt: int, cap: float = 120.0) -> float:
    """The server's RetryInfo hint when present, else exponential backoff.

    Gemini's 429 body usually carries details[] with a RetryInfo entry like
    {"retryDelay": "34s"}. Free-tier limits are per-account and unpublished,
    so the hint is the only honest number; the fallback exists for when the
    body is missing or shaped differently.
    """
    details = getattr(exc, "details", None)
    for entry in _walk(details):
        if isinstance(entry, dict) and "retryDelay" in entry:
            match = _RETRY_DELAY.match(str(entry["retryDelay"]))
            if match:
                return min(cap, float(match.group(1)) + 1.0)
    return min(cap, 10.0 * (2**attempt))


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


class GeminiJSON:
    """Schema-constrained JSON generation with pacing and retry."""

    def __init__(
        self,
        model: str,
        rpm: float,
        *,
        system_instruction: str,
        schema: dict[str, Any],
        client: Any | None = None,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        purpose: str = "gemini",
    ) -> None:
        self.model = model
        self.max_attempts = max_attempts
        self._system_instruction = system_instruction
        self._schema = schema
        self._client = client
        self._sleep = sleep
        self._pacer = Pacer(rpm, sleep=sleep)
        self._purpose = purpose  # log prefix only

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai  # reads GEMINI_API_KEY from the environment

            self._client = genai.Client()
        return self._client

    def _config(self) -> dict[str, Any]:
        # A plain dict, which the SDK coerces, so this module has no import-time
        # dependency on google.genai.
        return {
            "system_instruction": self._system_instruction,
            "response_mime_type": "application/json",
            "response_json_schema": self._schema,
            "temperature": 0.0,
            # No tools are declared; this only silences the SDK's warning that
            # automatic function calling is on by default.
            "automatic_function_calling": {"disable": True},
        }

    def generate(self, prompt: str) -> str:
        """The response text, or raise: DailyQuotaExhausted at once on a per-day
        429, the original error after the retries for anything else."""
        for attempt in range(self.max_attempts):
            self._pacer.wait()
            try:
                response = self._get_client().models.generate_content(
                    model=self.model, contents=prompt, config=self._config()
                )
            except Exception as exc:
                daily = _daily_quota(exc)
                if daily is not None:
                    raise DailyQuotaExhausted(self.model, *daily) from exc
                if _is_transient(exc) and attempt < self.max_attempts - 1:
                    delay = _retry_delay(exc, attempt)
                    log.warning(
                        "%s: %s from %s; sleeping %.0fs",
                        self._purpose,
                        getattr(exc, "code", type(exc).__name__),
                        self.model,
                        delay,
                    )
                    self._sleep(delay)
                    continue
                raise
            return response.text
        raise RuntimeError("unreachable: retry loop exhausted without raising")

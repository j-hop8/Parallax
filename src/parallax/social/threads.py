from __future__ import annotations

import gzip
import hashlib
import json
import time
from collections.abc import Iterator
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

from .. import settings
from ..config import load_outlets

# Verified against Meta's official Threads Postman collection, keyword_search.
FIELDS = "id,text,username,permalink,timestamp,media_type,is_quote_post,has_replies"


class ThreadsError(RuntimeError):
    pass


class ThreadsClient:
    def __init__(self, token=None, *, session=None, raw_dir=None, before_request=None):
        self.token = token or settings.THREADS_ACCESS_TOKEN
        if not self.token:
            raise ValueError("THREADS_ACCESS_TOKEN is unset")
        self.session = session or requests.Session()
        defaults, _ = load_outlets()
        self.session.headers.update({"User-Agent": defaults["user_agent"]})
        self.raw_dir = raw_dir or settings.RAW_DIR
        self.before_request = before_request
        self.queries = 0
        self.raw_path = None

    def redact(self, text):
        return (
            str(text)
            .replace(self.token, "[REDACTED]")
            .replace(urlencode({"": self.token})[1:], "[REDACTED]")
        )

    def _get(self, endpoint, params, *, refresh=False):
        url = f"{settings.THREADS_API_BASE.rstrip('/')}/{endpoint}"
        digest = hashlib.sha1(f"{url}?{urlencode(sorted(params.items()))}".encode()).hexdigest()
        day = datetime.now(ZoneInfo(settings.TIMEZONE)).date().isoformat()
        path = self.raw_dir / "threads" / day / f"{digest}.json.gz"
        self.raw_path = str(path)
        # Search results change throughout the day; raw responses are write-only.
        for attempt in range(3):
            if self.before_request:
                self.before_request()
            self.queries += 1
            try:
                # Header auth keeps credentials out of requests/urllib3 URL logs.
                response = self.session.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=20,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise ThreadsError(self.redact(exc)) from None
            body = response.text
            if refresh:
                # Refresh is the sole intentional token output; never cache it.
                try:
                    data = response.json()
                    body = body.replace(data.get("access_token", "\0"), "[REDACTED]")
                except ValueError:
                    pass
            body = self.redact(body)
            path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(path, "wt", encoding="utf-8") as stream:
                stream.write(body)
            if (response.status_code == 429 or 500 <= response.status_code < 600) and attempt < 2:
                time.sleep(4 * (attempt + 1))
                continue
            if not 200 <= response.status_code < 300:
                raise ThreadsError(f"Threads HTTP {response.status_code}: {body}")
            try:
                data = response.json() if refresh else json.loads(body)
            except ValueError:
                raise ThreadsError("Threads returned invalid JSON") from None
            if "error" in data:
                raise ThreadsError(self.redact(json.dumps(data["error"])))
            return data

    def me(self) -> dict:
        return self._get("me", {"fields": "id,username"})

    def refresh_token(self) -> dict:
        return self._get("refresh_access_token", {"grant_type": "th_refresh_token"}, refresh=True)

    def keyword_search(
        self, q, since, until, *, search_type="RECENT", page_size=100
    ) -> Iterator[dict]:
        if not q.strip() or not 1 <= page_size <= 100 or search_type not in {"RECENT", "TOP"}:
            raise ValueError("Invalid keyword search parameters")
        if not 1688540400 <= since < until <= time.time():
            raise ValueError("Invalid Threads time window")
        params = {
            "q": q,
            "since": since,
            "until": until,
            "search_type": search_type,
            "limit": page_size,
            "fields": FIELDS,
        }
        cursors = set()
        while True:
            data = self._get("keyword_search", params)
            yield from data["data"]
            after = data.get("paging", {}).get("cursors", {}).get("after")
            if not data["data"] or not after:
                return
            if after in cursors:
                raise ThreadsError("Threads repeated a pagination cursor")
            cursors.add(after)
            params["after"] = after

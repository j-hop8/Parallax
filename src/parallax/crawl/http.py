from __future__ import annotations

import time
from urllib.parse import urlsplit

import requests


class Fetcher:
    """HTTP client with a per-host minimum delay.

    Outlets are polled every 20 minutes from a single machine; the delay exists
    so that a burst of requests to one host stays well inside anything a news
    site would consider reasonable, and so a retry storm cannot turn into
    hammering. The User-Agent identifies the project and a contact address.
    """

    def __init__(self, user_agent: str, timeout: float = 20.0, min_delay: float = 2.0) -> None:
        self.timeout = timeout
        self.min_delay = min_delay
        self._last_request: dict[str, float] = {}
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})

    def _wait(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        last = self._last_request.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self.min_delay:
                time.sleep(self.min_delay - elapsed)
        self._last_request[host] = time.monotonic()

    def get(self, url: str) -> requests.Response:
        self._wait(url)
        response = self._session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response

    def get_text(self, url: str) -> str:
        response = self.get(url)
        # Taiwanese outlets are inconsistent about declaring charset; letting
        # requests fall back to its apparent-encoding guess mangles far less
        # Chinese than the ISO-8859-1 default it would otherwise assume.
        if response.encoding is None or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding
        return response.text

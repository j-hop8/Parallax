from __future__ import annotations

import logging
import socket
import time
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)


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

    def get(self, url: str, retries: int = 2, backoff: float = 4.0) -> requests.Response:
        """Fetch, retrying only failures that plausibly heal on their own.

        This host is a laptop that sleeps. On a dark wake the scheduler can fire
        seconds before Wi-Fi re-associates, so the first attempt fails against
        every outlet at once while the network is milliseconds from working. A
        couple of backed-off retries recover that cycle instead of losing 20
        minutes of listings that cannot be re-fetched.

        Only connection and timeout errors retry. An HTTP status error is the
        server answering, and a 404 will not become a 200 -- retrying it would
        just hammer the outlet.
        """
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            self._wait(url)
            try:
                response = self._session.get(url, timeout=self.timeout)
                response.raise_for_status()
                return response
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(backoff * (attempt + 1))

        assert last_error is not None
        raise last_error

    def get_text(self, url: str) -> str:
        response = self.get(url)
        # Taiwanese outlets are inconsistent about declaring charset; letting
        # requests fall back to its apparent-encoding guess mangles far less
        # Chinese than the ISO-8859-1 default it would otherwise assume.
        if response.encoding is None or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding
        return response.text


def wait_for_network(
    probe_hosts: tuple[str, ...] = ("feeds.feedburner.com", "news.ltn.com.tw"),
    timeout: float = 120.0,
    interval: float = 5.0,
) -> bool:
    """Block until DNS and TCP to a real outlet host work, or give up.

    Measured motivation: on a dark wake the crawl starts before Wi-Fi
    re-associates, and the failures land on the *first* feeds in config order --
    中央社's politics, social, intworld, finance, lifehealth and culture all
    failed while the last five of the same eleven succeeded. The network came up
    mid-run.

    Per-request retries help but still spend the early feeds on a dead
    interface. One upfront wait costs nothing when the network is fine and saves
    the whole cycle when it isn't. Returns False on timeout rather than raising:
    the crawl should still run and record real failures, because a recorded
    failure is what makes the gap visible.
    """
    deadline = time.monotonic() + timeout
    waited = False

    while True:
        for host in probe_hosts:
            try:
                with socket.create_connection((host, 443), timeout=5):
                    if waited:
                        log.info("network became reachable (%s); starting crawl", host)
                    return True
            except OSError:
                continue

        if time.monotonic() >= deadline:
            log.warning(
                "no network after %.0fs; crawling anyway so the failures are recorded", timeout
            )
            return False

        waited = True
        time.sleep(interval)

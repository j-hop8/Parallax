"""Regression tests for the ways a crawl can look healthy while losing data.

Every case here was a real bug caught in review, and each shipped precisely
because nothing asserted on it. They share one shape: the crawl completes, the
run is recorded ok, and articles are silently missing -- which then lets the
rollup mark the day complete and use a short count as a coverage denominator.
"""

from __future__ import annotations

import contextlib

import pytest
import requests

from parallax.config import UnverifiedOutletError
from parallax.crawl.adapters.base import PartialFetchError, RSSAdapter
from parallax.crawl.listing import _check_explicit_target
from parallax.models import ArticleStub, OutletConfig


def _outlet(code="x", n_feeds=3, verified=True) -> OutletConfig:
    return OutletConfig(
        code=code,
        name_zh=code,
        home_url="https://example.com/",
        feed_urls=tuple(f"https://example.com/feed{i}.xml" for i in range(n_feeds)),
        parser="rss",
        rate_limit_seconds=0,
        verified=verified,
    )


class _Fetcher:
    """Serves one working feed and fails the rest, mimicking a partial outage."""

    def __init__(self, working: set[int]):
        self.working = working

    def get(self, url: str):
        index = int(url.rstrip(".xml")[-1])
        if index not in self.working:
            raise ConnectionError(f"feed {index} down")

        class _Resp:
            content = b"""<?xml version="1.0"?><rss version="2.0"><channel>
              <item><title>Real headline</title><link>https://example.com/a1</link>
              <pubDate>Sat, 16 Aug 2026 09:00:00 +0800</pubDate></item>
            </channel></rss>"""

        return _Resp()


def test_partial_feed_failure_is_not_a_healthy_run():
    """1 of 3 sections working must not be recorded as a good crawl.

    中央社 is polled across 12 section feeds. If 11 fail, the survivor still
    returns articles; calling that ok would let a day missing ~90% of its output
    stand as a denominator.
    """
    adapter = RSSAdapter(_outlet(n_feeds=3), _Fetcher(working={0}))
    with pytest.raises(PartialFetchError) as excinfo:
        adapter.fetch()

    # The articles that did arrive must survive -- they cannot be re-fetched.
    assert len(excinfo.value.stubs) == 1
    assert excinfo.value.stubs[0].title == "Real headline"
    assert len(excinfo.value.errors) == 2


def test_all_feeds_working_returns_cleanly():
    stubs = RSSAdapter(_outlet(n_feeds=3), _Fetcher(working={0, 1, 2})).fetch()
    # One article, deduplicated across the three identical feeds.
    assert len(stubs) == 1
    assert isinstance(stubs[0], ArticleStub)


class _StubAdapter:
    def __init__(self, code):
        self.code = code

    def fetch(self):
        return [
            ArticleStub(
                outlet=self.code,
                url_original=f"https://example.com/{self.code}/1",
                title="t",
                published_at=None,
            )
        ]


def test_a_write_failure_for_one_outlet_does_not_stop_the_others(monkeypatch):
    """The isolation guarantee, exercised through crawl_all rather than the adapter.

    Round 1's fix persisted partial results inside the except handler, where a
    database error escaped the per-outlet try -- aborting the crawl, skipping
    every later outlet, and recording nothing. Testing only the adapter missed
    this entirely, which is how it shipped.
    """
    from parallax.crawl import listing as mod

    outlets = [_outlet("a"), _outlet("b"), _outlet("c")]
    monkeypatch.setattr(mod, "load_outlets", lambda: ({}, outlets))
    monkeypatch.setattr(mod, "_build_fetcher", lambda defaults: None)
    monkeypatch.setattr(mod, "build_adapter", lambda cfg, fetcher: _StubAdapter(cfg.code))

    class _Conn:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    conn = _Conn()

    @contextlib.contextmanager
    def _connect():
        yield conn

    recorded: list = []
    monkeypatch.setattr(mod.db, "connect", _connect)
    monkeypatch.setattr(mod.db, "ensure_outlets", lambda c, o: None)
    monkeypatch.setattr(mod.db, "record_crawl_run", lambda c, r: recorded.append(r))

    def _upsert(c, stubs):
        if stubs and stubs[0].outlet == "b":
            raise RuntimeError("disk full")
        return len(stubs), len(stubs)

    monkeypatch.setattr(mod.db, "upsert_article_index", _upsert)

    results = mod.crawl_all()

    # Every outlet was attempted -- 'b' failing did not truncate the loop.
    assert [r.outlet for r in results] == ["a", "b", "c"]
    assert [r.ok for r in results] == [True, False, True]

    # And every outcome reached crawl_runs, including the failure. A failure that
    # is not recorded is worse than the failure itself.
    assert len(recorded) == 3
    assert "disk full" in recorded[1].error
    assert conn.rollbacks >= 1


def test_naming_an_unverified_outlet_raises_rather_than_skipping():
    """A silent skip looks identical to a successful crawl of that outlet."""
    outlets = [_outlet("cna"), _outlet("setn", verified=False)]

    with pytest.raises(UnverifiedOutletError, match="setn"):
        _check_explicit_target(outlets, "setn")

    with pytest.raises(ValueError, match="unknown outlet"):
        _check_explicit_target(outlets, "nosuchoutlet")

    # The bulk crawl must still be allowed to run and skip it.
    _check_explicit_target(outlets, None)
    _check_explicit_target(outlets, "cna")


def test_transient_network_errors_retry_but_http_errors_do_not(monkeypatch):
    """On a dark wake the scheduler fires before Wi-Fi associates.

    That first attempt fails against every outlet at once while the network is
    milliseconds from working, so retrying recovers a cycle of listings that
    cannot be re-fetched. A 404 will never become a 200, though, and retrying it
    would only hammer the outlet.
    """
    from parallax.crawl.http import Fetcher

    monkeypatch.setattr("time.sleep", lambda _s: None)  # keep the test instant

    fetcher = Fetcher("test-agent", timeout=1, min_delay=0)
    attempts = {"n": 0}

    def _flaky(url, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise requests.ConnectionError("network down")

        class _R:
            status_code = 200

            def raise_for_status(self):
                return None

        return _R()

    monkeypatch.setattr(fetcher._session, "get", _flaky)
    assert fetcher.get("https://example.com/a").status_code == 200
    assert attempts["n"] == 3, "should have retried twice before succeeding"

    # A 404 is the server answering. One attempt only.
    attempts["n"] = 0

    def _not_found(url, timeout):
        attempts["n"] += 1

        class _R:
            status_code = 404

            def raise_for_status(self):
                raise requests.HTTPError("404")

        return _R()

    monkeypatch.setattr(fetcher._session, "get", _not_found)
    with pytest.raises(requests.HTTPError):
        fetcher.get("https://example.com/missing")
    assert attempts["n"] == 1, "HTTP status errors must not be retried"


def test_retries_are_exhausted_and_the_error_surfaces(monkeypatch):
    """A permanently dead network must raise, not silently return nothing."""
    from parallax.crawl.http import Fetcher

    monkeypatch.setattr("time.sleep", lambda _s: None)
    fetcher = Fetcher("test-agent", timeout=1, min_delay=0)

    def _always_down(url, timeout):
        raise requests.ConnectionError("still down")

    monkeypatch.setattr(fetcher._session, "get", _always_down)
    with pytest.raises(requests.ConnectionError):
        fetcher.get("https://example.com/a")


def test_network_preflight_returns_false_rather_than_raising(monkeypatch):
    """A dead network must not abort the crawl.

    Returning False lets the run proceed and record real failures in crawl_runs.
    Raising here would leave no row at all, and an invisible gap is worse than a
    recorded one -- it is exactly what the completeness flag relies on seeing.
    """
    from parallax.crawl.http import wait_for_network

    monkeypatch.setattr("time.sleep", lambda _s: None)
    # 192.0.2.0/24 is TEST-NET-1: reserved, guaranteed unroutable.
    assert wait_for_network(probe_hosts=("192.0.2.1",), timeout=0.01, interval=0.01) is False


def test_network_preflight_succeeds_without_waiting_when_reachable(monkeypatch):
    from parallax.crawl import http as mod

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    slept = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(mod.socket, "create_connection", lambda addr, timeout: _Sock())

    assert mod.wait_for_network(timeout=60) is True
    assert slept == [], "must not sleep when the network is already up"


def test_crawl_runs_records_the_real_start_not_the_write_time(monkeypatch):
    """started_at must be when the fetch began, not when the row was inserted.

    It previously took the column's DEFAULT now(), so it was set at INSERT time
    -- the same instant as finished_at. Every run recorded a 0.0s duration, and
    started_at was in truth the END of the crawl. The rollup measures coverage
    gaps between started_at values, so a slow run pushed its own timestamp later
    and distorted the completeness flag that decides whether a day is usable as a
    coverage-weight denominator.
    """
    import datetime as _dt

    from parallax.models import CrawlResult

    before = _dt.datetime.now(_dt.UTC)
    result = CrawlResult(outlet="cna")
    after = _dt.datetime.now(_dt.UTC)

    # Stamped at construction -- i.e. when the outlet's work begins.
    assert before <= result.started_at <= after
    assert result.started_at.tzinfo is not None, "must be timezone-aware"

    # And it is passed to the INSERT rather than left to the column default.
    import inspect

    from parallax import db

    sql = inspect.getsource(db.record_crawl_run)
    assert "started_at" in sql, "record_crawl_run must write started_at explicitly"
    assert "result.started_at" in sql


def test_one_hanging_outlet_cannot_consume_the_whole_crawl_cycle(monkeypatch):
    """A host that accepts connections then hangs must not starve the others.

    中央社 is polled across 11 feeds. Without a budget, timeout x retries per
    feed can exceed the 20-minute interval on its own, delaying or skipping every
    outlet queued behind it -- and tier-1 data missed that way is unrecoverable.

    The unreached feeds are reported as errors rather than dropped, so the run is
    honestly degraded and its day cannot qualify as a denominator.
    """
    import time as _time

    cfg = _outlet(n_feeds=6)
    object.__setattr__(cfg, "budget_seconds", 0.05)

    class _Hanging:
        def __init__(self):
            self.calls = 0

        def get(self, url):
            self.calls += 1
            _time.sleep(0.04)  # each feed eats a big slice of the budget
            raise ConnectionError("hung")

    fetcher = _Hanging()
    with pytest.raises(PartialFetchError) as excinfo:
        RSSAdapter(cfg, fetcher).fetch()

    # It gave up well before working through all six feeds.
    assert fetcher.calls < 6, f"budget not enforced; made {fetcher.calls} calls"
    # Every feed is still accounted for -- attempted or skipped, none vanish.
    assert len(excinfo.value.errors) == 6
    assert any("budget spent" in e for e in excinfo.value.errors)


def test_budget_does_not_engage_on_a_healthy_outlet():
    """Normal cost is ~22s for 中央社; the budget must never bite in that case."""
    stubs = RSSAdapter(_outlet(n_feeds=3), _Fetcher(working={0, 1, 2})).fetch()
    assert len(stubs) == 1


def test_worst_case_crawl_cycle_fits_the_launchd_interval():
    """Even if every host accepts connections and then hangs, one cycle must
    finish before launchd fires the next.

    The per-outlet budget is checked between requests, not inside one, so it is
    soft by a single request's worst case (attempts x timeout plus backoff).
    Pattern and TVBS adapters issue exactly one request, so that single-request
    cost IS their worst case, budget or not. This does the arithmetic from the
    shipped config, the Fetcher's retry defaults and the plist template, so
    raising budget_seconds, timeout_seconds or the feed count past the point
    where a hung cycle overlaps the next one fails here rather than in
    production, where the cost is tier-1 data that cannot be re-fetched.
    """
    import inspect
    import re
    from pathlib import Path

    from parallax.config import load_outlets
    from parallax.crawl.http import Fetcher, wait_for_network

    defaults, outlets = load_outlets()
    get_params = inspect.signature(Fetcher.get).parameters
    retries = get_params["retries"].default
    backoff = get_params["backoff"].default
    timeout = float(defaults["timeout_seconds"])
    rate_limit = float(defaults["rate_limit_seconds"])

    # One request that times out on every attempt: each attempt pays the rate
    # limit and the full timeout, and each retry pays its backoff.
    worst_request = (retries + 1) * (rate_limit + timeout) + sum(
        backoff * (i + 1) for i in range(retries)
    )

    # The upfront network wait runs once per cycle before any outlet.
    total = float(inspect.signature(wait_for_network).parameters["timeout"].default)
    for outlet in outlets:
        if not outlet.verified:
            continue
        n_requests = len(outlet.feed_urls) if outlet.parser == "rss" else 1
        # Sequential requests until the budget is spent, plus the one in flight.
        total += min(n_requests * worst_request, outlet.budget_seconds + worst_request)

    plist = Path(__file__).resolve().parents[1] / "ops" / "com.parallax.crawl.plist.template"
    match = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", plist.read_text())
    assert match, "StartInterval not found in the plist template"
    interval = int(match.group(1))

    assert total < interval, (
        f"a fully hung cycle takes {total:.0f}s against a {interval}s interval; "
        "lower budget_seconds or timeout_seconds, or the feed count"
    )

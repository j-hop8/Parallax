"""Scheduled crawl heartbeats, with all network and database work faked."""

import logging
from unittest.mock import Mock, call

import pytest
import requests

from parallax import settings
from parallax.jobs import crawl_listing as job
from parallax.models import CrawlResult

PING_URL = "https://hc-ping.com/secret-check-id"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    results = [
        CrawlResult(outlet="cna", ok=True, items_seen=3, items_new=1),
        CrawlResult(outlet="udn", ok=True, items_seen=2, items_new=1),
    ]
    monkeypatch.setattr(settings, "HEARTBEAT_URL", PING_URL)
    monkeypatch.setattr(job, "wait_for_network", Mock(return_value=True))
    monkeypatch.setattr(job, "crawl_all", Mock(return_value=results))
    monkeypatch.setattr(job, "crawl_dry_run", Mock(return_value=results))
    monkeypatch.setattr(
        requests, "get", Mock(side_effect=AssertionError("unexpected heartbeat request"))
    )


@pytest.mark.parametrize("failed", [False, True])
def test_unset_url_makes_no_request(monkeypatch, failed):
    monkeypatch.setattr(settings, "HEARTBEAT_URL", None)
    job.crawl_all.return_value[1].ok = not failed

    assert job.main([]) == int(failed)
    requests.get.assert_not_called()


@pytest.mark.parametrize("failed", [False, True])
def test_full_run_pings_its_outcome_after_crawl_returns(monkeypatch, failed):
    job.crawl_all.return_value[1].ok = not failed
    events = []
    results = job.crawl_all.return_value

    def crawl(*, only):
        assert only is None
        events.append("crawl finished")
        return results

    response = Mock()

    def get(url, *, timeout):
        assert events == ["crawl finished"]
        events.append("ping")
        assert url == PING_URL + ("/fail" if failed else "")
        assert timeout == (5, 5)
        return response

    monkeypatch.setattr(job, "crawl_all", crawl)
    monkeypatch.setattr(requests, "get", Mock(side_effect=get))

    assert job.main([]) == int(failed)
    requests.get.assert_called_once()
    response.raise_for_status.assert_called_once_with()
    assert events == ["crawl finished", "ping"]


@pytest.mark.parametrize("args", [["--dry-run"], ["--outlet", "cna"]])
def test_partial_and_dry_runs_do_not_ping(args):
    assert job.main(args) == 0
    requests.get.assert_not_called()
    if "--dry-run" in args:
        job.crawl_all.assert_not_called()
        job.crawl_dry_run.assert_called_once_with(only=None)
        job.wait_for_network.assert_not_called()
    else:
        job.crawl_all.assert_called_once_with(only="cna")


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError, RuntimeError])
def test_ping_exceptions_are_bounded_and_do_not_leak_urls(monkeypatch, caplog, failed, error):
    job.crawl_all.return_value[1].ok = not failed
    monkeypatch.setattr(requests, "get", Mock(side_effect=error(f"request failed: {PING_URL}")))
    caplog.set_level(logging.DEBUG)

    assert job.main([]) == int(failed)
    url = PING_URL + ("/fail" if failed else "")
    assert requests.get.call_args_list == [call(url, timeout=(5, 5))] * 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [f"heartbeat ping failed: {error.__name__}"] * 2
    assert all(r.exc_info is None for r in warnings)
    assert PING_URL not in caplog.text


def test_transient_ping_error_retries_then_stops(monkeypatch):
    monkeypatch.setattr(
        requests, "get", Mock(side_effect=[requests.Timeout(PING_URL), Mock()])
    )

    assert job.main([]) == 0
    assert requests.get.call_args_list == [call(PING_URL, timeout=(5, 5))] * 2


def test_http_error_is_logged_without_its_url(monkeypatch, caplog):
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError(f"503 for url: {PING_URL}")
    monkeypatch.setattr(requests, "get", Mock(return_value=response))

    assert job.main([]) == 0
    assert requests.get.call_count == 2
    assert "heartbeat ping failed: HTTPError" in caplog.text
    assert PING_URL not in caplog.text


@pytest.mark.parametrize("error", [None, requests.ConnectionError])
def test_verbose_transport_logs_cannot_expose_ping_path(monkeypatch, caplog, error):
    transport_log = logging.getLogger("urllib3.connectionpool")
    monkeypatch.setattr(transport_log, "disabled", False)
    caplog.set_level(logging.DEBUG)

    def get(url, *, timeout):
        transport_log.debug("GET /secret-check-id HTTP/1.1")
        if error:
            raise error(url)
        return Mock()

    monkeypatch.setattr(requests, "get", get)

    assert job.main(["--verbose"]) == 0
    assert "secret-check-id" not in caplog.text
    assert transport_log.disabled is False


def test_crashed_crawl_does_not_ping():
    job.crawl_all.side_effect = RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        job.main([])
    requests.get.assert_not_called()


def test_empty_crawl_keeps_exit_code_without_ping():
    job.crawl_all.return_value = []

    assert job.main([]) == 2
    requests.get.assert_not_called()

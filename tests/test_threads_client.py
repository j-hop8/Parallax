"""Sanitized API-shaped fixtures; no live network requests."""

import gzip
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from parallax.social.threads import ThreadsClient, ThreadsError

FIXTURES = Path(__file__).parent / "fixtures" / "threads"
SINCE, UNTIL = 1789747200, 1789833600


def response(name, status=200):
    r = requests.Response()
    r.status_code = status
    r._content = (FIXTURES / name).read_bytes()
    return r


def client(tmp_path, responses):
    session = Mock(headers={})
    session.get.side_effect = responses
    return ThreadsClient("secret-token", session=session, raw_dir=tmp_path)


def test_pages_cache_and_redaction(tmp_path):
    c = client(tmp_path, [response("page1.json"), response("page2.json")] * 2)
    assert len(list(c.keyword_search("沈伯洋", SINCE, UNTIL))) == 2
    assert c.queries == 2
    assert c.session.get.call_args.kwargs["params"]["after"] == "cursor2"
    assert "contact:" in c.session.headers["User-Agent"]
    paths = list(tmp_path.rglob("*.gz"))
    assert len(paths) == 2
    for path in paths:
        with gzip.open(path, "rt") as stream:
            assert "secret-token" not in stream.read()
    assert len(list(c.keyword_search("沈伯洋", SINCE, UNTIL))) == 2
    assert c.queries == 4
    assert c.session.get.call_count == 4


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retry_counts_every_attempt(tmp_path, monkeypatch, status):
    sleep = Mock()
    monkeypatch.setattr("parallax.social.threads.time.sleep", sleep)
    c = client(tmp_path, [response("error.json", status), response("page2.json")])
    assert len(list(c.keyword_search("x", SINCE, UNTIL))) == 1
    assert c.queries == 2
    sleep.assert_called_once_with(4)


@pytest.mark.parametrize("status", [400, 401, 403])
def test_no_retry_and_safe_error(tmp_path, status):
    c = client(tmp_path, [response("error.json", status)])
    with pytest.raises(ThreadsError, match="Invalid token") as exc:
        list(c.keyword_search("x", SINCE, UNTIL))
    assert "secret-token" not in str(exc.value)
    assert c.queries == 1


def test_network_error_not_retried(tmp_path):
    c = client(tmp_path, [requests.ConnectionError("url?access_token=secret-token")])
    with pytest.raises(ThreadsError) as exc:
        c.me()
    assert "secret-token" not in str(exc.value)
    assert c.queries == 1


def test_refresh_never_caches_new_token(tmp_path):
    c = client(tmp_path, [response("refresh.json")])
    assert c.refresh_token()["access_token"] == "new-secret-token"
    for path in tmp_path.rglob("*.gz"):
        with gzip.open(path, "rt") as stream:
            saved = json.load(stream)
        assert saved["access_token"] == "[REDACTED]"


def test_repeated_cursor_stops(tmp_path):
    c = client(tmp_path, [response("page1.json"), response("page1.json")])
    with pytest.raises(ThreadsError, match="repeated"):
        list(c.keyword_search("x", SINCE, UNTIL))
    assert c.queries == 2

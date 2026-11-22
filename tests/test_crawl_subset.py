"""Host outlet selection, exercising the real crawl loops without network or DB."""

import contextlib
import logging
import runpy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from parallax import settings
from parallax.config import UnverifiedOutletError, load_outlets
from parallax.crawl import listing
from parallax.jobs import crawl_listing as job
from parallax.models import ArticleStub

PING_URL = "https://hc-ping.com/subset-check-id"


@pytest.fixture
def offline(monkeypatch):
    _, outlets = load_outlets()
    outlets.append(replace(outlets[0], code="pending", verified=False))
    monkeypatch.setattr(job, "load_outlets", lambda: ({}, outlets))
    monkeypatch.setattr(listing, "load_outlets", lambda: ({}, outlets))
    monkeypatch.setattr(settings, "CRAWL_OUTLETS", ())
    monkeypatch.setattr(settings, "CRAWL_EXCLUDE", ())
    monkeypatch.setattr(settings, "HEARTBEAT_URL", None)
    monkeypatch.setattr(job, "wait_for_network", Mock(return_value=True))
    monkeypatch.setattr(listing, "_build_fetcher", Mock(return_value=None))
    monkeypatch.setattr(requests, "get", Mock(side_effect=AssertionError("unexpected request")))

    state = SimpleNamespace(outlets=outlets, fetched=[], recorded=[], failed=set())

    def adapter(cfg, fetcher):
        def fetch():
            state.fetched.append(cfg.code)
            if cfg.code in state.failed:
                raise RuntimeError("feed unavailable")
            return [ArticleStub(cfg.code, f"https://example.com/{cfg.code}/1", "title", None)]

        return SimpleNamespace(fetch=fetch)

    monkeypatch.setattr(listing, "build_adapter", adapter)
    monkeypatch.setattr(listing.db, "connect", Mock(return_value=contextlib.nullcontext(Mock())))
    monkeypatch.setattr(listing.db, "ensure_outlets", Mock())
    monkeypatch.setattr(
        listing.db, "upsert_article_index", lambda conn, stubs: (len(stubs), len(stubs))
    )
    monkeypatch.setattr(
        listing.db, "record_crawl_run", lambda conn, result: state.recorded.append(result)
    )
    return state


@pytest.fixture
def configure(monkeypatch):
    def configure(include="", exclude=""):
        monkeypatch.setenv("PARALLAX_CRAWL_OUTLETS", include)
        monkeypatch.setenv("PARALLAX_CRAWL_EXCLUDE", exclude)
        values = runpy.run_path(settings.__file__)
        for key in ("CRAWL_OUTLETS", "CRAWL_EXCLUDE"):
            monkeypatch.setattr(settings, key, values[key])

    return configure


@pytest.mark.parametrize("variable", ["PARALLAX_CRAWL_OUTLETS", "PARALLAX_CRAWL_EXCLUDE"])
@pytest.mark.parametrize("value", [None, "", "  ", " , , "])
def test_empty_or_unset_selection_is_not_set(monkeypatch, variable, value):
    monkeypatch.delenv(variable, raising=False)
    if value is not None:
        monkeypatch.setenv(variable, value)
    # Ignore any local .env so this also exercises truly unset settings.
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "")
    values = runpy.run_path(settings.__file__)
    assert values[variable.removeprefix("PARALLAX_")] == ()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("mode", ["include", "exclude"])
def test_host_selection_crawls_only_selected_adapters(
    offline, configure, monkeypatch, caplog, mode, dry_run
):
    configure(**{mode: " chinatimes, ftv, chinatimes , "})
    if dry_run:
        monkeypatch.setattr(settings, "HEARTBEAT_URL", PING_URL)
    caplog.set_level(logging.INFO)
    chosen = {"chinatimes", "ftv"}
    expected = [
        cfg.code
        for cfg in offline.outlets
        if cfg.verified and ((cfg.code in chosen) == (mode == "include"))
    ]
    variable = "PARALLAX_CRAWL_OUTLETS" if mode == "include" else "PARALLAX_CRAWL_EXCLUDE"

    assert job.main(["--dry-run"] if dry_run else []) == 0
    assert offline.fetched == expected
    announcements = [r.getMessage() for r in caplog.records if "on this host" in r.getMessage()]
    assert announcements == [
        (
            f"crawling {len(expected)} of 8 verified outlets on this host "
            f"({variable}: chinatimes, ftv, chinatimes)"
        )
    ]
    requests.get.assert_not_called()
    if dry_run:
        listing.db.connect.assert_not_called()
        job.wait_for_network.assert_not_called()
    else:
        assert [r.outlet for r in offline.recorded] == expected
        assert all(r.ok for r in offline.recorded)


@pytest.mark.parametrize("dry_run", [False, True])
def test_no_host_selection_still_crawls_all_verified_outlets(offline, caplog, dry_run):
    caplog.set_level(logging.INFO)

    assert job.main(["--dry-run"] if dry_run else []) == 0
    assert offline.fetched == [cfg.code for cfg in offline.outlets if cfg.verified]
    assert "crawling 8 of 8 verified outlets on this host (all verified outlets)" in caplog.text


def test_exclusion_includes_new_verified_outlets_and_allows_unverified_names(offline, configure):
    offline.outlets.append(replace(offline.outlets[0], code="new"))
    configure(exclude="chinatimes,ftv,pending")

    assert job.main([]) == 0
    assert offline.fetched == [
        cfg.code
        for cfg in offline.outlets
        if cfg.verified and cfg.code not in {"chinatimes", "ftv"}
    ]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "include,exclude,messages",
    [
        ("ftv", "chinatimes", ["PARALLAX_CRAWL_OUTLETS", "PARALLAX_CRAWL_EXCLUDE"]),
        ("cna,typo,another", "", ["PARALLAX_CRAWL_OUTLETS", "typo", "another"]),
        ("", "chinatimes,typo", ["PARALLAX_CRAWL_EXCLUDE", "typo"]),
    ],
)
def test_bad_config_fails_before_any_work(
    offline, configure, caplog, dry_run, include, exclude, messages
):
    configure(include, exclude)

    assert job.main(["--dry-run"] if dry_run else []) == 2
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert all(message in errors[0] for message in messages)
    assert offline.fetched == []
    job.wait_for_network.assert_not_called()
    listing._build_fetcher.assert_not_called()
    listing.db.connect.assert_not_called()
    requests.get.assert_not_called()


@pytest.mark.parametrize("dry_run", [False, True])
def test_explicit_unverified_env_outlet_raises(offline, configure, dry_run):
    configure(include="cna,pending")

    with pytest.raises(UnverifiedOutletError, match="pending"):
        job.main(["--dry-run"] if dry_run else [])
    assert offline.fetched == []
    job.wait_for_network.assert_not_called()
    listing._build_fetcher.assert_not_called()
    listing.db.connect.assert_not_called()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("include,exclude", [("ftv", ""), ("", "cna"), ("typo,pending", "cna")])
def test_manual_outlet_overrides_host_configuration(
    offline, configure, monkeypatch, dry_run, include, exclude
):
    configure(include, exclude)
    monkeypatch.setattr(settings, "HEARTBEAT_URL", PING_URL)
    args = ["--outlet", "cna"] + (["--dry-run"] if dry_run else [])

    assert job.main(args) == 0
    assert offline.fetched == ["cna"]
    requests.get.assert_not_called()


@pytest.mark.parametrize("selection", ["cna", ["cna", "ftv"], {"cna", "ftv"}, (), None])
@pytest.mark.parametrize("crawl", [listing.crawl_all, listing.crawl_dry_run])
def test_crawl_api_accepts_single_collection_empty_or_bulk_selection(offline, selection, crawl):
    crawl(only=selection)

    selected = {selection} if isinstance(selection, str) else selection
    assert offline.fetched == [
        cfg.code
        for cfg in offline.outlets
        if cfg.verified and (selected is None or cfg.code in selected)
    ]


@pytest.mark.parametrize("crawl", [listing.crawl_all, listing.crawl_dry_run])
@pytest.mark.parametrize("code,error", [("pending", UnverifiedOutletError), ("typo", ValueError)])
def test_crawl_api_validates_entire_selection_before_work(offline, crawl, code, error):
    with pytest.raises(error, match=code):
        crawl(only=["cna", code])
    assert offline.fetched == []
    listing._build_fetcher.assert_not_called()
    listing.db.connect.assert_not_called()


@pytest.mark.parametrize("dry_run", [False, True])
def test_excluding_every_verified_outlet_does_not_fall_back_to_all(offline, configure, dry_run):
    configure(exclude=",".join(cfg.code for cfg in offline.outlets if cfg.verified))

    assert job.main(["--dry-run"] if dry_run else []) == (0 if dry_run else 2)
    assert offline.fetched == []
    assert offline.recorded == []
    requests.get.assert_not_called()


@pytest.mark.parametrize("mode", ["include", "exclude"])
@pytest.mark.parametrize("failed", [False, True])
def test_scheduled_subset_pings_its_outcome_after_crawl_returns(
    offline, configure, monkeypatch, mode, failed
):
    """Extend T-033's post-crawl heartbeat guarantee to scheduled subsets."""
    configure(**{mode: "chinatimes,ftv"})
    expected = [
        cfg.code
        for cfg in offline.outlets
        if cfg.verified and ((cfg.code in {"chinatimes", "ftv"}) == (mode == "include"))
    ]
    if failed:
        offline.failed.add(expected[0])
    monkeypatch.setattr(settings, "HEARTBEAT_URL", PING_URL)
    response = Mock()

    def get(url, *, timeout):
        assert offline.fetched == expected
        assert [r.outlet for r in offline.recorded] == expected
        assert url == PING_URL + ("/fail" if failed else "")
        assert timeout == (5, 5)
        return response

    monkeypatch.setattr(requests, "get", Mock(side_effect=get))

    assert job.main([]) == int(failed)
    requests.get.assert_called_once()
    response.raise_for_status.assert_called_once_with()

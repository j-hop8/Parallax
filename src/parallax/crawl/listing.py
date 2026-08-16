from __future__ import annotations

import logging

from .. import db
from ..config import load_outlets
from ..models import CrawlResult
from ..urls import canonicalize
from .adapters.base import build_adapter
from .http import Fetcher

log = logging.getLogger(__name__)


def crawl_dry_run(only: str | None = None) -> list[CrawlResult]:
    """Fetch every adapter and report counts without touching the database.

    Exists so adapters can be developed and verified before Postgres is
    reachable -- and afterwards, to test a new selector without writing rows.
    """
    defaults, outlets = load_outlets()
    fetcher = _build_fetcher(defaults)

    results: list[CrawlResult] = []
    for cfg in outlets:
        if only and cfg.code != only:
            continue
        result = CrawlResult(outlet=cfg.code)
        if not cfg.verified:
            result.error = "not verified (no working adapter yet)"
            results.append(result)
            continue
        try:
            stubs = build_adapter(cfg, fetcher).fetch()
            result.items_seen = len(stubs)
            # Distinct canonical URLs: the gap versus items_seen is how much
            # overlap the outlet's section feeds have with each other.
            result.items_new = len({canonicalize(s.url_original) for s in stubs})
            result.ok = True
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        results.append(result)
    return results


def _build_fetcher(defaults: dict) -> Fetcher:
    return Fetcher(
        user_agent=defaults.get("user_agent", "ParallaxResearchBot/0.1"),
        timeout=float(defaults.get("timeout_seconds", 20)),
        min_delay=float(defaults.get("rate_limit_seconds", 2.0)),
    )


def crawl_all(only: str | None = None) -> list[CrawlResult]:
    """Run the tier-1 listing crawl across every configured outlet.

    One outlet failing must never abort the run: a markup change at one site
    would otherwise cost us that cycle's data at all eight, and tier-1 data
    cannot be backfilled once a feed rolls over. Each outlet is therefore
    isolated, recorded, and committed independently.
    """
    defaults, outlets = load_outlets()
    fetcher = _build_fetcher(defaults)

    results: list[CrawlResult] = []
    with db.connect() as conn:
        db.ensure_outlets(conn, outlets)
        conn.commit()

        for cfg in outlets:
            if only and cfg.code != only:
                continue

            if not cfg.verified:
                # Skipped, not failed: there is no adapter for it yet. Crawling
                # with a guessed URL would look like a working adapter quietly
                # returning nothing, which is the failure mode we most want to
                # be able to see.
                log.warning("skipping %s: not verified (no working adapter yet)", cfg.code)
                continue

            result = CrawlResult(outlet=cfg.code)
            try:
                stubs = build_adapter(cfg, fetcher).fetch()
                result.items_seen, result.items_new = db.upsert_article_index(conn, stubs)
                result.ok = True
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"[:2000]
                log.exception("crawl failed for %s", cfg.code)
                # The connection may be in an aborted transaction; clear it so the
                # crawl_runs insert below can still record the failure.
                conn.rollback()

            db.record_crawl_run(conn, result)
            conn.commit()
            results.append(result)

    return results

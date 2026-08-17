from __future__ import annotations

import logging

from .. import db
from ..config import UnverifiedOutletError, load_outlets
from ..models import CrawlResult, OutletConfig
from ..urls import canonicalize
from .adapters.base import PartialFetchError, build_adapter
from .http import Fetcher

log = logging.getLogger(__name__)


def crawl_dry_run(only: str | None = None) -> list[CrawlResult]:
    """Fetch every adapter and report counts without touching the database.

    Exists so adapters can be developed and verified before Postgres is
    reachable -- and afterwards, to test a new selector without writing rows.
    """
    defaults, outlets = load_outlets()
    _check_explicit_target(outlets, only)
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
        except PartialFetchError as exc:
            stubs = exc.stubs
            result.error = f"PartialFetchError: {exc}"
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
            results.append(result)
            continue

        result.items_seen = len(stubs)
        # Distinct canonical URLs: the gap versus items_seen is how much
        # overlap the outlet's section feeds have with each other.
        result.items_new = len({canonicalize(s.url_original) for s in stubs})
        result.ok = result.error is None and result.items_seen > 0
        results.append(result)
    return results


def _check_explicit_target(outlets: list[OutletConfig], only: str | None) -> None:
    """Fail loudly when an operator names an outlet that has no working adapter.

    The bulk crawl deliberately skips unverified outlets rather than aborting --
    blocking seven healthy outlets over one broken adapter would lose tier-1 data
    that cannot be recovered. But asking for one *by name* is a different act: a
    silent skip there looks exactly like a successful crawl of an outlet that is
    in fact uncollected, so it raises instead.
    """
    if not only:
        return
    target = next((o for o in outlets if o.code == only), None)
    if target is None:
        raise ValueError(f"unknown outlet {only!r}; check config/outlets.yaml")
    if not target.verified:
        raise UnverifiedOutletError(
            f"{only}: not verified -- no working adapter. Run scripts/audit_feeds.py, "
            f"record the resolved parser/pattern in config/outlets.yaml, and set verified: true."
        )


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
    _check_explicit_target(outlets, only)
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
            except PartialFetchError as exc:
                # Store what did arrive -- those articles are real and cannot be
                # re-fetched -- but the run is degraded, so ok stays False and
                # the day it touches will not qualify as a complete denominator.
                result.items_seen, result.items_new = db.upsert_article_index(conn, exc.stubs)
                result.error = f"PartialFetchError: {exc}"[:2000]
                log.warning("partial crawl for %s: %s", cfg.code, exc)
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"[:2000]
                log.exception("crawl failed for %s", cfg.code)
                # The connection may be in an aborted transaction; clear it so the
                # crawl_runs insert below can still record the failure.
                conn.rollback()
            else:
                result.items_seen, result.items_new = db.upsert_article_index(conn, stubs)
                # Zero items from a verified outlet is a broken selector or a
                # dead feed, never a quiet 20 minutes -- a listing always returns
                # its current window regardless of how much is new. Recording it
                # as ok would let a silently dead adapter satisfy rollup
                # completeness while articles are lost for good.
                result.ok = result.items_seen > 0
                if not result.ok:
                    result.error = "adapter returned 0 items -- selector or feed likely broken"
                    log.error("%s returned 0 items", cfg.code)

            db.record_crawl_run(conn, result)
            conn.commit()
            results.append(result)

    return results

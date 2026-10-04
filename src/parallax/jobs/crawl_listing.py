from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Collection

import requests

from .. import settings
from ..config import load_outlets
from ..crawl.http import wait_for_network
from ..crawl.listing import _check_explicit_target, crawl_all, crawl_dry_run

log = logging.getLogger(__name__)

HEARTBEAT_ATTEMPTS = 2
HEARTBEAT_TIMEOUT = 5


def _host_selection(outlet: str | None) -> str | Collection[str] | None:
    """Validate and describe this host's selection before any network or DB work."""
    if outlet is None and settings.CRAWL_OUTLETS and settings.CRAWL_EXCLUDE:
        raise ValueError("PARALLAX_CRAWL_OUTLETS and PARALLAX_CRAWL_EXCLUDE cannot both be set")

    _, outlets = load_outlets()
    verified = {cfg.code for cfg in outlets if cfg.verified}
    only: str | Collection[str] | None = outlet
    source = f"--outlet: {outlet}" if outlet is not None else "all verified outlets"
    if outlet is None and (settings.CRAWL_OUTLETS or settings.CRAWL_EXCLUDE):
        codes = settings.CRAWL_OUTLETS or settings.CRAWL_EXCLUDE
        variable = "PARALLAX_CRAWL_OUTLETS" if settings.CRAWL_OUTLETS else "PARALLAX_CRAWL_EXCLUDE"
        unknown = set(codes) - {cfg.code for cfg in outlets}
        if unknown:
            raise ValueError(
                f"{variable}: unknown outlet(s) {', '.join(sorted(unknown))}; "
                "check config/outlets.yaml"
            )
        source = f"{variable}: {', '.join(codes)}"
        if settings.CRAWL_OUTLETS:
            only = codes
        else:
            only = tuple(cfg.code for cfg in outlets if cfg.verified and cfg.code not in codes)

    _check_explicit_target(outlets, only)
    selected = (only,) if isinstance(only, str) else only
    count = len(verified if selected is None else verified.intersection(selected))
    log.info(
        "crawling %d of %d verified outlets on this host (%s)",
        count,
        len(verified),
        source,
    )
    return only


def _ping_heartbeat(*, failed: bool) -> None:
    url = settings.HEARTBEAT_URL
    if not url:
        return
    if failed:
        url = url.rstrip("/") + "/fail"

    # urllib3's verbose request logs include the secret ping path. Suppress
    # those only during the ping, then restore the crawl's logging policy.
    transport_log = logging.getLogger("urllib3.connectionpool")
    was_disabled = transport_log.disabled
    transport_log.disabled = True
    try:
        for _ in range(HEARTBEAT_ATTEMPTS):
            try:
                requests.get(
                    url, timeout=(HEARTBEAT_TIMEOUT, HEARTBEAT_TIMEOUT)
                ).raise_for_status()
                return
            except Exception as exc:  # noqa: BLE001 -- a ping must never abort the crawl
                # Exception messages and tracebacks can contain the URL too.
                log.warning("heartbeat ping failed: %s", type(exc).__name__)
    finally:
        transport_log.disabled = was_disabled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tier-1 listing crawl (metadata only).")
    parser.add_argument("--outlet", help="Run a single outlet by code, e.g. cna")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report counts without writing to the database.",
    )
    parser.add_argument(
        "--wait-network",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="wait up to SECONDS for connectivity before crawling (0 disables)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        only = _host_selection(args.outlet)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    if not args.dry_run and args.wait_network:
        # The scheduler can fire during a dark wake, before Wi-Fi re-associates.
        # Waiting here is free when the network is up and saves the whole cycle
        # when it is not -- otherwise the first feeds in config order are spent
        # on a dead interface.
        wait_for_network(timeout=args.wait_network)

    if args.dry_run:
        results = crawl_dry_run(only=only)
        for r in results:
            if r.ok:
                log.info(
                    "%-11s fetched=%-4d distinct_urls=%-4d", r.outlet, r.items_seen, r.items_new
                )
            else:
                log.warning("%-11s %s", r.outlet, r.error)
        return 0

    results = crawl_all(only=only)
    if not results:
        log.error("no outlets matched %r", only)
        return 2

    for r in results:
        if r.ok:
            log.info("%-11s seen=%-4d new=%-4d", r.outlet, r.items_seen, r.items_new)
        else:
            log.error("%-11s FAILED %s", r.outlet, r.error)

    failed = [r.outlet for r in results if not r.ok]
    empty = [r.outlet for r in results if r.ok and r.items_seen == 0]
    if empty:
        # Not an error exit -- an outlet can legitimately publish nothing in a
        # 10-minute window -- but it is the signature of a broken selector, so
        # it gets said out loud rather than buried in the counts.
        log.warning("returned zero items: %s", ", ".join(empty))

    if args.outlet is None:
        _ping_heartbeat(failed=bool(failed))

    # Non-zero exit so cron mail / log greps surface a persistent breakage.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

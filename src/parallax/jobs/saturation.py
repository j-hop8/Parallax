"""Report feed saturation; only accounting anomalies fail the metric check."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import db
from ..metrics.saturation import AT_RISK, MIN_USABLE_RUNS, saturation
from ..settings import TIMEZONE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.days <= 0:
        parser.error("--days must be positive")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    tz = ZoneInfo(TIMEZONE)
    since = datetime.now(tz) - timedelta(days=args.days)
    with db.connect() as conn:
        rows = db.crawl_runs_window(conn, since)
    logging.getLogger(__name__).debug("Read %d rows including predecessor context", len(rows))
    results = saturation(rows, since=since)
    for result in results:
        if result.median is None:
            estimates = f"insufficient data (need {MIN_USABLE_RUNS} usable runs)"
        else:
            worst, timestamp = result.worst
            estimates = (
                f"median={result.median:.1%} worst={worst:.1%} "
                f"at {timestamp.astimezone(tz):%Y-%m-%d %H:%M:%S %Z} "
                f"({TIMEZONE})"
            )
            turnover = result.fastest_turnover_minutes
            estimates += (
                f" fastest_turnover={turnover:.1f} min" if turnover is not None
                else " fastest_turnover=unavailable (no new items)"
            )
        exclusions = ",".join(f"{key}={value}" for key, value in result.excluded.items())
        print(
            f"{result.outlet}: runs={result.runs} at_risk={result.at_risk} {estimates} "
            f"excluded[{exclusions}] anomalies={result.anomalies}"
        )
    if not results:
        print("No crawl runs in the reporting window; insufficient data.")
    print(
        f"Saturation = new/seen; at risk >= {AT_RISK:.0%} (reported, never gated). "
        "Turnover = gap/saturation; fastest is the minimum observed minutes.\n"
        "Exclusions: failed, first successful run, zero seen, anomaly, nonpositive gap, "
        "outage (>3x median successful gap). Pre-window context is not counted."
    )
    anomalies = sum(result.anomalies for result in results)
    if anomalies:
        print(f"DATA-INTEGRITY WARNING: {anomalies} runs have items_new > items_seen.")
    return int(bool(anomalies))


if __name__ == "__main__":
    sys.exit(main())

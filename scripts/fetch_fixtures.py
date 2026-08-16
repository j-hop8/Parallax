#!/usr/bin/env python
"""Refresh the saved listing fixtures used by tests/test_adapters.py.

Run this when an outlet redesigns and its adapter test starts failing. Review the
diff before committing: a fixture refresh that silently drops the article count
is the redesign the tests exist to catch, not a reason to re-baseline.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from parallax.config import load_outlets
from parallax.crawl.http import Fetcher

OUT = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    defaults, outlets = load_outlets()
    fetcher = Fetcher(defaults["user_agent"], timeout=25, min_delay=2.0)
    OUT.mkdir(parents=True, exist_ok=True)

    for outlet in outlets:
        if outlet.parser == "rss" or not outlet.listing_url:
            continue
        if only and outlet.code != only:
            continue
        try:
            html = fetcher.get_text(outlet.listing_url)
            path = OUT / f"{outlet.code}_listing.html"
            path.write_text(html, encoding="utf-8")
            print(f"  {outlet.code:<11} {len(html):>8} bytes  <- {outlet.listing_url}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {outlet.code:<11} ERR {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

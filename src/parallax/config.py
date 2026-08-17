from __future__ import annotations

from pathlib import Path

import yaml

from .models import OutletConfig
from .settings import OUTLETS_YAML


class UnverifiedOutletError(RuntimeError):
    """Raised when a crawl is attempted against an outlet the audit hasn't cleared."""


def load_raw(path: Path | None = None) -> dict:
    with open(path or OUTLETS_YAML, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_outlets(path: Path | None = None) -> tuple[dict, list[OutletConfig]]:
    """Read outlets.yaml into typed configs -- all of them, verified or not.

    Verification is enforced by the caller, not here. Refusing to load the whole
    file because one outlet still lacks a working adapter would stop the other
    seven from crawling, and tier-1 data missed during that outage cannot be
    recovered. crawl_all() skips unverified outlets and says so.
    """
    raw = load_raw(path)
    defaults = raw.get("defaults", {})
    configs: list[OutletConfig] = []

    for entry in raw.get("outlets", []):
        verified = bool(entry.get("verified", False))
        feeds = entry.get("feed_urls") or ([entry["feed_url"]] if entry.get("feed_url") else [])
        configs.append(
            OutletConfig(
                code=entry["code"],
                name_zh=entry["name_zh"],
                home_url=entry["home_url"],
                feed_urls=tuple(feeds),
                has_dates=bool(entry.get("has_dates", True)),
                parser=entry.get("parser", "rss"),
                rate_limit_seconds=float(
                    entry.get("rate_limit_seconds", defaults.get("rate_limit_seconds", 2.0))
                ),
                verified=verified,
                listing_url=entry.get("listing_url"),
                article_url_pattern=entry.get("article_url_pattern"),
                title_datetime_pattern=entry.get("title_datetime_pattern"),
                strip_trailing_time=bool(entry.get("strip_trailing_time", False)),
                timestamp_offset_is_wrong=bool(entry.get("timestamp_offset_is_wrong", False)),
            )
        )
    return defaults, configs

#!/usr/bin/env python
"""Probe a wider candidate list for whole-outlet ("realtime"/"all") feeds.

Follow-up to the first audit, which took the first feed that parsed and so
settled on *section* feeds for some outlets. A section feed is the wrong
denominator for coverage weight: dividing incident articles by an outlet's
社會-only output overstates its editorial priority. What we want per outlet is
either a whole-site realtime feed, or enough section feeds to approximate one.
"""
from __future__ import annotations

import sys

import feedparser
import requests

UA = "ParallaxResearchBot/0.1 (media-coverage research; contact: j8211080@gmail.com)"

CANDIDATES: dict[str, list[str]] = {
    "cna": [
        "https://feeds.feedburner.com/rsscna/realtimenews",
        "https://feeds.feedburner.com/rsscna/intworld",
        "https://feeds.feedburner.com/rsscna/politics",
        "https://feeds.feedburner.com/rsscna/social",
        "https://feeds.feedburner.com/rsscna/finance",
        "https://feeds.feedburner.com/rsscna/lifehealth",
    ],
    "udn": [
        "https://udn.com/rssfeed/news/1?ch=news",
        "https://udn.com/rssfeed/news/2/6638?ch=news",
        "https://udn.com/rssfeed/news/2/6639?ch=news",
        "https://udn.com/rssfeed/news/2/6640?ch=news",
        "https://udn.com/rssfeed/news/2/7225?ch=news",
    ],
    "chinatimes": [
        "https://www.chinatimes.com/rss/realtimenews.xml",
        "https://www.chinatimes.com/rss/RealtimeNews.xml",
        "https://www.chinatimes.com/rss/politic.xml",
        "https://www.chinatimes.com/rss/society.xml",
    ],
    "ltn": [
        "https://news.ltn.com.tw/rss/all.xml",
    ],
    "ettoday": [
        "https://feeds.feedburner.com/ettoday/realtime",
        "https://www.ettoday.net/rss/rss-all.xml",
    ],
    "setn": [
        "https://www.setn.com/rss.aspx?ProjectID=1",
        "https://www.setn.com/Rss.aspx",
    ],
    "tvbs": [
        "https://news.tvbs.com.tw/rss/realtime",
        "https://news.tvbs.com.tw/rss/rss.xml",
    ],
    "ftv": [
        "https://www.ftvnews.com.tw/rss/realtime.xml",
        "https://www.ftvnews.com.tw/api/rss",
    ],
}


def probe(url: str) -> str:
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": UA})
        if r.status_code >= 400:
            return f"HTTP {r.status_code}"
        p = feedparser.parse(r.content)
        n = len(p.entries or [])
        if not n:
            return "empty"
        dated = sum(1 for e in p.entries if e.get("published_parsed") or e.get("updated_parsed"))
        return f"OK  entries={n:<4} dated={dated}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}"


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for outlet, urls in CANDIDATES.items():
        if only and outlet != only:
            continue
        print(f"\n=== {outlet} ===")
        for u in urls:
            print(f"  {probe(u):<28} {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

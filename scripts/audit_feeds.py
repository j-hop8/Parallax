#!/usr/bin/env python
"""T-002: probe each outlet's candidate feeds and robots.txt.

Answers three questions per outlet, which together decide whether it can use the
cheap RSS path or needs a fragile HTML listing parser:

  1. Does a candidate feed URL actually return a parseable feed with entries?
  2. Do those entries carry usable publish dates? (If not, effective_at falls
     back to seen_at -- workable, but worth knowing up front.)
  3. Does robots.txt permit our User-Agent on that path?

Prints a report plus a YAML fragment to paste into config/outlets.yaml. It does
not rewrite the file itself, because that file carries comments worth keeping.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import feedparser
import requests

from parallax.config import load_raw

TIMEOUT = 20


def check_robots(url: str, user_agent: str) -> tuple[bool, str]:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = RobotFileParser()
    try:
        resp = requests.get(robots_url, timeout=TIMEOUT, headers={"User-Agent": user_agent})
        if resp.status_code >= 400:
            # No robots.txt is not permission, but it is the absence of a
            # prohibition; flagged so it can be reviewed by hand.
            return True, f"no robots.txt (HTTP {resp.status_code})"
        parser.parse(resp.text.splitlines())
        allowed = parser.can_fetch(user_agent, url)
        return allowed, "allowed" if allowed else "DISALLOWED by robots.txt"
    except Exception as exc:  # noqa: BLE001
        return False, f"robots check failed: {type(exc).__name__}: {exc}"


def probe_feed(url: str, user_agent: str) -> dict:
    out = {"url": url, "ok": False, "entries": 0, "with_dates": 0, "note": ""}
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": user_agent})
        out["status"] = resp.status_code
        if resp.status_code >= 400:
            out["note"] = f"HTTP {resp.status_code}"
            return out
        parsed = feedparser.parse(resp.content)
        entries = parsed.entries or []
        out["entries"] = len(entries)

        # Counting entries is not enough. UDN serves 20 well-formed <item>
        # elements with empty <title>, empty <link> and a 1970 pubDate -- a feed
        # that looks healthy by entry count and captures nothing. An adapter
        # built on that would fail silently, which is the exact failure mode
        # this project cannot tolerate in tier 1.
        usable = [
            e for e in entries
            if (e.get("link") or "").strip() and (e.get("title") or "").strip()
        ]
        out["usable"] = len(usable)
        out["with_dates"] = sum(
            1
            for e in usable
            if (e.get("published_parsed") or e.get("updated_parsed"))
            # Epoch means "no date", dressed up as one.
            and (e.get("published_parsed") or e.get("updated_parsed"))[0] > 1971
        )

        if not entries:
            out["note"] = f"parsed but empty (bozo={getattr(parsed, 'bozo', '?')})"
            return out
        if not usable:
            out["note"] = f"HOLLOW: {len(entries)} items, all with empty link/title"
            return out
        out["ok"] = True
        out["note"] = f"ok ({len(usable)}/{len(entries)} usable)"
    except Exception as exc:  # noqa: BLE001
        out["note"] = f"{type(exc).__name__}: {exc}"
    return out


def main() -> int:
    raw = load_raw()
    defaults = raw.get("defaults", {})
    user_agent = defaults.get("user_agent", "ParallaxResearchBot/0.1")

    resolved: list[dict] = []
    for entry in raw.get("outlets", []):
        code, name = entry["code"], entry["name_zh"]
        print(f"\n=== {code}  {name} ===")

        winner = None
        for candidate in entry.get("feed_candidates", []):
            result = probe_feed(candidate, user_agent)
            flag = "OK " if result["ok"] else "-- "
            print(
                f"  {flag}{candidate}\n"
                f"      entries={result['entries']} with_dates={result['with_dates']} "
                f"({result['note']})"
            )
            if result["ok"] and winner is None:
                winner = result

        if winner:
            allowed, note = check_robots(winner["url"], user_agent)
            print(f"  robots: {note}")
            resolved.append(
                {
                    "code": code,
                    "parser": "rss",
                    "feed_url": winner["url"],
                    "robots_ok": allowed,
                    "has_dates": winner["with_dates"] > 0,
                    "verified": bool(allowed),
                }
            )
        else:
            allowed, note = check_robots(entry["home_url"], user_agent)
            print(f"  NO USABLE FEED -> needs an HTML listing parser. robots: {note}")
            resolved.append(
                {
                    "code": code,
                    "parser": "html",
                    "feed_url": None,
                    "robots_ok": allowed,
                    "has_dates": False,
                    "verified": False,
                }
            )

    print("\n\n--- summary ---")
    rss = [r for r in resolved if r["parser"] == "rss"]
    html = [r for r in resolved if r["parser"] == "html"]
    blocked = [r for r in resolved if not r["robots_ok"]]
    nodates = [r for r in rss if not r["has_dates"]]
    print(f"  usable RSS:        {len(rss)}/{len(resolved)}  {[r['code'] for r in rss]}")
    print(f"  need HTML parser:  {len(html)}  {[r['code'] for r in html]}")
    print(f"  robots-blocked:    {len(blocked)}  {[r['code'] for r in blocked]}")
    print(f"  RSS without dates: {len(nodates)}  {[r['code'] for r in nodates]}")

    print("\n--- paste into config/outlets.yaml ---")
    for r in resolved:
        print(f"  # {r['code']}")
        print(f"    parser: {r['parser']}")
        if r["feed_url"]:
            print(f"    feed_url: {r['feed_url']}")
        print(f"    robots_ok: {str(r['robots_ok']).lower()}")
        print(f"    has_dates: {str(r['has_dates']).lower()}")
        print(f"    verified: {str(r['verified']).lower()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

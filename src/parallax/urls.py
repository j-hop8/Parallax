from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Params that identify a referrer or campaign, never the article itself.
_TRACKING_KEYS = {
    "fbclid", "gclid", "yclid", "msclkid",
    "from", "source", "ref", "share", "share_source",
    "amp", "at_medium", "at_campaign", "_gl",
}
_TRACKING_PREFIXES = ("utm_",)

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in _TRACKING_KEYS or lowered.startswith(_TRACKING_PREFIXES)


def canonicalize(url: str) -> str:
    """Reduce a URL to a stable dedup key.

    This is only ever a key. Fetching always uses the original URL, which is why
    article_index keeps both columns -- dropping "www." here would break some
    hosts if we fetched the canonical form.
    """
    parts = urlsplit(url.strip())

    host = parts.hostname or ""
    if not host:
        # Nothing sensible to key on; hand the input back rather than emit
        # something like "https:///path" that would collide across outlets.
        return url.strip()
    host = host.removeprefix("www.")

    # The key is always https. Feeds emit both schemes for the same article and
    # every one of these outlets redirects http -> https, so keeping the scheme
    # would split one article into two rows across polls.
    scheme = "https"

    port = parts.port
    if port is not None and str(port) != _DEFAULT_PORTS.get(parts.scheme.lower(), "443"):
        host = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)]
    query = urlencode(sorted(kept))

    # Fragments never identify a distinct article.
    return urlunsplit((scheme, host, path, query, ""))

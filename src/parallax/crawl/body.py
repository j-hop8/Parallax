from __future__ import annotations

import gzip
import hashlib
import logging
import os
import tempfile
import zlib
from datetime import datetime
from pathlib import Path

from ..settings import RAW_DIR
from .http import Fetcher

log = logging.getLogger(__name__)


def cache_path(outlet: str, url_canonical: str, day: datetime) -> Path:
    """Content-addressed location for one article's raw HTML.

    Keyed on the canonical URL so the same article always lands in the same file,
    and foldered by the article's own day rather than the fetch date so the path
    is derivable from the database row alone -- no stored path needed to find it
    again after a crash.
    """
    digest = hashlib.sha256(url_canonical.encode("utf-8")).hexdigest()
    return RAW_DIR / outlet / day.strftime("%Y-%m-%d") / f"{digest}.html.gz"


def read_cached(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, EOFError, zlib.error, UnicodeDecodeError) as exc:
        # A truncated file from an interrupted write is not fatal: treat it as a
        # miss and re-fetch, rather than failing the run.
        #
        # zlib.error is listed explicitly because it is NOT an OSError. A file
        # with a valid gzip header and a corrupt deflate stream -- exactly what a
        # killed write leaves behind -- raises it, so catching only OSError and
        # EOFError let a bad cache entry take down the enrich batch.
        log.warning("discarding unreadable cache entry %s: %s", path, exc)
        return None


def write_cache(path: Path, html: str) -> None:
    """Write gzipped HTML atomically.

    The temp-then-rename matters because enrich runs are interrupted often on
    this host: a half-written .gz would otherwise be indistinguishable from a
    good one on the next run.

    The temp name is unique per writer rather than derived from the target. Two
    enrich runs for different keywords can legitimately match the same article,
    and Airflow will eventually launch them concurrently. With a shared
    `<sha>.tmp` the content is never corrupted -- rename is atomic -- but the
    second writer's rename fails with FileNotFoundError because the first already
    moved that temp away. Measured: 4 threads x 5 writes produced 3 such errors.
    In enrich that surfaces as a perfectly good article marked failed and
    re-fetched later, so it costs an outlet request for nothing.

    With a unique temp per writer both renames succeed and land identical
    content, making last-writer-wins genuinely harmless.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            fh.write(html)
        tmp.replace(path)
    except BaseException:
        # Never leave a stray temp behind, including on KeyboardInterrupt --
        # which is how these runs usually end.
        tmp.unlink(missing_ok=True)
        raise


def fetch_html(
    fetcher: Fetcher,
    outlet: str,
    url_original: str,
    url_canonical: str,
    day: datetime,
    *,
    refetch: bool = False,
) -> tuple[str, Path, bool]:
    """Return (html, cache_path, came_from_cache).

    Cache-first by design. Every parser fix in this project re-runs over articles
    we have already fetched, and re-requesting thousands of pages from eight news
    sites to correct our own selector bug would be both slow and rude. The cache
    means a body is fetched from an outlet exactly once.
    """
    path = cache_path(outlet, url_canonical, day)

    if not refetch:
        cached = read_cached(path)
        if cached is not None:
            return cached, path, True

    html = fetcher.get_text(url_original)
    write_cache(path, html)
    return html, path, False

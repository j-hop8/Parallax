from __future__ import annotations

import logging
from functools import cache
from pathlib import Path

import jieba

from ..settings import BIG_DICT, USERDICT

log = logging.getLogger(__name__)


@cache
def _ready() -> bool:
    """Load the traditional-Chinese dictionary and our custom terms, once.

    jieba's default dictionary is simplified-oriented. dict.txt.big covers
    traditional forms and ships in some distributions but not all, so its
    absence is a logged warning rather than a crash -- degraded segmentation
    still produces a working index.
    """
    # The PyPI wheel does not ship extra_dict/, so the traditional dictionary is
    # fetched into config/ by `make dict`. Prefer that; fall back to a copy
    # inside the installed package if one exists.
    candidates = [
        BIG_DICT,
        Path(jieba.__file__).parent / "extra_dict" / "dict.txt.big",
    ]
    for path in candidates:
        if path.exists():
            jieba.set_dictionary(str(path))
            break
    else:
        log.warning(
            "dict.txt.big not found (looked in %s); falling back to jieba's default "
            "simplified-oriented dictionary. Traditional-Chinese segmentation will be "
            "worse, which degrades keyword search and dedup. Run `make dict`.",
            ", ".join(str(p) for p in candidates),
        )

    if USERDICT.exists():
        jieba.load_userdict(str(USERDICT))
    return True


def segment_text(text: str | None) -> str | None:
    """Space-join jieba tokens so Postgres' 'simple' FTS config can index CJK.

    Stock Postgres has no Chinese parser, so the text must arrive pre-tokenised.
    Queries must be segmented through this same function or they will not match.
    """
    if not text:
        return None
    _ready()
    tokens = [t for t in jieba.cut(text, cut_all=False) if t.strip()]
    return " ".join(tokens)

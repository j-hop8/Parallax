"""The hand-labeled stance gold set: eval/stance_gold.csv.

Committed to the repo, because it is hand-made and irreplaceable (the proposal
budgets ~2 days of human labeling for it). Everything that reads or writes it
goes through here so the labeling CLI and the eval job agree on the columns.

The interactive loop lives here too, with input/output injected, so the part
of the tool most likely to lose someone's afternoon of labels is covered by
tests rather than tried once by hand.
"""

from __future__ import annotations

import csv
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..settings import EVAL_DIR
from .stance import LABELS, lede

GOLD_PATH = EVAL_DIR / "stance_gold.csv"
COLUMNS = ("article_id", "outlet", "url", "target", "label", "annotator", "labeled_at", "note")

KEYS = {"n": "neg", "e": "neu", "p": "pos"}
BODY_PREVIEW_CHARS = 3000


@dataclass(frozen=True)
class GoldRow:
    article_id: int
    outlet: str
    url: str
    target: str
    label: str
    annotator: str
    labeled_at: str
    note: str = ""


def load_gold(path: Path = GOLD_PATH) -> list[GoldRow]:
    """All rows; a missing file is an empty set, not an error."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = []
        for r in reader:
            if r["label"] not in LABELS:
                raise ValueError(
                    f"{path.name}: bad label {r['label']!r} for article {r['article_id']}"
                )
            rows.append(
                GoldRow(
                    article_id=int(r["article_id"]),
                    outlet=r["outlet"],
                    url=r["url"],
                    target=r["target"],
                    label=r["label"],
                    annotator=r.get("annotator", ""),
                    labeled_at=r.get("labeled_at", ""),
                    note=r.get("note", "") or "",
                )
            )
        return rows


def append_gold(path: Path, row: GoldRow) -> None:
    """Append one row, writing the header if the file is new. Flushes per row:
    a labeling session interrupted at row 40 must keep rows 1-39."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow(row.__dict__)
        fh.flush()


def pending(
    articles: Iterable[dict], gold: Iterable[GoldRow], target: str, seed: int | None = None
) -> list[dict]:
    """Articles for `target` not yet labeled, shuffled across outlets.

    Shuffled because find_enriched_articles returns newest-first, which tends to
    cluster one outlet's run of stories together; labeling ten udn pieces in a
    row primes the annotator. A fixed seed makes a session reproducible.
    """
    done = {(g.article_id, g.target) for g in gold}
    todo = [a for a in articles if (a["id"], target) not in done]
    random.Random(seed).shuffle(todo)
    return todo


def label_session(
    articles: list[dict],
    *,
    target: str,
    annotator: str,
    gold_path: Path = GOLD_PATH,
    limit: int = 50,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, int]:
    """Blind labeling loop. Shows outlet, headline, lede; never a model verdict.

    Keys: n=neg  e=neu  p=pos  s=skip  b=show more body  q=quit.
    Returns the counts for this session.
    """
    counts = {"neg": 0, "neu": 0, "pos": 0, "skipped": 0}
    queue = articles[:limit]
    write(
        f"{len(queue)} to label for {target!r} (annotator: {annotator}). Keys: n/e/p, s skip, b body, q quit."
    )

    for i, art in enumerate(queue, 1):
        body = art.get("body") or ""
        write("")
        write(f"[{i}/{len(queue)}] {art['outlet']}  ·  {art['title']}")
        write(lede(body) or "(no lede)")
        while True:
            key = read("  n/e/p/s/b/q > ").strip().lower()
            if key == "b":
                write(body[:BODY_PREVIEW_CHARS] + ("…" if len(body) > BODY_PREVIEW_CHARS else ""))
                continue
            if key in KEYS:
                label = KEYS[key]
                append_gold(
                    gold_path,
                    GoldRow(
                        article_id=art["id"],
                        outlet=art["outlet"],
                        url=art["url_original"],
                        target=target,
                        label=label,
                        annotator=annotator,
                        labeled_at=now().isoformat(timespec="seconds"),
                    ),
                )
                counts[label] += 1
                break
            if key == "s":
                counts["skipped"] += 1
                break
            if key == "q":
                write(_summary(counts))
                return counts
            write("  ? n=neg e=neu p=pos s=skip b=body q=quit")
        write(f"  {_summary(counts)}")

    write(_summary(counts))
    return counts


def _summary(counts: dict[str, int]) -> str:
    labeled = counts["neg"] + counts["neu"] + counts["pos"]
    return f"labeled {labeled}: neg {counts['neg']} / neu {counts['neu']} / pos {counts['pos']}  (skipped {counts['skipped']})"

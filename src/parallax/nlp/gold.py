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
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..settings import EVAL_DIR, TIMEZONE
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
    articles: Iterable[dict],
    gold: Iterable[GoldRow],
    target: str,
    seed: int | None = None,
    annotator: str | None = None,
) -> list[dict]:
    """Articles for `target` not yet labeled, shuffled across outlets.

    Shuffled because find_enriched_articles returns newest-first, which tends to
    cluster one outlet's run of stories together; labeling ten udn pieces in a
    row primes the annotator. A fixed seed makes a session reproducible.

    `annotator` scopes what "already labeled" means. Left None -- every caller
    before T-020 -- one label by anyone retires a row, which is what a growing
    gold set wants. Set to a name, only that person's labels retire a row, so a
    second annotator is offered what the first already did. That is the whole
    mechanism behind measuring agreement: without it the CSV can only ever hold
    one opinion per article and kappa has nothing to compare.
    """
    done = {
        (g.article_id, g.target)
        for g in gold
        if annotator is None or g.annotator == annotator
    }
    todo = [a for a in articles if (a["id"], target) not in done]
    random.Random(seed).shuffle(todo)
    return todo


def validation_sample(
    articles: Iterable[dict],
    gold: Iterable[GoldRow],
    target: str,
    annotator: str,
    *,
    n: int,
    seed: int | None = None,
) -> list[dict]:
    """A stratified sample of rows someone *else* already labeled, for kappa.

    Candidates are articles carrying a label from another annotator and none
    from this one. The annotator relabels them blind; the two opinions are then
    compared by `nlp.eval.agreement`.

    Stratified **proportionally to the existing label distribution**, not evenly
    across the three classes. Kappa is prevalence-sensitive: expected agreement
    is computed from the marginals, so an evenly-sampled overlap would report
    kappa for a corpus that does not exist -- flattering on a set that is mostly
    neutral, because it quietly removes the easy agreements chance would
    produce. Proportional sampling estimates the same quantity labeling the
    whole set would. Largest-remainder rounding, so the sample totals `n`
    instead of drifting a row or two short.
    """
    mine = {g.article_id for g in gold if g.target == target and g.annotator == annotator}
    theirs: dict[int, str] = {
        g.article_id: g.label
        for g in gold
        if g.target == target and g.annotator != annotator and g.article_id not in mine
    }
    by_id = {a["id"]: a for a in articles}
    pool: dict[str, list[dict]] = {lab: [] for lab in LABELS}
    for aid, label in theirs.items():
        if aid in by_id:
            pool[label].append(by_id[aid])

    rng = random.Random(seed)
    for bucket in pool.values():
        rng.shuffle(bucket)

    total = sum(len(v) for v in pool.values())
    want = min(n, total)
    if want == 0:
        return []
    exact = {lab: want * len(v) / total for lab, v in pool.items()}
    take = {lab: min(int(x), len(pool[lab])) for lab, x in exact.items()}
    # Largest remainder, capped by what each bucket actually holds.
    while sum(take.values()) < want:
        candidates = [lab for lab in LABELS if take[lab] < len(pool[lab])]
        if not candidates:
            break
        lab = max(candidates, key=lambda x: (exact[x] - take[x], x))
        take[lab] += 1

    picked = [a for lab in LABELS for a in pool[lab][: take[lab]]]
    rng.shuffle(picked)  # never hand the annotator all the negs in a row
    return picked


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


# ---- pair gold set (T-008): eval/dup_gold.csv --------------------------------

PAIR_GOLD_PATH = EVAL_DIR / "dup_gold.csv"
PAIR_COLUMNS = ("article_a", "article_b", "is_duplicate", "annotator", "labeled_at", "note")
PAIR_KEYS = {"y": True, "n": False}
_SENTENCE_BOUNDARY = re.compile(r"[。！？!?\n]")


@dataclass(frozen=True)
class PairGoldRow:
    article_a: int  # always the smaller id
    article_b: int
    is_duplicate: bool
    annotator: str
    labeled_at: str
    note: str = ""

    @property
    def key(self) -> tuple[int, int]:
        return (self.article_a, self.article_b)


def pair_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def load_pair_gold(path: Path = PAIR_GOLD_PATH) -> list[PairGoldRow]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        rows = []
        for r in csv.DictReader(fh):
            flag = r["is_duplicate"].strip().lower()
            if flag not in ("true", "false"):
                raise ValueError(f"{path.name}: is_duplicate must be true/false, got {flag!r}")
            a, b = pair_key(int(r["article_a"]), int(r["article_b"]))
            rows.append(
                PairGoldRow(
                    a,
                    b,
                    flag == "true",
                    r.get("annotator", ""),
                    r.get("labeled_at", ""),
                    r.get("note", "") or "",
                )
            )
        return rows


def append_pair_gold(path: Path, row: PairGoldRow) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=PAIR_COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow(
            {
                "article_a": row.article_a,
                "article_b": row.article_b,
                "is_duplicate": "true" if row.is_duplicate else "false",
                "annotator": row.annotator,
                "labeled_at": row.labeled_at,
                "note": row.note,
            }
        )
        fh.flush()


def sentences(body: str | None) -> set[str]:
    """Sentence set for the 'shared sentences' hint shown to the annotator."""
    if not body:
        return set()
    return {s.strip() for s in _SENTENCE_BOUNDARY.split(body) if len(s.strip()) >= 8}


def pair_label_session(
    pairs: list[tuple[dict, dict]],
    *,
    annotator: str,
    gold_path: Path = PAIR_GOLD_PATH,
    limit: int = 200,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, int]:
    """Blind pair labeling. Shows both articles; never the similarity score.

    The question is the proposal's: is this the same copy, syndicated or
    lightly rewritten? Two articles that quote the same press release at
    length inside their own reporting are NOT duplicates.
    Keys: y=duplicate  n=not  s=skip  b=more body  q=quit.
    """
    counts = {"dup": 0, "not": 0, "skipped": 0}
    queue = pairs[:limit]
    write(
        f"{len(queue)} pairs to label (annotator: {annotator}). "
        "Keys: y dup, n not, s skip, b body, q quit."
    )

    for i, (a, b) in enumerate(queue, 1):
        shared = len(sentences(a.get("body")) & sentences(b.get("body")))
        write("")
        write(f"[{i}/{len(queue)}]  shared sentences: {shared}")
        for tag, art in (("A", a), ("B", b)):
            write(f"  {tag}. {art['outlet']:<11} {art.get('title', '')}")
            write("     " + lede(art.get("body")).replace("\n", "\n     "))
        while True:
            key = read("  y/n/s/b/q > ").strip().lower()
            if key == "b":
                for tag, art in (("A", a), ("B", b)):
                    body = art.get("body") or ""
                    write(f"--- {tag} ---")
                    write(body[:BODY_PREVIEW_CHARS])
                continue
            if key in PAIR_KEYS:
                append_pair_gold(
                    gold_path,
                    PairGoldRow(
                        *pair_key(a["id"], b["id"]),
                        PAIR_KEYS[key],
                        annotator,
                        now().isoformat(timespec="seconds"),
                    ),
                )
                counts["dup" if PAIR_KEYS[key] else "not"] += 1
                break
            if key == "s":
                counts["skipped"] += 1
                break
            if key == "q":
                write(_pair_summary(counts))
                return counts
            write("  ? y=duplicate n=not s=skip b=body q=quit")
        write(f"  {_pair_summary(counts)}")

    write(_pair_summary(counts))
    return counts


def _pair_summary(counts: dict[str, int]) -> str:
    labeled = counts["dup"] + counts["not"]
    return f"labeled {labeled}: dup {counts['dup']} / not {counts['not']}  (skipped {counts['skipped']})"


# ---- post gold set (T-017): eval/post_stance_gold.csv ------------------------

POST_GOLD_PATH = EVAL_DIR / "post_stance_gold.csv"
POST_COLUMNS = (
    "post_id",
    "platform",
    "post_url",
    "author",
    "target",
    "label",
    "annotator",
    "labeled_at",
    "note",
)


@dataclass(frozen=True)
class PostGoldRow:
    post_id: int
    platform: str
    post_url: str
    author: str
    target: str
    label: str
    annotator: str
    labeled_at: str
    note: str = ""


def load_post_gold(path: Path = POST_GOLD_PATH) -> list[PostGoldRow]:
    """Read human post labels; a missing file is an empty set."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        rows = []
        for r in csv.DictReader(fh):
            if r["label"] not in LABELS:
                raise ValueError(f"{path.name}: bad label {r['label']!r} for post {r['post_id']}")
            rows.append(
                PostGoldRow(
                    post_id=int(r["post_id"]),
                    platform=r["platform"],
                    post_url=r["post_url"],
                    author=r["author"],
                    target=r["target"],
                    label=r["label"],
                    annotator=r.get("annotator", ""),
                    labeled_at=r.get("labeled_at", ""),
                    note=r.get("note", "") or "",
                )
            )
        return rows


def append_post_gold(path: Path, row: PostGoldRow) -> None:
    """Persist one valid label immediately, with a header only on an empty file."""
    if row.label not in LABELS:
        raise ValueError(f"{path.name}: bad label {row.label!r} for post {row.post_id}")
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=POST_COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow(row.__dict__)
        fh.flush()


def pending_posts(
    posts: Iterable[dict], gold: Iterable[PostGoldRow], target: str, seed: int | None = None
) -> list[dict]:
    """Drop media-only and labeled posts, then shuffle across authors.

    Permalinks survive database migrations; numeric post ids do not.
    """
    done = {(g.post_url, g.target) for g in gold}
    todo = [
        p for p in posts if (p.get("text") or "").strip() and (p["post_url"], target) not in done
    ]
    random.Random(seed).shuffle(todo)
    return todo


def post_label_session(
    posts: list[dict],
    *,
    target: str,
    annotator: str,
    gold_path: Path = POST_GOLD_PATH,
    limit: int = 50,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, int]:
    """Blind post labeling with immediate persistence and optional permalink display."""
    counts = {"neg": 0, "neu": 0, "pos": 0, "skipped": 0}
    queue = posts[:limit]
    write(
        f"{len(queue)} to label for {target!r} (annotator: {annotator}). "
        "Keys: n/e/p, s skip, o permalink, q quit."
    )
    for i, post in enumerate(queue, 1):
        posted_at = post.get("posted_at")
        time = (
            posted_at.astimezone(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M")
            if posted_at is not None
            else "(no time)"
        )
        write("")
        write(f"[{i}/{len(queue)}] @{post.get('author') or '?'} · {time}")
        write(post["text"])
        while True:
            key = read("  n/e/p/s/o/q > ").strip().lower()
            if key == "o":
                write(post["post_url"])
                continue
            if key in KEYS:
                label = KEYS[key]
                append_post_gold(
                    gold_path,
                    PostGoldRow(
                        post_id=post["id"],
                        platform=post["platform"],
                        post_url=post["post_url"],
                        author=post["author"],
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
            write("  ? n=neg e=neu p=pos s=skip o=permalink q=quit")
        write(f"  {_summary(counts)}")
    write(_summary(counts))
    return counts

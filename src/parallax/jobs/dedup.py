"""Q3: rebuild near-duplicate clusters over the enriched corpus and report them.

    make dedup                              # fingerprint, cluster, persist, print
    make dedup ARGS="--keyword 沈伯洋"        # same, readout narrowed to the keyword
    make dedup ARGS="--dry-run"             # compute and print, write nothing
    make dedup ARGS="--containment 0.8 --jaccard 0.4"

Everything is recomputed each run: containment needs the feature sets, and
the per-outlet boilerplate shifts as the corpus grows. Persistence is a full
replacement keyed by stable cluster ids, so a second run reports zero changes.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from .. import db
from ..nlp.dedup import (
    CONTAINMENT_THRESHOLD,
    HAMMING_PREFILTER,
    JACCARD_THRESHOLD,
    SHARES_SOURCE_THRESHOLD,
    Cluster,
    Fingerprint,
    Member,
    PairScore,
    bigrams,
    boilerplate,
    build_cluster,
    candidate_pairs,
    components,
    fingerprint,
    is_duplicate,
    to_signed,
)
from ..settings import TIMEZONE

log = logging.getLogger(__name__)


@dataclass
class DedupRun:
    fingerprints: dict[int, Fingerprint]
    boilerplate_sizes: dict[str, int]
    pairs_scored: int
    duplicates: list[PairScore]
    shares_source: list[PairScore]  # reported, never stored
    clusters: list[Cluster]  # ordered by first_published_at
    too_short: list[int]


def run_dedup(
    rows: list[dict],
    *,
    containment: float = CONTAINMENT_THRESHOLD,
    jaccard: float = JACCARD_THRESHOLD,
    hamming_max: int = HAMMING_PREFILTER,
) -> DedupRun:
    """Rows from db.enriched_for_dedup (or any dicts with the same keys)."""
    per_outlet: dict[str, list] = defaultdict(list)
    for r in rows:
        per_outlet[r["outlet"]].append(bigrams(r["body_seg"]))
    drop = boilerplate(per_outlet)

    fps = {
        r["id"]: fingerprint(r["id"], r["outlet"], r["body_seg"], drop[r["outlet"]]) for r in rows
    }

    duplicates: list[PairScore] = []
    shares: list[PairScore] = []
    scored = 0
    for s in candidate_pairs(fps.values(), hamming_max=hamming_max):
        scored += 1
        if is_duplicate(s, containment=containment, jaccard=jaccard):
            duplicates.append(s)
        elif s.containment >= SHARES_SOURCE_THRESHOLD:
            shares.append(s)

    members = {
        r["id"]: Member(r["id"], r["outlet"], r["effective_at"], r["published_at"]) for r in rows
    }
    clusters = [
        build_cluster(members[i] for i in comp)
        for comp in components((s.a, s.b) for s in duplicates)
    ]
    clusters.sort(key=lambda c: (c.first_published_at, c.cluster_id))

    return DedupRun(
        fingerprints=fps,
        boilerplate_sizes={o: len(v) for o, v in sorted(drop.items())},
        pairs_scored=scored,
        duplicates=duplicates,
        shares_source=shares,
        clusters=clusters,
        too_short=sorted(i for i, f in fps.items() if f.too_short),
    )


def persist(conn, run: DedupRun) -> dict[str, int]:
    changed = 0
    for fp in run.fingerprints.values():
        changed += db.save_fingerprint(conn, fp.article_id, to_signed(fp.simhash), fp.bands)
    counts = db.replace_clusters(conn, run.clusters, list(run.fingerprints))
    counts["fingerprints_changed"] = changed
    return counts


# ---- readout ---------------------------------------------------------------


def _local(dt) -> str:
    from zoneinfo import ZoneInfo

    return dt.astimezone(ZoneInfo(TIMEZONE)).strftime("%m-%d %H:%M")


def _gap(td: timedelta) -> str:
    s = int(td.total_seconds())
    if s < 3600:
        return f"+{s // 60}m"
    return f"+{s // 3600}h{(s % 3600) // 60:02d}m"


def render_clusters(
    run: DedupRun, rows_by_id: dict[int, dict], only: set[int] | None = None
) -> str:
    """Clusters in time order. The origin is named only when confident;
    otherwise the line says the order is indeterminate and why (invariant 5)."""
    shown = [
        c for c in run.clusters if only is None or any(m.article_id in only for m in c.members)
    ]
    if not shown:
        return "(no clusters)"
    out = []
    for c in shown:
        head = f"cluster {c.cluster_id}  {len(c.members)} members  "
        if c.origin_confident:
            head += f"origin: {c.origin.outlet} ({_local(c.origin.effective_at)})"
        else:
            head += f"order indeterminate: {c.reason}"
        out.append(head)
        for rank, m in enumerate(c.members, 1):
            title = (rows_by_id.get(m.article_id, {}).get("title") or "")[:40]
            gap = "" if rank == 1 else _gap(m.effective_at - c.origin.effective_at)
            out.append(f"  {rank}. {_local(m.effective_at)}  {m.outlet:<11}{gap:>8}  {title}")
    return "\n".join(out)


def originality_table(
    run: DedupRun, rows_by_id: dict[int, dict], only: set[int] | None = None
) -> str:
    """Per outlet: alone / first / follow / unresolved, and two originality rates.

    `original%` counts an article as original when it is alone or the confident
    first of a cluster -- the origin did the reporting. `strict%` is the
    proposal's literal definition, "not in any near-duplicate cluster".
    Too-short bodies are excluded from n and listed separately: they are not
    measured, which is different from being original.
    """
    role: dict[int, str] = {}
    for c in run.clusters:
        for rank, m in enumerate(c.members, 1):
            if not c.origin_confident:
                role[m.article_id] = "unresolved"
            else:
                role[m.article_id] = "first" if rank == 1 else "follow"

    per: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for aid, fp in run.fingerprints.items():
        if only is not None and aid not in only:
            continue
        o = fp.outlet
        if fp.too_short:
            per[o]["short"] += 1
            continue
        per[o]["n"] += 1
        per[o][role.get(aid, "alone")] += 1

    header = (
        f"{'outlet':<12}{'n':>5}{'alone':>7}{'first':>7}{'follow':>8}{'unresolved':>12}"
        f"{'original%':>11}{'strict%':>9}{'short':>7}"
    )
    lines = [header]
    for o in sorted(per):
        p = per[o]
        n = p["n"]
        orig = (p["alone"] + p["first"]) / n if n else 0.0
        strict = p["alone"] / n if n else 0.0
        lines.append(
            f"{o:<12}{n:>5}{p['alone']:>7}{p['first']:>7}{p['follow']:>8}{p['unresolved']:>12}"
            f"{orig:>10.0%}{strict:>9.0%}{p['short']:>7}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Q3: near-duplicate clusters over the enriched corpus."
    )
    parser.add_argument("--keyword", help="narrow the readout to clusters touching this keyword")
    parser.add_argument("--containment", type=float, default=CONTAINMENT_THRESHOLD)
    parser.add_argument("--jaccard", type=float, default=JACCARD_THRESHOLD)
    parser.add_argument("--hamming", type=int, default=HAMMING_PREFILTER)
    parser.add_argument("--dry-run", action="store_true", help="compute and print; write nothing")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    with db.connect() as conn:
        rows = db.enriched_for_dedup(conn)
        only = None
        if args.keyword:
            only = {r["id"] for r in db.find_enriched_articles(conn, args.keyword, limit=5000)}

        run = run_dedup(
            rows, containment=args.containment, jaccard=args.jaccard, hamming_max=args.hamming
        )
        counts = {}
        if not args.dry_run:
            counts = persist(conn, run)
            conn.commit()

    rows_by_id = {r["id"]: r for r in rows}
    log.info(
        "articles=%d pairs_scored=%d duplicates=%d shares_source=%d clusters=%d too_short=%d "
        "boilerplate=%s thresholds=(containment %.2f, jaccard %.2f, hamming<=%d)",
        len(rows),
        run.pairs_scored,
        len(run.duplicates),
        len(run.shares_source),
        len(run.clusters),
        len(run.too_short),
        run.boilerplate_sizes,
        args.containment,
        args.jaccard,
        args.hamming,
    )
    if counts:
        log.info("persisted: %s", counts)
    print(render_clusters(run, rows_by_id, only))
    print()
    print(originality_table(run, rows_by_id, only))
    if run.shares_source:
        print(
            f"\n{len(run.shares_source)} pair(s) share source text "
            f"(containment {SHARES_SOURCE_THRESHOLD:.2f}-{args.containment:.2f}) -- not clustered"
        )
    if run.too_short:
        print(f"{len(run.too_short)} article(s) too short to compare: {run.too_short[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

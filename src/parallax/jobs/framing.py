"""Q3, second half: what each cluster member added, dropped or rewrote.

    make framing                                # compute + persist + print, every cluster
    make framing ARGS="--keyword 關稅"           # readout narrowed to the keyword
    make framing ARGS="--dry-run"               # compute and print, write nothing
    make framing ARGS="--summarize --dry-run"   # how many model calls a summary pass needs
    make framing ARGS=--summarize               # make them; re-run makes zero

Runs after `make dedup`: it reads the stored clusters and never changes
membership. The deterministic part (core, deltas) is recomputed every run and
written only where it differs, so a second run reports zero changes. The
summary pass is the only model spend in Q3 and is opt-in per invocation.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from .. import db
from ..nlp.dedup import Member, build_cluster
from ..nlp.framing import (
    Framing,
    MemberText,
    boilerplate,
    diff_cluster,
    pair_edits,
    units,
)
from ..nlp.summary import (
    SUMMARY_VERSION,
    DailyQuotaExhausted,
    GeminiSummary,
    Summarizer,
    SummaryInput,
    caption_like,
    rule_summary,
)
from ..settings import FRAMING_MODEL, STANCE_RPM, TIMEZONE

log = logging.getLogger(__name__)

MAX_LINES_PER_MEMBER = 6
LINE_CHARS = 64


@dataclass
class ClusterFraming:
    cluster: dict  # from db.clusters_for_framing
    framing: Framing
    reason: str  # why the order is indeterminate; "" when confident


def compute(clusters: list[dict], bodies_by_outlet: dict[str, list[str]]) -> list[ClusterFraming]:
    """Pure given its inputs: the boilerplate table, then one diff per cluster."""
    drop = boilerplate(bodies_by_outlet)
    out = []
    for c in clusters:
        members = sorted(c["members"], key=lambda m: (m["cluster_rank"] or 0, m["id"]))
        if len(members) < 2:
            continue
        texts = [
            MemberText(
                m["id"],
                m["outlet"],
                m["title"] or "",
                tuple(units(m["body"], drop.get(m["outlet"], set()))),
            )
            for m in members
        ]
        framing = diff_cluster(texts, origin_confident=c["origin_confident"])
        reason = ""
        if not c["origin_confident"]:
            reason = build_cluster(
                Member(m["id"], m["outlet"], m["effective_at"], m["published_at"]) for m in members
            ).reason
        c["members"] = members
        out.append(ClusterFraming(c, framing, reason))
    return out


def persist(conn, computed: list[ClusterFraming]) -> dict[str, int]:
    totals = {"cores_set": 0, "deltas_set": 0, "summaries_reset": 0}
    for cf in computed:
        rows = []
        for i, d in enumerate(cf.framing.deltas):
            rule = rule_summary(d, is_origin=cf.framing.directional and i == 0)
            rows.append((d.article_id, list(d.added), list(d.removed), rule))
        counts = db.save_framing(conn, cf.cluster["cluster_id"], cf.framing.core_text, rows)
        for k, v in counts.items():
            totals[k] += v
    return totals


# ---- summaries -------------------------------------------------------------


def _stored_is_current(m: dict, d) -> bool:
    return (m["delta_added"] or []) == list(d.added) and (m["delta_removed"] or []) == list(
        d.removed
    )


def effective_summaries(computed: list[ClusterFraming], version: str) -> dict[int, str | None]:
    """What each member's summary is once this run's deltas are the stored ones.

    A stored summary counts only if the stored deltas match the computed ones
    and it was written at this prompt version; otherwise the member gets its
    rule string, or None, which is what "pending" means. Pure, so a dry run
    can print exactly what a real run would."""
    out: dict[int, str | None] = {}
    for cf in computed:
        for i, (m, d) in enumerate(zip(cf.cluster["members"], cf.framing.deltas, strict=True)):
            rule = rule_summary(d, is_origin=cf.framing.directional and i == 0)
            if rule is not None:
                out[m["id"]] = rule
            elif _stored_is_current(m, d) and m["delta_summary_version"] == version:
                out[m["id"]] = m["delta_summary"]
            else:
                out[m["id"]] = None
    return out


def summary_inputs(computed: list[ClusterFraming], pending: set[int]) -> list[SummaryInput]:
    out = []
    for cf in computed:
        origin_title = cf.cluster["members"][0]["title"] or ""
        for m, d in zip(cf.cluster["members"], cf.framing.deltas, strict=True):
            if m["id"] in pending and not d.empty:
                out.append(
                    SummaryInput(
                        article_id=m["id"],
                        directional=cf.framing.directional,
                        reference_headline=origin_title if cf.framing.directional else "",
                        headline=m["title"] or "",
                        added=d.added,
                        removed=d.removed,
                        captions=caption_like(d.added, cf.framing.core),
                    )
                )
    return out


def summarize(
    inputs: list[SummaryInput],
    summarizer: Summarizer,
    *,
    connect: Callable | None = None,
) -> dict[str, int]:
    """One call per pending member, each committed on its own; a daily quota
    stops the run and leaves the rest pending, exactly as stance does."""
    stats = {"planned": len(inputs), "summarized": 0, "failed": 0, "quota_exhausted": 0}
    with (connect or db.connect)() as conn:
        for inp in inputs:
            try:
                result = summarizer.summarize(inp)
                db.save_summary(conn, inp.article_id, result.summary, result.model, result.version)
                conn.commit()
            except DailyQuotaExhausted as exc:
                conn.rollback()
                stats["quota_exhausted"] = 1
                log.error(
                    "%s -- %d member(s) left unsummarized",
                    exc,
                    stats["planned"] - stats["summarized"] - stats["failed"],
                )
                break
            except Exception as exc:  # noqa: BLE001 -- isolation is the point
                conn.rollback()
                stats["failed"] += 1
                log.warning("summary failed for %s: %s", inp.article_id, exc)
                continue
            stats["summarized"] += 1
    return stats


# ---- readout ---------------------------------------------------------------


def _local(dt) -> str:
    from zoneinfo import ZoneInfo

    return dt.astimezone(ZoneInfo(TIMEZONE)).strftime("%m-%d %H:%M")


def _gap(td: timedelta) -> str:
    s = int(td.total_seconds())
    return f"+{s // 60}m" if s < 3600 else f"+{s // 3600}h{(s % 3600) // 60:02d}m"


def _clip(s: str, n: int = LINE_CHARS) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def render(
    computed: list[ClusterFraming],
    summaries: dict[int, str | None],
    only: set[int] | None = None,
) -> str:
    """The design's Q3 block: core excerpt, then one line per member with its
    summary, then the ＋ － ～ lines behind it. An indeterminate cluster never
    prints "removed": its members have "only in this version" lines."""
    shown = [
        cf for cf in computed if only is None or any(m["id"] in only for m in cf.cluster["members"])
    ]
    if not shown:
        return "(no clusters)"
    out = []
    for cf in shown:
        c, f = cf.cluster, cf.framing
        members = c["members"]
        first = members[0]
        head = f"cluster {c['cluster_id']}  {len(members)} members  "
        if f.directional:
            head += f"origin: {first['outlet']} ({_local(first['effective_at'])})"
        else:
            head += f"order indeterminate: {cf.reason}"
        out.append(head)
        core = f.core
        excerpt = _clip(core[0], 80) if core else "(no shared sentences after cleaning)"
        out.append(f"  core: {len(core)} sentence(s)  {excerpt}")
        for rank, (m, d) in enumerate(zip(members, f.deltas, strict=True), 1):
            gap = "" if rank == 1 else _gap(m["effective_at"] - first["effective_at"])
            summary = summaries.get(m["id"]) or "(no summary yet)"
            out.append(
                f"  #{rank} {_local(m['effective_at'])}  {m['outlet']:<11}{gap:>8}  {summary}"
            )
            if d.empty:
                continue
            edits, added, removed = pair_edits(d.added, d.removed)
            lines = []
            for old, new in edits:
                lines.append(f"～ {_clip(old, LINE_CHARS // 2)} → {_clip(new, LINE_CHARS // 2)}")
            mark = "＋" if f.directional else "本版獨有"
            lines += [f"{mark} {_clip(s)}" for s in added]
            lines += [f"－ {_clip(s)}" for s in removed]
            for line in lines[:MAX_LINES_PER_MEMBER]:
                out.append(f"        {line}")
            if len(lines) > MAX_LINES_PER_MEMBER:
                out.append(f"        … {len(lines) - MAX_LINES_PER_MEMBER} more")
        out.append("")
    return "\n".join(out).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Q3: framing delta within each cluster.")
    parser.add_argument("--keyword", help="narrow the readout to clusters touching this keyword")
    parser.add_argument("--dry-run", action="store_true", help="compute and print; write nothing")
    parser.add_argument("--summarize", action="store_true", help="spend model calls on summaries")
    parser.add_argument("--model", default=FRAMING_MODEL)
    parser.add_argument("--rpm", type=float, default=STANCE_RPM)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    with db.connect() as conn:
        clusters = db.clusters_for_framing(conn)
        computed = compute(clusters, db.bodies_by_outlet(conn))
        only = None
        if args.keyword:
            only = {r["id"] for r in db.find_enriched_articles(conn, args.keyword, limit=5000)}
        counts = {}
        if not args.dry_run:
            counts = persist(conn, computed)
            conn.commit()

    summaries = effective_summaries(computed, SUMMARY_VERSION)
    pending = {aid for aid, s in summaries.items() if s is None}
    n_members = sum(len(cf.cluster["members"]) for cf in computed)
    log.info(
        "clusters=%d members=%d with_delta=%d pending_summaries=%d",
        len(computed),
        n_members,
        sum(1 for cf in computed for d in cf.framing.deltas if not d.empty),
        len(pending),
    )
    if counts:
        log.info("persisted: %s", counts)

    if args.summarize:
        inputs = summary_inputs(computed, pending)
        if args.dry_run:
            print(f"dry run: {len(inputs)} summary call(s) would be made with {args.model}")
        else:
            stats = summarize(inputs, GeminiSummary(model=args.model, rpm=args.rpm))
            log.info("summaries: %s model=%s version=%s", stats, args.model, SUMMARY_VERSION)
            with db.connect() as conn:
                for c in db.clusters_for_framing(conn):
                    for m in c["members"]:
                        if m["delta_summary"] is not None and m["id"] in pending:
                            summaries[m["id"]] = m["delta_summary"]

    print(render(computed, summaries, only))
    return 0


if __name__ == "__main__":
    sys.exit(main())

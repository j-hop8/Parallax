"""The dedup job end to end on synthetic rows, and its persistence with a fake db."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

from parallax.jobs import dedup as job

T0 = datetime(2026, 8, 16, 2, 14, tzinfo=UTC)


def _words(n, prefix):
    return " ".join(f"{prefix}{i:03d}" for i in range(n))


def _row(i, outlet, seg, minutes, stamped=True, title=None):
    t = T0 + timedelta(minutes=minutes)
    return {
        "id": i,
        "outlet": outlet,
        "title": title or f"title {i}",
        "effective_at": t,
        "published_at": t if stamped else None,
        "body_seg": seg,
        "simhash": None,
        "dup_cluster_id": None,
    }


def _corpus():
    wire = _words(120, "w")
    return [
        _row(57, "cna", wire, 0, title="哥倫比亞總統與川普通話"),
        _row(
            200, "ltn", wire + " " + _words(20, "extra"), 170, title="哥倫比亞總統與川普通話 籲暫停"
        ),
        _row(300, "udn", _words(150, "z"), 30),  # unrelated
        _row(400, "ltn", "只有 一點 文字", 40),  # too short
        _row(500, "udn", _words(150, "z") + " " + _words(5, "q"), 45),  # udn exact-ish dup of 300
    ]


def test_run_dedup_finds_the_wire_copy_and_marks_the_teaser_too_short():
    run = job.run_dedup(_corpus())
    assert run.too_short == [400]
    ids = {frozenset(m.article_id for m in c.members) for c in run.clusters}
    assert frozenset({57, 200}) in ids
    assert frozenset({300, 500}) in ids
    wire = next(c for c in run.clusters if c.cluster_id == 57)
    assert wire.origin.outlet == "cna" and wire.origin_confident
    udn = next(c for c in run.clusters if c.cluster_id == 300)
    assert not udn.origin_confident and "both udn" in udn.reason
    assert run.pairs_scored >= 2 and len(run.duplicates) == 2


def test_readout_names_the_origin_only_when_confident():
    rows = _corpus()
    run = job.run_dedup(rows)
    text = job.render_clusters(run, {r["id"]: r for r in rows})
    assert "origin: cna" in text and "+2h50m" in text and "哥倫比亞" in text
    assert "order indeterminate: first two both udn" in text
    assert job.render_clusters(run, {}, only={999}) == "(no clusters)"
    assert "origin: cna" in job.render_clusters(run, {r["id"]: r for r in rows}, only={200})


def test_originality_table_separates_alone_first_follow_unresolved_and_short():
    rows = _corpus()
    run = job.run_dedup(rows)
    text = job.originality_table(run, {r["id"]: r for r in rows})
    lines = {ln.split()[0]: ln.split() for ln in text.splitlines()[1:]}
    # cna: 1 article, the confident first -> original 100%, strict 0%
    assert lines["cna"][1:6] == ["1", "0", "1", "0", "0"] and lines["cna"][6] == "100%"
    assert lines["cna"][7] == "0%"
    # ltn: one follow (n=1), one too-short (excluded from n)
    assert lines["ltn"][1:6] == ["1", "0", "0", "1", "0"] and lines["ltn"][8] == "1"
    # udn: two unresolved
    assert lines["udn"][1:6] == ["2", "0", "0", "0", "2"] and lines["udn"][6] == "0%"


def test_persist_writes_every_fingerprint_and_replaces_clusters(monkeypatch):
    rows = _corpus()
    run = job.run_dedup(rows)
    written = []
    replaced = {}

    def save_fingerprint(conn, article_id, signed, bands):
        written.append((article_id, signed, bands))
        return 1

    def replace_clusters(conn, clusters, scope_ids):
        replaced["clusters"] = clusters
        replaced["scope"] = sorted(scope_ids)
        return {"clusters_upserted": len(clusters), "clusters_deleted": 0, "members_set": 4}

    monkeypatch.setattr(job.db, "save_fingerprint", save_fingerprint)
    monkeypatch.setattr(job.db, "replace_clusters", replace_clusters)

    counts = job.persist(object(), run)
    assert sorted(a for a, _, _ in written) == [57, 200, 300, 400, 500], (
        "too-short still fingerprinted"
    )
    assert all(-(1 << 63) <= s < (1 << 63) for _, s, _ in written), "stored signed"
    assert replaced["scope"] == [57, 200, 300, 400, 500]
    assert counts["fingerprints_changed"] == 5 and counts["clusters_upserted"] == 2


def test_main_dry_run_prints_without_persisting(monkeypatch, capsys):
    rows = _corpus()

    class _Conn:
        commits = 0

        def commit(self):
            self.commits += 1

    conn = _Conn()
    monkeypatch.setattr(job.db, "connect", lambda: contextlib.nullcontext(conn))
    monkeypatch.setattr(job.db, "enriched_for_dedup", lambda c: rows)
    monkeypatch.setattr(job.db, "find_enriched_articles", lambda c, kw, limit: [rows[0]])
    monkeypatch.setattr(
        job.db, "save_fingerprint", lambda *a: (_ for _ in ()).throw(AssertionError("wrote"))
    )
    monkeypatch.setattr(
        job.db, "replace_clusters", lambda *a: (_ for _ in ()).throw(AssertionError("wrote"))
    )

    assert job.main(["--dry-run", "--keyword", "哥倫比亞"]) == 0
    out = capsys.readouterr().out
    assert "origin: cna" in out and "both udn" not in out, "--keyword narrows the readout"
    assert conn.commits == 0

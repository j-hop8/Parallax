"""Re-keying the gold set by URL (T-025).

The gold set is the most expensive artifact in the project and the only one a
lost database cannot rebuild. These pin the three ways a remap could make it
worse than leaving it alone: dropping a label, corrupting a join, or rewriting
a column that was the annotator's work.
"""

from __future__ import annotations

from dataclasses import replace

import psycopg
import pytest
from psycopg.rows import dict_row

from parallax import db
from parallax.nlp.gold import GoldRow
from parallax.nlp.remap import canonical_keys, plan
from parallax.settings import DATABASE_URL
from parallax.urls import canonicalize


def _row(article_id, url, outlet="setn", target="沈伯洋", label="neg", annotator="claude-opus-5"):
    return GoldRow(article_id, outlet, url, target, label, annotator, "2026-09-13", "note kept")


def test_canonical_keys_strip_tracking_and_www_and_dedupe():
    """The gold file stores url_original; article_index keys on the canonical
    form, so the lookup has to go through the same function the crawler used."""
    gold = [
        _row(1, "https://www.setn.com/news/1"),
        _row(2, "https://setn.com/news/1?utm_source=line"),  # same article, noisier URL
        _row(3, "https://www.setn.com/news/2"),
    ]
    assert canonical_keys(gold) == [
        ("setn", "https://setn.com/news/1"),
        ("setn", "https://setn.com/news/2"),
    ]


def test_rows_are_rekeyed_to_the_new_ids():
    gold = [_row(11, "https://www.setn.com/news/1"), _row(22, "https://www.setn.com/news/2")]
    found = {("setn", "https://setn.com/news/1"): 900, ("setn", "https://setn.com/news/2"): 901}
    p = plan(gold, found)
    assert (p.remapped, p.unchanged, len(p.unmatched)) == (2, 0, 0)
    assert [g.article_id for g in p.applied()] == [900, 901]


def test_only_the_id_changes_everything_else_is_the_annotators_work():
    gold = [_row(11, "https://www.setn.com/news/1", label="pos", annotator="jimmy")]
    p = plan(gold, {("setn", "https://setn.com/news/1"): 900})
    before, after = gold[0], p.applied()[0]
    assert after.article_id == 900
    assert replace(after, article_id=before.article_id) == before


def test_a_row_whose_article_is_not_back_yet_keeps_its_label():
    """A re-crawl fills in over days. Dropping unmatched rows would destroy the
    labels this tool exists to save, so they survive untouched and are listed."""
    gold = [_row(11, "https://www.setn.com/news/1"), _row(22, "https://www.setn.com/news/gone")]
    p = plan(gold, {("setn", "https://setn.com/news/1"): 900})
    assert len(p.unmatched) == 1
    assert p.unmatched[0].row.article_id == 22
    assert [g.article_id for g in p.applied()] == [900, 22], "the pending row is carried through"
    assert len(p.applied()) == 2, "no row is ever dropped"


def test_already_correct_ids_are_left_alone():
    gold = [_row(900, "https://www.setn.com/news/1")]
    p = plan(gold, {("setn", "https://setn.com/news/1"): 900})
    assert (p.remapped, p.unchanged) == (0, 1)
    assert p.applied() == tuple(gold)


def test_two_articles_claiming_one_id_stops_the_whole_file():
    """Not a bijection: writing it would make two different articles share an
    id and corrupt every join downstream. Refuse the file, not just the row."""
    gold = [_row(11, "https://www.setn.com/news/1"), _row(22, "https://www.setn.com/news/2")]
    found = {("setn", "https://setn.com/news/1"): 900, ("setn", "https://setn.com/news/2"): 900}
    p = plan(gold, found)
    assert not p.safe
    assert p.collisions == {900: (11, 22)}


def test_a_collision_against_an_untouched_row_is_still_a_collision():
    """The check is on the resulting file. A row that already holds the id a
    remapped row wants is exactly as broken as two remapped rows clashing."""
    gold = [_row(900, "https://www.setn.com/news/keeps"), _row(11, "https://www.setn.com/news/1")]
    found = {
        ("setn", "https://setn.com/news/keeps"): 900,
        ("setn", "https://setn.com/news/1"): 900,
    }
    assert not plan(gold, found).safe


def test_the_same_article_under_two_targets_is_not_a_collision():
    """One article legitimately carries a label per target -- and a second row
    per annotator once T-020's validation runs. Same id, same article, fine."""
    gold = [
        _row(11, "https://www.setn.com/news/1", target="沈伯洋"),
        _row(11, "https://www.setn.com/news/1", target="關稅"),
        _row(11, "https://www.setn.com/news/1", target="沈伯洋", annotator="jimmy"),
    ]
    p = plan(gold, {("setn", "https://setn.com/news/1"): 900})
    assert p.safe, "three rows, one article -- not a conflict"
    assert [g.article_id for g in p.applied()] == [900, 900, 900]


def test_outlet_is_part_of_the_key():
    """article_index is UNIQUE on (outlet, url_canonical). Two outlets can
    serve the same path; matching on URL alone would cross-wire them."""
    gold = [_row(11, "https://www.setn.com/news/1", outlet="setn")]
    assert plan(gold, {("udn", "https://setn.com/news/1"): 900}).unmatched


# ---- the lookup, against Postgres -------------------------------------------

TOKEN = "zzremaptoken"  # segments to itself; matches nothing real


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(DATABASE_URL, connect_timeout=3, row_factory=dict_row)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable: {exc}")
    try:
        connection.autocommit = False
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO outlets (code, name_zh, home_url) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (TOKEN, TOKEN, "https://example.com/"),
            )
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _seed(cur, original: str) -> int:
    cur.execute(
        """
        INSERT INTO article_index (outlet, url_canonical, url_original, title, title_seg, seen_at)
        VALUES (%s, %s, %s, %s, %s, now()) RETURNING id
        """,
        (TOKEN, canonicalize(original), original, f"{TOKEN} 標題", TOKEN),
    )
    return cur.fetchone()["id"]


def test_lookup_matches_the_labelled_url_through_canonicalisation(conn):
    """The end-to-end claim: a label made against a url_original re-attaches to
    a freshly crawled row whose id nobody could have predicted."""
    with conn.cursor() as cur:
        new_id = _seed(cur, "https://www.example.com/remap/1")

    gold = [_row(999_001, "https://www.example.com/remap/1?utm_source=line", outlet=TOKEN)]
    found = db.article_ids_by_canonical(conn, canonical_keys(gold))
    p = plan(gold, found)

    assert p.remapped == 1 and p.safe
    assert p.applied()[0].article_id == new_id
    assert p.applied()[0].label == gold[0].label, "the label rides along untouched"


def test_lookup_returns_nothing_for_urls_not_in_the_index(conn):
    gold = [_row(999_002, "https://www.example.com/never-crawled", outlet=TOKEN)]
    assert db.article_ids_by_canonical(conn, canonical_keys(gold)) == {}
    assert len(plan(gold, {}).unmatched) == 1


def test_lookup_does_not_cross_outlets(conn):
    """Same path under a different outlet must not match -- article_index is
    UNIQUE on (outlet, url_canonical), and the gold row carries the outlet."""
    with conn.cursor() as cur:
        _seed(cur, "https://www.example.com/remap/shared")
    gold = [_row(999_003, "https://www.example.com/remap/shared", outlet="udn")]
    assert db.article_ids_by_canonical(conn, canonical_keys(gold)) == {}


def test_empty_input_does_not_query(conn):
    assert db.article_ids_by_canonical(conn, []) == {}

"""Drift-guards for the v2 schema migration (Power Mode reversibility columns).

These pin the lessons learned building STEP 1:
  - a FRESH index has power_run/base_sha on nodes AND edges + the power indexes;
  - an OLD v1 on-disk index gains them on open WITHOUT data loss (replay-safe);
  - re-opening is idempotent (no crash, no duplicate work);
  - the order bug stays dead: the power_run indexes must NOT live in _SCHEMA (which
    runs before the ALTERs) or `CREATE INDEX ON nodes(power_run)` crashes an old DB.
"""

from __future__ import annotations

import sqlite3

from recall.db import SCHEMA_VERSION, connect


def _cols(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(db: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }


def test_fresh_db_has_power_columns_and_indexes():
    db = connect(":memory:")
    assert {"power_run", "base_sha"} <= _cols(db, "nodes")
    assert {"power_run", "base_sha"} <= _cols(db, "edges")
    idx = _indexes(db)
    assert "idx_nodes_power" in idx and "idx_edges_power" in idx


def _make_v1_db(path: str) -> None:
    """Write a pre-v2 index by hand — the shape that shipped before Power Mode."""
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE nodes(
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT,
            facets TEXT, file_path TEXT, symbol TEXT, line INTEGER, stamped_at_sha TEXT,
            origin TEXT DEFAULT 'live', created_at INTEGER);
        CREATE TABLE edges(
            id INTEGER PRIMARY KEY, src_node INTEGER, dst_node INTEGER, kind TEXT NOT NULL,
            weight REAL, stamped_at_sha TEXT, verified INTEGER DEFAULT 1);
        INSERT INTO meta VALUES('schema_version','1');
        INSERT INTO nodes(kind,title,origin) VALUES('lesson','OLD DATA SURVIVES','bootstrap');
        """
    )
    raw.commit()
    raw.close()


def test_v1_db_migrates_to_v2_without_data_loss(tmp_path):
    p = str(tmp_path / "old.db")
    _make_v1_db(p)

    db = connect(p)  # opening must migrate in place

    assert {"power_run", "base_sha", "author"} <= _cols(db, "nodes")  # v2 + v3 (author)
    assert {"power_run", "base_sha"} <= _cols(db, "edges")
    ver = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert int(ver) == SCHEMA_VERSION
    row = db.execute("SELECT title, origin FROM nodes").fetchone()
    assert row[0] == "OLD DATA SURVIVES" and row[1] == "bootstrap"  # no data loss
    assert {"idx_nodes_power", "idx_edges_power"} <= _indexes(db)


def test_migration_is_idempotent(tmp_path):
    p = str(tmp_path / "old.db")
    _make_v1_db(p)
    connect(p)  # v1 -> v2
    connect(p)  # already v2 -> no-op, must not crash
    db = connect(p)  # and again
    assert {"power_run", "base_sha"} <= _cols(db, "nodes")


# --- v4: porter stemmer on fts_anchors (modal/modals, lazy/lazily unify) ---
def _fts_ddl(db: sqlite3.Connection) -> str:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='fts_anchors'"
    ).fetchone()
    return (row[0] or "").lower() if row else ""


def test_fresh_db_fts_uses_porter_stemmer():
    db = connect(":memory:")
    assert "porter" in _fts_ddl(db)


def _make_v3_db_with_old_fts(path: str) -> None:
    """A pre-v4 index: fts_anchors built with the OLD unicode61 tokenizer (no stemming),
    plus the durable anchors/node_anchors rows the migration rebuilds the FTS from."""
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, kind TEXT, title TEXT, origin TEXT);
        CREATE TABLE anchors(id INTEGER PRIMARY KEY, term TEXT UNIQUE);
        CREATE TABLE node_anchors(node_id INTEGER, anchor_id INTEGER, PRIMARY KEY(node_id,anchor_id));
        CREATE VIRTUAL TABLE fts_anchors USING fts5(term, node_id UNINDEXED, tokenize='unicode61');
        INSERT INTO meta VALUES('schema_version','3');
        INSERT INTO nodes(id,kind,title,origin) VALUES(1,'lesson','rendered modals','bootstrap');
        INSERT INTO anchors(id,term) VALUES(1,'modals'),(2,'rendered');
        INSERT INTO node_anchors VALUES(1,1),(1,2);
        INSERT INTO fts_anchors(term,node_id) VALUES('modals',1),('rendered',1);
        """
    )
    raw.commit()
    raw.close()


def test_v3_fts_rebuilds_to_porter_and_stems(tmp_path):
    p = str(tmp_path / "old_fts.db")
    _make_v3_db_with_old_fts(p)

    # before migration: the old unicode61 FTS does NOT match the singular/base form
    raw = sqlite3.connect(p)
    assert raw.execute("SELECT COUNT(*) FROM fts_anchors WHERE term MATCH 'modal'").fetchone()[0] == 0
    raw.close()

    db = connect(p)  # opening must rebuild fts_anchors with the porter stemmer

    assert "porter" in _fts_ddl(db)
    # after migration: a query for the base form finds the stored plural/tense (stemmed)
    assert db.execute("SELECT node_id FROM fts_anchors WHERE term MATCH 'modal'").fetchone()[0] == 1
    assert db.execute("SELECT node_id FROM fts_anchors WHERE term MATCH 'render'").fetchone()[0] == 1
    # rebuilt from node_anchors (the source of truth) — no data lost
    assert db.execute("SELECT COUNT(*) FROM fts_anchors").fetchone()[0] == 2


def test_v4_fts_migration_is_idempotent(tmp_path):
    p = str(tmp_path / "old_fts.db")
    _make_v3_db_with_old_fts(p)
    connect(p)  # v3 -> v4 (rebuild)
    connect(p)  # already porter -> must be a no-op, not a second rebuild
    db = connect(p)
    assert "porter" in _fts_ddl(db)
    assert db.execute("SELECT COUNT(*) FROM fts_anchors").fetchone()[0] == 2  # not doubled


def test_power_indexes_not_in_schema_constant():
    """The order-bug guard: a CREATE INDEX on power_run must NOT be in _SCHEMA — _SCHEMA
    runs before the column-adding migration, so indexing power_run there crashes an old
    DB (the bug we hit). The power_run COLUMN belongs in _SCHEMA's CREATE TABLE (fine);
    only its INDEX must be deferred to _migrate(), after the ALTERs."""
    import re

    from recall import db as db_mod

    # find every CREATE INDEX statement in _SCHEMA and assert none touches power_run
    index_stmts = re.findall(r"CREATE INDEX[^;]*", db_mod._SCHEMA, re.IGNORECASE)
    offenders = [s for s in index_stmts if "power_run" in s.lower()]
    assert not offenders, (
        "CREATE INDEX on power_run must not live in _SCHEMA (runs before _migrate); "
        f"move it into _migrate() after the columns exist. Offending: {offenders}"
    )

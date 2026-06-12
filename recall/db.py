"""SQLite schema + connection for the recall index.

This is the proven engine_proto.py schema, hardened for a real on-disk store:
WAL mode, a schema_version table, indexes on the hot lookup paths, and the facets
column from engine_proto2.py folded in. Pure stdlib — sqlite3 ships with FTS5.

The store is one file (`.recall/index.db`) living inside the project's `.mind/`
folder. It is reproducible: delete it and `recall init` rebuilds it from the code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Bump when the schema below changes in a way that needs a rebuild.
# v2 (2026-06-06, ADR-008/ADR-012): Power Mode reversibility columns
# (power_run + base_sha on nodes AND edges) + their indexes.
# v3 (2026-06-07): `author` on nodes — WHO wrote the change (from git), so the
# dashboard can show + filter knowledge by person (team overview).
# v4 (2026-06-07): fts_anchors tokenizer unicode61 -> `porter unicode61` (stemming).
# Unifies plural/tense morphology so a query token finds its stemmed cousin. The
# migration rebuilds an old index's FTS table in place (no re-index, no LLM).
# v5 (2026-06-07, ADR-016): `importance` (0-10) on nodes — causal weight from the
# dependency graph (PageRank over depends_on/co_changed edges). Write-time, model-free.
# Lets recall split a code-track (ranked by importance) from a knowledge-track so a
# commit never buries the central code symbol the query is really about.
SCHEMA_VERSION = 5

# The FTS tokenizer, named once so _SCHEMA and the v4 migration can't drift apart.
_FTS_TOKENIZE = "porter unicode61"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- nodes: code AND knowledge in one table. kind discriminates
-- ('lesson' | 'commit' | 'code-symbol' | 'plan' | ...).
CREATE TABLE IF NOT EXISTS nodes (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT,
    facets          TEXT,            -- comma-joined tags (security, ui, ...) — proto2
    file_path       TEXT,
    symbol          TEXT,
    line            INTEGER,
    stamped_at_sha  TEXT,            -- the SHA this knowledge is pinned to (freshness)
    origin          TEXT DEFAULT 'live',  -- bootstrap | power | live | human (ADR-008, reversible)
    power_run       INTEGER,         -- which Power-Mode run created this (NULL = not from a power run), ADR-008
    base_sha        TEXT,            -- repo HEAD the power run read against (audit + undo snapshot)
    author          TEXT,            -- WHO wrote this (git author name) — team overview + filter (v3)
    importance      REAL DEFAULT 0,  -- causal weight 0-10 (PageRank over the dep graph), v5/ADR-016
    created_at      INTEGER DEFAULT (strftime('%s','now'))
);

-- typed, directional edges with a per-edge freshness flag (verified).
CREATE TABLE IF NOT EXISTS edges (
    id              INTEGER PRIMARY KEY,
    src_node        INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst_node        INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    weight          REAL DEFAULT 1.0,
    stamped_at_sha  TEXT,
    verified        INTEGER DEFAULT 1,
    power_run       INTEGER,         -- which Power-Mode run created this edge (ADR-008, undo as a unit)
    base_sha        TEXT             -- repo HEAD the power run read against
);

-- canonical anchor vocabulary (ADR-005: closed vocabulary, deduped here).
CREATE TABLE IF NOT EXISTS anchors (
    id   INTEGER PRIMARY KEY,
    term TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS node_anchors (
    node_id   INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    anchor_id INTEGER NOT NULL REFERENCES anchors(id) ON DELETE CASCADE,
    PRIMARY KEY (node_id, anchor_id)
);

-- the fast read path: full-text over anchor terms, no embedding model.
-- `porter unicode61` STEMS both the stored anchor and the query token, so
-- modal/modals, lazy/lazily, render/rendering all unify (v4). This matters most on
-- human-written code with sparse comments, where lexical recall is the whole game and
-- a plural-vs-singular miss means the node is simply never found. Keep in sync with
-- _FTS_TOKENIZE below — the migration rebuilds an old index's FTS to match.
CREATE VIRTUAL TABLE IF NOT EXISTS fts_anchors
    USING fts5(term, node_id UNINDEXED, tokenize='porter unicode61');

-- transparent statistics for the dashboard (ADR-004) — written on every recall.
CREATE TABLE IF NOT EXISTS access_log (
    ts         INTEGER DEFAULT (strftime('%s','now')),
    query      TEXT,
    node_id    INTEGER,
    score      REAL,
    surfaced   INTEGER,
    latency_us INTEGER,
    consumer   TEXT
);

-- feedback (ADR-016, v5): deterministic 'was this hit useful?' signal. Incremented when
-- a surfaced node is actually clicked (dashboard) or touched in a follow-up edit. No
-- model: a pure counter. Folded into importance as a gentle +/-20% nudge so the graph
-- stays the backbone but recall LEARNS what truly helps over time.
CREATE TABLE IF NOT EXISTS node_feedback (
    node_id      INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    useful_count INTEGER DEFAULT 0,   -- surfaced AND then used (clicked / touched)
    missed_count INTEGER DEFAULT 0,   -- surfaced as top but NOT used (weak negative)
    updated_at   INTEGER DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_nodes_title  ON nodes(title);
CREATE INDEX IF NOT EXISTS idx_nodes_kind   ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_edges_src    ON edges(src_node);
CREATE INDEX IF NOT EXISTS idx_edges_dst    ON edges(dst_node);
CREATE INDEX IF NOT EXISTS idx_na_node      ON node_anchors(node_id);
"""
# NOTE: the power_run indexes are NOT in _SCHEMA — _SCHEMA runs before _migrate(), so
# on an old on-disk DB the column doesn't exist yet and `CREATE INDEX ON nodes(power_run)`
# would crash. They're created in _migrate() instead, AFTER the columns are guaranteed.

# Columns added per schema version, applied to an EXISTING table that predates them.
# SQLite has no `ADD COLUMN IF NOT EXISTS`, so each is guarded by a PRAGMA probe in
# _migrate() — making the migration idempotent + replay-safe (the project's Supabase
# branch-replay lesson, applied to SQLite). A fresh DB already has them via _SCHEMA
# above; _migrate only heals an older on-disk index.
_MIGRATIONS: dict[str, list[str]] = {
    "nodes": ["power_run", "base_sha", "author", "importance"],  # v2 + v3 + v5
    "edges": ["power_run", "base_sha"],
}
_MIGRATION_COL_TYPE = {
    "power_run": "INTEGER", "base_sha": "TEXT", "author": "TEXT", "importance": "REAL DEFAULT 0",
}


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open (creating if needed) a recall index and ensure the schema is present.

    Passing ":memory:" gives an ephemeral index — used by tests and `--dry`.
    On a file path the parent directory is created and WAL is enabled.
    """
    is_memory = str(path) == ":memory:"
    if not is_memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    if not is_memory:
        # WAL: concurrent readers (the dashboard, the hook) never block the writer.
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        # busy_timeout: a bulk writer (freshen() updates thousands of edges) must
        # wait out a transient lock from a concurrent reader / WAL checkpoint
        # instead of raising OperationalError('database is locked') immediately.
        db.execute("PRAGMA busy_timeout = 5000")  # 5s

    db.executescript(_SCHEMA)
    _migrate(db)
    _ensure_version(db)
    db.commit()
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """Add any columns an older on-disk index is missing — idempotent + replay-safe.

    `CREATE TABLE IF NOT EXISTS` leaves a pre-existing table untouched, so a v1 index
    on disk never gains the v2 reversibility columns from _SCHEMA alone. We probe each
    table's columns via PRAGMA and ADD only what's absent, so running this on a fresh
    DB (already complete), a v1 DB (gains the columns), or a v2 DB (no-op) all converge
    to the same shape with zero data loss. Idempotent: safe to run on every open().
    """
    for table, cols in _MIGRATIONS.items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
        for col in cols:
            if col not in have:
                db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {_MIGRATION_COL_TYPE[col]}"
                )
    # The power_run indexes live ONLY here, created after the columns are guaranteed to
    # exist (a fresh DB got them via _SCHEMA, an old DB just got them via the ALTERs
    # above). IF NOT EXISTS keeps it idempotent across re-opens.
    db.execute("CREATE INDEX IF NOT EXISTS idx_nodes_power ON nodes(power_run)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_edges_power ON edges(power_run)")
    _migrate_fts_tokenizer(db)


def _migrate_fts_tokenizer(db: sqlite3.Connection) -> None:
    """v4: rebuild fts_anchors with the porter stemmer if it predates it.

    An old on-disk index created fts_anchors with `unicode61` (no stemming); _SCHEMA's
    `CREATE ... IF NOT EXISTS` leaves it untouched. We detect the old tokenizer from the
    table's stored DDL and, only then, drop + recreate the FTS table and repopulate it
    from the durable source of truth (anchors JOIN node_anchors). This is LLM-free and
    loss-free: node_anchors is the real anchor store; fts_anchors is just a search mirror,
    so rebuilding it cannot lose data. Idempotent — a porter index is a no-op."""
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='fts_anchors'"
    ).fetchone()
    if row is None or "porter" in (row[0] or "").lower():
        return  # fresh DB (already porter via _SCHEMA) or already migrated
    db.execute("DROP TABLE fts_anchors")
    db.execute(
        f"CREATE VIRTUAL TABLE fts_anchors "
        f"USING fts5(term, node_id UNINDEXED, tokenize='{_FTS_TOKENIZE}')"
    )
    db.execute(
        "INSERT INTO fts_anchors(term, node_id) "
        "SELECT a.term, na.node_id FROM node_anchors na "
        "JOIN anchors a ON a.id = na.anchor_id"
    )


def _ensure_version(db: sqlite3.Connection) -> None:
    row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        db.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif int(row[0]) < SCHEMA_VERSION:
        # _migrate() already brought the columns/indexes up to date; record the bump.
        db.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )


def schema_version(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 0

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
SCHEMA_VERSION = 11  # v11: nodes.visibility — 'team' (shared/exported) vs 'private' (never leaves this machine/org)

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
    predicate       TEXT,            -- v8: re-runnable CHECK for this claim (contains:/absent:, predicate.py, arrow 1)
    outcome         TEXT,            -- v9: what CAME of the decision (learned / bit us / nothing new) — chain end, NOT the title
    visibility      TEXT DEFAULT 'team',  -- v11: 'team' travels with a shared/exported brain; 'private' never leaves this machine/org
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
    base_sha        TEXT,            -- repo HEAD the power run read against
    refined_from    TEXT             -- v6: the pre-refine kind, so `recall unrefine` can reset it
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

-- transparent statistics for the dashboard (ADR-004) — written on every read-path call.
-- `kind` (v7) tags WHICH action wrote the row: recall / brief / explain / stamp. This is
-- what the live activity console streams as a usage proof (the calls a user/AI made,
-- across CLI + MCP + dashboard, all sharing this one DB). Default 'recall' keeps the two
-- legacy recall() callers unchanged on an old index.
CREATE TABLE IF NOT EXISTS access_log (
    ts         INTEGER DEFAULT (strftime('%s','now')),
    query      TEXT,
    node_id    INTEGER,
    score      REAL,
    surfaced   INTEGER,
    latency_us INTEGER,
    consumer   TEXT,
    kind       TEXT DEFAULT 'recall',
    resp_chars INTEGER DEFAULT 0   -- v10: emitted response size (chars) per call — the context tax, measured
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
    "nodes": ["power_run", "base_sha", "author", "importance", "predicate", "outcome", "visibility"],  # v2/v3/v5/v8/v9/v11
    "edges": ["power_run", "base_sha", "refined_from"],          # + v6 refine reversibility
    "access_log": ["kind", "resp_chars"],                        # v7 activity tag · v10 response size
}
_MIGRATION_COL_TYPE = {
    "power_run": "INTEGER", "base_sha": "TEXT", "author": "TEXT", "importance": "REAL DEFAULT 0",
    "refined_from": "TEXT", "kind": "TEXT DEFAULT 'recall'", "predicate": "TEXT", "outcome": "TEXT",
    "resp_chars": "INTEGER DEFAULT 0", "visibility": "TEXT DEFAULT 'team'",
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
    # If the file is corrupt / not a sqlite db, the FIRST real access raises DatabaseError
    # — and on a corrupt file that's `PRAGMA journal_mode=WAL` (measured), BEFORE the
    # schema setup. Cover everything after connect() so the connection we just opened is
    # closed before propagating, or it leaks (and on Windows the leaked handle blocks the
    # caller's recovery unlink of the corrupt file). (P3 bug-hunt 2026-06-15.)
    try:
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
    except Exception:
        db.close()
        raise
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
                try:
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} {_MIGRATION_COL_TYPE[col]}"
                    )
                except sqlite3.OperationalError as e:
                    # The PRAGMA check above is TOCTOU: two processes sharing one
                    # .mind/index.db (the long-lived dashboard + a fresh CLI/hook) can
                    # both see the column absent and both ALTER — the loser gets
                    # "duplicate column name", which previously propagated out of open()
                    # and crashed the second process. The column now exists either way,
                    # so the migration goal is met: swallow ONLY that, re-raise the rest.
                    # (P2 bug-hunt 2026-06-15.)
                    if "duplicate column name" not in str(e).lower():
                        raise
    # The power_run indexes live ONLY here, created after the columns are guaranteed to
    # exist (a fresh DB got them via _SCHEMA, an old DB just got them via the ALTERs
    # above). IF NOT EXISTS keeps it idempotent across re-opens.
    db.execute("CREATE INDEX IF NOT EXISTS idx_nodes_power ON nodes(power_run)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_edges_power ON edges(power_run)")
    # file lookups go through REPLACE(file_path,'\','/') everywhere (brief/blast/drift/known)
    # to normalize win32 backslashes; a plain index can't serve a function-wrapped column, so
    # this EXPRESSION index matches that exact expression. Measured: brief's why_rows query
    # 8.3ms -> 1.3ms (SCAN nodes -> COVERING INDEX), the biggest single term in brief()
    # latency (perf pass 2026-06-18). Lives HERE, not in _SCHEMA, and is column-guarded:
    # the FTS-migration tests build a synthetic pre-file_path nodes table, and an index that
    # references a missing column would crash executescript (same class as the power_run note).
    have_nodes = {r[1] for r in db.execute("PRAGMA table_info(nodes)").fetchall()}
    if "file_path" in have_nodes:
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_nodes_filepath_norm "
            "ON nodes(REPLACE(file_path, '\\', '/'))"
        )
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

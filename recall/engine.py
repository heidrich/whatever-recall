"""The engine — one class, fixed API. All intelligence lives here; adapters are
thin shells that call stamp() / recall() / init().

Synthesis of the proven prototypes, hardened:
  - engine_proto.py     -> stamp_from_commit, the 3-level recall (recursive CTE)
  - engine_proto2.py    -> facet weights + context boost (read from rules.md)
  - real_antisludge.py  -> dedup-via-recall with DISTINCT anchors (the ADR-005 fix)

LLM-free on the read path by design (0 tokens, offline, local-LLM-friendly later).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from recall import db as _db
from recall.anchors import canonicalize_tags, extract_anchors, tokenize_query
from recall.rules import Rules, load_rules

# BM25 constants (deliberately module constants, NOT rules.md knobs — the governance
# surface stays small; expose via rules.md with core-veto bounds only if a real project
# ever needs tuning). k1 caps porter over-stem floods, b=0.4 is the moderate length
# penalty validated on the live index (369-anchor task nodes stop dominating).
_BM25_K1 = 1.2
_BM25_B = 0.4
# Hook queries are whole edit texts (100+ tokens measured); _bm25_scores costs one FTS
# query per token. Oversized queries keep only the RAREST tokens (df-ranked) — typed
# questions are far below this and never change.
_QUERY_TOKEN_CAP = 32

# Test-file shapes across ecosystems: tests/ dirs, pytest test_*.py, Go *_test.go,
# JS/TS *.spec.ts / *.test.tsx. Used by the code track's soft downweight (ADR-028).
_TEST_FILE_RE = re.compile(
    r"(?:^|/)tests?/|(?:^|/)test_[^/]*$|_test\.[a-z0-9]+$|\.(?:spec|test)\.[a-z0-9]+$"
)


def _is_test_file(file_path: str | None) -> bool:
    """True when the path looks like a test file. The code track halves test
    relevance (never excludes): a "where is X?" question wants the implementation;
    the test that exercises X is still findable, just never ABOVE the source."""
    return bool(_TEST_FILE_RE.search((file_path or "").replace("\\", "/").lower()))


class Index:
    """A recall index over one project. Open it, stamp knowledge, recall it."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        rules: Rules | None = None,
        repo: Path | None = None,
        db_path: str | Path = ":memory:",
    ):
        self.db = conn
        self.rules = rules if rules is not None else Rules.defaults()
        # Remembered so freshen() can find the repo to diff against without the
        # caller having to re-derive it (the CLI/hook just call idx.freshen()).
        self._repo = repo
        self._db_path = db_path
        # BM25 corpus stats (n_docs, total_anchor_rows) — two full-table COUNTs that
        # were recomputed on EVERY recall. Cached here; every anchor/node mutation
        # calls _invalidate_corpus_stats() (hook hot-path, measured on a 7k-node index).
        self._corpus_stats: tuple[int, int] | None = None

    # ------------------------------------------------------------------ open
    @classmethod
    def open(cls, path: str | Path = ":memory:", repo: str | Path | None = None) -> "Index":
        """Open (creating if needed) an index file, loading layered rules.

        `repo` lets rules.md be layered ~/.recall < <repo>/.recall — when omitted
        we infer the repo from the index path's grandparent (.mind/index.db -> repo).
        """
        conn = _db.connect(path)
        repo_path = Path(repo) if repo else _infer_repo(path)
        rules = load_rules(repo_path)
        return cls(conn, rules, repo=repo_path, db_path=path)

    # ----------------------------------------------------------------- stamp
    def stamp(
        self,
        title: str,
        *,
        body: str | None = None,
        anchors: list[str] | None = None,
        tags: list[str] | None = None,
        kind: str = "lesson",
        edges: list[tuple[str, str]] | None = None,
        sha: str | None = None,
        file_path: str | None = None,
        symbol: str | None = None,
        line: int | None = None,
        origin: str = "live",
        power_run: int | None = None,
        base_sha: str | None = None,
        author: str | None = None,
        dedup: bool = True,
    ) -> dict[str, Any]:
        """Stamp a node. Returns {action: NEW|MERGE, node_id, ...}.

        ADR-005 fix: before creating, recall() the node's own anchors against the
        index (dedup-via-recall over FTS5, DISTINCT anchors). On high overlap we
        extend the existing node instead of creating a near-duplicate.

        power_run/base_sha (ADR-008): when a Power-Mode run calls stamp(), every
        node + edge it writes carries the run number so undo_power_run() can lift
        the whole run out as a unit. NULL on normal live/bootstrap stamps. A MERGE
        onto an existing node returns the anchors it ADDED (added_anchors) so the
        caller can record them for a precise synonym-onto-bootstrap undo.
        """
        anchor_set = set(a.strip().lower() for a in (anchors or []) if a.strip())
        if not anchor_set:
            anchor_set = extract_anchors(f"{title} {body or ''}")
        facets = canonicalize_tags(
            tags or [], self.rules.tag_aliases, self.rules.allowed_tags
        )

        if dedup and anchor_set:
            existing, overlap = self._dedup_via_recall(anchor_set)
            if existing is not None and overlap >= self.rules.dedup_threshold:
                added = self._add_anchors(existing, anchor_set)  # enrich, don't duplicate
                if facets:
                    self._merge_facets(existing, facets)
                self.db.commit()
                title_existing = self.db.execute(
                    "SELECT title FROM nodes WHERE id=?", (existing,)
                ).fetchone()[0]
                return {
                    "action": "MERGE",
                    "node_id": existing,
                    "into": title_existing,
                    "overlap": round(overlap, 2),
                    # the synonyms this stamp added onto an existing node. Power Mode
                    # records these so undo can remove EXACTLY them (they can't CASCADE —
                    # the node they hang on isn't a power node). ADR-008 undo trap.
                    "added_anchors": sorted(added),
                }

        node_id = self._insert_node(
            kind, title, body, facets, file_path, symbol, line, sha, origin,
            power_run, base_sha, author,
        )
        self._add_anchors(node_id, anchor_set)
        for ekind, target in (edges or []):
            self._add_edge_to_target(node_id, ekind, target, sha, power_run, base_sha)
        self.db.commit()
        return {"action": "NEW", "node_id": node_id, "anchors": len(anchor_set)}

    def stamp_from_commit(self, commit_msg: str, commit_sha: str,
                          author: str | None = None) -> dict[str, Any] | None:
        """Parse Recall-* trailers from a commit message and stamp self-acting.

        Returns None for a normal commit without trailers (the system ignores it).
        Trailers: Recall-anchors, Recall-why, Recall-tags, Recall-edge (kind -> target).
        """
        m_anchors = re.search(r"^Recall-anchors:\s*(.+)$", commit_msg, re.MULTILINE)
        if not m_anchors or not m_anchors.group(1).strip():
            return None  # no trailer, or an empty/whitespace-only anchors declaration
        m_why = re.search(r"^Recall-why:\s*(.+)$", commit_msg, re.MULTILINE)
        m_tags = re.search(r"^Recall-tags:\s*(.+)$", commit_msg, re.MULTILINE)
        edges = [
            (m.group(1).strip(), m.group(2).strip())
            for m in re.finditer(
                r"^Recall-edge:\s*(\w+)\s*->\s*(.+)$", commit_msg, re.MULTILINE
            )
        ]
        # Title = the why line, else the subject — but never a trailer line itself
        # (a degenerate commit that is only trailers must not title a node "Recall-anchors: …").
        subject = commit_msg.strip().splitlines()[0] if commit_msg.strip() else ""
        if m_why:
            title = m_why.group(1).strip()
        elif _is_trailer_line(subject):
            title = m_anchors.group(1).strip()
        else:
            title = subject
        # Level-2 meaning = the explanation paragraph: the commit body minus the
        # subject line and the Recall-* / Co-Authored-By trailers. Falls back to
        # the why line so recall never shows an empty meaning.
        body = _commit_explanation(commit_msg) or title
        return self.stamp(
            title=title,
            body=body,
            anchors=[a for a in m_anchors.group(1).split(",")],
            tags=[t for t in m_tags.group(1).split(",")] if m_tags else None,
            kind="lesson",
            edges=edges,
            sha=commit_sha,
            origin="live",
            author=author,
        )

    # ----------------------------------------------- power runs (ADR-008, reversible)
    def record_power_run(self, run: int, info: dict[str, Any]) -> None:
        """Persist a Power-Mode run's bookkeeping into the generic meta store as
        `power_run:<N>` -> JSON. No new table (the plan's STEP 1 decision). `info`
        carries base_sha/scope/model/token counts/status AND `added_anchors`
        (the synonym-onto-existing-node ledger undo needs)."""
        self.db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (f"power_run:{run}", json.dumps(info)),
        )
        self.db.commit()

    def power_run_info(self, run: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (f"power_run:{run}",)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def list_power_runs(self) -> list[dict[str, Any]]:
        """Every recorded run, newest-first. Free, no LLM — backs `recall power --list`."""
        rows = self.db.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'power_run:%'"
        ).fetchall()
        runs = []
        for key, value in rows:
            info = json.loads(value)
            info["run"] = int(key.split(":", 1)[1])
            runs.append(info)
        runs.sort(key=lambda r: -r["run"])
        return runs

    def next_power_run(self) -> int:
        """The next free run number (max recorded + 1, or 1)."""
        runs = self.list_power_runs()
        return (runs[0]["run"] + 1) if runs else 1

    def undo_power_run(self, run: int) -> dict[str, Any]:
        """Lift one Power-Mode run out as a unit — the proof of full reversibility.

        Deletes its nodes + edges (FK CASCADE clears node_anchors/fts for the nodes),
        then removes the synonyms it had grafted onto PRE-EXISTING nodes (those can't
        CASCADE — no power node holds them; we recorded them in the run's ledger).
        Leaves origin='bootstrap'/'live' untouched. Idempotent: a second call is a
        no-op. Flips the run's meta status to 'undone'."""
        info = self.power_run_info(run) or {}
        nodes = [r[0] for r in self.db.execute(
            "SELECT id FROM nodes WHERE power_run=?", (run,)
        ).fetchall()]
        # edges first is unnecessary (CASCADE handles edges of deleted nodes), but
        # power edges between two pre-existing nodes carry their own run tag.
        self.db.execute("DELETE FROM edges WHERE power_run=?", (run,))
        for nid in nodes:
            self._delete_node(nid)
        removed_syn = self._remove_recorded_synonyms(info.get("added_anchors", {}))
        info["status"] = "undone"
        self.record_power_run(run, info)  # commits
        return {
            "run": run,
            "nodes_removed": len(nodes),
            "synonyms_removed": removed_syn,
        }

    def undo_power_all(self) -> dict[str, Any]:
        """Remove every Power-Mode artifact → the raw bootstrap+live index again.

        WHERE filters origin='power' only, so the bootstrap base and live commits
        are untouched. Walks each recorded run for its synonym ledger."""
        runs = [r["run"] for r in self.list_power_runs() if r.get("status") != "undone"]
        total_nodes = total_syn = 0
        for run in runs:
            res = self.undo_power_run(run)
            total_nodes += res["nodes_removed"]
            total_syn += res["synonyms_removed"]
        # Safety net: any stray origin='power' node without a run tag (shouldn't exist,
        # but undo must never leave power data behind).
        stray = [r[0] for r in self.db.execute(
            "SELECT id FROM nodes WHERE origin='power'"
        ).fetchall()]
        for nid in stray:
            self._delete_node(nid)
        self.db.commit()
        return {"runs": len(runs), "nodes_removed": total_nodes + len(stray), "synonyms_removed": total_syn}

    def forget(self, node_id: int, *, force: bool = False) -> dict[str, Any]:
        """Remove a single node (CASCADE clears its anchors + edges).

        Refuses an origin='bootstrap' node without force — the bootstrap base is the
        sacred, reproducible ground truth (ADR-008). Power/live nodes drop freely."""
        row = self.db.execute(
            "SELECT origin FROM nodes WHERE id=?", (node_id,)
        ).fetchone()
        if row is None:
            return {"removed": False, "reason": "no such node", "node_id": node_id}
        if row[0] == "bootstrap" and not force:
            return {
                "removed": False,
                "reason": "bootstrap node is sacred; pass force=True to override",
                "node_id": node_id,
            }
        self._delete_node(node_id)
        self.db.commit()
        return {"removed": True, "node_id": node_id, "origin": row[0]}

    def clear_file_symbols(self, rel: str) -> int:
        """Remove the bootstrap code-symbol nodes for ONE file, so the incremental
        watcher can re-parse just that file without duplicating its symbols.

        Touches only kind='code-symbol' AND origin='bootstrap' for that exact path —
        lessons/commits pinned to the file, and any live/power node, are left alone.
        FTS mirror by hand (no FK), nodes via the cascade-safe delete. Returns the count."""
        rel = (rel or "").replace("\\", "/")
        if not rel:
            return 0
        ids = [r[0] for r in self.db.execute(
            "SELECT id FROM nodes WHERE kind='code-symbol' AND origin='bootstrap' "
            "AND REPLACE(file_path,'\\','/')=?", (rel,)
        ).fetchall()]
        for nid in ids:
            self._delete_node(nid)
        return len(ids)

    def clear_bootstrap(self) -> int:
        """Remove every origin='bootstrap' node so init() can rebuild idempotently.

        The bootstrap layer (code-map symbols, historical commits, imported lessons)
        is fully regenerable from code + git, and it carries dedup=False for code
        symbols — so a plain re-init (CLI re-run, or the live watcher re-indexing each
        commit) would DUPLICATE the whole code map every time. Clearing the bootstrap
        layer first makes init() a true rebuild, not an append.

        It touches ONLY origin='bootstrap': live stamps (origin='live') and Power-Mode
        nodes (power_run set) survive untouched. FK ON DELETE CASCADE clears each
        node's edges + node_anchors; the fts_anchors virtual table has no FK, so we
        mirror the delete by hand first. Returns how many nodes were removed."""
        ids = [r[0] for r in self.db.execute(
            "SELECT id FROM nodes WHERE origin='bootstrap'"
        ).fetchall()]
        if not ids:
            return 0
        CHUNK = 900  # SQLite caps bound vars at ~999
        for i in range(0, len(ids), CHUNK):
            batch = ids[i:i + CHUNK]
            ph = ",".join("?" * len(batch))
            # fts mirror first (no FK), then the nodes (edges + anchors CASCADE away)
            self.db.execute(f"DELETE FROM fts_anchors WHERE node_id IN ({ph})", batch)
            self.db.execute(f"DELETE FROM nodes WHERE id IN ({ph})", batch)
        self.db.commit()
        self._invalidate_corpus_stats()
        return len(ids)

    def clear_tasks(self) -> int:
        """Remove every task node (ADR-017) so tasks can be re-indexed without dupes
        (tasks are dedup=False). Touches ONLY kind='task'; edges + anchors CASCADE,
        fts mirror cleared by hand. Returns how many were removed."""
        ids = [r[0] for r in self.db.execute(
            "SELECT id FROM nodes WHERE kind='task'").fetchall()]
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        self.db.execute(f"DELETE FROM fts_anchors WHERE node_id IN ({ph})", ids)
        self.db.execute(f"DELETE FROM nodes WHERE id IN ({ph})", ids)
        # Sweep task-anchor file nodes that no longer carry any edge (their only reason to
        # exist was to anchor a now-deleted task link). Re-index recreates them if needed,
        # so this stays idempotent and avoids a slow orphan leak on a non-code file.
        self.db.execute(
            "DELETE FROM nodes WHERE origin='task-anchor' AND kind='file' "
            "AND id NOT IN (SELECT src_node FROM edges UNION SELECT dst_node FROM edges)"
        )
        self.db.commit()
        self._invalidate_corpus_stats()
        return len(ids)

    def _delete_node(self, node_id: int) -> None:
        """Delete a node and its FTS mirror. FK ON DELETE CASCADE clears node_anchors
        and any edges; fts_anchors is a virtual table with no FK, so mirror by hand."""
        self.db.execute("DELETE FROM fts_anchors WHERE node_id=?", (node_id,))
        self.db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        self._invalidate_corpus_stats()

    def _remove_recorded_synonyms(self, added: dict[str, list[str]]) -> int:
        """Undo the synonym-onto-existing-node grafts a run recorded.

        `added` maps node_id (str, JSON keys are strings) -> list of terms this run
        linked onto that already-existing node. We remove exactly those node_anchor
        rows + their fts mirror — never the node's original anchors. Skips a node that
        was itself deleted above (its rows are already gone via CASCADE)."""
        removed = 0
        for node_key, terms in (added or {}).items():
            node_id = int(node_key)
            still = self.db.execute(
                "SELECT 1 FROM nodes WHERE id=? LIMIT 1", (node_id,)
            ).fetchone()
            if not still:
                continue  # node gone -> its anchors already cleared by CASCADE
            for term in terms:
                term = term.strip().lower()
                aid = self.db.execute(
                    "SELECT id FROM anchors WHERE term=?", (term,)
                ).fetchone()
                if not aid:
                    continue
                deleted = self.db.execute(
                    "DELETE FROM node_anchors WHERE node_id=? AND anchor_id=?",
                    (node_id, aid[0]),
                ).rowcount
                if deleted:
                    self.db.execute(
                        "DELETE FROM fts_anchors WHERE node_id=? AND term=?",
                        (node_id, term),
                    )
                    removed += 1
        if removed:
            self._invalidate_corpus_stats()
        return removed

    # ---------------------------------------------------------------- recall
    def recall(
        self,
        query: str,
        *,
        intent: str | None = None,
        edit_context: str | None = None,
        topk: int = 3,
        consumer: str = "cli",
    ) -> dict[str, Any]:
        """Recall the 3 levels: hit / meaning / relation. Silent below the floor.

        Score = BM25 relevance (per-term IDF, saturated tf, length-normalized over the
        node's anchor count) * max(facet_weight) * context_boost. The floor is still
        checked on RAW hits (so weighting can't manufacture a hit from nothing) — the
        raw-hit query is byte-identical to the pre-BM25 engine.
        """
        t0 = time.perf_counter()
        topk = max(1, topk)  # defense-in-depth: a 0/negative topk must not silence real hits
        # deterministic iteration; project rules may ADD query stopwords on top of
        # the shipped QUERY_STOP set (additive only — governance via rules.md)
        toks = sorted({t for t in tokenize_query(query)
                       if t and t not in self.rules.query_stopwords})
        if not toks:
            return self._silenced("no tokens", t0)

        raw, bm25 = self._bm25_scores(toks)
        if not raw:
            return self._silenced("no match", t0)

        boosted_facet = self._context_facet(edit_context)
        floor = self.rules.silence_floor
        scored: list[tuple[int, int, float, list[str]]] = []
        for node_id, hits in raw.items():
            if hits < floor:
                continue  # RAW floor — weighting cannot manufacture a hit from nothing
            facets = self._node_facets(node_id)
            if self._is_tabu(facets):
                continue  # rules.md stay_silent_on — suppressed regardless of score
            fw = max([self.rules.facet_weight(f) for f in facets] or [1.0])
            boost = self.rules.context_multiplier if (boosted_facet and boosted_facet in facets) else 1.0
            score = bm25.get(node_id, 0.0) * fw * boost
            scored.append((node_id, hits, score, facets))

        # explicit tie-break (score, raw hits, node_id) — the old sort left equal scores
        # in undefined GROUP-BY emission order, a latent nondeterminism
        scored.sort(key=lambda x: (-x[2], -x[1], x[0]))
        scored = self._dedupe_results(scored)  # collapse same-(title,file) duplicates
        scored = scored[:topk]
        latency_us = round((time.perf_counter() - t0) * 1_000_000)

        if not scored:
            res = self._silenced(f"score < floor ({floor})", t0, latency_us=latency_us)
            self._log(query, None, 0, 0, latency_us, consumer)
            return res

        results = [self._build_levels(nid, hits, sc, set(toks)) for nid, hits, sc, _ in scored]
        top = scored[0]
        self._log(query, top[0], top[2], 1, latency_us, consumer)

        # Three parallel tracks (ADR-016): a noisy commit must never bury the central
        # code symbol a query is about, so we DON'T put them in one sorted list. Each
        # track answers a different question I (the LLM) actually ask while coding:
        #   code      — WHERE is it?      by text relevance, tests soft-downweighted,
        #                                 importance breaks ties (ADR-028 — importance-
        #                                 first measured 8% r@3 on location questions)
        #   knowledge — WHY is it so?     commits/lessons/ADRs by text relevance
        #   blast     — WHAT do I break?  who depends on the top code hit (impact)
        tracks = self._build_tracks(set(toks), boosted_facet,
                                    code_k=max(5, topk), know_k=max(5, topk),
                                    scores=(raw, bm25))

        return {
            "silenced": False,
            "latency_us": latency_us,
            "boosted_facet": boosted_facet,
            "results": results,          # back-compat: the old mixed, score-sorted list
            "code": tracks["code"],
            "knowledge": tracks["knowledge"],
            "blast_radius": tracks["blast_radius"],
            "open_tasks": tracks["open_tasks"],
        }

    # ------------------------------------------------------- brief (Wave A, ADR-018)
    def brief(self, file_path: str, *, why_k: int = 6, sym_k: int = 30) -> dict[str, Any]:
        """Pre-Edit Briefing — everything recall knows about ONE file, before I touch it.

        Where recall() answers a *query* in tracks, brief() answers a *file*: it bundles
        the five read-only views that keep me from silently undoing a deliberate decision.
          why        — commits/lessons/ADRs pinned to the file (WHY it is the way it is),
                       newest-meaningful first; never a code-symbol (those are `symbols`)
          breaks     — files that depend on this one (blast radius) → what I risk breaking
          depends_on — the static depends_on chain this file leans on
          open_tasks — unfinished plans/tasks wired to the file (ADR-017, standing intent)
          symbols    — the code-symbols defined in the file (what's in it)
        Plus `known` (does the index have this file at all) and `drift` (the worst drift
        level recorded on any of the file's pinned nodes, after a freshen()).

        Pure SQL over the already-stamped graph — no model is run (ADR-014). A file the
        index has never seen returns an empty-but-shaped briefing, never an error."""
        rel = (file_path or "").replace("\\", "/")
        # symbols defined IN this file — the "what's in it" view, by source order.
        # A file carries one symbol=NULL/line=NULL representative node (the anchor that
        # holds its file→file dependency/co_changed edges); that is the file itself, not
        # a symbol — exclude it so the briefing lists real defs only.
        sym_rows = self.db.execute(
            "SELECT id, symbol, title, line FROM nodes "
            "WHERE kind='code-symbol' AND REPLACE(file_path,'\\','/')=? "
            "AND (symbol IS NOT NULL OR line IS NOT NULL) "
            "ORDER BY line LIMIT ?",
            (rel, sym_k),
        ).fetchall()
        symbols = [
            {"node_id": r["id"], "symbol": r["symbol"] or r["title"], "line": r["line"]}
            for r in sym_rows
        ]
        # knowledge pinned to the file: commits/lessons/ADRs/plans — NOT code-symbols
        # (a symbol is structure, not the why). Newest id first ≈ most recent knowledge.
        why_rows = self.db.execute(
            "SELECT id, kind, title, body, stamped_at_sha FROM nodes "
            "WHERE REPLACE(file_path,'\\','/')=? AND kind NOT IN ('code-symbol','task','file') "
            "ORDER BY id DESC LIMIT ?",
            (rel, why_k),
        ).fetchall()
        why = [
            {
                "node_id": r["id"], "kind": r["kind"], "title": r["title"],
                "why": (r["body"] or "").splitlines()[0] if r["body"] else "",
                "sha": (r["stamped_at_sha"] or "")[:7],
                "drift": self._node_drift(r["id"]),
            }
            for r in why_rows
        ]
        known = bool(symbols or why_rows or self.db.execute(
            "SELECT 1 FROM nodes WHERE REPLACE(file_path,'\\','/')=? LIMIT 1", (rel,)
        ).fetchone())
        return {
            "file": rel,
            "known": known,
            "symbols": symbols,
            "why": why,
            "depends_on": self._file_dependencies(rel),
            "breaks": self._blast_radius(rel),
            "open_tasks": self.open_tasks_for_file(rel),
            "drift": self._file_drift(rel),
        }

    # ------------------------------------------------- contested spots (Wave B, ADR-019)
    def contested_spots(self, *, churn: dict[str, int] | None = None, repo: str | Path | None = None,
                        min_churn: int = 2, limit: int = 20) -> list[dict[str, Any]]:
        """Uncertainty hotspots — the code the team kept changing (Wave B, ADR-019).

        Contested = high CHURN (many commits touched it) AND high ENTANGLEMENT (it drags
        other files along when it moves, the co_changed degree). This is deliberately NOT
        importance: a load-bearing file written once is important but not contested; a file
        rewritten ten times is where time burns. A file touched by < min_churn commits was
        never re-litigated, so it is excluded no matter how entangled.

        `churn` is path -> commit-count (from recall.contested.file_churn, git-read, model-
        free). If omitted we read it from `repo` (or the index's repo). Entanglement is read
        from the co_changed graph here. All read-only, no model (ADR-014).

        score = churn * (1 + entanglement / DEGREE_SCALE) — churn is the spine, entanglement
        a bounded multiplier so an equally-churned but more-tangled file ranks above a lonely
        one without letting a single hub dominate purely on degree."""
        if churn is None:
            from recall.contested import file_churn
            target = repo or self._repo or _infer_repo(self._db_path)
            churn = file_churn(target) if target is not None else {}
        if not churn:
            return []
        degree = self._co_changed_degrees()
        DEGREE_SCALE = 4.0  # at 4 co_changed partners the entanglement factor doubles the score
        spots = []
        for path, n in churn.items():
            if n < min_churn:
                continue  # one commit is not a back-and-forth
            rel = path.replace("\\", "/")
            ent = degree.get(rel, 0)
            score = n * (1.0 + ent / DEGREE_SCALE)
            spots.append({"file": rel, "churn": n, "entanglement": ent, "score": round(score, 2)})
        spots.sort(key=lambda s: (-s["score"], -s["churn"], s["file"]))
        return spots[:limit]

    def _co_changed_degrees(self) -> dict[str, int]:
        """path -> number of DISTINCT other files it shares a co_changed edge with.

        co_changed edges are symmetric and live between files' representative code nodes, so
        we map each endpoint node back to its file and count distinct partner files (a file
        co-changing with itself, or twice with the same partner, counts once). Read-only."""
        rows = self.db.execute(
            """
            SELECT REPLACE(ns.file_path,'\\','/') AS a, REPLACE(nd.file_path,'\\','/') AS b
              FROM edges e
              JOIN nodes ns ON ns.id = e.src_node
              JOIN nodes nd ON nd.id = e.dst_node
             WHERE e.kind = 'co_changed'
               AND ns.file_path IS NOT NULL AND nd.file_path IS NOT NULL
            """
        ).fetchall()
        partners: dict[str, set[str]] = {}
        for a, b in rows:
            if a == b:
                continue
            partners.setdefault(a, set()).add(b)
        return {f: len(p) for f, p in partners.items()}

    def _file_drift(self, rel: str) -> str | None:
        """The worst drift level recorded on any node pinned to this file, or None if
        the index was never freshened. 'uncommitted' (edited) outranks 'committed'
        (drifted) outranks 'fresh' — so the briefing shows the loudest warning."""
        rank = {"uncommitted": 3, "committed": 2, "fresh": 1}
        worst = None
        worst_rank = 0
        rows = self.db.execute(
            "SELECT id FROM nodes WHERE REPLACE(file_path,'\\','/')=?", (rel,)
        ).fetchall()
        for (nid,) in rows:
            level = self._node_drift(nid)
            if level and rank.get(level, 0) > worst_rank:
                worst, worst_rank = level, rank[level]
        return worst

    def onboarding(self, *, top_k: int = 12, dec_k: int = 8, task_k: int = 12,
                   contested_k: int = 6) -> dict[str, Any]:
        """"Explain me this repo" — Wave C, ADR-020. The generated path a new dev (or a
        fresh AI session) needs to get oriented, from the already-indexed graph:

          top_files  — the load-bearing files, by causal importance (start reading HERE)
          decisions  — the must-know decisions/ADRs (foundation lessons, by importance)
          in_progress— every OPEN task/plan across the repo (what's being worked on now)
          contested  — where the team keeps burning time (Wave B, churn × entanglement)
          counts     — honest headline numbers (files / lessons / decisions / open tasks)

        Reuses the Wave A/B building blocks (importance, contested_spots). Read-only,
        model-free (ADR-014): pure SQL + arithmetic, 0 tokens, offline. An empty index
        returns an empty-but-shaped dict, never an error."""
        # top files: one row per file = its most important code-symbol's importance.
        file_rows = self.db.execute(
            """
            SELECT REPLACE(file_path,'\\','/') AS f, MAX(importance) AS imp,
                   COUNT(*) AS syms
              FROM nodes
             WHERE kind='code-symbol' AND file_path IS NOT NULL AND file_path != ''
             GROUP BY f
             ORDER BY imp DESC, syms DESC, f
             LIMIT ?
            """,
            (top_k,),
        ).fetchall()
        top_files = [{"file": r["f"], "importance": round(r["imp"] or 0, 1),
                      "symbols": r["syms"]} for r in file_rows]

        # must-know decisions, two tiers: real ADR-titled nodes first (PREFIX match —
        # the old '%ADR-%' substring let CHANGELOG stubs like '[Unreleased] … (ADR-019)'
        # and section headings leak in), newest decision first; foundation-tagged
        # lessons only fill the remainder (generality for repos without ADR naming).
        dec_rows = self.db.execute(
            """
            SELECT id, kind, title, file_path, importance,
                   COALESCE(stamped_at_sha,'') AS sha
              FROM nodes
             WHERE kind IN ('lesson','decision')
               AND (title LIKE 'ADR-%' OR title LIKE 'ADR %' OR kind='decision'
                    OR facets LIKE '%foundation%')
            """,
        ).fetchall()
        _adr_num = re.compile(r"^ADR[- ](\d+)")
        adrs, fill = [], []
        for r in dec_rows:
            m = _adr_num.match(r["title"] or "")
            if m:
                adrs.append((int(m.group(1)), r))
            elif r["kind"] == "decision":
                adrs.append((-1, r))
            else:
                fill.append(r)
        adrs.sort(key=lambda t: (-t[0], -t[1]["id"]))  # newest decision first, deterministic
        fill.sort(key=lambda r: (-(r["importance"] or 0), -r["id"]))
        # Pure recency drops the FOUNDING decisions once a repo has more ADRs than
        # dec_k (review follow-up: ADR-001 fell out of must-know). Reserve two slots
        # for the lowest-numbered ADRs — the foundations a newcomer must not miss —
        # and fill the rest newest-first.
        if len(adrs) > dec_k:
            numbered = [t for t in adrs if t[0] >= 0]
            founding = sorted(numbered, key=lambda t: t[0])[:2]
            founding_ids = {r["id"] for _, r in founding}
            newest = [t for t in adrs if t[1]["id"] not in founding_ids][:max(0, dec_k - len(founding))]
            picked = [r for _, r in newest] + [r for _, r in founding]
        else:
            picked = [r for _, r in adrs[:dec_k]]
        picked += fill[:max(0, dec_k - len(picked))]
        decisions = [{"node_id": r["id"], "title": r["title"],
                      "file": (r["file_path"] or "").replace("\\", "/"),
                      "importance": round(r["importance"] or 0, 1),
                      "sha": (r["sha"] or "")[:7]} for r in picked]

        # in progress: every OPEN task/plan (a task is open unless its facets carry a
        # terminal status). Newest first — what's being worked on right now.
        task_rows = self.db.execute(
            "SELECT id, title, file_path, facets FROM nodes WHERE kind='task' "
            "ORDER BY id DESC"
        ).fetchall()
        in_progress = []
        for r in task_rows:
            fs = set((r["facets"] or "").split(","))
            status = next((s for s in ("done", "dropped", "deferred", "open") if s in fs), "open")
            if status != "open":
                continue
            in_progress.append({"node_id": r["id"], "title": r["title"],
                                "file": (r["file_path"] or "").replace("\\", "/"),
                                "status": "open"})
            if len(in_progress) >= task_k:
                break

        # where time burns (Wave B) — best-effort: needs git churn, empty without it.
        try:
            contested = self.contested_spots(limit=contested_k)
        except Exception:
            contested = []

        counts = {
            "files": self.db.execute(
                "SELECT COUNT(DISTINCT file_path) FROM nodes WHERE kind='code-symbol' "
                "AND file_path IS NOT NULL AND file_path != ''").fetchone()[0],
            "lessons": self.db.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind IN ('lesson','decision')").fetchone()[0],
            "decisions": len(decisions),
            "open_tasks": len(in_progress),
            "commits": self.db.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind='commit'").fetchone()[0],
        }
        return {
            "repo_files": counts["files"],
            "top_files": top_files,
            "decisions": decisions,
            "in_progress": in_progress,
            "contested": contested,
            "counts": counts,
        }

    def _file_importance(self, rel: str) -> float:
        """A file's own causal importance = its most important code-symbol (0 if unknown).
        The same MAX(importance) read onboarding() uses for top_files, for one file."""
        row = self.db.execute(
            "SELECT MAX(importance) FROM nodes WHERE kind='code-symbol' "
            "AND REPLACE(file_path,'\\','/')=?", (rel,),
        ).fetchone()
        return round((row[0] or 0.0) if row else 0.0, 1)

    # ------------------------------------------------- review a change (Wave D, ADR-021)
    def review(self, sha: str | None = None, *, files: list[str] | None = None,
               repo: str | Path | None = None, risk_dependents: int = 3,
               risk_importance: float = 4.0) -> dict[str, Any]:
        """Review a change — what brief() does for one file, for a whole COMMIT or staged set.

        Given a commit `sha` (default HEAD) we read the files it touched; given `files`
        directly (the pre-commit path: staged files, no commit yet) we review those. For
        each file we bundle the briefing fields that matter to a reviewer — its own
        importance, what it breaks (blast radius), why it is the way it is, open tasks,
        drift — and then single out the RISK files: load-bearing (importance ≥
        risk_importance) OR many dependents (≥ risk_dependents) OR carrying an open task.
        That risk list is what the pre-commit hook warns on (never blocks) and what
        `recall review <sha> --for-prompt` renders as PR-markdown.

        Read-only, model-free (ADR-014): pure SQL over the stamped graph + one git read for
        the file list. A commit/file the index never saw yields an empty-but-shaped entry."""
        if files is None:
            from recall.contested import commit_files
            target = repo or self._repo or _infer_repo(self._db_path)
            files = commit_files(target, sha or "HEAD") if target is not None else []
        reviewed: list[dict[str, Any]] = []
        risk_files: list[dict[str, Any]] = []
        for f in files:
            rel = (f or "").replace("\\", "/")
            breaks = self._blast_radius(rel)
            tasks = self.open_tasks_for_file(rel)
            imp = self._file_importance(rel)
            entry = {
                "file": rel,
                "importance": imp,
                "breaks": breaks,
                "depends_on": self._file_dependencies(rel),
                "why": [
                    {"node_id": r["id"], "kind": r["kind"], "title": r["title"],
                     "why": (r["body"] or "").splitlines()[0] if r["body"] else "",
                     "sha": (r["stamped_at_sha"] or "")[:7], "drift": self._node_drift(r["id"])}
                    for r in self.db.execute(
                        "SELECT id, kind, title, body, stamped_at_sha FROM nodes "
                        "WHERE REPLACE(file_path,'\\','/')=? AND kind NOT IN ('code-symbol','task','file') "
                        "ORDER BY id DESC LIMIT 4", (rel,)).fetchall()
                ],
                "open_tasks": tasks,
                "drift": self._file_drift(rel),
            }
            reviewed.append(entry)
            reasons = []
            if imp >= risk_importance:
                reasons.append("load-bearing")
            if len(breaks) >= risk_dependents:
                reasons.append(f"{len(breaks)} dependents")
            if tasks:
                reasons.append("open task")
            if reasons:
                risk_files.append({"file": rel, "importance": imp,
                                   "dependents": len(breaks), "open_tasks": len(tasks),
                                   "reasons": reasons})
        risk_files.sort(key=lambda r: (-r["importance"], -r["dependents"]))
        return {
            "sha": sha,  # None when reviewing a staged set directly (pre-commit path)
            "files": reviewed,
            "risk_files": risk_files,
            "counts": {"files": len(reviewed), "risk": len(risk_files)},
        }

    # ------------------------------------------- stale-decision alarm (Wave E, ADR-022)
    def stale_decisions(self, *, repo: str | Path | None = None, min_commits: int = 2,
                        limit: int = 20) -> list[dict[str, Any]]:
        """Decisions whose code moved on without them (Wave E, ADR-022).

        A decision (an ADR / foundation lesson) is stamped at one moment in the code's
        life. If the files it REFERENCES then change a lot, the decision may no longer
        describe reality — "ADR-X might be outdated, the code it governs was rewritten N
        times since". We find each decision's referenced code files (its outgoing edges
        to file-pinned nodes), then count, via the same RepoState git read freshen() uses,
        how many commits touched those files strictly newer than the decision's stamp SHA.

        A decision is flagged only when its busiest referenced file saw ≥ min_commits new
        commits (one stray commit isn't "moved on"). Score = total commits-since across its
        referenced files. Read-only, model-free (ADR-014). No git / no stamp SHA → nothing
        flagged (we can't prove staleness, never a false alarm)."""
        from recall.freshness import RepoState

        target = repo or self._repo or _infer_repo(self._db_path)
        if target is None:
            return []
        state = RepoState(Path(target))
        if not state.has_git:
            return []

        # decisions: foundation lessons + ADR-titled notes, each with its stamp SHA.
        dec_rows = self.db.execute(
            """
            SELECT id, title, COALESCE(stamped_at_sha,'') AS sha
              FROM nodes
             WHERE kind IN ('lesson','decision')
               AND (facets LIKE '%foundation%' OR title LIKE 'ADR-%' OR title LIKE 'ADR %')
            """
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in dec_rows:
            sha = r["sha"]
            if not sha:
                continue  # no stamp SHA → can't measure drift against it
            # the code files this decision references: its outgoing edges to file nodes.
            ref_rows = self.db.execute(
                """
                SELECT DISTINCT REPLACE(nd.file_path,'\\','/') AS f
                  FROM edges e
                  JOIN nodes nd ON nd.id = e.dst_node
                 WHERE e.src_node = ?
                   AND nd.kind = 'code-symbol'
                   AND nd.file_path IS NOT NULL AND nd.file_path != ''
                """,
                (r["id"],),
            ).fetchall()
            stale_files = []
            total = 0
            for (f,) in ref_rows:
                n = state.commits_since(f, sha)
                if n > 0:
                    stale_files.append({"file": f, "commits_since": n})
                    total += n
            if not stale_files:
                continue
            busiest = max(sf["commits_since"] for sf in stale_files)
            if busiest < min_commits:
                continue  # moved a little, not "moved on"
            stale_files.sort(key=lambda sf: -sf["commits_since"])
            out.append({"node_id": r["id"], "title": r["title"], "sha": sha[:7],
                        "stale_files": stale_files, "score": total})
        out.sort(key=lambda d: (-d["score"], -len(d["stale_files"])))
        return out[:limit]

    # -------------------------------------------------------- freshness (stage 2)
    def freshen(self, repo: str | Path | None = None) -> dict[str, Any]:
        """Re-check every pinned node's file against git and write the drift flags.

        Thin pass-through to recall.freshness.freshen so adapters call idx.freshen()
        and the engine stays the single home for all intelligence. `repo` defaults
        to the index's inferred repo (.mind/index.db -> repo)."""
        from recall.freshness import freshen as _freshen

        target = repo or self._repo or _infer_repo(self._db_path)
        if target is None:
            return {"checked": 0, "fresh": 0, "committed": 0, "uncommitted": 0, "no_git": True}
        return _freshen(self, target)

    def _node_drift(self, node_id: int) -> str | None:
        """The drift level the last freshen() recorded for this node, or None if
        the index was never freshened (freshness unknown -> shown fresh)."""
        row = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (f"drift:{node_id}",)
        ).fetchone()
        return row[0] if row else None

    def _dedupe_results(self, scored: list[tuple]) -> list[tuple]:
        """Collapse rows that point at the same knowledge — git history often has
        a merge + the direct commit with an identical (title, file). Keep the
        highest-scored representative; scored is already score-descending."""
        seen: set[tuple] = set()
        out: list[tuple] = []
        for s in scored:
            node_id = s[0]
            row = self.db.execute(
                "SELECT title, file_path FROM nodes WHERE id=?", (node_id,)
            ).fetchone()
            key = (row["title"], row["file_path"])
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        return out

    # -------------------------------------------------------- 3-level builder
    def _build_levels(self, node_id: int, hits: int, score: float, toks: set[str]) -> dict[str, Any]:
        n = self.db.execute(
            "SELECT kind,title,body,file_path,symbol,line,stamped_at_sha,origin "
            "FROM nodes WHERE id=?",
            (node_id,),
        ).fetchone()
        matched = sorted(
            t for t in toks
            if self.db.execute(
                "SELECT 1 FROM fts_anchors WHERE term MATCH ? AND node_id=? LIMIT 1",
                (_fts_phrase(t), node_id),
            ).fetchone()
        )
        # Level 3: relation — multi-hop walk over typed edges (recursive CTE).
        relation = self.db.execute(
            """
            WITH RECURSIVE walk(src, dst, kind, sha, verified, hop) AS (
                SELECT src_node, dst_node, kind, stamped_at_sha, verified, 1
                  FROM edges WHERE src_node = ?
                UNION ALL
                SELECT e.src_node, e.dst_node, e.kind, e.stamped_at_sha, e.verified, w.hop + 1
                  FROM edges e JOIN walk w ON e.src_node = w.dst WHERE w.hop < 3
            )
            SELECT DISTINCT w.kind, nd.title, nd.file_path, w.sha, w.verified
              FROM walk w JOIN nodes nd ON nd.id = w.dst
            """,
            (node_id,),
        ).fetchall()
        why_line = (n["body"] or "").splitlines()[0] if n["body"] else ""
        return {
            "node_id": node_id,
            "score": round(score, 2),
            "anchor_hits": hits,
            # level 1 — the hit
            "kind": n["kind"],
            "title": n["title"],
            "matched_anchors": matched,
            "file": n["file_path"],
            "symbol": n["symbol"],
            "line": n["line"],
            "sha": (n["stamped_at_sha"] or "")[:7],
            "origin": n["origin"],
            # freshness (stage 2): the node's own drift level, set by freshen().
            # None = never freshened / not pinned to a file -> treated as fresh.
            "drift": self._node_drift(node_id),
            # level 2 — the meaning
            "why": why_line,
            "body": n["body"],
            # level 3 — the relation
            "relation": [
                {"kind": k, "target": t or fp, "sha": (sha or "")[:7], "verified": bool(v)}
                for k, t, fp, sha, v in relation
            ],
            # level 3b — the dependency chain of the hit's FILE (the static graph).
            # The per-node walk above only sees edges on THIS node; a hit that is a
            # commit/lesson has none. This surfaces "what this file depends on" by
            # seeding from every code-symbol of the same file — so the causal chain
            # shows up no matter which node-kind ranked to the top (the graph payoff).
            "depends_on": self._file_dependencies(n["file_path"]),
        }

    def _invalidate_corpus_stats(self) -> None:
        """Drop the cached BM25 corpus stats — called by every anchor/node mutation."""
        self._corpus_stats = None

    def _cap_query_tokens(self, toks: list[str]) -> list[str]:
        """Cap an oversized query at the _QUERY_TOKEN_CAP RAREST tokens.

        Hook queries are whole edit texts (measured 94+ unique tokens on a real edit)
        and _bm25_scores fires one FTS query per token — the hot-path cost is linear
        in tokens. Rarity = exact-term document frequency from node_anchors in ONE
        indexed query (a proxy: porter stemming may match more, but rare-LOOKING
        tokens are exactly the informative ones; an absent term has df 0 and is kept).
        Queries at or under the cap pass through untouched — typed questions never
        change. Deterministic: ties break lexicographically."""
        if len(toks) <= _QUERY_TOKEN_CAP:
            return toks
        ph = ",".join("?" * len(toks))
        df = dict(self.db.execute(
            f"SELECT a.term, COUNT(na.node_id) FROM anchors a "
            f"JOIN node_anchors na ON na.anchor_id = a.id "
            f"WHERE a.term IN ({ph}) GROUP BY a.term",
            list(toks),
        ).fetchall())
        # Known-rare first (the safest bridges into existing knowledge), then unknown
        # terms (df 0 exact — may still match via porter stemming), common terms cut
        # first. A plain df sort would let 40 unseen edit-text identifiers evict the
        # one known needle.
        def rank(t: str) -> tuple:
            d = df.get(t, 0)
            return (0, d, t) if d else (1, 0, t)
        return sorted(toks, key=rank)[:_QUERY_TOKEN_CAP]

    def _bm25_scores(self, toks: list[str]) -> tuple[dict[int, int], dict[int, float]]:
        """Raw hit counts + BM25 relevance per node for a token list.

        raw  — node_id -> count of matching anchor rows. This query is byte-identical
               to the pre-BM25 engine: the silence floor keeps living on RAW hits, so
               weighting can never manufacture a hit from nothing.
        bm25 — node_id -> Lucene-style BM25: idf = ln(1+(N-df+0.5)/(df+0.5)) (always
               > 0, unlike classic BM25 which goes negative for df > N/2 — `recall`
               has df 456/1043 here), tf saturated by k1, length-normalized over the
               node's anchor count (a 369-anchor roadmap node no longer beats a
               7-anchor code symbol on volume). Pure SQL counting + math — read-only,
               model-free (ADR-014).
        """
        toks = self._cap_query_tokens(toks)  # hook texts carry 100+ tokens — keep the rarest
        match = _fts_match(toks)
        raw: dict[int, int] = dict(self.db.execute(
            "SELECT node_id, COUNT(*) AS hits FROM fts_anchors WHERE term MATCH ? "
            "GROUP BY node_id",
            (match,),
        ).fetchall())
        if not raw:
            return {}, {}
        per_tok = {
            t: dict(self.db.execute(
                "SELECT node_id, COUNT(*) FROM fts_anchors WHERE term MATCH ? "
                "GROUP BY node_id",
                (_fts_phrase(t),),
            ).fetchall())
            for t in toks
        }
        if self._corpus_stats is None:
            self._corpus_stats = (
                self.db.execute(
                    "SELECT COUNT(DISTINCT node_id) FROM node_anchors").fetchone()[0] or 1,
                self.db.execute("SELECT COUNT(*) FROM node_anchors").fetchone()[0] or 1,
            )
        n_docs, total = self._corpus_stats
        avg_len = total / n_docs
        lens: dict[int, int] = {}
        ids = list(raw)
        for i in range(0, len(ids), 500):  # 999-parameter-safe IN batches
            batch = ids[i:i + 500]
            ph = ",".join("?" * len(batch))
            lens.update(self.db.execute(
                f"SELECT node_id, COUNT(*) FROM node_anchors "
                f"WHERE node_id IN ({ph}) GROUP BY node_id",
                batch,
            ).fetchall())
        bm25: dict[int, float] = {}
        for node_id in raw:
            norm = 1 - _BM25_B + _BM25_B * (lens.get(node_id, 0) / avg_len)
            rel = 0.0
            for t in toks:
                tf = per_tok[t].get(node_id, 0)
                if not tf:
                    continue
                df = len(per_tok[t])
                idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
                rel += idf * (tf * (_BM25_K1 + 1)) / (tf + _BM25_K1 * norm)
            bm25[node_id] = rel
        return raw, bm25

    def _build_tracks(self, toks: set[str], boosted_facet: str | None,
                      *, code_k: int = 5, know_k: int = 5,
                      scores: tuple[dict[int, int], dict[int, float]],
                      ) -> dict[str, list]:
        """The 3 parallel recall tracks. Each is ranked on ITS OWN axis so they never
        compete: code by causal importance, knowledge by text relevance (BM25 * facet
        weight). Returns a dict with 'code', 'knowledge', 'blast_radius', 'open_tasks'.
        All read-only, model-free (ADR-014)."""
        raw, bm25 = scores  # always precomputed by recall() — no silent double battery
        # Split the matched nodes by kind and grade each on its own scale.
        code: list[dict] = []
        knowledge: list[dict] = []
        for node_id, hits in raw.items():
            n = self.db.execute(
                "SELECT kind,title,file_path,symbol,line,importance,body,stamped_at_sha "
                "FROM nodes WHERE id=?",
                (node_id,),
            ).fetchone()
            if n is None:
                continue
            facets = self._node_facets(node_id)
            if self._is_tabu(facets):
                continue
            why = (n["body"] or "").splitlines()[0] if n["body"] else ""
            sha = (n["stamped_at_sha"] or "")[:7]
            if n["kind"] == "code-symbol":
                rel = bm25.get(node_id, 0.0)
                code.append({
                    "node_id": node_id, "title": n["title"], "file": n["file_path"],
                    "symbol": n["symbol"], "line": n["line"],
                    "importance": round(n["importance"] or 0, 1),  # causal weight 1-100
                    "relevance": round(rel, 2),
                    "hits": hits, "why": why, "sha": sha,
                    # unrounded sort key (ADR-028): a "where is X?" question wants the
                    # implementation — a test that merely exercises X ranks below it.
                    "_relw": rel * (0.5 if _is_test_file(n["file_path"]) else 1.0),
                })
            else:  # commit | lesson | plan | ... -> the knowledge track
                fw = max([self.rules.facet_weight(f) for f in facets] or [1.0])
                knowledge.append({
                    "node_id": node_id, "kind": n["kind"], "title": n["title"],
                    "file": n["file_path"],
                    "relevance": round(bm25.get(node_id, 0.0) * fw, 2),
                    "hits": hits, "why": why, "sha": sha,
                })
        # Code (ADR-028): ranked by BM25 relevance with test files at half weight;
        # importance is the TIE-BREAK, not the headline. Measured on bench v2's 12
        # verified location questions: importance-first r@3 1/12 — the right symbol
        # (importance 1.0-1.2, relevance 14-20) was cut by every 30+-importance node;
        # relevance-first + test-downweight r@3 10/12. node_id last for determinism.
        code.sort(key=lambda c: (-c["_relw"], -c["importance"], c["node_id"]))
        code = self._dedupe_track(code)[:code_k]
        for it in code:
            it.pop("_relw", None)  # sort key only — not part of the payload
        # Knowledge: ranked by text relevance (BM25 * facet weight).
        knowledge.sort(key=lambda k: (-k["relevance"], k["node_id"]))
        knowledge = self._dedupe_track(knowledge)[:know_k]
        # Drift only on the SURFACED items (a meta lookup per shown row, never per match).
        # Never-freshened fallback (kept from the old CLI badge): with no drift level,
        # an unverified edge on the node still means "check before trusting".
        for it in code + knowledge:
            lvl = self._node_drift(it["node_id"])
            if lvl is None:
                n_bad = self.db.execute(
                    "SELECT COUNT(*) FROM edges WHERE (src_node=? OR dst_node=?) "
                    "AND verified=0", (it["node_id"], it["node_id"])).fetchone()[0]
                lvl = "committed" if n_bad else None
            it["drift"] = lvl
        # Blast radius: for the single most important code hit, who leans on it?
        top_file = code[0]["file"] if code else None
        blast = self._blast_radius(top_file) if top_file else []
        # Open tasks (ADR-017): the forward-looking reminder that comes to ME when the
        # top code hit has an unfinished plan/task — so I can't forget it while editing.
        open_tasks = self.open_tasks_for_file(top_file) if top_file else []
        return {"code": code, "knowledge": knowledge,
                "blast_radius": blast, "open_tasks": open_tasks}

    def _dedupe_track(self, items: list[dict]) -> list[dict]:
        """Collapse rows pointing at the same (title, file) — keep the first (best-ranked)."""
        seen: set[tuple] = set()
        out: list[dict] = []
        for it in items:
            key = (it.get("title"), it.get("file"))
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out

    def _blast_radius(self, file_path: str | None, *, limit: int = 20) -> list[dict]:
        """Files that DEPEND ON this file — what breaks if I change it. The reverse of
        _file_dependencies: edges pointing INTO this file's code nodes. Each entry carries
        the dependent file's own importance, so I see not just 'X uses this' but 'how
        load-bearing X itself is'. Read-only, model-free."""
        if not file_path:
            return []
        # ONE row per dependent file (not per file×kind — that ate the LIMIT with
        # duplicate co_changed/depends_on rows and hid true dependents). For each file
        # keep the STRONGEST relation (a hard depends_on outranks a co_changed hint), so
        # the limit spends its budget on distinct files. Hard deps first, then importance.
        _RANK = {"depends_on": 5, "implements": 4, "guarded_by": 3, "relates_to": 2, "co_changed": 1}
        rows = self.db.execute(
            """
            SELECT ns.file_path, e.kind, MAX(ns.importance) AS imp
              FROM nodes nd
              JOIN edges e ON e.dst_node = nd.id
              JOIN nodes ns ON ns.id = e.src_node
             WHERE nd.file_path = ?
               AND e.kind IN ('depends_on','implements','guarded_by','relates_to','co_changed')
               AND ns.file_path IS NOT NULL AND ns.file_path != nd.file_path
             GROUP BY ns.file_path, e.kind
            """,
            (file_path,),
        ).fetchall()
        best: dict[str, tuple[str, float]] = {}
        for fp, k, imp in rows:
            cur = best.get(fp)
            if cur is None or _RANK.get(k, 0) > _RANK.get(cur[0], 0):
                best[fp] = (k, imp or 0)
        ordered = sorted(
            best.items(), key=lambda kv: (-_RANK.get(kv[1][0], 0), -kv[1][1]))[:limit]
        return [{"file": fp, "kind": k, "importance": round(imp, 1)} for fp, (k, imp) in ordered]

    def _file_dependencies(self, file_path: str | None, *, limit: int = 8) -> list[dict]:
        """The files this file depends on (static depends_on graph), deduped by target
        file. Empty when the hit isn't file-pinned or has no AST edges. Read-only, fast."""
        if not file_path:
            return []
        rows = self.db.execute(
            """
            SELECT DISTINCT nd.file_path, e.kind
              FROM nodes ns
              JOIN edges e ON e.src_node = ns.id
              JOIN nodes nd ON nd.id = e.dst_node
             WHERE ns.file_path = ?
               AND e.kind IN ('depends_on','implements','guarded_by','relates_to','co_changed')
               AND nd.file_path IS NOT NULL AND nd.file_path != ns.file_path
             LIMIT ?
            """,
            (file_path, limit),
        ).fetchall()
        return [{"kind": k, "target": fp} for fp, k in rows if fp]

    # ------------------------------------------------------- dedup (ADR-005)
    def _dedup_via_recall(self, new_anchors: set[str]) -> tuple[int | None, float]:
        """One recall() over FTS5; overlap = |distinct shared| / |new anchors|."""
        if not new_anchors:
            return None, 0.0
        match = _fts_match(new_anchors)
        rows = self.db.execute(
            "SELECT node_id, COUNT(*) AS hits FROM fts_anchors WHERE term MATCH ? "
            "GROUP BY node_id ORDER BY hits DESC LIMIT 5",
            (match,),
        ).fetchall()
        best, best_ratio = None, 0.0
        for node_id, _hits in rows:
            shared = new_anchors & self._node_anchor_set(node_id)
            ratio = len(shared) / len(new_anchors)  # always in [0, 1]
            if ratio > best_ratio:
                best, best_ratio = node_id, ratio
        return best, best_ratio

    def _node_anchor_set(self, node_id: int) -> set[str]:
        return {
            r[0]
            for r in self.db.execute(
                "SELECT DISTINCT term FROM fts_anchors WHERE node_id=?", (node_id,)
            ).fetchall()
        }

    # ----------------------------------------------------------- write helpers
    def _insert_node(
        self, kind, title, body, facets, file_path, symbol, line, sha, origin,
        power_run=None, base_sha=None, author=None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO nodes"
            "(kind,title,body,facets,file_path,symbol,line,stamped_at_sha,origin,power_run,base_sha,author) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, title, body, ",".join(facets), file_path, symbol, line, sha, origin,
             power_run, base_sha, author),
        )
        return int(cur.lastrowid)

    def _add_anchors(self, node_id: int, terms: set[str]) -> set[str]:
        """Attach anchors to a node; return the terms that were NEWLY linked here.

        The return value is the synonym-undo ledger: a term already on the node
        (INSERT OR IGNORE rowcount 0) is NOT returned, so Power Mode only records —
        and later only removes — node_anchor rows it actually created."""
        added: set[str] = set()
        for t in terms:
            t = t.strip().lower()
            if not t:
                continue
            self.db.execute("INSERT OR IGNORE INTO anchors(term) VALUES(?)", (t,))
            aid = self.db.execute("SELECT id FROM anchors WHERE term=?", (t,)).fetchone()[0]
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO node_anchors VALUES(?,?)", (node_id, aid)
            ).rowcount
            if inserted:  # only mirror into FTS once per (node, term)
                self.db.execute(
                    "INSERT INTO fts_anchors(term, node_id) VALUES(?,?)", (t, node_id)
                )
                added.add(t)
        if added:
            self._invalidate_corpus_stats()
        return added

    def _add_edge_to_target(
        self, src: int, ekind: str, target: str, sha: str | None,
        power_run=None, base_sha=None,
    ) -> None:
        if not (target and target.strip()):
            return  # skip empty edge targets — mirrors the empty-skip for anchors/tags
        target = target.strip()
        dst = self._get_or_create_code_node(target, sha, power_run, base_sha)
        self.db.execute(
            "INSERT INTO edges(src_node,dst_node,kind,stamped_at_sha,power_run,base_sha) "
            "VALUES(?,?,?,?,?,?)",
            (src, dst, ekind, sha, power_run, base_sha),
        )

    def _get_or_create_code_node(
        self, target: str, sha: str | None, power_run=None, base_sha=None
    ) -> int:
        row = self.db.execute(
            "SELECT id FROM nodes WHERE title=? AND kind='code-symbol'", (target,)
        ).fetchone()
        if row:
            return row[0]  # found an existing node — never re-tag it (undo must keep it)
        # A code-symbol node CREATED while wiring a power edge belongs to that run, so
        # undo lifts it out too. Without the run tag it would orphan after undo (a leak).
        # Edges from a normal commit (power_run=None) keep origin='live' as before.
        origin = "power" if power_run else "live"
        cur = self.db.execute(
            "INSERT INTO nodes(kind,title,file_path,stamped_at_sha,origin,power_run,base_sha) "
            "VALUES('code-symbol',?,?,?,?,?,?)",
            (target, target, sha, origin, power_run, base_sha),
        )
        return int(cur.lastrowid)

    def add_dependency_edges(self, pairs, *, kind: str = "depends_on") -> int:
        """Stamp file→file dependency edges between EXISTING code-symbol nodes.

        `pairs` is an iterable of (from_file, to_file) repo-relative paths — the static
        import graph from recall.graph (deterministic, model-free). For each pair we link
        a representative node of each file (its first code-symbol by line) with one typed
        edge, so the graph is one edge per file-pair, not symbol×symbol. Files with no
        code-symbol node (e.g. a pure-config file) are skipped. Returns edges added.

        These edges are part of the regenerable bootstrap layer: their endpoints are
        origin='bootstrap' code symbols, so clear_bootstrap()/rebuild CASCADE-drops them
        (no duplicate edges on re-init). The LLM einordnung-layer may later refine an
        edge's kind (depends_on -> implements/guarded_by); this is the deterministic floor.
        """
        rep: dict[str, int | None] = {}

        def _rep(rel: str):
            if rel not in rep:
                row = self.db.execute(
                    "SELECT id FROM nodes WHERE file_path=? AND kind='code-symbol' "
                    "ORDER BY line LIMIT 1",
                    (rel,),
                ).fetchone()
                rep[rel] = row[0] if row else None
            return rep[rel]

        added = 0
        for from_rel, to_rel in pairs:
            src = _rep(from_rel)
            dst = _rep(to_rel)
            if not src or not dst or src == dst:
                continue
            # idempotency guard: one (src,dst,kind) edge only, even if called twice
            exists = self.db.execute(
                "SELECT 1 FROM edges WHERE src_node=? AND dst_node=? AND kind=? LIMIT 1",
                (src, dst, kind),
            ).fetchone()
            if exists:
                continue
            self.db.execute(
                "INSERT INTO edges(src_node,dst_node,kind,stamped_at_sha) VALUES(?,?,?,?)",
                (src, dst, kind, "ast"),
            )
            added += 1
        self.db.commit()
        return added

    def record_co_change(self, files, *, kind: str = "co_changed", sha: str = "session") -> int:
        """Link the files touched together in ONE coding session (ADR-015: heal while
        coding). Unlike depends_on (A imports B, directional, from the AST), co_changed
        captures the *invisible* relation — files that must change together but don't
        import each other — as a free by-product of the LLM editing. No model is run.

        `files` is an iterable of repo-relative paths. Every unordered pair gets a
        symmetric edge (both directions) between each file's representative code-symbol
        node, so the relation surfaces from either side. Idempotent via the (src,dst,kind)
        guard. Files with no code-symbol node are skipped. Returns edges added.

        These edges are NOT bootstrap (origin stays as the existing node's) — they are
        live session knowledge and survive clear_bootstrap()/re-init, because the
        understanding they encode is exactly what a cold rebuild can never recover.
        The einordnung-layer may later refine kind -> relates_to/guarded_by once the
        *why* is known (recall.refine), but the deterministic floor needs no LLM.
        """
        rels = [f for f in dict.fromkeys(files) if f]  # de-dup, preserve order
        rep: dict[str, int | None] = {}

        def _rep(rel: str):
            if rel not in rep:
                row = self.db.execute(
                    "SELECT id FROM nodes WHERE file_path=? AND kind='code-symbol' "
                    "ORDER BY line LIMIT 1",
                    (rel,),
                ).fetchone()
                rep[rel] = row[0] if row else None
            return rep[rel]

        added = 0
        for i in range(len(rels)):
            for j in range(i + 1, len(rels)):
                a, b = _rep(rels[i]), _rep(rels[j])
                if not a or not b or a == b:
                    continue
                for src, dst in ((a, b), (b, a)):  # symmetric: surfaces from either file
                    exists = self.db.execute(
                        "SELECT 1 FROM edges WHERE src_node=? AND dst_node=? AND kind=? LIMIT 1",
                        (src, dst, kind),
                    ).fetchone()
                    if exists:
                        continue
                    self.db.execute(
                        "INSERT INTO edges(src_node,dst_node,kind,stamped_at_sha) VALUES(?,?,?,?)",
                        (src, dst, kind, sha),
                    )
                    added += 1
        self.db.commit()
        if added:
            # New relations changed the causal graph -> re-rank (model-free, ADR-016).
            # This is the 'heal while coding' payoff: the session's edits immediately
            # lift the importance of the files they wired together.
            from recall.importance import persist_importance
            persist_importance(self.db)
        return added

    def _file_anchor_node(self, rel: str) -> int | None:
        """A node id that represents `rel` for wiring task/edge links — resolved by
        priority so a non-code file is never left unanchored:
          1. the file's representative code symbol (first by line),
          2. else ANY node already pinned to the file (commit/lesson/ADR — file is known),
          3. else a lightweight kind='file' representative node we create here.
        Path match is backslash-normalized (Windows). Model-free, idempotent."""
        rel = (rel or "").replace("\\", "/")
        if not rel:
            return None
        row = self.db.execute(
            "SELECT id FROM nodes WHERE REPLACE(file_path,'\\','/')=? AND kind='code-symbol' "
            "ORDER BY line LIMIT 1",
            (rel,),
        ).fetchone()
        if row:
            return row[0]
        # No code symbol (HTML/JSON/config) — any node pinned to the file still anchors it.
        row = self.db.execute(
            "SELECT id FROM nodes WHERE REPLACE(file_path,'\\','/')=? "
            "ORDER BY (kind='file') ASC, id ASC LIMIT 1",
            (rel,),
        ).fetchone()
        if row:
            return row[0]
        # Nothing knows this file yet — create a minimal representative node so the task
        # link lands. origin='task-anchor' keeps it out of the bootstrap/power layers
        # (clear_bootstrap won't touch it; it is regenerated on demand here).
        cur = self.db.execute(
            "INSERT INTO nodes(kind,title,file_path,origin) VALUES('file',?,?,'task-anchor')",
            (rel, rel),
        )
        return int(cur.lastrowid)

    def link_task_to_files(self, task_node_id: int, files, *, kind: str = "relates_to") -> int:
        """Wire a task/plan node to the files it affects (ADR-017). One relates_to edge
        from the task to a representative node of each affected file, so editing that
        file surfaces the open task (the standing-intent reminder) and the wiki shows the
        link. Idempotent via the (src,dst,kind) guard. Model-free. Returns edges added.

        A file may have no code-symbol node — HTML, JSON, config, or any file the code
        indexer doesn't parse (the dashboard.html bug: tasks affecting it never came back).
        So the destination is resolved by priority: (1) the file's representative code
        symbol, (2) ANY node already pinned to the file (a commit/lesson — the file is
        known), else (3) a lightweight representative file node we create here. The task
        link must NEVER be silently dropped — that would break the TASK LAW's promise that
        a task returns the moment its files are touched (rules.md Rule 0)."""
        added = 0
        for rel in dict.fromkeys(f.replace("\\", "/") for f in files if f):
            dst = self._file_anchor_node(rel)
            if dst is None or dst == task_node_id:
                continue
            exists = self.db.execute(
                "SELECT 1 FROM edges WHERE src_node=? AND dst_node=? AND kind=? LIMIT 1",
                (task_node_id, dst, kind),
            ).fetchone()
            if exists:
                continue
            self.db.execute(
                "INSERT INTO edges(src_node,dst_node,kind,stamped_at_sha) VALUES(?,?,?,?)",
                (task_node_id, dst, kind, "task"),
            )
            added += 1
        self.db.commit()
        return added

    def open_tasks_for_file(self, file_path: str | None, *, limit: int = 4) -> list[dict]:
        """Open tasks/plans that affect this file (ADR-017) — the reminder that comes to
        ME when I touch the file, so I can't forget it. A task is open unless its facets
        carry a terminal status (done/dropped/deferred). Read-only, model-free."""
        if not file_path:
            return []
        rel = file_path.replace("\\", "/")
        rows = self.db.execute(
            """
            SELECT DISTINCT nt.id, nt.title, nt.facets, nt.file_path
              FROM nodes nd
              JOIN edges e ON e.dst_node = nd.id
              JOIN nodes nt ON nt.id = e.src_node
             WHERE REPLACE(nd.file_path,'\\','/') = ? AND nt.kind = 'task' AND e.kind = 'relates_to'
             LIMIT 50
            """,
            (rel,),
        ).fetchall()
        out: list[dict] = []
        for nid, title, facets, tfp in rows:
            fs = set((facets or "").split(","))
            status = next((s for s in ("done", "dropped", "deferred", "open") if s in fs), "open")
            if status != "open":
                continue
            out.append({"node_id": nid, "title": title, "file": tfp, "status": status})
        return out[:limit]

    def mark_useful(self, node_id: int, *, n: int = 1) -> None:
        """Record that a surfaced node was actually used (clicked / touched in a follow-up
        edit). Deterministic feedback (ADR-016): nudges the node's importance up within the
        bounded +/-20% band, so recall LEARNS what truly helps. Model-free. Re-ranks."""
        self._bump_feedback(node_id, useful=n)

    def mark_missed(self, node_id: int, *, n: int = 1) -> None:
        """Record that a node was surfaced as the top hit but NOT used — a weak negative
        signal that gently lowers its importance over time. Bounded, deterministic."""
        self._bump_feedback(node_id, missed=n)

    def _bump_feedback(self, node_id: int, *, useful: int = 0, missed: int = 0,
                       rerank: bool = True) -> None:
        self.db.execute(
            "INSERT INTO node_feedback(node_id, useful_count, missed_count) VALUES(?,?,?) "
            "ON CONFLICT(node_id) DO UPDATE SET "
            "useful_count = useful_count + ?, missed_count = missed_count + ?, "
            "updated_at = strftime('%s','now')",
            (node_id, useful, missed, useful, missed),
        )
        self.db.commit()
        if rerank:
            # fold the new feedback into the dial (model-free). Batch callers pass
            # rerank=False and call rerank_importance() ONCE — PageRank is O(graph), so
            # re-running it per node during bootstrap would be a real perf bug.
            from recall.importance import persist_importance
            persist_importance(self.db)

    def rerank_importance(self) -> int:
        """Recompute + persist importance once (graph PageRank * feedback). For batch
        callers that bumped feedback with rerank=False. Model-free (ADR-014)."""
        from recall.importance import persist_importance
        return persist_importance(self.db)

    def _merge_facets(self, node_id: int, new_facets: list[str]) -> None:
        cur = (self.db.execute("SELECT facets FROM nodes WHERE id=?", (node_id,)).fetchone()[0] or "")
        have = [f for f in cur.split(",") if f]
        merged = have + [f for f in new_facets if f not in have]
        self.db.execute("UPDATE nodes SET facets=? WHERE id=?", (",".join(merged), node_id))

    # --------------------------------------------------------------- read helpers
    def _node_facets(self, node_id: int) -> list[str]:
        row = self.db.execute("SELECT facets FROM nodes WHERE id=?", (node_id,)).fetchone()
        return [f for f in (row[0] or "").split(",") if f] if row else []

    def _context_facet(self, edit_context: str | None) -> str | None:
        if not edit_context:
            return None
        ctx = edit_context.lower()
        for key, facet in self.rules.context_boost.items():
            if key in ctx:
                return facet
        return None

    def _is_tabu(self, facets: list[str]) -> bool:
        return any(f in self.rules.stay_silent_on for f in facets)

    def _silenced(self, reason: str, t0: float, latency_us: int | None = None) -> dict[str, Any]:
        if latency_us is None:
            latency_us = round((time.perf_counter() - t0) * 1_000_000)
        return {"silenced": True, "reason": reason, "latency_us": latency_us, "results": []}

    def _log(self, query, node_id, score, surfaced, latency_us, consumer) -> None:
        self.db.execute(
            "INSERT INTO access_log(query,node_id,score,surfaced,latency_us,consumer) "
            "VALUES(?,?,?,?,?,?)",
            (query, node_id, float(score), surfaced, latency_us, consumer),
        )
        self.db.commit()

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        g = lambda q: self.db.execute(q).fetchone()[0]
        return {
            "nodes": g("SELECT COUNT(*) FROM nodes"),
            "edges": g("SELECT COUNT(*) FROM edges"),
            "anchors": g("SELECT COUNT(*) FROM anchors"),
            "by_kind": dict(
                self.db.execute("SELECT kind, COUNT(*) FROM nodes GROUP BY kind").fetchall()
            ),
            "recalls": g("SELECT COUNT(*) FROM access_log"),
            "surfaced": g("SELECT COUNT(*) FROM access_log WHERE surfaced=1"),
        }


def _fts_phrase(term: str) -> str:
    """Quote a term as an FTS5 phrase, escaping embedded double-quotes.

    FTS5 phrase syntax wraps a term in "...". An embedded " would close the phrase
    early and raise OperationalError('unterminated string') — the fix is to double
    internal quotes per the FTS5 quoting rule. This makes stamp()/recall() crash-proof
    against any character in an anchor (explicit anchors + Recall-* trailers bypass
    the regex extractor, so they can contain quotes; see the engine bug hunt)."""
    return '"' + term.replace('"', '""') + '"'


def _fts_match(terms) -> str:
    """Build an OR-joined FTS5 MATCH expression from terms, each safely quoted."""
    return " OR ".join(_fts_phrase(t) for t in terms)


_TRAILER_RE = re.compile(r"^(Recall-\w+|Co-Authored-By|Signed-off-by):", re.IGNORECASE)


def _is_trailer_line(line: str) -> bool:
    return bool(_TRAILER_RE.match(line.strip()))


def _commit_explanation(commit_msg: str) -> str:
    """The human explanation: drop the subject line and every trailer line."""
    lines = commit_msg.strip().splitlines()
    kept = [line for line in lines[1:] if not _is_trailer_line(line)]
    return "\n".join(kept).strip()


def _infer_repo(index_path: str | Path) -> Path | None:
    """`.../<repo>/.mind/index.db` -> `<repo>`; ':memory:' -> None."""
    p = Path(index_path)
    if str(p) == ":memory:":
        return None
    # index.db is inside .mind/ (or .recall/), repo is the parent of that folder.
    return p.parent.parent if p.parent.name in (".mind", ".recall") else p.parent

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
        # Resolver (search-inversion) — its construction loads ALL symbols + IDF + mines
        # the synonym maps (access_log + knowledge corpus); measured ~55ms on this index,
        # and resolve() rebuilt it on EVERY call. Cached here, invalidated by the same
        # node/anchor mutation seam as the corpus stats (a stamp/reindex changes it). The
        # access_log keeps growing under it, but the synonym FLYWHEEL learns across
        # sessions, not intra-query, so a per-process-warm resolver is correct + ~40x faster.
        self._resolver = None  # type: ignore[var-annotated]

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
        predicate: str | None = None,
        outcome: str | None = None,
        visibility: str = "team",
        dedup: bool = True,
        consumer: str = "cli",
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
        t0 = time.perf_counter()
        # HEADLINE SPLIT (2026-06-15): a stamp's title should be a short HEADLINE, with the
        # detail in the body — so the story chain shows the Decision once (the headline) and
        # the full text once (the body), never the same 400-char paragraph twice. Dogfooding
        # put whole summaries into `title` with body=None; when that happens we split the
        # title at its first sentence boundary: headline -> title, the rest -> body. Only when
        # there's no body to clobber, so an explicit (title, body) pair is left untouched.
        if (body is None or not str(body).strip()) and title and len(title) > _HEADLINE_MAX:
            head, rest = _split_headline(title)
            if rest:
                title, body = head, rest
        # that freshen() re-verifies free, every commit — catching a claim that is WRONG now
        # even on an unmoved file (GAP A). Validate at WRITE time so a typo fails the stamp
        # instead of silently storing an unparseable predicate that evaluate_predicate would
        # treat as UNKNOWN (a malformed check that never fires is worse than no check — it
        # reads as "verified" green). A None/empty predicate is the normal nudged path: the
        # node simply has no check and freshen() falls back to SHA-drift exactly as before.
        if outcome is not None:
            outcome = outcome.strip() or None  # empty -> None (nudged: no outcome is honest)
        if predicate is not None:
            predicate = predicate.strip() or None
        if predicate is not None:
            from recall.predicate import parse_predicate
            # Cheap ReDoS/footgun bound: a real predicate is a short anchor pattern. A
            # multi-KB regex is either a mistake or a catastrophic-backtracking hazard for
            # freshen() (which runs in the dashboard loop + the git hook). Reject it at
            # write time rather than store a check that could hang every later re-verify.
            if len(predicate) > 500:
                raise ValueError(
                    f"predicate too long ({len(predicate)} chars, max 500) — a check should "
                    "be a short anchor pattern, not a program"
                )
            if parse_predicate(predicate) is None:
                raise ValueError(
                    f"unusable predicate {predicate!r} — expected "
                    "'contains:<regex>' / 'absent:<regex>' clauses joined by ' && ', and no "
                    "catastrophic-backtracking shape like (x+)+ (it would hang freshen())"
                )
        # PATH CONTAINMENT (security, adversarial review 2026-06-15): an explicit file_path
        # must point INSIDE the repo. An absolute or '..'-escaping path would make freshen()'s
        # predicate read a file outside the repo on every tick. Reject at write time so a bad
        # stamp fails loud rather than persisting a permanent out-of-repo check. (The read
        # path in evaluate_predicate also guards this — defence in depth.) Only checked when
        # we can resolve against a real repo root; a None repo (memory/dry) skips it.
        if file_path is not None and self._repo is not None:
            from pathlib import Path as _Path
            norm = file_path.replace("\\", "/")
            try:
                root = _Path(self._repo).resolve()
                target = (root / norm).resolve()
                target.relative_to(root)  # ValueError if outside the repo
            except (ValueError, OSError):
                raise ValueError(
                    f"file_path {file_path!r} escapes the repo — a pinned file must be "
                    "inside the repository (no absolute paths, no '..')"
                )
        # ANCHOR→FILE PINNING (dogfood fix 2026-06-14): a `stamp --anchors <path>`
        # was meant to hang the note ON that file so `brief <path>` surfaces it as a
        # WHY — but stamp only ever set file_path from the explicit --file flag, so a
        # path passed via --anchors left file_path=NULL and the note "floated"
        # (invisible to brief, which reads WHERE file_path=?). If no file_path was
        # given, derive it from the FIRST anchor that matches a real file recall
        # already indexes (matching a known file avoids pinning on a stray "see foo/bar"
        # phrase). Anchors still go into anchor_set for FTS/dedup as before.
        if file_path is None and anchors:
            for a in anchors:
                cand = (a or "").strip().replace("\\", "/")
                if "/" in cand and self._is_known_file(cand):
                    file_path = cand
                    break
        anchor_set = set(a.strip().lower() for a in (anchors or []) if a.strip())
        if not anchor_set:
            anchor_set = extract_anchors(f"{title} {body or ''}")
        facets = canonicalize_tags(
            tags or [], self.rules.tag_aliases, self.rules.allowed_tags
        )
        # activity-console signal (v7): log only DELIBERATE, INTERACTIVE stamps (a real
        # `recall stamp` from the CLI or the MCP stamp tool). Three machine paths must stay
        # silent or they flood the console + add a commit per row on the index hot path:
        #   - bootstrap re-index (origin!='live'), Power-Mode (power_run set), AND
        #   - stamp_from_commit replaying git trailers, which calls stamp(origin='live') once
        #     per trailer-commit during the `git log` walk → it passes consumer='commit' so we
        #     skip it here (the console only wants user/AI actions, consumer cli/mcp).
        def _log_stamp(nid):
            if origin == "live" and power_run is None and consumer != "commit":
                self._log(title[:120], nid, 0, 1,
                          round((time.perf_counter() - t0) * 1_000_000), consumer, kind="stamp")

        if dedup and anchor_set:
            existing, overlap = self._dedup_via_recall(anchor_set)
            if existing is not None and overlap >= self.rules.dedup_threshold:
                added = self._add_anchors(existing, anchor_set)  # enrich, don't duplicate
                if facets:
                    self._merge_facets(existing, facets)
                # A predicate passed on a MERGE sets/replaces the existing node's check —
                # the freshest stamp wins (you re-stamped because the claim or its check
                # changed). Never CLEAR a predicate on merge: omitting --predicate on a
                # re-stamp must not silently drop a working check the node already had.
                if predicate is not None:
                    self.db.execute(
                        "UPDATE nodes SET predicate=? WHERE id=?", (predicate, existing)
                    )
                # Same rule as predicate: a re-stamp WITH an outcome sets/replaces it (the
                # outcome is exactly what a re-stamp records — "here's how it turned out");
                # omitting it never CLEARS an outcome the note already had.
                if outcome is not None:
                    self.db.execute(
                        "UPDATE nodes SET outcome=? WHERE id=?", (outcome, existing)
                    )
                # Visibility on a MERGE is MONOTONICALLY TIGHTENING: re-stamping with
                # `private` makes the node private, but a default `team` re-stamp NEVER
                # un-privates a node that was already private. The safe direction is the
                # default — you can never accidentally re-share a secret by re-stamping it.
                if visibility == "private":
                    self.db.execute(
                        "UPDATE nodes SET visibility='private' WHERE id=?", (existing,)
                    )
                self.db.commit()
                title_existing = self.db.execute(
                    "SELECT title FROM nodes WHERE id=?", (existing,)
                ).fetchone()[0]
                _log_stamp(existing)
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
            power_run, base_sha, author, predicate, outcome, visibility,
        )
        self._add_anchors(node_id, anchor_set)
        for ekind, target in (edges or []):
            self._add_edge_to_target(node_id, ekind, target, sha, power_run, base_sha)
        self.db.commit()
        _log_stamp(node_id)
        return {"action": "NEW", "node_id": node_id, "anchors": len(anchor_set)}

    def _is_known_file(self, rel_path: str) -> bool:
        """True if `rel_path` (forward-slash) is a file recall already indexes — used
        to decide whether a path-like anchor should also PIN the note's file_path.
        Matches any node carrying that file_path (a code-symbol or the file node)."""
        return self.db.execute(
            "SELECT 1 FROM nodes WHERE REPLACE(file_path,'\\','/')=? LIMIT 1",
            (rel_path,),
        ).fetchone() is not None

    def stamp_from_commit(self, commit_msg: str, commit_sha: str,
                          author: str | None = None) -> dict[str, Any] | None:
        """Parse Recall-* trailers from a commit message and stamp self-acting.

        Returns None for a normal commit without trailers (the system ignores it).
        Trailers: Recall-anchors, Recall-why, Recall-tags, Recall-edge (kind -> target),
        Recall-predicate (a re-runnable CHECK for the claim — arrow 1, v8),
        Recall-outcome (what CAME of the decision — the chain end, distinct from the why; v9).
        """
        m_anchors = re.search(r"^Recall-anchors:\s*(.+)$", commit_msg, re.MULTILINE)
        if not m_anchors or not m_anchors.group(1).strip():
            return None  # no trailer, or an empty/whitespace-only anchors declaration
        m_why = re.search(r"^Recall-why:\s*(.+)$", commit_msg, re.MULTILINE)
        m_tags = re.search(r"^Recall-tags:\s*(.+)$", commit_msg, re.MULTILINE)
        m_out = re.search(r"^Recall-outcome:\s*(.+)$", commit_msg, re.MULTILINE)
        outcome = m_out.group(1).strip() if (m_out and m_out.group(1).strip()) else None
        m_pred = re.search(r"^Recall-predicate:\s*(.+)$", commit_msg, re.MULTILINE)
        # A predicate in a trailer is validated inside stamp(); but a malformed one must
        # NOT abort the whole commit-replay (one bad trailer would lose the note + every
        # later trailer in the log walk). Pre-validate here and DROP a bad predicate to
        # no-predicate — the note still stamps, it just falls back to drift (the nudged
        # contract: a missing/unusable check is never an error, only a quieter signal).
        predicate = None
        if m_pred and m_pred.group(1).strip():
            from recall.predicate import parse_predicate
            cand = m_pred.group(1).strip()
            if parse_predicate(cand) is not None:
                predicate = cand
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
            predicate=predicate,
            outcome=outcome,
            origin="live",
            author=author,
            consumer="commit",  # machine git-trailer replay — NOT an interactive stamp, keep it
                                # out of the activity console (else a re-index floods it). v7.
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
        n = self.db.execute("SELECT COUNT(*) FROM nodes WHERE kind='task'").fetchone()[0]
        if not n:
            return 0
        # Delete via a subquery, NOT a materialized id-list IN — a repo with >999 task
        # nodes (SQLITE_MAX_VARIABLE_NUMBER on older SQLite) would otherwise trip "too
        # many SQL variables" on the bind list. A correlated subquery has zero binds and
        # is correct for any count. (P3 bug-hunt 2026-06-15.)
        self.db.execute(
            "DELETE FROM fts_anchors WHERE node_id IN (SELECT id FROM nodes WHERE kind='task')")
        self.db.execute("DELETE FROM nodes WHERE kind='task'")
        # Sweep task-anchor file nodes that no longer carry any edge (their only reason to
        # exist was to anchor a now-deleted task link). Re-index recreates them if needed,
        # so this stays idempotent and avoids a slow orphan leak on a non-code file.
        self.db.execute(
            "DELETE FROM nodes WHERE origin='task-anchor' AND kind='file' "
            "AND id NOT IN (SELECT src_node FROM edges UNION SELECT dst_node FROM edges)"
        )
        self.db.commit()
        self._invalidate_corpus_stats()
        return n

    def _delete_node(self, node_id: int) -> None:
        """Delete a node and its FTS mirror. FK ON DELETE CASCADE clears node_anchors
        and any edges; fts_anchors is a virtual table with no FK, so mirror by hand."""
        self.db.execute("DELETE FROM fts_anchors WHERE node_id=?", (node_id,))
        self.db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        self._invalidate_corpus_stats()

    def assert_no_private(self) -> None:
        """WATERPROOF GATE — raise unless this DB holds ZERO private content (owner:
        "vorm build muss das fix gecheckt werden, eine 100% wasserfeste sichere rule").

        This is the fail-closed verification that runs AFTER purge_private() on an
        export copy: it does not trust that the purge worked, it PROVES it. Checks
        every place a private note could hide — the node row, and its FTS mirror /
        node_anchors / edges / node_feedback that should have gone with it. Any
        survivor is a leak: raise SystemExit so the caller throws the output away and
        the build/export ABORTS. Never returns on a non-clean DB."""
        leaks: list[str] = []
        n_priv = self.db.execute(
            "SELECT COUNT(*) FROM nodes WHERE visibility='private'"
        ).fetchone()[0]
        if n_priv:
            leaks.append(f"{n_priv} node(s) still marked visibility='private'")
        # a private node's traces must not survive its deletion (orphans = a leak path)
        orphan_anchors = self.db.execute(
            "SELECT COUNT(*) FROM node_anchors na "
            "LEFT JOIN nodes n ON n.id=na.node_id WHERE n.id IS NULL"
        ).fetchone()[0]
        if orphan_anchors:
            leaks.append(f"{orphan_anchors} orphaned node_anchors row(s)")
        orphan_fts = self.db.execute(
            "SELECT COUNT(*) FROM fts_anchors fa "
            "WHERE fa.node_id NOT IN (SELECT id FROM nodes)"
        ).fetchone()[0]
        if orphan_fts:
            leaks.append(f"{orphan_fts} orphaned fts_anchors row(s)")
        orphan_edges = self.db.execute(
            "SELECT COUNT(*) FROM edges e WHERE e.src_node NOT IN (SELECT id FROM nodes) "
            "OR e.dst_node NOT IN (SELECT id FROM nodes)"
        ).fetchone()[0]
        if orphan_edges:
            leaks.append(f"{orphan_edges} orphaned edge(s)")
        if leaks:
            raise SystemExit(
                "ABORT — export is NOT private-clean (refusing to write a leaky brain):\n  "
                + "\n  ".join(leaks)
            )

    def purge_private(self) -> int:
        """Remove every node marked visibility='private' from THIS connection's DB.
        FK ON DELETE CASCADE clears their edges/node_anchors/node_feedback; the two
        FK-less tables (fts_anchors, access_log) are mirrored by hand. Used on a COPY
        of the brain by export() — never on the live `.mind`. Returns the count removed.

        Defensive: also drops any edge that touches a private node BEFORE the node
        delete, in case foreign_keys is off on this connection (a copied DB opened
        without the PRAGMA would otherwise leave dangling edges)."""
        ids = [r[0] for r in self.db.execute(
            "SELECT id FROM nodes WHERE visibility='private'"
        ).fetchall()]
        if not ids:
            return 0
        ph = ",".join("?" for _ in ids)
        # FK-less mirrors first (CASCADE won't touch these)
        self.db.execute(f"DELETE FROM fts_anchors WHERE node_id IN ({ph})", ids)
        self.db.execute(f"DELETE FROM access_log WHERE node_id IN ({ph})", ids)
        # belt-and-braces: drop edges to/from a private node explicitly (in case the
        # copied DB's connection has foreign_keys OFF — then CASCADE wouldn't fire)
        self.db.execute(
            f"DELETE FROM edges WHERE src_node IN ({ph}) OR dst_node IN ({ph})", ids + ids
        )
        self.db.execute(f"DELETE FROM node_anchors WHERE node_id IN ({ph})", ids)
        self.db.execute(f"DELETE FROM nodes WHERE id IN ({ph})", ids)
        self.db.commit()
        return len(ids)

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

        results = [r for nid, hits, sc, _ in scored
                   if (r := self._build_levels(nid, hits, sc, set(toks))) is not None]
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
    def brief(self, file_path: str, *, why_k: int = 6, sym_k: int = 30,
              rich: bool = False, consumer: str = "cli") -> dict[str, Any]:
        """Pre-Edit Briefing — everything recall knows about ONE file, before I touch it.

        Where recall() answers a *query* in tracks, brief() answers a *file*: it bundles
        the five read-only views that keep me from silently undoing a deliberate decision.
          why        — commits/lessons/ADRs pinned to the file (WHY it is the way it is),
                       newest-meaningful first; never a code-symbol (those are `symbols`)
          warns      — landmines: lessons/decisions that `warns_about` this file's code,
                       so a past mistake fires UNPROMPTED before I edit (arrow 2 / conscience)
          breaks     — files that depend on this one (blast radius) → what I risk breaking
          depends_on — the static depends_on chain this file leans on
          open_tasks — unfinished plans/tasks wired to the file (ADR-017, standing intent)
          symbols    — the code-symbols defined in the file (what's in it)
        Plus `known` (does the index have this file at all) and `drift` (the worst drift
        level recorded on any of the file's pinned nodes, after a freshen()).

        Pure SQL over the already-stamped graph — no model is run (ADR-014). A file the
        index has never seen returns an empty-but-shaped briefing, never an error."""
        t0 = time.perf_counter()
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
        #
        # TWO sources (dogfood fix 2026-06-14): a note reaches a file either by
        # file_path (the pin) OR by carrying the file's PATH as an anchor term
        # (`stamp --anchors <path>`). brief used to read ONLY file_path, so a note
        # anchored to the file by term — the primary way to attach a decision — was
        # invisible here. We now UNION both: file_path match ∪ anchored-by-path-term,
        # de-duplicated by node id. (Fix A makes new stamps also set file_path, so this
        # mainly recovers already-floating notes; together they close the gap.)
        why_rows = self.db.execute(
            "SELECT id, kind, title, body, stamped_at_sha, predicate, outcome FROM nodes WHERE id IN ("
            "  SELECT id FROM nodes WHERE REPLACE(file_path,'\\','/')=? "
            "  UNION "
            "  SELECT na.node_id FROM node_anchors na JOIN anchors an ON an.id=na.anchor_id "
            "  WHERE an.term=?"
            ") AND kind NOT IN ('code-symbol','task','file') "
            "ORDER BY id DESC LIMIT ?",
            (rel, rel.lower(), why_k),
        ).fetchall()
        why_drifts = self._node_drifts([r["id"] for r in why_rows])  # one query, not k
        why = [
            {
                "node_id": r["id"], "kind": r["kind"], "title": r["title"],
                "why": (r["body"] or "").splitlines()[0] if r["body"] else "",
                "sha": (r["stamped_at_sha"] or "")[:7],
                "drift": why_drifts.get(r["id"]),
                # arrow 1: the claim's own re-runnable check (surfaced so the modal can show
                # "recall re-verified this claim"). None for a claim without a predicate.
                "predicate": r["predicate"],
                # v9: what CAME of the decision — the chain end, distinct from the title.
                # None when nothing was recorded (shown honestly, never interpreted).
                "outcome": r["outcome"],
            }
            for r in why_rows
        ]
        known = bool(symbols or why_rows or self.db.execute(
            "SELECT 1 FROM nodes WHERE REPLACE(file_path,'\\','/')=? LIMIT 1", (rel,)
        ).fetchone())
        # activity-console signal (v7): one row per briefing. The file is the "query";
        # surfaced=1 when the file is actually known, so the console can show empty briefs too.
        self._log(rel, None, 0, 1 if known else 0,
                  round((time.perf_counter() - t0) * 1_000_000), consumer, kind="brief")
        # Landmines lead, and a lesson that is BOTH pinned here and warns about this file
        # must not show twice (once as WHY, once as LANDMINE) — the louder conscience signal
        # wins, so drop those node-ids from `why` (adversarial sweep 2026-06-15, dedup-overlap).
        warns = self._landmines(rel)
        warn_ids = {w["node_id"] for w in warns}
        why = [w for w in why if w["node_id"] not in warn_ids]
        out = {
            "file": rel,
            "known": known,
            "symbols": symbols,
            "why": why,
            "warns": warns,
            "depends_on": self._file_dependencies(rel),
            "breaks": self._blast_radius(rel),
            "open_tasks": self.open_tasks_for_file(rel),
            "drift": self._file_drift(rel),
        }
        # MOVES-WITH neighborhood (workstream D, always-on, pure SQL) — guarded so a query hiccup
        # never breaks the briefing; degrades to a silenced cluster.
        try:
            out["neighborhood"] = self.neighborhood(rel)
        except Exception:
            out["neighborhood"] = {"file": rel, "cluster": [], "bound_by": None, "silenced": True}
        # RICH brief (dashboard story chain only, 2026-06-15): the extra experience layers
        # that make the causal chain reicher — impact (precise blast radius from co-change +
        # structural dependents) and precedent (have we faced an analogous situation before?).
        # Kept behind `rich` so the CLI/MCP brief stays lean and fast (these run extra serves);
        # each is empty-but-shaped when there's nothing, so the modal renders nothing for it.
        if rich and known:
            try:
                imp = self.impact(rel, limit=8)
                out["impact"] = imp.get("impacted", [])
            except Exception:
                out["impact"] = []
            # precedent is keyed off a SITUATION; the file's lead note title is the best
            # available "what am I about to touch" phrase. Skip if the file has no why-note.
            try:
                situation = why[0]["title"] if why else None
                if situation:
                    prec = self.precedent(situation, limit=4)
                    # don't echo the very note we're reading back as its own precedent
                    out["precedent"] = [
                        p for p in prec.get("precedents", [])
                        if p.get("node_id") not in {w["node_id"] for w in why[:1]}
                    ][:3]
                else:
                    out["precedent"] = []
            except Exception:
                out["precedent"] = []
        return out

    # ------------------------------------------------- contested spots (Wave B, ADR-019)
    def resolve(self, guess: str, *, warmth: float | None = None, top: int = 5) -> dict[str, Any]:
        """Search-inversion (ADR-037): turn a HALLUCINATED search term into the real
        vocabulary of THIS repo BEFORE anything greps. `enforceSeats` → this repo
        means `confirmSeatOrRollback`. The vocabulary mismatch, corrected from
        write-time repo experience (not text statistics) — see recall.resolve.

        warmth: None → use the index's own measured warmth (fraction of symbols with
        lived experience), so a young/cold repo gets pure vocabulary correction and a
        worked-in repo additionally gets the experience tie-break + learned synonyms.
        Read-only, 0 model tokens; re-ranks and annotates but NEVER drops a candidate
        (grep stays the complete recall)."""
        from recall.resolve import Resolver
        # reuse the per-process-warm resolver; rebuilt only after a node/anchor mutation
        # (see _invalidate_corpus_stats). Rebuilding it per call cost ~55ms (full symbol
        # load + synonym mining) — the dominant term in resolve()'s latency.
        if self._resolver is None:
            self._resolver = Resolver(self.db)
        r = self._resolver
        idx_warmth = r.warmth_of_index()
        eff = idx_warmth if warmth is None else warmth
        cands = r.resolve(guess, warmth=eff, top=top)
        # log the resolve like any other read so the flywheel can learn from it too
        # (which guess landed on which symbol), but mark the consumer so it doesn't
        # masquerade as a primary query in synonym mining.
        try:
            self._log(guess, cands[0].node_id if cands else None,
                      0, 1 if cands else 0, 0, "resolve", kind="resolve")
        except Exception:
            pass
        return {
            "guess": guess,
            "index_warmth": idx_warmth,
            "warmth_used": eff,
            "candidates": [
                {
                    "symbol": c.symbol, "file": c.file_path, "line": c.line,
                    "node_id": c.node_id, "vocab_score": round(c.vocab_score, 4),
                    "exp_score": round(c.exp_score, 4), "via_synonym": c.via_synonym,
                    "why": c.why,
                }
                for c in cands
            ],
        }

    # ------------------------------------------------- precedent (arrow 3, the recall arrow)
    def precedent(self, situation: str, *, limit: int = 5, consumer: str = "cli") -> dict[str, Any]:
        """Precedent — arrow 3, the `recall` arrow (core → AI). Given a free-text SITUATION
        the AI is about to act in ("switching auth to JWT", "adding a money path"), serve the
        most ANALOGOUS past DECISIONS/LESSONS, each tagged with its OUTCOME, so the AI
        generalizes from THIS repo's lived experience instead of its priors.

        Where recall() answers "where / why / what" across every node kind and brief() answers
        a single file, precedent() answers a JUDGEMENT question — "have we been here before,
        and how did it go?" — and is therefore scoped to the deliberate, reusable record:
        kind IN ('decision','lesson'). Code symbols, commits and tasks are not precedent.

        The value over a plain knowledge search is the OUTCOME attached to each hit — the part
        that turns a search result into a precedent:
          superseded_by  — the decision that LATER replaced this one (the rule that governs
                           NOW: the terminal of the supersedes chain); None if it still stands.
                           "We tried X and reversed it" IS the lesson, so it is kept, not hidden.
          became_landmine— this decision now `warns_about` code, i.e. it bit someone and was
                           promoted to a conscience warning (arrow 2) — a strong "heed this".
          drift          — the code this precedent was pinned to has moved since, so it may no
                           longer describe the codebase (verify before leaning on it).

        Ranked by relevance (most analogous first), importance as the tie-break — never by
        outcome: the AI wants the closest situation regardless of how it ended, because the
        ending is exactly what it came to learn. Read-only, model-free, 0 tokens (ADR-014)."""
        t0 = time.perf_counter()
        toks = sorted({t for t in tokenize_query(situation)
                       if t and t not in self.rules.query_stopwords})
        if not toks:
            return self._no_precedent(situation, "no tokens", t0)
        raw, bm25 = self._bm25_scores(toks)
        if not raw:
            return self._no_precedent(situation, "no match", t0)
        floor = self.rules.silence_floor
        scored: list[tuple[int, float, Any]] = []
        for node_id, hits in raw.items():
            if hits < floor:
                continue  # RAW floor — weighting can't manufacture a precedent from nothing
            n = self.db.execute(
                "SELECT kind, title, body, file_path, stamped_at_sha, importance "
                "FROM nodes WHERE id=?", (node_id,)).fetchone()
            if n is None or n["kind"] not in ("decision", "lesson"):
                continue  # precedent = the DELIBERATE record only (no code / commits / tasks)
            facets = self._node_facets(node_id)
            if self._is_tabu(facets):
                continue  # rules.md stay_silent_on — suppressed regardless of score
            fw = max([self.rules.facet_weight(f) for f in facets] or [1.0])
            scored.append((node_id, bm25.get(node_id, 0.0) * fw, n))
        # most analogous first; importance breaks ties; node_id last for determinism
        scored.sort(key=lambda x: (-x[1], -(x[2]["importance"] or 0), x[0]))
        scored = scored[:max(1, limit)]
        latency_us = round((time.perf_counter() - t0) * 1_000_000)
        if not scored:
            res = self._no_precedent(situation, f"no decision/lesson above floor ({floor})",
                                     t0, latency_us=latency_us)
            self._log(situation, None, 0, 0, latency_us, consumer, kind="precedent")
            return res
        precedents = [self._precedent_outcome(nid, score, n) for nid, score, n in scored]
        self._log(situation, scored[0][0], scored[0][1], 1, latency_us, consumer, kind="precedent")
        return {
            "situation": situation,
            "silenced": False,
            "latency_us": latency_us,
            "precedents": precedents,
        }

    # ------------------------------------------------- impact (AI-native call-hierarchy replacement)
    def _co_change_partners(self, files) -> dict[str, int]:
        """The co-change partner-degree map for a set of files: {partner_file: distinct co-change
        degree}, excluding the input files. Factored byte-identically out of impact() (the SYMMETRIC-
        pair double-count fix, sweep #1/#11) so impact() AND neighborhood() (workstream D) share ONE
        query — pinned by the agreement drift-guard. Pure SELECT, model-free."""
        files = set(files)
        targets = sorted(files)[:50]
        if not targets:
            return {}
        ph = ",".join("?" * len(targets))
        co: dict[str, int] = {}
        for row in self.db.execute(
            f"""
            SELECT partner, COUNT(DISTINCT tfile) AS deg FROM (
              SELECT REPLACE(ns.file_path,'\\','/') AS tfile,
                     REPLACE(nd.file_path,'\\','/') AS partner
                FROM edges e JOIN nodes ns ON ns.id=e.src_node JOIN nodes nd ON nd.id=e.dst_node
               WHERE e.kind='co_changed' AND REPLACE(ns.file_path,'\\','/') IN ({ph})
                 AND nd.file_path IS NOT NULL
              UNION ALL
              SELECT REPLACE(nd.file_path,'\\','/') AS tfile,
                     REPLACE(ns.file_path,'\\','/') AS partner
                FROM edges e JOIN nodes ns ON ns.id=e.src_node JOIN nodes nd ON nd.id=e.dst_node
               WHERE e.kind='co_changed' AND REPLACE(nd.file_path,'\\','/') IN ({ph})
                 AND ns.file_path IS NOT NULL
            ) GROUP BY partner
            """,
            targets + targets,
        ).fetchall():
            p = row["partner"]
            if p and p not in files:
                co[p] = row["deg"]
        return co

    def _depends_corroboration(self, rel: str) -> set[str]:
        """Partner files that ALSO have a depends_on/implements/guarded_by edge to/from `rel` —
        used as a confidence LABEL only (import+co-change vs co-change-only), NEVER to filter or
        reorder (protects ADR-028 'never drops/hides' + the axis-4 blind spot). Scoped to rel."""
        out: set[str] = set()
        for a, b in self.db.execute(
            """SELECT REPLACE(ns.file_path,'\\','/') AS a, REPLACE(nd.file_path,'\\','/') AS b
                 FROM edges e JOIN nodes ns ON ns.id=e.src_node JOIN nodes nd ON nd.id=e.dst_node
                WHERE e.kind IN ('depends_on','implements','guarded_by')
                  AND ns.file_path IS NOT NULL AND nd.file_path IS NOT NULL
                  AND (REPLACE(ns.file_path,'\\','/')=? OR REPLACE(nd.file_path,'\\','/')=?)""",
            (rel, rel),
        ).fetchall():
            partner = b if a == rel else (a if b == rel else None)
            if partner and partner != rel:
                out.add(partner)
        return out

    def _co_change_verified(self, rel: str) -> dict[str, bool]:
        """{partner_file: edge.verified bool} for `rel`'s co_changed edges — the co-movement
        relationship's OWN freshness (distinct from the partner FILE's drift). A 0 means 'this
        co-change may be stale' (git no longer confirms the pair moves together). Scoped to rel."""
        out: dict[str, bool] = {}
        for a, b, v in self.db.execute(
            """SELECT REPLACE(ns.file_path,'\\','/') AS a, REPLACE(nd.file_path,'\\','/') AS b, e.verified AS v
                 FROM edges e JOIN nodes ns ON ns.id=e.src_node JOIN nodes nd ON nd.id=e.dst_node
                WHERE e.kind='co_changed' AND ns.file_path IS NOT NULL AND nd.file_path IS NOT NULL
                  AND (REPLACE(ns.file_path,'\\','/')=? OR REPLACE(nd.file_path,'\\','/')=?)""",
            (rel, rel),
        ).fetchall():
            partner = b if a == rel else (a if b == rel else None)
            if partner and partner != rel:
                out[partner] = bool(v) and out.get(partner, True)
        return out

    def _binding_decision(self, file_path: str, *, cluster_files=()) -> dict | None:
        """The single most load-bearing decision that GOVERNS this file (or its co-change cluster):
        a decided_by/guarded_by/supersedes/warns_about source pinned to the file or a cluster file,
        importance-ranked, a SUPERSEDED source dropped (the `NOT EXISTS supersedes` clause copied
        from _landmines — NEW SQL, not a reuse: _landmines is warns_about-only). Returns the binding
        row or None. This is what makes the co-change cluster recall-SHAPED, not a git-reconstructable
        degree map (None on a repo with no decided_by/guarded_by — honest, not differentiated there)."""
        rel = (file_path or "").replace("\\", "/")
        scope = sorted({s for s in ([rel] + [(f or "").replace("\\", "/") for f in cluster_files]) if s})[:51]
        if not scope:
            return None
        ph = ",".join("?" * len(scope))
        row = self.db.execute(
            f"""
            SELECT ns.id, ns.kind, ns.title, ns.body, ns.stamped_at_sha
              FROM nodes nd
              JOIN edges e ON e.dst_node = nd.id
              JOIN nodes ns ON ns.id = e.src_node
             WHERE e.kind IN ('decided_by','guarded_by','supersedes','warns_about')
               AND REPLACE(nd.file_path,'\\','/') IN ({ph})
               AND NOT EXISTS (
                     SELECT 1 FROM edges sup
                      WHERE sup.dst_node = ns.id AND sup.kind = 'supersedes')
             GROUP BY ns.id
             ORDER BY ns.importance DESC, ns.id DESC
             LIMIT 1
            """,
            scope,
        ).fetchone()
        if row is None:
            return None
        return {"node_id": row["id"], "kind": row["kind"], "title": row["title"],
                "why": (row["body"] or "").splitlines()[0] if row["body"] else "",
                "sha": (row["stamped_at_sha"] or "")[:7], "drift": self._node_drift(row["id"])}

    def neighborhood(self, file_path: str, *, limit: int = 8, min_partners: int = 1) -> dict[str, Any]:
        """v1.2 Stage-1/2 (workstream D) — the read-only co-change NEIGHBORHOOD of a file: which
        files move WITH it (git-proven), each labeled with a confidence (import-corroborated vs
        co-change-only) and TWO distinct staleness signals — `edge_verified` (is the co-movement
        relationship still git-confirmed) and `partner_drift` (does the partner FILE's own stamped
        knowledge currently fail its check, via the SAME _file_drift B/brief use) — FUSED with the
        one binding decision that governs the cluster. A LENS, never a judgmental verdict;
        `cluster: none` is an honest output. Pure SELECT, model-free. Stage-3 render is OUT OF SCOPE."""
        rel = (file_path or "").replace("\\", "/")
        partners = self._co_change_partners([rel])
        if len(partners) < max(0, min_partners):
            return {"file": rel, "cluster": [], "bound_by": self._binding_decision(rel),
                    "silenced": True,
                    "why": "too little co-change history yet — the neighborhood grows as commits accumulate"}
        global_deg = self._co_changed_degrees()        # hub down-weight signal (label only)
        corrob = self._depends_corroboration(rel)       # import corroboration (label only)
        verified_map = self._co_change_verified(rel)    # the co-change edge's own freshness
        ordered = sorted(partners.items(), key=lambda kv: (-kv[1], kv[0]))[:max(1, limit)]
        cluster = [{
            "file": partner,
            "confidence": "import + co-change" if partner in corrob else "co-change only",
            "co_degree": deg,
            "partner_degree": global_deg.get(partner, deg),
            "edge_verified": verified_map.get(partner, True),
            "partner_drift": self._file_drift(partner),
        } for partner, deg in ordered]
        bound = self._binding_decision(rel, cluster_files=[c["file"] for c in cluster])
        return {"file": rel, "cluster": cluster, "bound_by": bound, "silenced": False}

    # ------------------------------------------------- impact (AI-native call-hierarchy replacement)
    def impact(self, target: str, *, depth: int = 2, limit: int = 25,
               consumer: str = "cli") -> dict[str, Any]:
        """AI-native impact set — "if I touch this, what is actually affected?" — the 0-token
        read-time answer that replaces a human-style call hierarchy.

        A call graph is a HUMAN navigation aid: it must be traversed at read time and only
        knows the THEORETICAL wiring (imports/calls). recall answers the real edit-time
        question from two truths a static call graph can't use:
          • EMPIRICAL co-change — what git history PROVES moved together (recall's `co_changed`
            graph). Language-agnostic, free, and often a better impact predictor than imports —
            a config and a handler with no import edge still always shipped together.
          • STRUCTURAL dependents — the transitive depends_on/implements graph (who imports it).
        Fused, weighted by causal importance, annotated with landmines + drift. Everything is
        pre-stamped at write time, so the read is a pure SELECT — 0 model tokens (ADR-014).

        Resolves a SYMBOL or a FILE to the file(s) it lives in (recall's static graph is
        file-granular — anchored on each file's representative symbol; it builds no per-call-site
        edges, by design: 'a wrong edge is worse than no edge'. A future write-time `calls` edge,
        emitted by the editing AI itself, would slot into the SAME walk for function precision —
        no static parser). Empty-but-shaped for an unknown target, never an error."""
        t0 = time.perf_counter()
        depth = max(1, depth)  # adversarial sweep #17/#18: depth<1 made struct weights negative
        files = self._resolve_to_files(target)
        if not files:
            return {"target": target, "resolved": [], "silenced": True,
                    "reason": "unknown target", "impacted": [],
                    "latency_us": round((time.perf_counter() - t0) * 1_000_000)}
        # cap the resolved set: a name in 50+ files is too generic for a meaningful impact, and an
        # unbounded IN(...) would trip SQLite's variable limit (sweep #16).
        targets = sorted(files)[:50]
        # 1. EMPIRICAL — co-change partners. Factored to _co_change_partners() so neighborhood()
        # (workstream D) shares the SAME query: the SYMMETRIC-pair double-count fix (sweep #1/#11)
        # lives there ONCE, and an agreement drift-guard pins the two paths can never diverge.
        co = self._co_change_partners(files)
        # 2. STRUCTURAL — transitive dependents (who imports it), BFS up the reverse edge map.
        # `calls` is included so a future write-time call edge needs no code change here.
        rev: dict[str, set[str]] = {}  # dependency_file -> {dependent_files}
        for s, d in self.db.execute(
            """
            SELECT DISTINCT REPLACE(ns.file_path,'\\','/') AS dependent,
                            REPLACE(nd.file_path,'\\','/') AS dependency
              FROM edges e JOIN nodes ns ON ns.id=e.src_node JOIN nodes nd ON nd.id=e.dst_node
             WHERE e.kind IN ('depends_on','implements','guarded_by','calls')
               AND ns.file_path IS NOT NULL AND nd.file_path IS NOT NULL
               AND ns.file_path != nd.file_path
            """
        ).fetchall():
            if s and d and s != d:
                rev.setdefault(d, set()).add(s)
        struct: dict[str, int] = {}  # dependent file -> min hop
        seen = set(files)
        frontier = set(files)
        for hop in range(1, max(1, depth) + 1):
            nxt = set()
            for f in frontier:
                for dep in rev.get(f, ()):
                    if dep not in seen:
                        nxt.add(dep)
            if not nxt:
                break
            for dep in nxt:
                struct[dep] = hop
                seen.add(dep)
            frontier = nxt
        # 3. MERGE + score. co-change leads (the empirical signal), structural is second,
        # importance gently boosts a load-bearing dependent. Closer hop = stronger structural.
        W_CO, W_STRUCT, IMP_SCALE = 2.0, 1.0, 25.0
        rows = []
        for f in set(co) | set(struct):
            co_deg = co.get(f, 0)
            hop = struct.get(f)
            struct_w = (depth - hop + 1) if hop else 0
            imp = self._file_importance(f)
            tw = 0.5 if _is_test_file(f) else 1.0  # sweep #10: a test that exercises X is lower-impact than prod that uses it
            score = (W_CO * co_deg + W_STRUCT * struct_w) * (1.0 + imp / IMP_SCALE) * tw
            rows.append({"file": f, "co_change": co_deg, "struct_hop": hop,
                         "importance": round(imp, 1), "score": round(score, 2)})
        rows.sort(key=lambda r: (-r["score"], -r["co_change"], r["file"]))
        rows = rows[:max(1, limit)]
        # annotate ONLY the surfaced rows (one meta lookup per shown row, never per candidate)
        for r in rows:
            r["drift"] = self._file_drift(r["file"])
            r["landmine"] = bool(self._landmines(r["file"], limit=1))
        latency_us = round((time.perf_counter() - t0) * 1_000_000)
        self._log(target, None, 0, 1 if rows else 0, latency_us, consumer, kind="impact")
        try:
            nb = self.neighborhood(targets[0]) if targets else {"cluster": [], "bound_by": None, "silenced": True}
        except Exception:
            nb = {"cluster": [], "bound_by": None, "silenced": True}
        return {"target": target, "resolved": targets, "silenced": not rows,
                "impacted": rows, "neighborhood": nb, "latency_us": latency_us}

    # --------------------------------------------- code intelligence (static-code-intel serves)
    # These answer the navigation questions a static call-graph tool answers (who calls X, what
    # implements this, what's dead/untested, where are the cycles) — but over recall's ALREADY-STAMPED
    # graph, so each is a pure SELECT/walk: 0 model tokens, offline (ADR-014). One deliberate honesty:
    # recall's structural graph is FILE-GRANULAR (it anchors edges on each file's representative
    # symbol and builds no per-call-site edges — "a wrong edge is worse than no edge"). So these serve
    # FILE-level truth and SAY so; a future write-time `calls` edge emitted by the editing AI would
    # slot into the same walks for function precision, no static parser. Every result is shaped-when-
    # empty (silenced), never an error.

    # File representation is NOT uniform: Python files get a NULL-symbol representative node that
    # carries the structural edges; TS/JS files are represented by their per-function nodes and the
    # edges attach to one of THOSE. So we never filter on `symbol IS NULL` — we group by file_path
    # across ALL code-symbol nodes (see _code_files / _depends_on_graph). (Measured 2026-06-15.)
    _STRUCT_EDGES = ("depends_on", "implements", "calls", "guarded_by")
    # Non-code file extensions: a .md/.json/.txt is never "dead code" or "untested code".
    _CODE_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
                  ".rb", ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt")
    # Framework/entry files that are loaded by a runtime or convention, not by an import edge —
    # so "nothing imports it" does NOT mean dead. Matched on the normalized path's basename/segment.
    _ENTRY_RE = re.compile(
        r"(?:^|/)(?:__main__|__init__|conftest|setup|manage|main|index|app|wsgi|asgi)\.[a-z]+$"
        r"|(?:^|/)(?:page|layout|route|loading|error|not-found|middleware|proxy|template|"
        r"head|sitemap|robots|opengraph-image|default)\.[a-z]+$"  # Next.js / web framework convention
        r"|\.config\.[a-z]+$|\.d\.ts$"  # build configs + ambient type decls are loaded by tooling, not imported
    )

    def _norm(self, p: str | None) -> str:
        return (p or "").replace("\\", "/")

    def _on_disk(self, rel: str) -> bool:
        """True if the repo-relative path exists on disk. Guards against stale index nodes
        (a renamed/deleted file leaves a bare-name node behind with 0 edges — it would read
        as 'dead code' but the file is simply gone). No repo known -> can't verify -> assume yes."""
        if not self._repo:
            return True
        try:
            return (self._repo / rel).exists()
        except OSError:
            return True

    def _code_files(self) -> set[str]:
        """Every real, on-disk file that has at least one code-symbol node (normalized path).
        File-granular by design: a file is one entry no matter how many functions/classes it
        holds, and edges may attach to ANY of its nodes (Python files get a NULL-symbol
        representative node; TS/JS files are represented by their per-function nodes — there is
        no single rep convention, so we group by file_path). The on-disk filter drops stale
        bare-name/renamed nodes so dead/untested can't false-positive on phantoms."""
        out: set[str] = set()
        for (fp,) in self.db.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE kind='code-symbol' "
            "AND file_path IS NOT NULL AND file_path != ''"
        ).fetchall():
            rel = self._norm(fp)
            if rel and rel not in out and self._on_disk(rel):
                out.add(rel)
        return out

    def _depends_on_graph(self) -> dict[str, set[str]]:
        """The file→file dependency map: adj[a] = {b, ...} means file a DEPENDS ON (imports) b.
        Built once from the depends_on edges, normalized, self-loops dropped. The spine of
        callers/callees/cycles."""
        adj: dict[str, set[str]] = {}
        for s, d in self.db.execute(
            "SELECT REPLACE(ns.file_path,'\\','/'), REPLACE(nd.file_path,'\\','/') "
            "FROM edges e JOIN nodes ns ON ns.id=e.src_node JOIN nodes nd ON nd.id=e.dst_node "
            "WHERE e.kind='depends_on' AND ns.file_path IS NOT NULL AND nd.file_path IS NOT NULL"
        ).fetchall():
            if s and d and s != d:
                adj.setdefault(s, set()).add(d)
        return adj

    def _annotate_file(self, rel: str, hop: int | None = None) -> dict[str, Any]:
        """The per-row decoration every code-intel serve shares: importance, drift, landmine flag."""
        row = {"file": rel, "importance": self._file_importance(rel),
               "drift": self._file_drift(rel), "landmine": bool(self._landmines(rel, limit=1))}
        if hop is not None:
            row["hop"] = hop
        return row

    def callers(self, target: str, *, depth: int = 2, limit: int = 50,
                consumer: str = "cli") -> dict[str, Any]:
        """Who depends on this? — the FILE-granular call-hierarchy replacement (reverse direction).
        Walks UP the depends_on graph from the target's file(s): every file that imports it,
        transitively, with the hop distance. Ranked by closeness then importance. 0 tokens."""
        return self._hierarchy(target, direction="callers", depth=depth, limit=limit, consumer=consumer)

    def callees(self, target: str, *, depth: int = 2, limit: int = 50,
                consumer: str = "cli") -> dict[str, Any]:
        """What does this depend on? — the forward direction: every file the target imports,
        transitively, with hop distance. The other half of the call hierarchy. 0 tokens."""
        return self._hierarchy(target, direction="callees", depth=depth, limit=limit, consumer=consumer)

    def _hierarchy(self, target: str, *, direction: str, depth: int, limit: int,
                   consumer: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        depth = max(1, depth)
        files = self._resolve_to_files(target)
        if not files:
            return self._empty_hierarchy(target, direction, "unknown target", t0, consumer)
        adj = self._depends_on_graph()
        if direction == "callees":
            graph = adj  # a -> things a depends on
        else:
            graph = {}    # reverse: b -> things that depend on b
            for a, deps in adj.items():
                for b in deps:
                    graph.setdefault(b, set()).add(a)
        seen: set[str] = set(self._norm(f) for f in files)
        frontier = set(seen)
        out: dict[str, int] = {}
        for hop in range(1, depth + 1):
            nxt: set[str] = set()
            for f in frontier:
                for nb in graph.get(f, ()):
                    if nb not in seen:
                        nxt.add(nb)
            if not nxt:
                break
            for nb in nxt:
                out[nb] = hop
                seen.add(nb)
            frontier = nxt
        rows = [self._annotate_file(f, hop=h) for f, h in out.items()]
        rows.sort(key=lambda r: (r["hop"], -r["importance"], r["file"]))
        rows = rows[:max(1, limit)]
        latency_us = round((time.perf_counter() - t0) * 1_000_000)
        self._log(target, None, 0, 1 if rows else 0, latency_us, consumer, kind=direction)
        out_dict = {"target": target, "direction": direction, "granularity": "file",
                    "resolved": sorted(self._norm(f) for f in files), "silenced": not rows,
                    "results": rows, "latency_us": latency_us,
                    "note": "file-granular: recall builds no per-call-site edges by design"}
        if not rows:
            # resolved, but no edges on this side — carry a reason so the silenced contract is
            # uniform with the unknown-target path (red-team callgraph P3). A slash-bearing name
            # that no node actually carries resolves best-effort, so this is the common empty case.
            verb = "depends on nothing" if direction == "callees" else "is used by nothing"
            out_dict["reason"] = f"no {direction} edges recorded ({verb})"
        return out_dict

    def _empty_hierarchy(self, target, direction, reason, t0, consumer):
        latency_us = round((time.perf_counter() - t0) * 1_000_000)
        self._log(target, None, 0, 0, latency_us, consumer, kind=direction)
        return {"target": target, "direction": direction, "granularity": "file",
                "resolved": [], "silenced": True, "reason": reason, "results": [],
                "latency_us": latency_us}

    # NOTE: no `implementors` serve. recall's `implements` edge does NOT encode "file A realizes
    # interface B" — measured against the live index, all `implements` edges go lesson/decision ->
    # code-file (they record which DECISION governs a file, not a code-to-code interface link). So a
    # file->file implementors() would be dead on arrival (every src.file_path is NULL). Shipping an
    # always-empty command that misrepresents the data is exactly the kind of thing that loses trust
    # at launch — "a wrong edge is worse than no edge". The govern-this-file relationship is already
    # served by brief()'s WHY track. (Adversarial red-team finding, 2026-06-15.)

    def dead_code(self, *, limit: int = 50, consumer: str = "cli") -> dict[str, Any]:
        """Dead-code CANDIDATES — code files that exist on disk but NOTHING in the recorded graph
        points at them. Conservative by construction: only real on-disk CODE files (excludes
        docs/configs), excludes test files (a test is meant to have no importer) and framework/entry
        files (cli, __main__, Next.js page/route/layout — loaded by a runtime, not an import). The
        live import signal is `depends_on`; `implements`/`guarded_by` are kept in the incoming check
        only so a file a decision governs is never wrongly flagged (the safe, conservative direction;
        `calls` is reserved for a future write-time edge and matches nothing today). Labelled
        CANDIDATES: a file-granular graph can't see dynamic imports/reflection, so this narrows
        where to look, it doesn't condemn. 0 tokens."""
        t0 = time.perf_counter()
        files = self._code_files()  # on-disk code files only (phantoms already dropped)
        # which files have an incoming structural edge (via ANY of their nodes)? Being inclusive here
        # makes dead-code MORE conservative (fewer false positives) — the safe direction.
        imported: set[str] = set()
        eph = ",".join("?" * len(self._STRUCT_EDGES))
        for d in self.db.execute(
            f"SELECT DISTINCT REPLACE(nd.file_path,'\\','/') "
            f"FROM edges e JOIN nodes nd ON nd.id=e.dst_node "
            f"WHERE e.kind IN ({eph}) AND nd.file_path IS NOT NULL",
            list(self._STRUCT_EDGES),
        ).fetchall():
            if d[0]:
                imported.add(d[0])
        cands = []
        for rel in files:
            if rel in imported:
                continue
            if not rel.lower().endswith(self._CODE_EXTS):
                continue  # docs/config are not "dead code"
            if _is_test_file(rel) or self._ENTRY_RE.search(rel.lower()):
                continue  # tests + entrypoints are SUPPOSED to have no importer
            cands.append(self._annotate_file(rel))
        cands.sort(key=lambda r: (r["importance"], r["file"]))  # least-important first = most likely dead
        cands = cands[:max(1, limit)]
        latency_us = round((time.perf_counter() - t0) * 1_000_000)
        self._log("dead-code", None, 0, len(cands), latency_us, consumer, kind="dead_code")
        return {"silenced": not cands, "granularity": "file", "candidates": cands,
                "latency_us": latency_us,
                "note": "candidates — file-granular graph can't see dynamic imports; verify before deleting"}

    def untested(self, *, limit: int = 50, consumer: str = "cli") -> dict[str, Any]:
        """Code files with NO recorded link to any test file — no test depends_on / co_changed /
        calls them. The file-granular 'what has no test edge?' serve. Excludes test files, docs,
        and entry files. Honest: 'no test EDGE recorded' (a test that exercises a file only via a
        deep transitive import may not show a direct edge). 0 tokens."""
        t0 = time.perf_counter()
        files = self._code_files()
        # files a TEST file IMPORTS (depends_on / calls only). co_changed is DELIBERATELY excluded:
        # it is symmetric and empirical, so a single git co-change between a payment file and a test
        # would falsely mark the payment file "tested" — measured, it hid 23 genuinely-untested files
        # incl. stripe.ts / orgs.ts / the billing webhook. A test that actually exercises a module
        # IMPORTS it (a depends_on edge); that is the honest, rigorous test signal. (Red-team 2026-06-15.)
        tested: set[str] = set()
        for s_fp, d_fp in self.db.execute(
            "SELECT REPLACE(ns.file_path,'\\','/'), REPLACE(nd.file_path,'\\','/') "
            "FROM edges e JOIN nodes ns ON ns.id=e.src_node JOIN nodes nd ON nd.id=e.dst_node "
            "WHERE e.kind IN ('depends_on','calls') "
            "AND ns.file_path IS NOT NULL AND nd.file_path IS NOT NULL"
        ).fetchall():
            # a test importing prod marks the prod file tested; the reverse (prod importing a test)
            # is nonsense and rare, but symmetric handling is cheap and harmless.
            if s_fp and d_fp:
                if _is_test_file(s_fp):
                    tested.add(d_fp)
                if _is_test_file(d_fp):
                    tested.add(s_fp)
        rows = []
        for rel in files:
            if rel in tested:
                continue
            if not rel.lower().endswith(self._CODE_EXTS):
                continue
            if _is_test_file(rel) or self._ENTRY_RE.search(rel.lower()):
                continue
            rows.append(self._annotate_file(rel))
        rows.sort(key=lambda r: (-r["importance"], r["file"]))  # most-important-untested first
        rows = rows[:max(1, limit)]
        latency_us = round((time.perf_counter() - t0) * 1_000_000)
        self._log("untested", None, 0, len(rows), latency_us, consumer, kind="untested")
        return {"silenced": not rows, "granularity": "file", "untested": rows,
                "latency_us": latency_us,
                "note": "no recorded test edge — a deep transitive test link may not show here"}

    # A path-carrying DFS over every node enumerates ALL simple cycles, which is factorial in a
    # strongly-connected cluster (measured: >1.1M cycles / >15s at just 10 mutually-importing files
    # — a real risk on a legacy module with circular imports, not just adversarial input). So we
    # first find strongly-connected COMPONENTS (Tarjan, O(V+E)): a cycle lives entirely inside one
    # SCC. We then enumerate short representative cycles ONLY inside each SCC, under a hard push
    # budget. The common case (a few small cycles in an otherwise acyclic graph) is instant; a
    # pathological SCC is reported as the entangled set + a budget-bounded sample, never a hang.
    _CYCLE_PUSH_BUDGET = 50_000

    def cycles(self, *, limit: int = 50, consumer: str = "cli") -> dict[str, Any]:
        """Dependency cycles — file→file import cycles (A imports B imports ... A). Tarjan SCC first
        (a cycle is confined to one SCC), then a budget-bounded short-cycle enumeration inside each
        multi-node SCC. Each distinct cycle is canonicalized (rotated to its lexicographically
        smallest member) so it is reported ONCE regardless of entry point. Bounded — never hangs.
        0 tokens. `truncated` is True if the push budget cut the enumeration short."""
        t0 = time.perf_counter()
        adj = self._depends_on_graph()
        sccs = self._strongly_connected(adj)  # only components of size > 1 (or a self-loop) cycle
        found: set[tuple[str, ...]] = set()
        pushes = 0
        truncated = False
        for comp in sccs:
            if truncated:
                break
            comp_set = comp  # restrict the walk to this SCC — edges out of it can't be on a cycle
            for start in sorted(comp):
                stack: list[tuple[str, tuple[str, ...]]] = [(start, (start,))]
                while stack:
                    node, path = stack.pop()
                    for nb in adj.get(node, ()):
                        if nb not in comp_set:
                            continue
                        if nb == start:           # closed a cycle back to this start
                            m = path.index(min(path))
                            found.add(path[m:] + path[:m])  # canonical rotation
                        elif nb not in path and len(path) < len(comp_set):
                            pushes += 1
                            if pushes > self._CYCLE_PUSH_BUDGET:
                                truncated = True
                                stack.clear()
                                break
                            stack.append((nb, path + (nb,)))
                    if truncated:
                        break
        cycles = sorted(found, key=lambda c: (len(c), c))[:max(1, limit)]
        rows = [{"files": list(c), "length": len(c),
                 "importance": round(max((self._file_importance(f) for f in c), default=0.0), 1)}
                for c in cycles]
        rows.sort(key=lambda r: (-r["importance"], r["length"], r["files"]))
        latency_us = round((time.perf_counter() - t0) * 1_000_000)
        self._log("cycles", None, 0, len(rows), latency_us, consumer, kind="cycles")
        note = "file-granular import cycles from the depends_on graph"
        if truncated:
            note += " — enumeration truncated at the push budget; a very tangled cluster has more"
        return {"silenced": not rows, "granularity": "file", "cycles": rows,
                "truncated": truncated, "latency_us": latency_us, "note": note}

    @staticmethod
    def _strongly_connected(adj: dict[str, set[str]]) -> list[set[str]]:
        """Tarjan's SCC, iterative (no recursion-depth limit). Returns only the components that can
        contain an inter-file cycle: size > 1. (`adj` already drops self-loops — a file "importing
        itself" is an indexer artifact, not a real cycle — so a 1-node SCC is never a cycle.) O(V+E)."""
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        scc_stack: list[str] = []
        result: list[set[str]] = []
        counter = 0
        nodes = set(adj) | {d for deps in adj.values() for d in deps}
        for root in sorted(nodes):
            if root in index:
                continue
            # work stack of (node, iterator-of-neighbors); emulate recursion iteratively
            work: list[tuple[str, list[str]]] = [(root, sorted(adj.get(root, ())))]
            index[root] = low[root] = counter
            counter += 1
            scc_stack.append(root)
            on_stack.add(root)
            while work:
                node, nbrs = work[-1]
                advanced = False
                while nbrs:
                    nb = nbrs.pop(0)
                    if nb not in index:
                        index[nb] = low[nb] = counter
                        counter += 1
                        scc_stack.append(nb)
                        on_stack.add(nb)
                        work.append((nb, sorted(adj.get(nb, ()))))
                        advanced = True
                        break
                    elif nb in on_stack:
                        low[node] = min(low[node], index[nb])
                if advanced:
                    continue
                # done with node — if it's an SCC root, pop the component
                if low[node] == index[node]:
                    comp: set[str] = set()
                    while True:
                        w = scc_stack.pop()
                        on_stack.discard(w)
                        comp.add(w)
                        if w == node:
                            break
                    if len(comp) > 1:   # adj has no self-loops, so a singleton SCC never cycles
                        result.append(comp)
                work.pop()
                if work:  # propagate low-link up to the parent
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
        return result

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
        the index was never freshened. 'broken' (a claim's check now FAILS, arrow 1)
        is the loudest, then 'uncommitted' (edited), then 'committed' (drifted), then
        'fresh' — so the briefing shows the loudest warning. (broken added 2026-06-15;
        without it a BROKEN-only file fell through to None and read as fresh.)"""
        rank = {"broken": 4, "uncommitted": 3, "committed": 2, "fresh": 1}
        # One JOIN instead of N+1: this used to fetch the file's node ids, then issue a
        # separate meta lookup per node (~99 queries for a big file — the bulk of brief()'s
        # SQL round-trips, perf pass 2026-06-18). Join nodes→meta on the drift:<id> key and
        # take the worst level in a single pass.
        worst = None
        worst_rank = 0
        rows = self.db.execute(
            "SELECT m.value FROM nodes n "
            "JOIN meta m ON m.key = 'drift:' || n.id "
            "WHERE REPLACE(n.file_path,'\\','/')=?",
            (rel,),
        ).fetchall()
        for (level,) in rows:
            if level and rank.get(level, 0) > worst_rank:
                worst, worst_rank = level, rank[level]
        return worst

    def onboarding(self, *, top_k: int = 12, dec_k: int = 8, task_k: int = 12,
                   contested_k: int = 6, broken_k: int = 6, consumer: str = "cli") -> dict[str, Any]:
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
        t0 = time.perf_counter()
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

        # 🔴 BROKEN claims (workstream B, arrow 1): nodes whose stamped check FAILS its
        # re-run NOW. The SINGLE SOURCE is the persisted meta 'drift:<id>'='broken' rows —
        # exactly what freshen()/merge_signal write and what _file_drift/_node_drifts read,
        # so the pushed flag, the brief() field and the dashboard all derive from one place.
        # Loudest (most load-bearing) first, capped; the honest TOTAL drives counts['broken'].
        broken_rows = self.db.execute(
            "SELECT n.id, n.title, REPLACE(n.file_path,'\\','/') AS f, n.predicate "
            "FROM nodes n JOIN meta m ON m.key = 'drift:' || n.id "
            "WHERE m.value = 'broken' "
            "ORDER BY n.importance DESC, n.id DESC LIMIT ?",
            (broken_k,),
        ).fetchall()
        broken = [{"node_id": r["id"], "title": r["title"], "file": r["f"],
                   "predicate": r["predicate"]} for r in broken_rows]
        broken_total = self.db.execute(
            "SELECT COUNT(*) FROM nodes n JOIN meta m ON m.key = 'drift:' || n.id "
            "WHERE m.value = 'broken'"
        ).fetchone()[0]

        counts = {
            "files": self.db.execute(
                "SELECT COUNT(DISTINCT file_path) FROM nodes WHERE kind='code-symbol' "
                "AND file_path IS NOT NULL AND file_path != ''").fetchone()[0],
            "lessons": self.db.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind IN ('lesson','decision')").fetchone()[0],
            "decisions": len(decisions),
            "open_tasks": len(in_progress),
            "broken": broken_total,
            "commits": self.db.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind='commit'").fetchone()[0],
        }
        # activity-console signal (v7): one row per explain/onboarding call.
        self._log("explain", None, 0, 1,
                  round((time.perf_counter() - t0) * 1_000_000), consumer, kind="explain")
        return {
            "repo_files": counts["files"],
            "top_files": top_files,
            "decisions": decisions,
            "in_progress": in_progress,
            "contested": contested,
            "broken": broken,
            "counts": counts,
        }

    # ---------------------------------------------- the in-the-path state block (adoption fix)
    # The adoption gap (2026-06-17, 11-agent verified): recall's first link — the AI choosing
    # to call it — routes through the one faculty recall's thesis says is unreliable, secured
    # only by skimmable prose. The highest-leverage fix is NOT a hook: render the live state
    # into the instruction file every client already loads into its system prompt (CLAUDE.md /
    # AGENTS.md / .cursor/rules / copilot-instructions), regenerated on every commit. The AI
    # doesn't CALL recall; it reads the context it always reads, and recall is in it. This
    # converts the big, fixable failure-to-CALL into the irreducible failure-to-ACT (the trade
    # we want). It is coarse (repo/file-level, not the exact edit-time file) and 0 model tokens.
    STATE_BEGIN = "<!-- recall:begin (auto-generated — do not edit by hand) -->"
    STATE_END = "<!-- recall:end -->"

    def render_state_block(self, *, top_k: int = 6, dec_k: int = 5, task_k: int = 6,
                           landmine_k: int = 8) -> str:
        """The recall STATE block for the repo's instruction file — what every AI should
        carry in its system prompt WITHOUT calling a tool. Negation-first (kills the
        'recall is search' prior), then the live load-bearing facts. Read-only, 0 tokens.

        Returned WITHOUT the begin/end markers — `sync_context_files` wraps + writes it,
        so the same body can also be shown by the CLI."""
        ob = self.onboarding(top_k=top_k, dec_k=dec_k, task_k=task_k, consumer="state")
        L: list[str] = []
        L.append("## recall — this project's memory (read before you act)")
        L.append("")
        L.append("recall is NOT search and NOT a token-saver (that's a side effect). It is the "
                 "project's write-time memory: *why* each file is the way it is, *what breaks* "
                 "if you touch it, *what's in flight*, and the *real name* to search for — "
                 "stamped at commit time, read back offline at 0 model tokens.")
        L.append("")
        L.append("**Before editing a file, run `recall brief <file>` first** (orient → then "
                 "grep/read to locate → then edit). Before grepping a name you're guessing, "
                 "run `recall resolve <guess>`. The facts below are a *coarse* snapshot — "
                 "`brief` is the precise, per-file version.")
        L.append("")

        if ob["top_files"]:
            L.append("**Load-bearing files (start here):**")
            for f in ob["top_files"][:top_k]:
                L.append(f"- `{f['file']}` (importance {f['importance']})")
            L.append("")

        if ob["decisions"]:
            L.append("**Must-know decisions (do not silently undo these):**")
            for d in ob["decisions"][:dec_k]:
                title = (d["title"] or "").split("\n", 1)[0][:120]
                L.append(f"- {title}")
            L.append("")

        # landmines across the load-bearing files — the warns_about lessons that exist ONLY
        # in recall and that a cold agent steps on. This is the calibration payload.
        seen_mine: set[str] = set()
        mines: list[str] = []
        for f in ob["top_files"]:
            for w in self._landmines(f["file"], limit=2):
                key = w.get("title", "")[:80]
                if key and key not in seen_mine:
                    seen_mine.add(key)
                    mines.append((w.get("title") or "").split("\n", 1)[0][:140])
                if len(mines) >= landmine_k:
                    break
            if len(mines) >= landmine_k:
                break
        if mines:
            L.append("**Landmines (past mistakes — recall warns about these):**")
            for m in mines:
                L.append(f"- ⚠ {m}")
            L.append("")

        # 🔴 BROKEN claims (workstream B, arrow 1) — a stamped why whose own re-check FAILS
        # right now: treat its claim as wrong until re-verified. Emitted ONLY when there is at
        # least one (so an all-green repo's block is byte-identical to before). Lean on purpose
        # — claim title + a `recall brief` pointer, NOT the predicate regex — so the cached
        # block stays small and stable for an unchanged broken-set.
        if ob["counts"].get("broken", 0) > 0:
            L.append("**🔴 BROKEN claims (a stamped why FAILS its own re-check NOW — "
                     "treat as wrong until re-verified):**")
            for b in ob.get("broken", []):
                title = (b["title"] or "").split("\n", 1)[0][:120]
                where = f" — `recall brief {b['file']}`" if b.get("file") else ""
                L.append(f"- 🔴 {title}{where}")
            extra = ob["counts"]["broken"] - len(ob.get("broken", []))
            if extra > 0:
                L.append(f"- …and {extra} more — `recall explain`")
            L.append("")

        if ob["in_progress"]:
            L.append("**In progress right now (standing intent — treat like a failing test):**")
            for t in ob["in_progress"][:task_k]:
                where = f" — `{t['file']}`" if t.get("file") else ""
                L.append(f"- {(t['title'] or '')[:120]}{where}")
            L.append("")

        # Build/share settings the owner has set (config.toml [share]) — INJECTED so any
        # AI session carries the active rules without asking. Lets an agent honour
        # "new notes are private here" and know how the brain is shared. Only shown when
        # it actually matters (a non-default that changes how the AI should stamp/share).
        try:
            from recall.config import load_build_config
            _repo = self._repo or _infer_repo(self._db_path)
            cfg = load_build_config(_repo)
            sl: list[str] = []
            if cfg.default_visibility == "private":
                # the owner's deliberate, non-default choice — the AI must know new
                # notes stay local here (and that sharing needs `recall export`).
                sl.append("- **New stamps default to PRIVATE** here — they stay on this machine "
                          "and are left out of `recall export`. To share knowledge, stamp it and "
                          "run `recall export`; the raw `.mind` is never committed (a pre-commit "
                          "guard blocks a leak).")
            if sl:
                L.append("**Build & share settings (owner-set):**")
                L.extend(sl)
                L.append("")
        except Exception:
            pass  # settings are advisory in the state block — never break it

        c = ob["counts"]
        L.append(f"_recall knows {c['files']} files · {c['lessons']} lessons · "
                 f"{c['open_tasks']} open tasks. Full picture: `recall explain`; "
                 f"per-file: `recall brief <file>`. Read path = 0 model tokens._")
        block = "\n".join(L)
        # workstream E: record the published state-block SIZE on the consumer='state' row that
        # onboarding() just wrote — thread it in, NEVER a second row (no double-count). Best-effort.
        try:
            self.db.execute(
                "UPDATE access_log SET resp_chars=? WHERE rowid=("
                "SELECT rowid FROM access_log WHERE consumer='state' ORDER BY rowid DESC LIMIT 1)",
                (len(block),))
            self.db.commit()
        except sqlite3.OperationalError:
            pass
        return block

    @staticmethod
    def _verdict_tag(drift: str | None, predicate: str | None) -> str:
        """Axis-3 trust label for a surfaced claim. Empty for a claim with NO predicate; for a
        predicate-backed claim an HONEST verdict from the SAME drift the brief() return carries
        (broken→🔴, fresh→🟢 holds, else ⚪ unverified) — never a false CONFIRMED, never a second
        evaluator. So every predicate-backed claim a push surfaces carries its trust status."""
        if not predicate:
            return ""
        if drift == "broken":
            return "  🔴 BROKEN (its re-check fails now)"
        if drift == "fresh":
            return "  🟢 check holds"
        return "  ⚪ check unverified"  # None (never freshened) or committed/uncommitted (drifted)

    def _situational_file_lines(self, rel: str, b: dict[str, Any]) -> list[str]:
        """Lean per-file push lines: landmines LEAD (the conscience signal), then the live BROKEN
        trust-status, open tasks, and the top why — each predicate-backed claim labeled (axis-3).
        Returns [] for a known-but-empty file so it contributes nothing."""
        out: list[str] = [f"**{rel}**"]
        for w in (b.get("warns") or [])[:3]:
            tag = self._verdict_tag(w.get("drift"), w.get("predicate"))
            out.append(f"- 🔴 landmine: {(w.get('title') or '').split(chr(10), 1)[0][:120]}{tag}")
        if b.get("drift") == "broken":
            out.append("- 🔴 a stamped claim about this file FAILS its re-check NOW — "
                       "treat its WHY as wrong until re-verified")
        elif b.get("drift") in ("committed", "uncommitted"):
            out.append("- ⚠ this file drifted since some knowledge was stamped — verify before trusting it")
        for t in (b.get("open_tasks") or [])[:2]:
            out.append(f"- 📋 open task: {(t.get('title') or '')[:120]}")
        for w in (b.get("why") or [])[:2]:
            tag = self._verdict_tag(w.get("drift"), w.get("predicate"))
            out.append(f"- why: {(w.get('title') or '').split(chr(10), 1)[0][:120]}{tag}")
        out.append("")
        return out if len(out) > 2 else []  # header + blank only → nothing useful

    def _situational_task_lines(self, task: str, *, top_k: int = 3) -> list[str]:
        """Task-scoped push lines: the most relevant stamped knowledge + one analogous precedent.
        Reuses recall()/precedent() (consumer='push') — no new query path."""
        out: list[str] = []
        r = self.recall(task, topk=top_k, consumer="push")
        know = (r.get("knowledge") or [])[:top_k] if not r.get("silenced") else []
        if know:
            out.append("**relevant past knowledge:**")
            for k in know:
                tag = self._verdict_tag(k.get("drift"), k.get("predicate"))
                out.append(f"- {(k.get('title') or '').split(chr(10), 1)[0][:120]}{tag}")
        pre = self.precedent(task, limit=1, consumer="push")
        plist = pre.get("precedents") or []
        if plist:
            out.append(f"**precedent (we faced this before):** "
                       f"{(plist[0].get('title') or '').split(chr(10), 1)[0][:120]}")
        if out:
            out.append("")
        return out

    def render_situational_block(self, *, focus_file: str | None = None,
                                 diff_files: list[str] | None = None,
                                 task: str | None = None, top_k: int = 3) -> str:
        """SITUATIONAL push (workstream A): the scoped brief + landmines + live BROKEN trust-status
        for what the agent is doing NOW — the file about to be edited, the working diff, the stated
        task — fused from brief()/recall()/precedent(), which ALREADY carry the predicate verdict
        (workstream B), so this RENDERS the verdict, never re-evaluates it.

        Unlike the repo-static render_state_block() (cached in the system prompt → ~0 tokens/turn),
        this is FRESH tokens each call, so it is kept terse. With NO situational signal it delegates
        to render_state_block() — the repo-static block is the universal floor. Read-only and
        model-free; the only writes are access_log rows tagged consumer='push'."""
        files: list[str] = []
        if focus_file:
            files.append(focus_file.replace("\\", "/"))
        for f in (diff_files or []):
            rf = (f or "").replace("\\", "/")
            if rf and rf not in files:
                files.append(rf)

        L: list[str] = []
        for rel in files[:5]:
            b = self.brief(rel, consumer="push")
            if b.get("known"):
                L.extend(self._situational_file_lines(rel, b))
        if task:
            L.extend(self._situational_task_lines(task, top_k=top_k))

        if not L:
            return self.render_state_block()  # no situational signal → the static floor

        head = ["## recall — situational memory for what you're doing NOW",
                "_Fresh per prompt (not cached). The repo-wide picture is the recall block already "
                "in your system prompt; this is scoped to your current focus. Read-only, 0 tool calls._",
                ""]
        return "\n".join(head + L).rstrip()

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

    def _node_drifts(self, node_ids: list[int]) -> dict[int, str]:
        """Batch the per-node drift lookup: one query for a set of nodes instead of one
        query each (the why-list and landmines both annotate ≤k rows — perf pass 2026-06-18).
        Returns {node_id: level} only for nodes that have a recorded drift level."""
        if not node_ids:
            return {}
        keys = [f"drift:{n}" for n in node_ids]
        ph = ",".join("?" * len(keys))
        out: dict[int, str] = {}
        for key, val in self.db.execute(
            f"SELECT key, value FROM meta WHERE key IN ({ph})", keys
        ).fetchall():
            try:
                out[int(key.split(":", 1)[1])] = val
            except (ValueError, IndexError):
                continue
        return out

    def _node_drift(self, node_id: int) -> str | None:
        """The drift level the last freshen() recorded for this node, or None if
        the index was never freshened (freshness unknown -> shown fresh)."""
        row = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (f"drift:{node_id}",)
        ).fetchone()
        return row[0] if row else None

    # source extensions where a code-anchor predicate makes sense — a claim about a
    # lockfile/data/doc has no stable code token to pin (keeps the nudge from misfiring).
    _PRED_CODE_EXTS = frozenset(
        ("py", "js", "ts", "jsx", "tsx", "go", "rs", "java", "rb", "php",
         "c", "cc", "cpp", "h", "hpp", "cs", "swift", "kt", "scala"))

    @staticmethod
    def suggest_predicate_from_diff(file_rel: str, added_lines: list[str]) -> str | None:
        """Deterministic, model-free predicate SUGGESTION for a claim-bearing change: propose a
        single `contains:<escaped-anchor>` pinned to a high-signal token in the diff's ADDED
        lines, so an author can add a free re-check (a `Recall-predicate:` trailer). stdlib `re`
        ONLY — never an LLM, never a parser. Returns None aggressively on low signal (a noisy
        nudge just trains the agent to ignore it). Every non-None return is GUARANTEED to
        round-trip through `parse_predicate()` and survive `stamp()`'s validators (len<=500,
        ReDoS-shape reject, parse) — verified here, not assumed."""
        ext = file_rel.rsplit(".", 1)[-1].lower() if file_rel and "." in file_rel else ""
        if ext not in Index._PRED_CODE_EXTS:
            return None  # not a source file — no stable code anchor to pin
        # prefer the most stable, claim-defining tokens: a def/class/function NAME, then an
        # ALL-CAPS constant. A future edit that weakens the claim has to remove that token.
        deflike = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function)\s+([A-Za-z_]\w{2,})\b")
        constlike = re.compile(r"^\s*(?:export\s+)?(?:const\s+)?([A-Z][A-Z0-9_]{3,})\s*[:=]")
        anchor = None
        for line in (added_lines or []):
            m = deflike.match(line)
            if m:
                anchor = m.group(1)
                break
        if anchor is None:
            for line in (added_lines or []):
                m = constlike.match(line)
                if m:
                    anchor = m.group(1)
                    break
        if anchor is None:
            return None  # low signal — stay quiet
        cand = f"contains:{re.escape(anchor)}"
        # GUARANTEE the contract the nudge promises: a pasted suggestion must parse AND survive
        # stamp(). re.escape of an identifier is a literal pattern (short, no ReDoS shape), but
        # we verify rather than assume — if it somehow fails the gate, suggest nothing.
        from recall.predicate import parse_predicate
        if len(cand) > 500 or parse_predicate(cand) is None:
            return None
        return cand

    def _dedupe_results(self, scored: list[tuple]) -> list[tuple]:
        """Collapse rows that point at the same knowledge — git history often has
        a merge + the direct commit with an identical (title, file). Keep the
        highest-scored representative; scored is already score-descending.

        Code symbols are NOT collapsed by (title, file): one file can define
        several distinct symbols of the same name (two `__init__`s, a `render`
        per class), each a separate node — keying on the symbol's line keeps them
        distinct. Only knowledge (commit/lesson) rows merge on (title, file)."""
        seen: set[tuple] = set()
        out: list[tuple] = []
        for s in scored:
            node_id = s[0]
            row = self.db.execute(
                "SELECT title, file_path, kind, symbol, line FROM nodes WHERE id=?",
                (node_id,),
            ).fetchone()
            if row is None:
                continue  # deleted by another process (dashboard forget/undo) mid-query
            if row["kind"] == "code-symbol":
                key = ("code", row["file_path"], row["symbol"], row["line"], node_id)
            else:
                key = ("knowledge", row["title"], row["file_path"])
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        return out

    # -------------------------------------------------------- 3-level builder
    def _build_levels(self, node_id: int, hits: int, score: float, toks: set[str]) -> dict[str, Any] | None:
        n = self.db.execute(
            "SELECT kind,title,body,file_path,symbol,line,stamped_at_sha,origin "
            "FROM nodes WHERE id=?",
            (node_id,),
        ).fetchone()
        if n is None:
            # the node was deleted by another process (dashboard forget()/undo_power_run())
            # in the window between scoring and building — skip it, don't deref None.
            # (P2 bug-hunt 2026-06-15.) Caller filters None.
            return None
        matched = sorted(
            t for t in toks
            if self.db.execute(
                "SELECT 1 FROM fts_anchors WHERE term MATCH ? AND node_id=? LIMIT 1",
                (_fts_phrase(t), node_id),
            ).fetchone()
        )
        # Level 3: relation — multi-hop walk over typed edges (recursive CTE).
        # CYCLE-SAFE via UNION, not UNION ALL (bug-hunt MEDIUM, 2026-06-17).
        # co_changed edges are stored bidirectionally (record_co_change inserts
        # both (a,b) and (b,a)), so the files touched in one session form a
        # fully-connected bidirectional clique. The old `UNION ALL` walk enumerated
        # EVERY path through that clique — the intermediate row count grew
        # multiplicatively with clique size (a synthetic 60-node clique took ~190 ms)
        # — even though the final `SELECT DISTINCT` deduped the OUTPUT. `hop < 3`
        # already bounds the recursion DEPTH (so it always terminates); switching to
        # `UNION` dedups the per-hop rows so the clique can't multiply within a level.
        # Measured on this repo's own index: byte-for-byte identical output to the
        # old query on all 30 busiest nodes, ~3x faster (6.5 ms -> 2.2 ms worst case).
        # This mirrors the deliberate `UNION` choice already made in the sibling
        # supersedes-chain CTE ("UNION (not UNION ALL) dedups, so a cycle terminates").
        # NB: a path-visited-set guard was tried and rejected — it was both SLOWER
        # (string LIKE per edge) and changed output (it dropped real relations that
        # were only reachable via a cyclic return through the clique).
        relation = self.db.execute(
            """
            WITH RECURSIVE walk(src, dst, kind, sha, verified, hop) AS (
                SELECT src_node, dst_node, kind, stamped_at_sha, verified, 1
                  FROM edges WHERE src_node = ?
                UNION
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
        """Drop the cached BM25 corpus stats AND the cached resolver — called by every
        anchor/node mutation. Both are derived from the node/anchor tables, so any stamp,
        reindex, or edge change must rebuild them on next use (correctness over the speedup)."""
        self._corpus_stats = None
        self._resolver = None

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
        # Batch the df lookup in 999-safe chunks — a huge paste (a whole file's worth of
        # unique tokens) would otherwise bind one '?' per token in a single IN and trip
        # SQLite's SQLITE_MAX_VARIABLE_NUMBER ("too many SQL variables"). The sibling lens
        # query already batches at 500 for exactly this reason. (P2 bug-hunt 2026-06-15.)
        toks_list = list(toks)
        df: dict[str, int] = {}
        for i in range(0, len(toks_list), 500):
            batch = toks_list[i:i + 500]
            ph = ",".join("?" * len(batch))
            df.update(self.db.execute(
                f"SELECT a.term, COUNT(na.node_id) FROM anchors a "
                f"JOIN node_anchors na ON na.anchor_id = a.id "
                f"WHERE a.term IN ({ph}) GROUP BY a.term",
                batch,
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
        """Collapse genuine duplicates — keep the first (best-ranked).

        Knowledge rows (commits/lessons) legitimately duplicate by (title, file):
        git history carries a merge + the direct commit with an identical pair, so
        collapsing on (title, file) is right. Code rows do NOT: one file routinely
        defines several DISTINCT symbols with the same name (two `__init__`s, a
        `render`/`handler`/`forward` per class), each a separate node — collapsing
        them on (title, file) silently drops real, different code locations. So for
        code rows the identity is the symbol's own line (its node), not its name."""
        seen: set[tuple] = set()
        out: list[dict] = []
        for it in items:
            if it.get("symbol") is not None or it.get("line") is not None:
                # code-symbol row: distinct symbols at distinct lines are distinct
                key = ("code", it.get("file"), it.get("symbol"), it.get("line"),
                       it.get("node_id"))
            else:
                # knowledge row: a merge + direct commit legitimately duplicate
                key = ("knowledge", it.get("title"), it.get("file"))
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

    def _landmines(self, file_path: str | None, *, limit: int = 10) -> list[dict]:
        """Landmines on this file — the lessons/decisions/commits that `warns_about` its
        code. This is the CONSCIENCE signal of the deviation push (arrow 2): a past mistake
        marked here surfaces UNPROMPTED in the pre-edit briefing, so an AI is warned BEFORE
        it repeats it — not only when it thinks to ask (recall() already serves these on a
        PULL; brief() left them out, so the push never fired — dogfood gap 2026-06-15).

        The mirror of _blast_radius, but over `warns_about` edges pointing INTO this file's
        nodes; what I surface is the WARNING SOURCE (the lesson), not the warned-about code.
        A warning that lives in another file (or is pinned to none) still fires here — that
        is exactly the case `why` (file_path-pinned only) cannot reach. Read-only, model-free,
        fleet-safe (a pure SELECT); ordered by importance then recency, drift carried so a
        stale warning is flagged rather than trusted blindly.

        Resolves a landmine to this file TWO ways (adversarial sweep 2026-06-15 found the
        symbol path was a silent false-negative — `warns_about -> <symbol>`, the documented
        way to mark one, created an orphan node keyed by the symbol name that the path-only
        query missed): by file PATH (the target pinned to this file) OR by SYMBOL (the target
        names a symbol this file defines). A warning whose source was SUPERSEDED is dropped —
        a landmine must not fire after the lesson that set it was replaced."""
        if not file_path:
            return []
        rel = file_path.replace("\\", "/")
        rows = self.db.execute(
            """
            WITH file_syms(name) AS (
                SELECT LOWER(symbol) FROM nodes
                 WHERE REPLACE(file_path,'\\','/')=? AND symbol IS NOT NULL
                UNION
                SELECT LOWER(title) FROM nodes
                 WHERE REPLACE(file_path,'\\','/')=? AND kind='code-symbol'
            )
            SELECT ns.id, ns.kind, ns.title, ns.body, ns.stamped_at_sha, ns.importance
              FROM nodes nd
              JOIN edges e ON e.dst_node = nd.id
              JOIN nodes ns ON ns.id = e.src_node
             WHERE e.kind = 'warns_about'
               AND ( REPLACE(nd.file_path,'\\','/') = ?
                     OR LOWER(nd.title) IN (SELECT name FROM file_syms)
                     OR LOWER(nd.symbol) IN (SELECT name FROM file_syms) )
               AND NOT EXISTS (
                     SELECT 1 FROM edges sup
                      WHERE sup.dst_node = ns.id AND sup.kind = 'supersedes')
             GROUP BY ns.id
             ORDER BY ns.importance DESC, ns.id DESC
             LIMIT ?
            """,
            (rel, rel, rel, limit),
        ).fetchall()
        drifts = self._node_drifts([r["id"] for r in rows])  # one query, not per-landmine
        return [
            {
                "node_id": r["id"], "kind": r["kind"], "title": r["title"],
                "why": (r["body"] or "").splitlines()[0] if r["body"] else "",
                "sha": (r["stamped_at_sha"] or "")[:7],
                "drift": drifts.get(r["id"]),
            }
            for r in rows
        ]

    def _no_precedent(self, situation: str, reason: str, t0: float,
                      *, latency_us: int | None = None) -> dict[str, Any]:
        """The shaped empty precedent result (mirrors _silenced for recall) — never an error,
        so a caller can always read `.precedents`."""
        return {
            "situation": situation,
            "silenced": True,
            "reason": reason,
            "latency_us": latency_us if latency_us is not None
                          else round((time.perf_counter() - t0) * 1_000_000),
            "precedents": [],
        }

    def _precedent_outcome(self, node_id: int, score: float, n) -> dict[str, Any]:
        """Attach a precedent's FATE — the thing that makes it a precedent and not just a
        search hit: the decision that superseded it, whether it was promoted to a landmine,
        and its drift.

        supersedes is (newer)->(older) [see _landmines], so we walk dst=this BACKWARDS to the
        head of the chain — the decision that governs NOW. UNION (not UNION ALL) dedups, so a
        cycle terminates; depth is capped as defense-in-depth and the deepest (id-tie-broken)
        row is the current rule. Read-only."""
        head = self.db.execute(
            """
            WITH RECURSIVE chain(id, depth) AS (
                SELECT src_node, 1 FROM edges WHERE dst_node=? AND kind='supersedes'
                UNION
                SELECT e.src_node, c.depth+1 FROM edges e JOIN chain c ON e.dst_node=c.id
                 WHERE e.kind='supersedes' AND c.depth < 16
            )
            SELECT ch.id, nd.title FROM chain ch JOIN nodes nd ON nd.id=ch.id
             ORDER BY ch.depth DESC, ch.id DESC LIMIT 1
            """,
            (node_id,),
        ).fetchone()
        superseded_by = {"node_id": head["id"], "title": head["title"]} if head else None
        became_landmine = bool(self.db.execute(
            "SELECT 1 FROM edges WHERE src_node=? AND kind='warns_about' LIMIT 1",
            (node_id,)).fetchone())
        return {
            "node_id": node_id,
            "kind": n["kind"],
            "title": n["title"],
            "what": (n["body"] or "").splitlines()[0] if n["body"] else "",
            "relevance": round(score, 2),
            "importance": round(n["importance"] or 0, 1),
            "sha": (n["stamped_at_sha"] or "")[:7],
            "drift": self._node_drift(node_id),
            "file": (n["file_path"] or "").replace("\\", "/") or None,
            "outcome": "superseded" if superseded_by else "standing",
            "superseded_by": superseded_by,
            "became_landmine": became_landmine,
        }

    def _resolve_to_files(self, target: str) -> set[str]:
        """Resolve an impact target to the file(s) it lives in. Order matters: a KNOWN file path
        wins first (so a root-level 'lib.py' with no slash is still a file, not a symbol); else a
        bare name resolves to the file(s) of the code-symbol(s) that define it (case-insensitive,
        by symbol or title); else a slash-bearing but unknown path is taken best-effort so impact
        can still answer (usually empty). Empty set when nothing matches at all."""
        t = (str(target) if target is not None else "").strip().replace("\\", "/")
        t = t[2:] if t.startswith("./") else t  # sweep #5: './a.py' must resolve like 'a.py'
        t = t.rstrip("/")                         # sweep #5/#19: trailing slash / non-str must not crash or mask
        if not t:
            return set()
        if self.db.execute(
            "SELECT 1 FROM nodes WHERE REPLACE(file_path,'\\','/')=? LIMIT 1", (t,)
        ).fetchone():
            return {t}
        rows = self.db.execute(
            "SELECT DISTINCT REPLACE(file_path,'\\','/') AS f FROM nodes "
            "WHERE kind='code-symbol' AND file_path IS NOT NULL AND file_path != '' "
            "AND (LOWER(symbol)=? OR LOWER(title)=?)",
            (t.lower(), t.lower()),
        ).fetchall()
        files = {r["f"] for r in rows if r["f"]}
        if files:
            return files
        return {t} if "/" in t else set()

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
        power_run=None, base_sha=None, author=None, predicate=None, outcome=None,
        visibility="team",
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO nodes"
            "(kind,title,body,facets,file_path,symbol,line,stamped_at_sha,origin,power_run,base_sha,author,predicate,outcome,visibility) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, title, body, ",".join(facets), file_path, symbol, line, sha, origin,
             power_run, base_sha, author, predicate, outcome, visibility),
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

    def record_co_change(self, files, *, kind: str = "co_changed", sha: str = "session",
                         rerank: bool = True) -> int:
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
        if added and rerank:
            # New relations changed the causal graph -> re-rank (model-free, ADR-016).
            # This is the 'heal while coding' payoff: the session's edits immediately
            # lift the importance of the files they wired together. rerank=False during
            # init()/update_incremental(), which re-rank ONCE at the end — otherwise a
            # full PageRank runs per commit (~60×), every intermediate result throwaway.
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

    def _log(self, query, node_id, score, surfaced, latency_us, consumer, kind="recall",
             resp_chars: int = 0) -> None:
        # `kind` (v7) tags the read-path action — recall / brief / explain / stamp — so the
        # live activity console can stream a usage proof. `resp_chars` (v10, workstream E) is the
        # emitted response size; default 0 leaves every existing caller untouched. Best-effort: an
        # OperationalError on a pre-migration on-disk DB must never break the actual call.
        try:
            self.db.execute(
                "INSERT INTO access_log(query,node_id,score,surfaced,latency_us,consumer,kind,resp_chars) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (query, node_id, float(score), surfaced, latency_us, consumer, kind, int(resp_chars)),
            )
            self.db.commit()
        except sqlite3.OperationalError:
            pass

    def _record_served(self, served_kind: str, consumer: str, resp_chars: int) -> None:
        """Record that a response of `resp_chars` chars was EMITTED to `consumer` for `served_kind`
        — the context-tax measurement (workstream E) — WITHOUT re-running a query/score. Stored as
        a DISTINCT kind='served' row (the tool name in `query`) so it never inflates the loop-event
        counts the receipt's MEASURED block reports. A thin best-effort INSERT sibling of _log,
        recorded at the consumer boundary (mcp.py/cli.py) so the engine stays text-free (ADR-014)."""
        try:
            self.db.execute(
                "INSERT INTO access_log(query,node_id,score,surfaced,latency_us,consumer,kind,resp_chars) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (served_kind, None, 0.0, 0, 0, consumer, "served", int(resp_chars)),
            )
            self.db.commit()
        except sqlite3.OperationalError:
            pass

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

    def receipt(self, *, window_days: int = 14, interactive_only: bool = True) -> dict[str, Any]:
        """Money-receipt (workstream C) — the loop recall was IN over a rolling window, in HONEST
        MEASURED units straight from access_log rows. COUNTS-ONLY: no token/$ figure ships (the
        modeled block is a deliberate later increment — the measured/modeled wall is structural,
        ADR-041). Pure SELECT, model-free, 0 tokens.

        `interactive_only` (default) excludes machine/background traffic (consumer 'commit'/'state')
        but KEEPS 'hook' — a hook recall IS recall helping the agent. `briefed_edits` counts the
        ack event (the highest-signal 'briefed before editing' proof); distinct files come from the
        ack's query STRING, since cmd_ack logs node_id=None (a COUNT DISTINCT node_id would collapse
        every ack into one NULL bucket)."""
        where = "ts >= strftime('%s','now',?)"
        params: list[Any] = [f"-{int(max(1, window_days))} days"]
        if interactive_only:
            where += " AND consumer NOT IN ('commit','state')"
        rows = self.db.execute(
            f"SELECT kind, consumer, query, surfaced FROM access_log WHERE {where}", params,
        ).fetchall()
        _READ_KINDS = ("recall", "brief", "resolve", "precedent", "impact", "push",
                       "callers", "callees", "dead_code", "untested", "cycles")
        per_kind: dict[str, int] = {}
        briefed_files: set[str] = set()
        briefed_edits = recall_calls = surfaced_calls = 0
        for r in rows:
            k = r["kind"] or "recall"
            if k == "served":
                continue  # workstream E size-measurement rows are NOT loop events — see 'emitted'
            per_kind[k] = per_kind.get(k, 0) + 1
            if k == "ack":
                briefed_edits += 1
                if r["query"]:
                    briefed_files.add((r["query"] or "").replace("\\", "/"))
            if k in _READ_KINDS:
                recall_calls += 1
                if r["surfaced"]:  # of the calls recall was consulted on, how many had a hit
                    surfaced_calls += 1
        # workstream E: per-consumer EMITTED response size (the context tax, MEASURED in chars).
        # Sums resp_chars over rows that carry it — the 'served' MCP rows AND the threaded 'state'
        # block row — so it captures what entered context. Recall-ABSOLUTE only (a single arm, no
        # comparison-baseline arm, no 2-arm %). Tokens are derived at the CLI boundary (the arms.py
        # contract self-labels which tokenizer); the engine stays text/tokenizer-free (ADR-014).
        emit_rows = self.db.execute(
            "SELECT consumer, COUNT(*) AS serves, COALESCE(SUM(resp_chars),0) AS chars "
            "FROM access_log WHERE ts >= strftime('%s','now',?) AND resp_chars > 0 "
            "GROUP BY consumer", [params[0]],
        ).fetchall()
        emitted = {(r["consumer"] or "?"): {"serves": r["serves"], "chars": r["chars"]}
                   for r in emit_rows}
        return {
            "window_days": window_days,
            "interactive_only": interactive_only,
            # MEASURED only. NO 'modeled' key in the counts-only ship — a token/$ figure may live
            # ONLY under a future receipt['modeled'] (the wall a drift-guard enforces).
            "measured": {
                "briefed_edits": briefed_edits,
                "distinct_files_briefed": len(briefed_files),
                "recall_calls": recall_calls,
                "surfaced_calls": surfaced_calls,
                "per_kind": per_kind,
                "total_events": len(rows),
            },
            # workstream E — per-consumer emitted CHARS (recall-absolute; tokens derived at the CLI).
            "emitted": emitted,
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

# A title longer than this is a paragraph, not a headline — split it (see _split_headline).
_HEADLINE_MAX = 120
_HEADLINE_MIN = 18   # a headline shorter than this (e.g. "P1 bug fixed:") is a stub, not a headline
# sentence boundary: a real end-of-sentence ('. ' / '! ' / '? '). A colon is NOT a boundary —
# it usually INTRODUCES the content ("P1 bug fixed: <the actual point>"), so cutting there
# leaves a stub headline. We split only at genuine sentence ends.
_HEADLINE_BREAK = re.compile(r"(?<=[.!?])\s+")


def _split_headline(title: str) -> tuple[str, str]:
    """Split a too-long `title` into (headline, rest) at its first SENTENCE end, so a stamp
    whose whole summary landed in `title` reads as headline + detail instead of one wall of
    text repeated across the story chain. Pure string work, no interpretation: we only CUT at
    a sentence boundary, never rephrase. Returns (title, "") when there is no clean break that
    yields a sane headline — a single long sentence stays whole (better one long headline than
    a cut mid-thought, or a stub like "P1 bug fixed:")."""
    t = (title or "").strip()
    # scan boundaries left-to-right; take the FIRST that leaves a headline of sane length.
    for m in _HEADLINE_BREAK.finditer(t):
        head = t[: m.start()].strip()
        if len(head) < _HEADLINE_MIN:
            continue                       # too short -> a stub; keep scanning
        if len(head) > _HEADLINE_MAX * 2:
            break                          # first sentence is itself a paragraph -> don't split
        return head, t[m.end():].strip()
    return t, ""


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

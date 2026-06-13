"""Stage 2 — the honest drift traffic-light (SHA-diff against the working tree).

Every node is pinned to a `stamped_at_sha` + a `file_path`. Knowledge drifts when
the file it describes changes *after* the stamp. This module answers that question
git-token-free (stdlib + plain `git`, no model), two levels deep:

  GREEN  🟢  fresh — the file has not changed since the stamp.
  YELLOW 🟡  committed-drift — the file saw new commits after stamped_at_sha.
  ORANGE 🟠  uncommitted-edit — the file has open changes in the working tree.

The result is written back onto the node's edges as `verified` (1 = fresh, 0 =
drifted), so recall()'s existing relation walk surfaces it and the CLI ampel —
which was wired but structurally blind (nothing ever set verified=0) — finally
tells the truth. 🟡/🟠 now mean "we KNOW it drifted", never "we have no SHA"
(exactly the semantics _is_fresh() already documents).

Pure read-time work, no LLM, no tokens. Designed to run on demand (`recall freshen`)
or from a hook after a commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

# Drift levels, ordered worst-last so max() picks the loudest signal for a node.
FRESH = "fresh"            # 🟢 no change since the stamp
COMMITTED = "committed"    # 🟡 new commits touched the file after the stamp
UNCOMMITTED = "uncommitted"  # 🟠 open edits in the working tree

_LEVEL_RANK = {FRESH: 0, COMMITTED: 1, UNCOMMITTED: 2}

# Only CLAIM-BEARING knowledge can drift (Owner crux, 2026-06-09). A drift light is a
# promise that "this STATEMENT may no longer match the code" — so it only makes sense for
# nodes that make a statement which can become wrong:
#   lesson/decision/task/plan  → curated knowledge, CAN go stale → classified
#   code-symbol                → the auto-regenerated code map; re-index rebuilds it, so
#                                "drift" on it is meaningless noise → never flagged
#   commit                     → an immutable historical fact ("this commit happened");
#                                a later commit to the same file can't make it false → never flagged
# Measured on this repo: of 55 "committed-drift" nodes, 38 were commits + 17 code-symbols and
# ZERO were claim-bearing — i.e. the entire drift count was false-alarm. Restricting to
# claim-bearing kinds is what makes the traffic-light honest instead of alarm-fatiguing.
CLAIM_BEARING_KINDS = frozenset({"lesson", "decision", "task", "plan"})


def _git(repo: Path, *args: str) -> tuple[str, int]:
    """Run git in `repo`, returning (stdout, returncode). 127 = git absent.

    NOTE: stdout is returned RAW (only a trailing newline trimmed), never
    str.strip()'d — porcelain status lines begin with a significant leading space
    (' M path' for an unstaged change). A global strip ate that space and shifted
    the path parse by one char ('path' -> 'ath'), silently dropping dirty files
    from the 🟠 set. Callers that want trimming do it per-line themselves.
    """
    try:
        # core.quotepath=false: keep non-ASCII paths as raw UTF-8 so an edited
        # file like 'Grüße.py' is matched (and shows 🟠 drift) instead of being
        # silently reported fresh — git's default C-quoting mangles the key.
        p = subprocess.run(
            ["git", "-C", str(repo), "-c", "core.quotepath=false", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return p.stdout.rstrip("\n"), p.returncode
    except OSError:
        return "", 127


def _file_is_dirty(repo: Path, rel: str) -> bool:
    """True if `rel` has uncommitted changes (staged or unstaged) in the work tree."""
    out, rc = _git(repo, "status", "--porcelain", "--", rel)
    return rc == 0 and bool(out)


def _commits_since(repo: Path, rel: str, stamped_sha: str) -> bool:
    """True if any commit touched `rel` strictly after `stamped_sha`.

    `git log <sha>..HEAD -- <file>` lists exactly the commits that changed the
    file since the stamp. A non-empty result means the pinned knowledge predates
    real changes to its file → committed-drift. An unknown SHA (rewritten history,
    shortened-but-ambiguous) makes git error → treated as 'cannot prove drift' =
    fresh, never a false alarm.
    """
    if not stamped_sha:
        return False
    out, rc = _git(repo, "log", f"{stamped_sha}..HEAD", "--oneline", "--", rel)
    return rc == 0 and bool(out)


def file_drift(repo: Path, rel: str, stamped_sha: str | None) -> str:
    """Classify one file's drift against its stamp. Worst signal wins.

    Uncommitted edits are the loudest (🟠) — they're live, this second. Committed
    drift (🟡) is the historical signal. No signal → fresh (🟢).

    Single-file path (one git call each) — used for ad-hoc checks and tests. The
    bulk freshen() uses RepoState below to answer the same questions for thousands
    of files from THREE global git reads instead of 2×N subprocesses.
    """
    if not rel:
        return FRESH  # nothing pinned to a file → no drift to measure
    if not (repo / rel).exists():
        # A pinned file that's gone from disk is drift — but it was *removed in a
        # commit* (history rewrite, Vite→Next migration deleting index.html), not
        # "edited this second". So it's committed-drift 🟡, not the live-edit 🟠
        # signal — keeping 🟠 strictly for open changes in the working tree.
        return COMMITTED
    if _file_is_dirty(repo, rel):
        return UNCOMMITTED
    if stamped_sha and _commits_since(repo, rel, stamped_sha):
        return COMMITTED
    return FRESH


class RepoState:
    """All the git facts freshen() needs, gathered in THREE reads for the whole repo.

    The per-file path costs one `git status` + one `git log` PER FILE — on a 7k-node
    / 1k-file repo that's ~2000 Windows subprocess spawns (dogfooding a 1k-file production repo:
    85 s, all of it process-start overhead, user-CPU 0.04 s). This gathers the same
    facts once:
      1. `git status --porcelain`            -> the set of dirty work-tree paths (🟠)
      2. `git log --format=%H --name-only`   -> per-file commit order (for 🟡)
      3. (disk existence is a plain stat)    -> deleted pinned files (🟡)

    Then drift_of() answers each node in-memory, zero further subprocesses.
    """

    def __init__(self, repo: Path):
        self.repo = repo
        self.has_git = (repo / ".git").exists()
        self._dirty: set[str] = set()
        # file -> ordered list of SHAs that touched it (NEWEST FIRST, matching git log).
        self._touch: dict[str, list[str]] = {}
        # global commit order, newest first; index in this list = "how recent".
        self._order: dict[str, int] = {}
        if self.has_git:
            self._load_dirty()
            self._load_history()

    def _load_dirty(self) -> None:
        out, rc = _git(self.repo, "status", "--porcelain")
        if rc != 0:
            return
        for line in out.splitlines():
            # porcelain: 2 status chars, a space, then the path (with optional "orig -> new")
            path = line[3:].strip()
            if " -> " in path:  # rename: the new path is what's on disk now
                path = path.split(" -> ", 1)[1]
            if path:
                self._dirty.add(path.strip('"'))

    def _load_history(self) -> None:
        # One walk of history: a %H header line, then the files that commit changed.
        out, rc = _git(self.repo, "log", "--format=%H", "--name-only")
        if rc != 0:
            return
        cur: str | None = None
        rank = 0
        for line in out.splitlines():
            if _looks_like_full_sha(line):
                cur = line.strip()
                self._order[cur] = rank
                self._order[cur[:7]] = rank  # short-sha lookups hit the same rank
                rank += 1
            elif line.strip() and cur is not None:
                self._touch.setdefault(line.strip(), []).append(cur)

    def drift_of(self, rel: str, stamped_sha: str | None) -> str:
        """Same classification as file_drift(), answered from the cached reads."""
        if not rel:
            return FRESH
        if not (self.repo / rel).exists():
            return COMMITTED  # deleted-in-a-commit, not a live edit (see file_drift)
        if rel in self._dirty:
            return UNCOMMITTED
        if stamped_sha and self._committed_since(rel, stamped_sha):
            return COMMITTED
        return FRESH

    def _committed_since(self, rel: str, stamped_sha: str) -> bool:
        """True if a commit touched `rel` strictly newer than `stamped_sha`.

        Newer = a smaller rank in the newest-first history order. If the stamp SHA
        isn't in history (rewritten/ambiguous) we can't prove drift → fresh, never
        a false alarm (mirrors the single-file path's git-error handling).
        """
        return self.commits_since(rel, stamped_sha) > 0

    def commits_since(self, rel: str, stamped_sha: str | None) -> int:
        """How many commits touched `rel` strictly newer than `stamped_sha` (Wave E).

        Where _committed_since() answers yes/no for the drift ampel, the stale-decision
        alarm wants the MAGNITUDE — a decision whose code moved twenty times since it was
        written is more suspect than one that moved once. Same newest-first rank logic; an
        unknown/None stamp SHA returns 0 (can't prove drift, never a false alarm)."""
        if not stamped_sha:
            return 0
        stamp_rank = self._order.get(stamped_sha)
        if stamp_rank is None and len(stamped_sha) >= 7:
            stamp_rank = self._order.get(stamped_sha[:7])
        if stamp_rank is None:
            return 0
        return sum(1 for sha in self._touch.get(rel, ())
                   if self._order.get(sha, 1 << 30) < stamp_rank)


def _looks_like_full_sha(line: str) -> bool:
    t = line.strip()
    return len(t) == 40 and all(c in "0123456789abcdef" for c in t.lower())


def freshen(index, repo: str | Path) -> dict[str, Any]:
    """Walk every pinned node, classify drift, write `verified` onto its edges.

    Returns a summary {checked, fresh, committed, uncommitted, no_git}. The map of
    node_id -> level is also stamped into meta so the CLI/dashboard can show counts
    without re-running git. Token-free, deterministic, safe to re-run any time.
    """
    repo = Path(repo)
    has_git = (repo / ".git").exists()
    summary = {"checked": 0, FRESH: 0, COMMITTED: 0, UNCOMMITTED: 0, "no_git": not has_git}

    # Only nodes pinned to a real file participate — a lesson with no file_path has
    # nothing to diff against, so its freshness stays unknown-but-shown-fresh. We also pull
    # `kind`: only claim-bearing kinds (lesson/decision/task/plan) can actually drift; the
    # auto-regenerated code map and immutable commit facts are forced FRESH (see
    # CLAIM_BEARING_KINDS) so the light measures real staleness, not commit noise.
    rows = index.db.execute(
        "SELECT id, file_path, stamped_at_sha, kind FROM nodes "
        "WHERE file_path IS NOT NULL AND file_path != ''"
    ).fetchall()

    # Gather every git fact once (3 reads for the whole repo), then answer each node
    # in-memory. Plus a per-(rel, sha) cache so the 36 symbol nodes in one
    # EditorShell.tsx share their answer. Dogfooding a 1k-file production repo drove this: per-file
    # git calls were 85% redundant AND the remaining ~2000 subprocess spawns cost 85 s
    # of pure process-start overhead on Windows — RepoState collapses both.
    state = RepoState(repo)
    drift_cache: dict[tuple[str, str | None], str] = {}
    fresh_edges: list[int] = []
    stale_edges: list[int] = []
    drift_by_node: dict[int, str] = {}
    for node_id, rel, sha, kind in rows:
        if kind in CLAIM_BEARING_KINDS:
            # Curated knowledge: the full traffic-light (🟢/🟡/🟠) diffed against the stamp.
            key = (rel, sha)
            level = drift_cache.get(key)
            if level is None:
                level = state.drift_of(rel, sha) if has_git else (
                    COMMITTED if rel and not (repo / rel).exists() else FRESH
                )
                drift_cache[key] = level
        else:
            # Non-claim-bearing nodes (the auto-regenerated code map, immutable commit
            # facts) can't go COMMITTED-stale — a re-index rebuilds them and a commit is
            # history. So we SUPPRESS 🟡. But we KEEP the live 🟠 uncommitted-edit signal:
            # if the file has open working-tree changes RIGHT NOW, the briefing must still
            # warn before you edit it (that warning is exactly what brief() exists for).
            # The fix for the over-eager filter that blinded brief() on 108/110 code files.
            key = (rel, "\0dirty")  # namespaced: 🟠-only, never collides with a real sha key
            level = drift_cache.get(key)
            if level is None:
                full = state.drift_of(rel, None) if has_git else FRESH
                level = UNCOMMITTED if full == UNCOMMITTED else FRESH
                drift_cache[key] = level
        drift_by_node[node_id] = level
        summary["checked"] += 1
        summary[level] += 1
        # verified=1 only when fresh; any drift (🟡/🟠) flips outgoing edges to 0
        # so recall()'s relation walk renders the stale flag honestly.
        (fresh_edges if level == FRESH else stale_edges).append(node_id)

    # One bulk UPDATE per verified-state instead of one per node — fewer round trips.
    _bulk_set_verified(index, fresh_edges, 1)
    _bulk_set_verified(index, stale_edges, 0)

    _store_drift_meta(index, drift_by_node)
    index.db.commit()
    return summary


def _bulk_set_verified(index, node_ids: list[int], verified: int) -> None:
    """Set edges.verified for all the given src nodes in chunked IN-clauses.

    SQLite caps a statement at ~999 bound variables, so we chunk. One UPDATE per
    chunk beats one UPDATE per node on a 7k-node index."""
    CHUNK = 900
    for i in range(0, len(node_ids), CHUNK):
        batch = node_ids[i : i + CHUNK]
        placeholders = ",".join("?" * len(batch))
        index.db.execute(
            f"UPDATE edges SET verified=? WHERE src_node IN ({placeholders})",
            (verified, *batch),
        )


def _store_drift_meta(index, drift_by_node: dict[int, str]) -> None:
    """Persist the per-node drift level into meta as 'drift:<node_id>' so the
    dashboard (and `recall stats`) can read freshness without invoking git again.
    Old drift keys are cleared first so a re-freshen never leaves stale levels."""
    index.db.execute("DELETE FROM meta WHERE key LIKE 'drift:%'")
    index.db.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        [(f"drift:{nid}", level) for nid, level in drift_by_node.items()],
    )


def drift_counts(index) -> dict[str, int]:
    """Read the last freshen()'s per-node drift levels back from meta, as counts."""
    counts = {FRESH: 0, COMMITTED: 0, UNCOMMITTED: 0}
    for (level,) in index.db.execute(
        "SELECT value FROM meta WHERE key LIKE 'drift:%'"
    ).fetchall():
        if level in counts:
            counts[level] += 1
    return counts

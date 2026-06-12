"""The local dashboard — `recall dashboard`.

A tiny pure-stdlib web app (http.server, no FastAPI, no npm, no Node) that reads
the project's .mind/index.db READ-ONLY and serves:

  GET  /              -> the single-page dashboard (recall/dashboard.html)
  GET  /api/data      -> a JSON snapshot of the whole index (live, from the DB)
  GET  /api/file      -> one repo file (jailed, capped) for the code viewer
  GET  /api/diff      -> `git diff <sha>..HEAD` for the before/after story step
  GET  /api/connection -> the current AI connection (names only, never the key)
  GET  /api/pulse     -> a cheap heartbeat (HEAD, tree-hash, counts) — LIVE polling
  GET  /api/hook      -> are the git commit hooks installed? (post-commit + pre-commit)
  GET  /api/mcp       -> is recall registered as the project MCP server + last use (ADR-029)
  GET  /api/license   -> the stored account license payload, decoded (ADR-030; never the raw token)
  GET  /api/guide     -> the shipped getting-started guide, verbatim (?download=1 saves)
  GET  /api/about     -> product facts: name, version, license id, copyright
  GET  /api/legal     -> the product's legal full texts (?doc=license|commercial)
  GET  /api/brief     -> the pre-edit briefing for one file (Wave A, ADR-018)
  GET  /api/contested -> uncertainty hotspots, churn × entanglement (Wave B, ADR-019)
  GET  /api/stale     -> decisions whose code moved on since the stamp (Wave E, ADR-022)
  GET  /api/onboarding-> "explain me this repo" orientation (Wave C, ADR-020)
  POST /api/connect   -> connect / disconnect the AI (writes connect.json, ADR-012)
  POST /api/hook      -> install / remove a commit hook, ?which=pre|post (local only)
  POST /api/mcp       -> register / unregister recall in .mcp.json (local only, ADR-029)
  POST /api/license   -> save / clear the account license token (local only, ADR-030)

A background watcher (LIVE mode, on by default) polls HEAD and auto-indexes when a
new commit lands, so fresh commits become knowledge by themselves — the read path
stays LLM-free (it only ever calls init()/freshen(), never a model; Seam-Guard
ADR-014). It never indexes while a Power run holds the index (_POWER_LOCK).

It runs on localhost. The code never leaves the machine — the server only reads
the local SQLite the engine already built. This is the "small app you install"
that makes the wiki visible: `pip install` it, `recall init`, `recall dashboard`.

The read path is pure: zero tokens, zero model, offline. The ONLY write is
/api/connect, which records which AI the user chose (the connect-modal, ADR-012) —
it stores only the env-var NAME of any key, never the key itself, exactly like
`recall connect`. That write is hardened against DNS-rebinding: it accepts only
loopback clients and same-origin requests (see _is_local / _same_origin).

Git timestamps are read with one `git log` (token-free) so the wiki has a real
timeline.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from recall.engine import Index
from recall.freshness import drift_counts
from recall.tasks import looks_done, parse_subtasks

_HERE = Path(__file__).resolve().parent
_HTML = _HERE / "dashboard.html"
_VENDOR = _HERE / "vendor"  # bundled static assets (highlight.js + theme), shipped offline
_VENDOR_TYPES = {".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}

# How much we surface to the page. Generous but bounded so a 7k-node repo stays snappy.
_MAX_LESSONS = 200
_MAX_CODE_FILES = 60
_MAX_FILE_BYTES = 1_000_000  # 1 MB cap for the inline code viewer
_GS = "\x1f"  # field separator unlikely to appear in git output (vs a fragile pipe)


def _git(repo: Path, *args: str) -> str:
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return p.stdout if p.returncode == 0 else ""
    except OSError:
        return ""


def _commit_times(repo: Path) -> dict[str, int]:
    """short-sha -> author unix time, from one `git log` (token-free timeline)."""
    out = _git(repo, "log", "--format=%h %at", "-n", "4000")
    times: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            times[parts[0]] = int(parts[1])
    return times


def _file_times(repo: Path) -> dict[str, int]:
    """rel-path -> the unix time it was LAST committed, from one `git log --name-only`.

    Lets a doc-lesson (pinned to a .md file, no commit SHA of its own) show WHEN that
    knowledge actually last changed, instead of the index build time (the '5h ago' bug).
    One git read for the whole repo; newest-first walk so the first time we see a path is
    its most recent commit."""
    out = _git(repo, "log", "--format=%x00%at", "--name-only", "-n", "4000")
    times: dict[str, int] = {}
    cur_t: int | None = None
    for line in out.splitlines():
        if line.startswith("\x00"):
            ts = line[1:].strip()
            cur_t = int(ts) if ts.isdigit() else None
        elif line.strip() and cur_t is not None:
            rel = line.strip().replace("\\", "/")
            if rel not in times:  # first (newest) wins
                times[rel] = cur_t
    return times


def _file_authored(repo: Path) -> dict[str, dict]:
    """rel-path -> {created_ts, created_by, last_ts, last_by} from one `git log`.

    The honest "who made this + when" for a file (e.g. a task file): created = the FIRST
    commit that touched it (author + time), last = the most recent. Used to show a task's
    creation and — when it is done/dropped — its completion, straight from git, model-free.
    One newest-first walk: the first time we see a path is its last commit, the last time
    is its creation."""
    out = _git(repo, "log", "--format=%x00%at%x1f%an", "--name-only", "-n", "4000")
    info: dict[str, dict] = {}
    cur_t: int | None = None
    cur_a: str = ""
    for line in out.splitlines():
        if line.startswith("\x00"):
            rest = line[1:]
            ts, _, an = rest.partition("\x1f")
            cur_t = int(ts) if ts.strip().isdigit() else None
            cur_a = an.strip()
        elif line.strip() and cur_t is not None:
            rel = line.strip().replace("\\", "/")
            if rel not in info:  # first sighting = newest commit
                info[rel] = {"last_ts": cur_t, "last_by": cur_a,
                             "created_ts": cur_t, "created_by": cur_a}
            else:                # keep overwriting created with the older commit
                info[rel]["created_ts"] = cur_t
                info[rel]["created_by"] = cur_a
    return info


def git_snapshot(repo: Path) -> dict:
    """Branches, recent rich commit log, and the file tree at HEAD — token-free.

    Five read-only plumbing reads (~0.1s on a normal repo). Uses a unit-separator
    delimiter so a commit subject containing '|' can't break parsing. Degrades to
    empty lists if git is absent (the dashboard still works without a Git tab)."""
    fmt_b = (f"%(refname:short){_GS}%(objectname:short){_GS}%(committerdate:unix)"
             f"{_GS}%(upstream:short){_GS}%(upstream:track)")
    branches = []
    for ln in _git(repo, "for-each-ref", "--sort=-committerdate", f"--format={fmt_b}",
                   "refs/heads").splitlines():
        f = ln.split(_GS)
        if len(f) == 5:
            branches.append({
                "name": f[0], "sha": f[1],
                "ts": int(f[2]) if f[2].isdigit() else None,
                "upstream": f[3] or None, "track": f[4] or None,
            })

    commits = []
    for ln in _git(repo, "log", f"--format=%h{_GS}%an{_GS}%at{_GS}%s{_GS}%D",
                   "-n", "100").splitlines():
        f = ln.split(_GS)
        if len(f) == 5:
            commits.append({
                "sha": f[0], "author": f[1],
                "ts": int(f[2]) if f[2].isdigit() else None,
                "subject": f[3], "decoration": f[4],
            })

    tree = [p for p in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines() if p]
    return {"branches": branches, "commits": commits, "tree": tree}


def pulse(repo: Path, idx_path: Path) -> dict:
    """A tiny, cheap heartbeat the page polls every few seconds — the LIVE signal.

    This is deliberately MUCH lighter than build_snapshot(): just enough for the page
    to notice 'something changed' and decide whether to reload + toast. Three fast git
    reads + two cheap COUNTs, no freshen, no graph, no per-node work. If any field
    changed since the last pulse the page reloads /api/data and shows the delta.

      head        HEAD short-sha (new commit -> changed)
      tree_hash   `git write-tree`-ish digest of the working tree (uncommitted edits)
      indexed_at  index file mtime (a re-index -> changed) -> drives 'last update'
      nodes/lessons/commits  counts, so the toast can say '+M lessons, +N commits'
    """
    head = _git(repo, "rev-parse", "--short", "HEAD").strip() or "—"
    # status digest: cheap proxy for 'working tree changed' without write-tree side
    # effects. porcelain output is stable for a given dirty-state; hash it small.
    status = _git(repo, "status", "--porcelain")
    tree_hash = str(hash(status) & 0xFFFFFFFF) if status else "clean"
    # commit count is cheap and monotonic — perfect for a '+N commits' delta.
    rc = _git(repo, "rev-list", "--count", "HEAD").strip()
    commits = int(rc) if rc.isdigit() else 0

    nodes = lessons = 0
    if idx_path.exists():
        try:
            idx = Index.open(idx_path, repo=repo)
            try:
                r = idx.db.execute("SELECT count(*) FROM nodes").fetchone()
                nodes = r[0] if r else 0
                r = idx.db.execute("SELECT count(*) FROM nodes WHERE kind='lesson'").fetchone()
                lessons = r[0] if r else 0
            finally:
                idx.db.close()
        except Exception:
            pass  # a busy/locked DB must never break the heartbeat
    return {
        "head": head, "tree_hash": tree_hash, "commits": commits,
        "indexed_at": _index_mtime(idx_path), "has_index": idx_path.exists(),
        "nodes": nodes, "lessons": lessons,
        # the auto-index worker reports its last action here so the toast can name it
        "auto": dict(_WATCH_STATE),
    }


import re as _re

# commit scope from a conventional subject: "feat(dashboard): ..." -> "dashboard".
_SCOPE_RE = _re.compile(r"^\s*(?:feat|fix|refactor|perf|chore|docs|test)\s*\(([^)]+)\)")
_TYPE_RE = _re.compile(r"^\s*(feat|fix|refactor|perf|chore|docs|test)\b")


def _feature_of(title: str) -> str | None:
    """Derive a feature/area name from a conventional commit subject — the grouping key
    for the product tree when no explicit feature/plan node exists. 'feat(dashboard): x'
    -> 'dashboard'. Returns None if the subject isn't conventional."""
    m = _SCOPE_RE.match(title or "")
    if m:
        return m.group(1).strip().split(",")[0].strip().lower()
    return None


def build_product_tree(db) -> dict:
    """The living product map (ADR-018), derived from the already-indexed data — so it is
    always current (the code is the single source of truth, ADR-001). Two groupings the UI
    can toggle:
      by_feature — features/areas (explicit feature/plan/task nodes + conventional commit
                   scopes) each with their decisions (ADRs), code files, commits, tasks.
      by_status  — tasks/plans grouped by lifecycle (open/done/dropped/deferred).
    Pure read + arithmetic, model-free (ADR-014)."""
    features: dict[str, dict] = {}

    def feat(name: str) -> dict:
        key = name.strip().lower()
        if key not in features:
            features[key] = {"name": key, "decisions": [], "code": [],
                             "commits": [], "tasks": [], "n": 0}
        return features[key]

    # 1) explicit task/plan/feature nodes -> a feature bucket each, with their status + affects
    for nid, title, facets, fp in db.execute(
        "SELECT id,title,facets,file_path FROM nodes WHERE kind='task'"
    ).fetchall():
        fs = set((facets or "").split(","))
        status = next((s for s in ("done", "dropped", "deferred", "open") if s in fs), "open")
        # name the feature after the task title (short) — it IS a feature/plan
        b = feat(title[:48])
        b["tasks"].append({"id": nid, "title": title, "status": status})
        affects = [r[0].replace("\\", "/") for r in db.execute(
            "SELECT DISTINCT nd.file_path FROM edges e JOIN nodes nd ON nd.id=e.dst_node "
            "WHERE e.src_node=? AND e.kind='relates_to' AND nd.file_path IS NOT NULL", (nid,)
        ).fetchall()]
        for a in affects:
            if a not in [c["file"] for c in b["code"]]:
                b["code"].append({"file": a})

    # 2) commits -> grouped by conventional scope; their files become the feature's code
    for nid, title, fp, sha in db.execute(
        "SELECT id,title,file_path,stamped_at_sha FROM nodes WHERE kind='commit' ORDER BY id DESC"
    ).fetchall():
        name = _feature_of(title)
        if not name:
            continue
        b = feat(name)
        b["commits"].append({"id": nid, "title": title, "sha": (sha or "")[:7]})
        # the commit's main file becomes part of the feature's code surface
        if fp:
            rel = fp.replace("\\", "/")
            if rel and not rel.endswith((".md", ".json", ".lock")) and rel not in [c["file"] for c in b["code"]]:
                b["code"].append({"file": rel})

    # 3) ADR / decision lessons -> attach to the feature whose name appears in the title,
    #    else keep as a top-level 'decisions' area. Cheap substring match.
    decisions = []
    for nid, title in db.execute(
        "SELECT id,title FROM nodes WHERE kind='lesson' AND (title LIKE 'ADR-%' OR title LIKE 'ADR %') "
        "ORDER BY id DESC LIMIT 60"
    ).fetchall():
        placed = False
        low = (title or "").lower()
        for key, b in features.items():
            if key and len(key) >= 4 and key in low:
                b["decisions"].append({"id": nid, "title": title})
                placed = True
                break
        if not placed:
            decisions.append({"id": nid, "title": title})

    # finalise counts + sort (richest features first)
    out = []
    for b in features.values():
        b["n"] = len(b["decisions"]) + len(b["code"]) + len(b["commits"]) + len(b["tasks"])
        # status rollup for the feature: open if any task open, else done if any done
        st = {t["status"] for t in b["tasks"]}
        b["status"] = ("open" if "open" in st else ("done" if "done" in st else
                        ("deferred" if "deferred" in st else None)))
        if b["n"] > 0:
            out.append(b)
    out.sort(key=lambda b: -b["n"])

    # by_status grouping (tasks/plans only — the lifecycle view)
    by_status: dict[str, list] = {"open": [], "done": [], "dropped": [], "deferred": []}
    for nid, title, facets in db.execute(
        "SELECT id,title,facets FROM nodes WHERE kind='task' ORDER BY id DESC"
    ).fetchall():
        fs = set((facets or "").split(","))
        status = next((s for s in ("done", "dropped", "deferred", "open") if s in fs), "open")
        by_status.setdefault(status, []).append({"id": nid, "title": title})

    return {"by_feature": out[:40], "loose_decisions": decisions[:20], "by_status": by_status}


def build_snapshot(idx: Index, repo: Path) -> dict:
    """The whole index as one JSON-able dict — read live from the DB on every request.

    Cheap enough to recompute per request for a normal repo; the dashboard is a local
    single user. Drift is read from the last freshen()'s meta (we also run freshen here
    so the traffic-light is always current when the page loads)."""
    db = idx.db
    try:
        idx.freshen()  # refresh the drift meta so the ampel is current
    except Exception:
        pass  # a locked/again-running freshen must never break the read-only view

    s = idx.stats()
    dc = drift_counts(idx)
    ctimes = _commit_times(repo)
    ftimes = _file_times(repo)  # rel-path -> last-commit time, for doc-lessons without a SHA
    fauth = _file_authored(repo)  # rel-path -> {created/last ts+author}, for task created/closed

    # file -> max importance of its code symbols (ADR-016), so a lesson/task/commit can
    # INHERIT the causal weight of the file it concerns and show the same badge everywhere.
    file_imp: dict[str, float] = {}
    for fp, imp in db.execute(
        "SELECT file_path, MAX(importance) FROM nodes WHERE kind='code-symbol' "
        "AND file_path IS NOT NULL GROUP BY file_path"
    ).fetchall():
        if fp:
            file_imp[fp.replace("\\", "/")] = round(imp or 0, 1)

    def imp_for(fp: str | None) -> float:
        return file_imp.get((fp or "").replace("\\", "/"), 0) if fp else 0

    def drift(nid: int) -> str:
        r = db.execute("SELECT value FROM meta WHERE key=?", (f"drift:{nid}",)).fetchone()
        return r[0] if r else "fresh"

    def node_brief(nid: int) -> dict:
        r = db.execute(
            "SELECT title, kind, file_path, line FROM nodes WHERE id=?", (nid,)
        ).fetchone()
        if not r:
            return {}
        return {
            "id": nid, "title": r["title"], "kind": r["kind"],
            "file": (r["file_path"] or "").replace("\\", "/") or None, "line": r["line"],
        }

    def anchors(nid: int, lim: int = 16) -> list[str]:
        return [
            r[0] for r in db.execute(
                "SELECT DISTINCT term FROM fts_anchors WHERE node_id=? LIMIT ?", (nid, lim)
            ).fetchall()
        ]

    # ---- "what this file touches" — a per-lesson flag so the wiki LIST shows which
    # lessons carry a briefing (breaks / leans-on / symbols / open tasks) and the bar can
    # filter to them. Computed ONCE as file→counts (group-by over the whole graph), not
    # per-lesson — cheap, model-free (mirrors what brief() bundles, ADR-018/ADR-020).
    touch_breaks: dict[str, int] = {}   # files that DEPEND ON this file (blast radius)
    touch_deps: dict[str, int] = {}     # files this file leans on
    touch_syms: dict[str, int] = {}     # code-symbols defined in the file
    touch_tasks: dict[str, int] = {}    # open tasks wired to the file
    _DEP_KINDS = ("depends_on", "implements", "guarded_by", "relates_to", "co_changed")
    _ph = ",".join("?" * len(_DEP_KINDS))
    for a, b in db.execute(
        f"""SELECT REPLACE(nd.file_path,'\\','/') AS dep_on, REPLACE(ns.file_path,'\\','/') AS dependent
              FROM edges e JOIN nodes ns ON ns.id=e.src_node JOIN nodes nd ON nd.id=e.dst_node
             WHERE e.kind IN ({_ph}) AND ns.file_path IS NOT NULL AND nd.file_path IS NOT NULL
               AND ns.file_path != nd.file_path""", _DEP_KINDS,
    ).fetchall():
        if a:
            touch_breaks[a] = touch_breaks.get(a, 0) + 1   # b depends on a → a "breaks" b
        if b:
            touch_deps[b] = touch_deps.get(b, 0) + 1        # b leans on a
    for (f, n) in db.execute(
        "SELECT REPLACE(file_path,'\\','/') AS f, COUNT(*) FROM nodes "
        "WHERE kind='code-symbol' AND file_path IS NOT NULL AND file_path!='' "
        "AND (symbol IS NOT NULL OR line IS NOT NULL) GROUP BY f"
    ).fetchall():
        touch_syms[f] = n
    for (f, n) in db.execute(
        """SELECT REPLACE(nd.file_path,'\\','/') AS f, COUNT(DISTINCT nt.id)
             FROM edges e JOIN nodes nt ON nt.id=e.src_node JOIN nodes nd ON nd.id=e.dst_node
            WHERE nt.kind='task' AND e.kind='relates_to'
              AND (nt.facets IS NULL OR (nt.facets NOT LIKE '%done%'
                   AND nt.facets NOT LIKE '%dropped%' AND nt.facets NOT LIKE '%deferred%'))
              AND nd.file_path IS NOT NULL GROUP BY f"""
    ).fetchall():
        touch_tasks[f] = n

    def touches_for(rel: str) -> dict:
        """The briefing summary for a file: counts + a boolean 'any'. Empty-but-shaped."""
        br, de = touch_breaks.get(rel, 0), touch_deps.get(rel, 0)
        sy, tk = touch_syms.get(rel, 0), touch_tasks.get(rel, 0)
        return {"breaks": br, "deps": de, "symbols": sy, "tasks": tk,
                "any": bool(br or de or sy or tk)}

    # ---- lessons (the wiki + recent feed), newest first ----
    # `author` (v3): WHO wrote it, from git — surfaced so the page can show + filter by
    # person. NULL-safe: an older index without the column still works (COALESCE → '').
    lessons = []
    for nid, title, body, fp, sha, author, created, facets in db.execute(
        "SELECT id,title,body,file_path,stamped_at_sha,author,created_at,facets FROM nodes "
        "WHERE kind='lesson' ORDER BY id DESC LIMIT ?", (_MAX_LESSONS,)
    ).fetchall():
        edges = [
            {"kind": ek, **node_brief(dst)}
            for ek, dst in db.execute(
                "SELECT kind,dst_node FROM edges WHERE src_node=?", (nid,)
            ).fetchall()
        ]
        short = (sha or "")[:7]
        rel = (fp or "").replace("\\", "/")
        # honest timestamp: the commit's time (if pinned to a SHA), else the file's
        # last-commit time (when the knowledge actually changed), else index build time.
        ts = ctimes.get(short) or ftimes.get(rel) or created or None
        lessons.append({
            "id": nid, "title": title, "body": (body or "")[:2000],
            "why": (body or "").splitlines()[0] if body else "",
            "file": rel, "sha": short,
            "author": author or None,
            "ts": ts,
            "imp": imp_for(rel),  # inherited causal weight of the file it concerns (ADR-016)
            "tags": [f for f in (facets or "").split(",") if f],  # facets shown as tags
            "drift": drift(nid), "anchors": anchors(nid), "edges": edges,
            "touches": touches_for(rel),  # briefing summary for the lesson's file (ADR-020)
        })

    # ---- code map: file -> symbols (with lines + causal importance 1-100, ADR-016) ----
    # importance is the PageRank-over-dependency-graph weight, nudged by feedback. The
    # file's headline importance = its most load-bearing symbol, so a file's badge says
    # 'how much of the system leans on this file'.
    files: dict[str, list] = {}
    for sym, fp, line, imp in db.execute(
        "SELECT title,file_path,line,importance FROM nodes WHERE kind='code-symbol' "
        "AND file_path IS NOT NULL AND file_path!='' ORDER BY file_path, line"
    ).fetchall():
        files.setdefault(fp.replace("\\", "/"), []).append(
            {"sym": sym, "line": line, "imp": round(imp or 0, 1)})
    # REVERSE view (Owner: "see on the code files which learnings/touches/dependencies
    # hang off them"): for each code file, how many wiki LESSONS are pinned to it. The forward
    # edges (breaks/deps/tasks) reuse the touch_* maps from above — the same self-updating
    # web of facts, just read from the code end. Cheap group-by, model-free.
    lessons_per_file: dict[str, int] = {}
    for (f, n) in db.execute(
        "SELECT REPLACE(file_path,'\\','/') AS f, COUNT(*) FROM nodes "
        "WHERE kind IN ('lesson','decision') AND file_path IS NOT NULL AND file_path!='' "
        "GROUP BY f"
    ).fetchall():
        lessons_per_file[f] = n
    # the contested set (Wave B) so a file can carry a "contested" tag — best-effort.
    contested_files: set[str] = set()
    try:
        for spot in idx.contested_spots(repo=repo, limit=40):
            contested_files.add(spot["file"])
    except Exception:
        pass

    def code_tags(f: str, imp: float) -> list[str]:
        """Computed (never manual → always current) tags a code file carries, so the Code
        tab can show + filter them: has lessons / has open tasks / pillar / contested."""
        tg = []
        if lessons_per_file.get(f, 0) > 0:
            tg.append("has-lessons")
        if touch_tasks.get(f, 0) > 0:
            tg.append("has-tasks")
        if imp >= 50:
            tg.append("pillar")
        if f in contested_files:
            tg.append("contested")
        return tg

    code = [{
        "file": f, "symbols": sorted(syms, key=lambda s: -s["imp"])[:60], "count": len(syms),
        "imp": round(max((s["imp"] for s in syms), default=0), 1),  # file headline weight
        # the reverse "what hangs on this file" counts + computed tags (always current)
        "lessons": lessons_per_file.get(f, 0),
        "breaks": touch_breaks.get(f, 0), "deps": touch_deps.get(f, 0),
        "tasks": touch_tasks.get(f, 0),
        "tags": code_tags(f, round(max((s["imp"] for s in syms), default=0), 1)),
    } for f, syms in files.items()]
    # Lead with the load-bearing files (importance first, size as tiebreak) so the pillars
    # are visible at a glance — then cap. Symbols within a file are ordered by importance
    # too, so the most critical functions sit at the top of each file's list.
    code.sort(key=lambda c: (-c["imp"], -c["count"]))
    code = code[:_MAX_CODE_FILES]

    # ---- drift list ----
    drifted = []
    for nid, title, fp, sha in db.execute(
        "SELECT id,title,file_path,stamped_at_sha FROM nodes "
        "WHERE file_path IS NOT NULL AND file_path!='' ORDER BY id"
    ).fetchall():
        lvl = drift(nid)
        if lvl in ("committed", "uncommitted"):
            drifted.append({
                "id": nid, "title": title, "file": fp.replace("\\", "/"),
                "sha": (sha or "")[:7], "drift": lvl,
            })
        if len(drifted) >= 40:
            break

    # ---- tasks & plans (ADR-017): the lifecycle, wired to the files they affect ----
    tasks = []
    for nid, title, body, fp, facets, created in db.execute(
        "SELECT id,title,body,file_path,facets,created_at FROM nodes "
        "WHERE kind='task' ORDER BY id DESC"
    ).fetchall():
        fs = set((facets or "").split(","))
        status = next((s for s in ("done", "dropped", "deferred", "open") if s in fs), "open")
        tkind = next((k for k in ("plan", "roadmap", "sprint", "feature", "task") if k in fs), "task")
        affects = [
            r[0].replace("\\", "/") for r in db.execute(
                "SELECT DISTINCT nd.file_path FROM edges e JOIN nodes nd ON nd.id=e.dst_node "
                "WHERE e.src_node=? AND e.kind='relates_to' AND nd.file_path IS NOT NULL", (nid,)
            ).fetchall()
        ]
        ts = created or None
        # stale: open + the task file hasn't changed in 30d (drift alert signal)
        rel = (fp or "").replace("\\", "/")
        f_ts = ftimes.get(rel)
        # a task's importance = the highest of the files it affects (ADR-016) — so a task
        # touching a pillar reads as high-stakes everywhere it shows.
        t_imp = max([imp_for(a) for a in affects] + [imp_for(rel)], default=0)
        # checklist items in the body become sub-tasks with progress (the roadmap shows a
        # real, checkable list — not a flat node). Pure text parse, stays model-free.
        subtasks = parse_subtasks(body or "")
        done_n = sum(1 for s in subtasks if s["done"])
        # dropped/moved steps are RESOLVED (a done task may carry them) — only an
        # `- [ ]` left under status:done is a real inconsistency the page must flag.
        moved_n = sum(1 for s in subtasks if s.get("state") == "moved")
        dropped_n = sum(1 for s in subtasks if s.get("state") == "dropped")
        # who made the task + when, straight from the task file's git history (model-free).
        # created = first commit of the file; closed = its last commit, but ONLY when the
        # task reached a terminal status (done/dropped) — an open task isn't closed yet.
        ga = fauth.get(rel, {})
        closed_ts = closed_by = None
        if status in ("done", "dropped") and ga:
            closed_ts, closed_by = ga.get("last_ts"), ga.get("last_by")
        tasks.append({
            "id": nid, "title": title, "why": (body or "").splitlines()[0] if body else "",
            "body": (body or "")[:2000],
            "status": status, "task_kind": tkind, "file": rel, "affects": affects,
            "ts": f_ts or ts, "imp": round(t_imp, 1),
            "subtasks": subtasks, "done": done_n, "total": len(subtasks),
            "moved": moved_n, "dropped": dropped_n,
            "resolved": done_n + moved_n + dropped_n,
            # every step resolved but status still open -> the file's frontmatter was
            # probably never flipped. The UI nudges; the flip stays the author's call.
            "looks_done": looks_done(status, subtasks),
            "created_ts": ga.get("created_ts") or ts, "created_by": ga.get("created_by"),
            "closed_ts": closed_ts, "closed_by": closed_by,
        })
    open_count = sum(1 for t in tasks if t["status"] == "open")

    head = (_git(repo, "rev-parse", "--short", "HEAD").strip() or "—")
    branch = (_git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() or "—")

    # distinct authors with their lesson counts — drives the person filter (v3).
    authors = [
        {"name": a, "count": c}
        for a, c in db.execute(
            "SELECT author, COUNT(*) FROM nodes WHERE kind='lesson' AND author IS NOT NULL "
            "AND author!='' GROUP BY author ORDER BY COUNT(*) DESC"
        ).fetchall()
    ]

    return {
        "repo": repo.name, "branch": branch, "head": head,
        "indexed_at": _index_mtime(repo / ".mind" / "index.db"),
        "stats": {
            "nodes": s["nodes"], "lessons": s["by_kind"].get("lesson", 0),
            "symbols": s["by_kind"].get("code-symbol", 0),
            "anchors": s["anchors"], "edges": s["edges"],
        },
        "drift": dc,
        "lessons": lessons,
        "authors": authors,
        "code": code,
        "drifted": drifted,
        "tasks": tasks,
        "open_tasks": open_count,
        "product": build_product_tree(db),  # ADR-018: the living product map (PM/CTO view)
        "power_runs": idx.list_power_runs(),
        "git": git_snapshot(repo),
    }


def _safe_path(repo: Path, rel: str) -> Path | None:
    """Resolve `rel` strictly INSIDE the repo, or return None.

    The security core of /api/file and /api/diff — without it both would serve any
    file on disk. resolve() follows symlinks, so a symlink escaping the repo resolves
    outside and fails the jail check; '../' and absolute paths fail too."""
    if not rel or "\x00" in rel or "\\" in rel or Path(rel).is_absolute():
        return None
    base = repo.resolve()
    target = (base / rel).resolve()
    if target == base or base in target.parents:
        return target
    return None


# ------------------------------------------------------------ recent projects
# The dashboard runs against one repo at a time, but remembers the ones you've
# opened so the header can offer a switcher. One small file, names + paths only.
RECENT_PATH = Path.home() / ".recall" / "recent.json"
_MAX_RECENT = 12


def _index_mtime(idx_path: Path) -> int | None:
    """Unix mtime of the index file = when the project was last (re)indexed. None if
    there is no index yet. Token-free, always available — no need for a meta column."""
    try:
        return int(idx_path.stat().st_mtime)
    except OSError:
        return None


def _load_recent(path: Path | None = None) -> list[dict]:
    """The recently-opened projects, newest first. Tolerates a missing/garbled file.

    Self-pruning (owner 2026-06-11: "only show projects you actually chose"):
    entries whose directory is gone — deleted repos, vanished temp folders —
    are dropped on read, so the switcher only ever lists real, openable
    projects. The pruned list is NOT written back here (read paths stay
    write-free); the next deliberate _remember_recent persists the cleanup.
    """
    path = path if path is not None else RECENT_PATH
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for r in raw:
        if not (isinstance(r, dict) and r.get("path")):
            continue
        p = str(r["path"])
        try:
            if not Path(p).is_dir():
                continue
        except OSError:
            continue
        out.append({"name": str(r.get("name") or ""), "path": p})
    return out[:_MAX_RECENT]


def _remember_recent(repo: Path, idx_path: Path, path: Path | None = None) -> None:
    """Record (or bump to front) a project. Stores the repo path + its index path."""
    path = path if path is not None else RECENT_PATH
    rp = str(repo.resolve())
    entry = {"name": repo.resolve().name, "path": rp, "idx": str(idx_path)}
    items = [r for r in _load_recent(path) if r.get("path") != rp]
    items.insert(0, entry)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(items[:_MAX_RECENT], indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # remembering is a nicety; never break the dashboard over it


def _resolve_project(raw_path: str) -> tuple[Path, Path] | None:
    """Validate a user-supplied project path and find its index.

    Returns (repo, idx_path) or None if the path isn't a real directory. The index
    is `<repo>/.mind/index.db`; it may not exist yet (the caller can offer to init)."""
    try:
        repo = Path(raw_path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not repo.is_dir():
        return None
    return repo, repo / ".mind" / "index.db"


# ----------------------------------------------------------- power-mode jobs
# Power Mode (and indexing) take seconds-to-minutes, so the dashboard runs them on a
# background thread and the page polls for status — no blocking request, no fetch
# timeout. One job at a time (a single local user), guarded by a lock. The read path
# stays LLM-free: the model is only ever reached inside the worker, never at import.
_POWER_LOCK = threading.Lock()
_POWER_JOB: dict = {"state": "idle"}  # state: idle|running|done|error; + result/error/kind

# ----------------------------------------------------------- live auto-index
# The watcher thread (LIVE mode, ADR-…): it polls HEAD every few seconds and, when a
# NEW commit lands, re-indexes the project IN THE BACKGROUND so fresh commits become
# knowledge by themselves — no click. stdlib only (git rev-parse polling), no
# watchdog dep. The read path stays LLM-free: auto-index calls init()/freshen(), which
# never reach a model (the Seam-Guard, ADR-014). It respects _POWER_LOCK (never index
# while a Power run holds the index) and writes its last action into _WATCH_STATE so
# the page's pulse can raise a toast ("+M lessons loaded").
_WATCH_LOCK = threading.Lock()
_WATCH_STATE: dict = {"on": False, "last_head": None, "last_index_ts": None,
                      "last_added": 0, "indexing": False}


def _auto_index(repo: Path, idx_path: Path, since_sha: str | None = None) -> dict:
    """Update the index for the latest commit and report how much changed.

    When `since_sha` is given (the HEAD the index was last built at), this does an
    INCREMENTAL update — only the files that commit changed are re-parsed, not the
    whole repo (a one-file commit shouldn't trigger a full-tree reindex). Without it,
    it does a full idempotent init() (first build / startup). Returns
    {added, nodes, lessons, files_changed, incremental}. Never reaches a model
    (Seam-Guard ADR-014: init/update_incremental call no provider)."""
    from recall.bootstrap import init, update_incremental  # lazy: tree-sitter on index

    before = 0
    if idx_path.exists():
        try:
            tmp = Index.open(idx_path, repo=repo)
            try:
                before = tmp.db.execute("SELECT count(*) FROM nodes").fetchone()[0]
            finally:
                tmp.db.close()
        except Exception:
            before = 0
    idx = Index.open(idx_path, repo=repo)
    incremental = False
    files_changed = 0
    try:
        if since_sha and idx_path.exists():
            res = update_incremental(idx, repo, since_sha)
            incremental = bool(res.get("ok"))
            files_changed = res.get("files_changed", 0)
        else:
            init(idx, repo)
            try:
                idx.freshen()
            except Exception:
                pass
        after = idx.db.execute("SELECT count(*) FROM nodes").fetchone()[0]
        lessons = idx.db.execute("SELECT count(*) FROM nodes WHERE kind='lesson'").fetchone()[0]
    finally:
        idx.db.close()
    return {"added": max(0, after - before), "nodes": after, "lessons": lessons,
            "files_changed": files_changed, "incremental": incremental}


def _watch_loop(state: dict, stop: threading.Event, interval: float = 4.0) -> None:
    """Poll HEAD; on a new commit, auto-index (unless a Power run holds the index).

    `state` is the handler's mutable STATE dict (so a project switch repoints the
    watcher too). Designed to be unkillably safe: every iteration is wrapped, a git
    or index hiccup just skips this tick. The interval is a few seconds — cheap
    (one `git rev-parse`), invisible to the user, well under the prompt-cache window."""
    _WATCH_STATE["on"] = True
    # seed last_head from the current HEAD so we don't re-index on the very first tick
    # (the user just started the server against an already-built index).
    repo0 = state.get("repo")
    if repo0 is not None:
        _WATCH_STATE["last_head"] = _git(repo0, "rev-parse", "HEAD").strip() or None
    while not stop.wait(interval):
        try:
            repo = state.get("repo")
            idx_path = state.get("idx")
            if repo is None or idx_path is None:
                continue
            head = _git(repo, "rev-parse", "HEAD").strip()
            if not head or head == _WATCH_STATE.get("last_head"):
                continue  # no new commit since last tick
            # a new commit landed — but never index while Power holds the index.
            if _POWER_JOB.get("state") == "running":
                continue  # try again next tick, once Power is done
            with _WATCH_LOCK:
                if _WATCH_STATE.get("indexing"):
                    continue
                _WATCH_STATE["indexing"] = True
            try:
                # incremental: only re-parse what changed since the indexed HEAD
                prev = _WATCH_STATE.get("last_head")
                res = _auto_index(Path(repo), Path(idx_path), since_sha=prev)
                _WATCH_STATE.update({
                    "last_head": head, "last_index_ts": _index_mtime(Path(idx_path)),
                    "last_added": res.get("added", 0),
                    "last_files": res.get("files_changed", 0),
                    "last_incremental": res.get("incremental", False),
                })
            except Exception:
                # indexing failed this tick — don't advance last_head so we retry,
                # but never crash the watcher thread.
                pass
            finally:
                _WATCH_STATE["indexing"] = False
        except Exception:
            continue  # a watcher must never die on a transient error


def power_estimate(idx: Index, repo: Path, top_n: int | None, scope: str | None) -> dict:
    """The real estimate (hotspots + tokens + provider-aware cost). Zero completion
    calls — estimate_tokens never spends. Cost is $0 for a local/subscription provider
    (claude-cli, ollama) and a labelled estimate for a paid API (anthropic, custom)."""
    from recall.connect import load_connection
    from recall.llm import get_provider
    from recall.power import DEFAULT_TOP_N, estimate_tokens, select_hotspots

    conn = load_connection()
    if conn is None:
        return {"connected": False}
    provider = get_provider(conn)
    n = top_n if top_n is not None else DEFAULT_TOP_N
    hotspots = select_hotspots(idx, repo, scope=scope, top_n=n)
    est = estimate_tokens(idx, repo, hotspots, provider)
    paid = est.est_cost_usd > 0
    return {
        "connected": True,
        "provider": provider.name, "model": est.model,
        "hotspots": est.hotspots,
        "input_tokens": est.input_tokens, "output_tokens": est.est_output_tokens,
        "paid": paid,
        # the honest cost line: free when the provider bills no marginal tokens
        # (claude-cli runs on your subscription; ollama is local), else an estimate.
        "cost_usd": est.est_cost_usd,
        "cost_label": (f"~${est.est_cost_usd:.2f} estimated" if paid
                       else "free · runs on your subscription / locally"),
        # Hotspot is a dataclass with .file_path (not .file); the dict branch is a
        # belt-and-braces in case the selector ever returns plain dicts.
        "hotspot_files": [h.get("file_path") if isinstance(h, dict)
                          else getattr(h, "file_path", None)
                          for h in hotspots][:n],
    }


def _power_worker(idx_path: Path, repo: Path, top_n: int | None, scope: str | None) -> None:
    """Run a real power pass on a background thread, recording the outcome in _POWER_JOB.
    A fresh Index connection is opened here (never share the request thread's conn)."""
    from recall.connect import load_connection
    from recall.llm import get_provider
    from recall.power import DEFAULT_TOP_N, run_power

    try:
        conn = load_connection()
        if conn is None:
            raise RuntimeError("no AI connected")
        provider = get_provider(conn)
        idx = Index.open(idx_path, repo=repo)
        try:
            n = top_n if top_n is not None else DEFAULT_TOP_N

            def _progress(done: int, total: int) -> None:
                # surface a live count the page can render as a bar (done of total hotspots)
                _POWER_JOB["done"] = done
                _POWER_JOB["total"] = total

            res = run_power(idx, repo, provider=provider, scope=scope, top_n=n,
                            progress=_progress)
        finally:
            idx.db.close()
        _POWER_JOB.update({
            "state": "done",
            "result": {
                "run": res.run, "nodes_added": res.nodes_added,
                "synonyms_added": res.synonyms_added, "edges_added": res.edges_added,
                "model": getattr(provider, "model", "?"),
            },
        })
    except Exception as e:  # any failure surfaces as a status the page can show
        _POWER_JOB.update({"state": "error", "error": str(e)})


def _make_handler(repo: Path, idx_path: Path):
    # Serve the page shell FRESH (Owner 2026-06-09 "I still see the old one"): re-read
    # dashboard.html whenever its mtime changes, so editing the file shows up on the next
    # request with NO server restart. Cheap — a stat() per request, a read() only on change.
    # Combined with the no-store header below, the browser never shows a stale build either.
    _html_cache = {"mtime": -1.0, "text": ""}
    def _dashboard_html() -> str:
        try:
            mt = _HTML.stat().st_mtime
        except OSError:
            return _html_cache["text"]
        if mt != _html_cache["mtime"]:
            _html_cache["text"] = _HTML.read_text(encoding="utf-8")
            _html_cache["mtime"] = mt
        return _html_cache["text"]
    # Mutable so the header's project switcher can repoint the whole dashboard at
    # another repo at runtime (POST /api/switch, /api/open) without a restart. Every
    # request reads STATE fresh, so a switch takes effect on the next call.
    STATE = {"repo": repo.resolve(), "idx": idx_path, "base": repo.resolve()}
    _remember_recent(repo, idx_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet — no per-request noise in the terminal
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _is_local(self) -> bool:
            """True only for loopback clients. The connect write is local-machine only;
            even if the user bound the dashboard to 0.0.0.0, a remote box cannot write
            the connection. Read endpoints don't gate on this (they expose no secret)."""
            try:
                ip = ipaddress.ip_address(self.client_address[0])
            except (ValueError, IndexError):
                return False
            return ip.is_loopback

        def _same_origin(self) -> bool:
            """Reject cross-site / DNS-rebinding POSTs. A browser sends Origin on POST;
            we require its host to be a loopback name. Requests with no Origin (curl,
            the page's own fetch in some engines) are allowed only from a local client —
            _is_local already gates that. This blocks a malicious page on another origin
            from POSTing to the dashboard via the victim's browser."""
            origin = self.headers.get("Origin")
            if not origin:
                return True  # no Origin -> rely on _is_local; not a browser cross-site post
            host = urlparse(origin).hostname or ""
            if host in ("localhost", "127.0.0.1", "::1"):
                return True
            try:
                return ipaddress.ip_address(host).is_loopback
            except ValueError:
                return False

        def _host_ok(self) -> bool:
            """DNS-rebinding defense for EVERY request (reads included).

            The Host header carries the name the browser was told to connect to. In a
            DNS-rebind attack the page is on evil.com (which now resolves to 127.0.0.1):
            the TCP peer is loopback, but the browser still sends `Host: evil.com`. We
            require Host to be a loopback name/IP, so a rebound page can't read /api/file
            or /api/data (the docstring's own threat model — now covering the read path,
            not just writes). A missing Host (non-browser client, e.g. curl) is allowed;
            those aren't the cross-origin browser threat and a real attacker can't forge
            the victim's loopback Host from another origin."""
            host = self.headers.get("Host")
            if not host:
                return True
            name = host.rsplit(":", 1)[0].strip("[]").lower()  # drop :port, strip ipv6 []
            if name in ("localhost", "127.0.0.1", "::1") or name.endswith(".localhost"):
                return True
            try:
                return ipaddress.ip_address(name).is_loopback
            except ValueError:
                return False

        def do_GET(self):
            path = urlparse(self.path).path
            # The page shell + vendored assets are static + non-sensitive (no repo data),
            # so they load even under an odd Host; every data/file endpoint is gated.
            if path in ("/", "/index.html"):
                self._send(200, _dashboard_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path.startswith("/api/") and not self._host_ok():
                return self._json(403, {"error": "bad Host header (DNS-rebinding guard)"})
            if path == "/api/data":
                repo, idx_path = STATE["repo"], STATE["idx"]
                if not idx_path.exists():
                    # the repo has no index yet — tell the page so it can offer `recall init`
                    self._json(200, {"repo": repo.name, "no_index": True,
                                     "path": str(repo), "lessons": [], "stats": {},
                                     "drift": {},
                                     "code": [], "drifted": [], "power_runs": [],
                                     "git": {"branches": [], "commits": [], "tree": []},
                                     "branch": "—", "head": "—"})
                    return
                idx = Index.open(idx_path, repo=repo)  # fresh conn per request (thread-safe)
                try:
                    snap = build_snapshot(idx, repo)
                finally:
                    idx.db.close()
                self._json(200, snap)
                return
            if path == "/api/file":
                self._serve_file()
                return
            if path == "/api/diff":
                self._serve_diff()
                return
            if path == "/api/connection":
                self._serve_connection()
                return
            if path == "/api/projects":
                self._serve_projects()
                return
            if path == "/api/power-estimate":
                self._serve_power_estimate()
                return
            if path == "/api/power-status":
                self._serve_power_status()
                return
            if path == "/api/pulse":
                self._serve_pulse()
                return
            if path == "/api/hook":
                self._serve_hook_status()
                return
            if path == "/api/mcp":
                self._serve_mcp_status()
                return
            if path == "/api/license":
                self._serve_license()
                return
            if path == "/api/recall":
                self._serve_recall()
                return
            if path == "/api/brief":
                self._serve_brief()
                return
            if path == "/api/contested":
                self._serve_contested()
                return
            if path == "/api/stale":
                self._serve_stale()
                return
            if path == "/api/onboarding":
                self._serve_onboarding()
                return
            if path == "/api/commit":
                self._serve_commit()
                return
            if path == "/api/rules":
                self._serve_rules()
                return
            if path == "/api/guide":
                self._serve_guide()
                return
            if path == "/api/changelog":
                self._serve_changelog()
                return
            if path == "/api/about":
                self._serve_about()
                return
            if path == "/api/legal":
                self._serve_legal()
                return
            if path.startswith("/vendor/"):
                self._serve_vendor(path)
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = urlparse(self.path).path
            if not self._host_ok():  # belt-and-braces with the per-handler same-origin check
                return self._json(403, {"error": "bad Host header (DNS-rebinding guard)"})
            if path == "/api/connect":
                self._do_connect()
                return
            if path == "/api/switch":
                self._do_switch()
                return
            if path == "/api/index":
                self._do_index()
                return
            if path == "/api/power-run":
                self._do_power_run()
                return
            if path == "/api/hook":
                self._do_hook()
                return
            if path == "/api/mcp":
                self._do_mcp()
                return
            if path == "/api/license":
                self._do_license()
                return
            self._send(404, b"not found", "text/plain")

        def _serve_file(self):
            """Read one repo file (jailed, capped) and return it as JSON so the page can
            render line numbers + a highlight in one round-trip."""
            qs = parse_qs(urlparse(self.path).query)
            rel = (qs.get("path") or [""])[0]
            line = (qs.get("line") or [None])[0]
            target = _safe_path(STATE["base"], rel)
            if target is None:
                return self._json(400, {"error": "bad or out-of-repo path"})
            if not target.is_file():
                return self._json(404, {"error": "not a file"})
            try:
                size = target.stat().st_size
            except OSError:
                return self._json(404, {"error": "not readable"})
            truncated = size > _MAX_FILE_BYTES
            try:
                raw = target.read_bytes()[:_MAX_FILE_BYTES] if truncated else target.read_bytes()
            except OSError:
                return self._json(404, {"error": "not readable"})
            if b"\x00" in raw[:1024]:
                return self._json(415, {"error": "binary file"})
            content = raw.decode("utf-8", errors="replace")
            self._json(200, {
                "path": rel, "line": int(line) if (line and line.isdigit()) else None,
                "lines": content.count("\n") + 1, "truncated": truncated, "content": content,
            })

        def _serve_diff(self):
            """The before/after: `git diff <sha>..HEAD -- <file>` — what changed in the
            file since this knowledge was stamped. The visual heart of 'follow along'."""
            qs = parse_qs(urlparse(self.path).query)
            rel = (qs.get("path") or [""])[0]
            sha = (qs.get("sha") or [""])[0]
            target = _safe_path(STATE["base"], rel)
            if target is None:
                return self._json(400, {"error": "bad or out-of-repo path"})
            if not sha or not all(c in "0123456789abcdefABCDEF" for c in sha):
                return self._json(400, {"error": "missing or invalid sha"})
            # token-free, read-only; rel is jail-checked, sha is hex-checked.
            diff = _git(STATE["repo"], "diff", f"{sha}..HEAD", "--", rel)
            self._json(200, {"path": rel, "sha": sha[:7], "diff": diff, "empty": not diff.strip()})

        def _serve_commit(self):
            """What a commit actually CHANGED — its message + per-file diffs. The core of
            'follow the change', token-free. `git show` for the metadata, then one
            file-scoped `git diff <sha>^..<sha> -- <file>` per changed path so each file's
            patch is a clean unit the page can render with the diff viewer."""
            qs = parse_qs(urlparse(self.path).query)
            sha = (qs.get("sha") or [""])[0]
            if not sha or not all(c in "0123456789abcdefABCDEF" for c in sha):
                return self._json(400, {"error": "missing or invalid sha"})
            repo = STATE["repo"]
            _GS = "\x1f"
            # metadata: short-sha, author, time, subject, body — one read.
            meta = _git(repo, "show", "-s", f"--format=%h{_GS}%an{_GS}%at{_GS}%s{_GS}%b", sha)
            parts = meta.split(_GS)
            if len(parts) < 4:
                return self._json(404, {"error": "no such commit"})
            short, author = parts[0].strip(), parts[1].strip()
            ts = int(parts[2]) if parts[2].strip().isdigit() else None
            subject, body = parts[3].strip(), (parts[4].strip() if len(parts) > 4 else "")
            # changed files with status (A/M/D/R) — the parent range covers the root commit
            # too (git show on a root commit diffs against the empty tree).
            names = _git(repo, "show", "--name-status", "--format=", sha)
            files = []
            total = 0
            for line in names.splitlines():
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                bits = line.split("\t")
                status = bits[0][:1]
                rel = (bits[-1] if bits else "").strip().replace("\\", "/")
                if not rel:
                    continue
                # one clean per-file patch; capped so a giant commit can't blow the response
                d = _git(repo, "show", "--format=", "-p", sha, "--", rel)
                if total > 600_000:  # overall cap across files
                    files.append({"path": rel, "status": status, "diff": "",
                                  "truncated": True})
                    continue
                if len(d) > 200_000:
                    d = d[:200_000]
                total += len(d)
                files.append({"path": rel, "status": status, "diff": d})
            self._json(200, {
                "sha": short, "author": author, "ts": ts,
                "subject": subject, "body": body, "files": files,
            })

        def _serve_connection(self):
            """The current AI connection for the connect-modal — NAMES ONLY. We return
            the provider/model/base_url and the env-var NAME of any key, plus whether
            that env var is currently set, but NEVER the key value itself (ADR-012)."""
            import os

            from recall.connect import PROVIDERS, load_connection

            conn = load_connection()
            out = {"connected": conn is not None, "providers": list(PROVIDERS)}
            if conn is not None:
                out["connection"] = {
                    "provider": conn.provider, "model": conn.model,
                    "base_url": conn.base_url, "api_key_env": conn.api_key_env,
                    # only a boolean — does the named env var hold something? never the value.
                    "key_present": bool(conn.api_key_env and os.environ.get(conn.api_key_env)),
                }
            self._json(200, out)

        def _read_json_body(self) -> dict | None:
            """Parse a small JSON request body, or None if missing/oversized/invalid."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > 64_000:  # a connection is tiny; cap to be safe
                return None
            try:
                raw = self.rfile.read(length)
                obj = json.loads(raw.decode("utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                return None
            return obj if isinstance(obj, dict) else None

        def _serve_license(self):
            """The stored account license, decoded for display (ADR-030). The raw
            token never goes to the page — only the payload + computed state."""
            from recall.license import load_license

            lic = load_license()
            self._json(200, {"signed_in": lic is not None, "license": lic})

        def _do_license(self):
            """Save / clear the account license token — the Account tab's only write.

            Same hardening as _do_connect: loopback clients only + same-origin, so
            a rebound page can't plant or wipe a license. Uses the SAME file
            (`~/.recall/license.token`) that `recall login` will write later, so
            the dashboard and the CLI gate stay byte-for-byte compatible."""
            from recall.license import clear_license, save_license

            if not self._is_local():
                return self._json(403, {"error": "license is local-machine only"})
            if not self._same_origin():
                return self._json(403, {"error": "cross-origin license refused"})
            body = self._read_json_body()
            if body is None:
                return self._json(400, {"error": "invalid request body"})

            if body.get("action") == "clear":
                removed = clear_license()
                return self._json(200, {"signed_in": False, "cleared": removed})

            token = str(body.get("token") or "")
            try:
                payload = save_license(token)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except OSError as e:
                return self._json(500, {"error": f"could not save: {e}"})
            self._json(200, {"signed_in": True, "license": payload})

        def _do_connect(self):
            """Write / clear the AI connection — the connect-modal's only write.

            Hardened: loopback clients only (_is_local) and same-origin (_same_origin),
            so a page on another origin cannot rebind+POST here. Uses the SAME
            save_connection/Connection as `recall connect`, so the CLI and the modal
            agree byte-for-byte (ADR-012). Stores only the env-var NAME, never a key."""
            from recall.connect import Connection, clear_connection, save_connection

            if not self._is_local():
                return self._json(403, {"error": "connect is local-machine only"})
            if not self._same_origin():
                return self._json(403, {"error": "cross-origin connect refused"})
            body = self._read_json_body()
            if body is None:
                return self._json(400, {"error": "invalid request body"})

            if body.get("action") == "clear":
                removed = clear_connection()
                return self._json(200, {"connected": False, "cleared": removed})

            provider = str(body.get("provider") or "").strip()
            model = str(body.get("model") or "").strip()
            base_url = (str(body.get("base_url")).strip() or None) if body.get("base_url") else None
            key_env = (str(body.get("api_key_env")).strip() or None) if body.get("api_key_env") else None
            # mirror the CLI: anthropic defaults its key env var when none is given.
            if provider == "anthropic" and not key_env:
                key_env = "ANTHROPIC_API_KEY"
            try:
                conn = Connection(
                    provider=provider, model=model, base_url=base_url, api_key_env=key_env,
                )
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            try:
                save_connection(conn)
            except OSError as e:
                return self._json(500, {"error": f"could not save: {e}"})
            self._json(200, {
                "connected": True,
                "connection": {
                    "provider": conn.provider, "model": conn.model,
                    "base_url": conn.base_url, "api_key_env": conn.api_key_env,
                },
            })

        def _serve_projects(self):
            """The current project + the recent list, for the header switcher."""
            repo, idx_path = STATE["repo"], STATE["idx"]
            recent = []
            for r in _load_recent():
                p = Path(r["path"])
                recent.append({
                    "name": r["name"] or p.name, "path": r["path"],
                    "current": p.resolve() == repo.resolve(),
                    "indexed": (p / ".mind" / "index.db").exists(),
                })
            self._json(200, {
                "current": {"name": repo.name, "path": str(repo),
                            "indexed": idx_path.exists(),
                            "indexed_at": _index_mtime(idx_path)},
                "recent": recent,
            })

        def _do_switch(self):
            """Repoint the dashboard at another project at runtime (header switcher).

            `{"path": "..."}` opens any directory on this machine; `{"action":"open"}`
            is the same with an explicit init offer. Local + same-origin only — this
            reads arbitrary local dirs, so it must never be reachable cross-site. The
            path itself is NOT jailed to the old repo (switching is the whole point),
            but it is gated to loopback callers, exactly like the connect write."""
            if not self._is_local():
                return self._json(403, {"error": "switching projects is local-machine only"})
            if not self._same_origin():
                return self._json(403, {"error": "cross-origin switch refused"})
            body = self._read_json_body()
            if body is None:
                return self._json(400, {"error": "invalid request body"})
            resolved = _resolve_project(str(body.get("path") or ""))
            if resolved is None:
                return self._json(400, {"error": "not a directory on this machine"})
            new_repo, new_idx = resolved
            STATE["repo"], STATE["idx"], STATE["base"] = new_repo, new_idx, new_repo.resolve()
            _remember_recent(new_repo, new_idx)
            self._json(200, {
                "switched": True,
                "current": {"name": new_repo.name, "path": str(new_repo),
                            "indexed": new_idx.exists()},
            })

        def _do_index(self):
            """Index the current project (`recall init`) so the dashboard has data.

            Local + same-origin only — it writes to .mind/ on disk. Runs synchronously:
            indexing is fast (token-free, no model), so the page can wait on it."""
            if not self._is_local():
                return self._json(403, {"error": "indexing is local-machine only"})
            if not self._same_origin():
                return self._json(403, {"error": "cross-origin index refused"})
            repo, idx_path = STATE["repo"], STATE["idx"]
            try:
                from recall.bootstrap import init  # lazy: tree-sitter only when indexing
                idx = Index.open(idx_path, repo=repo)
                try:
                    init(idx, repo)
                    stats = idx.stats()
                finally:
                    idx.db.close()
            except Exception as e:
                return self._json(500, {"error": f"indexing failed: {e}"})
            STATE["idx"] = idx_path  # now exists
            self._json(200, {"indexed": True, "nodes": stats.get("nodes", 0),
                             "anchors": stats.get("anchors", 0),
                             "indexed_at": _index_mtime(idx_path)})

        def _serve_power_estimate(self):
            """The real Power-Mode estimate for the current project — provider-aware
            cost, zero completion calls. The page shows this before offering to run."""
            repo, idx_path = STATE["repo"], STATE["idx"]
            if not idx_path.exists():
                return self._json(200, {"no_index": True})
            qs = parse_qs(urlparse(self.path).query)
            tn = (qs.get("top_n") or [None])[0]
            top_n = int(tn) if (tn and tn.isdigit()) else None
            scope = (qs.get("scope") or [None])[0] or None
            idx = Index.open(idx_path, repo=repo)
            try:
                out = power_estimate(idx, repo, top_n, scope)
            except Exception as e:
                return self._json(500, {"error": str(e)})
            finally:
                idx.db.close()
            self._json(200, out)

        def _do_power_run(self):
            """Start a real Power run on a background thread (ADR-008). Local + same-
            origin only — it reaches the connected model and writes the index. Returns
            immediately; the page polls /api/power-status. One run at a time."""
            if not self._is_local():
                return self._json(403, {"error": "power is local-machine only"})
            if not self._same_origin():
                return self._json(403, {"error": "cross-origin power refused"})
            repo, idx_path = STATE["repo"], STATE["idx"]
            if not idx_path.exists():
                return self._json(400, {"error": "no index — index this project first"})
            body = self._read_json_body() or {}
            top_n = body.get("top_n") if isinstance(body.get("top_n"), int) else None
            scope = (str(body.get("scope")).strip() or None) if body.get("scope") else None
            with _POWER_LOCK:
                if _POWER_JOB.get("state") == "running":
                    return self._json(409, {"error": "a power run is already in progress"})
                _POWER_JOB.clear()
                _POWER_JOB.update({"state": "running", "kind": "power"})
                t = threading.Thread(
                    target=_power_worker, args=(idx_path, repo, top_n, scope), daemon=True)
                t.start()
            self._json(202, {"started": True})

        def _serve_power_status(self):
            """Poll target for a running/finished power job."""
            self._json(200, dict(_POWER_JOB))

        def _serve_pulse(self):
            """The cheap heartbeat the page polls for LIVE mode (see pulse())."""
            self._json(200, pulse(STATE["repo"], STATE["idx"]))

        def _serve_recall(self):
            """A REAL 3-level recall over the local index — 0 tokens, 0 model, offline.

            This makes the Search tab and the Overview "Recall" card honest: the latency
            shown is the ACTUAL measured idx.recall() time (latency_us), not a demo value.
            LLM-free (recall() never touches a model — that's the whole point, ADR-014)."""
            repo, idx_path = STATE["repo"], STATE["idx"]
            if not idx_path.exists():
                return self._json(200, {"no_index": True})
            qs = parse_qs(urlparse(self.path).query)
            query = (qs.get("q") or [""])[0].strip()
            if not query:
                return self._json(400, {"error": "empty query"})
            idx = Index.open(idx_path, repo=repo)
            try:
                res = idx.recall(query, topk=3, consumer="dashboard")
            except Exception as e:
                return self._json(500, {"error": str(e)})
            finally:
                idx.db.close()
            # trim each result to what the page renders (drop the heavy body for the list)
            out = {
                "silenced": res.get("silenced", True),
                "latency_us": res.get("latency_us", 0),
                "results": [
                    {
                        "node_id": r.get("node_id"), "title": r.get("title"),
                        "why": r.get("why"), "file": r.get("file"), "sha": r.get("sha"),
                        "drift": r.get("drift"), "kind": r.get("kind"), "line": r.get("line"),
                        "matched_anchors": r.get("matched_anchors", []),
                        "relation": r.get("relation", []),
                    }
                    for r in res.get("results", [])
                ],
                # the 3 parallel tracks (ADR-016) — so Search shows code (by importance),
                # knowledge (by relevance) and blast_radius (what breaks) side by side,
                # never one burying the other.
                "code": res.get("code", []),
                "knowledge": res.get("knowledge", []),
                "blast_radius": res.get("blast_radius", []),
                "open_tasks": res.get("open_tasks", []),
            }
            self._json(200, out)

        def _serve_brief(self):
            """Wave A — the Pre-Edit Briefing for ONE file: why it is the way it is, what
            breaks if you change it, what it leans on, which open tasks affect it, and the
            symbols it defines. 0 tokens, 0 model, offline (ADR-014) — pure idx.brief()."""
            repo, idx_path = STATE["repo"], STATE["idx"]
            if not idx_path.exists():
                return self._json(200, {"no_index": True})
            qs = parse_qs(urlparse(self.path).query)
            rel = (qs.get("file") or [""])[0].strip()
            if not rel:
                return self._json(400, {"error": "empty file"})
            idx = Index.open(idx_path, repo=repo)
            try:
                b = idx.brief(rel)
            except Exception as e:
                return self._json(500, {"error": str(e)})
            finally:
                idx.db.close()
            self._json(200, b)

        def _serve_contested(self):
            """Wave B — uncertainty hotspots: the files the team kept changing (high churn
            AND entanglement). Git churn is read lazily here (one `git log`), so the heavy
            history scan only runs when the Drift tab is opened, not on every /api/data.
            0 tokens, 0 model (ADR-019/ADR-014)."""
            repo, idx_path = STATE["repo"], STATE["idx"]
            if not idx_path.exists():
                return self._json(200, {"no_index": True, "spots": []})
            idx = Index.open(idx_path, repo=repo)
            try:
                spots = idx.contested_spots(repo=repo, limit=15)
            except Exception as e:
                return self._json(500, {"error": str(e)})
            finally:
                idx.db.close()
            self._json(200, {"spots": spots})

        def _serve_stale(self):
            """Wave E — the stale-decision alarm (ADR-022): decisions whose referenced code
            has moved on a lot since they were stamped. Like contested, the git history scan
            runs lazily here (only when the Drift tab asks), 0 tokens, 0 model (ADR-014)."""
            repo, idx_path = STATE["repo"], STATE["idx"]
            if not idx_path.exists():
                return self._json(200, {"no_index": True, "stale": []})
            idx = Index.open(idx_path, repo=repo)
            try:
                stale = idx.stale_decisions(repo=repo, limit=15)
            except Exception as e:
                return self._json(500, {"error": str(e)})
            finally:
                idx.db.close()
            self._json(200, {"stale": stale})

        def _serve_onboarding(self):
            """Wave C — "explain me this repo" (ADR-020): the orientation path for a new
            dev / fresh AI session — load-bearing files, must-know decisions, what's in
            progress, where time burns. Pure idx.onboarding(), 0 tokens, model-free."""
            repo, idx_path = STATE["repo"], STATE["idx"]
            if not idx_path.exists():
                return self._json(200, {"no_index": True})
            idx = Index.open(idx_path, repo=repo)
            try:
                o = idx.onboarding()
            except Exception as e:
                return self._json(500, {"error": str(e)})
            finally:
                idx.db.close()
            self._json(200, o)

        def _serve_hook_status(self):
            """Is the git post-commit auto-stamp hook installed in this project?"""
            from adapters.hook import hook_status
            self._json(200, hook_status(STATE["repo"]))

        def _serve_mcp_status(self):
            """Is recall registered as this project's MCP server (.mcp.json), and when
            did an MCP client last actually use it (access_log consumer='mcp')?"""
            from recall.mcp import mcp_status
            repo, idx_path = STATE["repo"], STATE["idx"]
            if not idx_path.exists():
                return self._json(200, mcp_status(repo))
            idx = Index.open(idx_path, repo=repo)  # fresh conn per request (thread-safe)
            try:
                st = mcp_status(repo, db=idx.db)
            finally:
                idx.db.close()
            self._json(200, st)

        def _do_mcp(self):
            """Register / unregister recall in <repo>/.mcp.json (the dashboard pill).
            Local + same-origin only — it writes a file into the repo (same guards as
            the git-hook toggle). Never clobbers foreign servers in the file."""
            from recall.mcp import mcp_status, register_project, unregister_project
            if not self._is_local():
                return self._json(403, {"error": "the MCP registration is local-machine only"})
            if not self._same_origin():
                return self._json(403, {"error": "cross-origin MCP change refused"})
            body = self._read_json_body() or {}
            action = str(body.get("action") or "install")
            r = unregister_project(STATE["repo"]) if action == "uninstall" \
                else register_project(STATE["repo"])
            r.update(mcp_status(STATE["repo"]))  # always return the resulting status
            self._json(200 if r.get("ok", True) else 400, r)

        def _do_hook(self):
            """Install / uninstall a git hook (the dashboard toggle). `which` selects the
            hook: "post" = the auto-stamp post-commit hook (default), "pre" = the
            pre-commit risk-warning hook (Wave D). Local + same-origin only — it writes
            into .git/hooks on disk."""
            from adapters.hook import (
                hook_status,
                install_post_commit,
                install_pre_commit,
                uninstall_post_commit,
                uninstall_pre_commit,
            )
            if not self._is_local():
                return self._json(403, {"error": "the commit hook is local-machine only"})
            if not self._same_origin():
                return self._json(403, {"error": "cross-origin hook change refused"})
            body = self._read_json_body() or {}
            action = str(body.get("action") or "install")
            is_pre = str(body.get("which") or "post") == "pre"
            if action == "uninstall":
                r = uninstall_pre_commit(STATE["repo"]) if is_pre else uninstall_post_commit(STATE["repo"])
            else:
                r = install_pre_commit(STATE["repo"]) if is_pre else install_post_commit(STATE["repo"])
            r.update(hook_status(STATE["repo"]))  # always return the resulting status
            self._json(200 if r.get("ok", True) else 400, r)

        def _serve_rules(self):
            """Serve the shipped governance file (rules.md) — verbatim, the exact
            file the engine loads as layer 0. Transparency: an AI/dev installing
            recall can read (and with ?download=1 save) the real rules, not a copy.
            `?download=1` sets a Content-Disposition so the browser saves it."""
            from recall.rules import _bundled_rules_path
            try:
                body = _bundled_rules_path().read_bytes()
            except OSError:
                return self._send(404, b"rules.md not found", "text/plain")
            want_dl = parse_qs(urlparse(self.path).query).get("download", ["0"])[0] == "1"
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if want_dl:
                self.send_header("Content-Disposition", 'attachment; filename="rules.md"')
            self.end_headers()
            self.wfile.write(body)

        def _serve_guide(self):
            """Serve the shipped getting-started guide verbatim (the ONE source the
            How-it-works section, the README and the website all share — never three
            drifting copies). `?download=1` saves it as a file."""
            guide = Path(__file__).resolve().parent / "getting-started.md"
            try:
                body = guide.read_bytes()
            except OSError:
                return self._send(404, b"getting-started.md not found", "text/plain")
            want_dl = parse_qs(urlparse(self.path).query).get("download", ["0"])[0] == "1"
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if want_dl:
                self.send_header("Content-Disposition", 'attachment; filename="getting-started.md"')
            self.end_headers()
            self.wfile.write(body)

        def _serve_changelog(self):
            """Serve the shipped user-facing changelog verbatim (the ONE source the
            Changelog tab and the website's /changelog page share). `?download=1`
            saves it as a file."""
            log = Path(__file__).resolve().parent / "changelog.md"
            try:
                body = log.read_bytes()
            except OSError:
                return self._send(404, b"changelog.md not found", "text/plain")
            want_dl = parse_qs(urlparse(self.path).query).get("download", ["0"])[0] == "1"
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if want_dl:
                self.send_header("Content-Disposition", 'attachment; filename="changelog.md"')
            self.end_headers()
            self.wfile.write(body)

        def _serve_about(self):
            """Product facts for the About tab — name, version, license id, copyright.
            The legal FULL TEXTS come from /api/legal; this is the small header data."""
            try:
                from importlib.metadata import version
                ver = version("whatever-recall")
            except Exception:
                ver = "0.0.0"
            self._json(200, {
                "name": "whatever-recall",
                "version": ver,
                "license": "Business Source License 1.1",
                "copyright": "© 2026 Kathrin & Christian Mc Cain · McCain Digital",
                "vendor_url": "https://mccain-digital.com",
                # who is behind the product + the ONE real mail domain (owner
                # 2026-06-12) — the About tab renders this verbatim.
                "vendor_note": "a product & service of McCain Digital — all support, "
                               "info & payment mail comes from @mccain-digital.com",
                "support_email": "support@mccain-digital.com",
                "payment_email": "payment@mccain-digital.com",
            })

        def _serve_legal(self):
            """Serve the PRODUCT's legal texts verbatim (?doc=license|commercial) —
            same no-drifting-copies rule as rules/guide. They live at the package
            source root (the editable/clone install this guide ships); a future
            PyPI wheel adds them to dist-info (noted in the about task)."""
            doc = parse_qs(urlparse(self.path).query).get("doc", ["license"])[0]
            names = {"license": "LICENSE", "commercial": "COMMERCIAL.md"}
            if doc not in names:
                return self._json(400, {"error": "doc must be license|commercial"})
            target = Path(__file__).resolve().parent.parent / names[doc]
            try:
                body = target.read_bytes()
            except OSError:
                return self._send(404, b"legal text not found in this install "
                                       b"- see github.com/heidrich/whatever-recall", "text/plain")
            want_dl = parse_qs(urlparse(self.path).query).get("download", ["0"])[0] == "1"
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if want_dl:
                self.send_header("Content-Disposition", f'attachment; filename="{names[doc]}"')
            self.end_headers()
            self.wfile.write(body)

        def _serve_vendor(self, path: str):
            """Serve a bundled static asset (highlight.js + theme) from recall/vendor/.
            Jailed to that folder (only .js/.css, no traversal) — these ship in the
            package so the dashboard highlights code offline, no CDN, no npm."""
            rel = path[len("/vendor/"):]
            if not rel or "/" in rel or "\\" in rel or ".." in rel:
                return self._send(404, b"not found", "text/plain")
            target = (_VENDOR / rel).resolve()
            if _VENDOR.resolve() not in target.parents or not target.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = _VENDOR_TYPES.get(target.suffix)
            if ctype is None:
                return self._send(404, b"not found", "text/plain")
            try:
                body = target.read_bytes()
            except OSError:
                return self._send(404, b"not found", "text/plain")
            # static vendored asset — cacheable (unlike the live API responses)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)

    return Handler, STATE


def serve(repo: Path, idx_path: Path, *, host: str = "127.0.0.1", port: int = 7099,
          open_browser: bool = True, watch: bool = True) -> int:
    """Start the dashboard. Returns an exit code (0 on clean Ctrl-C).

    `watch=True` (the default) starts the live auto-index thread: a new commit gets
    indexed on its own, so the dashboard stays current without a reload. Pass
    watch=False (`--no-watch`) to run a purely passive viewer."""
    handler, state = _make_handler(repo, idx_path)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"

    stop = threading.Event()
    watcher: threading.Thread | None = None
    if watch:
        watcher = threading.Thread(target=_watch_loop, args=(state, stop), daemon=True)
        watcher.start()

    print(f"recall · dashboard on {url}  (reading {idx_path}, read-only)")
    print("  live mode " + ("on — new commits auto-index" if watch else "off (--no-watch)"))
    print("  Ctrl-C to stop")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nrecall · dashboard stopped")
    finally:
        stop.set()
        httpd.server_close()
    return 0

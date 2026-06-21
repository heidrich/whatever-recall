"""Adapter A — the CLI. Your dogfood tool.

    recall init [path]            index a project (.mind/index.db)
    recall "<query>"              the 3 levels, pretty in the terminal
    recall "<query>" --for-prompt a copy-paste context block for any web AI
    recall stamp "<title>" ...    stamp a node by hand
    recall stats                  what's in the index

The index lives in <repo>/.mind/index.db — inside the project, deploy-safe,
reproducible. Knowledge lives where the code lives.
"""

from __future__ import annotations

import argparse
import io
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from recall.engine import Index


def _force_utf8_stdout() -> None:
    """Windows consoles default to cp1252 and choke on ✓/→/● etc. Reconfigure
    stdout/stderr to UTF-8 so the pretty output never crashes the CLI."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # py3.7+
        except (AttributeError, ValueError):
            buf = getattr(stream, "buffer", None)
            if buf is not None:
                setattr(sys, stream_name, io.TextIOWrapper(buf, encoding="utf-8", errors="replace"))


_force_utf8_stdout()

MIND_DIR = ".mind"
INDEX_NAME = "index.db"


# --------------------------------------------------------------- index location
def _index_path(repo: str | Path) -> Path:
    return Path(repo) / MIND_DIR / INDEX_NAME


def _find_repo(start: str | Path = ".") -> Path:
    """Walk up from `start` to the nearest dir containing .git or .mind; else cwd."""
    p = Path(start).resolve()
    for cand in [p, *p.parents]:
        if (cand / ".git").exists() or (cand / MIND_DIR).exists():
            return cand
    return Path(start).resolve()


def _repo_from_args(args) -> Path:
    """Resolve the target repo from either an optional positional `path` or `--repo`.

    Most subcommands take `--repo`; the hand-typed ones (init/dashboard/shortcut/…)
    ALSO accept a positional path so `recall <cmd> .` works like `recall init .`
    (audit 2026-06-13: `recall shortcut .` used to error 'unrecognized arguments').
    Positional wins when given; otherwise --repo; otherwise here."""
    pos = getattr(args, "path", None)
    return _find_repo(pos or getattr(args, "repo", None) or ".")


class CorruptIndexError(Exception):
    """The .mind/index.db exists but isn't a readable recall index (truncated,
    garbage, a merge artifact). Carries an ACTIONABLE message so the central
    error boundary (_dispatch) tells the user how to recover instead of printing
    the raw 'file is not a database' (self-audit 2026-06-14)."""


def _open_existing(repo: Path) -> Index | None:
    idx_path = _index_path(repo)
    if not idx_path.exists():
        return None
    try:
        return Index.open(idx_path, repo=repo)
    except sqlite3.OperationalError:
        # A TRANSIENT lock/busy ('database is locked') is NOT corruption — telling the
        # user to delete .mind/ on a momentary contention with the dashboard would be
        # destructive advice. Let it propagate to the _dispatch boundary, which prints a
        # clean retry line. (P3 bug-hunt round 2, 2026-06-15: round-1 over-broadened this.)
        raise
    except sqlite3.DatabaseError:
        # GENUINE corruption / non-sqlite file ('file is not a database', 'malformed'):
        # don't return None (that lies "no index" for a file that IS there) and don't
        # surface the cryptic sqlite message. Tell the user exactly how to recover — the
        # index is a rebuildable artifact.
        raise CorruptIndexError(
            f"the recall index at {idx_path} is unreadable (corrupt or incomplete) — "
            f"delete .mind/ and run `recall init .` in {repo} to rebuild it (token-free, offline)"
        ) from None


# ----------------------------------------------------------------- ANSI helpers
class C:
    DIM = "\033[2m"; B = "\033[1m"; RESET = "\033[0m"
    MUSTARD = "\033[33m"; GREEN = "\033[32m"; RED = "\033[31m"; CYAN = "\033[36m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _c(s: str, code: str) -> str:
    return f"{code}{s}{C.RESET}" if _supports_color() else s


# ------------------------------------------------------------------ subcommands
def cmd_init(args) -> int:
    from recall.bootstrap import init  # lazy: tree-sitter import only when indexing

    as_json = getattr(args, "json", False)

    def _fail_json(reason: str) -> int:
        import json as _json
        from recall import __version__ as ver
        print(_json.dumps({"ok": False, "reason": reason, "engine": "recall", "version": ver}))
        return 1

    repo = Path(args.path).resolve()
    if not repo.exists():
        if as_json:
            return _fail_json("path-not-found")
        print(_c(f"path not found: {repo}", C.RED))
        return 1
    idx_path = _index_path(repo)
    try:
        idx = Index.open(idx_path, repo=repo)
    except sqlite3.OperationalError:
        raise  # transient lock/busy — NOT corruption; don't unlink a healthy index
    except sqlite3.DatabaseError:
        # `recall init` is the REBUILD command, so a GENUINELY corrupt/incomplete existing
        # index (merge artifact, interrupted write — 'file is not a database') should
        # self-heal: drop it and build fresh, rather than dying on a raw sqlite error.
        # (P3 bug-hunt 2026-06-15; lock-vs-corruption split added round 2.)
        if not as_json:
            print(_c(f"recall · existing index at {idx_path} is unreadable — rebuilding", C.MUSTARD))
        try:
            idx_path.unlink()
        except OSError as e:
            if as_json:
                return _fail_json("corrupt-unremovable")
            print(_c(f"could not remove the corrupt index: {e}", C.RED))
            return 1
        idx = Index.open(idx_path, repo=repo)
    if not as_json:
        print(_c(f"recall · indexing {repo} …", C.DIM))
    st = init(idx, repo, max_commits=args.max_commits, code_map=not args.no_code_map)
    s = idx.stats()

    if as_json:
        import json as _json
        from recall import __version__ as ver
        # seed the state block (same best-effort as the human path) before emitting.
        try:
            block = idx.render_state_block()
            wrapped = f"{idx.STATE_BEGIN}\n{block}\n{idx.STATE_END}"
            seed = repo / ("CLAUDE.md" if (repo / "CLAUDE.md").exists() else "AGENTS.md")
            existing = seed.read_text(encoding="utf-8") if seed.exists() else ""
            if idx.STATE_BEGIN not in existing:
                seed.write_text((existing.rstrip() + "\n\n" if existing.strip() else "")
                                + wrapped + "\n", encoding="utf-8")
        except Exception:
            pass
        print(_json.dumps({
            "ok": True, "engine": "recall", "version": ver,
            "repo": str(repo), "name": repo.name,
            "nodes": s["nodes"], "edges": s["edges"], "anchors": s["anchors"],
            "codeSymbols": st.get("code_symbols", 0), "commits": st.get("commits", 0),
            "lessons": st.get("lessons", 0),
            "gitError": st.get("git_error") or "",
        }))
        return 0
    print(_c("✓ indexed", C.GREEN) + f"  →  {_c(str(s['nodes']), C.B)} nodes, "
          f"{s['anchors']} anchors")
    parts = []
    for k, label in [("code_symbols", "code symbols"), ("commits", "commits"),
                     ("trailers", "stamped commits"), ("lessons", "lessons")]:
        if st.get(k):
            parts.append(f"{st[k]} {label}")
    print(_c("  " + " · ".join(parts), C.DIM))
    if st.get("git_error"):
        print(_c(f"  ⚠ git could not read this repo: {st['git_error']}", C.MUSTARD))
        print(_c("    (commit/trailer history was skipped — code map + docs still indexed)", C.DIM))
    if st.get("skipped_commits"):
        print(_c(f"  ⚠ {st['skipped_commits']} commit(s) skipped (malformed)", C.MUSTARD))
    print(_c(f"  stored in {idx_path}", C.DIM))
    # Seed the in-the-path state block from day one (the adoption fix) — so the AI carries
    # recall's memory in its system prompt before the first commit, not only after. The
    # post-commit hook keeps it fresh thereafter. Best-effort: never fail init over it.
    try:
        block = idx.render_state_block()
        wrapped = f"{idx.STATE_BEGIN}\n{block}\n{idx.STATE_END}"
        seed = repo / ("CLAUDE.md" if (repo / "CLAUDE.md").exists() else "AGENTS.md")
        existing = seed.read_text(encoding="utf-8") if seed.exists() else ""
        if idx.STATE_BEGIN not in existing:
            seed.write_text((existing.rstrip() + "\n\n" if existing.strip() else "")
                            + wrapped + "\n", encoding="utf-8")
            print(_c(f"  ↳ recall state seeded into {seed.name} (loads into your AI's context)", C.DIM))
    except Exception:
        pass
    return 0


def cmd_recall(args) -> int:
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    res = idx.recall(args.query, edit_context=args.context, topk=args.topk, consumer="cli")
    # --terse: the agent/Bash path (machine caller) — render the prompt block in
    # compressed form. --for-prompt: the rich web-AI block. Bare: pretty terminal.
    if getattr(args, "terse", False):
        print(_format_for_prompt(args.query, res, terse=True))
        return 0
    if args.for_prompt:
        print(_format_for_prompt(args.query, res))
        return 0
    _print_pretty(args.query, res)
    return 0 if not res["silenced"] else 0


def cmd_brief(args) -> int:
    """Wave A — the Pre-Edit Briefing. Everything recall knows about ONE file, before
    you touch it: why it is the way it is, what breaks, which open tasks affect it."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    b = idx.brief(args.file)
    if getattr(args, "terse", False):
        print(_format_brief_for_prompt(b, terse=True))
        return 0
    if args.for_prompt:
        print(_format_brief_for_prompt(b))
        return 0
    _print_brief(b)
    return 0


def _working_diff_files(repo) -> list[str]:
    """The tracked files changed vs HEAD (staged + unstaged) — the working diff, for
    `recall push --diff`. Quotepath off so non-ASCII paths match the index."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", str(repo),
             "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        return [ln.strip().replace("\\", "/") for ln in out.stdout.splitlines() if ln.strip()]
    except OSError:
        return []


def cmd_push(args) -> int:
    """Workstream A — the SITUATIONAL push for a subagent or a hookless harness: print the
    scoped brief + landmines + live BROKEN trust-status for what you're about to do (a --file,
    the working --diff, and/or a --task). An installed Claude Code session gets this automatically
    on prompt-submit; `recall push` is the manual / subagent path. Read-only, model-free — but,
    unlike the cached state block, this is FRESH tokens, so keep the scope tight. With no
    --file/--diff/--task it degrades to the repo-static state block (the universal floor)."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    diff_files = _working_diff_files(repo) if getattr(args, "diff", False) else None
    block = idx.render_situational_block(
        focus_file=getattr(args, "file", None),
        diff_files=diff_files,
        task=getattr(args, "task", None),
    )
    print(block)  # always a paste-in block; --terse/--for-prompt are accepted aliases
    return 0


def cmd_receipt(args) -> int:
    """Money-receipt (workstream C) — the loop recall was IN over a rolling window, in MEASURED
    units (counts only — NO token/$ estimate; that ships later, walled under receipt['modeled']).
    Read-only, 0 model tokens. License-gated; the dashboard card is the surface for an
    unsigned/trial-expired user, so this is NOT marketed as always-on."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    r = idx.receipt(window_days=getattr(args, "days", 14))
    if getattr(args, "json", False):
        import json as _json
        print(_json.dumps(r, indent=2))
        return 0
    m = r["measured"]
    print(_c("recall · receipt", C.B)
          + _c(f"  last {r['window_days']} days — MEASURED (counts only)", C.DIM))
    print(f"  briefed edits (ack'd before editing): {_c(str(m['briefed_edits']), C.GREEN)}"
          + _c(f"  across {m['distinct_files_briefed']} file(s)", C.DIM))
    print(f"  recall consulted: {_c(str(m['recall_calls']), C.GREEN)} call(s)"
          + _c(f"  ({m['surfaced_calls']} surfaced a hit)", C.DIM))
    if m["total_events"] == 0:
        print(_c("  nothing yet — the receipt grows as you work (brief / ack / recall while coding).", C.DIM))
    else:
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(m["per_kind"].items(), key=lambda x: -x[1]))
        print(_c(f"  by kind: {kinds}", C.DIM))
    # workstream E: per-call EMITTED size (the context tax). tokens ≈ chars/4 (we store the SIZE,
    # not the text, so this is the chars/4 estimate — never tiktoken here). Recall-ABSOLUTE; the
    # "% of session" line prints ONLY when --session-tokens supplies the denominator (never invented).
    emitted = r.get("emitted") or {}
    if emitted:
        print(_c("  emitted to context (per-call, recall-absolute):", C.DIM))
        total_chars = 0
        for consumer, e in sorted(emitted.items(), key=lambda x: -x[1]["chars"]):
            total_chars += e["chars"]
            print(_c(f"    {consumer}: {e['serves']} serve(s), ~{e['chars'] // 4} tok "
                     f"(chars/4 of {e['chars']})", C.DIM))
        st = getattr(args, "session_tokens", None)
        if st:
            pct = 100.0 * (total_chars // 4) / max(1, int(st))
            print(_c(f"  recall = {pct:.1f}% of the {int(st)}-token session "
                     f"(per-call emitted ÷ the session total you supplied)", C.DIM))
    return 0


def cmd_graph(args) -> int:
    """Dump the stamped graph as JSON for the desktop app (the CORE view).

    Read-only, 0 model tokens — two SELECTs over the v11 schema, projected down to a
    FILE graph (the app keys nodes by file id). The app computes ALL the rest itself
    (3D layout, layers, blast/hopMap/importance), so the engine only ships raw
    {nodes, edges}. The SAME command later feeds the brain dataset via --kind.

    Honest-by-construction safety: every drift level and edge kind is collapsed into
    the app's CLOSED sets (fresh/moved/broken/gone · depends/co/guards/relates) here,
    so the renderer never meets an unknown enum key (the prior crash root-cause)."""
    import json as _json
    from recall import __version__ as ver

    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        # no index → a CLEAN fallback signal, not an error (exit 0): the app shows seed.
        print(_json.dumps({"ok": False, "reason": "no-index", "engine": "recall", "version": ver}))
        return 0

    db = idx.db
    cap = max(10, min(400, getattr(args, "limit", 140) or 140))

    # drift:<node_id> → the app's freshness verdict (closed set). committed/uncommitted
    # both read as "moved" (the file drifted from where the claim was stamped).
    DRIFT_TO_FRESH = {"fresh": "fresh", "committed": "moved", "uncommitted": "moved", "broken": "broken"}
    drift = {}
    for r in db.execute("SELECT key, value FROM meta WHERE key LIKE 'drift:%'"):
        drift[r[0].split(":", 1)[1]] = DRIFT_TO_FRESH.get(r[1], "fresh")

    # edge kind → the app's EDGE_COLOR set (depends/co/guards/relates). Anything the
    # engine adds later falls to "relates" (safe default), never an unknown key.
    KIND_MAP = {
        "depends_on": "depends", "implements": "depends", "calls": "depends",
        "co_changed": "co",
        "guarded_by": "guards", "guards": "guards", "warns_about": "guards",
        "relates_to": "relates", "supersedes": "relates", "decided_by": "relates", "presents": "relates",
    }

    norm = lambda p: p.replace("\\", "/") if p else p  # noqa: E731

    # generated / throwaway dirs that are NOT real architecture — they bury the
    # actual code-graph in noise (e.g. proof/_runs/* fixtures = 40% of nodes on this
    # repo) and skew importance/blast. Filtered out of the file-graph by path prefix.
    NOISE_PREFIXES = ("proof/", "experiments/", ".venv/", "node_modules/", "dist/",
                      "build/", ".mind/", "__pycache__/", ".git/")
    NOISE_MARKERS = ("/_runs/", "past-incident", "/run_with/", "/run_without/")

    def _is_noise(path: str) -> bool:
        p = path.lstrip("./")
        return p.startswith(NOISE_PREFIXES) or any(m in path for m in NOISE_MARKERS)

    # PHANTOM-NODE GUARD (2026-06-20): a few code-symbol rows carry a file_path that is a
    # BASENAME or a non-file token (e.g. 'cli.py' instead of 'recall/cli.py', 'ADR-008',
    # 'decisions.md') — stamp-time artifacts. They surface as ghost top-level "files" that
    # the app then reads as their OWN sub-system, fracturing the cluster layout (Welle B
    # derives sub-system from the path's first segment). Drop any node whose file_path
    # doesn't resolve to a real file in the repo. Cheap: one set built from os.walk once.
    from pathlib import Path as _P
    _real_files: set[str] = set()
    try:
        _root = _P(repo).resolve()
        for _dp, _dns, _fns in os.walk(_root):
            # prune heavy/irrelevant dirs in-place (mirrors NOISE_PREFIXES) so the walk is fast
            _dns[:] = [d for d in _dns if d not in
                       ("node_modules", ".git", ".venv", "dist", "build", "__pycache__", ".next", ".mind")]
            for _fn in _fns:
                _real_files.add((_P(_dp) / _fn).resolve().relative_to(_root).as_posix())
    except OSError:
        _real_files = set()  # can't walk → don't filter (degrade to old behaviour, never crash)

    def _is_real_file(path: str) -> bool:
        # empty set = walk failed → keep everything (safe degrade). Otherwise require a hit.
        return (not _real_files) or (path in _real_files)

    # ── nodes: code files only (the code-graph). Group code-symbol rows by file_path,
    #    take the max importance + the file's node-id (for drift lookup). private out. ──
    file_rows: dict[str, dict] = {}
    for r in db.execute(
        "SELECT id, file_path, importance, visibility FROM nodes "
        "WHERE kind='code-symbol' AND file_path IS NOT NULL AND file_path <> ''"
    ):
        if (r[3] or "team") == "private":
            continue
        f = norm(r[1])
        if _is_noise(f):
            continue
        if not _is_real_file(f):
            continue   # phantom: basename-only / non-file token, not a real repo file
        cur = file_rows.get(f)
        imp = r[2] if r[2] is not None else 0.0
        fresh = drift.get(str(r[0]), "fresh")
        if cur is None:
            file_rows[f] = {"id": f, "imp": imp, "fresh": fresh}
        else:
            if imp > cur["imp"]:
                cur["imp"] = imp
            if fresh != "fresh":  # the loudest verdict on any of the file's symbols wins
                cur["fresh"] = fresh

    # ── edges: resolve src/dst to file_path, project to file→file, dedup, drop
    #    self-loops + edges touching a private/unknown file. ──
    seen_edges: set[tuple] = set()
    raw_edges: list[tuple] = []
    for r in db.execute(
        "SELECT REPLACE(ns.file_path,'\\','/') AS a, REPLACE(nd.file_path,'\\','/') AS b, e.kind "
        "FROM edges e JOIN nodes ns ON ns.id=e.src_node JOIN nodes nd ON nd.id=e.dst_node "
        "WHERE ns.file_path IS NOT NULL AND nd.file_path IS NOT NULL"
    ):
        a, b, k = r[0], r[1], r[2]
        if not a or not b or a == b:
            continue
        if a not in file_rows or b not in file_rows:
            continue
        kind = KIND_MAP.get(k, "relates")
        key = (a, b, kind)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        raw_edges.append((a, b, kind))

    # ── KNOWLEDGE: the recall MOAT — files the brain has STAMPED (lessons / decisions /
    #    tasks anchored to a file_path). This is what an editor/LSP can NEVER show: "this
    #    file carries a decision". Folded onto the file node as `knowledge` so the app draws
    #    the glow aura. Counts the loudest tag so the aura can colour by kind. ──
    KN_KINDS = ("lesson", "task")  # decisions are stored as lesson rows with a tag in recall
    know: dict[str, dict] = {}
    for r in db.execute(
        "SELECT REPLACE(file_path,'\\','/') AS f, kind, COUNT(*) FROM nodes "
        "WHERE kind IN ('lesson','task') AND file_path IS NOT NULL AND file_path <> '' "
        "AND (visibility IS NULL OR visibility <> 'private') GROUP BY f, kind"
    ):
        f = norm(r[0])
        if not f or f not in file_rows:
            continue
        e = know.setdefault(f, {"lesson": 0, "task": 0})
        if r[1] in e:
            e[r[1]] += r[2]

    # ── GUARDS: a file PROTECTED by a stamped rule/lesson (guards / guarded_by /
    #    warns_about edges). In recall these run from a LESSON node (no file_path) to the
    #    code file it defends — so they don't survive the file→file edge scan above. Pull
    #    them directly: the destination file gets a shield ("a rule defends this"). ──
    guarded: set[str] = set()   # files defended by a stamped rule/lesson
    for r in db.execute(
        "SELECT REPLACE(nd.file_path,'\\','/') AS f FROM edges e "
        "JOIN nodes nd ON nd.id=e.dst_node "
        "WHERE e.kind IN ('guards','guarded_by','warns_about') "
        "AND nd.file_path IS NOT NULL AND nd.file_path <> ''"
    ):
        f = norm(r[0])
        if f and f in file_rows:
            guarded.add(f)

    # ── cap to a readable size (the seed is ~100 nodes; 1700 files is unreadable).
    #    rank by importance + degree, keep the top `cap`, then keep only edges whose
    #    BOTH endpoints survived the cap. ──
    deg: dict[str, int] = {}
    for a, b, _ in raw_edges:
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    ranked = sorted(file_rows.values(), key=lambda n: (n["imp"] + deg.get(n["id"], 0) * 8.0), reverse=True)
    keep = {n["id"] for n in ranked[:cap]}

    def _node(n: dict) -> dict:
        f = n["id"]
        out = {"id": f, "kind": "code", "fresh": n["fresh"],
               "importance": round(n["imp"] / 100.0, 4) if n["imp"] else 0.0}
        kn = know.get(f)
        if kn and (kn["lesson"] or kn["task"]):
            # tag = the dominant knowledge kind (lesson wins ties — it's the moat signal)
            out["knowledge"] = {"lessons": kn["lesson"], "tasks": kn["task"],
                                "tag": "decision" if kn["lesson"] else "task"}
        if f in guarded:
            out["guarded"] = True
        return out

    nodes = [_node(n) for n in ranked[:cap]]
    edges = [[a, b, k] for (a, b, k) in raw_edges if a in keep and b in keep]

    print(_json.dumps({"ok": True, "engine": "recall", "version": ver,
                       "nodes": nodes, "edges": edges}))
    return 0


def cmd_ack(args) -> int:
    """Acknowledge a file's pre-edit briefing so the hard gate lets the edit through.

    The PreToolUse gate (adapters/hook.py) DENIES an edit to a file recall has knowledge
    about until this runs — proving the briefing was seen, not skimmed. The ack is
    per-file + time-boxed (editgate.ACK_TTL_S); a stamp that changes the file's knowledge
    clears it so the next edit re-briefs against the new truth. Read-only, 0 model tokens."""
    from recall import editgate

    repo = _repo_from_args(args)
    mind = _index_path(repo).parent
    # Workstream C: the ack is the highest-signal "briefed before edit" event — log it (once
    # per cmd_ack, unconditionally; cmd_ack has NO is_acked short-circuit) as a usage row so the
    # receipt can MEASURE the loop. The gate's is_acked fast-path stays DB-free (JSON-only); the
    # logging lives here, on the explicit ack path. Best-effort — never blocks the ack.
    idx = _open_existing(repo)
    log_cb = None
    if idx is not None:
        rel = args.file.replace("\\", "/")
        log_cb = lambda: idx._log(rel, None, 0.0, 1, 0, "ack", kind="ack")  # noqa: E731
    editgate.ack(mind, repo, args.file, log=log_cb)
    print(_c("✓ acknowledged", C.GREEN)
          + _c(f"  {args.file} — the edit gate will let your next edit through "
               f"(~{editgate.ACK_TTL_S // 60} min).", C.DIM))
    return 0


def cmd_contested(args) -> int:
    """Wave B — uncertainty hotspots: the code the team kept changing (high churn AND
    entanglement). Answers 'where does the team burn time', model-free (ADR-019)."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    spots = idx.contested_spots(repo=repo, limit=args.limit, min_churn=args.min_churn)
    if not spots:
        print(_c("recall · contested", C.B) + _c("  no hotspots — too little git history, or run `recall init`", C.DIM))
        return 0
    print(_c("recall · contested spots", C.B) + _c(f"  ({repo.name}) — where the team burns time", C.DIM))
    print(_c(f"  {'score':>6}  {'churn':>5}  {'tangle':>6}  file", C.DIM))
    for s in spots:
        bar = "█" * min(20, int(s["score"] / 4)) if s["score"] else ""
        score = _c(f"{s['score']:>6}", C.MUSTARD)
        print(f"  {score}  {s['churn']:>5}  {s['entanglement']:>6}  {s['file']}  {_c(bar, C.DIM)}")
    print(_c("  churn = commits that touched it · tangle = files that move with it", C.DIM))
    return 0


def cmd_resolve(args) -> int:
    """Search-inversion (ADR-037): correct a hallucinated search term into THIS
    repo's real vocabulary before you grep. `recall resolve seatLimit` →
    'this repo means confirmSeatOrRollback'. --terse for the agent/Bash path,
    --for-prompt for a paste-in block. Read-only, 0 model tokens."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    warmth = None if args.warmth is None else max(0.0, min(1.0, args.warmth))
    res = idx.resolve(args.guess, warmth=warmth, top=args.top)
    if getattr(args, "terse", False) or args.for_prompt:
        print(_format_resolve_for_prompt(res))
        return 0
    _print_resolve(res)
    return 0


def cmd_precedent(args) -> int:
    """Arrow 3 — precedent: the most analogous PAST decisions for a situation you're about
    to act in, each with its outcome (superseded? became a landmine? drifted?). Answers
    'have we been here before, and how did it go?' Read-only, 0 model tokens."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    res = idx.precedent(args.situation, limit=args.limit, consumer="cli")
    if getattr(args, "terse", False) or args.for_prompt:
        print(_format_precedent_for_prompt(res))
        return 0
    _print_precedent(res)
    return 0


def cmd_impact(args) -> int:
    """The AI-native call-hierarchy replacement: 'if I touch this, what's actually affected?'
    Fuses empirical co-change (what git proves moves together) with structural dependents,
    weighted by importance. 0 model tokens — a pure SELECT over the pre-stamped graph."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    res = idx.impact(args.target, depth=args.depth, limit=args.limit, consumer="cli")
    if getattr(args, "terse", False) or args.for_prompt:
        print(_format_impact_for_prompt(res))
        return 0
    _print_impact(res)
    return 0


def cmd_callers(args) -> int:
    """Code-intel: who depends on this file/symbol (the file-granular call-hierarchy, reverse)."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    res = (idx.callees if args.callees else idx.callers)(
        args.target, depth=args.depth, limit=args.limit, consumer="cli")
    if getattr(args, "terse", False) or args.for_prompt:
        print(_format_hierarchy_for_prompt(res))
        return 0
    _print_hierarchy(res)
    return 0


def cmd_dead_code(args) -> int:
    """Code-intel: code files nothing in the graph imports — dead-code CANDIDATES (verify)."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    res = idx.dead_code(limit=args.limit, consumer="cli")
    if getattr(args, "terse", False) or args.for_prompt:
        print(_format_listing_for_prompt(res, "dead-code", "candidates",
              "code files nothing imports (candidates — verify; dynamic imports invisible)"))
        return 0
    _print_listing(res, "dead-code", "candidates",
                   "code files nothing imports — CANDIDATES, verify before deleting", C.MUSTARD)
    return 0


def cmd_untested(args) -> int:
    """Code-intel: code files with no recorded test edge."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    res = idx.untested(limit=args.limit, consumer="cli")
    if getattr(args, "terse", False) or args.for_prompt:
        print(_format_listing_for_prompt(res, "untested", "untested",
              "code files with no recorded test edge (file-granular)"))
        return 0
    _print_listing(res, "untested", "untested",
                   "code files with no recorded test edge", C.CYAN)
    return 0


def cmd_cycles(args) -> int:
    """Code-intel: file→file import cycles in the depends_on graph."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    res = idx.cycles(limit=args.limit, consumer="cli")
    if getattr(args, "terse", False) or args.for_prompt:
        print(_format_cycles_for_prompt(res))
        return 0
    _print_cycles(res)
    return 0


def cmd_explain(args) -> int:
    """Wave C — "explain me this repo" (ADR-020). The generated orientation path a new
    dev or a fresh AI session needs: load-bearing files, must-know decisions, what's in
    progress, where time burns. Read-only, model-free."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    o = idx.onboarding()
    if getattr(args, "terse", False):
        print(_format_explain_for_prompt(o, repo.name, terse=True))
        return 0
    if args.for_prompt:
        print(_format_explain_for_prompt(o, repo.name))
        return 0
    _print_explain(o, repo.name)
    return 0


def cmd_sync_context(args) -> int:
    """THE ADOPTION FIX (2026-06-17): write recall's live STATE block into the repo's
    AI instruction files (CLAUDE.md / AGENTS.md / .cursor/rules / copilot-instructions),
    so EVERY client loads it into the system prompt with no tool call, on every turn —
    recall becomes the air in the room instead of a tool the AI must remember to call.

    Idempotent: the block lives between markers; we replace it, never touching the user's
    own content. If no instruction file exists yet, we create AGENTS.md (the cross-client
    standard). Read-only against the index, 0 model tokens. Called by the post-commit hook
    so the state regenerates itself; also runnable by hand."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        if not getattr(args, "quiet", False):
            print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    block = idx.render_state_block()
    wrapped = f"{idx.STATE_BEGIN}\n{block}\n{idx.STATE_END}"

    # Candidate instruction files every major client loads into its system prompt.
    # Write into every AI instruction file already present, AND seed the right file for any
    # client whose config DIRECTORY exists (a Cursor/Copilot user who hasn't written rules yet).
    instruction_files = ["CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md",
                         ".cursor/rules/recall.mdc"]
    targets = {repo / c for c in instruction_files if (repo / c).exists()}
    if (repo / ".cursor").is_dir():
        targets.add(repo / ".cursor/rules/recall.mdc")   # Cursor reads .cursor/rules/*.mdc
    if (repo / ".github").is_dir():
        targets.add(repo / ".github/copilot-instructions.md")  # Copilot reads this
    if not targets:
        # nothing yet — AGENTS.md is the emerging cross-client standard (Claude/Cursor/Codex).
        targets = {repo / "AGENTS.md"}
    targets = sorted(targets)

    import re as _re
    pat = _re.compile(_re.escape(idx.STATE_BEGIN) + r".*?" + _re.escape(idx.STATE_END), _re.S)
    written = []
    for path in targets:
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError:
            existing = ""
        if idx.STATE_BEGIN in existing:
            updated = pat.sub(wrapped, existing)  # replace the old block in place
        elif existing.strip():
            updated = existing.rstrip() + "\n\n" + wrapped + "\n"  # append below user content
        else:
            updated = wrapped + "\n"
        if updated != existing:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(updated, encoding="utf-8")
                written.append(path)
            except OSError as e:
                if not getattr(args, "quiet", False):
                    print(_c(f"  could not write {path.name}: {e}", C.DIM))
    if not getattr(args, "quiet", False):
        if written:
            names = ", ".join(p.name for p in written)
            print(_c(f"✓ recall state synced into {names}", C.GREEN))
        else:
            print(_c("recall state already current — nothing to update", C.DIM))
    return 0


def cmd_review(args) -> int:
    """Wave D — review a change (ADR-021). `recall review <sha>` bundles, per file the
    commit touched, what brief() shows for one file (what breaks / why / open tasks /
    drift) and singles out the RISK files (load-bearing + many dependents + open task).
    `--for-prompt` renders a PR-markdown block. Read-only, model-free."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    r = idx.review(args.sha, repo=repo)
    if not r["files"]:
        print(_c("recall · review", C.B) + _c(f"  {args.sha or 'HEAD'} — no tracked files in this change", C.DIM))
        return 0
    if args.for_prompt:
        print(_format_review_markdown(r))
        return 0
    _print_review(r)
    return 0


def cmd_precommit_check(args) -> int:
    """Wave D — the pre-commit warning. Reviews the STAGED files (no commit yet) and warns
    on any risk file. Always exits 0 — it warns, it never blocks the commit (a memory tool
    must never get between you and git)."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        return 0  # no memory yet — nothing to warn about, never block
    import subprocess
    try:
        # -c core.quotepath=false: without it git C-quotes any path byte >0x7F, so a
        # staged `Grüße.py` comes back as the literal `"Gr\303\274\303\237e.py"` and never
        # matches the index's real path — the risk warning silently misses non-ASCII files.
        # (P2 bug-hunt 2026-06-15; same fix as the _git helpers elsewhere.)
        out = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", str(repo),
             "diff", "--cached", "--name-only"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        staged = [ln.strip().replace("\\", "/") for ln in out.stdout.splitlines() if ln.strip()]
    except OSError:
        return 0
    if not staged:
        return 0
    r = idx.review(files=staged)
    if r["risk_files"]:
        print(_c("recall · pre-commit", C.MUSTARD) + _c("  you are touching load-bearing code:", C.B))
        for rf in r["risk_files"]:
            why = ", ".join(rf["reasons"])
            print(f"  {_c('⚠', C.MUSTARD)} {rf['file']} {_c('— ' + why, C.DIM)}")
        print(_c("  `recall brief <file>` for the full briefing before you commit.", C.DIM))
    # forgotten status flips (Owner dogfood 2026-06-12: "massenweise offene tasks" that
    # were long finished). One line per candidate, every commit, until the file is flipped
    # — structurally impossible to miss, still warn-only.
    from recall.tasks import flip_candidates
    flips = flip_candidates(idx)
    if flips:
        print(_c("recall · tasks", C.MUSTARD)
              + _c(f"  {len(flips)} open task{'s' if len(flips) > 1 else ''} look{'' if len(flips) > 1 else 's'} finished (every step resolved) — flip status: done:", C.B))
        for f in flips[:6]:
            print(f"  {_c('✓', C.MUSTARD)} {f['title'][:70]} {_c('— ' + (f['file'] or ''), C.DIM)}")
        if len(flips) > 6:
            print(_c(f"  … and {len(flips) - 6} more (dashboard → Tasks)", C.DIM))
    # predicate nudge (workstream B): if a staged file recall has a WHY for still carries no
    # re-check, propose a free one derived from its diff. Model-free, PRINT-ONLY, never blocks,
    # and NEVER amends the commit message (that stays between you and git). Gated by
    # rules.predicate_nudge; at most one nudge per commit (aggressively quiet).
    if idx.rules.predicate_nudge:
        for f in staged:
            claim = idx.db.execute(
                "SELECT 1 FROM nodes WHERE REPLACE(file_path,'\\','/')=? "
                "AND kind NOT IN ('code-symbol','task','file') LIMIT 1", (f,)).fetchone()
            if not claim:
                continue  # no stamped why for this file — nothing to re-check
            has_pred = idx.db.execute(
                "SELECT 1 FROM nodes WHERE REPLACE(file_path,'\\','/')=? "
                "AND predicate IS NOT NULL AND predicate != '' LIMIT 1", (f,)).fetchone()
            if has_pred:
                continue  # already carries a check — don't nag
            try:
                d = subprocess.run(
                    ["git", "-c", "core.quotepath=false", "-C", str(repo),
                     "diff", "--cached", "-U0", "--", f],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
                added = [ln[1:] for ln in d.stdout.splitlines()
                         if ln.startswith("+") and not ln.startswith("+++")]
            except OSError:
                continue
            sugg = idx.suggest_predicate_from_diff(f, added)
            if sugg:
                print(_c("recall · predicate", C.MUSTARD)
                      + _c(f"  {f} has a stamped why but no re-check — add a free one:", C.B))
                print(_c(f"  Recall-predicate: {sugg}", C.DIM)
                      + _c("  (a contains:/absent: check freshen re-runs every commit; warn-only)", C.DIM))
                break  # one nudge per commit
    return 0  # warn only, never block


def _staged_files(repo: Path) -> list[str]:
    """The staged paths for this commit ('/'-normalized), or [] on any git error."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", str(repo),
             "diff", "--cached", "--name-only"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        return [ln.strip().replace("\\", "/") for ln in out.stdout.splitlines() if ln.strip()]
    except OSError:
        return []


def cmd_check_leak(args) -> int:
    """The LEAK GUARD — unlike precommit-check (which only warns), this BLOCKS.

    Refuses a commit that would leak private knowledge: any staged SQLite brain file
    (`.mind/*.db`, or any *.db that is a recall index) is opened and checked for
    nodes marked visibility='private'. A hit returns non-zero so the pre-commit hook
    aborts the commit (owner: "100% wasserfeste sichere rule"). Honors
    [share].block_raw_mind_commit — but FAIL-CLOSED: the guard is skipped only on an
    explicit `block_raw_mind_commit = false`, never by a missing/broken config.

    A private brain belongs on YOUR machine; share via `recall export` (which strips
    private nodes and verifies the result clean) — never the raw index."""
    repo = _repo_from_args(args)
    from recall.config import load_build_config
    if not load_build_config(repo).block_raw_mind_commit:
        return 0  # owner explicitly opted out of the guard
    staged = _staged_files(repo)
    # candidate brain files: anything that looks like a recall SQLite index
    import sqlite3
    offenders: list[str] = []
    for rel in staged:
        if not (rel.endswith(".db") or "/.mind/" in f"/{rel}"):
            continue
        fp = (repo / rel)
        if not fp.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{fp}?mode=ro", uri=True)
            # is it a recall brain (has a nodes table with a visibility column)?
            cols = {r[1] for r in con.execute("PRAGMA table_info(nodes)").fetchall()}
            if "visibility" in cols:
                n = con.execute(
                    "SELECT COUNT(*) FROM nodes WHERE visibility='private'"
                ).fetchone()[0]
                if n:
                    offenders.append(f"{rel} — {n} private node(s)")
            con.close()
        except sqlite3.Error:
            continue  # not a readable recall brain — not our concern
    if offenders:
        print(_c("✗ BLOCKED — a staged brain file holds PRIVATE notes:", C.RED))
        for o in offenders:
            print(_c(f"    {o}", C.RED))
        print(_c("  Private knowledge must not enter git. Share via "
                 "`recall export --out <file>` (it strips private nodes and verifies "
                 "the copy clean), then commit THAT — never the raw index.", C.DIM))
        print(_c("  (To opt out, set block_raw_mind_commit = false in "
                 ".recall/config.toml [share].)", C.DIM))
        return 1
    return 0


def cmd_stamp(args) -> int:
    repo = _repo_from_args(args)
    idx = _open_existing(repo) or Index.open(_index_path(repo), repo=repo)
    try:
        # --private always wins; otherwise the project's configured default
        # (.recall/config.toml [share].default_visibility — 'team' if unset).
        from recall.config import load_build_config
        default_vis = load_build_config(repo).default_visibility
        visibility = "private" if getattr(args, "private", False) else default_vis
        r = idx.stamp(
            title=args.title,
            body=args.body,
            anchors=args.anchors.split(",") if args.anchors else None,
            tags=args.tags.split(",") if args.tags else None,
            file_path=args.file,
            line=getattr(args, "line", None),
            predicate=args.predicate,
            outcome=getattr(args, "outcome", None),
            visibility=visibility,
            update_id=getattr(args, "update_id", None),
            origin="live",
        )
    except ValueError as e:  # unparseable predicate / bad --id — fail loud
        if getattr(args, "json", False):
            import json as _json
            print(_json.dumps({"ok": False, "reason": str(e)}))
            return 1
        print(_c(f"✗ {e}", C.RED))
        return 1
    # New knowledge on a file invalidates its edit-gate ack: the next edit must re-brief
    # against the now-current truth (e.g. a fresh landmine the agent hasn't seen).
    if args.file:
        from recall import editgate
        editgate.clear(_index_path(repo).parent, repo, args.file)
    # --json: machine-readable for the desktop app's Stamp console.
    if getattr(args, "json", False):
        import json as _json
        print(_json.dumps({
            "ok": True, "action": r["action"], "nodeId": r.get("node_id"),
            "into": r.get("into"), "anchors": r.get("anchors", 0),
            "private": visibility == "private", "title": args.title,
        }))
        return 0
    if r["action"] == "MERGE":
        print(_c(f"✓ merged into: {r['into']}", C.GREEN) + _c(f"  (#{r['node_id']} · overlap {r['overlap']})", C.DIM))
    elif r["action"] == "UPDATE":
        print(_c(f"✓ updated #{r['node_id']}", C.GREEN) + _c(f"  ({r['anchors']} anchors)", C.DIM))
    else:
        print(_c(f"✓ stamped #{r['node_id']}", C.GREEN) + _c(f"  ({r['anchors']} anchors)", C.DIM))
    if visibility == "private":
        why = "" if getattr(args, "private", False) else " (project default)"
        print(_c(f"  🔒 private{why}", C.DIM)
              + _c("  — stays in this brain; recall export leaves it out", C.DIM))
    if getattr(args, "predicate", None):
        print(_c(f"  predicate: {args.predicate}", C.DIM)
              + _c("  — re-checked free on every freshen()", C.DIM))
    if getattr(args, "outcome", None):
        print(_c(f"  outcome: {args.outcome}", C.DIM))
    return 0


def cmd_handoff(args) -> int:
    """Docking point #4 — session handoff (docs/ecosystem-docking.md). When a session
    is about to compact/reset, stamp WHAT IS IN FLIGHT as a recall node so the next
    session rebuilds the state FROM RECALL (`recall explain` + the per-file brief)
    instead of an ad-hoc summary that dies with the context.

    It's a thin, opinionated wrapper over stamp: kind=lesson, tagged
    handoff/session so it surfaces in `recall explain` and in `recall "session
    handoff"`, AND anchored to each in-flight file (passed via --files) so it shows
    up in the pre-edit brief of exactly those files next session. The standing TASK
    LAW still owns durable instructions; handoff captures the volatile 'where I am
    right now' that isn't a task."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo) or Index.open(_index_path(repo), repo=repo)
    files = [f.strip().replace("\\", "/") for f in (args.files or "").split(",") if f.strip()]
    title = args.summary if len(args.summary) <= 90 else args.summary[:87] + "…"
    n = 0
    # stamp once per file so the per-file brief surfaces it on each (stamp pins
    # file_path from a known-file path anchor, fix f631244); if no files, one node.
    targets = files or [None]
    for fp in targets:
        # Each per-file node anchors on the fixed handoff terms + ONLY ITS OWN file
        # path — NOT every in-flight file. If a node carried all the file paths as
        # anchors, brief's "file_path ∪ anchored-by-path-term" union would surface
        # the SAME handoff twice on a file (once via its own file_path, once via the
        # shared cross-file anchor). One file per node keeps each brief clean.
        anchors = ["handoff", "session-handoff", "in-flight"] + ([fp] if fp else [])
        # dedup=False: a handoff is a point-in-time SNAPSHOT, not a fact to merge
        # into an older one — and the shared handoff anchors would otherwise collapse
        # the per-file nodes into one. Old handoffs age out via freshness/drift.
        idx.stamp(
            title=title,
            body=args.summary if args.summary != title else None,
            anchors=anchors,
            tags=["handoff", "session", "lesson"],
            file_path=fp,
            kind="lesson",
            origin="live",
            dedup=False,
        )
        n += 1
    print(_c(f"✓ handoff stamped ({n} node{'s' if n != 1 else ''})", C.GREEN)
          + _c(f"  — next session: `recall explain` + brief on {', '.join(files) or 'the repo'}", C.DIM))
    return 0


def cmd_freshen(args) -> int:
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    print(_c(f"recall · checking drift against {repo} …", C.DIM))
    try:
        s = idx.freshen()
    except sqlite3.OperationalError as e:
        # busy_timeout already waited; a still-locked DB means another writer holds
        # it (a parallel init/freshen). Fail cleanly, never with a raw traceback.
        print(_c(f"  ✗ index busy: {e} — is another recall process running?", C.RED))
        return 1
    if s.get("no_git"):
        print(_c("  ⚠ no .git — drift falls back to file existence only", C.MUSTARD))
    print(
        _c("✓ freshened", C.GREEN)
        + f"  →  {_c(str(s['checked']), C.B)} pinned nodes checked"
    )
    line = (
        "  "
        + _c(f"● {s['fresh']} fresh", C.GREEN) + _c("  ·  ", C.DIM)
        + _c(f"● {s['committed']} drifted", C.MUSTARD) + _c("  ·  ", C.DIM)
        + _c(f"● {s['uncommitted']} edited", C.RED)
    )
    # 🔴 BROKEN (arrow 1): a claim whose own re-runnable check now FAILS. Only shown when
    # there is at least one — a predicate-free repo never sees this count and reads exactly
    # as it always did. This is the signal SHA-drift is blind to (wrong-from-start claims).
    if s.get("broken"):
        line += _c("  ·  ", C.DIM) + _c(f"🔴 {s['broken']} broken", C.RED)
    print(line)
    # Wave E — decisions whose referenced code has moved on a lot since they were stamped.
    try:
        stale = idx.stale_decisions()
    except Exception:
        stale = []
    if stale:
        print(_c(f"\n  ⚠ {len(stale)} decision(s) may be stale — their code changed a lot since:", C.MUSTARD))
        for d in stale[:5]:
            busiest = d["stale_files"][0]["commits_since"] if d["stale_files"] else 0
            print(f"    {_c('▸', C.MUSTARD)} {d['title'][:64]} {_c(f'(+{busiest} commits on its code)', C.DIM)}")
    return 0


def cmd_mcp(args) -> int:
    """Run the MCP stdio server (Phase M) — recall as native tools in any MCP client.

    No output on stdout besides protocol messages (spec rule), so all human-facing
    text lives behind --print-config, which never starts the server."""
    if args.print_config:
        snippet = '{"mcpServers": {"recall": {"command": "recall", "args": ["mcp"]}}}'
        print(_c("recall · MCP — plug the project memory into your AI client", C.B))
        print(_c("\n  Every MCP client uses the SAME server shape — only the config", C.DIM))
        print(_c("  location differs. Pick yours:", C.DIM))

        print(_c("\n  Claude Code (run inside the project):", C.DIM))
        print("    claude mcp add recall -- recall mcp")
        print(_c("    or check in for the team — .mcp.json at the repo root:", C.DIM))
        print(f"    {snippet}")

        print(_c("\n  Cursor — ~/.cursor/mcp.json (global) or .cursor/mcp.json (project):", C.DIM))
        print(f"    {snippet}")

        print(_c("\n  VS Code / GitHub Copilot — .vscode/mcp.json (project):", C.DIM))
        print('    {"servers": {"recall": {"command": "recall", "args": ["mcp"]}}}')

        print(_c("\n  Windsurf — ~/.codeium/windsurf/mcp_config.json · Zed · Cline · any", C.DIM))
        print(_c("  other MCP client: the same mcpServers block above.", C.DIM))

        from recall.mcp import _TOOLS  # one source of truth for the tool list — never drifts
        names = " · ".join(t["name"] for t in _TOOLS)
        print(_c(f"\n  Tools ({len(_TOOLS)}): {names}", C.DIM))
        print(_c("  Spawned subagents can't see MCP (session-scoped) — they use the CLI:", C.DIM))
        print(_c("  `recall brief <file> --terse` (the agent rule — see docs/guide agents).", C.DIM))
        print(_c("\n  Requires an index: run `recall init .` in the project once.", C.DIM))
        return 0
    from recall import mcp  # lazy, like dashboard — the CLI core stays import-light

    return mcp.serve(args.repo)


def cmd_snapshot(args) -> int:
    """Dump the WHOLE index as one JSON snapshot — the same data the dashboard's
    /api/data serves (lessons, stats, drift, code, tasks, product, git, …), via the
    SAME build_snapshot() (one source of truth, no duplicated logic). This is the
    desktop app's data seam: one call fills every tab. Read-only, 0 model tokens.

    A missing index is a CLEAN signal (exit 0, ok:false), never an error — the app
    shows its empty state, exactly like `recall stats --json`."""
    import json as _json
    from recall import __version__ as ver
    from recall import dashboard

    repo = _repo_from_args(args)
    idx_path = _index_path(repo)
    if not idx_path.exists():
        print(_json.dumps({"ok": False, "reason": "no-index", "engine": "recall",
                           "version": ver, "repo": str(repo), "name": Path(repo).name}))
        return 0
    idx = Index.open(idx_path, repo=repo)
    try:
        snap = dashboard.build_snapshot(idx, repo)
    finally:
        idx.db.close()
    snap["ok"] = True
    snap["engine"] = "recall"
    snap["version"] = ver
    print(_json.dumps(snap, ensure_ascii=False))
    return 0


def cmd_dashboard(args) -> int:
    """Start the local dashboard — the small app that makes the wiki visible."""
    from recall import dashboard

    repo = _repo_from_args(args)
    idx_path = _index_path(repo)
    if not idx_path.exists():
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    # if a dashboard is ACTUALLY serving for this repo, open it instead of
    # binding a second server on the same port (on Windows the bind would
    # otherwise succeed and clobber the run-lock). Same probe `recall tray` uses.
    info = dashboard.is_dashboard_live(repo)
    if info:
        import webbrowser

        url = f"http://{info['host']}:{info['port']}"
        print(_c(f"dashboard already running — opening {url}", C.DIM))
        if not args.no_open:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return 0
    return dashboard.serve(
        repo, idx_path, host=args.host, port=args.port, open_browser=not args.no_open,
        watch=not args.no_watch,
    )


def cmd_tray(args) -> int:
    """Run the dashboard as a background app (tray icon if available, else a loud
    DO-NOT-CLOSE console). This is what the Desktop launcher calls so closing a window
    can never silently kill the server."""
    from recall import dashboard, tray

    repo = _repo_from_args(args)
    idx_path = _index_path(repo)
    if not idx_path.exists():
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    # if a dashboard is ACTUALLY serving for this repo, just open it — don't double-bind.
    # Probe liveness (not just the lock file): a stale lock from a crash must NOT make us
    # open a dead URL and skip starting — that's the very footgun tray exists to kill.
    info = dashboard.is_dashboard_live(repo)
    if info:
        import webbrowser

        url = f"http://{info['host']}:{info['port']}"
        print(_c(f"dashboard already running — opening {url}", C.DIM))
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return 0
    return tray.run(
        repo, idx_path, host=args.host, port=args.port,
        open_browser=not args.no_open, watch=not args.no_watch,
    )


def cmd_stop(args) -> int:
    """Stop a dashboard running in the background for this project."""
    from recall import dashboard

    repo = _repo_from_args(args)
    ok, msg = dashboard.stop(repo)
    print(_c(("✓ " if ok else "") + msg, C.GREEN if ok else C.DIM))
    return 0


def cmd_login(args) -> int:
    """Sign in to whatever-recall via the browser (device-flow). Loads your license
    token so the CLI can work offline within its window. No key copy-paste."""
    from recall import login as _login
    from recall import license as L

    state = L.load_license()
    if state and state.get("verified") and not state.get("expired") and not state.get("pending"):
        who = state.get("email", "your account")
        print(_c(f"already signed in as {who}", C.GREEN)
              + _c(f"  ({state.get('days_left', 0)}d left in this window)", C.DIM))
        return 0
    result = _login.device_login(open_browser=not getattr(args, "no_browser", False))
    return 0 if result is not None else 1


def cmd_logout(args) -> int:
    """Sign out: remove the stored license token from this machine."""
    from recall import login as _login

    removed = _login.logout()
    print(_c("✓ signed out — run `recall login` to sign back in", C.GREEN) if removed
          else _c("you weren't signed in", C.DIM))
    return 0


def cmd_shortcut(args) -> int:
    """Put a double-click dashboard launcher on the Desktop — no AI, no terminal."""
    from recall import shortcut

    repo = _repo_from_args(args)
    if args.remove:
        if shortcut.remove_shortcut(repo):
            print(_c("✓ desktop launcher removed", C.GREEN))
        else:
            print(_c("no launcher on the Desktop for this project", C.DIM))
        return 0
    path = shortcut.create_shortcut(repo, port=args.port)
    print(_c(f"✓ desktop launcher written: {path}", C.GREEN))
    print(_c("  double-click it to start the dashboard — works without any AI running", C.DIM))
    return 0


def cmd_stamp_commit(args) -> int:
    """Stamp HEAD if it carries Recall-* trailers, then re-freshen — token-free.

    This is what the git post-commit hook calls (`recall hook --install`). It never
    spends a token or touches a model; it just records the commit's own trailers and
    re-checks drift. Silent-by-default so a commit hook stays quiet."""
    from adapters.hook import stamp_latest_commit

    repo = _repo_from_args(args)
    out = stamp_latest_commit(repo)
    # ADOPTION FIX (2026-06-17): re-sync the in-the-path state block on EVERY commit, so the
    # AI's instruction file always carries recall's live memory without a tool call. This is
    # the git-hook path (what `recall hook --install` writes), so it reaches every client, not
    # just Claude Code. Best-effort + silent: a sync hiccup must never disturb the commit hook.
    try:
        idx = _open_existing(repo)
        if idx is not None:
            import types as _t
            cmd_sync_context(_t.SimpleNamespace(path=str(repo), repo=None, quiet=True))
    except Exception:
        pass
    if out.get("stamped"):
        verb = "merged into" if out.get("action") == "MERGE" else "stamped"
        print(_c(f"🧠 recall {verb} {out.get('into')}", C.GREEN))
    elif args.verbose:
        print(_c(out.get("reason", "no recall trailers — nothing to stamp"), C.DIM))
    return 0


def cmd_hook(args) -> int:
    """Install / remove / show the git post-commit auto-stamp hook (write-time)."""
    from adapters.hook import (
        hook_status,
        install_post_commit,
        install_pre_commit,
        uninstall_post_commit,
        uninstall_pre_commit,
    )

    repo = _repo_from_args(args)
    # --client: the opt-in situational-push hooks into an AI client's settings.json (workstream A)
    client = getattr(args, "client", None)
    if client:
        from adapters.hook import install_client_hooks, uninstall_client_hooks
        if args.uninstall:
            r = uninstall_client_hooks(repo, client)
            print(_c(f"✓ removed recall {client} hooks", C.GREEN) if r.get("ok")
                  else _c(f"✗ {r.get('reason')}", C.RED))
        else:
            r = install_client_hooks(repo, client)
            if r.get("ok"):
                print(_c("✓ installed", C.GREEN) + _c(f"  {r.get('path')}", C.DIM))
                print(_c("  recall now PUSHES a scoped situational block on prompt-submit + session-start "
                         "(0 tool calls). Foreign hook entries were preserved.", C.DIM))
            else:
                print(_c(f"✗ {r.get('reason')}", C.RED))
        return 0 if r.get("ok") else 1
    pre = getattr(args, "pre_commit", False)
    label = "pre-commit warning" if pre else "post-commit auto-stamp"
    if args.uninstall:
        r = uninstall_pre_commit(repo) if pre else uninstall_post_commit(repo)
        if r.get("ok"):
            print(_c(f"✓ removed recall {label} hook", C.GREEN))
        else:
            print(_c(f"✗ {r.get('reason')}", C.RED))
        return 0 if r.get("ok") else 1
    if args.install:
        r = install_pre_commit(repo) if pre else install_post_commit(repo)
        if r.get("ok"):
            print(_c("✓ installed", C.GREEN) + _c(f"  {r.get('path')}", C.DIM))
            if pre:
                print(_c("  recall now warns (never blocks) before you commit load-bearing code.", C.DIM))
            else:
                print(_c("  every commit with Recall-* trailers now auto-stamps (offline).", C.DIM))
        else:
            print(_c(f"✗ {r.get('reason')}", C.RED))
        return 0 if r.get("ok") else 1
    # default: show status (both hooks)
    st = hook_status(repo)
    if not st["has_git"]:
        print(_c("no .git in this project — the commit hooks need git", C.MUSTARD))
        return 0
    post_state = _c("installed", C.GREEN) if st["installed"] else _c("not installed", C.MUSTARD)
    pre_state = _c("installed", C.GREEN) if st.get("pre_commit") else _c("not installed", C.MUSTARD)
    print(_c("recall · git hooks", C.B))
    print(f"  post-commit (auto-stamp)   {post_state}  {_c(st['path'], C.DIM)}")
    print(f"  pre-commit  (risk warning) {pre_state}  {_c(st.get('pre_commit_path', ''), C.DIM)}")
    if not st["installed"]:
        print(_c("  → `recall hook --install` to auto-stamp every commit.", C.DIM))
    if not st.get("pre_commit"):
        print(_c("  → `recall hook --install --pre-commit` to warn before risky commits.", C.DIM))
    return 0


def _git_head(repo: Path) -> dict:
    """Best-effort git branch + commit count for the app's Topbar. Never raises:
    a missing/absent git just yields blanks, so the desktop app shows the index
    facts without a fake '668 commits' string."""
    import subprocess
    out = {"branch": "", "commits": 0, "head": ""}
    try:
        run = lambda a: subprocess.run(  # noqa: E731
            ["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=4
        )
        b = run(["rev-parse", "--abbrev-ref", "HEAD"])
        if b.returncode == 0:
            out["branch"] = b.stdout.strip()
        c = run(["rev-list", "--count", "HEAD"])
        if c.returncode == 0 and c.stdout.strip().isdigit():
            out["commits"] = int(c.stdout.strip())
        h = run(["rev-parse", "--short", "HEAD"])
        if h.returncode == 0:
            out["head"] = h.stdout.strip()
    except Exception:  # noqa: BLE001 — git absent/odd checkout must not break stats
        pass
    return out


def cmd_stats(args) -> int:
    repo = _repo_from_args(args)
    idx = _open_existing(repo)

    # --json: machine-readable for the desktop app's Topbar / Index view. A missing
    # index is a CLEAN signal (exit 0, ok:false), not an error — the app shows its
    # empty state, never a fake count.
    if getattr(args, "json", False):
        import json as _json
        from recall import __version__ as ver
        if idx is None:
            print(_json.dumps({"ok": False, "reason": "no-index", "engine": "recall",
                               "version": ver, "repo": str(repo)}))
            return 0
        s = idx.stats()
        from recall.freshness import drift_counts
        d = drift_counts(idx)
        git = _git_head(repo)
        print(_json.dumps({
            "ok": True, "engine": "recall", "version": ver, "repo": str(repo),
            "name": Path(repo).name,
            "branch": git["branch"], "commits": git["commits"], "head": git["head"],
            "nodes": s["nodes"], "edges": s["edges"], "anchors": s["anchors"],
            "recalls": s["recalls"], "surfaced": s["surfaced"],
            "byKind": s["by_kind"],
            "fresh": d.get("fresh", 0), "drifted": d.get("committed", 0),
            "edited": d.get("uncommitted", 0), "broken": d.get("broken", 0),
            "freshChecked": sum(d.values()),
        }))
        return 0

    if idx is None:
        print(_c(f"no index in {repo}", C.RED))
        return 1
    s = idx.stats()
    print(_c("recall · index", C.B) + _c(f"  ({repo})", C.DIM))
    print(f"  nodes    {s['nodes']}")
    print(f"  edges    {s['edges']}")
    print(f"  anchors  {s['anchors']}")
    print(f"  recalls  {s['recalls']}  ({s['surfaced']} surfaced)")
    print(_c("  by kind: " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_kind"].items())), C.DIM))
    from recall.freshness import drift_counts
    d = drift_counts(idx)
    if sum(d.values()):  # only after a `recall freshen` has run
        line = (
            "  freshness: "
            + _c(f"{d['fresh']} fresh", C.GREEN) + ", "
            + _c(f"{d['committed']} drifted", C.MUSTARD) + ", "
            + _c(f"{d['uncommitted']} edited", C.RED)
        )
        if d.get("broken"):  # arrow 1: claims whose own check now fails (shown only when >0)
            line += ", " + _c(f"{d['broken']} 🔴 broken", C.RED)
        print(line)
    return 0


# ----------------------------------------------------- power mode (ADR-008/012)
def cmd_connect(args) -> int:
    """Connect / show / clear the AI used for Power Mode. No default (ADR-012)."""
    from recall.connect import Connection, clear_connection, load_connection, save_connection

    if args.clear:
        print(_c("✓ disconnected", C.GREEN) if clear_connection() else _c("nothing was connected", C.DIM))
        return 0
    if args.show or not args.provider:
        conn = load_connection()
        if conn is None:
            print(_c("not connected", C.MUSTARD) + _c("  — `recall connect --provider ollama --model <m>` (local, free)", C.DIM))
            print(_c("                 or `--provider anthropic --model <m>` (online, paid)", C.DIM))
            return 0
        key = f", key in ${conn.api_key_env}" if conn.api_key_env else ""
        print(_c("connected", C.GREEN) + f"  {conn.provider} · {conn.model}"
              + _c((f" · {conn.base_url}" if conn.base_url else "") + key, C.DIM))
        return 0

    # anthropic defaults its key env var; the others only get a key env if the user
    # passed one (claude-cli never needs one, custom/ollama are optional).
    key_env = args.key_env or ("ANTHROPIC_API_KEY" if args.provider == "anthropic" else None)
    if not args.model:
        print(_c("✗ a connection needs --model", C.RED)
              + _c("  (for claude-cli that's the CLI command, e.g. `claude`)", C.DIM))
        return 1
    try:
        conn = Connection(
            provider=args.provider,
            model=args.model,
            base_url=args.base_url,
            api_key_env=key_env,
        )
    except ValueError as e:
        print(_c(f"✗ {e}", C.RED))
        return 1
    save_connection(conn)
    print(_c("✓ connected", C.GREEN) + f"  {conn.provider} · {conn.model}")
    if conn.provider == "claude-cli":
        print(_c(f"  recall will run `{conn.model}` — make sure it is installed and logged in", C.DIM))
        print(_c("  (uses your existing subscription; no API key, no extra spend)", C.DIM))
    elif conn.api_key_env:
        print(_c(f"  the key is read from ${conn.api_key_env} (never stored here)", C.DIM))
    return 0


def cmd_refine(args) -> int:
    """Graph refinement: classify the static depends_on edges into implements/guarded_by
    with the connected (ideally local) AI. Write-time, reversible-by-re-run, read path
    stays LLM-free. Cheap + local-friendly (one call per source file, decomposed)."""
    from recall.connect import load_connection
    from recall.llm import get_provider
    from recall.refine import estimate_refine_cost, refine_edges

    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    have = idx.db.execute("SELECT COUNT(*) FROM edges WHERE kind='depends_on'").fetchone()[0]
    if have == 0:
        print(_c("no depends_on edges to refine", C.MUSTARD)
              + _c(" — run `recall init` (the AST graph builds them)", C.DIM))
        return 0
    conn = load_connection()
    if conn is None:
        print(_c("not connected", C.MUSTARD)
              + _c(" — `recall connect ollama` for a free local model (ADR-012)", C.DIM))
        return 1
    provider = get_provider(conn)

    # ADR-008 cost-before-spend: refine makes one paid completion per source FILE. Show the
    # estimate first and require explicit consent before spending on a paid provider — same
    # contract as `recall power` (bug-hunt MEDIUM, 2026-06-17: refine billed N completions
    # with no preview / no --yes). count_tokens never spends, so the preview is free.
    est = estimate_refine_cost(idx, provider)
    print(_c("recall · refine estimate", C.B) + _c(f"  ({provider.name} · {est.model})", C.DIM))
    print(f"  {_c(str(est.files), C.B)} files · {est.edges} edges · "
          f"~{est.input_tokens:,} in + up to {est.est_output_tokens:,} out tokens")
    cost = "free (local)" if est.est_cost_usd == 0 else f"~${est.est_cost_usd:.2f} (estimate)"
    print(_c(f"  cost: {cost}", C.MUSTARD if est.est_cost_usd else C.GREEN))
    if args.dry_run:
        print(_c("  → dry run: nothing was sent to the model", C.DIM))
        return 0
    # A paid provider must be confirmed; a free/local one (cost 0) runs after the preview.
    if est.est_cost_usd > 0 and not args.yes:
        print(_c("  → add --yes to run (this spends tokens), or --dry-run to preview only", C.DIM))
        return 0
    print(_c("recall · refine", C.B)
          + _c(f"  ({provider.name} · {provider.model}) — {have} edges", C.DIM))

    def progress(done, total):
        if done % 25 == 0 or done == total:
            print(_c(f"  [{done}/{total}] files", C.DIM))

    res = refine_edges(idx, provider, progress=progress)
    # A fully-down provider must NOT read as a healthy "refined 0 edges — no change".
    # But "0 refined" is also the LEGITIMATE result when the model classified everything as
    # plain depends_on — so only call it a failure when EVERY call errored; a partial set of
    # failures is a warning, never a hard fail (the rest of the run is valid).
    if res.call_failures and res.call_failures >= res.files_seen and res.files_seen > 0:
        print(_c(f"✗ refine failed — all {res.files_seen} model calls errored (provider down?)", C.RED)
              + _c("  nothing changed; check the connection and retry", C.DIM))
        return 1
    if res.call_failures:
        print(_c(f"⚠ partial refine — {res.call_failures}/{res.files_seen} model calls errored "
                 f"(the rest succeeded)", C.MUSTARD))
    kinds = ", ".join(f"{k}={v}" for k, v in sorted(res.by_kind.items()))
    print(_c(f"✓ refined {res.edges_refined} edges", C.GREEN)
          + f"  ({res.files_seen} files · {kinds or 'no change'})")
    if res.dropped_labels:
        print(_c(f"  {res.dropped_labels} invalid labels dropped (kept as depends_on)", C.DIM))
    return 0


def cmd_unrefine(args) -> int:
    """Reset every refined edge back to its original kind — the reverse of `recall refine`.
    Model-free, loss-free, idempotent. The reversibility refine promises."""
    from recall.refine import unrefine

    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    n = unrefine(idx)
    if n == 0:
        print(_c("nothing to unrefine", C.MUSTARD)
              + _c(" — no edges have been refined", C.DIM))
        return 0
    print(_c(f"✓ unrefined {n} edges", C.GREEN) + _c("  reset to their original kind", C.DIM))
    return 0


def cmd_power(args) -> int:
    """Power Mode: by default show the estimate and STOP. --yes runs (ADR-008)."""
    from recall.connect import load_connection
    from recall.llm import get_provider
    from recall.power import DEFAULT_TOP_N, run_power, select_hotspots, estimate_tokens

    repo = _find_repo(args.repo or args.path or ".")
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    top_n = args.top_n if args.top_n is not None else DEFAULT_TOP_N

    if args.list:
        runs = idx.list_power_runs()
        if not runs:
            print(_c("no power runs yet", C.DIM))
            return 0
        print(_c("recall · power runs", C.B) + _c(f"  ({repo})", C.DIM))
        for r in runs:
            tag = _c(" (undone)", C.DIM) if r.get("status") == "undone" else ""
            print(f"  #{r['run']}  {r.get('model','?')}  "
                  f"{r.get('nodes_added',0)} nodes, {r.get('synonyms_added',0)} synonyms{tag}")
        return 0

    # Connection gate (ADR-012): no AI connected -> do nothing, point at connect.
    conn = load_connection()
    if conn is None:
        print(_c("not connected", C.MUSTARD) + _c(" — run `recall connect` first (ADR-012: per default nothing is connected)", C.DIM))
        return 1
    provider = get_provider(conn)

    # The estimate is always shown — the ADR-008 mandate (cost before spend).
    hotspots = select_hotspots(idx, repo, scope=args.scope, top_n=top_n)
    est = estimate_tokens(idx, repo, hotspots, provider)
    print(_c("recall · power estimate", C.B) + _c(f"  ({provider.name} · {est.model})", C.DIM))
    print(f"  {_c(str(est.hotspots), C.B)} hotspots · "
          f"~{est.input_tokens:,} in + up to {est.est_output_tokens:,} out tokens")
    cost = "free (local)" if est.est_cost_usd == 0 else f"~${est.est_cost_usd:.2f} (estimate)"
    print(_c(f"  cost: {cost}", C.MUSTARD if est.est_cost_usd else C.GREEN))

    if not (args.yes or args.dry_run):
        print(_c("  → add --yes to run, or --dry-run to preview without touching the index", C.DIM))
        return 0

    if args.dry_run:
        # run against a throwaway in-memory copy of the live index so nothing persists
        mem = _memory_clone(idx, repo)
        res = run_power(mem, repo, provider=provider, scope=args.scope, top_n=top_n, dry_run=True)
        print(_c(f"  [dry-run] would add {res.nodes_added} nodes, {res.synonyms_added} synonyms, "
                 f"{res.edges_added} edges ({res.dropped_tags} junk tags / {res.dropped_edges} junk edges dropped)", C.CYAN))
        _warn_power_yield(res)
        return 0

    print(_c("  running …", C.DIM))
    res = run_power(idx, repo, provider=provider, scope=args.scope, top_n=top_n)
    print(_c(f"✓ power run #{res.run}", C.GREEN)
          + f"  +{res.nodes_added} nodes, +{res.synonyms_added} synonyms, +{res.edges_added} edges")
    _warn_power_yield(res)
    print(_c(f"  reversible: `recall undo --power-run {res.run}`", C.DIM))
    return 0


def _warn_power_yield(res) -> None:
    """Surface a schema mismatch LOUDLY. The dogfood bug was 45/50 replies silently
    discarded; this makes the provider's compliance impossible to miss after a run."""
    if getattr(res, "alt_keys_seen", None):
        seen = ", ".join(f"{k}×{n}" for k, n in sorted(res.alt_keys_seen.items()))
        print(_c(f"  ↺ model used off-schema key(s): {seen} (forgiven, but the prompt asks for \"nodes\")", C.MUSTARD))
    discarded = getattr(res, "responses_discarded", 0)
    if discarded:
        share = f"{discarded}/{res.files}"
        msg = (f"  ⚠ {share} replies yielded NOTHING (bad JSON or wrong schema). "
               f"Your provider is not returning {{\"nodes\": [...]}}.")
        print(_c(msg, C.RED))
        if discarded >= max(1, res.files // 2):
            print(_c("    more than half failed — fix the provider/prompt before trusting this run.", C.RED))


def cmd_undo(args) -> int:
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index in {repo}", C.RED))
        return 1
    if args.all:
        r = idx.undo_power_all()
        print(_c("✓ undone all power runs", C.GREEN)
              + f"  −{r['nodes_removed']} nodes, −{r['synonyms_removed']} synonyms ({r['runs']} runs)")
        return 0
    if args.power_run is None:
        print(_c("specify --power-run <N> or --all", C.RED))
        return 1
    r = idx.undo_power_run(args.power_run)
    print(_c(f"✓ undone power run #{r['run']}", C.GREEN)
          + f"  −{r['nodes_removed']} nodes, −{r['synonyms_removed']} synonyms")
    return 0


def cmd_forget(args) -> int:
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index in {repo}", C.RED))
        return 1
    r = idx.forget(args.node_id, force=args.force)
    if r["removed"]:
        print(_c(f"✓ forgot node #{r['node_id']}", C.GREEN) + _c(f"  (was origin={r['origin']})", C.DIM))
        return 0
    print(_c(f"✗ {r['reason']}", C.RED))
    return 1


def cmd_export(args) -> int:
    """Write a SHAREABLE copy of the brain that LEAVES OUT every private note.

    Your reasoning never leaves your machine unless you say so: `recall stamp
    --private` keeps a note local; `recall export` produces the brain you can commit,
    sync or hand over — with all private nodes (and their edges/anchors) removed. The
    live `.mind/index.db` is never modified."""
    repo = _repo_from_args(args)
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index in {repo}", C.RED))
        return 1
    out = Path(args.out).resolve()
    if out == _index_path(repo).resolve():
        print(_c("✗ refusing to overwrite the live .mind/index.db — choose a different --out", C.RED))
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    # byte-consistent copy via SQLite's backup API (WAL-safe), then purge private FROM
    # the copy — the live brain is read-only here and never loses a private node.
    dest = Index.open(out, repo=repo)
    idx.db.backup(dest.db)
    removed = dest.purge_private()
    dest.db.commit()
    # WATERPROOF GATE (owner: "100% wasserfeste sichere rule"): PROVE the copy is
    # private-clean before it is allowed to exist. We don't trust the purge — we
    # verify it. On any survivor, throw the file away and ABORT; a leaky brain must
    # never reach disk where it could be committed/shared.
    try:
        dest.assert_no_private()
    except SystemExit as e:
        dest.db.close()
        if out.exists():
            out.unlink()  # leave NOTHING shareable behind on a failed gate
        print(_c(str(e), C.RED))
        return 1
    dest.db.close()
    kept = idx.db.execute("SELECT COUNT(*) FROM nodes WHERE visibility!='private'").fetchone()[0]
    print(_c(f"✓ exported a shareable brain → {out}", C.GREEN))
    print(_c(f"  {kept} team node(s) kept · {removed} private node(s) left out", C.DIM))
    print(_c("  ✓ verified private-clean — no private note left this machine", C.DIM))
    if removed:
        print(_c("  🔒 your private notes stayed in this machine's brain", C.DIM))
    return 0


def _memory_clone(idx: Index, repo: Path) -> Index:
    """An in-memory copy of the live index for a non-destructive dry run. Copies the
    DB byte-for-byte via SQLite's backup API so the dry run sees real data but can
    never persist — the real .mind/index.db is untouched."""
    mem = Index.open(":memory:", repo=repo)
    idx.db.backup(mem.db)
    return mem


# ------------------------------------------------------------------- rendering
# The CLI renders the 3 ADR-016 tracks — code (WHERE), knowledge (WHY), blast radius
# (WHAT breaks) + open tasks. The legacy mixed list stays in the dict API (hook,
# dashboard, bridge depend on it) but is no longer printed: it buried the central
# code symbol under anchor-rich task/lesson nodes, which is the failure the tracks
# were built to fix (A/B 75/75). Sections are silent-when-empty (_print_brief style).
def _print_pretty(query: str, res: dict[str, Any]) -> None:
    if res["silenced"]:
        print(_c(f"· silent", C.DIM) + _c(f"  ({res['reason']}, {res['latency_us']} µs)", C.DIM))
        return
    print(_c(f"recall  \"{query}\"", C.DIM) + _c(f"  {res['latency_us']} µs · 0 tokens", C.DIM))

    code = res.get("code", [])
    if code:
        print()
        print(_c("── code · where (by importance)", C.DIM))
        for it in code:
            loc = (it.get("file") or "") + (f":{it['line']}" if it.get("line") else "")
            sym = _c(it["symbol"], C.B) if it.get("symbol") else _c("(file)", C.DIM)
            drift = _brief_drift_tag(it.get("drift"))
            why = _c(f"  — {it['why'][:80]}", C.DIM) if it.get("why") else ""
            imp = _c(f"{int(it['importance']):>4}", C.MUSTARD)
            print(f"  {imp} {drift}  {loc}  {sym}{why}")

    knowledge = res.get("knowledge", [])
    if knowledge:
        print()
        print(_c("── knowledge · why", C.DIM))
        for it in knowledge:
            sha = _c(f"  (sha {it['sha']})", C.DIM) if it.get("sha") else ""
            drift = _brief_drift_tag(it.get("drift"))
            print(f"  {_c('['+it['kind']+']', C.MUSTARD)} {_c(it['title'], C.B)}{sha}  {drift}")
            if it.get("why"):
                print(f"      {_c(it['why'][:100], C.DIM)}")

    blast = res.get("blast_radius", [])
    top_file = code[0].get("file") if code else None
    if blast and top_file:
        print()
        print(_c(f"── blast radius · changing {top_file} may break", C.DIM))
        for b in blast[:8]:
            print(f"  {_c('←', C.RED)} {b['file']}  " + _c(f"({b['kind']} · imp {b['importance']})", C.DIM))
        if len(blast) > 8:
            print(_c(f"  … +{len(blast) - 8} more", C.DIM))

    tasks = res.get("open_tasks", [])
    if tasks and top_file:
        print()
        print(_c(f"── open tasks on {top_file}", C.DIM))
        for t in tasks:
            print(f"  {_c('▸', C.MUSTARD)} {t['title']}")

    if top_file:
        print(_c(f"  └ recall brief {top_file} for the full pre-edit briefing", C.DIM))


def _format_for_prompt(query: str, res: dict[str, Any], terse: bool = False) -> str:
    """Web-AI path 1: a copy-paste context block for ChatGPT/Gemini/Claude-web.
    Carries the 3 tracks with self-explanatory CAPS headers (the _format_brief_for_prompt
    house style) — this text is pasted into OTHER AIs, so it must stand on its own.

    terse=True (the agent/Bash path, ADR machine-first): keep the WHY (knowledge)
    verbatim — that is the signal that stops an AI undoing a decision — but COMPRESS
    the structural WHERE list to one line per hit (location + symbol, no per-hit why
    blow-up) and cap depth. Same intent as the brief/explain terse formatters."""
    if res["silenced"]:
        return f"[recall] no project memory for: {query}"
    lines = [f"[recall · project memory for: {query}]"]
    drift_warn = DRIFT_WARN

    code = res.get("code", [])
    if code:
        lines.append("")
        lines.append("WHERE (code, by importance):")
        for it in (code[:5] if terse else code):
            loc = (it.get("file") or "") + (f":{it['line']}" if it.get("line") else "")
            sym = f" {it['symbol']}" if it.get("symbol") else ""
            lines.append(f"- {loc}{sym} (importance {int(it['importance'])})")
            warn = drift_warn.get(it.get("drift"))
            if warn:
                lines.append(warn)
            if it.get("why") and not terse:
                lines.append(f"  why: {it['why']}")
        if terse and len(code) > 5:
            lines.append(f"(+{len(code) - 5} more)")

    knowledge = res.get("knowledge", [])
    if knowledge:
        # the typed edges (level 3) live on the legacy `results` items — keyed by
        # (title, file), the same identity the dedup uses. Carrying 1-2 of them per
        # knowledge item restores the `supersedes -> ADR-007` chains the old block had.
        rel_by_key = {(r.get("title"), r.get("file")): r.get("relation") or []
                      for r in res.get("results", [])}
        lines.append("")
        lines.append("WHY (knowledge):")
        for it in knowledge:
            sha = f" (sha {it['sha']})" if it.get("sha") else ""
            lines.append(f"- [{it['kind']}] {it['title']}{sha}")
            warn = drift_warn.get(it.get("drift"))
            if warn:
                lines.append(warn)
            if it.get("why"):
                lines.append(f"  why: {it['why']}")
            for rl in rel_by_key.get((it.get("title"), it.get("file")), [])[:2]:
                if rl.get("target"):
                    lines.append(f"  relation: {rl['kind']} -> {rl['target']}")

    blast = res.get("blast_radius", [])
    top_file = code[0].get("file") if code else None
    if blast and top_file:
        lines.append("")
        lines.append(f"WHAT BREAKS if you change {top_file}:")
        for b in blast[:8]:
            lines.append(f"- {b['file']} ({b['kind']})")
        if len(blast) > 8:
            lines.append(f"(+{len(blast) - 8} more dependents)")

    tasks = res.get("open_tasks", [])
    if tasks and top_file:
        lines.append("")
        lines.append(f"OPEN TASKS on {top_file} — standing intent, read first:")
        for t in tasks:
            lines.append(f"- {t['title']}")

    return "\n".join(lines).rstrip()


def _print_resolve(res: dict[str, Any]) -> None:
    """Pretty terminal rendering of a search-inversion (ADR-037)."""
    guess = res["guess"]
    warm = res["index_warmth"]
    label = "warm" if res["warmth_used"] >= 0.5 else "cold"
    head = _c("recall · resolve", C.B) + _c(f"  {guess}", C.CYAN)
    print(head + _c(f"   [{label}, index warmth {warm:.0%}]", C.DIM))
    cands = res["candidates"]
    if not cands:
        print(_c(f"  no symbol resembling {guess!r} in this repo's vocabulary.", C.DIM))
        return
    print(_c(f"  no exact {guess!r} here — this repo most likely means:", C.DIM))
    for i, c in enumerate(cands, 1):
        loc = f"{c['file']}:{c['line']}" if c["file"] else "?"
        n = _c(f"{i}.", C.MUSTARD)
        print(f"  {n} {c['symbol']}  {_c('(' + loc + ')', C.DIM)}")
        if c["why"]:
            print(f"     {_c(' · '.join(c['why']), C.DIM)}")
    print(_c(f"  → this repo says {cands[0]['symbol']!r}, not {guess!r}.", C.B))


def _format_resolve_for_prompt(res: dict[str, Any]) -> str:
    """Machine/web-AI block: the vocabulary correction, compact. The point is the
    FIRST line — the real term — so an agent stops grepping the hallucinated one."""
    guess = res["guess"]
    cands = res["candidates"]
    if not cands:
        return f"[recall · resolve] no symbol resembling {guess!r} in this repo's vocabulary."
    lines = [f"[recall · vocabulary for: {guess}]",
             f"this repo says {cands[0]['symbol']!r}, not {guess!r}. Real candidates:"]
    for i, c in enumerate(cands, 1):
        loc = (c["file"] or "") + (f":{c['line']}" if c["line"] else "")
        syn = f"  (learned synonym via {c['via_synonym']!r})" if c["via_synonym"] else ""
        lines.append(f"{i}. {c['symbol']}  {loc}{syn}")
    return "\n".join(lines)


def _precedent_outcome_tag(p: dict[str, Any]) -> str:
    """One short outcome badge for a precedent — the fate that turns a hit into a precedent."""
    bits = []
    if p.get("superseded_by"):
        bits.append("⤳ superseded — current rule: " + p["superseded_by"]["title"])
    if p.get("became_landmine"):
        bits.append("🔴 became a landmine")
    if p.get("drift") in DRIFTED:
        bits.append("⚠ code drifted since")
    return "  ·  ".join(bits)


def _print_precedent(res: dict[str, Any]) -> None:
    """Pretty terminal rendering of arrow 3 — past decisions analogous to a situation."""
    sit = res["situation"]
    head = _c("recall · precedent", C.B) + _c(f"  {sit}", C.CYAN)
    print(head)
    ps = res.get("precedents", [])
    if not ps:
        print(_c("  no precedent — no past decision/lesson resembles this situation yet.", C.DIM))
        return
    print(_c("  have we been here before? the closest past decisions, and how they went:", C.DIM))
    for i, p in enumerate(ps, 1):
        n = _c(f"{i}.", C.MUSTARD)
        sha = _c(f" ({p['sha']})", C.DIM) if p.get("sha") else ""
        print(f"  {n} {_c('['+p['kind']+']', C.MUSTARD)} {p['title']}{sha}")
        if p.get("what"):
            print(f"     {_c(p['what'][:110], C.DIM)}")
        tag = _precedent_outcome_tag(p)
        if tag:
            colour = C.RED if (p.get("superseded_by") or p.get("became_landmine")) else C.MUSTARD
            print(f"     {_c(tag, colour)}")


def _format_precedent_for_prompt(res: dict[str, Any]) -> str:
    """Machine / web-AI block: the analogous past decisions WITH outcomes, so the AI
    generalizes from this repo's experience. The outcome line is the point — it is what a
    plain search cannot give."""
    sit = res["situation"]
    ps = res.get("precedents", [])
    if not ps:
        return f"[recall · precedent for: {sit}] no past decision/lesson resembles this yet."
    lines = [f"[recall · precedent for: {sit}]",
             "the closest past decisions in THIS repo, and how each turned out:"]
    for i, p in enumerate(ps, 1):
        sha = f" (sha {p['sha']})" if p.get("sha") else ""
        lines.append(f"{i}. [{p['kind']}] {p['title']}{sha}")
        if p.get("what"):
            lines.append(f"     {p['what']}")
        tag = _precedent_outcome_tag(p)
        if tag:
            lines.append(f"     → {tag}")
    return "\n".join(lines)


def _impact_why(r: dict[str, Any]) -> str:
    """The legible 'why this is affected' for one impacted file — the two signals, shown."""
    bits = []
    if r.get("co_change"):
        bits.append(f"co-changed ×{r['co_change']}")
    if r.get("struct_hop"):
        bits.append("imports it" if r["struct_hop"] == 1 else f"depends (hop {r['struct_hop']})")
    if r.get("landmine"):
        bits.append("🔴 landmine")
    if r.get("drift") in DRIFTED:
        bits.append("⚠ drifted")
    return " · ".join(bits)


def _print_impact(res: dict[str, Any]) -> None:
    """Pretty terminal rendering of the impact set."""
    tgt = res["target"]
    head = _c("recall · impact", C.B) + _c(f"  {tgt}", C.CYAN)
    resolved = res.get("resolved") or []
    if resolved and resolved != [tgt]:
        head += _c(f"  ({', '.join(resolved)})", C.DIM)
    print(head)
    rows = res.get("impacted", [])
    if not rows:
        print(_c("  no recorded impact — recall has no co-change or dependency on this yet.", C.DIM))
        return
    print(_c("  if you touch this, what's actually affected (history + structure):", C.DIM))
    for r in rows:
        imp = _c(f"{r['importance']:>5}", C.MUSTARD) if r.get("importance") else _c("    ·", C.DIM)
        why = _impact_why(r)
        print(f"  {imp}  {r['file']}  {_c('(' + why + ')', C.DIM) if why else ''}")
    print(_c("  co-change = git proved they move together · importance = how load-bearing", C.DIM))


def _format_impact_for_prompt(res: dict[str, Any]) -> str:
    """Machine / web-AI block: the impact set as a paste-in, the two signals legible per row."""
    tgt = res["target"]
    rows = res.get("impacted", [])
    if not rows:
        return f"[recall · impact for: {tgt}] no recorded co-change or dependency yet."
    lines = [f"[recall · impact for: {tgt} — what's affected if you change it]",
             "ranked by empirical co-change (git history) + structural dependents, × importance:"]
    for r in rows:
        why = _impact_why(r)
        lines.append(f"  - {r['file']}" + (f"  ({why})" if why else ""))
    lines += _format_neighborhood(res.get("neighborhood") or {})
    return "\n".join(lines)


# --------------------------------------------------------- code-intelligence renderers
def _intel_flags(r: dict[str, Any]) -> str:
    """The shared per-row tail: drift + landmine, the same wording across the intel serves."""
    bits = []
    if r.get("drift") in DRIFTED:
        bits.append({"committed": "⚠ stale", "uncommitted": "⚠ edited",
                     "broken": "🔴 broken"}.get(r["drift"], "⚠ stale"))
    if r.get("landmine"):
        bits.append("☡ landmine")
    return ("  " + " · ".join(bits)) if bits else ""


def _print_hierarchy(res: dict[str, Any]) -> None:
    """callers/callees terminal rendering."""
    arrow = "depends on" if res["direction"] == "callees" else "is used by"
    head = _c("recall · " + res["direction"], C.B) + _c(f"  {res['target']}", C.CYAN)
    print(head)
    rows = res.get("results", [])
    if not rows:
        print(_c(f"  nothing {arrow} this in the recorded graph.", C.DIM))
        return
    verb = ("what it depends on" if res["direction"] == "callees" else "what depends on it")
    print(_c(f"  {verb} (file-granular, by hop then importance):", C.DIM))
    for r in rows:
        imp = _c(f"{r['importance']:>5}", C.MUSTARD) if r.get("importance") else _c("    ·", C.DIM)
        print(f"  {imp}  h{r['hop']}  {r['file']}{_c(_intel_flags(r), C.DIM)}")
    print(_c("  " + res.get("note", ""), C.DIM))


def _format_hierarchy_for_prompt(res: dict[str, Any]) -> str:
    rows = res.get("results", [])
    verb = "depends on" if res["direction"] == "callees" else "is used by"
    if not rows:
        return f"[recall · {res['direction']} for: {res['target']}] nothing {verb} this in the recorded graph."
    lines = [f"[recall · {res['direction']} for: {res['target']} — file-granular]"]
    for r in rows:
        lines.append(f"  - h{r['hop']} {r['file']}{_intel_flags(r)}")
    lines.append(f"  ({res.get('note','')})")
    return "\n".join(lines)


def _print_listing(res: dict[str, Any], name: str, key: str, blurb: str, color: str) -> None:
    """Shared renderer for the flat list serves (dead-code, untested)."""
    print(_c(f"recall · {name}", C.B))
    rows = res.get(key, [])
    if not rows:
        print(_c(f"  none — {blurb} (clean).", C.DIM))
        return
    print(_c(f"  {blurb}:", C.DIM))
    for r in rows:
        imp = _c(f"{r['importance']:>5}", color) if r.get("importance") else _c("    ·", C.DIM)
        print(f"  {imp}  {r['file']}{_c(_intel_flags(r), C.DIM)}")
    print(_c("  " + res.get("note", ""), C.DIM))


def _format_listing_for_prompt(res: dict[str, Any], name: str, key: str, blurb: str) -> str:
    rows = res.get(key, [])
    if not rows:
        return f"[recall · {name}] none."
    lines = [f"[recall · {name} — {blurb}]"]
    for r in rows:
        lines.append(f"  - {r['file']}{_intel_flags(r)}")
    lines.append(f"  ({res.get('note','')})")
    return "\n".join(lines)


def _print_cycles(res: dict[str, Any]) -> None:
    print(_c("recall · cycles", C.B))
    rows = res.get("cycles", [])
    if not rows:
        print(_c("  no import cycles in the depends_on graph (clean).", C.DIM))
        return
    print(_c("  file→file import cycles (each shown once):", C.DIM))
    for r in rows:
        chain = " → ".join(r["files"]) + " → " + r["files"][0]
        print(f"  {_c('↻', C.RED)} {chain}")
    print(_c("  " + res.get("note", ""), C.DIM))


def _format_cycles_for_prompt(res: dict[str, Any]) -> str:
    rows = res.get("cycles", [])
    if not rows:
        return "[recall · cycles] no import cycles in the depends_on graph."
    lines = ["[recall · cycles — file→file import cycles]"]
    for r in rows:
        lines.append("  - " + " -> ".join(r["files"]) + " -> " + r["files"][0])
    return "\n".join(lines)


# Node-level drift warnings for prompt blocks (ONE copy — the brief formatter keeps
# its own FILE-level wording, which is a different claim).
DRIFT_WARN = {"committed": "  ⚠ may be stale — file changed since this was stamped",
              "uncommitted": "  ⚠ stale — file has uncommitted edits right now",
              "broken": "  🔴 BROKEN — this claim's own check now FAILS; the code no longer matches it"}

# Every drift level that means "do not trust this at face value". BROKEN is the loudest
# (the claim is wrong NOW, arrow 1) — kept alongside the two SHA-drift levels so every
# "is it stale?" gate catches it too, not just the new red-specific rendering.
DRIFTED = ("committed", "uncommitted", "broken")


def _brief_drift_tag(level: str | None) -> str:
    """A short drift badge for the briefing header (mirrors _drift_badge's meaning)."""
    if level == "broken":
        return _c("● BROKEN", C.RED)
    if level == "uncommitted":
        return _c("● edited", C.RED)
    if level == "committed":
        return _c("● check", C.MUSTARD)
    if level == "fresh":
        return _c("● fresh", C.GREEN)
    return ""


def _print_brief(b: dict[str, Any]) -> None:
    """Pretty terminal briefing — the five tracks, each silent when empty."""
    head = _c("recall · brief", C.B) + _c(f"  {b['file']}", C.CYAN)
    tag = _brief_drift_tag(b.get("drift"))
    print(head + (f"  {tag}" if tag else ""))
    if not b["known"]:
        print(_c("  (recall has never seen this file — nothing to brief)", C.DIM))
        return

    warns = b.get("warns", [])
    if warns:
        print(_c("\n  🔴 landmines — past mistakes warn about this file, heed before editing:", C.RED))
        for w in warns:
            sha = _c(f" ({w['sha']})", C.DIM) if w.get("sha") else ""
            stale = _c("  ⚠ stale", C.RED) if w.get("drift") in DRIFTED else ""
            print(f"    {_c('['+w['kind']+']', C.RED)} {w['title']}{sha}{stale}")
            if w.get("why"):
                print(f"      {_c(w['why'][:100], C.DIM)}")

    tasks = b.get("open_tasks", [])
    if tasks:
        print(_c("\n  ⚠ open tasks on this file — read before you edit:", C.MUSTARD))
        for t in tasks:
            print(f"    {_c('▸', C.MUSTARD)} {t['title']}")

    if b["why"]:
        print(_c("\n  why it is the way it is:", C.B))
        for w in b["why"]:
            sha = _c(f" ({w['sha']})", C.DIM) if w.get("sha") else ""
            drift = _c("  ⚠ stale", C.RED) if w.get("drift") in DRIFTED else ""
            print(f"    {_c('['+w['kind']+']', C.MUSTARD)} {w['title']}{sha}{drift}")
            if w.get("why"):
                print(f"      {_c(w['why'][:100], C.DIM)}")

    if b["breaks"]:
        print(_c("\n  what breaks if you change it (dependents):", C.B))
        for x in b["breaks"][:10]:
            print(f"    {_c('←', C.RED)} {x['file']} {_c('('+x['kind']+')', C.DIM)}")

    if b["depends_on"]:
        print(_c("\n  what it leans on (depends_on):", C.B))
        for d in b["depends_on"][:10]:
            print(f"    {_c('→', C.CYAN)} {d['target']} {_c('('+d['kind']+')', C.DIM)}")

    nb_lines = _format_neighborhood(b.get("neighborhood") or {})
    if nb_lines:
        print()
        for ln in nb_lines:
            print(_c("    " + ln, C.DIM))

    if b["symbols"]:
        names = ", ".join(s["symbol"] for s in b["symbols"][:12])
        more = "" if len(b["symbols"]) <= 12 else f" … +{len(b['symbols'])-12}"
        print(_c("\n  symbols: ", C.B) + _c(names + more, C.DIM))


def _format_neighborhood(nb: dict[str, Any]) -> list[str]:
    """The MOVES-WITH render block (workstream D), shared by brief + impact. A LENS: each partner
    carries a confidence LABEL + two distinct staleness flags, never a verdict. Silent when the
    cluster is silenced/empty; the binding decision (`bound by:`) renders even on a thin cluster."""
    if not nb:
        return []
    lines: list[str] = []
    cluster = nb.get("cluster") or []
    if cluster and not nb.get("silenced"):
        lines.append("MOVES WITH (co-change neighborhood — git-proven; a lens, not a verdict):")
        for c in cluster:
            flags = []
            if not c.get("edge_verified", True):
                flags.append("co-change may be stale")
            if c.get("partner_drift") == "broken":
                flags.append("partner has a 🔴 BROKEN claim")
            elif c.get("partner_drift") in ("committed", "uncommitted"):
                flags.append("partner drifted")
            tail = f"  ⚠ {'; '.join(flags)}" if flags else ""
            lines.append(f"  - {c['file']}  [{c['confidence']}]{tail}")
    b = nb.get("bound_by")
    if b:
        sha = f" (sha {b['sha']})" if b.get("sha") else ""
        lines.append(f"bound by: [{b['kind']}] {(b.get('title') or '').splitlines()[0][:120]}{sha}")
    return lines


def _format_brief_for_prompt(b: dict[str, Any], terse: bool = False) -> str:
    """Web-AI / MCP path: a briefing block to read before editing the file.

    terse=True (MCP — the caller is a machine, ADR machine-first): keep the
    high-value signal verbatim (the WHY + open tasks — what stops an AI from
    silently undoing a decision) but COMPRESS the structural lists (blast radius /
    depends_on) to one short line each. The rich CLI/--for-prompt path (terse=False)
    keeps the full per-file listing a human reading the terminal wants."""
    if not b["known"]:
        return f"[recall · brief] no project memory for: {b['file']}"
    lines = [f"[recall · pre-edit briefing for: {b['file']}]", ""]
    # BROKEN reuses the loud shared constant (workstream B) — the file-level 'broken' case
    # used to fall through to nothing, so a claim failing its own re-check read as fresh.
    drift = {"committed": "⚠ this file changed since some knowledge was stamped — verify before trusting it",
             "uncommitted": "⚠ this file has uncommitted edits right now",
             "broken": DRIFT_WARN["broken"].strip()}.get(b.get("drift"))
    if drift:
        lines.append(drift)
        lines.append("")
    # Landmines lead and are kept verbatim in BOTH modes (like WHY) — the conscience
    # signal (arrow 2) is the highest-value thing an AI must see before editing.
    if b.get("warns"):
        lines.append("LANDMINES — past mistakes warn about this file; heed them before editing:")
        for w in b["warns"]:
            sha = f" (sha {w['sha']})" if w.get("sha") else ""
            stale = " ⚠ check freshness" if w.get("drift") in DRIFTED else ""
            lines.append(f"  - [{w['kind']}] {w['title']}{sha}{stale}")
            if w.get("why"):
                lines.append(f"      {w['why']}")
        lines.append("")
    if b.get("open_tasks"):
        lines.append("OPEN TASKS on this file — read these first, they are standing intent:")
        for t in b["open_tasks"]:
            lines.append(f"  - {t['title']}")
        lines.append("")
    if b["why"]:
        # the WHY is the whole point — kept verbatim in BOTH modes
        lines.append("WHY this file is the way it is:")
        for w in b["why"]:
            sha = f" (sha {w['sha']})" if w.get("sha") else ""
            # a why whose own predicate FAILS now is wrong until re-verified — render it
            # loud and show WHAT failed (the verdict + predicate already ride the brief return).
            broke = w.get("drift") == "broken"
            flag = " 🔴 BROKEN — its own re-check FAILS now" if broke else ""
            lines.append(f"  - [{w['kind']}] {w['title']}{sha}{flag}")
            if w.get("why"):
                lines.append(f"      {w['why']}")
            if broke and w.get("predicate"):
                lines.append(f"      failing check: {w['predicate']}")
        lines.append("")
    if terse:
        # structure as compact one-liners — the names matter, the per-edge kind does not
        if b["breaks"]:
            names = [x["file"] for x in b["breaks"][:5]]
            more = f" (+{len(b['breaks']) - 5} more)" if len(b["breaks"]) > 5 else ""
            lines.append("BREAKS IF CHANGED: " + ", ".join(names) + more)
        if b["depends_on"]:
            names = [d["target"] for d in b["depends_on"][:5]]
            more = f" (+{len(b['depends_on']) - 5} more)" if len(b["depends_on"]) > 5 else ""
            lines.append("LEANS ON: " + ", ".join(names) + more)
        lines += _format_neighborhood(b.get("neighborhood") or {})
        return "\n".join(lines).rstrip()
    if b["breaks"]:
        lines.append("WHAT BREAKS if you change it (files that depend on it):")
        for x in b["breaks"][:10]:
            lines.append(f"  - {x['file']} ({x['kind']})")
        lines.append("")
    if b["depends_on"]:
        lines.append("WHAT IT LEANS ON (depends_on):")
        for d in b["depends_on"][:10]:
            lines.append(f"  - {d['target']} ({d['kind']})")
    lines += _format_neighborhood(b.get("neighborhood") or {})
    return "\n".join(lines).rstrip()


def _print_review(r: dict[str, Any]) -> None:
    """Pretty terminal review — the risk files loud, the rest as a quiet roll-call."""
    sha = r.get("sha") or "HEAD"
    head = _c("recall · review", C.B) + _c(f"  {sha}", C.CYAN)
    print(head + _c(f"  ({r['counts']['files']} files, {r['counts']['risk']} risk)", C.DIM))
    if r["risk_files"]:
        print(_c("\n  ⚠ risk — load-bearing code in this change:", C.MUSTARD))
        for rf in r["risk_files"]:
            why = ", ".join(rf["reasons"])
            print(f"    {_c('▸', C.MUSTARD)} {rf['file']}  {_c('(' + why + ')', C.DIM)}")
    safe = [f["file"] for f in r["files"] if f["file"] not in {x["file"] for x in r["risk_files"]}]
    if safe:
        print(_c("\n  other files changed:", C.B))
        for f in safe:
            print(f"    {_c('·', C.DIM)} {f}")
    print(_c("\n  `recall brief <file>` or `recall review " + sha + " --for-prompt` for the PR markdown.", C.DIM))


def _format_review_markdown(r: dict[str, Any]) -> str:
    """Web-AI / PR path: a markdown block describing the change's blast radius and risk.
    Drop it into a pull-request description so a reviewer sees what the change can break."""
    sha = r.get("sha") or "HEAD"
    lines = [f"# Review of `{sha}`", "",
             f"{r['counts']['files']} file(s) changed, {r['counts']['risk']} flagged as risk.", ""]
    if r["risk_files"]:
        lines.append("## ⚠ Risk files")
        for rf in r["risk_files"]:
            lines.append(f"- **{rf['file']}** — {', '.join(rf['reasons'])} "
                         f"(importance {rf['importance']}, {rf['dependents']} dependents)")
        lines.append("")
    for f in r["files"]:
        lines.append(f"### `{f['file']}`")
        if f["breaks"]:
            lines.append("- **Breaks if changed:** " + ", ".join(x["file"] for x in f["breaks"][:8]))
        if f["why"]:
            lines.append("- **Why it exists:**")
            for w in f["why"]:
                shatag = f" (`{w['sha']}`)" if w.get("sha") else ""
                lines.append(f"  - [{w['kind']}] {w['title']}{shatag}")
        if f["open_tasks"]:
            lines.append("- **Open tasks:** " + "; ".join(t["title"] for t in f["open_tasks"]))
        if f.get("drift") in DRIFTED:
            lines.append(f"- **Drift:** {f['drift']} — verify pinned knowledge before trusting it")
        lines.append("")
    return "\n".join(lines).rstrip()


def _print_explain(o: dict[str, Any], repo_name: str) -> None:
    """Wave C — the terminal "explain me this repo" path."""
    c = o["counts"]
    print(_c(f"recall · explain  {repo_name}", C.B)
          + _c(f"  {c['files']} files · {c['lessons']} lessons · {c['open_tasks']} open tasks", C.DIM))

    if o["top_files"]:
        print(_c("\n  start reading here — the load-bearing files:", C.B))
        for f in o["top_files"][:10]:
            imp = _c(f"  imp {int(f['importance'])}", C.MUSTARD) if f["importance"] else ""
            print(f"    {_c('▸', C.MUSTARD)} {f['file']} {_c('('+str(f['symbols'])+' symbols)', C.DIM)}{imp}")

    if o["decisions"]:
        print(_c("\n  must-know decisions:", C.B))
        for d in o["decisions"][:8]:
            sha = _c(f" ({d['sha']})", C.DIM) if d.get("sha") else ""
            print(f"    {_c('◆', C.CYAN)} {d['title']}{sha}")

    if o["in_progress"]:
        print(_c("\n  in progress right now (open tasks):", C.B))
        for t in o["in_progress"][:12]:
            print(f"    {_c('▸', C.MUSTARD)} {t['title']}")

    if o["contested"]:
        print(_c("\n  where the team burns time (touch with care):", C.B))
        for s in o["contested"][:6]:
            print(f"    {_c('~', C.RED)} {s['file']} {_c('(churn '+str(s['churn'])+')', C.DIM)}")

    print(_c("\n  next: `recall brief <file>` before editing · `recall \"<question>\"` to ask", C.DIM))


def _format_explain_for_prompt(o: dict[str, Any], repo_name: str, terse: bool = False) -> str:
    """Web-AI / MCP path: a repo orientation block for a fresh session.

    terse=True (MCP — machine caller): tighter caps and drop the file-name-only
    importance/symbol detail (keep the names) + the contested section, which is the
    lowest value-per-token for an agent about to judge ONE file. The decisions +
    open tasks (the standing intent) stay — that is what orients judgment."""
    c = o["counts"]
    lines = [f"[recall · repo orientation for: {repo_name}]",
             f"{c['files']} files · {c['lessons']} lessons · {c['open_tasks']} open tasks", ""]
    if o["top_files"]:
        lines.append("START HERE — the load-bearing files (by causal importance):")
        n = 6 if terse else 10
        for f in o["top_files"][:n]:
            if terse:
                lines.append(f"  - {f['file']}")
            else:
                lines.append(f"  - {f['file']} ({f['symbols']} symbols, importance {int(f['importance'])})")
        lines.append("")
    if o["decisions"]:
        lines.append("MUST-KNOW DECISIONS:")
        for d in o["decisions"][: 6 if terse else 8]:
            sha = f" (sha {d['sha']})" if d.get("sha") else ""
            lines.append(f"  - {d['title']}{sha}")
        lines.append("")
    if o["in_progress"]:
        lines.append("IN PROGRESS RIGHT NOW (open tasks — standing intent):")
        for t in o["in_progress"][: 6 if terse else 12]:
            lines.append(f"  - {t['title']}")
        lines.append("")
    if o["contested"] and not terse:
        lines.append("WHERE THE TEAM BURNS TIME (touch with extra care):")
        for s in o["contested"][:6]:
            lines.append(f"  - {s['file']} (churn {s['churn']})")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recall", description="AI-native project memory. The code is the wiki.")
    # `recall --version` so users (and support) can see exactly which release they
    # run — read from the package's single source of truth (recall.__version__).
    from recall import __version__ as _recall_version
    p.add_argument("--version", action="version", version=f"recall {_recall_version}")
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("init", help="index a project")
    pi.add_argument("path", nargs="?", default=".")
    pi.add_argument("--max-commits", type=int, default=400)
    pi.add_argument("--no-code-map", action="store_true", help="skip tree-sitter code map")
    pi.add_argument("--json", action="store_true", help="machine-readable result for the desktop app")
    pi.set_defaults(func=cmd_init)

    pb = sub.add_parser("brief", help="pre-edit briefing for a file (why / what breaks / open tasks)")
    pb.add_argument("file", help="the file you're about to edit (repo-relative)")
    pb.add_argument("--for-prompt", action="store_true", help="copy-paste briefing block for web AI")
    pb.add_argument("--terse", action="store_true", help="machine-first compact block (for AI agents via Bash; keeps WHY + tasks verbatim, compresses lists)")
    pb.add_argument("--repo", default=None)
    pb.set_defaults(func=cmd_brief)

    ppu = sub.add_parser("push", help="situational push: the scoped brief + landmines + live BROKEN trust-status for what you're doing now (for subagents / hookless harnesses)")
    ppu.add_argument("--file", default=None, help="the file you're about to edit (repo-relative)")
    ppu.add_argument("--diff", action="store_true", help="scope to the working diff (files changed vs HEAD)")
    ppu.add_argument("--task", default=None, help="what you're trying to do — surfaces relevant knowledge + precedent")
    ppu.add_argument("--for-prompt", action="store_true", help="paste-in block (the default output is already this form)")
    ppu.add_argument("--terse", action="store_true", help="paste-in block for AI agents via Bash (same as --for-prompt here)")
    ppu.add_argument("--repo", default=None)
    ppu.set_defaults(func=cmd_push)

    prc = sub.add_parser("receipt", help="money-receipt: the loop recall was in over a rolling window, in MEASURED counts + per-call emitted size (no token/$ estimate, no invented denominator)")
    prc.add_argument("--days", type=int, default=14, help="rolling window in days (default 14)")
    prc.add_argument("--session-tokens", type=int, default=None,
                     help="your session's total token count — only then is a recall-vs-session %% shown (never invented)")
    prc.add_argument("--json", action="store_true", help="emit the raw receipt dict as JSON")
    prc.add_argument("--repo", default=None)
    prc.set_defaults(func=cmd_receipt)

    pg = sub.add_parser("graph", help="dump the stamped file-graph as JSON for the desktop app (read-only, 0 tokens)")
    pg.add_argument("--json", action="store_true", help="emit the graph as JSON (the only output mode)")
    pg.add_argument("--limit", type=int, default=140, help="max files in the graph, ranked by importance + degree (default 140)")
    pg.add_argument("--repo", default=None)
    pg.set_defaults(func=cmd_graph)

    pa = sub.add_parser("ack", help="acknowledge a file's briefing so the hard pre-edit gate lets the edit through")
    pa.add_argument("file", help="the file you briefed and are about to edit (repo-relative)")
    pa.add_argument("--repo", default=None)
    pa.set_defaults(func=cmd_ack)

    pco = sub.add_parser("contested", help="uncertainty hotspots — code the team kept changing")
    pco.add_argument("path", nargs="?", default=None, help="project path (same as --repo; defaults to here)")
    pco.add_argument("--limit", type=int, default=20)
    pco.add_argument("--min-churn", type=int, default=2, help="ignore files touched by fewer commits")
    pco.add_argument("--repo", default=None)
    pco.set_defaults(func=cmd_contested)

    prs = sub.add_parser("resolve", help="search-inversion: correct a hallucinated search term into this repo's real vocabulary (ADR-037)")
    prs.add_argument("guess", help="the term you're guessing (e.g. seatLimit)")
    prs.add_argument("--top", type=int, default=5)
    prs.add_argument("--warmth", type=float, default=None, help="0=cold (vocab only) .. 1=warm (vocab + experience); default = the index's own warmth")
    prs.add_argument("--for-prompt", action="store_true", help="copy-paste vocabulary block for web AI")
    prs.add_argument("--terse", action="store_true", help="machine-first block for AI agents via Bash")
    prs.add_argument("--repo", default=None)
    prs.set_defaults(func=cmd_resolve)

    ppr = sub.add_parser("precedent", help="arrow 3: the most analogous past decisions for a situation, with how each turned out")
    ppr.add_argument("situation", help="what you're about to do (e.g. 'switching auth to JWT')")
    ppr.add_argument("--limit", type=int, default=5)
    ppr.add_argument("--for-prompt", action="store_true", help="copy-paste precedent block for web AI")
    ppr.add_argument("--terse", action="store_true", help="machine-first block for AI agents via Bash")
    ppr.add_argument("--repo", default=None)
    ppr.set_defaults(func=cmd_precedent)

    pim = sub.add_parser("impact", help="'if I touch this, what's affected?' — empirical co-change + structural dependents (the 0-token call-hierarchy replacement)")
    pim.add_argument("target", help="a file (a/b.py) or a symbol name (login)")
    pim.add_argument("--depth", type=int, default=2, help="how many hops of structural dependents to walk")
    pim.add_argument("--limit", type=int, default=25)
    pim.add_argument("--for-prompt", action="store_true", help="copy-paste impact block for web AI")
    pim.add_argument("--terse", action="store_true", help="machine-first block for AI agents via Bash")
    pim.add_argument("--repo", default=None)
    pim.set_defaults(func=cmd_impact)

    # ---- code intelligence (static-code-intel serves, file-granular, 0 tokens) ----
    pca = sub.add_parser("callers", help="who depends on this file/symbol (file-granular call-hierarchy)")
    pca.add_argument("target", help="a file (a/b.py) or a symbol name")
    pca.add_argument("--callees", action="store_true", help="invert: what THIS depends on (forward)")
    pca.add_argument("--depth", type=int, default=2, help="how many hops to walk")
    pca.add_argument("--limit", type=int, default=50)
    pca.add_argument("--for-prompt", action="store_true", help="copy-paste block for web AI")
    pca.add_argument("--terse", action="store_true", help="machine-first block for AI agents via Bash")
    pca.add_argument("--repo", default=None)
    pca.set_defaults(func=cmd_callers)

    pce = sub.add_parser("callees", help="what this file/symbol depends on (forward call-hierarchy)")
    pce.add_argument("target", help="a file (a/b.py) or a symbol name")
    pce.add_argument("--depth", type=int, default=2, help="how many hops to walk")
    pce.add_argument("--limit", type=int, default=50)
    pce.add_argument("--for-prompt", action="store_true")
    pce.add_argument("--terse", action="store_true")
    pce.add_argument("--repo", default=None)
    pce.set_defaults(func=cmd_callers, callees=True)

    pdc = sub.add_parser("dead-code", help="code files nothing imports — dead-code candidates (verify)")
    pdc.add_argument("--limit", type=int, default=50)
    pdc.add_argument("--for-prompt", action="store_true")
    pdc.add_argument("--terse", action="store_true")
    pdc.add_argument("--repo", default=None)
    pdc.set_defaults(func=cmd_dead_code)

    put = sub.add_parser("untested", help="code files with no recorded test edge (file-granular)")
    put.add_argument("--limit", type=int, default=50)
    put.add_argument("--for-prompt", action="store_true")
    put.add_argument("--terse", action="store_true")
    put.add_argument("--repo", default=None)
    put.set_defaults(func=cmd_untested)

    pcy = sub.add_parser("cycles", help="file→file import cycles in the depends_on graph")
    pcy.add_argument("--limit", type=int, default=50)
    pcy.add_argument("--for-prompt", action="store_true")
    pcy.add_argument("--terse", action="store_true")
    pcy.add_argument("--repo", default=None)
    pcy.set_defaults(func=cmd_cycles)

    pex = sub.add_parser("explain", help="explain the repo to a new dev/AI: top files, decisions, what's in progress")
    pex.add_argument("path", nargs="?", default=None, help="project path (same as --repo; defaults to here)")
    pex.add_argument("--for-prompt", action="store_true", help="copy-paste orientation block for web AI")
    pex.add_argument("--terse", action="store_true", help="machine-first compact block (for AI agents via Bash; keeps decisions + tasks verbatim)")
    pex.add_argument("--repo", default=None)
    pex.set_defaults(func=cmd_explain)

    psc = sub.add_parser("sync-context", help="write recall's live state into the AI instruction file (CLAUDE.md/AGENTS.md/…) so every client loads it without a tool call")
    psc.add_argument("path", nargs="?", default=None, help="project path (same as --repo; defaults to here)")
    psc.add_argument("--quiet", action="store_true", help="no output (used by the post-commit hook)")
    psc.add_argument("--repo", default=None)
    psc.set_defaults(func=cmd_sync_context)

    prv = sub.add_parser("review", help="review a commit: what it breaks, risk files, PR-markdown")
    prv.add_argument("sha", nargs="?", default=None, help="commit SHA (default HEAD)")
    prv.add_argument("--for-prompt", action="store_true", help="PR-markdown block for a pull request")
    prv.add_argument("--repo", default=None)
    prv.set_defaults(func=cmd_review)

    ppc = sub.add_parser("precommit-check", help="warn (never block) when staged files are load-bearing (what the pre-commit hook calls)")
    ppc.add_argument("--repo", default=None)
    ppc.set_defaults(func=cmd_precommit_check)

    pcl = sub.add_parser("check-leak", help="BLOCK a commit that stages a brain holding private notes (the leak guard)")
    pcl.add_argument("--repo", default=None)
    pcl.set_defaults(func=cmd_check_leak)

    ps = sub.add_parser("stamp", help="stamp a node by hand")
    ps.add_argument("title")
    ps.add_argument("--id", type=int, default=None, dest="update_id",
                    help="update EXACTLY this node id (fast, unambiguous) instead of "
                         "creating a new one — like editing a task by its id, not its name")
    ps.add_argument("--body", default=None)
    ps.add_argument("--anchors", default=None, help="comma-separated")
    ps.add_argument("--tags", default=None, help="comma-separated")
    ps.add_argument("--file", default=None)
    ps.add_argument("--predicate", default=None,
                    help="re-runnable CHECK: 'contains:<re>' / 'absent:<re>' joined by ' && ' "
                         "— freshen() re-verifies it free, flags 🔴 when the claim breaks")
    ps.add_argument("--line", type=int, default=None,
                    help="symbol start line this claim is about — scopes a --predicate to that "
                         "symbol's span (else the check runs against the whole file)")
    ps.add_argument("--outcome", default=None,
                    help="what CAME of this decision — what was learned / how it turned out. The "
                         "end of the causal chain, distinct from the title (the decision itself)")
    ps.add_argument("--private", action="store_true",
                    help="mark this note private: it stays in THIS brain and is left out of "
                         "`recall export` — your reasoning never leaves your machine/org unless you say so")
    ps.add_argument("--json", action="store_true", help="machine-readable result for the desktop app")
    ps.add_argument("--repo", default=None)
    ps.set_defaults(func=cmd_stamp)

    ph = sub.add_parser("handoff", help="stamp the in-flight session state so the next session rebuilds it from recall (docking point #4)")
    ph.add_argument("summary", help="what is in flight right now (one or two sentences)")
    ph.add_argument("--files", default=None, help="comma-separated in-flight files — surfaces in their pre-edit brief next session")
    ph.add_argument("--repo", default=None)
    ph.set_defaults(func=cmd_handoff)

    pf = sub.add_parser("freshen", help="re-check pinned nodes for drift against git")
    pf.add_argument("path", nargs="?", default=None, help="project path (same as --repo; defaults to here)")
    pf.add_argument("--repo", default=None)
    pf.set_defaults(func=cmd_freshen)

    pt = sub.add_parser("stats", help="show index stats")
    pt.add_argument("path", nargs="?", default=None, help="project path (same as --repo; defaults to here)")
    pt.add_argument("--repo", default=None)
    pt.add_argument("--json", action="store_true", help="machine-readable stats for the desktop app")
    pt.set_defaults(func=cmd_stats)

    psn = sub.add_parser("snapshot", help="dump the whole index as one JSON snapshot for the desktop app (same data as the dashboard, read-only, 0 tokens)")
    psn.add_argument("path", nargs="?", default=None, help="project path (same as --repo; defaults to here)")
    psn.add_argument("--repo", default=None)
    psn.add_argument("--json", action="store_true", help="machine-readable (the only mode; flag accepted for symmetry)")
    psn.set_defaults(func=cmd_snapshot)

    pd = sub.add_parser("dashboard", help="open the local dashboard (the wiki, visible)")
    pd.add_argument("path", nargs="?", default=None, help="project path (same as --repo; defaults to here)")
    pd.add_argument("--repo", default=None)
    pd.add_argument("--port", type=int, default=7099)
    pd.add_argument("--host", default="127.0.0.1")
    pd.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    pd.add_argument("--no-watch", action="store_true",
                    help="don't auto-index when a new commit lands (live mode is on by default)")
    pd.set_defaults(func=cmd_dashboard)

    ptr = sub.add_parser("tray", help="run the dashboard in the background (tray icon if installed, else a no-close console)")
    ptr.add_argument("path", nargs="?", default=None, help="project path (same as --repo; defaults to here)")
    ptr.add_argument("--repo", default=None)
    ptr.add_argument("--port", type=int, default=7099)
    ptr.add_argument("--host", default="127.0.0.1")
    ptr.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    ptr.add_argument("--no-watch", action="store_true",
                     help="don't auto-index when a new commit lands (live mode is on by default)")
    ptr.set_defaults(func=cmd_tray)

    pst = sub.add_parser("stop", help="stop a dashboard running in the background for this project")
    pst.add_argument("path", nargs="?", default=None, help="project path (same as --repo; defaults to here)")
    pst.add_argument("--repo", default=None)
    pst.set_defaults(func=cmd_stop)

    plg = sub.add_parser("login", help="sign in to whatever-recall (opens your browser — no key to copy)")
    plg.add_argument("--no-browser", action="store_true", help="don't auto-open the browser; print the URL + code instead")
    plg.set_defaults(func=cmd_login)

    plo = sub.add_parser("logout", help="sign out: remove the stored license from this machine")
    plo.set_defaults(func=cmd_logout)

    psh = sub.add_parser("shortcut", help="put a double-click dashboard launcher on your Desktop (no AI needed)")
    psh.add_argument("path", nargs="?", default=None, help="project path (same as --repo; defaults to here)")
    psh.add_argument("--repo", default=None)
    psh.add_argument("--port", type=int, default=7099)
    psh.add_argument("--remove", action="store_true", help="remove the launcher again")
    psh.set_defaults(func=cmd_shortcut)

    # MCP (Phase M, ADR-029): recall as native tools in Claude/Cursor — stdio, stdlib.
    pm = sub.add_parser("mcp", help="run the MCP server on stdio (recall as native tools in Claude/Cursor)")
    pm.add_argument("--repo", default=None)
    pm.add_argument("--print-config", action="store_true",
                    help="print the one-liner + JSON snippets to register recall in MCP clients")
    pm.set_defaults(func=cmd_mcp)

    # The write-time loop made real: a git post-commit hook + the token-free stamper.
    ph = sub.add_parser("hook", help="install the git post-commit auto-stamp / pre-commit warning hooks")
    ph.add_argument("--install", action="store_true", help="install into .git/hooks/")
    ph.add_argument("--uninstall", action="store_true", help="remove the recall hook")
    ph.add_argument("--pre-commit", action="store_true",
                    help="target the pre-commit risk-warning hook (default: post-commit)")
    ph.add_argument("--client", choices=["claude", "cursor", "windsurf"], default=None,
                    help="opt-in: install recall's situational-push hooks into the AI client's settings.json (Claude Code today; foreign entries preserved)")
    ph.add_argument("--repo", default=None)
    ph.set_defaults(func=cmd_hook)

    psc = sub.add_parser("stamp-commit", help="stamp HEAD's Recall-* trailers (what the hook calls)")
    psc.add_argument("--repo", default=None)
    psc.add_argument("--verbose", action="store_true", help="say something even when nothing to stamp")
    psc.set_defaults(func=cmd_stamp_commit)

    # Power Mode (ADR-008 / ADR-012) — connect an AI, then enrich write-time.
    pc = sub.add_parser("connect", help="connect the AI used for Power Mode (no default)")
    pc.add_argument("--provider", choices=["claude-cli", "ollama", "anthropic", "custom"], default=None,
                    help="claude-cli (your Max/Pro CLI, no key) · ollama (local) · anthropic (paid API) · custom (OpenAI-compatible)")
    pc.add_argument("--model", default=None,
                    help="model id, or for claude-cli the CLI command (e.g. `claude`)")
    pc.add_argument("--base-url", default=None, help="Ollama host or custom endpoint URL")
    pc.add_argument("--key-env", default=None, help="env var NAME holding the API key (never the key)")
    pc.add_argument("--show", action="store_true", help="show the current connection")
    pc.add_argument("--clear", action="store_true", help="disconnect")
    pc.set_defaults(func=cmd_connect)

    pp = sub.add_parser("power", help="Power Mode: enrich the index with your connected AI")
    pp.add_argument("path", nargs="?", default=None)
    pp.add_argument("--scope", default=None, help="limit to a path prefix, e.g. src/auth")
    pp.add_argument("--top-n", type=int, default=None, help="max hotspots (token budget)")
    pp.add_argument("--yes", action="store_true", help="run after the estimate (spends tokens)")
    pp.add_argument("--dry-run", action="store_true", help="preview without touching the index")
    pp.add_argument("--list", action="store_true", help="list past power runs")
    pp.add_argument("--repo", default=None)
    pp.set_defaults(func=cmd_power)

    prf = sub.add_parser("refine", help="classify depends_on edges (implements/guarded_by) with your AI")
    prf.add_argument("--yes", action="store_true", help="run after the estimate (spends tokens on a paid provider)")
    prf.add_argument("--dry-run", action="store_true", help="show the estimate only, never call the model")
    prf.add_argument("--repo", default=None)
    prf.set_defaults(func=cmd_refine)

    pur = sub.add_parser("unrefine", help="reset refined edges back to their original kind (reverse of refine)")
    pur.add_argument("path", nargs="?", default=None, help="project path (same as --repo; defaults to here)")
    pur.add_argument("--repo", default=None)
    pur.set_defaults(func=cmd_unrefine)

    pu = sub.add_parser("undo", help="reverse a power run (surgically or all)")
    pu.add_argument("--power-run", type=int, default=None)
    pu.add_argument("--all", action="store_true", help="undo every power run")
    pu.add_argument("--repo", default=None)
    pu.set_defaults(func=cmd_undo)

    pg = sub.add_parser("forget", help="remove a single node (refuses bootstrap without --force)")
    pg.add_argument("node_id", type=int)
    pg.add_argument("--force", action="store_true", help="allow forgetting a bootstrap node")
    pg.add_argument("--repo", default=None)
    pg.set_defaults(func=cmd_forget)

    pe = sub.add_parser("export", help="write a shareable brain copy with all --private notes left out")
    pe.add_argument("--out", required=True, help="where to write the shareable .mind copy (e.g. .mind/shared.db)")
    pe.add_argument("--repo", default=None)
    pe.set_defaults(func=cmd_export)

    return p


def _subcommand_names(parser) -> set[str]:
    """Every registered subcommand name, read off the parser's subparsers action — so the
    bare-query router can never drift out of sync with the actual commands."""
    import argparse as _ap

    names: set[str] = set()
    for action in parser._actions:
        if isinstance(action, _ap._SubParsersAction):
            names.update(action.choices.keys())
    return names


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    # Bare `recall "<query>"` (no subcommand) is the most common call — route it
    # to recall without forcing the user to type a subcommand. The set of real
    # subcommands is read from the parser itself (not a hand-kept list that drifts —
    # that's how `tray`/`stop` once fell through to being parsed as queries).
    # The top-level flags (--help, --version) must also reach the parser, not the
    # query router — otherwise `recall --version` is treated as a search for the
    # literal string "--version".
    known = _subcommand_names(parser) | {"-h", "--help", "--version"}
    if argv and argv[0] not in known:
        return _run_recall(argv)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return _dispatch(args.func, args)


def _dispatch(func, args) -> int:
    """Run a command with a top-level error boundary, so a provider/network/subprocess
    failure (a down Ollama, an expired API key, a PowerShell .lnk error) prints a clean
    line and exits non-zero instead of dumping a raw traceback on a paying user. The
    dashboard worker already guards its own calls; this is the CLI's equivalent.
    KeyboardInterrupt and SystemExit pass through untouched (clean Ctrl-C, argparse exits).

    W2 (D3/Q1): the LICENSE GATE runs FIRST — every command except login/logout/stop
    requires a verified, in-window license. If not signed in, the gate starts the
    browser device-flow itself (D7) and only proceeds once activated."""
    from recall.login import ensure_licensed, NotLicensed
    try:
        ensure_licensed(getattr(func, "__name__", ""))
    except NotLicensed as e:
        print(_c(str(e), C.RED))
        return 1
    except KeyboardInterrupt:
        print(_c("\nlogin cancelled", C.DIM))
        return 130
    try:
        return func(args)
    except KeyboardInterrupt:
        print(_c("\ninterrupted", C.DIM))
        return 130
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — the boundary's whole job is to catch broadly
        # one friendly line; the class name helps support without a scary traceback.
        print(_c(f"error: {e}", C.RED))
        if os.environ.get("RECALL_DEBUG"):
            raise  # opt-in full traceback for debugging
        return 1


def _run_recall(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="recall")
    p.add_argument("query")
    p.add_argument("--for-prompt", action="store_true", help="copy-paste block for web AI")
    p.add_argument("--terse", action="store_true", help="machine-first compact block (for AI agents via Bash; keeps WHY verbatim, compresses the WHERE list)")
    p.add_argument("--context", default=None, help="edit context (file path) for boosting")
    p.add_argument("--topk", type=int, default=3)
    p.add_argument("--repo", default=None)
    args = p.parse_args(argv)
    return _dispatch(cmd_recall, args)


if __name__ == "__main__":
    raise SystemExit(main())

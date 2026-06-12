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


def _open_existing(repo: Path) -> Index | None:
    idx_path = _index_path(repo)
    if not idx_path.exists():
        return None
    return Index.open(idx_path, repo=repo)


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

    repo = Path(args.path).resolve()
    if not repo.exists():
        print(_c(f"path not found: {repo}", C.RED))
        return 1
    idx_path = _index_path(repo)
    idx = Index.open(idx_path, repo=repo)
    print(_c(f"recall · indexing {repo} …", C.DIM))
    st = init(idx, repo, max_commits=args.max_commits, code_map=not args.no_code_map)
    s = idx.stats()
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
    return 0


def cmd_recall(args) -> int:
    repo = _find_repo(args.repo or ".")
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    res = idx.recall(args.query, edit_context=args.context, topk=args.topk, consumer="cli")
    if args.for_prompt:
        print(_format_for_prompt(args.query, res))
        return 0
    _print_pretty(args.query, res)
    return 0 if not res["silenced"] else 0


def cmd_brief(args) -> int:
    """Wave A — the Pre-Edit Briefing. Everything recall knows about ONE file, before
    you touch it: why it is the way it is, what breaks, which open tasks affect it."""
    repo = _find_repo(args.repo or ".")
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    b = idx.brief(args.file)
    if args.for_prompt:
        print(_format_brief_for_prompt(b))
        return 0
    _print_brief(b)
    return 0


def cmd_contested(args) -> int:
    """Wave B — uncertainty hotspots: the code the team kept changing (high churn AND
    entanglement). Answers 'where does the team burn time', model-free (ADR-019)."""
    repo = _find_repo(args.repo or ".")
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


def cmd_explain(args) -> int:
    """Wave C — "explain me this repo" (ADR-020). The generated orientation path a new
    dev or a fresh AI session needs: load-bearing files, must-know decisions, what's in
    progress, where time burns. Read-only, model-free."""
    repo = _find_repo(args.repo or ".")
    idx = _open_existing(repo)
    if idx is None:
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    o = idx.onboarding()
    if args.for_prompt:
        print(_format_explain_for_prompt(o, repo.name))
        return 0
    _print_explain(o, repo.name)
    return 0


def cmd_review(args) -> int:
    """Wave D — review a change (ADR-021). `recall review <sha>` bundles, per file the
    commit touched, what brief() shows for one file (what breaks / why / open tasks /
    drift) and singles out the RISK files (load-bearing + many dependents + open task).
    `--for-prompt` renders a PR-markdown block. Read-only, model-free."""
    repo = _find_repo(args.repo or ".")
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
    repo = _find_repo(args.repo or ".")
    idx = _open_existing(repo)
    if idx is None:
        return 0  # no memory yet — nothing to warn about, never block
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"],
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
    return 0  # warn only, never block


def cmd_stamp(args) -> int:
    repo = _find_repo(args.repo or ".")
    idx = _open_existing(repo) or Index.open(_index_path(repo), repo=repo)
    r = idx.stamp(
        title=args.title,
        body=args.body,
        anchors=args.anchors.split(",") if args.anchors else None,
        tags=args.tags.split(",") if args.tags else None,
        file_path=args.file,
        origin="live",
    )
    if r["action"] == "MERGE":
        print(_c(f"✓ merged into: {r['into']}", C.GREEN) + _c(f"  (overlap {r['overlap']})", C.DIM))
    else:
        print(_c(f"✓ stamped #{r['node_id']}", C.GREEN) + _c(f"  ({r['anchors']} anchors)", C.DIM))
    return 0


def cmd_freshen(args) -> int:
    repo = _find_repo(args.repo or ".")
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
    print(
        "  "
        + _c(f"● {s['fresh']} fresh", C.GREEN) + _c("  ·  ", C.DIM)
        + _c(f"● {s['committed']} drifted", C.MUSTARD) + _c("  ·  ", C.DIM)
        + _c(f"● {s['uncommitted']} edited", C.RED)
    )
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
        print(_c("recall · MCP — plug the project memory into your AI client", C.B))
        print(_c("\n  Claude Code (run inside the project):", C.DIM))
        print("    claude mcp add recall -- recall mcp")
        print(_c("\n  .mcp.json (Claude Code project config / checked in for the team):", C.DIM))
        print('    {"mcpServers": {"recall": {"command": "recall", "args": ["mcp"]}}}')
        print(_c("\n  Cursor (~/.cursor/mcp.json) and most other clients use the same shape.", C.DIM))
        print(_c("\n  Tools: recall · brief · explain · stamp · contested · freshen", C.DIM))
        print(_c("  Requires an index: run `recall init .` in the project once.", C.DIM))
        return 0
    from recall import mcp  # lazy, like dashboard — the CLI core stays import-light

    return mcp.serve(args.repo)


def cmd_dashboard(args) -> int:
    """Start the local dashboard — the small app that makes the wiki visible."""
    from recall import dashboard

    repo = _find_repo(args.repo or ".")
    idx_path = _index_path(repo)
    if not idx_path.exists():
        print(_c(f"no index — run `recall init` in {repo} first", C.RED))
        return 1
    return dashboard.serve(
        repo, idx_path, host=args.host, port=args.port, open_browser=not args.no_open,
        watch=not args.no_watch,
    )


def cmd_shortcut(args) -> int:
    """Put a double-click dashboard launcher on the Desktop — no AI, no terminal."""
    from recall import shortcut

    repo = _find_repo(args.repo or ".")
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

    repo = _find_repo(args.repo or ".")
    out = stamp_latest_commit(repo)
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

    repo = _find_repo(args.repo or ".")
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


def cmd_stats(args) -> int:
    repo = _find_repo(args.repo or ".")
    idx = _open_existing(repo)
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
        print(
            "  freshness: "
            + _c(f"{d['fresh']} fresh", C.GREEN) + ", "
            + _c(f"{d['committed']} drifted", C.MUSTARD) + ", "
            + _c(f"{d['uncommitted']} edited", C.RED)
        )
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
    from recall.refine import refine_edges

    repo = _find_repo(args.repo or ".")
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
    print(_c("recall · refine", C.B)
          + _c(f"  ({provider.name} · {provider.model}) — {have} edges", C.DIM))

    def progress(done, total):
        if done % 25 == 0 or done == total:
            print(_c(f"  [{done}/{total}] files", C.DIM))

    res = refine_edges(idx, provider, progress=progress)
    kinds = ", ".join(f"{k}={v}" for k, v in sorted(res.by_kind.items()))
    print(_c(f"✓ refined {res.edges_refined} edges", C.GREEN)
          + f"  ({res.files_seen} files · {kinds or 'no change'})")
    if res.dropped_labels:
        print(_c(f"  {res.dropped_labels} invalid labels dropped (kept as depends_on)", C.DIM))
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
          f"~{est.input_tokens:,} in + ~{est.est_output_tokens:,} out tokens")
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
    repo = _find_repo(args.repo or ".")
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
    repo = _find_repo(args.repo or ".")
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


def _format_for_prompt(query: str, res: dict[str, Any]) -> str:
    """Web-AI path 1: a copy-paste context block for ChatGPT/Gemini/Claude-web.
    Carries the 3 tracks with self-explanatory CAPS headers (the _format_brief_for_prompt
    house style) — this text is pasted into OTHER AIs, so it must stand on its own."""
    if res["silenced"]:
        return f"[recall] no project memory for: {query}"
    lines = [f"[recall · project memory for: {query}]"]
    drift_warn = DRIFT_WARN

    code = res.get("code", [])
    if code:
        lines.append("")
        lines.append("WHERE (code, by importance):")
        for it in code:
            loc = (it.get("file") or "") + (f":{it['line']}" if it.get("line") else "")
            sym = f" {it['symbol']}" if it.get("symbol") else ""
            lines.append(f"- {loc}{sym} (importance {int(it['importance'])})")
            warn = drift_warn.get(it.get("drift"))
            if warn:
                lines.append(warn)
            if it.get("why"):
                lines.append(f"  why: {it['why']}")

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


# Node-level drift warnings for prompt blocks (ONE copy — the brief formatter keeps
# its own FILE-level wording, which is a different claim).
DRIFT_WARN = {"committed": "  ⚠ may be stale — file changed since this was stamped",
              "uncommitted": "  ⚠ stale — file has uncommitted edits right now"}


def _brief_drift_tag(level: str | None) -> str:
    """A short drift badge for the briefing header (mirrors _drift_badge's meaning)."""
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

    tasks = b.get("open_tasks", [])
    if tasks:
        print(_c("\n  ⚠ open tasks on this file — read before you edit:", C.MUSTARD))
        for t in tasks:
            print(f"    {_c('▸', C.MUSTARD)} {t['title']}")

    if b["why"]:
        print(_c("\n  why it is the way it is:", C.B))
        for w in b["why"]:
            sha = _c(f" ({w['sha']})", C.DIM) if w.get("sha") else ""
            drift = _c("  ⚠ stale", C.RED) if w.get("drift") in ("committed", "uncommitted") else ""
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

    if b["symbols"]:
        names = ", ".join(s["symbol"] for s in b["symbols"][:12])
        more = "" if len(b["symbols"]) <= 12 else f" … +{len(b['symbols'])-12}"
        print(_c("\n  symbols: ", C.B) + _c(names + more, C.DIM))


def _format_brief_for_prompt(b: dict[str, Any]) -> str:
    """Web-AI path: a copy-paste briefing block to paste before editing the file."""
    if not b["known"]:
        return f"[recall · brief] no project memory for: {b['file']}"
    lines = [f"[recall · pre-edit briefing for: {b['file']}]", ""]
    drift = {"committed": "⚠ this file changed since some knowledge was stamped — verify before trusting it",
             "uncommitted": "⚠ this file has uncommitted edits right now"}.get(b.get("drift"))
    if drift:
        lines.append(drift)
        lines.append("")
    if b.get("open_tasks"):
        lines.append("OPEN TASKS on this file — read these first, they are standing intent:")
        for t in b["open_tasks"]:
            lines.append(f"  - {t['title']}")
        lines.append("")
    if b["why"]:
        lines.append("WHY this file is the way it is:")
        for w in b["why"]:
            sha = f" (sha {w['sha']})" if w.get("sha") else ""
            lines.append(f"  - [{w['kind']}] {w['title']}{sha}")
            if w.get("why"):
                lines.append(f"      {w['why']}")
        lines.append("")
    if b["breaks"]:
        lines.append("WHAT BREAKS if you change it (files that depend on it):")
        for x in b["breaks"][:10]:
            lines.append(f"  - {x['file']} ({x['kind']})")
        lines.append("")
    if b["depends_on"]:
        lines.append("WHAT IT LEANS ON (depends_on):")
        for d in b["depends_on"][:10]:
            lines.append(f"  - {d['target']} ({d['kind']})")
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
        if f.get("drift") in ("committed", "uncommitted"):
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


def _format_explain_for_prompt(o: dict[str, Any], repo_name: str) -> str:
    """Web-AI path: a copy-paste repo orientation block for a fresh AI session."""
    c = o["counts"]
    lines = [f"[recall · repo orientation for: {repo_name}]",
             f"{c['files']} files · {c['lessons']} lessons · {c['open_tasks']} open tasks", ""]
    if o["top_files"]:
        lines.append("START HERE — the load-bearing files (by causal importance):")
        for f in o["top_files"][:10]:
            lines.append(f"  - {f['file']} ({f['symbols']} symbols, importance {int(f['importance'])})")
        lines.append("")
    if o["decisions"]:
        lines.append("MUST-KNOW DECISIONS:")
        for d in o["decisions"][:8]:
            sha = f" (sha {d['sha']})" if d.get("sha") else ""
            lines.append(f"  - {d['title']}{sha}")
        lines.append("")
    if o["in_progress"]:
        lines.append("IN PROGRESS RIGHT NOW (open tasks — standing intent):")
        for t in o["in_progress"][:12]:
            lines.append(f"  - {t['title']}")
        lines.append("")
    if o["contested"]:
        lines.append("WHERE THE TEAM BURNS TIME (touch with extra care):")
        for s in o["contested"][:6]:
            lines.append(f"  - {s['file']} (churn {s['churn']})")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recall", description="AI-native project memory. The code is the wiki.")
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("init", help="index a project")
    pi.add_argument("path", nargs="?", default=".")
    pi.add_argument("--max-commits", type=int, default=400)
    pi.add_argument("--no-code-map", action="store_true", help="skip tree-sitter code map")
    pi.set_defaults(func=cmd_init)

    pb = sub.add_parser("brief", help="pre-edit briefing for a file (why / what breaks / open tasks)")
    pb.add_argument("file", help="the file you're about to edit (repo-relative)")
    pb.add_argument("--for-prompt", action="store_true", help="copy-paste briefing block for web AI")
    pb.add_argument("--repo", default=None)
    pb.set_defaults(func=cmd_brief)

    pco = sub.add_parser("contested", help="uncertainty hotspots — code the team kept changing")
    pco.add_argument("--limit", type=int, default=20)
    pco.add_argument("--min-churn", type=int, default=2, help="ignore files touched by fewer commits")
    pco.add_argument("--repo", default=None)
    pco.set_defaults(func=cmd_contested)

    pex = sub.add_parser("explain", help="explain the repo to a new dev/AI: top files, decisions, what's in progress")
    pex.add_argument("--for-prompt", action="store_true", help="copy-paste orientation block for web AI")
    pex.add_argument("--repo", default=None)
    pex.set_defaults(func=cmd_explain)

    prv = sub.add_parser("review", help="review a commit: what it breaks, risk files, PR-markdown")
    prv.add_argument("sha", nargs="?", default=None, help="commit SHA (default HEAD)")
    prv.add_argument("--for-prompt", action="store_true", help="PR-markdown block for a pull request")
    prv.add_argument("--repo", default=None)
    prv.set_defaults(func=cmd_review)

    ppc = sub.add_parser("precommit-check", help="warn (never block) when staged files are load-bearing (what the pre-commit hook calls)")
    ppc.add_argument("--repo", default=None)
    ppc.set_defaults(func=cmd_precommit_check)

    ps = sub.add_parser("stamp", help="stamp a node by hand")
    ps.add_argument("title")
    ps.add_argument("--body", default=None)
    ps.add_argument("--anchors", default=None, help="comma-separated")
    ps.add_argument("--tags", default=None, help="comma-separated")
    ps.add_argument("--file", default=None)
    ps.add_argument("--repo", default=None)
    ps.set_defaults(func=cmd_stamp)

    pf = sub.add_parser("freshen", help="re-check pinned nodes for drift against git")
    pf.add_argument("--repo", default=None)
    pf.set_defaults(func=cmd_freshen)

    pt = sub.add_parser("stats", help="show index stats")
    pt.add_argument("--repo", default=None)
    pt.set_defaults(func=cmd_stats)

    pd = sub.add_parser("dashboard", help="open the local dashboard (the wiki, visible)")
    pd.add_argument("--repo", default=None)
    pd.add_argument("--port", type=int, default=7099)
    pd.add_argument("--host", default="127.0.0.1")
    pd.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    pd.add_argument("--no-watch", action="store_true",
                    help="don't auto-index when a new commit lands (live mode is on by default)")
    pd.set_defaults(func=cmd_dashboard)

    psh = sub.add_parser("shortcut", help="put a double-click dashboard launcher on your Desktop (no AI needed)")
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
    prf.add_argument("--repo", default=None)
    prf.set_defaults(func=cmd_refine)

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

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    # Bare `recall "<query>"` (no subcommand) is the most common call — route it
    # to recall without forcing the user to type a subcommand.
    known = {"init", "brief", "contested", "explain", "review", "precommit-check", "stamp", "stamp-commit",
             "stats", "freshen", "dashboard", "shortcut", "hook", "mcp",
             "connect", "power", "refine", "undo", "forget", "-h", "--help"}
    if argv and argv[0] not in known:
        return _run_recall(argv)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


def _run_recall(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="recall")
    p.add_argument("query")
    p.add_argument("--for-prompt", action="store_true", help="copy-paste block for web AI")
    p.add_argument("--context", default=None, help="edit context (file path) for boosting")
    p.add_argument("--topk", type=int, default=3)
    p.add_argument("--repo", default=None)
    args = p.parse_args(argv)
    return cmd_recall(args)


if __name__ == "__main__":
    raise SystemExit(main())

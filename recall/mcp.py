"""MCP server (Phase M, ADR-029) — recall as native tools in any MCP client.

Pure stdlib. MCP's stdio transport is newline-delimited JSON-RPC 2.0 (spec
2025-06-18, verified against modelcontextprotocol.io): five methods carry the
whole protocol — initialize, notifications/initialized, ping, tools/list,
tools/call. Implementing them directly keeps recall zero-dependency, so
`claude mcp add recall -- recall mcp` is the ENTIRE install (the beta plan's
zero-friction requirement). The official SDK would pull ~10 packages for this.

Hard rules of the transport (spec):
  - stdout carries ONLY protocol messages, one JSON object per line, UTF-8,
    no embedded newlines. Logs go to stderr.
  - Tool EXECUTION errors are results with isError:true; protocol errors
    (unknown tool/method, bad JSON) are JSON-RPC error objects.

The read path stays model-free (ADR-014): every tool is SQL over the local
.mind/index.db. The only writing tool is `stamp` — the same write-time door
the CLI and the git hook use.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

from recall.engine import Index

_SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# What an LLM should know about recall before its first call — sent once in the
# initialize result. Kept short: clients prepend this to the model's context.
_INSTRUCTIONS = (
    # Chain-first / negation-first (the adoption fix, 2026-06-17): an AI pattern-matches a
    # verb list to its nearest training cluster {search, RAG} and confabulates the rest. So
    # lead with what recall IS NOT, then the mental model, THEN the verbs.
    "recall is NOT a search engine and NOT a token-saver (that's a side effect). It is "
    "THIS project's write-time memory: it tells you WHY each file is the way it is, WHAT "
    "BREAKS if you touch it, WHAT'S IN FLIGHT, and the REAL name to search for — written "
    "at commit time, read back offline at 0 model tokens. grep finds code; recall tells you "
    "the reasons grep can't see. Treat the project as a coworker with a memory you must "
    "consult, not a pile of text to re-derive.\n\n"
    "HOW YOU MUST USE IT (orient → locate → edit):\n"
    "1. `explain` once at session start for the lay of the land (load-bearing files, "
    "must-know decisions, what's in progress).\n"
    "2. Before editing ANY file you MUST call `brief <file>` first — it returns the why, the "
    "open tasks, and the blast radius at 0 tokens. Skipping it never saves tokens; it only "
    "loses the context that stops you silently undoing a deliberate decision.\n"
    "3. Before you grep a name you're GUESSING, call `resolve <guess>` — it maps your "
    "invented term to what this repo actually calls it (the #1 reason a grep finds nothing).\n"
    "4. Call `recall \"<question>\"` for where/why/what-breaks questions.\n"
    "5. After a decision or lesson, `stamp` it so the memory compounds for the next session.\n\n"
    "`dashboard` opens the browsable wiki. No index? run `recall init .` once. Same actions "
    "exist as slash commands (/recall /brief /explain /resolve /dashboard). If you SPAWN "
    "subagents, they can't see this MCP server (session-scoped) — have them call the recall "
    "CLI instead: `recall brief <file> --terse` before each agent judges a file."
)

# Workstream E: appended ONLY when the commit-synced STATE block is actually present in an
# agent-config file (otherwise the sentence would lie). The block is the always-present floor
# regardless of A's per-prompt push gating — so this is true under A's gated default.
_STATE_SENTENCE = (
    "\n\nNOTE: this project's baseline orientation is ALREADY in your system prompt as the recall "
    "STATE block (re-synced into the AI instruction file on every commit). These tools are the "
    "on-demand FALLBACK — prefer the cached block, and keep tool calls few and terse."
)


def _sync_context_installed(repo) -> bool:
    """True iff recall's STATE block markers are present in an agent-config file (so sync-context
    is effectively installed) — only then is the 'baseline is in your system prompt' sentence TRUE."""
    from recall.engine import Index
    for rel in ("CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md", ".cursor/rules"):
        try:
            p = repo / rel
            if p.exists() and Index.STATE_BEGIN in p.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def _instructions(session: "_Session") -> str:
    try:
        if _sync_context_installed(session.repo):
            return _INSTRUCTIONS + _STATE_SENTENCE
    except Exception:
        pass
    return _INSTRUCTIONS


def _server_version() -> str:
    try:
        from importlib.metadata import version

        return version("whatever-recall")
    except Exception:
        return "0.0.0"


# ------------------------------------------------------------------ session
class _Session:
    """Repo + lazily-opened index for one stdio connection.

    Index.open() CREATES a db file — opening blindly would plant an empty index
    and make every later no-index detection lie (the bench_v2 lesson), so the
    exists() check stays mandatory."""

    def __init__(self, repo: str | Path | None = None):
        from recall.cli import _find_repo

        self.repo = _find_repo(repo or ".")
        self._idx: Index | None = None

    def index(self) -> Index | None:
        if self._idx is None:
            from recall.cli import _open_existing

            self._idx = _open_existing(self.repo)
        return self._idx


_NO_INDEX = (
    "No recall index in {repo} — run `recall init .` there once (token-free, "
    "offline), then this tool answers from the project's own memory."
)


# ------------------------------------------------------------------ tools
def _tool_recall(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    res = idx.recall(a["query"], edit_context=a.get("context"), consumer="mcp")
    if res.get("silenced"):
        return ("recall stays silent — no reliable knowledge for this query "
                f"({res.get('reason', 'no match')}). That is deliberate: no guessing.")
    from recall.cli import _format_for_prompt

    return _format_for_prompt(a["query"], res)


def _tool_brief(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    from recall.cli import _format_brief_for_prompt

    # terse=True: the MCP caller is a machine (Claude/Cursor), so compress the
    # structural lists but keep the WHY verbatim — machine-first, fewer tokens.
    return _format_brief_for_prompt(idx.brief(a["file"], consumer="mcp"), terse=True)


def _tool_explain(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    from recall.cli import _format_explain_for_prompt

    # terse=True: machine caller — tighter caps, drop the contested section, keep
    # the decisions + open tasks that actually orient an AI's judgment.
    return _format_explain_for_prompt(idx.onboarding(consumer="mcp"), s.repo.name, terse=True)


def _tool_resolve(s: _Session, a: dict) -> str:
    """Search-inversion (ADR-037): correct a hallucinated search term into this
    repo's real vocabulary before the caller greps it."""
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    from recall.cli import _format_resolve_for_prompt

    return _format_resolve_for_prompt(idx.resolve(a["guess"], top=a.get("top", 5)))


def _tool_push(s: _Session, a: dict) -> str:
    """Workstream A — the situational push as a user-invoked PROMPT (read once, embedded
    server-side — NOT a lingering tool): the scoped brief + landmines + live BROKEN trust-status
    for the file and/or task the caller is on. With no args it degrades to the repo state block."""
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    return idx.render_situational_block(focus_file=a.get("file"), task=a.get("task"))


def _tool_stamp(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    try:
        r = idx.stamp(
            title=a["title"],
            body=a.get("body"),
            anchors=a.get("anchors"),
            tags=a.get("tags"),
            file_path=a.get("file"),
            predicate=a.get("predicate"),
            outcome=a.get("outcome"),
            origin="live",
            consumer="mcp",
        )
    except ValueError as e:  # unparseable predicate / bad path — surface, don't store it
        return f"NOT stamped — {e}"
    pred = f"  predicate set (re-checked free on every freshen)." if a.get("predicate") else ""
    if r["action"] == "MERGE":
        return f"merged into existing note: {r['into']} (anchor overlap {r['overlap']}).{pred}"
    return f"stamped #{r['node_id']} ({r['anchors']} anchors) — the next session will know.{pred}"


def _tool_contested(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    spots = idx.contested_spots(repo=s.repo, limit=int(a.get("limit", 10)))
    if not spots:
        return "no contested spots — too little git history for a churn signal."
    lines = ["CONTESTED SPOTS (where the team burns time — churn x entanglement):"]
    for sp in spots:
        lines.append(f"  {sp['score']:>5}  {sp['file']}  "
                     f"(churn {sp['churn']}, tangle {sp['entanglement']})")
    return "\n".join(lines)


def _tool_freshen(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    st = idx.freshen()
    out = [f"freshened: {st['checked']} pinned notes checked — "
           f"{st['fresh']} fresh, {st['committed']} drifted, {st['uncommitted']} edited."]
    if st.get("no_git"):
        out.append("warning: no .git — drift falls back to file existence only.")
    try:
        stale = idx.stale_decisions()
    except Exception:
        stale = []
    if stale:
        out.append(f"{len(stale)} decision(s) may be stale (their code moved on):")
        out.extend(f"  - {d['title'][:80]}" for d in stale[:5])
    return "\n".join(out)


def _probe_dashboard(url: str) -> bool:
    """Is a dashboard already answering at `url`? (separate fn so tests inject it)

    ANY HTTP response means a server is bound — including a 402 from a signed-out but
    LIVE dashboard (/api/pulse is gated). Reading only `status == 200` mis-read that as
    'not alive' and spawned a SECOND `recall dashboard --port 7099` that fails to bind
    (then the tool lied 'opening in the browser'). This is the SAME 402-is-alive fix as
    dashboard.is_dashboard_live — round 1 patched one of the two identical probes only.
    (P2 bug-hunt round 2, 2026-06-15: incomplete-fix regression.)"""
    import urllib.error
    from urllib.request import urlopen

    try:
        with urlopen(url + "/api/pulse", timeout=1.5) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return True  # a gated (402/403) response still proves a server is bound
    except Exception:
        return False  # connection refused / timeout — genuinely not alive


def _tool_dashboard(s: _Session, a: dict, *, probe=_probe_dashboard) -> str:
    """Open the local dashboard: reuse a running server on :7099 or spawn one
    detached (it opens the browser itself). Returns the URL either way — so a
    chat "open the dashboard" / the /dashboard slash command just works."""
    url = "http://127.0.0.1:7099"
    if probe(url):
        return f"The recall dashboard is already running: {url}"
    import subprocess

    kw: dict[str, Any] = {"cwd": str(s.repo), "stdin": subprocess.DEVNULL,
                          "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "recall.cli", "dashboard", "--port", "7099"], **kw)
    return (f"Started the recall dashboard for {s.repo.name} — it is opening in the "
            f"browser; otherwise visit {url}")


# --------------------------------------------- code intelligence (static-code-intel serves)
# impact + precedent (arrow 3) and the file-granular code-intel serves, exposed to MCP clients
# (Claude/Cursor) so a real user gets the navigation answers without shelling out. Subagent fleets
# still use the CLI (MCP is session-scoped — they can't reach it), per the dogfood lesson.
def _tool_impact(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    from recall.cli import _format_impact_for_prompt
    return _format_impact_for_prompt(
        idx.impact(a["target"], depth=int(a.get("depth", 2)), consumer="mcp"))


def _tool_precedent(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    from recall.cli import _format_precedent_for_prompt
    return _format_precedent_for_prompt(
        idx.precedent(a["situation"], limit=int(a.get("limit", 5)), consumer="mcp"))


def _tool_callers(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    from recall.cli import _format_hierarchy_for_prompt
    fn = idx.callees if a.get("callees") else idx.callers
    return _format_hierarchy_for_prompt(
        fn(a["target"], depth=int(a.get("depth", 2)), consumer="mcp"))


# Workstream E: the listing serves are terse-by-default + pointer-first — a digest of the top
# rows on the MCP surface (no lingering 50-row tax), with the full list one read-once CLI call away.
_MCP_DIGEST = 12


def _pointer(text: str, cli_cmd: str) -> str:
    return f"{text}\n\nfull list: run `{cli_cmd}` (CLI, read-once, no MCP residue)."


def _tool_dead_code(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    from recall.cli import _format_listing_for_prompt
    text = _format_listing_for_prompt(
        idx.dead_code(limit=int(a.get("limit", _MCP_DIGEST)), consumer="mcp"),
        "dead-code", "candidates",
        "code files nothing imports (candidates — verify; dynamic imports invisible)")
    return _pointer(text, "recall dead-code --limit 50")


def _tool_untested(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    from recall.cli import _format_listing_for_prompt
    text = _format_listing_for_prompt(
        idx.untested(limit=int(a.get("limit", _MCP_DIGEST)), consumer="mcp"),
        "untested", "untested", "code files with no recorded test edge (file-granular)")
    return _pointer(text, "recall untested --limit 50")


def _tool_cycles(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    from recall.cli import _format_cycles_for_prompt
    text = _format_cycles_for_prompt(idx.cycles(limit=int(a.get("limit", _MCP_DIGEST)), consumer="mcp"))
    return _pointer(text, "recall cycles --limit 50")


class _NoIndex(Exception):
    pass


_STR = {"type": "string"}
_TOOLS: list[dict[str, Any]] = [
    {
        "name": "recall",
        "title": "Ask the project's memory",
        "description": (
            "Query this project's recall index: WHERE is something implemented, WHY "
            "is it the way it is (decisions/lessons/commits), WHAT BREAKS if you "
            "change it, and which OPEN TASKS touch it. Offline, zero tokens. "
            "Stays silent rather than guessing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {**_STR, "description": "natural-language question or search terms"},
                "context": {**_STR, "description": "optional: file you are editing (boosts its facet)"},
            },
            "required": ["query"],
        },
        "handler": _tool_recall,
    },
    {
        "name": "brief",
        "title": "Pre-edit briefing for a file",
        "description": (
            "Call this BEFORE editing a file. Returns everything recall knows about "
            "it: why it is the way it is (pinned decisions/lessons), what depends on "
            "it (blast radius), what it leans on, open tasks wired to it, and its "
            "symbols — so you don't silently undo a deliberate decision."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {**_STR, "description": "repo-relative file path"},
            },
            "required": ["file"],
        },
        "handler": _tool_brief,
    },
    {
        "name": "explain",
        "title": "Explain this repo",
        "description": (
            "Orientation for a fresh session: the load-bearing files, the must-know "
            "decisions, what's in progress, where the team burns time. Start here "
            "in an unfamiliar project."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_explain,
    },
    {
        "name": "resolve",
        "title": "Correct a search term into this repo's real vocabulary",
        "description": (
            "Search-inversion: BEFORE you grep a name you're guessing, call this — it "
            "maps the hallucinated term to what THIS repo actually calls it (e.g. "
            "'seatLimit' -> 'confirmSeatOrRollback'). Corrects the vocabulary mismatch "
            "from the repo's lived experience, so you don't burn a round grepping a "
            "term that doesn't exist here."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "guess": {**_STR, "description": "the term you're guessing (e.g. seatLimit)"},
            },
            "required": ["guess"],
        },
        "handler": _tool_resolve,
    },
    {
        "name": "stamp",
        "title": "Write knowledge into the project memory",
        "description": (
            "Record a decision, lesson or gotcha so every future session knows it. "
            "Use after fixing a tricky bug or making a deliberate choice. Anchors "
            "are the search terms it should be found by. OPTIONAL predicate: a "
            "re-runnable CHECK that lets recall re-verify your claim free on every "
            "commit — it catches a 'why' that was wrong from the start (which SHA "
            "drift can never see). Pass it when the claim is about CODE that is "
            "checkable (e.g. 'always lowercases' -> contains:\\.lower\\(\\))."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {**_STR, "description": "one-line claim, e.g. 'RLS: writers must set workspace_id'"},
                "body": {**_STR, "description": "the why — 1-3 sentences of explanation"},
                "anchors": {"type": "array", "items": _STR,
                            "description": "search terms (symbols, concepts) this should match"},
                "tags": {"type": "array", "items": _STR,
                         "description": "optional facets, e.g. security, performance, bugfix"},
                "file": {**_STR, "description": "optional: file this knowledge is pinned to"},
                "predicate": {**_STR, "description":
                    "optional re-runnable check: 'contains:<regex>' / 'absent:<regex>' "
                    "clauses joined by ' && '. HOLDS = claim still true. Checked against "
                    "the pinned file's text on every freshen() — flags 🔴 BROKEN when it fails."},
                "outcome": {**_STR, "description":
                    "optional: what CAME of this decision — what was learned, or how it turned "
                    "out. The END of the causal chain, kept DISTINCT from the title (which is "
                    "the decision itself). Omit it honestly if nothing came of it yet."},
            },
            "required": ["title"],
        },
        "handler": _tool_stamp,
    },
    {
        "name": "contested",
        "title": "Uncertainty hotspots",
        "description": (
            "Files the team kept changing (high churn AND entanglement) — where "
            "time burns and extra care pays off."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "max spots (default 10)"},
            },
        },
        "handler": _tool_contested,
    },
    {
        "name": "freshen",
        "title": "Re-check knowledge against git",
        "description": (
            "Re-verify every pinned note against the current git state (fresh / "
            "drifted / edited) and flag decisions whose code moved on a lot."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_freshen,
    },
    {
        "name": "dashboard",
        "title": "Open the recall dashboard",
        "description": (
            "Start (or find) the local recall dashboard and return its URL — the "
            "browsable wiki: knowledge graph, freshness, tasks, code map. Local "
            "only; nothing leaves the machine."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_dashboard,
    },
    {
        "name": "impact",
        "title": "What's affected if you change this",
        "description": (
            "'If I touch this file/symbol, what actually breaks?' — fuses empirical "
            "co-change (what git history proves moves together) with structural "
            "dependents, ranked by importance. The 0-token read-time call-hierarchy "
            "replacement. Call before a risky edit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {**_STR, "description": "a file (a/b.py) or a symbol name"},
                "depth": {"type": "integer", "description": "structural hops to walk (default 2)"},
            },
            "required": ["target"],
        },
        "handler": _tool_impact,
    },
    {
        "name": "precedent",
        "title": "Have we been here before?",
        "description": (
            "Given a situation you're about to act in ('switching auth to JWT', "
            "'adding a money path'), serve the most ANALOGOUS past decisions/lessons, "
            "each with how it turned out (superseded? became a landmine? drifted?). "
            "Generalize from THIS repo's lived experience, not your priors."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "situation": {**_STR, "description": "what you're about to do"},
                "limit": {"type": "integer", "description": "max precedents (default 5)"},
            },
            "required": ["situation"],
        },
        "handler": _tool_precedent,
    },
    {
        "name": "callers",
        "title": "Who depends on this (call-hierarchy)",
        "description": (
            "The file-granular call-hierarchy: every file that depends on (imports/uses) "
            "the target, transitively, by hop. Set callees=true to invert (what the "
            "target depends on). File-granular by design — recall builds no per-call-site edges."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {**_STR, "description": "a file or a symbol name"},
                "callees": {"type": "boolean", "description": "invert: what THIS depends on"},
                "depth": {"type": "integer", "description": "hops to walk (default 2)"},
            },
            "required": ["target"],
        },
        "handler": _tool_callers,
    },
    {
        "name": "dead_code",
        "title": "Dead-code candidates",
        "description": (
            "Code files that exist on disk but nothing in the recorded graph imports — "
            "dead-code CANDIDATES. Conservative: excludes tests, entry/framework files, "
            "docs. File-granular can't see dynamic imports, so verify before deleting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "max candidates (default 50)"}},
        },
        "handler": _tool_dead_code,
    },
    {
        "name": "untested",
        "title": "Untested code files",
        "description": (
            "Code files with NO recorded test edge — nothing in tests/ depends on or "
            "co-changed with them. File-granular 'what has no test?'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "max files (default 50)"}},
        },
        "handler": _tool_untested,
    },
    {
        "name": "cycles",
        "title": "Dependency cycles",
        "description": (
            "File→file import cycles in the depends_on graph (A imports B imports ... A). "
            "Each distinct cycle reported once."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "max cycles (default 50)"}},
        },
        "handler": _tool_cycles,
    },
]


# ------------------------------------------------------------------- prompts
# Slash commands (spec: prompts are USER-controlled — clients surface them e.g.
# as /mcp__recall__brief). prompts/get EXECUTES server-side and embeds the LIVE
# result text, so the slash command lands the content in context directly — no
# extra tool roundtrip. Same model-free read path underneath (ADR-014).
_PROMPTS: list[dict[str, Any]] = [
    {
        "name": "recall",
        "title": "Ask the project's memory",
        "description": "Where/why/what-breaks answer from the project's recall index.",
        "arguments": [{"name": "query", "description": "your question or search terms",
                       "required": True}],
        "handler": _tool_recall,
    },
    {
        "name": "brief",
        "title": "Pre-edit briefing for a file",
        "description": "Everything recall knows about ONE file — run before editing it.",
        "arguments": [{"name": "file", "description": "repo-relative file path",
                       "required": True}],
        "handler": _tool_brief,
    },
    {
        "name": "explain",
        "title": "Explain this repo",
        "description": "Orientation: load-bearing files, must-know decisions, what's in flight.",
        "arguments": [],
        "handler": _tool_explain,
    },
    {
        "name": "resolve",
        "title": "Correct a search term into this repo's vocabulary",
        "description": "Search-inversion: map a guessed name to what this repo actually calls it.",
        "arguments": [{"name": "guess", "description": "the term you're guessing",
                       "required": True}],
        "handler": _tool_resolve,
    },
    {
        "name": "dashboard",
        "title": "Open the recall dashboard",
        "description": "Start (or find) the local dashboard and get its URL.",
        "arguments": [],
        "handler": _tool_dashboard,
    },
    {
        "name": "push",
        "title": "Situational memory for what you're doing now",
        "description": "Scoped brief + landmines + live BROKEN trust-status for a file and/or task — read once, no lingering tool.",
        "arguments": [{"name": "file", "description": "repo-relative file you're about to edit",
                       "required": False},
                      {"name": "task", "description": "what you're trying to do",
                       "required": False}],
        "handler": _tool_push,
    },
]


# ------------------------------------------- project registration (.mcp.json)
# The dashboard pill + toggle live on these three functions — same contract as
# adapters/hook.py for the git hooks: status / install / uninstall, idempotent,
# and NEVER clobbering anything foreign (other servers in the file stay).
_RECALL_SERVER = {"command": "recall", "args": ["mcp"]}


def _read_mcp_json(cfg: Path) -> dict | None:
    """Parse .mcp.json; None when the file is unreadable/invalid (callers refuse
    to write over a file they cannot faithfully preserve)."""
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def mcp_status(repo: str | Path, db=None) -> dict:
    """Is recall registered as a project MCP server, and when was it last USED?

    `registered` reads .mcp.json (the checked-in project scope every clone gets);
    `last_used` is honest evidence from the access_log (consumer='mcp') — a pill
    can say "registered AND actually answering", not just "a file exists"."""
    cfg = Path(repo) / ".mcp.json"
    data = _read_mcp_json(cfg) if cfg.exists() else {}
    servers = (data or {}).get("mcpServers") or {}
    last_used = None
    if db is not None:
        try:
            row = db.execute(
                "SELECT MAX(ts) FROM access_log WHERE consumer='mcp'").fetchone()
            last_used = row[0] if row else None
        except Exception:
            last_used = None
    return {
        "registered": isinstance(servers, dict) and "recall" in servers,
        "config_path": str(cfg),
        "last_used": last_used,
    }


def register_project(repo: str | Path) -> dict:
    """Write the recall entry into <repo>/.mcp.json (merge, never clobber)."""
    cfg = Path(repo) / ".mcp.json"
    data: dict = {}
    if cfg.exists():
        parsed = _read_mcp_json(cfg)
        if parsed is None:
            return {"ok": False,
                    "reason": ".mcp.json exists but is not valid JSON — fix it by hand, "
                              "I won't overwrite a file I can't preserve."}
        data = parsed
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return {"ok": False, "reason": ".mcp.json has a non-object mcpServers — fix it by hand."}
    servers["recall"] = dict(_RECALL_SERVER)
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"ok": True}


def unregister_project(repo: str | Path) -> dict:
    """Remove ONLY the recall entry; other servers (and the file, if they remain)
    stay. Deletes the file when recall was the last thing in it."""
    cfg = Path(repo) / ".mcp.json"
    if not cfg.exists():
        return {"ok": True}  # idempotent — already unregistered
    data = _read_mcp_json(cfg)
    if data is None:
        return {"ok": False,
                "reason": ".mcp.json is not valid JSON — fix it by hand, I won't touch it."}
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        servers.pop("recall", None)
        if not servers:
            data.pop("mcpServers", None)
    if not data:
        cfg.unlink()
    else:
        cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"ok": True}


# --------------------------------------------------------------- JSON-RPC core
def _ok(msg_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str, data: dict | None = None) -> dict:
    e: dict[str, Any] = {"code": code, "message": message}
    if data:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": e}


def _text_result(msg_id: Any, text: str, *, is_error: bool = False) -> dict:
    return _ok(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})


_NOT_SIGNED_IN = (
    "You're not signed in to whatever-recall, so recall can't run.\n\n"
    "To sign in: open a terminal and run  `recall login`  (it opens your browser; "
    "no key to copy). After you authorize the device, recall works here again.\n\n"
    "recall confirms your seat with a brief online check about once an hour, so once "
    "you're signed in this rarely interrupts you."
)


def _licensed() -> bool:
    """W2 (D7): is there a verified, in-window, non-pending license? MCP can't open
    a browser inside the stdio loop, so when unlicensed we return a clear, machine-
    readable instruction (NOT a silent error) — the user runs `recall login` once."""
    try:
        from recall import license as L
        state = L.load_license()
        return bool(state) and state.get("verified") and not state.get("expired") and not state.get("pending")
    except Exception:
        return False


def _served(session: "_Session", kind: str, msg_id, text: str, *, is_error: bool = False,
            consumer: str = "mcp") -> dict:
    """Workstream E choke point: record the EMITTED response SIZE once per call (best-effort,
    never breaks the call), then return the tools/call result. Covers success AND isError results
    — exactly one resp_chars row per MCP call, the context tax measured at the boundary."""
    idx = session.index()
    if idx is not None:
        try:
            idx._record_served(kind, consumer, len(text or ""))
        except Exception:
            pass
    return _text_result(msg_id, text, is_error=is_error)


def handle(msg: dict, session: _Session) -> dict | None:
    """One JSON-RPC message in, one response dict out (None for notifications).

    Pure function over (message, session) — the tests drive it directly, the
    stdio loop in serve() is just framing around it."""
    # A valid-JSON-but-non-object message (a bare number/string/null, or a
    # JSON-RPC 2.0 batch array) would make every `msg.get(...)` below raise
    # AttributeError and — without the serve()-level guard — kill the whole
    # stdio loop. The spec answer to a non-object request is -32600.
    if not isinstance(msg, dict):
        return _err(None, -32600, "Invalid Request: message must be a JSON object")
    method = msg.get("method")
    msg_id = msg.get("id")
    is_request = "id" in msg

    # params may be absent or — from a misbehaving client — a non-object.
    # Coerce to {} so the per-method `.get(...)` below can never raise.
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        client_ver = params.get("protocolVersion")
        # version negotiation (spec): echo a supported client version, else offer
        # our latest — the client decides whether to proceed.
        ver = client_ver if client_ver in _SUPPORTED_VERSIONS else _SUPPORTED_VERSIONS[0]
        return _ok(msg_id, {
            "protocolVersion": ver,
            "capabilities": {"tools": {}, "prompts": {}},
            "serverInfo": {"name": "recall", "title": "whatever-recall",
                           "version": _server_version()},
            "instructions": _instructions(session),
        })

    if method == "ping":
        return _ok(msg_id, {})

    if method == "tools/list":
        return _ok(msg_id, {"tools": [
            {k: t[k] for k in ("name", "title", "description", "inputSchema")}
            for t in _TOOLS
        ]})

    if method == "tools/call":
        name = params.get("name")
        tool = next((t for t in _TOOLS if t["name"] == name), None)
        if tool is None:
            return _err(msg_id, -32602, f"Unknown tool: {name}")
        if not _licensed():  # W2/D7: clear "sign in" answer, never a silent failure
            return _text_result(msg_id, _NOT_SIGNED_IN, is_error=True)
        args = params.get("arguments")
        if not isinstance(args, dict):  # a truthy non-object (5, true, "x", []) is NOT {}
            args = {}                    # `or {}` left it through -> `r not in 5` raised
        missing = [r for r in tool["inputSchema"].get("required", []) if r not in args]
        if missing:
            return _err(msg_id, -32602, f"Missing required argument(s): {', '.join(missing)}")
        try:
            return _served(session, name, msg_id, tool["handler"](session, args))
        except _NoIndex:
            return _served(session, name, msg_id, _NO_INDEX.format(repo=session.repo), is_error=True)
        except Exception as e:  # execution error -> isError result, never a crash
            traceback.print_exc(file=sys.stderr)
            return _served(session, name, msg_id, f"{type(e).__name__}: {e}", is_error=True)

    if method == "prompts/list":
        return _ok(msg_id, {"prompts": [
            {k: p[k] for k in ("name", "title", "description", "arguments")}
            for p in _PROMPTS
        ]})

    if method == "prompts/get":
        name = params.get("name")
        prompt = next((p for p in _PROMPTS if p["name"] == name), None)
        if prompt is None:
            return _err(msg_id, -32602, f"Unknown prompt: {name}")
        # The unlicensed answer must be a GetPromptResult ({description, messages}),
        # NOT a CallToolResult ({content, isError}) — bug-hunt MEDIUM, 2026-06-17.
        # _text_result emits the tools/call shape; a spec-compliant client parsing a
        # prompts/get response looks for result.messages, finds it absent, and renders
        # nothing — so the whole point of _NOT_SIGNED_IN ("a clear sign-in answer, never
        # a silent failure") was defeated for slash commands. Surface the same text
        # through the success shape below by treating "signed out" as the message body.
        signed_out = not _licensed()  # W2/D7
        args = params.get("arguments")
        if not isinstance(args, dict):  # same non-object guard as tools/call above
            args = {}
        if signed_out:
            text = _NOT_SIGNED_IN
        else:
            missing = [a["name"] for a in prompt["arguments"]
                       if a.get("required") and a["name"] not in args]
            if missing:
                return _err(msg_id, -32602, f"Missing required argument(s): {', '.join(missing)}")
            try:
                text = prompt["handler"](session, args)
            except _NoIndex:
                text = _NO_INDEX.format(repo=session.repo)
            except Exception as e:  # surface as content — a slash command must never crash
                traceback.print_exc(file=sys.stderr)
                text = f"{type(e).__name__}: {e}"
        # workstream E: record the emitted prompt size too (tagged 'mcp-prompt'), best-effort
        idx = session.index()
        if idx is not None:
            try:
                idx._record_served(name, "mcp-prompt", len(text or ""))
            except Exception:
                pass
        return _ok(msg_id, {
            "description": prompt["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        })

    if not is_request:
        return None  # notifications/initialized, notifications/cancelled, ... — no reply
    return _err(msg_id, -32601, f"Method not found: {method}")


def serve(repo: str | Path | None = None) -> int:
    """Run the stdio server until the client closes stdin (the MCP shutdown)."""
    # Windows defaults text streams to the locale codepage — the spec demands UTF-8.
    # newline="\n" keeps the framing exact (no \r\n).
    # errors="replace": a single invalid UTF-8 byte on the wire (corrupted/binary pipe)
    # must NOT raise UnicodeDecodeError — that decode happens in `for line in sys.stdin`
    # BEFORE the per-line try, so a strict codec would kill the whole stdio loop and drop
    # the MCP connection in Claude/Cursor. Replace the bad byte; the line then JSON-fails
    # cleanly into a -32700 Parse error. (P1 bug-hunt 2026-06-15.)
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    session = _Session(repo)
    print(f"recall mcp · serving {session.repo} (stdio)", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            out: dict | None = _err(None, -32700, f"Parse error: {e}")
        else:
            # Defense in depth: NO handler exception may ever break the stdio
            # loop — that would silently kill the whole MCP connection in
            # Claude/Cursor. Any uncaught error becomes a JSON-RPC -32603.
            try:
                out = handle(msg, session)
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                msg_id = msg.get("id") if isinstance(msg, dict) else None
                out = _err(msg_id, -32603, f"Internal error: {type(e).__name__}: {e}")
        if out is not None:
            # ensure_ascii=False -> real UTF-8; dumps without indent emits no newlines
            print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0

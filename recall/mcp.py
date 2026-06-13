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
    "recall is this project's persistent memory: decisions, lessons, code map, "
    "blast radius — written at commit time, queried offline with zero tokens. "
    "Call `brief` with a file path BEFORE editing that file (why it is the way it "
    "is, what breaks, open tasks). Call `recall` to ask where/why/what-breaks "
    "questions. After a significant decision or lesson, `stamp` it so the next "
    "session knows. `dashboard` opens the browsable wiki. If a tool says there is "
    "no index, run `recall init .` in the project once. The same actions exist as "
    "user slash commands (the server's prompts: /recall /brief /explain /dashboard)."
)


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

    return _format_brief_for_prompt(idx.brief(a["file"]))


def _tool_explain(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    from recall.cli import _format_explain_for_prompt

    return _format_explain_for_prompt(idx.onboarding(), s.repo.name)


def _tool_stamp(s: _Session, a: dict) -> str:
    idx = s.index()
    if idx is None:
        raise _NoIndex()
    r = idx.stamp(
        title=a["title"],
        body=a.get("body"),
        anchors=a.get("anchors"),
        tags=a.get("tags"),
        file_path=a.get("file"),
        origin="live",
    )
    if r["action"] == "MERGE":
        return f"merged into existing note: {r['into']} (anchor overlap {r['overlap']})"
    return f"stamped #{r['node_id']} ({r['anchors']} anchors) — the next session will know."


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
    """Is a dashboard already answering at `url`? (separate fn so tests inject it)"""
    from urllib.request import urlopen

    try:
        with urlopen(url + "/api/pulse", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


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
        "name": "stamp",
        "title": "Write knowledge into the project memory",
        "description": (
            "Record a decision, lesson or gotcha so every future session knows it. "
            "Use after fixing a tricky bug or making a deliberate choice. Anchors "
            "are the search terms it should be found by."
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
        "name": "dashboard",
        "title": "Open the recall dashboard",
        "description": "Start (or find) the local dashboard and get its URL.",
        "arguments": [],
        "handler": _tool_dashboard,
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
            "instructions": _INSTRUCTIONS,
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
        args = params.get("arguments") or {}
        missing = [r for r in tool["inputSchema"].get("required", []) if r not in args]
        if missing:
            return _err(msg_id, -32602, f"Missing required argument(s): {', '.join(missing)}")
        try:
            return _text_result(msg_id, tool["handler"](session, args))
        except _NoIndex:
            return _text_result(msg_id, _NO_INDEX.format(repo=session.repo), is_error=True)
        except Exception as e:  # execution error -> isError result, never a crash
            traceback.print_exc(file=sys.stderr)
            return _text_result(msg_id, f"{type(e).__name__}: {e}", is_error=True)

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
        args = params.get("arguments") or {}
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
    sys.stdin.reconfigure(encoding="utf-8")
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

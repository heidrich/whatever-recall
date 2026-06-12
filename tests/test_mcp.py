"""MCP server (ADR-029) — protocol drift-guards.

Handler-level tests drive recall.mcp.handle() directly (pure function over
message + session); one subprocess test proves the real stdio framing
end-to-end. All offline, model-free."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from recall import Index
from recall.mcp import _SUPPORTED_VERSIONS, _Session, handle


# ------------------------------------------------------------------ fixtures
def _repo_with_index(tmp_path: Path) -> Path:
    """A minimal 'project': .mind/index.db with one stamped lesson."""
    (tmp_path / ".mind").mkdir()
    idx = Index.open(tmp_path / ".mind" / "index.db", repo=tmp_path)
    idx.stamp(title="RLS writers must set workspace_id",
              body="insert path forgot the scope column",
              anchors=["mcptest", "mcptesttwo"], kind="lesson", dedup=False)
    idx.db.close()
    return tmp_path


def _req(method: str, msg_id: int | None = 1, **params) -> dict:
    m: dict = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        m["id"] = msg_id
    if params:
        m["params"] = params
    return m


# ---------------------------------------------------------------- lifecycle
def test_initialize_echoes_a_supported_client_version(tmp_path):
    s = _Session(_repo_with_index(tmp_path))
    out = handle(_req("initialize", protocolVersion="2025-03-26"), s)
    assert out["result"]["protocolVersion"] == "2025-03-26"
    assert "tools" in out["result"]["capabilities"]
    assert out["result"]["serverInfo"]["name"] == "recall"
    assert out["result"]["instructions"]  # the LLM onboarding text rides along


def test_initialize_offers_latest_on_unknown_version(tmp_path):
    s = _Session(_repo_with_index(tmp_path))
    out = handle(_req("initialize", protocolVersion="1.0.0"), s)
    assert out["result"]["protocolVersion"] == _SUPPORTED_VERSIONS[0]


def test_initialized_notification_gets_no_reply(tmp_path):
    s = _Session(_repo_with_index(tmp_path))
    assert handle(_req("notifications/initialized", msg_id=None), s) is None


def test_ping_returns_empty_result(tmp_path):
    s = _Session(_repo_with_index(tmp_path))
    assert handle(_req("ping"), s)["result"] == {}


def test_unknown_method_is_32601(tmp_path):
    s = _Session(_repo_with_index(tmp_path))
    assert handle(_req("resources/list"), s)["error"]["code"] == -32601


# -------------------------------------------------------------------- tools
def test_tools_list_exposes_seven_tools_without_handlers(tmp_path):
    s = _Session(_repo_with_index(tmp_path))
    tools = handle(_req("tools/list"), s)["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "recall", "brief", "explain", "stamp", "contested", "freshen", "dashboard"]
    for t in tools:
        assert set(t) == {"name", "title", "description", "inputSchema"}  # no handler leak
        assert t["inputSchema"]["type"] == "object"


def test_call_recall_finds_stamped_knowledge(tmp_path):
    s = _Session(_repo_with_index(tmp_path))
    out = handle(_req("tools/call", name="recall",
                      arguments={"query": "mcptest mcptesttwo"}), s)
    r = out["result"]
    assert r["isError"] is False
    assert "workspace_id" in r["content"][0]["text"]


def test_call_stamp_writes_a_node(tmp_path):
    repo = _repo_with_index(tmp_path)
    s = _Session(repo)
    out = handle(_req("tools/call", name="stamp", arguments={
        "title": "gotcha: the cache must be invalidated on writes",
        "anchors": ["cachegotcha", "cachegotchatwo"]}), s)
    assert out["result"]["isError"] is False
    assert "stamped #" in out["result"]["content"][0]["text"]
    n = s.index().db.execute(
        "SELECT COUNT(*) FROM nodes WHERE title LIKE 'gotcha%'").fetchone()[0]
    assert n == 1


def test_call_unknown_tool_is_protocol_error(tmp_path):
    s = _Session(_repo_with_index(tmp_path))
    out = handle(_req("tools/call", name="rm_rf", arguments={}), s)
    assert out["error"]["code"] == -32602


def test_call_missing_required_argument_is_protocol_error(tmp_path):
    s = _Session(_repo_with_index(tmp_path))
    out = handle(_req("tools/call", name="brief", arguments={}), s)
    assert out["error"]["code"] == -32602
    assert "file" in out["error"]["message"]


def test_no_index_is_an_iserror_result_and_plants_no_db(tmp_path):
    """The bench_v2 lesson: never CREATE an index as a side effect of reading."""
    (tmp_path / ".git").mkdir()  # a repo, but never `recall init`ed
    s = _Session(tmp_path)
    out = handle(_req("tools/call", name="recall", arguments={"query": "anything"}), s)
    assert out["result"]["isError"] is True
    assert "recall init" in out["result"]["content"][0]["text"]
    assert not (tmp_path / ".mind" / "index.db").exists()


# ------------------------------------------------------------------- stdio e2e
def test_stdio_roundtrip_subprocess(tmp_path):
    """The real thing: spawn `recall mcp`, speak newline-delimited JSON-RPC over
    the pipes, get framed responses back — one line per message, nothing else."""
    repo = _repo_with_index(tmp_path)
    msgs = [
        _req("initialize", protocolVersion=_SUPPORTED_VERSIONS[0],
             capabilities={}, clientInfo={"name": "pytest", "version": "0"}),
        _req("notifications/initialized", msg_id=None),
        _req("tools/list", msg_id=2),
        _req("tools/call", msg_id=3, name="recall",
             arguments={"query": "mcptest mcptesttwo"}),
        "this is not json",  # parse error must answer -32700, not kill the loop
        _req("ping", msg_id=4),
    ]
    stdin = "\n".join(m if isinstance(m, str) else json.dumps(m) for m in msgs) + "\n"
    proc = subprocess.run(
        [sys.executable, "-m", "recall.cli", "mcp"],
        input=stdin, capture_output=True, text=True, encoding="utf-8",
        cwd=repo, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    out = [json.loads(ln) for ln in lines]   # every stdout line IS a JSON message
    by_id = {m.get("id"): m for m in out}
    assert by_id[1]["result"]["serverInfo"]["name"] == "recall"
    assert len(by_id[2]["result"]["tools"]) == 7
    assert "workspace_id" in by_id[3]["result"]["content"][0]["text"]
    assert by_id[None]["error"]["code"] == -32700
    assert by_id[4]["result"] == {}
    assert len(out) == 5  # exactly one reply per request, none for the notification


def test_mcp_status_last_used_comes_from_the_access_log(tmp_path):
    """The pill's 'last used' is honest USE evidence: a recall with consumer='mcp'
    (what every MCP tool call logs) sets it; before that it is None."""
    from recall.mcp import mcp_status
    repo = _repo_with_index(tmp_path)
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    assert mcp_status(repo, db=idx.db)["last_used"] is None
    idx.recall("mcptest mcptesttwo", consumer="mcp")
    assert mcp_status(repo, db=idx.db)["last_used"] is not None
    idx.db.close()


# ------------------------------------------------------------------- prompts
def test_initialize_declares_prompts_capability(tmp_path):
    s = _Session(_repo_with_index(tmp_path))
    out = handle(_req("initialize", protocolVersion=_SUPPORTED_VERSIONS[0]), s)
    assert "prompts" in out["result"]["capabilities"]


def test_prompts_list_exposes_four_slash_commands(tmp_path):
    s = _Session(_repo_with_index(tmp_path))
    prompts = handle(_req("prompts/list"), s)["result"]["prompts"]
    assert [p["name"] for p in prompts] == ["recall", "brief", "explain", "dashboard"]
    for p in prompts:
        assert set(p) == {"name", "title", "description", "arguments"}  # no handler leak
    args = {p["name"]: p["arguments"] for p in prompts}
    assert args["recall"][0] == {"name": "query",
                                 "description": "your question or search terms",
                                 "required": True}
    assert args["explain"] == []


def test_prompts_get_embeds_the_live_result(tmp_path):
    """A slash command lands the CONTENT in context — prompts/get executes
    server-side instead of returning a template."""
    s = _Session(_repo_with_index(tmp_path))
    out = handle(_req("prompts/get", name="recall",
                      arguments={"query": "mcptest mcptesttwo"}), s)
    msgs = out["result"]["messages"]
    assert msgs[0]["role"] == "user" and msgs[0]["content"]["type"] == "text"
    assert "workspace_id" in msgs[0]["content"]["text"]


def test_prompts_get_unknown_and_missing_arg_are_32602(tmp_path):
    s = _Session(_repo_with_index(tmp_path))
    assert handle(_req("prompts/get", name="nope"), s)["error"]["code"] == -32602
    out = handle(_req("prompts/get", name="brief", arguments={}), s)
    assert out["error"]["code"] == -32602 and "file" in out["error"]["message"]


# ------------------------------------------------------------- dashboard tool
def test_dashboard_tool_reuses_a_running_server(tmp_path):
    from recall.mcp import _tool_dashboard
    s = _Session(_repo_with_index(tmp_path))
    out = _tool_dashboard(s, {}, probe=lambda url: True)
    assert "already running" in out and "127.0.0.1:7099" in out


def test_dashboard_tool_spawns_detached_when_absent(tmp_path, monkeypatch):
    """No server answering -> spawn `recall dashboard` detached and say so.
    The Popen is intercepted — the test must not actually start a server."""
    import recall.mcp as mcp_mod
    calls = {}

    class _FakePopen:
        def __init__(self, cmd, **kw):
            calls["cmd"] = cmd
            calls["kw"] = kw
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    s = _Session(_repo_with_index(tmp_path))
    out = mcp_mod._tool_dashboard(s, {}, probe=lambda url: False)
    assert "Started the recall dashboard" in out
    assert calls["cmd"][-2:] == ["--port", "7099"]
    assert "dashboard" in calls["cmd"]
    assert calls["kw"]["cwd"] == str(s.repo)

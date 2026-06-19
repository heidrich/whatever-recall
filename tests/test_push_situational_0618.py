"""M2 / Workstream A (2026-06-18) — task-aware situational PUSH.

render_situational_block scopes the brief + landmines + live BROKEN trust-status to what the
agent is doing now (file / diff / task), pushed with 0 tool calls via UserPromptSubmit /
SessionStart / `recall push` / the MCP `push` prompt. The repo-static block stays the floor.
These tests pin: the leads-with-landmines + axis-3 verdict label, the graceful-degradation
invariant, the read-only/push-tagged contract, the inject-only handlers, the no-parser guard,
and the sentinel-guarded settings.json array merge (foreign entries preserved).
"""

import json
from types import SimpleNamespace

import pytest

from recall import predicate as P
from recall.engine import Index
from recall.cli import _index_path
from adapters import hook as H


# --------------------------------------------------------------------------- helpers
def _mem(tmp_path):
    return Index.open(":memory:", repo=tmp_path)


def _broken_node(idx, *, file_path="auth.py", title="login lowercases the email"):
    r = idx.stamp(title=title, anchors=["login"], file_path=file_path,
                  predicate=r"contains:lower", kind="lesson", body="login lowercases the address")
    idx.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                   (f"drift:{r['node_id']}", P.BROKEN))
    idx.db.commit()
    return r["node_id"]


# --------------------------------------------------- 1. file segment leads + axis-3 verdict label
def test_file_segment_leads_with_landmines_and_labels_verdict(tmp_path):
    idx = _mem(tmp_path)
    brief = {
        "warns": [{"title": "never log the raw password", "drift": None, "predicate": None}],
        "drift": "broken",
        "open_tasks": [{"title": "harden the login path"}],
        "why": [{"title": "login lowercases the email", "drift": "broken",
                 "predicate": "contains:lower"}],
    }
    lines = idx._situational_file_lines("auth.py", brief)
    body = "\n".join(lines)
    assert lines[0] == "**auth.py**"
    assert "🔴 landmine: never log the raw password" in lines[1]   # landmines LEAD
    assert "FAILS its re-check NOW" in body                         # live BROKEN status
    # axis-3: the predicate-backed why carries its verdict label
    assert "🔴 BROKEN (its re-check fails now)" in body


def test_verdict_tag_is_honest():
    assert Index._verdict_tag(None, None) == ""                    # no predicate → no claim
    assert "BROKEN" in Index._verdict_tag("broken", "contains:x")
    assert "holds" in Index._verdict_tag("fresh", "contains:x")
    assert "unverified" in Index._verdict_tag(None, "contains:x")  # never freshened ≠ confirmed


# ----------------------------------------------------- 2. end-to-end + graceful degradation
def test_situational_block_renders_broken_for_focus_file(tmp_path):
    idx = _mem(tmp_path)
    _broken_node(idx)
    block = idx.render_situational_block(focus_file="auth.py")
    assert block.startswith("## recall — situational memory")
    assert "🔴" in block and "auth.py" in block


def test_graceful_degradation_no_signal_is_the_static_block(tmp_path):
    idx = _mem(tmp_path)
    idx.stamp(title="x", anchors=["x"], kind="code-symbol", file_path="a.py", symbol="x", line=1)
    assert idx.render_situational_block() == idx.render_state_block()
    # a task with no above-floor hit also falls back to the static block
    assert idx.render_situational_block(task="zzz nonexistent qqq") == idx.render_state_block()


# ----------------------------------------------------------- 3. read-only + push-tagged contract
def test_push_is_read_only_and_push_tagged(tmp_path):
    idx = _mem(tmp_path)
    _broken_node(idx)
    idx.stamp(title="login design note", anchors=["login"], file_path="auth.py", kind="lesson",
              body="why login")
    def counts():
        return tuple(idx.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ("nodes", "edges", "meta"))
    before = counts()
    base_log = idx.db.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    idx.render_situational_block(focus_file="auth.py", task="login lowercases the email")
    assert counts() == before                                      # no node/edge/meta mutation
    new = idx.db.execute("SELECT consumer FROM access_log "
                         "ORDER BY rowid DESC LIMIT (SELECT COUNT(*)-? FROM access_log)",
                         (base_log,)).fetchall()
    assert new and all(r[0] == "push" for r in new)                # every new log row is push-tagged


# ------------------------------------------------------ 4. inject-only handlers + routing
def _repo_with_broken(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    idx = Index.open(_index_path(repo), repo=repo)
    idx.stamp(title="auth lowercases the email", anchors=["auth"], file_path="auth.py",
              kind="lesson", body="why auth", predicate=r"contains:lower")
    nid = idx.db.execute("SELECT id FROM nodes WHERE kind='lesson'").fetchone()[0]
    idx.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"drift:{nid}", P.BROKEN))
    idx.db.commit()
    idx.db.close()
    return repo


def test_user_prompt_injects_situational_never_denies(tmp_path):
    repo = _repo_with_broken(tmp_path)
    out = H.handle_user_prompt({"cwd": str(repo), "prompt": "please fix auth.py login"})
    assert "permissionDecision" not in json.dumps(out)             # a prompt is never denied
    block = out["hookSpecificOutput"]["additionalContext"]
    assert block.startswith("## recall — situational memory") and "auth.py" in block


def test_user_prompt_silent_when_no_match(tmp_path):
    repo = _repo_with_broken(tmp_path)
    # mentions no known file and no above-floor term → stay silent (static block is already
    # in the system prompt; we don't re-inject it)
    assert H.handle_user_prompt({"cwd": str(repo), "prompt": "hello there"}) == {}


def test_session_start_returns_the_static_block(tmp_path):
    repo = _repo_with_broken(tmp_path)
    out = H.handle_session_start({"cwd": str(repo)})
    block = out["hookSpecificOutput"]["additionalContext"]
    assert block.startswith("## recall — this project's memory")


def test_route_dispatches_new_events_and_keeps_old(tmp_path):
    repo = _repo_with_broken(tmp_path)
    up = H.route({"hook_event_name": "UserPromptSubmit", "cwd": str(repo), "prompt": "fix auth.py"})
    assert up["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    ss = H.route({"hook_event_name": "SessionStart", "cwd": str(repo)})
    assert ss["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    # the old routes still resolve (no exception, no situational hijack)
    assert isinstance(H.route({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                               "cwd": str(repo), "tool_input": {}}), dict)


# --------------------------------------------------------- 5. no-parser drift-guard
def test_push_path_imports_no_parser():
    """The path-mention extractor is a path-STRING regex — never an AST/tokenize/semantic parser."""
    import inspect
    src = inspect.getsource(H)
    assert "import ast" not in src and "import tokenize" not in src
    assert "_PATH_MENTION = re.compile" in src


# ------------------------------------------ 6. settings.json sentinel-guarded array merge
def _settings(repo):
    return json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_install_preserves_foreign_entries_and_is_idempotent(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".claude").mkdir(parents=True)
    foreign = {"hooks": {"PreToolUse": [
        {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "my-own-linter"}]}
    ]}, "model": "opus"}
    (repo / ".claude" / "settings.json").write_text(json.dumps(foreign), encoding="utf-8")

    r = H.install_client_hooks(repo, "claude")
    assert r["ok"]
    s = _settings(repo)
    assert s["model"] == "opus"                                    # foreign top-level key preserved
    pre = s["hooks"]["PreToolUse"]
    assert any(e.get("hooks", [{}])[0].get("command") == "my-own-linter" for e in pre)  # foreign entry kept
    assert any(H._entry_is_recall(e) for e in pre)                 # recall entry added alongside
    assert "UserPromptSubmit" in s["hooks"] and "SessionStart" in s["hooks"]

    H.install_client_hooks(repo, "claude")                         # idempotent — no duplicate
    s2 = _settings(repo)
    assert sum(1 for e in s2["hooks"]["PreToolUse"] if H._entry_is_recall(e)) == 1


def test_uninstall_removes_only_recall_entries(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Edit", "hooks": [{"type": "command", "command": "my-own-linter"}]}
    ]}}), encoding="utf-8")
    H.install_client_hooks(repo, "claude")
    H.uninstall_client_hooks(repo, "claude")
    s = _settings(repo)
    pre = s.get("hooks", {}).get("PreToolUse", [])
    assert any(e["hooks"][0]["command"] == "my-own-linter" for e in pre)   # foreign survives
    assert not any(H._entry_is_recall(e) for e in pre)                     # recall gone
    # the recall-only event arrays recall created are pruned
    assert "UserPromptSubmit" not in s.get("hooks", {})


def test_install_refuses_invalid_json_and_unmanaged_recallish(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text("{ not json", encoding="utf-8")
    assert not H.install_client_hooks(repo, "claude")["ok"]         # refuse, don't overwrite

    (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "python -m adapters.hook"}]}  # recallish, NO sentinel
    ]}}), encoding="utf-8")
    r = H.install_client_hooks(repo, "claude")
    assert not r["ok"] and "recall-like" in r["reason"]


def test_unsupported_client_is_flagged_not_faked(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    r = H.install_client_hooks(repo, "cursor")
    assert not r["ok"] and "not supported yet" in r["reason"]

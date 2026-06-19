"""The hard pre-edit gate (recall/editgate.py + adapters/hook.py) — drift guards.

Owner 2026-06-17: "wenn DU als Dev recall nutzt, dann MÜSSEN die Agents recall nutzen."
The PreToolUse hook DENIES an edit to a file recall has knowledge about until the agent
runs `recall ack <file>` — proving it saw the briefing, not just had it appended. These
guards lock that behavior so a future edit can't silently soften the gate back to a
skimmable suggestion.
"""
from __future__ import annotations

import time
from pathlib import Path

from recall import Index, editgate


def _repo_with_knowledge(tmp_path: Path) -> Path:
    """A tiny repo whose .mind carries a decision governing src/a.py."""
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    mind = tmp_path / ".mind"
    mind.mkdir()
    idx = Index.open(mind / "index.db", repo=tmp_path)
    idx.stamp(title="DECISION: do not change f()'s return shape — external callers parse it",
              body="a downstream consumer depends on the int return", kind="decision",
              anchors=["f", "return", "a.py"], file_path="src/a.py", dedup=False)
    idx.db.close()
    return tmp_path


# ---- editgate store -------------------------------------------------------
def test_ack_unlocks_within_ttl_then_expires(tmp_path):
    mind, repo = tmp_path / ".mind", tmp_path
    assert editgate.is_acked(mind, repo, "src/a.py") is False  # nothing acked yet
    editgate.ack(mind, repo, "src/a.py")
    assert editgate.is_acked(mind, repo, "src/a.py") is True
    # past the TTL it lapses (simulate via now=)
    later = time.time() + editgate.ACK_TTL_S + 1
    assert editgate.is_acked(mind, repo, "src/a.py", now=later) is False


def test_ack_is_per_file(tmp_path):
    mind, repo = tmp_path / ".mind", tmp_path
    editgate.ack(mind, repo, "src/a.py")
    assert editgate.is_acked(mind, repo, "src/a.py") is True
    assert editgate.is_acked(mind, repo, "src/b.py") is False  # a different file is still gated


def test_clear_re_gates_a_file(tmp_path):
    mind, repo = tmp_path / ".mind", tmp_path
    editgate.ack(mind, repo, "src/a.py")
    editgate.clear(mind, repo, "src/a.py")
    assert editgate.is_acked(mind, repo, "src/a.py") is False  # new knowledge → must re-brief


# ---- the hook deny → ack → allow cycle ------------------------------------
def _edit_event(repo: Path, rel: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": str(repo),
            "tool_input": {"file_path": rel, "old_string": "return 1", "new_string": "return 2"}}


def test_hook_denies_an_unacked_edit_to_a_known_file(tmp_path):
    repo = _repo_with_knowledge(tmp_path)
    from adapters.hook import route
    out = route(_edit_event(repo, "src/a.py"))
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") == "deny", "edit to a file with knowledge must be DENIED before ack"
    reason = hso.get("permissionDecisionReason", "")
    assert "recall ack" in reason, "the deny reason must tell the agent how to proceed"
    assert "DECISION" in reason or "external callers" in reason, "the deny must carry the briefing"


def test_hook_allows_after_ack(tmp_path):
    repo = _repo_with_knowledge(tmp_path)
    from adapters.hook import route
    editgate.ack(repo / ".mind", repo, "src/a.py")
    out = route(_edit_event(repo, "src/a.py"))
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") != "deny", "an acked edit must NOT be denied"
    # acked → falls back to context injection (the old, non-blocking behavior)
    assert "additionalContext" in hso, "an acked edit should still get the briefing as context"


def test_hook_stays_silent_on_an_unknown_file(tmp_path):
    """A file recall knows nothing about is not gated — the gate fires only where there's
    knowledge to heed (so it never becomes Clippy on every untracked file)."""
    repo = _repo_with_knowledge(tmp_path)
    from adapters.hook import route
    out = route(_edit_event(repo, "src/unknown_new_file.py"))
    # no knowledge → either empty (silent) or at most a non-deny; never a block
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"

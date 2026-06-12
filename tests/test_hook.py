"""adapters/hook.py — PreToolUse injection + silence, PostCommit stamping."""

import json
import subprocess

import pytest

from recall import Index
from recall.cli import _index_path


def _seed_repo(tmp_path):
    """A repo with an index that already knows an RLS lesson."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / ".git").mkdir()  # mark as a repo so _find_repo stops here
    idx = Index.open(_index_path(repo), repo=repo)
    idx.stamp(
        title="RLS cutover writers set workspace_id",
        body="The insert path must set workspace_id or rows are invisible.",
        anchors=["rls_cutover", "workspace_id", "workspace-api", "insert", "scope-spalte", "tenancy"],
        tags=["security"],
        file_path="src/lib/workspace-api.ts",
        sha="a1b2c3d",
    )
    return repo


def test_pre_edit_injects_for_known_file(tmp_path):
    from adapters import hook

    repo = _seed_repo(tmp_path)
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "cwd": str(repo),
        "tool_input": {"file_path": "src/lib/workspace-api.ts",
                       "new_string": "rls cutover workspace_id insert"},
    }
    out = hook.route(event)
    assert out, "should inject context for a known file"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "workspace_id" in ctx.lower()
    assert "sha a1b2c3d" in ctx


def test_pre_edit_injects_briefing_tasks_and_blast(tmp_path):
    """The hook now injects the pre-edit BRIEFING (ADR-018): open tasks (standing
    intent) and the blast radius, not just relevance-ranked lessons."""
    from adapters import hook

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / ".git").mkdir()
    idx = Index.open(_index_path(repo), repo=repo)
    # the file we'll edit, with a symbol so it's a known code file
    idx.stamp(title="run", anchors=["core", "run"], kind="code-symbol",
              file_path="src/core.ts", symbol="run", line=1, origin="bootstrap")
    # a dependent file → blast radius (something breaks if core.ts changes)
    idx.stamp(title="caller", anchors=["caller"], kind="code-symbol",
              file_path="src/caller.ts", symbol="caller", line=1, origin="bootstrap")
    # wire caller -> depends_on -> core (so core's blast radius includes caller)
    idx.add_dependency_edges([("src/caller.ts", "src/core.ts")])
    # an OPEN TASK wired to the file (standing intent)
    t = idx.stamp(title="Finish the core refactor", kind="task",
                  tags=["task", "open"], file_path=".recall/tasks/core.md",
                  origin="bootstrap", dedup=False)
    idx.link_task_to_files(t["node_id"], ["src/core.ts"])

    event = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": str(repo),
             "tool_input": {"file_path": "src/core.ts", "new_string": "core run"}}
    out = hook.route(event)
    assert out, "should brief on a known file"
    ctx = out["hookSpecificOutput"]["additionalContext"].lower()
    assert "open task" in ctx and "core refactor" in ctx
    assert "depend on this" in ctx or "break" in ctx
    assert "caller.ts" in ctx


def test_pre_edit_silent_for_unknown_file(tmp_path):
    from adapters import hook

    repo = _seed_repo(tmp_path)
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "cwd": str(repo),
        "tool_input": {"file_path": "README.md", "new_string": "hello world typo"},
    }
    assert hook.route(event) == {}  # Clippy killer


def test_pre_edit_silent_without_index(tmp_path):
    from adapters import hook

    repo = tmp_path / "noindex"
    repo.mkdir()
    (repo / ".git").mkdir()
    event = {"hook_event_name": "PreToolUse", "tool_name": "Edit",
             "cwd": str(repo), "tool_input": {"file_path": "x.ts"}}
    assert hook.route(event) == {}


def test_non_edit_event_ignored(tmp_path):
    from adapters import hook

    repo = _seed_repo(tmp_path)
    event = {"hook_event_name": "PreToolUse", "tool_name": "Read",
             "cwd": str(repo), "tool_input": {"file_path": "x.ts"}}
    assert hook.route(event) == {}


def test_post_commit_stamps_trailer(tmp_path):
    from adapters import hook

    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git unavailable")
    repo = tmp_path / "gitproj"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t.t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m",
         "fix: thing\n\nRecall-anchors: alpha, beta, gamma\nRecall-why: a stamped lesson"],
        check=True, capture_output=True,
    )
    # index must exist for the hook to stamp into
    Index.open(_index_path(repo), repo=repo)
    out = hook.route({"hook_event_name": "Stop", "cwd": str(repo)})
    assert out
    assert "recall" in out["hookSpecificOutput"]["additionalContext"].lower()
    # and the lesson is now recallable
    idx = Index.open(_index_path(repo), repo=repo)
    assert not idx.recall("alpha beta gamma")["silenced"]


def test_main_never_raises_on_garbage(monkeypatch, tmp_path):
    """A hook must never crash the host tool, even on malformed stdin."""
    from adapters import hook
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all {{{"))
    assert hook.main() == 0


# -------------------------------------------------- Wave D — pre-commit warning hook
def test_pre_commit_install_is_idempotent_and_marked(tmp_path):
    from adapters import hook

    repo = tmp_path / "proj"
    (repo / ".git" / "hooks").mkdir(parents=True)
    r1 = hook.install_pre_commit(repo)
    assert r1["ok"] and r1["installed"]
    hookfile = repo / ".git" / "hooks" / "pre-commit"
    body = hookfile.read_text(encoding="utf-8")
    assert hook._PRE_MARKER in body
    assert "precommit-check" in body
    assert "exit 0" in body  # must never block the commit
    # installing again is a clean no-op (no duplication)
    r2 = hook.install_pre_commit(repo)
    assert r2["ok"]
    assert hookfile.read_text(encoding="utf-8").count(hook._PRE_MARKER) == 1


def test_pre_commit_does_not_clobber_a_foreign_hook(tmp_path):
    from adapters import hook

    repo = tmp_path / "proj"
    (repo / ".git" / "hooks").mkdir(parents=True)
    foreign = repo / ".git" / "hooks" / "pre-commit"
    foreign.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    r = hook.install_pre_commit(repo)
    assert not r["ok"]
    assert "different pre-commit hook" in r["reason"]
    assert "echo mine" in foreign.read_text(encoding="utf-8")  # left intact


def test_pre_commit_uninstall_leaves_foreign_alone(tmp_path):
    from adapters import hook

    repo = tmp_path / "proj"
    (repo / ".git" / "hooks").mkdir(parents=True)
    foreign = repo / ".git" / "hooks" / "pre-commit"
    foreign.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    r = hook.uninstall_pre_commit(repo)
    assert not r["ok"]
    assert foreign.exists()  # not ours -> not removed
    # but ours uninstalls cleanly
    foreign.unlink()
    hook.install_pre_commit(repo)
    assert hook.uninstall_pre_commit(repo)["ok"]
    assert not foreign.exists()


def test_hook_status_reports_both(tmp_path):
    from adapters import hook

    repo = tmp_path / "proj"
    (repo / ".git" / "hooks").mkdir(parents=True)
    st = hook.hook_status(repo)
    assert st["has_git"] and not st["installed"] and not st["pre_commit"]
    hook.install_pre_commit(repo)
    assert hook.hook_status(repo)["pre_commit"]


def test_precommit_check_warns_but_exits_zero(tmp_path):
    """The risk warning prints, but the command ALWAYS returns 0 — never blocks git."""
    from recall.cli import cmd_precommit_check
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git unavailable")
    repo = tmp_path / "gitproj"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t.t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "core.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    # seed: core.py load-bearing (two dependents) so a staged edit is a risk file
    idx = Index.open(_index_path(repo), repo=repo)
    idx.stamp(title="run", anchors=["run"], kind="code-symbol", file_path="core.py",
              symbol="run", line=1, origin="bootstrap")
    idx.stamp(title="a", anchors=["a"], kind="code-symbol", file_path="a.py",
              symbol="a", line=1, origin="bootstrap")
    idx.stamp(title="b", anchors=["b"], kind="code-symbol", file_path="b.py",
              symbol="b", line=1, origin="bootstrap")
    idx.add_dependency_edges([("a.py", "core.py"), ("b.py", "core.py")])
    idx.rerank_importance()
    idx.db.close()
    # stage an edit to the load-bearing file
    (repo / "core.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "core.py"], check=True, capture_output=True)

    from types import SimpleNamespace
    assert cmd_precommit_check(SimpleNamespace(repo=str(repo))) == 0  # warned, never blocks

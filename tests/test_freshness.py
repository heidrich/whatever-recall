"""freshness.py — stage 2: the honest two-stage drift traffic-light.

🟢 fresh (file unchanged since stamp) · 🟡 committed-drift (new commits) ·
🟠 uncommitted-edit (open changes). These guard the whole point of stage 2:
the traffic light was wired but structurally blind — nothing ever set verified=0.
"""

import shutil
import subprocess

import pytest

from recall import Index
from recall.freshness import (
    COMMITTED, FRESH, UNCOMMITTED, RepoState, file_drift, freshen, drift_counts,
)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _repo_with_commit(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "auth.py").write_text("def login(u):\n    return u\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: login")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return repo, sha


@needs_git
def test_unchanged_file_is_fresh(tmp_path):
    repo, sha = _repo_with_commit(tmp_path)
    assert file_drift(repo, "auth.py", sha[:7]) == FRESH


@needs_git
def test_new_commit_marks_committed_drift(tmp_path):
    repo, sha = _repo_with_commit(tmp_path)
    # a later commit touches the same file -> 🟡 committed-drift against the old sha
    (repo / "auth.py").write_text("def login(u):\n    return u.lower()\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "fix: lowercase user")
    assert file_drift(repo, "auth.py", sha[:7]) == COMMITTED


@needs_git
def test_uncommitted_edit_is_loudest(tmp_path):
    repo, sha = _repo_with_commit(tmp_path)
    # open, unsaved-to-git change -> 🟠 even if no new commit exists
    (repo / "auth.py").write_text("def login(u):\n    return None  # WIP\n", encoding="utf-8")
    assert file_drift(repo, "auth.py", sha[:7]) == UNCOMMITTED


@needs_git
def test_deleted_pinned_file_is_committed_not_edited(tmp_path):
    """A file that's gone from disk (deleted in a migration, history rewrite) is
    committed-drift 🟡, NOT the live-edit signal 🟠. 🟠 is strictly for open
    working-tree changes — found dogfooding 360: 113 'edited' nodes were mostly
    long-deleted Vite-era files, drowning the real ~50 live edits."""
    repo, sha = _repo_with_commit(tmp_path)
    (repo / "auth.py").unlink()
    assert file_drift(repo, "auth.py", sha[:7]) == COMMITTED


@needs_git
def test_unknown_sha_never_false_alarms(tmp_path):
    repo, _ = _repo_with_commit(tmp_path)
    # a SHA git can't resolve must NOT raise and must NOT cry drift
    assert file_drift(repo, "auth.py", "deadbeef") == FRESH


def test_node_without_file_stays_fresh(tmp_path):
    # a lesson with no file_path has nothing to diff -> classified fresh, no crash
    assert file_drift(tmp_path, "", "abc1234") == FRESH


@needs_git
def test_freshen_flips_edges_and_node_drift(tmp_path):
    repo, sha = _repo_with_commit(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    # a lesson pinned to auth.py at the original sha, with an outgoing edge
    idx.stamp(title="login lowercases the user", anchors=["login", "auth", "lowercase", "user"],
              file_path="auth.py", sha=sha[:7], edges=[("touches", "auth.py")])
    # drift the file with a new commit
    (repo / "auth.py").write_text("def login(u):\n    return u.lower()\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "fix: lowercase")

    summary = freshen(idx, repo)
    assert summary["checked"] >= 1
    assert summary[COMMITTED] >= 1
    # the node now reports its drift, and the recall traffic light can read it
    res = idx.recall("login auth lowercase user")
    assert res["results"][0]["drift"] == COMMITTED
    # every outgoing edge flipped to verified=0
    assert all(e["verified"] is False for e in res["results"][0]["relation"])


@needs_git
def test_only_claim_bearing_kinds_drift(tmp_path):
    """Owner crux: a code-symbol (auto-regenerated map) and a commit (immutable history)
    must NOT drift just because their file got a later commit — only curated knowledge
    (lesson/decision/task/plan) can go stale. Otherwise the light is alarm-fatigue noise."""
    repo, sha = _repo_with_commit(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    # three nodes on the SAME file at the SAME old sha: a lesson, a code-symbol, a commit.
    # Distinct anchor sets so dedup keeps them separate; code-symbol uses origin='bootstrap'
    # (how the real code map is created — dedup=False there).
    idx.stamp(title="login lowercases the user", anchors=["lessonword", "auth", "lower", "user"],
              file_path="auth.py", sha=sha[:7], kind="lesson")
    idx.stamp(title="login", anchors=["symbolword", "loginsym", "defword"], file_path="auth.py",
              sha=sha[:7], kind="code-symbol", symbol="login", line=1, origin="bootstrap")
    idx.stamp(title="feat: login", anchors=["commitword", "featword", "shipword"],
              file_path="auth.py", sha=sha[:7], kind="commit")
    # drift the file with a later commit
    (repo / "auth.py").write_text("def login(u):\n    return u.lower()\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "fix: lowercase")

    freshen(idx, repo)
    drift = {}
    for nid, kind in idx.db.execute("SELECT id, kind FROM nodes WHERE file_path='auth.py'"):
        lvl = idx.db.execute("SELECT value FROM meta WHERE key=?", (f"drift:{nid}",)).fetchone()
        drift[kind] = lvl[0] if lvl else None
    assert drift["lesson"] == COMMITTED          # curated knowledge CAN go stale
    assert drift["code-symbol"] == FRESH         # auto-regenerated map → no 🟡 commit noise
    assert drift["commit"] == FRESH              # immutable historical fact → no 🟡 commit noise


@needs_git
def test_code_symbol_keeps_uncommitted_warning(tmp_path):
    """Regression guard (65fd0ed): suppressing 🟡 for code-symbols must NOT also blind the
    live 🟠 'uncommitted-edit' warning — brief()'s whole job is to warn before you edit a
    file, and most code files carry only code-symbol nodes (no lesson). The 🟠 must survive."""
    repo, sha = _repo_with_commit(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="login", anchors=["symbolword", "loginsym", "defword"], file_path="auth.py",
              sha=sha[:7], kind="code-symbol", symbol="login", line=1, origin="bootstrap")
    # open, UNSAVED working-tree change on the file (no new commit)
    (repo / "auth.py").write_text("def login(u):\n    return None  # WIP\n", encoding="utf-8")

    freshen(idx, repo)
    # the code-symbol node must report 🟠, and brief() must show it
    nid = idx.db.execute("SELECT id FROM nodes WHERE kind='code-symbol'").fetchone()[0]
    lvl = idx.db.execute("SELECT value FROM meta WHERE key=?", (f"drift:{nid}",)).fetchone()[0]
    assert lvl == UNCOMMITTED
    assert idx.brief("auth.py")["drift"] == UNCOMMITTED


@needs_git
def test_freshen_keeps_fresh_node_green(tmp_path):
    repo, sha = _repo_with_commit(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="login returns the user", anchors=["login", "auth", "returns", "user"],
              file_path="auth.py", sha=sha[:7])
    summary = freshen(idx, repo)
    assert summary[FRESH] >= 1
    assert idx.recall("login auth returns user")["results"][0]["drift"] == FRESH


@needs_git
def test_drift_counts_roundtrip_through_meta(tmp_path):
    repo, sha = _repo_with_commit(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="x", anchors=["login", "auth", "user", "token"], file_path="auth.py", sha=sha[:7])
    freshen(idx, repo)
    counts = drift_counts(idx)
    assert counts[FRESH] == 1
    # re-freshen must not leave stale drift keys behind (DELETE LIKE 'drift:%')
    freshen(idx, repo)
    assert drift_counts(idx)[FRESH] == 1


@needs_git
def test_index_freshen_method_uses_remembered_repo(tmp_path):
    """idx.freshen() with no arg must find the repo it was opened with."""
    repo, sha = _repo_with_commit(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="y", anchors=["login", "auth", "user", "scope"], file_path="auth.py", sha=sha[:7])
    summary = idx.freshen()  # no explicit repo
    assert summary["checked"] >= 1


@needs_git
def test_repostate_matches_single_file_path(tmp_path):
    """The bulk RepoState path must agree with file_drift() on every case — it's
    a speed optimization (3 global git reads vs 2 per file), never a behaviour
    change. Measured on a 1k-file production repo: 85 s of subprocess spawns collapsed to a few reads."""
    repo, sha = _repo_with_commit(tmp_path)
    # set up all three drift states at once
    (repo / "fresh.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "drifted.py").write_text("y = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add fresh + drifted")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    # drifted.py gets a NEW commit; edited.py gets an uncommitted edit
    (repo / "drifted.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "change drifted")
    (repo / "fresh.py").write_text("x = 99  # WIP\n", encoding="utf-8")  # now dirty

    state = RepoState(repo)
    cases = [("fresh.py", base), ("drifted.py", base), ("auth.py", sha[:7]),
             ("gone.py", base), ("", None)]
    for rel, pin in cases:
        assert state.drift_of(rel, pin) == file_drift(repo, rel, pin), f"mismatch on {rel}"


def test_on_disk_index_sets_busy_timeout(tmp_path):
    """A file-backed index must carry a busy_timeout so a bulk freshen() writer
    waits out a transient lock instead of raising 'database is locked' (found
    dogfooding a large production repo: a parallel writer crashed freshen on 7173 nodes)."""
    from recall.db import connect
    db = connect(tmp_path / "x.db")
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000

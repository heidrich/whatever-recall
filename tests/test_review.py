"""review() + commit_files() — Wave D, the pre-commit warning + `recall review <sha>` (ADR-021).

Where brief() answers ONE file before I touch it, review() answers a COMMIT (or a staged
set): for every file the change touches it bundles the briefing (what breaks, why, open
tasks, drift) and singles out the RISK files — load-bearing + many dependents + open task.
That same risk list is what the pre-commit hook warns on (never blocks) and what
`recall review <sha> --for-prompt` renders as a PR-markdown block. Read-only, model-free
(ADR-014): pure SQL + git reads, 0 tokens.
"""

import shutil
import subprocess

import pytest

from recall import Index
from recall.contested import commit_files

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _repo_with_change(tmp_path):
    """A repo whose core.py is load-bearing (two dependents + an open task), then a
    commit edits core.py + a leaf util.py. Returns (repo, idx, sha)."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "core.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "util.py").write_text("def helper():\n    return 0\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="run", anchors=["core", "run"], kind="code-symbol",
              file_path="core.py", symbol="run", line=1, origin="bootstrap")
    idx.stamp(title="a", anchors=["a"], kind="code-symbol",
              file_path="a.py", symbol="a", line=1, origin="bootstrap")
    idx.stamp(title="b", anchors=["b"], kind="code-symbol",
              file_path="b.py", symbol="b", line=1, origin="bootstrap")
    idx.stamp(title="helper", anchors=["helper"], kind="code-symbol",
              file_path="util.py", symbol="helper", line=1, origin="bootstrap")
    # a.py and b.py depend on core.py -> core.py is load-bearing
    idx.add_dependency_edges([("a.py", "core.py"), ("b.py", "core.py")])
    # an open task wired to core.py
    t = idx.stamp(title="Rework the run loop", kind="task", tags=["task", "open"],
                  file_path=".recall/tasks/run.md", origin="bootstrap", dedup=False)
    idx.link_task_to_files(t["node_id"], ["core.py"])
    idx.rerank_importance()

    # now a commit changes core.py (risky) and util.py (a leaf)
    (repo / "core.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    (repo / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "change core + util")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return repo, idx, sha


@needs_git
def test_commit_files_lists_the_changed_paths(tmp_path):
    repo, idx, sha = _repo_with_change(tmp_path)
    files = commit_files(repo, sha)
    assert set(files) == {"core.py", "util.py"}


@needs_git
def test_commit_files_empty_outside_git(tmp_path):
    (tmp_path / "lonely.py").write_text("x = 1\n", encoding="utf-8")
    assert commit_files(tmp_path, "HEAD") == []


@needs_git
def test_review_returns_a_briefing_per_changed_file(tmp_path):
    repo, idx, sha = _repo_with_change(tmp_path)
    r = idx.review(sha)
    for key in ("sha", "files", "risk_files", "counts"):
        assert key in r, f"missing key: {key}"
    reviewed = {f["file"] for f in r["files"]}
    assert reviewed == {"core.py", "util.py"}
    core = next(f for f in r["files"] if f["file"] == "core.py")
    # the briefing fields ride along
    for key in ("importance", "breaks", "why", "open_tasks", "drift"):
        assert key in core


@needs_git
def test_review_flags_the_load_bearing_file_as_risk(tmp_path):
    repo, idx, sha = _repo_with_change(tmp_path)
    r = idx.review(sha)
    risk = {f["file"] for f in r["risk_files"]}
    # core.py: load-bearing (2 dependents) + open task -> risk; util.py is a leaf -> not.
    assert "core.py" in risk
    assert "util.py" not in risk


@needs_git
def test_review_of_specific_files_skips_git(tmp_path):
    """The pre-commit path passes the staged files directly (no commit yet)."""
    repo, idx, sha = _repo_with_change(tmp_path)
    r = idx.review(files=["core.py"])
    assert [f["file"] for f in r["files"]] == ["core.py"]
    assert {f["file"] for f in r["risk_files"]} == {"core.py"}


@needs_git
def test_review_markdown_names_the_risk_files(tmp_path):
    from recall.cli import _format_review_markdown
    repo, idx, sha = _repo_with_change(tmp_path)
    r = idx.review(sha)
    md = _format_review_markdown(r)
    assert "core.py" in md
    assert md.lstrip().startswith("#")  # a markdown heading


def test_review_empty_repo_is_shaped_not_an_error():
    idx = Index.open(":memory:")
    r = idx.review(files=["nope.py"])
    assert r["files"] == [{"file": "nope.py", "importance": 0.0, "breaks": [],
                           "depends_on": [], "why": [], "open_tasks": [], "drift": None}]
    assert r["risk_files"] == []

"""stale_decisions() — Wave E, the stale-decision alarm (ADR-022).

A decision (ADR / foundation lesson) is stamped at a moment in the code's life. If the
code it references then changes a lot, the decision may no longer describe reality — "ADR-X
might be outdated". stale_decisions() finds those: for each decision, the referenced code
files, and how many commits touched them SINCE the decision's stamp SHA. Read-only,
model-free (ADR-014): the same RepoState git read freshen() uses, plus arithmetic.
"""

import shutil
import subprocess

import pytest

from recall import Index

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _repo_with_decision(tmp_path, *, churns: int):
    """A repo where an ADR references core.py, stamped at the FIRST commit, after which
    core.py is changed `churns` more times. Returns (repo, idx, decision_node_id)."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "core.py").write_text("v = 0\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()

    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="run", anchors=["core", "run"], kind="code-symbol",
              file_path="core.py", symbol="run", line=1, origin="bootstrap")
    dec = idx.stamp(title="ADR-001: core owns the run loop",
                    body="Every entry routes through core so the guard runs once.",
                    anchors=["adr", "core", "run"], tags=["foundation"],
                    kind="lesson", file_path="docs/decisions.md", sha=base, dedup=False)
    # wire the decision to core.py (it references that file)
    idx.link_task_to_files(dec["node_id"], ["core.py"])  # relates_to edge to core.py's node

    # now churn core.py `churns` times AFTER the decision's stamp
    for i in range(churns):
        (repo / "core.py").write_text(f"v = {i + 1}\n", encoding="utf-8")
        _git(repo, "commit", "-aqm", f"change core {i + 1}")
    return repo, idx, dec["node_id"]


@needs_git
def test_decision_whose_code_churned_is_stale(tmp_path):
    repo, idx, _ = _repo_with_decision(tmp_path, churns=3)
    stale = idx.stale_decisions()
    assert stale, "a decision whose code moved 3× since the stamp should be flagged"
    top = stale[0]
    assert "ADR-001" in top["title"]
    assert any(sf["file"] == "core.py" for sf in top["stale_files"])
    assert top["score"] >= 1


@needs_git
def test_decision_with_unchanged_code_is_not_stale(tmp_path):
    repo, idx, _ = _repo_with_decision(tmp_path, churns=0)
    stale = idx.stale_decisions()
    assert stale == [], "no commits after the stamp -> nothing stale"


@needs_git
def test_stale_shape(tmp_path):
    repo, idx, _ = _repo_with_decision(tmp_path, churns=2)
    stale = idx.stale_decisions()
    top = stale[0]
    for key in ("node_id", "title", "sha", "stale_files", "score"):
        assert key in top
    sf = top["stale_files"][0]
    for key in ("file", "commits_since"):
        assert key in sf


def test_stale_empty_index_no_error():
    idx = Index.open(":memory:")
    assert idx.stale_decisions() == []


@needs_git
def test_min_commits_threshold(tmp_path):
    """A single commit after the stamp is below the default threshold (not yet 'a lot')."""
    repo, idx, _ = _repo_with_decision(tmp_path, churns=1)
    assert idx.stale_decisions(min_commits=2) == []
    assert idx.stale_decisions(min_commits=1), "lowering the threshold surfaces it"

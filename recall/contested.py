"""Wave B — contested spots (uncertainty hotspots). The git churn reader.

`file_churn(repo)` returns, for each path, how many distinct commits touched it —
straight from git history, token-free, model-free (ADR-014). That count is the churn
half of a contested score; the entanglement half (co_changed degree) lives in the graph
and is read by Index.contested_spots(). Kept here (not in the engine) so the engine stays
git-free and this stays unit-testable against a throwaway repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> tuple[str, int]:
    """Run git, return (stdout, returncode). 127 = git absent / not a repo."""
    try:
        # core.quotepath=false: without it git C-quotes any path byte > 0x7F
        # ('"Gr\303\274\303\237e.py"'), so non-ASCII filenames never match the
        # raw UTF-8 repo-relative keys used everywhere else — the churn map
        # silently misses them. German project paths hit this routinely.
        p = subprocess.run(
            ["git", "-C", str(repo), "-c", "core.quotepath=false", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return p.stdout, p.returncode
    except OSError:
        return "", 127


def file_churn(repo: str | Path, *, max_commits: int = 4000) -> dict[str, int]:
    """path (repo-relative, forward slashes) -> number of distinct commits that touched it.

    Reads `git log --name-only` over the recent window. The churn signal: a file the team
    kept changing has a high count; a file written once has 1. Not a git repo (or git
    absent) -> {} (never raises). Each commit counts a file once even if listed twice."""
    repo = Path(repo)
    out, rc = _git(repo, "log", f"-{max_commits}", "--format=%x00", "--name-only")
    if rc != 0 or not out:
        return {}
    churn: dict[str, int] = {}
    # records are split by the %x00 we emit per commit; within a record, the lines after
    # the (empty) format line are the changed paths. We tally each path once per record.
    for record in out.split("\x00"):
        seen: set[str] = set()
        for line in record.splitlines():
            p = line.strip().replace("\\", "/")
            if not p or p in seen:
                continue
            seen.add(p)
            churn[p] = churn.get(p, 0) + 1
    return churn


def commit_files(repo: str | Path, sha: str = "HEAD") -> list[str]:
    """The repo-relative paths a single commit touched (forward slashes), in git order.

    Used by Index.review() (Wave D) to know what a commit changed before bundling each
    file's briefing. Not a git repo / unknown sha / git absent -> [] (never raises). Kept
    here next to file_churn so the engine stays git-free and this stays unit-testable."""
    repo = Path(repo)
    # --root makes diff-tree list the full file set for a PARENTLESS (initial)
    # commit, which otherwise produces no output (nothing to diff against). A new
    # user's very first commit IS the root commit, so `recall review` on it would
    # be empty without this. --root is a harmless no-op for normal commits.
    out, rc = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha)
    if rc != 0 or not out:
        return []
    files: list[str] = []
    seen: set[str] = set()
    for line in out.splitlines():
        p = line.strip().replace("\\", "/")
        if p and p not in seen:
            seen.add(p)
            files.append(p)
    return files

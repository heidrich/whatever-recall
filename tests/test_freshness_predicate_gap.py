"""CHARACTERIZATION — the gap north-star §2 names: drift ≠ truth.

Freshness today answers exactly ONE question: *did the file move since the stamp?*
It never asks *is the claim still TRUE?*. These two tests pin that down empirically,
so the predicate work has a hard before/after baseline (see test_predicate.py for the
after). They are written to PASS against today's engine — they document the CURRENT
behaviour, including the two ways it is blind:

  GAP A — wrong-from-start stays GREEN forever.
    A lesson whose "why" was false the moment it was stamped, on a file that never
    moves again, is reported 🟢 fresh. The drift light certifies a lie as truth.

  GAP B — still-true claim goes YELLOW on unrelated movement (false alarm).
    A lesson that is STILL 100% correct goes 🟡 committed-drift the instant ANY
    later commit touches its file — even a change that has nothing to do with the
    claim. Drift over-warns on truth.

Together: SHA-drift is ORTHOGONAL to whether the statement holds. That is the whole
case for storing a re-runnable predicate, not a bare conclusion.

When the predicate lands, GAP A's assertion flips (false claim -> BROKEN/red) and
GAP B's stays green; this file then becomes the regression proof that the fix works.
"""

import shutil
import subprocess

import pytest

from recall import Index
from recall.freshness import COMMITTED, FRESH, freshen


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _repo(tmp_path, body="def login(u):\n    return u\n"):
    """A one-file git repo. `body` is auth.py's content — the GROUND TRUTH the
    stamped claim will be measured against."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "auth.py").write_text(body, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: login")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return repo, sha


def _drift_of(idx, file_path, kind="lesson"):
    """Read the drift level freshen() stored for a node, via meta (no git re-run)."""
    nid = idx.db.execute(
        "SELECT id FROM nodes WHERE file_path=? AND kind=?", (file_path, kind)
    ).fetchone()[0]
    row = idx.db.execute("SELECT value FROM meta WHERE key=?", (f"drift:{nid}",)).fetchone()
    return row[0] if row else None


@needs_git
def test_gap_a_wrong_from_start_claim_stays_green(tmp_path):
    """A FALSE 'why', on a file that never moves, is certified 🟢 fresh forever.

    The code returns `u` UNCHANGED. We stamp the opposite — "login lowercases the
    user" — a claim that was wrong the instant it was written. The file is never
    touched again, so SHA-drift sees nothing, so freshness reports FRESH. The lie is
    green. This is the case drift CANNOT catch, and the core reason a predicate is
    needed: truth was never checked, only file movement.
    """
    repo, sha = _repo(tmp_path, body="def login(u):\n    return u\n")  # does NOT lowercase
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="login lowercases the user before lookup",
              anchors=["login", "lowercase", "auth", "user"],
              file_path="auth.py", sha=sha[:7], kind="lesson")

    freshen(idx, repo)

    # Today's hard fact: a claim that was false from the start is reported fresh.
    assert _drift_of(idx, "auth.py") == FRESH, (
        "characterization: SHA-drift can't see a wrong-from-start claim — it's green"
    )


@needs_git
def test_gap_b_still_true_claim_goes_yellow_on_unrelated_change(tmp_path):
    """A claim that is STILL TRUE goes 🟡 the moment its file gets ANY later commit.

    The claim "login returns the user unchanged" is true before and after we add an
    unrelated `logout` function. Nothing about `login` changed. But drift is file-
    level, so the unrelated commit flips the still-correct claim to committed-drift.
    Drift over-warns on truth exactly as it under-warns on lies (GAP A).
    """
    repo, sha = _repo(tmp_path, body="def login(u):\n    return u\n")
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="login returns the user unchanged",
              anchors=["login", "returns", "unchanged", "auth"],
              file_path="auth.py", sha=sha[:7], kind="lesson")

    # an UNRELATED change to the same file — the claim about login is untouched/true
    (repo / "auth.py").write_text(
        "def login(u):\n    return u\n\n\ndef logout(u):\n    return None\n", encoding="utf-8"
    )
    _git(repo, "commit", "-aqm", "feat: add logout (unrelated to the login claim)")

    freshen(idx, repo)

    # Today's hard fact: a still-true claim is flagged stale because the FILE moved.
    assert _drift_of(idx, "auth.py") == COMMITTED, (
        "characterization: SHA-drift fires on file movement even when the claim still holds"
    )

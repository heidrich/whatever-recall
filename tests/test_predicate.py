"""The predicate closes both gaps SHA-drift is blind to (the AFTER).

Pair this with tests/test_freshness_predicate_gap.py (the BEFORE): the SAME two
scenarios that drift gets WRONG, now resolved by a re-runnable predicate.

  GAP A  wrong-from-start claim   drift: 🟢 fresh (lie certified)  → predicate: 🔴 BROKEN
  GAP B  still-true, file moved   drift: 🟡 committed (false alarm)→ predicate: 🟢 CONFIRMED

Plus: a predicate tracks truth across a REAL change (CONFIRMED → BROKEN when the code
that backed the claim is removed), and degrades to UNKNOWN — never a false alarm — when
it can't check. Pure, deterministic, 0-token, stdlib-only.
"""

import shutil
import subprocess

import pytest

from recall.freshness import COMMITTED, FRESH, file_drift
from recall.predicate import (
    BROKEN, CONFIRMED, UNKNOWN, evaluate_predicate, merge_signal, parse_predicate,
)


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _repo(tmp_path, body):
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


# --------------------------------------------------------------------------- #
# GAP A — wrong-from-start: drift says fresh, the predicate says broken.
# --------------------------------------------------------------------------- #
@needs_git
def test_gap_a_predicate_catches_the_lie_drift_called_fresh(tmp_path):
    # code returns u UNCHANGED; the stamped claim says it lowercases — false from day 1
    repo, sha = _repo(tmp_path, body="def login(u):\n    return u\n")
    predicate = r"contains:u\.lower\(\)"  # "login lowercases the user" must show u.lower()

    # BEFORE: SHA-drift is blind — the file never moved, so it's fresh (the lie is green)
    assert file_drift(repo, "auth.py", sha[:7]) == FRESH

    # AFTER: the predicate re-checks the actual code and catches it
    assert evaluate_predicate(repo, "auth.py", predicate) == BROKEN

    # the merged light flips from a false green to a true red
    assert merge_signal(FRESH, evaluate_predicate(repo, "auth.py", predicate)) == BROKEN


# --------------------------------------------------------------------------- #
# GAP B — still-true claim, unrelated file move: drift cries yellow, predicate holds.
# --------------------------------------------------------------------------- #
@needs_git
def test_gap_b_predicate_keeps_a_still_true_claim_green(tmp_path):
    repo, sha = _repo(tmp_path, body="def login(u):\n    return u\n")
    # "login returns the user unchanged" — present `return u`, never lowercases
    predicate = r"contains:return u && absent:u\.lower\(\)"

    assert evaluate_predicate(repo, "auth.py", predicate) == CONFIRMED

    # an UNRELATED commit touches the file (adds logout) — login is untouched/true
    (repo / "auth.py").write_text(
        "def login(u):\n    return u\n\n\ndef logout(u):\n    return None\n", encoding="utf-8"
    )
    _git(repo, "commit", "-aqm", "feat: add logout")

    # BEFORE: drift fires on file movement even though the claim still holds
    assert file_drift(repo, "auth.py", sha[:7]) == COMMITTED
    # AFTER: the predicate still holds -> CONFIRMED, suppressing the false alarm
    assert evaluate_predicate(repo, "auth.py", predicate) == CONFIRMED
    assert merge_signal(COMMITTED, CONFIRMED) == "fresh"


# --------------------------------------------------------------------------- #
# A predicate tracks TRUTH across a real change: CONFIRMED -> BROKEN.
# --------------------------------------------------------------------------- #
@needs_git
def test_predicate_goes_broken_when_the_backing_code_is_removed(tmp_path):
    # this time the code DOES lowercase — the claim is true now
    repo, _ = _repo(tmp_path, body="def login(u):\n    return u.lower()\n")
    predicate = r"contains:u\.lower\(\)"
    assert evaluate_predicate(repo, "auth.py", predicate) == CONFIRMED

    # a real change removes the lowercasing — the claim is now false
    (repo / "auth.py").write_text("def login(u):\n    return u\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "refactor: drop lowercasing")
    assert evaluate_predicate(repo, "auth.py", predicate) == BROKEN


# --------------------------------------------------------------------------- #
# Safety: when it can't check, it's UNKNOWN — never a false CONFIRMED or BROKEN.
# --------------------------------------------------------------------------- #
@needs_git
def test_unknown_never_false_alarms(tmp_path):
    repo, _ = _repo(tmp_path, body="def login(u):\n    return u\n")
    assert evaluate_predicate(repo, "auth.py", None) == UNKNOWN          # no predicate
    assert evaluate_predicate(repo, "auth.py", "") == UNKNOWN            # empty
    assert evaluate_predicate(repo, "auth.py", "garbage no op") == UNKNOWN  # unparseable
    assert evaluate_predicate(repo, "auth.py", "contains:[") == UNKNOWN  # bad regex
    assert evaluate_predicate(repo, "missing.py", r"contains:x") == UNKNOWN  # no file
    assert evaluate_predicate(repo, None, r"contains:x") == UNKNOWN      # no pinned file
    # UNKNOWN defers entirely to drift — the light is unchanged
    assert merge_signal(COMMITTED, UNKNOWN) == COMMITTED
    assert merge_signal(FRESH, UNKNOWN) == FRESH


def test_parse_predicate_shapes():
    assert parse_predicate(None) is None
    assert parse_predicate("   ") is None
    assert parse_predicate("contains:[") is None        # bad regex -> None (UNKNOWN)
    assert parse_predicate("nonsense") is None
    assert len(parse_predicate(r"contains:a && absent:b")) == 2


# --------------------------------------------------------------------------- #
# ADVERSARIAL — break my own concept: a whole-file predicate false-confirms when
# the pattern lives in a DIFFERENT function. Symbol-scoping is the fix.
# --------------------------------------------------------------------------- #
@needs_git
def test_whole_file_predicate_false_confirms_wrong_symbol(tmp_path):
    """The honest weakness: `login` does NOT lowercase, `logout` DOES. A claim about
    login, checked against the WHOLE FILE, is falsely CONFIRMED because `u.lower()`
    exists somewhere. Scoping the check to login's line span catches the lie."""
    body = (
        "def login(u):\n"      # line 1
        "    return u\n"       # line 2  (login does NOT lowercase)
        "\n"                   # line 3
        "def logout(u):\n"     # line 4
        "    return u.lower()\n"  # line 5  (logout DOES — the decoy)
    )
    repo, _ = _repo(tmp_path, body=body)
    pred = r"contains:u\.lower\(\)"  # claim: "login lowercases the user"

    # whole-file: the decoy in logout makes it FALSELY confirm — the bug
    assert evaluate_predicate(repo, "auth.py", pred) == CONFIRMED
    # scoped to login (lines 1-2): correctly BROKEN — login really doesn't lowercase
    assert evaluate_predicate(repo, "auth.py", pred, line_range=(1, 2)) == BROKEN
    # scoped to logout (lines 4-5): genuinely CONFIRMED — the pattern is really there
    assert evaluate_predicate(repo, "auth.py", pred, line_range=(4, 5)) == CONFIRMED


@needs_git
def test_line_range_degrades_gracefully(tmp_path):
    """An out-of-bounds or inverted span must fall back to the whole file, never crash —
    a stamped line number can go stale as the file grows/shrinks."""
    repo, _ = _repo(tmp_path, body="def login(u):\n    return u.lower()\n")
    pred = r"contains:u\.lower\(\)"
    assert evaluate_predicate(repo, "auth.py", pred, line_range=(999, 1000)) == CONFIRMED  # past EOF
    assert evaluate_predicate(repo, "auth.py", pred, line_range=(5, 1)) == CONFIRMED       # inverted


@needs_git
def test_predicate_can_be_written_whitespace_tolerant(tmp_path):
    """Grammar expressiveness: a brittle pattern breaks on reformatting, but the AUTHOR
    can write a robust one. `u\\s*\\.\\s*lower` survives `black`/`prettier` respacing."""
    repo, _ = _repo(tmp_path, body="def login(u):\n    return u . lower()\n")  # odd spacing
    assert evaluate_predicate(repo, "auth.py", r"contains:u\.lower") == BROKEN          # brittle
    assert evaluate_predicate(repo, "auth.py", r"contains:u\s*\.\s*lower") == CONFIRMED  # robust

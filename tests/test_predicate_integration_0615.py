"""Arrow 1 INTEGRATION — the predicate spike wired into stamp -> freshen -> light.

test_predicate.py proves the pure engine (parse/evaluate/merge). This file proves the
WIRING the owner signed off on (2026-06-15):
  • Storage  — a `predicate` TEXT column on nodes (db.py v8).
  • Capture  — nudged: `stamp(predicate=...)`, optional, malformed rejected at write time;
               a stamp WITHOUT a predicate behaves exactly as before (drift only).
  • Surface  — freshen() folds the verdict into the drift level via merge_signal, adding
               the new 🔴 BROKEN; drift_counts() reports it.

The headline proofs are the two gaps from test_freshness_predicate_gap.py, now CLOSED
end-to-end through the real freshen() path (not the pure function):
  • GAP A — a wrong-from-start claim on an unmoved file flips 🟢 -> 🔴 (drift can't, the
            predicate can). This is the whole reason the predicate exists.
  • GAP B — a still-true claim survives an unrelated commit as 🟢 instead of a false 🟡.

These deliberately DUPLICATE the gap file's setup so the before/after sit side by side:
the gap file asserts the blindness (no predicate), this file asserts the cure (predicate).
"""

import shutil
import subprocess

import pytest

from recall.engine import Index
from recall.db import connect
from recall.freshness import BROKEN, COMMITTED, FRESH, drift_counts, freshen


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


def _drift_of(idx, file_path="auth.py", kind="lesson"):
    nid = idx.db.execute(
        "SELECT id FROM nodes WHERE file_path=? AND kind=?", (file_path, kind)
    ).fetchone()[0]
    row = idx.db.execute("SELECT value FROM meta WHERE key=?", (f"drift:{nid}",)).fetchone()
    return row[0] if row else None


# ----------------------------------------------------------------- STORAGE (db.py v8)

def test_schema_has_predicate_column():
    """A fresh index has the predicate column (v8) — the storage the owner chose."""
    db = connect(":memory:")
    cols = {r[1] for r in db.execute("PRAGMA table_info(nodes)").fetchall()}
    assert "predicate" in cols


def test_migration_adds_predicate_to_old_index(tmp_path):
    """An older on-disk index (no predicate column) gains it on open — idempotent,
    replay-safe, zero data loss (the project's migration discipline)."""
    p = tmp_path / "old.db"
    raw = __import__("sqlite3").connect(str(p))
    # a minimal pre-v8 nodes table WITHOUT predicate
    raw.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, kind TEXT, title TEXT, "
                "file_path TEXT, importance REAL)")
    raw.execute("INSERT INTO nodes(kind,title) VALUES('lesson','keep me')")
    raw.commit()
    raw.close()
    db = connect(str(p))  # connect() runs _migrate()
    cols = {r[1] for r in db.execute("PRAGMA table_info(nodes)").fetchall()}
    assert "predicate" in cols
    # the pre-existing row survived the ALTER
    assert db.execute("SELECT title FROM nodes WHERE title='keep me'").fetchone() is not None


# --------------------------------------------------------------- CAPTURE (nudged stamp)

def test_stamp_persists_predicate(tmp_path):
    idx = Index.open(":memory:", repo=tmp_path)
    r = idx.stamp(title="x", anchors=["x"], file_path="a.py",
                  predicate=r"contains:foo", kind="lesson")
    stored = idx.db.execute(
        "SELECT predicate FROM nodes WHERE id=?", (r["node_id"],)
    ).fetchone()[0]
    assert stored == r"contains:foo"


def test_stamp_without_predicate_stores_null(tmp_path):
    """The nudged contract: omitting --predicate is the normal path, not an error."""
    idx = Index.open(":memory:", repo=tmp_path)
    r = idx.stamp(title="x", anchors=["x"], file_path="a.py", kind="lesson")
    assert idx.db.execute(
        "SELECT predicate FROM nodes WHERE id=?", (r["node_id"],)
    ).fetchone()[0] is None


def test_stamp_rejects_malformed_predicate(tmp_path):
    """A typo fails the stamp LOUDLY instead of silently storing a dead check that
    would read as a green 'verified' — a wrong check is worse than no check."""
    idx = Index.open(":memory:", repo=tmp_path)
    with pytest.raises(ValueError):
        idx.stamp(title="x", anchors=["x"], file_path="a.py",
                  predicate="this has no clause", kind="lesson")
    with pytest.raises(ValueError):
        idx.stamp(title="y", anchors=["y"], file_path="a.py",
                  predicate=r"contains:(unclosed", kind="lesson")  # bad regex


def test_stamp_rejects_overlong_predicate(tmp_path):
    """Cheap ReDoS/footgun bound: a multi-KB regex is rejected at write time so it can
    never hang freshen() (which runs in the dashboard loop + git hook)."""
    idx = Index.open(":memory:", repo=tmp_path)
    with pytest.raises(ValueError):
        idx.stamp(title="x", anchors=["x"], file_path="a.py",
                  predicate="contains:" + "a" * 600, kind="lesson")


def test_stamp_merge_updates_predicate_but_never_clears_it(tmp_path):
    """Re-stamping the same claim WITH a predicate sets it (freshest wins); re-stamping
    WITHOUT one must not silently drop a working check the node already had."""
    idx = Index.open(":memory:", repo=tmp_path)
    a = ["login", "lowercase", "auth", "email", "trim"]
    r1 = idx.stamp(title="login lowercases the email", anchors=a, file_path="a.py",
                   predicate=r"contains:lower", kind="lesson")
    # a near-identical re-stamp (high anchor overlap) -> MERGE; new predicate replaces
    r2 = idx.stamp(title="login lowercases the email address", anchors=a, file_path="a.py",
                   predicate=r"contains:toLowerCase", kind="lesson")
    assert r2["action"] == "MERGE"
    nid = r1["node_id"]
    assert idx.db.execute("SELECT predicate FROM nodes WHERE id=?", (nid,)).fetchone()[0] \
        == r"contains:toLowerCase"
    # a third merge with NO predicate must keep the existing one
    idx.stamp(title="login lowercases the email value", anchors=a, file_path="a.py", kind="lesson")
    assert idx.db.execute("SELECT predicate FROM nodes WHERE id=?", (nid,)).fetchone()[0] \
        == r"contains:toLowerCase"


# ---------------------------------------------------------------- SURFACE (freshen)

@needs_git
def test_gap_a_closed_wrong_claim_with_predicate_goes_broken(tmp_path):
    """THE headline. Same setup as test_gap_a (a false claim on an unmoved file) — but
    WITH a predicate. SHA-drift still sees nothing (the file never moved), yet freshen()
    flips it 🔴 BROKEN because the check fails. The lie no longer reads green."""
    repo, sha = _repo(tmp_path, body="def login(u):\n    return u\n")  # does NOT lowercase
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="login lowercases the user before lookup",
              anchors=["login", "lowercase", "auth", "user"],
              file_path="auth.py", sha=sha[:7], kind="lesson",
              predicate=r"contains:\.lower\(\)")  # the claim, made checkable

    freshen(idx, repo)

    assert _drift_of(idx) == BROKEN, "GAP A must be CLOSED: a false claim is now red, not green"


@needs_git
def test_gap_b_closed_true_claim_with_predicate_survives_unrelated_commit(tmp_path):
    """A still-true claim, WITH a predicate, survives an unrelated commit as 🟢 instead
    of the false 🟡 of test_gap_b. The predicate proves the claim still holds, so the
    file move is correctly judged irrelevant (CONFIRMED overrides drift down to fresh)."""
    repo, sha = _repo(tmp_path, body="def login(u):\n    return u\n")
    idx = Index.open(":memory:", repo=repo)
    # seed TWO code-symbols so login's span has a boundary (login@1 .. just-before help@3) —
    # span scoping needs a NEXT symbol to bound the claim. An unscoped CONFIRMED is (by design,
    # post-review) downgraded to defer, so the GAP-B cure (legitimately quieting a 🟡 on a
    # still-true claim) only applies to a SCOPED check.
    idx.stamp(title="login", anchors=["login"], file_path="auth.py", symbol="login",
              line=1, kind="code-symbol")
    idx.stamp(title="help", anchors=["help"], file_path="auth.py", symbol="help",
              line=3, kind="code-symbol")
    idx.stamp(title="login returns the user unchanged",
              anchors=["login", "returns", "unchanged", "auth"],
              file_path="auth.py", sha=sha[:7], kind="lesson", line=1,
              predicate=r"contains:return u")  # still holds after the unrelated change

    (repo / "auth.py").write_text(
        "def login(u):\n    return u\n\n\ndef logout(u):\n    return None\n", encoding="utf-8"
    )
    _git(repo, "commit", "-aqm", "feat: add logout (unrelated)")

    freshen(idx, repo)

    assert _drift_of(idx) == FRESH, "GAP B must be CLOSED: a still-true SCOPED claim stays green"


def test_no_predicate_falls_back_to_drift_unchanged(tmp_path):
    """The nudged guarantee at the freshen layer: a node WITHOUT a predicate is
    classified exactly as before — UNKNOWN defers entirely to drift. (No git here, so
    a deleted file => committed; an existing file => fresh, the documented no-git path.)"""
    repo = tmp_path / "p"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="plain claim, no check", anchors=["plain"], file_path="a.py", kind="lesson")
    s = freshen(idx, repo)
    assert s["broken"] == 0
    assert _drift_of(idx, "a.py") == FRESH  # file exists, no predicate -> unchanged behaviour


def test_drift_counts_reports_broken(tmp_path):
    """drift_counts() (what stats/dashboard read) carries the broken tally."""
    repo = tmp_path / "p"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="f returns two", anchors=["f", "two"], file_path="a.py", kind="lesson",
              predicate=r"contains:return 2")  # WRONG — f returns 1
    freshen(idx, repo)
    c = drift_counts(idx)
    assert "broken" in c and c["broken"] == 1


def test_predicate_scoped_to_symbol_span_no_false_confirm(tmp_path):
    """SCOPING is what makes the predicate trustworthy: a `contains:` pattern living in
    an UNRELATED function must not false-confirm a claim pinned to a different symbol.

    Two functions; the claim is about `parse` (line of `def parse`), and its check looks
    for `.lower()`. Only `other()` calls .lower(). With span scoping (next code-symbol's
    line bounds parse), the check sees ONLY parse's body and correctly reports BROKEN."""
    repo = tmp_path / "p"
    repo.mkdir()
    (repo / "m.py").write_text(
        "def parse(x):\n"        # line 1  -- the claim is about THIS
        "    return x\n"          # line 2
        "\n"                       # line 3
        "def other(x):\n"        # line 4
        "    return x.lower()\n", # line 5  -- .lower() lives HERE, not in parse
        encoding="utf-8",
    )
    idx = Index.open(":memory:", repo=repo)
    # seed the code-symbol nodes so span scoping has boundaries (parse@1, other@4)
    idx.stamp(title="parse", anchors=["parse"], file_path="m.py", symbol="parse",
              line=1, kind="code-symbol")
    idx.stamp(title="other", anchors=["other"], file_path="m.py", symbol="other",
              line=4, kind="code-symbol")
    # the CLAIM, pinned to parse's line, asserting parse lowercases (it does NOT)
    idx.stamp(title="parse lowercases its input", anchors=["parse", "lowercase", "input"],
              file_path="m.py", line=1, kind="lesson",
              predicate=r"contains:\.lower\(\)")
    freshen(idx, repo)
    # scoped to parse (lines 1..3), .lower() is absent -> BROKEN. A whole-file check would
    # have found other()'s .lower() and falsely CONFIRMED.
    assert _drift_of(idx, "m.py") == BROKEN


@needs_git
def test_commit_trailer_carries_predicate(tmp_path):
    """Recall-predicate: trailer is the most common real capture point. A valid one is
    stored; a malformed one is DROPPED (the note still stamps) so one bad trailer can't
    abort the whole commit-replay walk."""
    repo, _ = _repo(tmp_path, body="def login(u):\n    return u.lower()\n")
    idx = Index.open(":memory:", repo=repo)
    msg_ok = ("feat: login\n\n"
              "Recall-anchors: login, auth\n"
              "Recall-why: login lowercases\n"
              r"Recall-predicate: contains:\.lower\(\)" + "\n")
    r = idx.stamp_from_commit(msg_ok, "deadbeef")
    assert r is not None
    stored = idx.db.execute(
        "SELECT predicate FROM nodes WHERE id=?", (r["node_id"],)
    ).fetchone()[0]
    assert stored == r"contains:\.lower\(\)"

    msg_bad = ("fix: thing\n\n"
               "Recall-anchors: thing\n"
               "Recall-predicate: not a valid clause\n")
    r2 = idx.stamp_from_commit(msg_bad, "cafebabe")
    assert r2 is not None  # note still stamped despite the bad predicate
    assert idx.db.execute(
        "SELECT predicate FROM nodes WHERE id=?", (r2["node_id"],)
    ).fetchone()[0] is None  # the malformed predicate was dropped, not stored


# ============================================================================
# ADVERSARIAL REVIEW DRIFT-GUARDS (2026-06-15) — lock the four findings closed.
# ============================================================================

def test_redos_shape_rejected_at_write_time(tmp_path):
    """P2 ReDoS: a catastrophic-backtracking predicate `(a+)+$` parses as a valid regex
    but would hang freshen() (in the dashboard loop + git hook). It must be rejected at
    stamp time — a Python-thread timeout can't save us (re.search holds the GIL)."""
    idx = Index.open(":memory:", repo=tmp_path)
    with pytest.raises(ValueError):
        idx.stamp(title="redos", anchors=["x"], file_path="a.py",
                  predicate=r"contains:(a+)+$", kind="lesson")


def test_redos_shape_is_unknown_not_a_hang_if_somehow_stored(tmp_path):
    """Defence in depth: even if a pathological shape reached evaluate_predicate, the
    parse-time shape screen makes it UNKNOWN (never runs the dangerous match), so the
    read path can never hang on it."""
    from recall.predicate import evaluate_predicate, UNKNOWN
    (tmp_path / "v.py").write_text("a" * 40 + "!\n", encoding="utf-8")
    # must return instantly (the shape is rejected before .search runs), not backtrack
    assert evaluate_predicate(tmp_path, "v.py", r"contains:(a+)+$") == UNKNOWN


def test_path_traversal_rejected_at_write_and_read(tmp_path):
    """P2 path traversal: a predicate must never read a file OUTSIDE the repo. Rejected
    at stamp time, and the read path also returns UNKNOWN (defence in depth)."""
    from recall.predicate import evaluate_predicate, UNKNOWN
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "secret.txt").write_text("TOPSECRET", encoding="utf-8")  # OUTSIDE the repo
    idx = Index.open(":memory:", repo=repo)
    with pytest.raises(ValueError):
        idx.stamp(title="exfil", anchors=["x"], file_path="../secret.txt",
                  predicate="contains:TOPSECRET", kind="lesson")
    # read-path guard: an escaping file_rel is UNKNOWN, never read
    assert evaluate_predicate(repo, "../secret.txt", "contains:TOPSECRET") == UNKNOWN
    assert evaluate_predicate(repo, str((tmp_path / "secret.txt").resolve()),
                              "contains:TOPSECRET") == UNKNOWN  # absolute path too


@needs_git
def test_confirmed_predicate_never_suppresses_uncommitted_edit(tmp_path):
    """P3 liveness: a CONFIRMED predicate quiets a 🟡 committed-drift false alarm, but must
    NEVER hide a 🟠 uncommitted working-tree edit — even when the predicate-bearing claim is
    the file's ONLY pinned node. 🟠 is a liveness signal; a predicate vouches for one clause,
    not the rest of the live diff."""
    repo, sha = _repo(tmp_path, body="def f():\n    return 1\n")
    idx = Index.open(":memory:", repo=repo)
    # scoped, still-true claim — the ONLY node on the file (no code-symbol seeded)
    idx.stamp(title="f returns one", anchors=["f", "one"], file_path="auth.py", line=1,
              sha=sha[:7], kind="lesson", predicate=r"contains:return 1")
    # a GENUINE uncommitted edit the predicate does NOT cover
    (repo / "auth.py").write_text(
        "def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8")
    freshen(idx, repo)
    # the predicate still holds, but the working tree is dirty RIGHT NOW -> 🟠 must survive
    assert idx._file_drift("auth.py") == "uncommitted", \
        "a confirmed predicate must not mask a live uncommitted edit"


def test_unscoped_predicate_does_not_false_confirm(tmp_path):
    """P3 scope-or-defer: an UNSCOPED whole-file check (no --line, every commit-trailer
    predicate) must not FALSE-CONFIRM a claim that is only true in an unrelated function.
    An unscoped CONFIRMED is downgraded to defer-to-drift; an unscoped BROKEN still works
    (the pattern is absent from the WHOLE file, so the claim is definitely false)."""
    # _repo writes `body` to auth.py: parse() does NOT lowercase, only other() does.
    repo, _ = _repo(tmp_path, body="def parse(x):\n    return x\n\n\ndef other(x):\n    return x.lower()\n")
    idx = Index.open(":memory:", repo=repo)
    # claim about parse, NO line -> unscoped. .lower() lives only in other(), so a whole-file
    # check would FALSE-CONFIRM. With scope-or-defer the unscoped CONFIRMED is downgraded to
    # defer — so the claim is NOT certified true from an unrelated function.
    idx.stamp(title="parse lowercases its input", anchors=["parse", "lowercase"],
              file_path="auth.py", kind="lesson",
              predicate=r"contains:\.lower\(\)")  # unscoped (no line)
    s = freshen(idx, repo)
    # the hard guarantee: not a false BROKEN, and (the real risk) not a CONFIRMED lie. The
    # unscoped CONFIRMED defers to drift (clean tree, stamped at HEAD -> fresh), never
    # asserting truth from other()'s .lower().
    assert s["broken"] == 0
    assert _drift_of(idx) == FRESH  # deferred to drift, not falsely confirmed-from-elsewhere


def test_headline_split_long_title_into_title_plus_body(tmp_path):
    """A long title with no body splits at the first sentence end → headline to title, the
    rest to body — so the story chain shows the decision once and the detail once, never the
    same paragraph twice. Owner 2026-06-15 ('Decision and Conclusion can't be the same')."""
    from recall.engine import _split_headline
    head, rest = _split_headline(
        "Switched auth to JWT. Killed three login bugs; refresh-token was the gotcha.")
    assert head == "Switched auth to JWT."
    assert rest == "Killed three login bugs; refresh-token was the gotcha."
    # a colon is NOT a boundary (it introduces content) — no stub headline
    h2, r2 = _split_headline("P1 bug fixed: the seat counter double-charged. Added a guard.")
    assert h2.startswith("P1 bug fixed:") and "double-charged" in h2  # colon stays in headline
    assert r2 == "Added a guard."
    # short title or single long sentence stays whole
    assert _split_headline("Short headline") == ("Short headline", "")
    assert _split_headline("a " * 120)[1] == ""  # no sentence end -> not split


def test_stamp_splits_long_title_but_not_explicit_body(tmp_path):
    """The split fires at stamp time only when the title is long (>120) AND there is NO body
    to clobber; an explicit (title, body) pair is left exactly as given."""
    idx = Index.open(":memory:", repo=tmp_path)
    # >120 chars so the stamp gate fires; first sentence end is the split point.
    long = ("Switched the auth layer over to JWT tokens this sprint. It later needed a "
            "refresh-token fallback for the long-lived-session edge case we missed.")
    assert len(long) > 120
    r = idx.stamp(title=long, anchors=["x"], file_path="a.py", kind="lesson")
    t, b = idx.db.execute("SELECT title, body FROM nodes WHERE id=?", (r["node_id"],)).fetchone()
    assert t == "Switched the auth layer over to JWT tokens this sprint." and "fallback" in b
    # an explicit (title, body) pair — even with a long title — is NOT split
    r2 = idx.stamp(title=long, body="explicit body", anchors=["y"], file_path="a.py", kind="lesson")
    t2, b2 = idx.db.execute("SELECT title, body FROM nodes WHERE id=?", (r2["node_id"],)).fetchone()
    assert b2 == "explicit body" and t2 == long  # not overwritten by the split


def test_outcome_is_stored_and_distinct_from_title(tmp_path):
    """v9: the OUTCOME (what came of the decision) is a SEPARATE recorded field, never the
    title re-worded. Owner 2026-06-15: the chain must make sense as DATA — Decision (title)
    != Outcome (what it taught us)."""
    idx = Index.open(":memory:", repo=tmp_path)
    r = idx.stamp(title="switch to JWT auth", anchors=["jwt", "auth"], file_path="a.py",
                  outcome="killed 3 login bugs; token-refresh was the gotcha", kind="lesson")
    title, outcome = idx.db.execute(
        "SELECT title, outcome FROM nodes WHERE id=?", (r["node_id"],)).fetchone()
    assert outcome == "killed 3 login bugs; token-refresh was the gotcha"
    assert outcome != title  # the whole point: it is NOT a duplicate of the decision


def test_outcome_none_when_not_given(tmp_path):
    """Nudged: no outcome is the honest default — an empty string normalises to None, not ''."""
    idx = Index.open(":memory:", repo=tmp_path)
    r = idx.stamp(title="x", anchors=["x"], file_path="a.py", kind="lesson")
    assert idx.db.execute("SELECT outcome FROM nodes WHERE id=?", (r["node_id"],)).fetchone()[0] is None
    r2 = idx.stamp(title="y", anchors=["y"], file_path="a.py", kind="lesson", outcome="   ")
    assert idx.db.execute("SELECT outcome FROM nodes WHERE id=?", (r2["node_id"],)).fetchone()[0] is None


def test_outcome_merge_updates_but_never_clears(tmp_path):
    """A re-stamp WITH an outcome sets it (the freshest record of how it went wins); a
    re-stamp WITHOUT one never drops an outcome the note already had."""
    idx = Index.open(":memory:", repo=tmp_path)
    a = ["jwt", "auth", "session", "token", "login"]
    r1 = idx.stamp(title="switch to JWT", anchors=a, file_path="a.py", kind="lesson",
                   outcome="shipped clean")
    idx.stamp(title="switch to JWT auth", anchors=a, file_path="a.py", kind="lesson",
              outcome="later: refresh-token bug found")  # MERGE -> replaces
    nid = r1["node_id"]
    assert idx.db.execute("SELECT outcome FROM nodes WHERE id=?", (nid,)).fetchone()[0] \
        == "later: refresh-token bug found"
    idx.stamp(title="switch to JWT auth method", anchors=a, file_path="a.py", kind="lesson")  # no outcome
    assert idx.db.execute("SELECT outcome FROM nodes WHERE id=?", (nid,)).fetchone()[0] \
        == "later: refresh-token bug found"  # NOT cleared


def test_outcome_commit_trailer(tmp_path):
    """Recall-outcome: trailer carries the chain end through the commit-replay capture path."""
    idx = Index.open(":memory:", repo=tmp_path)
    msg = ("feat: thing\n\nRecall-anchors: pkg/x.py\nRecall-outcome: shipped, no regressions\n")
    r = idx.stamp_from_commit(msg, "abc1234")
    assert r is not None
    assert idx.db.execute("SELECT outcome FROM nodes WHERE id=?", (r["node_id"],)).fetchone()[0] \
        == "shipped, no regressions"


def test_brief_rich_carries_outcome(tmp_path):
    """The dashboard story chain reads the outcome FIELD (it never interprets one from body)."""
    idx = Index.open(":memory:", repo=tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    idx.stamp(title="decided X", anchors=["x"], file_path="a.py", kind="lesson",
              outcome="X turned out to need a fallback")
    b = idx.brief("a.py", rich=True)
    assert b["why"][0]["outcome"] == "X turned out to need a fallback"
    # lean brief still carries the per-note outcome (it's a node field, cheap), but not impact
    lean = idx.brief("a.py", rich=False)
    assert lean["why"][0]["outcome"] == "X turned out to need a fallback"
    assert "impact" not in lean


def test_unscoped_broken_still_works(tmp_path):
    """The other half of scope-or-defer: an unscoped check whose pattern is absent from the
    ENTIRE file is safely BROKEN (GAP A still closed on the common nudged path)."""
    repo, sha = _repo(tmp_path, body="def f():\n    return 1\n")
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="f lowercases", anchors=["f", "lower"], file_path="auth.py",
              sha=sha[:7], kind="lesson", predicate=r"contains:\.lower\(\)")  # NO line, absent everywhere
    freshen(idx, repo)
    assert _drift_of(idx) == BROKEN, "unscoped BROKEN (pattern absent from whole file) must still fire"

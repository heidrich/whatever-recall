"""contested_spots() — Wave B, the "umstrittene Stellen" / uncertainty hotspots (ADR-019).

A contested spot is code the team kept changing — high churn AND high entanglement
(it drags other files along when it moves). It answers a different question than
importance (PageRank): a load-bearing file written once and never touched is important
but NOT contested; a file rewritten ten times is where the team burns time.

Signal, all read-only & model-free (ADR-014):
  churn        — how many distinct commits touched the file (from git history)
  entanglement — its co_changed degree (how many other files move with it)
  score        — churn weighted up by entanglement; a 1-commit file is never contested.
"""

from recall import Index


def _wired(idx):
    """Three files with code-symbols, co_changed-wired so degrees differ:
       shell  ←co→ guards, shell ←co→ api   (shell entangled with two)
       guards ←co→ api                       (each other has one more)"""
    for f, sym in [("src/shell.tsx", "Shell"), ("src/guards.ts", "guard"),
                   ("src/api.ts", "api"), ("src/quiet.ts", "quiet")]:
        idx.stamp(title=sym, anchors=[sym.lower(), f.split("/")[-1]], kind="code-symbol",
                  file_path=f, symbol=sym, line=1, origin="bootstrap")
    idx.record_co_change(["src/shell.tsx", "src/guards.ts", "src/api.ts"])
    return idx


def test_contested_spots_returns_scored_files():
    idx = _wired(Index.open(":memory:"))
    churn = {"src/shell.tsx": 9, "src/guards.ts": 3, "src/api.ts": 2, "src/quiet.ts": 1}
    spots = idx.contested_spots(churn=churn)
    assert isinstance(spots, list)
    for s in spots:
        assert {"file", "churn", "entanglement", "score"} <= set(s)


def test_contested_ranks_high_churn_high_entanglement_first():
    idx = _wired(Index.open(":memory:"))
    churn = {"src/shell.tsx": 9, "src/guards.ts": 3, "src/api.ts": 2, "src/quiet.ts": 1}
    spots = idx.contested_spots(churn=churn)
    # shell: most commits AND most co_changed partners -> the top hotspot
    assert spots[0]["file"] == "src/shell.tsx"


def test_contested_excludes_single_commit_files():
    """A file touched by only one commit was never re-litigated — not contested,
    no matter how entangled. min_churn defaults to 2 (it takes a back-and-forth)."""
    idx = _wired(Index.open(":memory:"))
    churn = {"src/shell.tsx": 9, "src/guards.ts": 3, "src/api.ts": 2, "src/quiet.ts": 1}
    files = [s["file"] for s in idx.contested_spots(churn=churn)]
    assert "src/quiet.ts" not in files  # only 1 commit


def test_contested_entanglement_lifts_an_equal_churn_file():
    """Two files with the SAME churn: the more entangled one ranks higher (it drags
    more code along — more team uncertainty)."""
    idx = Index.open(":memory:")
    for f, sym in [("a.ts", "A"), ("b.ts", "B"), ("c.ts", "C"), ("lonely.ts", "L")]:
        idx.stamp(title=sym, anchors=[sym.lower()], kind="code-symbol",
                  file_path=f, symbol=sym, line=1, origin="bootstrap")
    idx.record_co_change(["a.ts", "b.ts", "c.ts"])  # a is entangled with b and c
    # lonely has no co_changed partner
    churn = {"a.ts": 5, "lonely.ts": 5, "b.ts": 2, "c.ts": 2}
    spots = idx.contested_spots(churn=churn)
    rank = {s["file"]: i for i, s in enumerate(spots)}
    assert rank["a.ts"] < rank["lonely.ts"]  # equal churn, a is more entangled -> higher


def test_contested_respects_limit():
    idx = _wired(Index.open(":memory:"))
    churn = {"src/shell.tsx": 9, "src/guards.ts": 3, "src/api.ts": 2, "src/quiet.ts": 1}
    assert len(idx.contested_spots(churn=churn, limit=1)) == 1


def test_contested_empty_when_no_churn():
    idx = _wired(Index.open(":memory:"))
    assert idx.contested_spots(churn={}) == []


def test_contested_is_model_free_and_fast():
    import time
    idx = _wired(Index.open(":memory:"))
    churn = {"src/shell.tsx": 9, "src/guards.ts": 3, "src/api.ts": 2}
    t0 = time.perf_counter()
    idx.contested_spots(churn=churn)
    assert (time.perf_counter() - t0) * 1000 < 50


def test_file_churn_counts_commits_per_file(tmp_path):
    """The git churn reader: a real tiny repo, two commits touching one file twice."""
    import subprocess
    repo = tmp_path
    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       capture_output=True, text=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    git("config", "user.email", "t@t.t"); git("config", "user.name", "t")
    (repo / "hot.py").write_text("v1\n")
    git("add", "."); git("commit", "-m", "one")
    (repo / "hot.py").write_text("v2\n")
    (repo / "cold.py").write_text("x\n")
    git("add", "."); git("commit", "-m", "two")
    from recall.contested import file_churn
    churn = file_churn(repo)
    assert churn.get("hot.py") == 2   # touched by both commits
    assert churn.get("cold.py") == 1  # touched by one


def test_file_churn_no_git_is_empty(tmp_path):
    from recall.contested import file_churn
    assert file_churn(tmp_path) == {}  # not a git repo -> empty, never raises

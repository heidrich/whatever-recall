"""Drift-guards for `recall handoff` — docking point #4 (session handoff).

When a session compacts/resets, `recall handoff` stamps the in-flight state so the
NEXT session rebuilds it from recall (explain + per-file brief) instead of an
ad-hoc summary that dies with the context. These lock the two invariants that make
it work:
  - the handoff surfaces in the pre-edit brief of each in-flight file, ONCE (no
    cross-file anchor duplication — the bug found while building it 2026-06-14);
  - it surfaces in `recall "session handoff"` (the fixed handoff anchors).
"""
from __future__ import annotations

from recall import cli
from recall.engine import Index


def _repo(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".mind").mkdir(parents=True)
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    # two known files (so file_path pins, fix f631244)
    for f, sym in (("a.py", "afn"), ("b.py", "bfn")):
        idx.stamp(title=sym, anchors=[sym, sym + "tag"], kind="code-symbol",
                  file_path=f, symbol=sym, line=1, origin="bootstrap")
    idx.db.commit()
    idx.db.close()
    return repo


def test_handoff_surfaces_in_each_file_brief_exactly_once(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert cli.main(["handoff", "mid-refactor on the seat logic, tests still red",
                     "--files", "a.py,b.py", "--repo", str(repo)]) == 0
    capsys.readouterr()
    # brief on a.py shows the handoff ONCE (not duplicated via the b.py anchor)
    cli.main(["brief", "a.py", "--terse", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert out.count("mid-refactor on the seat logic") == 1, "handoff duplicated in a single brief"
    # and it's there for b.py too
    cli.main(["brief", "b.py", "--terse", "--repo", str(repo)])
    out2 = capsys.readouterr().out
    assert "mid-refactor on the seat logic" in out2


def test_handoff_is_findable_by_query(tmp_path, capsys):
    repo = _repo(tmp_path)
    cli.main(["handoff", "in-flight: wiring the webhook idempotency",
              "--files", "a.py", "--repo", str(repo)])
    capsys.readouterr()
    cli.main(["session handoff in-flight", "--terse", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert "webhook idempotency" in out


def test_handoff_without_files_makes_one_node(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert cli.main(["handoff", "general session note, no specific file",
                     "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "1 node" in out


def test_handoff_snapshots_do_not_merge(tmp_path, capsys):
    """Two handoffs on the same file must NOT merge (dedup=False) — each is a
    point-in-time snapshot; the latest is the most recent state."""
    repo = _repo(tmp_path)
    cli.main(["handoff", "first snapshot here", "--files", "a.py", "--repo", str(repo)])
    cli.main(["handoff", "second snapshot here", "--files", "a.py", "--repo", str(repo)])
    capsys.readouterr()
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    rows = idx.db.execute(
        "SELECT COUNT(*) c FROM nodes WHERE title LIKE '%snapshot here'").fetchone()
    idx.db.close()
    assert rows["c"] == 2, "handoff snapshots must not merge into one"

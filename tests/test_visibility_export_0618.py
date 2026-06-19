"""Per-node visibility + the filtered export — the security feature: your private
reasoning never leaves your machine/org unless you say so (owner 2026-06-18:
"owner entscheidungen verlassen nie die eigene umgebung, außer man will das so").

A `recall stamp --private` note stays in THIS brain; `recall export` writes a
shareable brain copy with every private node — and its edges, anchors and FTS
mirror — removed. The live brain is never modified by an export.
"""
import sqlite3
from pathlib import Path

from recall.engine import Index


def _fresh(tmp_path: Path) -> Index:
    return Index.open(tmp_path / "index.db", repo=tmp_path)


def test_visibility_defaults_to_team(tmp_path):
    ix = _fresh(tmp_path)
    r = ix.stamp(title="a normal decision", anchors=["alpha", "beta"])
    v = ix.db.execute("SELECT visibility FROM nodes WHERE id=?", (r["node_id"],)).fetchone()[0]
    assert v == "team", "a plain stamp must default to team-visible"


def test_private_flag_marks_the_node(tmp_path):
    ix = _fresh(tmp_path)
    r = ix.stamp(title="a secret", anchors=["sekret", "intern"], visibility="private")
    v = ix.db.execute("SELECT visibility FROM nodes WHERE id=?", (r["node_id"],)).fetchone()[0]
    assert v == "private"


def test_export_omits_private_nodes_and_their_traces(tmp_path):
    """The shareable export contains the team node and NOTHING of the private one —
    not the node, not its body, not its anchors, not its FTS terms."""
    ix = _fresh(tmp_path)
    ix.stamp(title="Public architecture", body="this is meant to travel",
             anchors=["pub", "arch"], visibility="team")
    ix.stamp(title="SECRET owner reasoning", body="massenweise offene tasks — internal",
             anchors=["sekret", "intern"], visibility="private")
    ix.db.commit()

    out = tmp_path / "shared.db"
    dest = Index.open(out, repo=tmp_path)
    ix.db.backup(dest.db)
    removed = dest.purge_private()
    dest.db.commit()
    dest.db.close()
    assert removed == 1

    chk = sqlite3.connect(out)
    assert chk.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1, "only the team node should remain"
    assert chk.execute("SELECT COUNT(*) FROM nodes WHERE visibility='private'").fetchone()[0] == 0
    # the secret text must be gone from nodes
    assert chk.execute(
        "SELECT COUNT(*) FROM nodes WHERE title LIKE '%SECRET%' OR body LIKE '%massenweise%'"
    ).fetchone()[0] == 0, "private node text leaked into the export"
    # its search anchors must be gone from the FTS mirror (else a search could surface it)
    assert chk.execute(
        "SELECT COUNT(*) FROM fts_anchors WHERE term IN ('sekret','intern')"
    ).fetchone()[0] == 0, "private node's FTS anchors leaked into the export"
    # the team node's anchors survive
    assert chk.execute(
        "SELECT COUNT(*) FROM fts_anchors WHERE term IN ('pub','arch')"
    ).fetchone()[0] > 0, "the team node's anchors were wrongly dropped"
    chk.close()


def test_export_does_not_touch_the_live_brain(tmp_path):
    """Exporting is read-only on the live `.mind` — the private node is still there
    after an export (it was removed only from the COPY)."""
    ix = _fresh(tmp_path)
    ix.stamp(title="local secret", anchors=["sekret"], visibility="private")
    ix.db.commit()
    before = ix.db.execute("SELECT COUNT(*) FROM nodes WHERE visibility='private'").fetchone()[0]

    out = tmp_path / "shared.db"
    dest = Index.open(out, repo=tmp_path)
    ix.db.backup(dest.db)
    dest.purge_private()
    dest.db.commit()
    dest.db.close()

    after = ix.db.execute("SELECT COUNT(*) FROM nodes WHERE visibility='private'").fetchone()[0]
    assert before == after == 1, "the live brain must keep its private node after an export"


def test_private_is_monotonically_tightening_on_restamp(tmp_path):
    """Re-stamping with --private privates a node; a later default (team) re-stamp on
    the same anchors must NOT un-private it (you can't accidentally re-share a secret)."""
    ix = _fresh(tmp_path)
    r1 = ix.stamp(title="a decision about widgets", anchors=["widget", "config", "tuning"])
    assert ix.db.execute("SELECT visibility FROM nodes WHERE id=?", (r1["node_id"],)).fetchone()[0] == "team"
    # re-stamp the SAME anchors as private → should merge + tighten to private
    ix.stamp(title="a decision about widgets", anchors=["widget", "config", "tuning"], visibility="private")
    vid = r1["node_id"]
    assert ix.db.execute("SELECT visibility FROM nodes WHERE id=?", (vid,)).fetchone()[0] == "private", \
        "a private re-stamp must tighten the node to private"
    # a default (team) re-stamp must NOT loosen it back
    ix.stamp(title="a decision about widgets", anchors=["widget", "config", "tuning"], visibility="team")
    assert ix.db.execute("SELECT visibility FROM nodes WHERE id=?", (vid,)).fetchone()[0] == "private", \
        "a default re-stamp must never un-private a node"


def test_schema_carries_visibility_with_team_default(tmp_path):
    """Fresh DB and migrated old DB both end up with a visibility column defaulting team."""
    ix = _fresh(tmp_path)
    cols = {r[1] for r in ix.db.execute("PRAGMA table_info(nodes)").fetchall()}
    assert "visibility" in cols, "the nodes schema lost the visibility column"


# --- the WATERPROOF gate (owner: "100% wasserfeste sichere rule") -------------------

def test_gate_passes_on_a_clean_export(tmp_path):
    ix = _fresh(tmp_path)
    ix.stamp(title="team", anchors=["t1", "t2"], visibility="team")
    ix.stamp(title="secret", anchors=["s1", "s2"], visibility="private")
    ix.db.commit()
    out = tmp_path / "good.db"
    dest = Index.open(out, repo=tmp_path)
    ix.db.backup(dest.db)
    dest.purge_private()
    dest.db.commit()
    dest.assert_no_private()  # must NOT raise — purge was real


def test_gate_aborts_when_a_private_node_survives(tmp_path):
    """If purge_private is ever bypassed/incomplete, the gate must catch it — the copy
    here is backed up WITHOUT a purge, so the private node is still present."""
    import pytest
    ix = _fresh(tmp_path)
    ix.stamp(title="team", anchors=["t1", "t2"], visibility="team")
    ix.stamp(title="secret", anchors=["s1", "s2"], visibility="private")
    ix.db.commit()
    leaky = tmp_path / "leaky.db"
    dest = Index.open(leaky, repo=tmp_path)
    ix.db.backup(dest.db)  # NO purge → private node present
    dest.db.commit()
    with pytest.raises(SystemExit, match="NOT private-clean"):
        dest.assert_no_private()


def test_cmd_export_deletes_the_file_on_a_failed_gate(tmp_path, monkeypatch):
    """End-to-end fail-closed: if the gate fails, cmd_export must leave NO shareable
    file behind (a leaky brain must never reach disk). We force the failure by making
    purge_private a no-op so a private node survives into the copy."""
    from recall import cli
    ix = _fresh(tmp_path)
    ix.stamp(title="team", anchors=["t1", "t2"], visibility="team")
    ix.stamp(title="secret", anchors=["s1", "s2"], visibility="private")
    ix.db.commit()
    out = tmp_path / "shared.db"

    # make purge a no-op so the gate has something to catch
    monkeypatch.setattr(Index, "purge_private", lambda self: 0)
    monkeypatch.setattr(cli, "_open_existing", lambda repo: ix)
    monkeypatch.setattr(cli, "_index_path", lambda repo: tmp_path / "index.db")

    from types import SimpleNamespace
    rc = cli.cmd_export(SimpleNamespace(out=str(out), repo=str(tmp_path)))
    assert rc == 1, "a failed gate must return non-zero"
    assert not out.exists(), "a leaky export file must be deleted, never left on disk"

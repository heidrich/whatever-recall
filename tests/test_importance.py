"""Drift-guards for ADR-016: causal importance (PageRank), the 3 parallel recall tracks
(code / knowledge / blast_radius), co_changed 'heal while coding', and the bounded
feedback nudge. All model-free — the read-path stays LLM-free (ADR-014).
"""
from __future__ import annotations

from recall import Index
from recall.importance import compute_importance, persist_importance


def _chain_graph() -> Index:
    """leaf.py -> mid.py -> core.py (depends_on). core is the most load-bearing.

    Each node gets >=2 anchors so a single-term query clears the silence_floor (2)."""
    idx = Index.open(":memory:")
    for f, sym in (("leaf.py", "leaf"), ("mid.py", "mid"), ("core.py", "core")):
        idx.stamp(title=sym, anchors=[sym, sym + "node", "graphnode"], kind="code-symbol",
                  file_path=f, symbol=sym, line=1, origin="bootstrap")
    idx.add_dependency_edges([("leaf.py", "mid.py"), ("mid.py", "core.py")])
    return idx


# ----------------------------------------------------------------- importance
def test_importance_ranks_load_bearing_highest():
    idx = _chain_graph()
    persist_importance(idx.db)
    imp = {r[0]: r[1] for r in idx.db.execute(
        "SELECT file_path, importance FROM nodes WHERE kind='code-symbol'").fetchall()}
    # core is depended-on (directly + transitively) -> highest; leaf depends on nobody.
    assert imp["core.py"] > imp["mid.py"] > imp["leaf.py"]
    assert imp["core.py"] <= 100 and imp["leaf.py"] >= 1


def test_importance_flat_graph_is_low_constant_not_all_max():
    idx = Index.open(":memory:")
    for f in ("a.py", "b.py"):
        idx.stamp(title=f, anchors=[f], kind="code-symbol", file_path=f, symbol=f, line=1)
    scores = compute_importance(idx.db)  # no edges
    assert set(scores.values()) == {1.0}  # honest low constant, not 100


def test_importance_only_code_nodes():
    idx = _chain_graph()
    idx.stamp(title="a commit", anchors=["commit"], kind="commit")
    scores = compute_importance(idx.db)
    kinds = {idx.db.execute("SELECT kind FROM nodes WHERE id=?", (i,)).fetchone()[0]
             for i in scores}
    assert kinds == {"code-symbol"}


# ----------------------------------------------------- 3 parallel tracks
def test_recall_splits_code_and_knowledge_tracks():
    idx = _chain_graph()
    persist_importance(idx.db)
    # a commit that ALSO matches the code symbols must not bury them — separate tracks.
    # Distinct anchors (+ the shared 'graphnode') so it's its own node yet co-matches.
    idx.stamp(title="fixed core handling", anchors=["graphnode", "commitword"],
              kind="commit", dedup=False)
    res = idx.recall("graphnode commitword")
    assert "code" in res and "knowledge" in res and "blast_radius" in res
    assert any(c["file"] == "core.py" for c in res["code"])      # code track has the symbol
    assert any(k["kind"] == "commit" for k in res["knowledge"])  # commit lives in knowledge
    # back-compat: the old mixed list still exists
    assert "results" in res


def test_code_track_importance_breaks_relevance_ties():
    """ADR-028: relevance is the headline axis; with EQUAL relevance the most
    load-bearing symbol still leads (importance kept as the tie-break)."""
    idx = _chain_graph()
    persist_importance(idx.db)
    # all three share the 'graphnode' anchor; query two shared terms to clear the floor.
    for f in ("leaf.py", "mid.py", "core.py"):
        nid = idx.db.execute("SELECT id FROM nodes WHERE file_path=?", (f,)).fetchone()[0]
        idx._add_anchors(nid, {"shared", "sharedtwo"})
        idx.db.commit()
    res = idx.recall("shared sharedtwo")
    files = [c["file"] for c in res["code"]]
    assert files[0] == "core.py"  # equal relevance -> most load-bearing leads


def test_code_track_relevance_beats_importance():
    """The measured 8%-r@3 failure mode: a low-importance symbol that MATCHES the
    question must outrank a load-bearing symbol that barely matches."""
    idx = _chain_graph()
    persist_importance(idx.db)
    # leaf (lowest importance) matches both query terms, core (highest) only one.
    leaf = idx.db.execute("SELECT id FROM nodes WHERE file_path='leaf.py'").fetchone()[0]
    core = idx.db.execute("SELECT id FROM nodes WHERE file_path='core.py'").fetchone()[0]
    idx._add_anchors(leaf, {"findme", "findmetwo"})
    idx._add_anchors(core, {"findme"})
    idx.db.commit()
    res = idx.recall("findme findmetwo")
    files = [c["file"] for c in res["code"]]
    assert files.index("leaf.py") < files.index("core.py")


def test_code_track_downweights_test_files():
    """ADR-028: 'where is X?' wants the implementation — a test file with the SAME
    relevance ranks below the source symbol (halved, never excluded)."""
    idx = _chain_graph()
    idx.stamp(title="test_core", anchors=["targetterm", "targettermtwo"],
              kind="code-symbol", file_path="tests/test_core.py", symbol="test_core",
              line=1, origin="bootstrap", dedup=False)
    idx.stamp(title="impl", anchors=["targetterm", "targettermtwo"],
              kind="code-symbol", file_path="impl.py", symbol="impl",
              line=1, origin="bootstrap", dedup=False)
    persist_importance(idx.db)
    res = idx.recall("targetterm targettermtwo")
    files = [c["file"] for c in res["code"]]
    assert files.index("impl.py") < files.index("tests/test_core.py")
    assert "tests/test_core.py" in files  # downweighted, NOT excluded


def test_blast_radius_one_row_per_dependent_prefers_hard_edge():
    """A dependent linked by BOTH depends_on and co_changed must appear ONCE, as the
    hard depends_on — duplicate rows used to eat the limit and hide true dependents."""
    idx = _chain_graph()
    # mid.py depends_on core.py (from _chain_graph); also co_change them.
    idx.record_co_change(["mid.py", "core.py"])
    persist_importance(idx.db)
    blast = idx._blast_radius("core.py")
    mids = [b for b in blast if b["file"] == "mid.py"]
    assert len(mids) == 1                  # deduped, not one-per-kind
    assert mids[0]["kind"] == "depends_on"  # the hard edge wins over co_changed


def test_blast_radius_lists_dependents():
    idx = _chain_graph()
    persist_importance(idx.db)
    nid = idx.db.execute("SELECT id FROM nodes WHERE file_path='core.py'").fetchone()[0]
    idx._add_anchors(nid, {"corefind", "corefindtwo"}); idx.db.commit()
    res = idx.recall("corefind corefindtwo")
    blast = {b["file"] for b in res["blast_radius"]}
    assert "mid.py" in blast  # mid depends on core -> breaks if core changes


# --------------------------------------------------- co_changed (heal while coding)
def test_co_change_creates_symmetric_edges_and_reranks():
    idx = _chain_graph()
    persist_importance(idx.db)
    before = idx.db.execute(
        "SELECT importance FROM nodes WHERE file_path='leaf.py'").fetchone()[0]
    added = idx.record_co_change(["leaf.py", "core.py"])
    assert added == 2  # symmetric: leaf<->core
    kinds = {r[0] for r in idx.db.execute(
        "SELECT kind FROM edges WHERE kind='co_changed'").fetchall()}
    assert kinds == {"co_changed"}
    after = idx.db.execute(
        "SELECT importance FROM nodes WHERE file_path='leaf.py'").fetchone()[0]
    assert after != before  # re-ranked after the new relation landed


def test_co_change_is_idempotent():
    idx = _chain_graph()
    idx.record_co_change(["leaf.py", "core.py"])
    again = idx.record_co_change(["leaf.py", "core.py"])
    assert again == 0  # the (src,dst,kind) guard prevents duplicates


def test_co_change_surfaces_in_dependency_chain():
    idx = _chain_graph()
    persist_importance(idx.db)
    # leaf and core don't import each other; only co_changed links them.
    idx.record_co_change(["leaf.py", "core.py"])
    nid = idx.db.execute("SELECT id FROM nodes WHERE file_path='leaf.py'").fetchone()[0]
    idx._add_anchors(nid, {"leaffind", "leaffindtwo"}); idx.db.commit()
    res = idx.recall("leaffind leaffindtwo")
    kinds = set()
    for r in res["results"]:
        for c in r.get("depends_on") or []:
            kinds.add(c["kind"])
    assert "co_changed" in kinds


# --------------------------------------------------- feedback nudge (bounded)
def test_feedback_useful_lifts_within_band():
    idx = _chain_graph()
    persist_importance(idx.db)
    nid = idx.db.execute("SELECT id FROM nodes WHERE file_path='mid.py'").fetchone()[0]
    before = idx.db.execute("SELECT importance FROM nodes WHERE id=?", (nid,)).fetchone()[0]
    for _ in range(5):
        idx.mark_useful(nid)
    after = idx.db.execute("SELECT importance FROM nodes WHERE id=?", (nid,)).fetchone()[0]
    assert after > before
    assert after <= round(before * 1.2, 1) + 0.1  # capped at +20%


def test_feedback_missed_lowers_within_band():
    idx = _chain_graph()
    persist_importance(idx.db)
    nid = idx.db.execute("SELECT id FROM nodes WHERE file_path='mid.py'").fetchone()[0]
    before = idx.db.execute("SELECT importance FROM nodes WHERE id=?", (nid,)).fetchone()[0]
    for _ in range(10):
        idx.mark_missed(nid)
    after = idx.db.execute("SELECT importance FROM nodes WHERE id=?", (nid,)).fetchone()[0]
    assert after < before
    assert after >= round(before * 0.8, 1) - 0.1  # never below -20%


def test_feedback_cannot_flip_pillar_to_leaf():
    """The graph LEADS: even max negative feedback keeps core above leaf."""
    idx = _chain_graph()
    persist_importance(idx.db)
    core = idx.db.execute("SELECT id FROM nodes WHERE file_path='core.py'").fetchone()[0]
    for _ in range(50):
        idx.mark_missed(core)
    imp = {r[0]: r[1] for r in idx.db.execute(
        "SELECT file_path, importance FROM nodes WHERE kind='code-symbol'").fetchall()}
    assert imp["core.py"] > imp["leaf.py"]  # bounded nudge can't invert the graph


def test_tracks_carry_sha_and_drift():
    """Track items now carry sha + drift (the CLI renders them) — additive fields,
    'results' back-compat untouched."""
    idx = _chain_graph()
    persist_importance(idx.db)
    idx.stamp(title="a why lesson", body="because measured", sha="a1b2c3d",
              anchors=["graphnode", "whylesson"], kind="lesson", dedup=False)
    res = idx.recall("graphnode whylesson")
    assert "results" in res  # back-compat pin
    assert res["knowledge"], res
    assert res["knowledge"][0]["sha"] == "a1b2c3d"
    assert "drift" in res["knowledge"][0]
    if res["code"]:
        assert "sha" in res["code"][0] and "drift" in res["code"][0]

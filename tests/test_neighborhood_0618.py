"""M5 / Workstream D (2026-06-18) — the co-change neighborhood LENS (v1.2 Stage-1/2).

brief()/impact() gain a model-free MOVES-WITH neighborhood: which files move together (git-proven),
each labeled (import-corroborated vs co-change-only) with two distinct staleness signals, fused with
the one binding decision that governs the cluster. A LENS, never a verdict. Stage-3 render is OUT.
These tests pin the labels (co-change-only is NEVER filtered — the axis-4 invariant), the two named
staleness sources, the superseded-binding drop, the shared-co-map drift-guard, and the read-only/
no-model contract. The public precision/recall claim is gated on M6's stats infra — NONE ships here.
"""

import inspect
import tempfile

import pytest

from recall import predicate as P
from recall.engine import Index


def _idx():
    return Index.open(":memory:", repo=tempfile.mkdtemp())


def _sym(ix, file, name, *, importance=None):
    r = ix.stamp(title=name, kind="code-symbol", anchors=[name.lower(), file],
                 file_path=file, symbol=name, line=1, origin="bootstrap", dedup=False)
    if importance is not None:
        ix.db.execute("UPDATE nodes SET importance=? WHERE id=?", (importance, r["node_id"]))
        ix.db.commit()
    return r["node_id"]


def _cluster_idx():
    """a.py co-changes with b.py (ALSO imports it) and c.py (co-change ONLY)."""
    ix = _idx()
    _sym(ix, "a.py", "fa")
    _sym(ix, "b.py", "fb")
    _sym(ix, "c.py", "fc")
    ix.record_co_change(["a.py", "b.py"])
    ix.record_co_change(["a.py", "c.py"])
    ix.add_dependency_edges([("a.py", "b.py")])   # a imports b → corroborated
    return ix


# --------------------------------------------------------- 1. partners + confidence labels
def test_neighborhood_labels_corroboration():
    ix = _cluster_idx()
    nb = ix.neighborhood("a.py")
    assert not nb["silenced"]
    by_file = {c["file"]: c for c in nb["cluster"]}
    assert by_file["b.py"]["confidence"] == "import + co-change"
    assert by_file["c.py"]["confidence"] == "co-change only"


# --------------------------------------- 2. axis-4: a co-change-only pair is LABELED, never filtered
def test_co_change_only_pair_appears_labeled():
    ix = _cluster_idx()
    files = [c["file"] for c in ix.neighborhood("a.py")["cluster"]]
    assert "c.py" in files, "a co-change-only pair must appear (corroboration is a LABEL, not a filter)"


# ------------------------------------------------------------- 3. cluster: none is clean
def test_cluster_none_is_honest():
    ix = _idx()
    _sym(ix, "lonely.py", "solo")
    nb = ix.neighborhood("lonely.py")
    assert nb["silenced"] and nb["cluster"] == [] and "too little co-change history" in nb["why"]


# ------------------------------------------------------- 4 + 5. binding decision (+ superseded drop)
def test_binding_decision_is_fused_and_superseded_is_dropped():
    ix = _cluster_idx()
    a_node = ix.db.execute("SELECT id FROM nodes WHERE file_path='a.py'").fetchone()[0]
    dec = ix.stamp(title="seat limit is enforced server-side", kind="decision",
                   anchors=["seatlimit"], body="never trust the client count")
    ix.db.execute("INSERT INTO edges(src_node,dst_node,kind,verified) VALUES(?,?,?,1)",
                  (dec["node_id"], a_node, "guarded_by"))
    ix.db.commit()
    assert ix.neighborhood("a.py")["bound_by"]["node_id"] == dec["node_id"]

    # now supersede that decision — it must NEVER appear as the binding decision again
    newer = ix.stamp(title="seat enforcement moved to the edge function", kind="decision",
                     anchors=["seatedge"])
    ix.db.execute("INSERT INTO edges(src_node,dst_node,kind,verified) VALUES(?,?,?,1)",
                  (newer["node_id"], dec["node_id"], "supersedes"))
    ix.db.commit()
    bound = ix.neighborhood("a.py")["bound_by"]
    assert bound is None or bound["node_id"] != dec["node_id"]


# ------------------------------------------- 6. the TWO distinct, single-sourced staleness signals
def test_two_staleness_signals_each_from_its_source():
    ix = _cluster_idx()
    # (a) edge_verified — the co_changed EDGE's own verified bool
    ix.db.execute("UPDATE edges SET verified=0 WHERE kind='co_changed'")
    # (b) partner_drift — the partner FILE's worst drift via the SAME _file_drift B/brief use
    c_node = ix.db.execute("SELECT id FROM nodes WHERE file_path='c.py'").fetchone()[0]
    ix.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"drift:{c_node}", P.BROKEN))
    ix.db.commit()
    c = next(x for x in ix.neighborhood("a.py")["cluster"] if x["file"] == "c.py")
    assert c["edge_verified"] is False                         # source (a): the edge flag
    assert c["partner_drift"] == P.BROKEN == ix._file_drift("c.py")  # source (b): _file_drift, no third source


# ------------------------------------------------ 7. drift-guard: impact's co map == _co_change_partners
def test_impact_co_map_equals_co_change_partners():
    ix = _cluster_idx()
    partners = ix._co_change_partners(["a.py"])               # the shared query
    impact_co = {r["file"]: r["co_change"] for r in ix.impact("a.py")["impacted"]
                 if r["file"] in partners}
    assert impact_co == partners                              # one query, two callers — never diverge


# ------------------------------------------------------ 8. read-only + model-free contract
def test_neighborhood_is_read_only_and_model_free():
    ix = _cluster_idx()
    def counts():
        return tuple(ix.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ("nodes", "edges", "meta"))
    before = counts()
    ix.neighborhood("a.py")
    ix.brief("a.py")            # always-on neighborhood field
    assert counts() == before  # pure SELECT — no INSERT/UPDATE/DELETE
    # the neighborhood path must not import a model
    src = inspect.getsource(Index.neighborhood) + inspect.getsource(Index._binding_decision)
    assert "recall.llm" not in src and "import llm" not in src


# ----------------------------------------------- 9. brief() field, always-on, never raises
def test_brief_carries_neighborhood_and_unknown_file_is_safe():
    ix = _cluster_idx()
    assert "neighborhood" in ix.brief("a.py")                  # always-on (not rich-gated)
    nb = ix.brief("nope_unknown.py").get("neighborhood")       # unknown file → silenced, no raise
    assert nb is not None and nb["silenced"]

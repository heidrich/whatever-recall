"""Drift-guards for recall/refine.py — LLM edge refinement (depends_on -> implements/
guarded_by). The model is ADVISORY and GROUNDED: it may only relabel existing depends_on
edges to a closed label set, never invent/delete edges. Uses EchoProvider (zero network).
"""

from __future__ import annotations

import json

from recall import Index
from recall.llm import EchoProvider
from recall.refine import refine_edges, _parse_labels, _loads


def _two_file_graph():
    """An index with one depends_on edge: handler.py -> guards.py."""
    idx = Index.open(":memory:")
    idx.stamp(title="handle", anchors=["handle"], kind="code-symbol",
              file_path="pkg/handler.py", symbol="handle", line=1, origin="bootstrap")
    idx.stamp(title="require_auth", anchors=["require_auth"], kind="code-symbol",
              file_path="pkg/guards.py", symbol="require_auth", line=1, origin="bootstrap")
    idx.add_dependency_edges([("pkg/handler.py", "pkg/guards.py")])
    return idx


# ----------------------------------------------------------------- parsing
def test_parse_labels_keeps_only_asked_targets():
    text = json.dumps({"edges": [
        {"target": "pkg/guards.py", "kind": "guarded_by"},
        {"target": "pkg/NOT-ASKED.py", "kind": "implements"},  # we never asked -> drop
    ]})
    out = _parse_labels(text, {"pkg/guards.py"})
    assert out == {"pkg/guards.py": "guarded_by"}


def test_parse_labels_tolerates_fenced_json():
    text = "Here:\n```json\n" + json.dumps({"edges": [{"target": "a", "kind": "depends_on"}]}) + "\n```"
    assert _parse_labels(text, {"a"}) == {"a": "depends_on"}


def test_loads_handles_garbage():
    assert _loads("not json at all") is None
    assert _loads("") is None


# ----------------------------------------------------------------- refine_edges
def test_refine_relabels_depends_on_to_guarded_by():
    idx = _two_file_graph()
    echo = EchoProvider(canned=json.dumps(
        {"edges": [{"target": "pkg/guards.py", "kind": "guarded_by"}]}))
    res = refine_edges(idx, echo)
    assert res.edges_refined == 1
    kind = idx.db.execute("SELECT kind FROM edges").fetchone()[0]
    assert kind == "guarded_by"


def test_refine_ignores_hallucinated_label():
    idx = _two_file_graph()
    echo = EchoProvider(canned=json.dumps(
        {"edges": [{"target": "pkg/guards.py", "kind": "teleports_to"}]}))  # not allowed
    res = refine_edges(idx, echo)
    assert res.edges_refined == 0 and res.dropped_labels == 1
    # the edge stays the safe deterministic default
    assert idx.db.execute("SELECT kind FROM edges").fetchone()[0] == "depends_on"


def test_refine_leaves_depends_on_when_model_says_depends_on():
    idx = _two_file_graph()
    echo = EchoProvider(canned=json.dumps(
        {"edges": [{"target": "pkg/guards.py", "kind": "depends_on"}]}))
    res = refine_edges(idx, echo)
    assert res.edges_refined == 0 and res.edges_unchanged == 1
    assert idx.db.execute("SELECT kind FROM edges").fetchone()[0] == "depends_on"


def test_refine_never_touches_non_depends_on_edges():
    """A semantic edge a human/commit declared must survive refinement untouched."""
    idx = _two_file_graph()
    # add a human 'supersedes' edge between the same nodes via raw insert
    ids = [r[0] for r in idx.db.execute("SELECT id FROM nodes ORDER BY id").fetchall()]
    idx.db.execute("INSERT INTO edges(src_node,dst_node,kind) VALUES(?,?,'supersedes')",
                   (ids[0], ids[1]))
    idx.db.commit()
    echo = EchoProvider(canned=json.dumps(
        {"edges": [{"target": "pkg/guards.py", "kind": "implements"}]}))
    refine_edges(idx, echo)
    kinds = {r[0] for r in idx.db.execute("SELECT kind FROM edges").fetchall()}
    assert "supersedes" in kinds  # the declared edge survived
    assert "implements" in kinds  # the depends_on got refined


def test_refine_survives_a_bad_model_reply():
    idx = _two_file_graph()
    echo = EchoProvider(canned="the model rambled and returned no json")
    res = refine_edges(idx, echo)  # must not raise
    assert res.edges_refined == 0
    assert idx.db.execute("SELECT kind FROM edges").fetchone()[0] == "depends_on"

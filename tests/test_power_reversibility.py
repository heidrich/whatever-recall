"""Drift-guards for STEP 2 — Power-Mode reversibility primitives (LLM-free).

These pin the ADR-008 guarantee: a Power-Mode run is fully reversible BEFORE a
single token is ever spent. No LLM is touched here — we stamp with origin='power'
by hand and prove undo lifts exactly the run out and nothing else.

The four lessons pinned:
  - undo_power_run removes ONLY that run's nodes/edges, never bootstrap/live;
  - the synonym-onto-existing-bootstrap-node graft is undone PRECISELY (the undo
    trap from the plan: those anchors can't CASCADE, so they're tracked + removed
    by the recorded ledger — not too much, not too little);
  - undo is idempotent (a second call is a clean no-op);
  - forget() refuses a bootstrap node without force (the base is sacred).
"""

from __future__ import annotations

from recall.db import connect
from recall.engine import Index


def _fresh() -> Index:
    return Index(connect(":memory:"))


def _anchor_terms(idx: Index, node_id: int) -> set[str]:
    return {
        r[0]
        for r in idx.db.execute(
            "SELECT DISTINCT term FROM fts_anchors WHERE node_id=?", (node_id,)
        ).fetchall()
    }


def test_undo_power_run_removes_only_that_run():
    idx = _fresh()
    boot = idx.stamp("bootstrap fact", anchors=["alpha", "beta"], origin="bootstrap")
    live = idx.stamp("live fact", anchors=["gamma", "delta"], origin="live")
    p1 = idx.stamp(
        "power insight", anchors=["epsilon", "zeta"], origin="power",
        power_run=1, base_sha="deadbeef", dedup=False,
    )
    idx.record_power_run(1, {"base_sha": "deadbeef", "status": "done", "added_anchors": {}})

    assert idx.stats()["nodes"] == 3
    res = idx.undo_power_run(1)

    assert res["nodes_removed"] == 1
    rows = idx.db.execute("SELECT id, origin FROM nodes ORDER BY id").fetchall()
    origins = {r[1] for r in rows}
    assert origins == {"bootstrap", "live"}  # power node gone, nothing else
    # the surviving nodes keep their anchors
    assert _anchor_terms(idx, boot["node_id"]) == {"alpha", "beta"}
    assert _anchor_terms(idx, live["node_id"]) == {"gamma", "delta"}
    # the power node's anchors are gone (CASCADE + fts mirror)
    assert idx.db.execute(
        "SELECT COUNT(*) FROM fts_anchors WHERE node_id=?", (p1["node_id"],)
    ).fetchone()[0] == 0
    # meta status flipped
    assert idx.power_run_info(1)["status"] == "undone"


def test_power_edge_carrying_run_tag_is_undone_by_that_tag():
    """A power edge has no power *node* to CASCADE it when its source is a pre-existing
    node — it must carry its own power_run tag and be deleted by that tag. (The edge's
    target is resolved to a code-symbol node via the normal edge path, exactly as
    power.py will create typed edges.)"""
    idx = _fresh()
    a = idx.stamp("node a", anchors=["aaa"], origin="bootstrap")
    nodes_before = idx.stats()["nodes"]  # 1 lesson; the edge target adds a code-symbol
    # a power edge a -> (code symbol "auth.py"), tagged to run 1
    idx._add_edge_to_target(a["node_id"], "touches", "auth.py", "sha1", power_run=1)
    idx.db.commit()
    assert idx.db.execute("SELECT COUNT(*) FROM edges WHERE power_run=1").fetchone()[0] == 1

    idx.record_power_run(1, {"status": "done", "added_anchors": {}})
    idx.undo_power_run(1)

    # the run-tagged edge is gone …
    assert idx.db.execute("SELECT COUNT(*) FROM edges WHERE power_run=1").fetchone()[0] == 0
    # … and the pre-existing source node survives untouched
    assert idx.db.execute(
        "SELECT origin FROM nodes WHERE id=?", (a["node_id"],)
    ).fetchone()[0] == "bootstrap"
    assert nodes_before == 1


def test_power_edge_does_not_orphan_a_newly_created_code_node():
    """The leak this guards: a power edge whose target code-symbol node doesn't exist
    yet creates one. That fresh node must inherit the run tag so undo removes it too —
    otherwise undo leaves an orphan code-symbol behind."""
    idx = _fresh()
    a = idx.stamp("source", anchors=["src"], origin="bootstrap")
    idx._add_edge_to_target(a["node_id"], "touches", "brand_new_symbol", "sha1", power_run=1)
    idx.db.commit()
    created = idx.db.execute(
        "SELECT origin, power_run FROM nodes WHERE title='brand_new_symbol'"
    ).fetchone()
    assert created["origin"] == "power" and created["power_run"] == 1

    idx.record_power_run(1, {"status": "done", "added_anchors": {}})
    idx.undo_power_run(1)

    # the fresh code-symbol node is gone — no orphan
    assert idx.db.execute(
        "SELECT COUNT(*) FROM nodes WHERE title='brand_new_symbol'"
    ).fetchone()[0] == 0
    # only the untouched bootstrap source remains
    assert idx.stats()["nodes"] == 1


def test_power_edge_reuses_existing_code_node_without_retagging_it():
    """The flip side: a code-symbol node that ALREADY exists (e.g. from bootstrap)
    must be reused as the edge target and NOT re-tagged with the run — undo keeps it."""
    idx = _fresh()
    # an existing code-symbol node (origin live, no run tag), as bootstrap would create
    existing = idx.stamp("auth.py", anchors=["auth"], kind="code-symbol", origin="bootstrap")
    a = idx.stamp("source", anchors=["src"], origin="bootstrap")
    idx._add_edge_to_target(a["node_id"], "touches", "auth.py", "sha1", power_run=1)
    idx.db.commit()
    idx.record_power_run(1, {"status": "done", "added_anchors": {}})
    idx.undo_power_run(1)

    # the pre-existing code node survives, never re-tagged
    row = idx.db.execute(
        "SELECT origin, power_run FROM nodes WHERE id=?", (existing["node_id"],)
    ).fetchone()
    assert row["origin"] == "bootstrap" and row["power_run"] is None


def test_synonym_onto_bootstrap_node_is_undone_precisely():
    """The undo trap: Power Mode grafts synonyms onto an existing bootstrap node's
    anchors. undo must remove EXACTLY those synonyms and keep the original ones."""
    idx = _fresh()
    boot = idx.stamp("rls cutover", anchors=["rls", "cutover"], origin="bootstrap")
    nid = boot["node_id"]
    assert _anchor_terms(idx, nid) == {"rls", "cutover"}

    # Power Mode merges a synonym-rich stamp onto the same node (high overlap -> MERGE).
    merge = idx.stamp(
        "rls cutover (enriched)",
        anchors=["rls", "cutover", "workspace", "tenancy"],  # 2 new synonyms
        origin="power", power_run=1, base_sha="sha1",
    )
    assert merge["action"] == "MERGE" and merge["node_id"] == nid
    assert set(merge["added_anchors"]) == {"workspace", "tenancy"}
    assert _anchor_terms(idx, nid) == {"rls", "cutover", "workspace", "tenancy"}

    # record the ledger exactly as power.py would, then undo
    idx.record_power_run(1, {
        "status": "done",
        "added_anchors": {str(nid): merge["added_anchors"]},
    })
    res = idx.undo_power_run(1)

    assert res["synonyms_removed"] == 2
    # precisely back to the pre-run anchor set — node itself survives (it's bootstrap)
    assert idx.db.execute("SELECT origin FROM nodes WHERE id=?", (nid,)).fetchone()[0] == "bootstrap"
    assert _anchor_terms(idx, nid) == {"rls", "cutover"}


def test_undo_is_idempotent():
    idx = _fresh()
    idx.stamp("power node", anchors=["x", "y"], origin="power", power_run=1, dedup=False)
    idx.record_power_run(1, {"status": "done", "added_anchors": {}})
    idx.undo_power_run(1)
    before = idx.stats()["nodes"]
    idx.undo_power_run(1)  # second call must not crash or change anything
    assert idx.stats()["nodes"] == before


def test_undo_power_all_leaves_bootstrap_and_live():
    idx = _fresh()
    idx.stamp("base", anchors=["b1"], origin="bootstrap")
    idx.stamp("commit", anchors=["c1"], origin="live")
    idx.stamp("p run 1", anchors=["p1"], origin="power", power_run=1, dedup=False)
    idx.stamp("p run 2", anchors=["p2"], origin="power", power_run=2, dedup=False)
    idx.record_power_run(1, {"status": "done", "added_anchors": {}})
    idx.record_power_run(2, {"status": "done", "added_anchors": {}})

    res = idx.undo_power_all()

    assert res["nodes_removed"] == 2
    origins = {r[0] for r in idx.db.execute("SELECT DISTINCT origin FROM nodes").fetchall()}
    assert origins == {"bootstrap", "live"}
    assert idx.db.execute("SELECT COUNT(*) FROM nodes WHERE origin='power'").fetchone()[0] == 0


def test_forget_refuses_bootstrap_without_force():
    idx = _fresh()
    boot = idx.stamp("sacred base", anchors=["s1"], origin="bootstrap")
    res = idx.forget(boot["node_id"])
    assert res["removed"] is False and "sacred" in res["reason"]
    assert idx.stats()["nodes"] == 1  # still there

    forced = idx.forget(boot["node_id"], force=True)
    assert forced["removed"] is True
    assert idx.stats()["nodes"] == 0


def test_forget_removes_power_and_live_freely():
    idx = _fresh()
    p = idx.stamp("power note", anchors=["pp"], origin="power", power_run=1, dedup=False)
    live = idx.stamp("live note", anchors=["ll"], origin="live")
    assert idx.forget(p["node_id"])["removed"] is True
    assert idx.forget(live["node_id"])["removed"] is True
    assert idx.stats()["nodes"] == 0


def test_next_power_run_increments():
    idx = _fresh()
    assert idx.next_power_run() == 1
    idx.record_power_run(1, {"status": "done"})
    assert idx.next_power_run() == 2
    idx.record_power_run(2, {"status": "done"})
    assert idx.next_power_run() == 3


def test_stamp_threads_power_run_onto_node():
    """A normal NEW power stamp persists power_run + base_sha on the node itself."""
    idx = _fresh()
    p = idx.stamp("p", anchors=["zz"], origin="power", power_run=7, base_sha="cafe", dedup=False)
    row = idx.db.execute(
        "SELECT power_run, base_sha, origin FROM nodes WHERE id=?", (p["node_id"],)
    ).fetchone()
    assert row[0] == 7 and row[1] == "cafe" and row[2] == "power"

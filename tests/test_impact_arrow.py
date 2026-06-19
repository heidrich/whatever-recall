"""The AI-native call-hierarchy replacement: `recall impact <target>` answers "if I touch
this, what is actually affected?" — fusing EMPIRICAL co-change (what git proves moved together)
with STRUCTURAL dependents (who imports it), weighted by importance, 0 tokens at read.

The thesis these tests pin: recall surfaces impact a static call graph / grep STRUCTURALLY
CANNOT — a file that co-changed with the target but has no import edge. That is the signal a
human-built call hierarchy never had.
"""

import os
import tempfile

from recall import Index
from recall.cli import _format_impact_for_prompt


def _idx():
    d = tempfile.mkdtemp()
    return Index.open(":memory:", repo=d)


def _sym(ix, file, name, *, line=1, importance=None):
    """A code-symbol node for `file` (so the file has a representative node for edges)."""
    r = ix.stamp(title=name, kind="code-symbol",
                 anchors=[name.lower(), file, f"sym{name.lower()}"],
                 file_path=file, symbol=name, line=line, origin="bootstrap", dedup=False)
    if importance is not None:
        ix.db.execute("UPDATE nodes SET importance=? WHERE id=?", (importance, r["node_id"]))
        ix.db.commit()
    return r["node_id"]


def test_impact_surfaces_a_co_change_partner_with_no_import():
    """THE killer case: a file that historically co-changed with the target but has NO import
    edge must still surface — exactly what a static call graph / grep would miss."""
    ix = _idx()
    _sym(ix, "core.py", "run")
    _sym(ix, "config.yaml_loader.py", "load")  # no import relation to core.py
    ix.record_co_change(["core.py", "config.yaml_loader.py"])
    res = ix.impact("core.py")
    hit = next((r for r in res["impacted"] if r["file"] == "config.yaml_loader.py"), None)
    assert hit is not None, "a co-change partner must surface even with no import edge"
    assert hit["co_change"] >= 1
    assert hit["struct_hop"] is None, "this is the empirical-only signal a call graph can't see"


def test_impact_surfaces_a_structural_dependent():
    ix = _idx()
    _sym(ix, "lib.py", "helper")
    _sym(ix, "app.py", "main")
    ix.add_dependency_edges([("app.py", "lib.py")])  # app depends_on lib
    res = ix.impact("lib.py")
    hit = next((r for r in res["impacted"] if r["file"] == "app.py"), None)
    assert hit is not None, "a file that imports the target must surface"
    assert hit["struct_hop"] == 1


def test_impact_resolves_a_bare_symbol_to_its_file():
    ix = _idx()
    _sym(ix, "auth.py", "login")
    _sym(ix, "ui.py", "Page")
    ix.record_co_change(["auth.py", "ui.py"])
    res = ix.impact("login")  # bare symbol, not a path
    assert res["resolved"] == ["auth.py"], "a symbol must resolve to the file that defines it"
    assert any(r["file"] == "ui.py" for r in res["impacted"])


def test_impact_excludes_the_target_itself():
    ix = _idx()
    _sym(ix, "a.py", "fa")
    _sym(ix, "b.py", "fb")
    ix.record_co_change(["a.py", "b.py"])
    res = ix.impact("a.py")
    assert all(r["file"] != "a.py" for r in res["impacted"]), "the target must not impact itself"


def test_impact_ranks_load_bearing_dependents_higher():
    """Equal signal → importance breaks it: the load-bearing dependent ranks above a trivial one."""
    ix = _idx()
    _sym(ix, "core.py", "run")
    _sym(ix, "big.py", "Big", importance=90)
    _sym(ix, "small.py", "small", importance=1)
    ix.record_co_change(["core.py", "big.py"])
    ix.record_co_change(["core.py", "small.py"])
    res = ix.impact("core.py")
    files = [r["file"] for r in res["impacted"]]
    assert files.index("big.py") < files.index("small.py"), "importance must boost the ranking"


def test_impact_walks_structural_depth():
    """app → lib → leaf: at depth 2 the transitive dependent (app) of leaf surfaces; at depth 1
    only the direct one (lib) does."""
    ix = _idx()
    _sym(ix, "leaf.py", "leaf")
    _sym(ix, "lib.py", "lib")
    _sym(ix, "app.py", "app")
    ix.add_dependency_edges([("lib.py", "leaf.py"), ("app.py", "lib.py")])
    shallow = ix.impact("leaf.py", depth=1)
    assert not any(r["file"] == "app.py" for r in shallow["impacted"]), "depth 1 stops at lib"
    deep = ix.impact("leaf.py", depth=2)
    app = next((r for r in deep["impacted"] if r["file"] == "app.py"), None)
    assert app is not None and app["struct_hop"] == 2, "depth 2 reaches the transitive dependent"


def test_impact_annotates_a_landmine():
    ix = _idx()
    _sym(ix, "danger.py", "wipe")
    _sym(ix, "caller.py", "call")
    ix.add_dependency_edges([("caller.py", "danger.py")])
    ix.stamp(title="never call wipe() without a backup", file_path="docs/incident.md",
             anchors=["wipe", "backup", "danger", "incident"],
             edges=[("warns_about", "danger.py")])
    res = ix.impact("caller.py")  # caller depends on danger; danger has a landmine
    # danger.py itself isn't in caller's impact (it's a dependency, not dependent); assert the
    # landmine annotation works on a file that IS impacted:
    res2 = ix.impact("danger.py")
    # nothing depends-on/co-changes here for danger except caller (structural up)
    hit = next((r for r in res2["impacted"] if r["file"] == "caller.py"), None)
    assert hit is not None
    # and a direct check that the flag plumbs through for a landmined file:
    ix.record_co_change(["caller.py", "danger.py"])
    flagged = ix.impact("caller.py")
    d = next((r for r in flagged["impacted"] if r["file"] == "danger.py"), None)
    assert d is not None and d["landmine"] is True, "an impacted file with a landmine must be flagged"


def test_impact_is_empty_for_an_unknown_target():
    ix = _idx()
    _sym(ix, "a.py", "fa")
    res = ix.impact("does_not_exist_anywhere")
    assert res["silenced"] is True
    assert res["impacted"] == []


def test_impact_renders_in_the_prompt_block():
    ix = _idx()
    _sym(ix, "core.py", "run")
    _sym(ix, "partner.py", "p")
    ix.record_co_change(["core.py", "partner.py"])
    block = _format_impact_for_prompt(ix.impact("core.py"))
    assert "impact for: core.py" in block
    assert "partner.py" in block and "co-changed" in block


# ─────────────── adversarial-sweep regressions (2026-06-15, impact-adversarial-sweep) ───────────────

def test_co_change_degree_is_not_double_counted():
    """#1/#11 (sweep): co_changed is stored as a symmetric pair; ONE shared relationship must
    report co_change == 1, not 2. Tight equality — the old `>= 1` test was blind to the 2× bug."""
    ix = _idx()
    _sym(ix, "a.py", "fa")
    _sym(ix, "b.py", "fb")
    ix.record_co_change(["a.py", "b.py"])
    hit = next(r for r in ix.impact("a.py")["impacted"] if r["file"] == "b.py")
    assert hit["co_change"] == 1, f"one relationship must be degree 1, got {hit['co_change']}"


def test_negative_or_zero_depth_never_makes_a_negative_score():
    """#17/#18 (sweep): depth < 1 must be clamped, not produce negative structural weights."""
    ix = _idx()
    _sym(ix, "leaf.py", "leaf")
    _sym(ix, "app.py", "app")
    ix.add_dependency_edges([("app.py", "leaf.py")])
    for d in (-5, 0):
        res = ix.impact("leaf.py", depth=d)
        assert all(r["score"] >= 0 for r in res["impacted"]), f"depth={d} produced a negative score"


def test_non_string_target_does_not_crash():
    """#19 (sweep): a non-string target must degrade gracefully, never AttributeError."""
    ix = _idx()
    _sym(ix, "a.py", "fa")
    for bad in (123, None, ["a.py"]):
        res = ix.impact(bad)  # must not raise
        assert res["silenced"] is True and res["impacted"] == []


def test_dotslash_and_trailing_slash_resolve_like_the_plain_path():
    """#5 (sweep): './core.py' and 'core.py/' must resolve to the same file as 'core.py'."""
    ix = _idx()
    _sym(ix, "core.py", "run")
    _sym(ix, "partner.py", "p")
    ix.record_co_change(["core.py", "partner.py"])
    base = {r["file"] for r in ix.impact("core.py")["impacted"]}
    assert base == {r["file"] for r in ix.impact("./core.py")["impacted"]}
    assert base == {r["file"] for r in ix.impact("core.py/")["impacted"]}


def test_prod_co_change_partner_outranks_a_test_partner_of_equal_importance():
    """#10 (sweep): test files are downweighted, so a production co-change partner ranks above a
    test partner with the same importance."""
    ix = _idx()
    _sym(ix, "core.py", "run")
    _sym(ix, "helper.py", "helper", importance=10)
    _sym(ix, "tests/test_core.py", "t", importance=10)
    ix.record_co_change(["core.py", "helper.py"])
    ix.record_co_change(["core.py", "tests/test_core.py"])
    files = [r["file"] for r in ix.impact("core.py")["impacted"]]
    assert files.index("helper.py") < files.index("tests/test_core.py")

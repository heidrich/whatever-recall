"""engine.py — stamp/recall roundtrip, 3 levels, silence, facets, dedup, freshness."""

import time

from recall import Index


def _rls_commit():
    return """fix: RLS-cutover — uploads vanished for the owner

The insert path didn't set workspace_id, so rows were NULL after the legacy drop.

Recall-anchors: rls_cutover, workspace_id, insert-pfad, scope-spalte, uploads
Recall-tags: security, backend
Recall-edge: warns_about -> supabase/migrations/032_rls.sql
Recall-why: Writers must set the new scope column on insert"""


def test_stamp_from_commit_returns_node():
    idx = Index.open(":memory:")
    r = idx.stamp_from_commit(_rls_commit(), "a1b2c3d")
    assert r["action"] == "NEW"


def test_normal_commit_without_trailer_ignored():
    idx = Index.open(":memory:")
    assert idx.stamp_from_commit("chore: typo fix", "zzz") is None


def test_recall_returns_three_levels():
    idx = Index.open(":memory:")
    idx.stamp_from_commit(_rls_commit(), "a1b2c3d")
    res = idx.recall("rls cutover workspace_id uploads weg")
    assert not res["silenced"]
    top = res["results"][0]
    # level 1 — the hit
    assert top["kind"] == "lesson"
    assert "workspace_id" in top["matched_anchors"]
    # level 2 — the meaning (the explanation, not the subject line)
    assert "insert path" in top["why"].lower()
    # level 3 — the relation (typed edge to the migration)
    kinds = {e["kind"] for e in top["relation"]}
    assert "warns_about" in kinds


def test_silence_on_nonsense():
    idx = Index.open(":memory:")
    idx.stamp_from_commit(_rls_commit(), "a1b2c3d")
    assert idx.recall("wie ist das wetter in berlin")["silenced"]


def test_silence_below_floor():
    idx = Index.open(":memory:")
    idx.stamp_from_commit(_rls_commit(), "a1b2c3d")
    # a single matching anchor is below the default floor of 2
    res = idx.recall("uploads")
    assert res["silenced"]


def test_facet_weight_orders_results():
    """Same anchors, different facets — security must outrank ui (engine_proto2)."""
    idx = Index.open(":memory:")
    idx.stamp(title="escape user input before render", anchors=["render", "comment", "input", "escape"], tags=["security"])
    idx.stamp(title="comment box padding 16px", anchors=["render", "comment", "padding", "box"], tags=["ui"])
    res = idx.recall("render comment input box")
    assert res["results"][0]["title"].startswith("escape")  # security wins


def test_dedup_via_recall_merges_restatement():
    idx = Index.open(":memory:")
    idx.stamp(title="RLS cutover writers set workspace_id on insert",
              anchors=["rls_cutover", "workspace_id", "insert", "scope-spalte", "uploads", "tenancy", "writer"])
    before = idx.stats()["nodes"]
    r = idx.stamp(title="After tenancy switch uploads vanish for owner",
                  anchors=["rls_cutover", "workspace_id", "insert", "scope-spalte", "uploads", "tenancy", "writer"])
    assert r["action"] == "MERGE"
    assert idx.stats()["nodes"] == before  # no new node


def test_dedup_lets_foreign_topic_through():
    idx = Index.open(":memory:")
    idx.stamp(title="RLS cutover", anchors=["rls_cutover", "workspace_id", "uploads", "tenancy"])
    r = idx.stamp(title="Stripe webhook idempotency",
                  anchors=["stripe", "webhook", "idempotency", "unique-index", "race"])
    assert r["action"] == "NEW"


def test_recall_is_sub_millisecond_on_small_index():
    idx = Index.open(":memory:")
    idx.stamp_from_commit(_rls_commit(), "a1b2c3d")
    t0 = time.perf_counter()
    idx.recall("rls cutover workspace_id")
    assert (time.perf_counter() - t0) * 1000 < 50  # generous CI ceiling


def test_fts5_special_chars_never_crash():
    """Adversarial: FTS5 operators / quotes / wildcards in a query must never
    raise — the anchor extractor strips them before they reach MATCH."""
    idx = Index.open(":memory:")
    idx.stamp(title="x", anchors=["alpha", "beta", "gamma"])
    for q in ['a"b', "NEAR(", "foo OR bar", "*", "col:val", 'x" OR "1"="1', "", "   ", "🎯 ünïcödé"]:
        idx.recall(q)  # must not raise


def test_results_deduped_by_title_and_file():
    idx = Index.open(":memory:")
    # two distinct nodes, same title+file (a merge commit + the direct commit)
    for _ in range(2):
        idx.stamp(title="viewer core split", anchors=["viewer", "core", "split", "editor", "embed"],
                  kind="commit", file_path="src/app/embed/page.tsx", dedup=False)
    res = idx.recall("viewer core split editor embed")
    titles = [r["title"] for r in res["results"]]
    assert titles.count("viewer core split") == 1  # collapsed


# ---- static dependency graph (recall.graph wired into the engine) ----
def _two_files(idx):
    """Two files, each with a code-symbol node, so dependency edges can connect them."""
    idx.stamp(title="PortalShell", anchors=["portalshell", "shell"], kind="code-symbol",
              file_path="src/shell.tsx", symbol="PortalShell", line=1, origin="bootstrap")
    idx.stamp(title="authGuard", anchors=["authguard", "guard"], kind="code-symbol",
              file_path="src/lib/guards.ts", symbol="authGuard", line=1, origin="bootstrap")


def test_add_dependency_edges_links_files():
    idx = Index.open(":memory:")
    _two_files(idx)
    added = idx.add_dependency_edges([("src/shell.tsx", "src/lib/guards.ts")])
    assert added == 1
    assert idx.db.execute("SELECT COUNT(*) FROM edges WHERE kind='depends_on'").fetchone()[0] == 1


def test_add_dependency_edges_is_idempotent():
    idx = Index.open(":memory:")
    _two_files(idx)
    idx.add_dependency_edges([("src/shell.tsx", "src/lib/guards.ts")])
    again = idx.add_dependency_edges([("src/shell.tsx", "src/lib/guards.ts")])
    assert again == 0  # same edge not duplicated
    assert idx.db.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1


def test_add_dependency_edges_skips_unknown_file():
    idx = Index.open(":memory:")
    _two_files(idx)
    # target file has no code-symbol node -> no edge (a wrong edge is worse than none)
    assert idx.add_dependency_edges([("src/shell.tsx", "src/nope.ts")]) == 0


def test_recall_surfaces_file_dependency_chain_even_on_a_commit_hit():
    """The graph payoff: a hit that is a COMMIT (no edges of its own) still shows what
    its FILE depends on, by seeding from the file's code-symbols."""
    idx = Index.open(":memory:")
    _two_files(idx)
    idx.add_dependency_edges([("src/shell.tsx", "src/lib/guards.ts")])
    # a commit node pinned to the same file, found by its own anchors
    idx.stamp(title="feat: portal shell", anchors=["portal", "shell", "layout", "feat"],
              kind="commit", file_path="src/shell.tsx", dedup=False)
    res = idx.recall("portal shell layout")
    top = res["results"][0]
    assert top["kind"] == "commit"  # a commit ranked top...
    deps = [d["target"] for d in top.get("depends_on", [])]
    assert "src/lib/guards.ts" in deps  # ...yet the file's dependency chain still shows


def test_file_dependencies_empty_when_not_file_pinned():
    idx = Index.open(":memory:")
    idx.stamp(title="floating lesson", anchors=["floating", "lesson", "idea"], kind="lesson")
    res = idx.recall("floating lesson idea")
    assert res["results"][0]["depends_on"] == []


# ----------------------------------------------------- BM25 scoring (ADR-025 wave)
def test_long_node_no_longer_dominates():
    """The roadmap case: a long node (many anchors) that co-matches must not bury a
    short, precise code-symbol on volume. Length normalization is the fix."""
    idx = Index.open(":memory:")
    filler = [f"filler{i:03d}" for i in range(150)]
    idx.stamp(title="ten feature roadmap", kind="task", tags=["roadmap"],
              anchors=["rareterm", "common1", "common2", "common3"] + filler, dedup=False)
    idx.stamp(title="the precise symbol", kind="code-symbol", file_path="x.py",
              symbol="precise", line=1, anchors=["rareterm", "common1", "common2"],
              dedup=False)
    # spread the common terms over more nodes so their IDF drops below rareterm's
    for i in range(6):
        idx.stamp(title=f"noise {i}", anchors=["common1", "common2", f"noisepad{i}"],
                  kind="commit", dedup=False)
    res = idx.recall("rareterm common1 common2")
    titles = [r["title"] for r in res["results"]]
    assert titles[0] == "the precise symbol", titles


def test_idf_rare_term_beats_common_volume():
    """A node carrying the RARE query term outranks one with more hits on common terms."""
    idx = Index.open(":memory:")
    for i in range(10):
        idx.stamp(title=f"common carrier {i}", anchors=["commonword", f"pad{i}"],
                  kind="commit", dedup=False)
    idx.stamp(title="the mtime node", anchors=["mtimeword", "commonword"],
              kind="lesson", dedup=False)
    res = idx.recall("mtimeword commonword")
    assert res["results"][0]["title"] == "the mtime node"


def test_scoring_is_deterministic():
    idx = Index.open(":memory:")
    for i in range(5):
        idx.stamp(title=f"twin {i}", anchors=["twinword", "pairword"], kind="lesson",
                  dedup=False)
    a = idx.recall("twinword pairword")
    b = idx.recall("twinword pairword")
    assert [r["title"] for r in a["results"]] == [r["title"] for r in b["results"]]
    assert [r["score"] for r in a["results"]] == [r["score"] for r in b["results"]]


def test_floor_cannot_be_manufactured_by_weights():
    """A 1-hit node stays silenced even with the loudest facet (security 2.0) — the
    docstring promise: weighting can't manufacture a hit from nothing."""
    idx = Index.open(":memory:")
    idx.stamp(title="single anchor security note", anchors=["lonelyanchor"],
              tags=["security"], dedup=False)
    assert idx.recall("lonelyanchor")["silenced"]


def test_query_stopwords_silence_filler_but_keep_domain_words():
    idx = Index.open(":memory:")
    idx.stamp(title="open tasks live here", anchors=["open", "tasks"], kind="task",
              dedup=False)
    # pure filler query -> no tokens left -> silent (anti-Clippy)
    assert idx.recall("what is still where does")["silenced"]
    # 'open' and 'tasks' are deliberately NOT stopped — domain words
    assert not idx.recall("open tasks")["silenced"]


def test_query_stop_does_not_affect_stamping():
    """QUERY_STOP is query-side only — stamped prose keeps its anchors."""
    idx = Index.open(":memory:")
    idx.stamp(title="warum das design so ist", body="weil es gemessen wurde",
              anchors=["warum", "design", "gemessen"], dedup=False)
    n = idx.db.execute(
        "SELECT COUNT(*) FROM fts_anchors WHERE term MATCH '\"warum\"'").fetchone()[0]
    assert n == 1  # the anchor exists in the index even though queries drop it


# ------------------------------------------- hook hot-path (token cap + stats cache)
def test_query_token_cap_keeps_known_rare_first():
    """Hook queries are whole edit texts (100+ tokens); the engine caps at the
    _QUERY_TOKEN_CAP most informative: known-rare first, then unknown (possible
    stemming matches), known-common cut first WITHIN the known set."""
    from recall.engine import _QUERY_TOKEN_CAP
    idx = Index.open(":memory:")
    for i in range(5):  # 'common' sits on 5 nodes, 'needle' on one
        idx.stamp(title=f"filler {i}", anchors=["common", f"pad{i}"], kind="commit",
                  dedup=False)
    idx.stamp(title="the needle", anchors=["needle", "needletwo"], kind="commit",
              dedup=False)
    oversized = ["common", "needle", "needletwo"] + [f"unseen{i}" for i in range(40)]
    capped = idx._cap_query_tokens(oversized)
    assert len(capped) == _QUERY_TOKEN_CAP
    # 40 unseen edit-text identifiers must never evict the known terms; within the
    # known set the rare ones lead (common is the first to go under cap pressure)
    assert capped[:3] == ["needle", "needletwo", "common"]
    # at-or-under the cap: passthrough, order untouched (typed questions never change)
    small = ["alpha", "beta"]
    assert idx._cap_query_tokens(small) == small


def test_corpus_stats_cached_and_invalidated_on_writes():
    """The two full-table COUNTs run once, then come from the cache; ANY anchor/node
    mutation (stamp, delete) drops the cache so BM25 never computes on stale stats."""
    idx = Index.open(":memory:")
    idx.stamp(title="first", anchors=["alpha", "alphatwo"], kind="commit", dedup=False)
    assert idx._corpus_stats is None  # the write invalidated (nothing cached yet)
    idx.recall("alpha alphatwo")
    assert idx._corpus_stats is not None  # recall filled the cache
    n_docs, total = idx._corpus_stats
    fresh = (idx.db.execute(
        "SELECT COUNT(DISTINCT node_id) FROM node_anchors").fetchone()[0],
        idx.db.execute("SELECT COUNT(*) FROM node_anchors").fetchone()[0])
    assert (n_docs, total) == fresh
    idx.stamp(title="second", anchors=["beta", "betatwo"], kind="commit", dedup=False)
    assert idx._corpus_stats is None  # stamp dropped it
    idx.recall("beta betatwo")
    assert idx._corpus_stats[1] == total + 2  # recomputed with the new anchors


def test_rules_query_stopwords_silence_project_filler():
    """A project-added stopword is dropped at query time; the same word still
    works as a STAMPED anchor for other queries (write path untouched)."""
    from recall.rules import Rules
    from dataclasses import replace
    idx = Index.open(":memory:")
    idx.rules = replace(idx.rules, query_stopwords=frozenset({"projectfiller"}))
    idx.stamp(title="target note", anchors=["realterm", "realtermtwo"], dedup=False)
    # filler-only query -> no tokens -> silent (the added word acts like QUERY_STOP)
    assert idx.recall("projectfiller")["silenced"]
    # mixed query: the filler is dropped, the real terms still hit
    assert not idx.recall("projectfiller realterm realtermtwo")["silenced"]

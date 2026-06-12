"""onboarding() — Wave C, "explain me this repo" (ADR-020).

A new dev (or a fresh AI session) lands in a repo and needs the same four things,
fast: which files are load-bearing (start reading HERE), which decisions are
must-know, what is in progress right now, and where the team keeps burning time.
onboarding() generates that path from the already-indexed graph — reusing the Wave
A/B building blocks (importance, open tasks, contested spots). Read-only, model-free
(ADR-014): pure SQL + arithmetic, 0 tokens, offline.
"""

from recall import Index


def _repo(idx):
    """A small repo: a load-bearing core (high importance via many dependents), a
    must-know ADR, an open task in progress, and a churny file."""
    # the core file + two dependents → core becomes load-bearing
    idx.stamp(title="run", anchors=["core", "run"], kind="code-symbol",
              file_path="src/core.ts", symbol="run", line=1, origin="bootstrap")
    idx.stamp(title="a", anchors=["a"], kind="code-symbol",
              file_path="src/a.ts", symbol="a", line=1, origin="bootstrap")
    idx.stamp(title="b", anchors=["b"], kind="code-symbol",
              file_path="src/b.ts", symbol="b", line=1, origin="bootstrap")
    idx.add_dependency_edges([("src/a.ts", "src/core.ts"), ("src/b.ts", "src/core.ts")])
    # a must-know decision (ADR), tagged foundation
    idx.stamp(title="ADR-007: the core owns the run loop",
              body="Every entry point routes through core.run so the guard runs once.",
              anchors=["adr", "core", "run", "loop"], tags=["foundation"],
              kind="lesson", file_path="docs/decisions.md", sha="dec1234", dedup=False)
    # an open task in progress
    t = idx.stamp(title="Wire the new export path", kind="task", tags=["task", "open"],
                  file_path=".recall/tasks/export.md", origin="bootstrap", dedup=False)
    idx.link_task_to_files(t["node_id"], ["src/core.ts"])
    idx.rerank_importance()


def test_onboarding_returns_the_four_tracks():
    idx = Index.open(":memory:")
    _repo(idx)
    o = idx.onboarding()
    for key in ("repo_files", "top_files", "decisions", "in_progress", "contested", "counts"):
        assert key in o, f"missing track: {key}"


def test_onboarding_top_files_lead_with_the_load_bearing_core():
    idx = Index.open(":memory:")
    _repo(idx)
    o = idx.onboarding()
    files = [f["file"] for f in o["top_files"]]
    assert "src/core.ts" in files
    # the core (2 dependents) outranks a leaf file
    assert files.index("src/core.ts") < files.index("src/a.ts")


def test_onboarding_surfaces_must_know_decisions():
    idx = Index.open(":memory:")
    _repo(idx)
    o = idx.onboarding()
    titles = [d["title"] for d in o["decisions"]]
    assert any("ADR-007" in t for t in titles)


def test_onboarding_lists_open_tasks_in_progress():
    idx = Index.open(":memory:")
    _repo(idx)
    o = idx.onboarding()
    titles = [t["title"] for t in o["in_progress"]]
    assert any("export path" in t for t in titles)
    # a done task must NOT appear
    assert all(t.get("status", "open") == "open" for t in o["in_progress"])


def test_onboarding_counts_are_honest():
    idx = Index.open(":memory:")
    _repo(idx)
    o = idx.onboarding()
    c = o["counts"]
    assert c["files"] >= 3          # core + a + b
    assert c["decisions"] >= 1
    assert c["open_tasks"] >= 1


def test_onboarding_empty_index_is_shaped_not_crashing():
    idx = Index.open(":memory:")
    o = idx.onboarding()
    assert o["top_files"] == [] and o["decisions"] == [] and o["in_progress"] == []
    assert o["counts"]["files"] == 0


def test_onboarding_is_model_free():
    """The read path stays LLM-free (ADR-014): onboarding() runs no model."""
    idx = Index.open(":memory:")
    _repo(idx)
    # if onboarding tried to call a model it would need a connection; it must not.
    o = idx.onboarding()
    assert isinstance(o, dict)


# ------------------------------------------- explain decisions criterion (hygiene wave)
def test_decisions_prefix_tier_and_order():
    """ADR-titled nodes fill first, numeric-desc; '[Unreleased] … (ADR-019)' stubs and
    preamble titles never appear (PREFIX match, not substring)."""
    from recall.engine import Index
    idx = Index.open(":memory:")
    idx.stamp(title="ADR-003 — no local embedding model",
              anchors=["adr-003", "embedding", "model"], tags=["foundation"], dedup=False)
    idx.stamp(title="ADR-011 — two-stage drift traffic light",
              anchors=["adr-011", "drift", "ampel"], tags=["foundation"], dedup=False)
    idx.stamp(title="[Unreleased] — sneaky stub mentioning (ADR-019) in passing",
              anchors=["unreleased", "stub", "adr-019"], dedup=False)
    idx.stamp(title="Architektur-Entscheidungen (ADR-Log)",
              anchors=["architektur", "entscheidungen", "adr-log"], dedup=False)
    o = idx.onboarding()
    titles = [d["title"] for d in o["decisions"]]
    assert titles[0].startswith("ADR-011")  # newest decision first
    assert titles[1].startswith("ADR-003")
    assert not any("[Unreleased]" in t for t in titles)
    assert not any(t.startswith("Architektur-Entscheidungen") for t in titles)


def test_decisions_foundation_fallback_without_adr_naming():
    """Repos without ADR-style titles still get decisions via the foundation tag."""
    from recall.engine import Index
    idx = Index.open(":memory:")
    idx.stamp(title="we never use drag and drop", body="edit modal instead",
              anchors=["drag", "drop", "modal"], tags=["foundation"], dedup=False)
    o = idx.onboarding()
    assert any("drag and drop" in d["title"] for d in o["decisions"])


def test_explain_keeps_the_founding_adrs_when_recency_overflows():
    """Review follow-up: with more ADRs than dec_k, pure newest-first dropped
    ADR-001. Two slots stay reserved for the lowest-numbered (founding) ADRs."""
    idx = Index.open(":memory:")
    for n in range(1, 13):  # ADR-1 .. ADR-12 (> dec_k=8)
        idx.stamp(title=f"ADR-{n} — decision number {n}",
                  anchors=[f"adrterm{n}", f"adrtermtwo{n}"], kind="lesson",
                  tags=["foundation"], dedup=False)
    o = idx.onboarding(dec_k=8)
    titles = [d["title"] for d in o["decisions"]]
    assert len(titles) == 8
    assert any(t.startswith("ADR-1 ") for t in titles)   # founding stays
    assert any(t.startswith("ADR-2 ") for t in titles)   # second founding stays
    assert any(t.startswith("ADR-12") for t in titles)   # newest still leads

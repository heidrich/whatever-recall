"""brief(file) — Wave A, the Pre-Edit Briefing (ADR-018).

Before a file is edited, recall bundles — for that ONE file — everything it
already knows, so a bewusste Entscheidung is never silently undone:
  why        — the commits/lessons/ADRs pinned to the file (the knowledge track)
  breaks     — who depends on it (blast radius) → what I risk breaking
  depends_on — what it leans on (the static graph)
  open_tasks — unfinished plans/tasks wired to it (ADR-017, the standing intent)
  symbols    — the code-symbols defined in the file (what's in it)

All five are read-only SQL over the already-stamped graph — no model is run
(ADR-014: the read path stays LLM-free, 0 tokens, offline).
"""

from recall import Index


def _portal(idx):
    """A small repo: a shell that depends on a guard, each with code-symbols, a
    commit explaining the shell, and a guard with a security lesson on it."""
    idx.stamp(title="PortalShell", anchors=["portalshell", "shell"], kind="code-symbol",
              file_path="src/shell.tsx", symbol="PortalShell", line=1, origin="bootstrap")
    idx.stamp(title="renderNav", anchors=["rendernav", "nav"], kind="code-symbol",
              file_path="src/shell.tsx", symbol="renderNav", line=40, origin="bootstrap")
    idx.stamp(title="authGuard", anchors=["authguard", "guard"], kind="code-symbol",
              file_path="src/lib/guards.ts", symbol="authGuard", line=1, origin="bootstrap")
    # shell depends on the guard (the static import graph)
    idx.add_dependency_edges([("src/shell.tsx", "src/lib/guards.ts")])
    # a commit that explains WHY the shell is the way it is, pinned to the file
    idx.stamp(title="feat: portal shell wraps every route in the guard",
              body="Every authenticated route must mount inside PortalShell so the guard runs once.",
              anchors=["portal", "shell", "guard", "route", "wrap", "feat"],
              kind="commit", file_path="src/shell.tsx", sha="abc1234", dedup=False)


def test_brief_returns_the_five_tracks():
    idx = Index.open(":memory:")
    _portal(idx)
    b = idx.brief("src/shell.tsx")
    assert b["file"] == "src/shell.tsx"
    # the keys a briefing must always carry, even when empty
    for key in ("why", "breaks", "depends_on", "open_tasks", "symbols"):
        assert key in b


def test_brief_why_surfaces_the_pinned_commit():
    """Level 2: WHY is this file the way it is — its commits/lessons/ADRs."""
    idx = Index.open(":memory:")
    _portal(idx)
    b = idx.brief("src/shell.tsx")
    titles = [w["title"] for w in b["why"]]
    assert any("portal shell wraps" in t for t in titles)
    # a code-symbol is NOT knowledge — it belongs in `symbols`, never in `why`
    assert all(w["kind"] != "code-symbol" for w in b["why"])


def test_brief_depends_on_shows_the_static_chain():
    idx = Index.open(":memory:")
    _portal(idx)
    b = idx.brief("src/shell.tsx")
    targets = [d["target"] for d in b["depends_on"]]
    assert "src/lib/guards.ts" in targets


def test_brief_breaks_lists_dependents_of_a_leaf_file():
    """Editing the guard endangers the shell that depends on it (the reverse edge)."""
    idx = Index.open(":memory:")
    _portal(idx)
    b = idx.brief("src/lib/guards.ts")
    files = [x["file"] for x in b["breaks"]]
    assert "src/shell.tsx" in files


def test_brief_symbols_lists_the_files_own_code_symbols():
    idx = Index.open(":memory:")
    _portal(idx)
    b = idx.brief("src/shell.tsx")
    names = {s["symbol"] for s in b["symbols"]}
    assert {"PortalShell", "renderNav"} <= names
    # symbols from OTHER files must not leak in
    assert "authGuard" not in names


def test_brief_symbols_excludes_the_file_representative_node():
    """A file carries a symbol=NULL/line=NULL representative node (it holds the
    file→file edges). That is the file, not a symbol — it must not show in `symbols`."""
    idx = Index.open(":memory:")
    _portal(idx)
    # mimic the bootstrap file-representative node (no symbol, no line)
    idx.stamp(title="src/shell.tsx", anchors=["shellfile"], kind="code-symbol",
              file_path="src/shell.tsx", origin="bootstrap")
    b = idx.brief("src/shell.tsx")
    titles = {s["symbol"] for s in b["symbols"]}
    assert "src/shell.tsx" not in titles
    assert {"PortalShell", "renderNav"} <= titles


def test_brief_surfaces_an_open_task_wired_to_the_file():
    """ADR-017: an open task on the file is the standing intent — the briefing's
    whole point is that I cannot edit the file without seeing it."""
    idx = Index.open(":memory:")
    _portal(idx)
    r = idx.stamp(title="Plan: move the guard into middleware", kind="task",
                  tags=["plan"], body="- [ ] move guard\n- [ ] drop per-route mount")
    idx.link_task_to_files(r["node_id"], ["src/shell.tsx"])
    b = idx.brief("src/shell.tsx")
    titles = [t["title"] for t in b["open_tasks"]]
    assert any("move the guard" in t for t in titles)


def test_brief_hides_a_done_task():
    idx = Index.open(":memory:")
    _portal(idx)
    r = idx.stamp(title="Done: shell exists", kind="task", tags=["done"])
    idx.link_task_to_files(r["node_id"], ["src/shell.tsx"])
    b = idx.brief("src/shell.tsx")
    assert all("Done: shell exists" not in t["title"] for t in b["open_tasks"])


def test_brief_unknown_file_is_empty_not_an_error():
    """A file recall has never seen returns an empty-but-shaped briefing, never raises."""
    idx = Index.open(":memory:")
    _portal(idx)
    b = idx.brief("src/never/seen.ts")
    assert b["file"] == "src/never/seen.ts"
    assert b["known"] is False
    assert b["why"] == [] and b["symbols"] == [] and b["breaks"] == []


def test_brief_known_flag_true_for_indexed_file():
    idx = Index.open(":memory:")
    _portal(idx)
    assert idx.brief("src/shell.tsx")["known"] is True


def test_brief_normalises_windows_path_separators():
    """A caller on Windows passes backslashes; the index stores forward slashes."""
    idx = Index.open(":memory:")
    _portal(idx)
    b = idx.brief("src\\shell.tsx")
    assert b["known"] is True
    assert {"PortalShell", "renderNav"} <= {s["symbol"] for s in b["symbols"]}


def test_brief_is_model_free_and_fast():
    """ADR-014 in spirit: a briefing is pure SQL — fast, deterministic, offline."""
    import time
    idx = Index.open(":memory:")
    _portal(idx)
    t0 = time.perf_counter()
    idx.brief("src/shell.tsx")
    assert (time.perf_counter() - t0) * 1000 < 50  # generous CI ceiling


def test_stamp_anchor_path_pins_file_path():
    """Dogfood fix 2026-06-14: `stamp --anchors <known-path>` (no explicit --file)
    must PIN the note's file_path to that file, so brief(<path>) surfaces it. The
    anchor must match a file recall already indexes (a stray phrase like 'see foo'
    never pins). Before the fix the note floated with file_path=NULL, invisible to brief."""
    idx = Index.open(":memory:")
    _portal(idx)  # seeds src/shell.tsx as a known file
    r = idx.stamp(title="Shell mounts the guard ON PURPOSE — do not 'simplify' it away",
                  anchors=["src/shell.tsx"], kind="lesson", dedup=False)
    row = idx.db.execute("SELECT file_path FROM nodes WHERE id=?", (r["node_id"],)).fetchone()
    assert (row[0] or "").replace("\\", "/") == "src/shell.tsx", "path-like anchor did not pin file_path"


def test_stamp_anchor_unknown_path_does_not_pin():
    """A path-shaped anchor that is NOT a known file must NOT pin file_path — only
    real indexed files anchor a note, so a 'see config/old.yml' phrase can't hijack."""
    idx = Index.open(":memory:")
    _portal(idx)
    r = idx.stamp(title="random note mentioning some/unknown/path.ts",
                  anchors=["some/unknown/path.ts"], kind="lesson", dedup=False)
    row = idx.db.execute("SELECT file_path FROM nodes WHERE id=?", (r["node_id"],)).fetchone()
    assert row[0] is None, "an unknown path-anchor must not pin file_path"


def test_brief_surfaces_a_note_anchored_by_path_even_without_file_path():
    """Dogfood fix B 2026-06-14: brief collects WHY via file_path OR an anchor that
    is the file's path-term — so a note attached with `stamp --anchors <path>` shows
    up even on an older 'floating' note whose file_path was never set."""
    idx = Index.open(":memory:")
    _portal(idx)
    # force a floating note: anchored to the path term, but file_path explicitly NULL
    idx.stamp(title="The guard runs ONCE in the shell — a per-route guard is a regression",
              anchors=["src/shell.tsx"], kind="lesson", file_path=None, dedup=False)
    # simulate the pre-fix state: blank its file_path so only the anchor connects it
    idx.db.execute("UPDATE nodes SET file_path=NULL WHERE title LIKE 'The guard runs ONCE%'")
    idx.db.commit()
    b = idx.brief("src/shell.tsx")
    titles = [w["title"] for w in b["why"]]
    assert any("runs ONCE in the shell" in t for t in titles), "brief did not surface the anchor-only note"

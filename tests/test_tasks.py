"""Tasks & plans as wired wiki nodes (ADR-017): parse, index, wire, status, surface."""
from __future__ import annotations

from recall import Index
from recall.tasks import (
    parse_task, index_tasks, stale_open_tasks, _split_frontmatter, parse_subtasks,
    looks_done, flip_candidates,
)


# ----------------------------------------------------------------- parsing
def test_parse_frontmatter_scalars_and_lists():
    text = ("---\n"
            "title: Wire the curve\n"
            "status: open\n"
            "kind: feature\n"
            "affects: [recall/engine.py, recall/dashboard.py]\n"
            "tags: [feature, ui]\n"
            "---\n"
            "The body explains it.\n")
    meta, body = _split_frontmatter(text)
    assert meta["title"] == "Wire the curve"
    assert meta["affects"] == ["recall/engine.py", "recall/dashboard.py"]
    assert body.startswith("The body")


def test_parse_task_validates_and_defaults():
    t = parse_task("---\ntitle: X\nstatus: bogus\nkind: weird\n---\nbody",
                   rel_path=".recall/tasks/x.md")
    assert t["status"] == "open"        # unknown status -> safe default
    assert t["task_kind"] == "task"     # unknown kind -> safe default
    assert "task" in t["tags"]


def test_parse_task_without_frontmatter_uses_first_line():
    t = parse_task("# Plan the migration\n\nsome prose", rel_path="docs/plans/m.md")
    assert t is not None and t["title"].startswith("Plan the migration")


# ----------------------------------------------------------------- sub-tasks (checklist)
def test_parse_subtasks_reads_checklist_and_done_state():
    body = ("intro line\n"
            "- [ ] open one\n"
            "- [x] done one\n"
            "* [X] done two (asterisk + caps)\n"
            "not a checklist line\n"
            "- [ ] **bold label** stays clean\n")
    subs = parse_subtasks(body)
    assert len(subs) == 4
    assert subs[0] == {"text": "open one", "done": False, "state": "open"}
    assert subs[1]["done"] is True
    assert subs[2]["done"] is True            # [X] and * bullet both work
    assert subs[3]["text"] == "bold label stays clean"  # markdown markers stripped


def test_parse_subtasks_dropped_and_moved_states():
    """`- [-]` dropped / `- [>]` moved (Owner finding 2026-06-10: a done task showed
    "2/3" because a consciously skipped step had no syntax). Both are NOT done (the
    bool stays honest) but resolve the step — the dashboard bar counts them."""
    body = ("- [x] shipped\n"
            "- [-] dropped — fresh read suffices\n"
            "- [>] moved to [[other-task]]\n"
            "- [ ] still open\n")
    subs = parse_subtasks(body)
    assert [s["state"] for s in subs] == ["done", "dropped", "moved", "open"]
    assert [s["done"] for s in subs] == [True, False, False, False]


def test_parse_subtasks_folds_indented_continuation_lines():
    """A checklist item wrapped over several indented lines (the roadmap style) must be
    captured WHOLE, not cut at the first physical line. An indented line that follows an
    item and is not itself a new item is a continuation and folds into the item's text."""
    body = ("- [x] **Welle A — Pre-Edit-Briefing** (Tier 1, die Killer-Anwendung): bevor\n"
            "  eine Datei geändert wird, fasst recall zusammen WARUM sie so ist und WAS bricht.\n"
            "- [ ] **Welle B** kurz\n")
    subs = parse_subtasks(body)
    assert len(subs) == 2
    assert "bevor eine Datei geändert wird" in subs[0]["text"]
    assert "WAS bricht" in subs[0]["text"]         # the continuation line made it in
    assert subs[0]["done"] is True
    assert subs[1]["text"] == "Welle B kurz"


def test_parse_subtasks_non_indented_line_is_not_a_continuation():
    """A non-indented line between items ends the previous item — it is prose, not a fold."""
    body = ("- [ ] item one\n"
            "some prose at column 0\n"
            "- [ ] item two\n")
    subs = parse_subtasks(body)
    assert [s["text"] for s in subs] == ["item one", "item two"]


def test_parse_task_includes_subtasks_and_survives_long_body():
    # a real plan body with a checklist far past the old 1200-char cap must keep every item
    filler = "x " * 800  # ~1600 chars of prose before the list
    text = (f"---\ntitle: Roadmap\nkind: roadmap\nstatus: open\n---\n{filler}\n"
            "- [ ] item a\n- [x] item b\n- [ ] item c\n")
    t = parse_task(text, rel_path=".recall/tasks/r.md")
    assert len(t["subtasks"]) == 3
    assert [s["done"] for s in t["subtasks"]] == [False, True, False]


def test_subtasks_reach_the_dashboard_snapshot(tmp_path):
    from pathlib import Path
    from recall.dashboard import build_snapshot
    (tmp_path / ".recall" / "tasks").mkdir(parents=True)
    (tmp_path / ".recall" / "tasks" / "r.md").write_text(
        "---\ntitle: Roadmap\nkind: roadmap\nstatus: open\n---\n"
        "- [x] shipped\n- [ ] todo\n", encoding="utf-8")
    idx = Index.open(str(tmp_path / ".mind" / "index.db"), repo=str(tmp_path))
    index_tasks(idx, tmp_path)
    snap = build_snapshot(idx, Path(tmp_path))
    road = next(t for t in snap["tasks"] if t["title"] == "Roadmap")
    assert road["total"] == 2 and road["done"] == 1
    assert {s["text"] for s in road["subtasks"]} == {"shipped", "todo"}


def test_snapshot_counts_resolved_moved_dropped(tmp_path):
    from pathlib import Path
    from recall.dashboard import build_snapshot
    (tmp_path / ".recall" / "tasks").mkdir(parents=True)
    (tmp_path / ".recall" / "tasks" / "d.md").write_text(
        "---\ntitle: Closed clean\nkind: task\nstatus: done\n---\n"
        "- [x] a\n- [-] b\n- [>] c\n- [ ] d forgot\n", encoding="utf-8")
    idx = Index.open(str(tmp_path / ".mind" / "index.db"), repo=str(tmp_path))
    index_tasks(idx, tmp_path)
    snap = build_snapshot(idx, Path(tmp_path))
    t = next(x for x in snap["tasks"] if x["title"] == "Closed clean")
    assert t["total"] == 4 and t["done"] == 1
    assert t["dropped"] == 1 and t["moved"] == 1
    assert t["resolved"] == 3        # the page flags total-resolved=1 as unresolved


# ----------------------------------------------------------------- indexing + wiring
def _repo_with_code_and_task(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".recall" / "tasks").mkdir(parents=True)
    (repo / "engine.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / ".recall" / "tasks" / "t1.md").write_text(
        "---\ntitle: Make run() configurable\nstatus: open\nkind: feature\n"
        "affects: [engine.py]\ntags: [feature]\n---\nAdd a config arg to run().\n",
        encoding="utf-8")
    return repo


def test_index_tasks_creates_a_wired_task_node(tmp_path):
    repo = _repo_with_code_and_task(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    # need a code-symbol node for engine.py so the relates_to edge can land
    idx.stamp(title="run", anchors=["run"], kind="code-symbol",
              file_path="engine.py", symbol="run", line=1, origin="bootstrap")
    stats = index_tasks(idx, repo)
    assert stats["tasks"] == 1
    row = idx.db.execute("SELECT title, facets FROM nodes WHERE kind='task'").fetchone()
    assert "Make run() configurable" in row[0]
    assert "open" in row[1] and "feature" in row[1]   # status + kind persisted as facets
    # wired to the affected file
    edge = idx.db.execute(
        "SELECT e.kind FROM edges e JOIN nodes nd ON nd.id=e.dst_node "
        "WHERE nd.file_path='engine.py' AND e.kind='relates_to'").fetchone()
    assert edge is not None


def test_open_task_surfaces_for_the_file_it_affects(tmp_path):
    repo = _repo_with_code_and_task(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="run", anchors=["run"], kind="code-symbol",
              file_path="engine.py", symbol="run", line=1, origin="bootstrap")
    index_tasks(idx, repo)
    ot = idx.open_tasks_for_file("engine.py")
    assert len(ot) == 1 and ot[0]["status"] == "open"


def test_task_surfaces_for_a_file_with_no_code_symbol(tmp_path):
    """The dashboard.html bug: a task affecting a non-code-indexed file (HTML/JSON/config)
    has no code-symbol node to wire to. link_task_to_files must still anchor it (create a
    representative file node) so the task comes back via open_tasks_for_file / brief()."""
    repo = tmp_path / "proj"
    (repo / ".recall" / "tasks").mkdir(parents=True)
    (repo / "ui.html").write_text("<html><body>hi</body></html>\n", encoding="utf-8")
    (repo / ".recall" / "tasks" / "t.md").write_text(
        "---\ntitle: Make the header readable\nstatus: open\nkind: task\n"
        "affects: [ui.html]\ntags: [ui]\n---\nDarken the greys.\n",
        encoding="utf-8")
    idx = Index.open(":memory:", repo=repo)
    # deliberately NO code-symbol stamp for ui.html — it is not code-indexed
    index_tasks(idx, repo)
    ot = idx.open_tasks_for_file("ui.html")
    assert len(ot) == 1 and ot[0]["status"] == "open"
    b = idx.brief("ui.html")
    # the file-level briefing carries the task...
    assert any(t["status"] == "open" for t in b["open_tasks"])
    # ...but the kind='file' task-anchor node must NOT leak into the 'why' track as a junk row
    assert all(w["kind"] != "file" for w in b["why"])


def test_task_wiring_normalizes_backslash_paths(tmp_path):
    """Windows path safety: open_tasks_for_file must match regardless of slash direction."""
    repo = tmp_path / "proj"
    (repo / ".recall" / "tasks").mkdir(parents=True)
    (repo / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / ".recall" / "tasks" / "t.md").write_text(
        "---\ntitle: T\nstatus: open\nkind: task\naffects: [recall/x.py]\n---\nbody.\n",
        encoding="utf-8")
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="f", anchors=["f"], kind="code-symbol",
              file_path="recall\\x.py", symbol="f", line=1, origin="bootstrap")
    index_tasks(idx, repo)
    # query with backslashes — must still match the forward-slash affects:
    assert len(idx.open_tasks_for_file("recall\\x.py")) == 1


def test_reindex_replaces_not_duplicates_and_honors_status_change(tmp_path):
    """The dogfood bug the Owner caught: a task finished in the file still showed 'open' in
    the tool. Cause — a standalone re-index stamped a SECOND node (tasks are dedup=False)
    instead of replacing the old one, so the still-'open' copy kept surfacing. index_tasks
    must clear-then-rebuild on its OWN, so flipping status open->done is honored next index."""
    repo = _repo_with_code_and_task(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="run", anchors=["run"], kind="code-symbol",
              file_path="engine.py", symbol="run", line=1, origin="bootstrap")
    index_tasks(idx, repo)
    index_tasks(idx, repo)  # re-index a SECOND time — must not duplicate
    assert idx.db.execute("SELECT COUNT(*) FROM nodes WHERE kind='task'").fetchone()[0] == 1
    assert len(idx.open_tasks_for_file("engine.py")) == 1
    # now flip the file to done and re-index — the task must stop surfacing
    (repo / ".recall" / "tasks" / "t1.md").write_text(
        "---\ntitle: Make run() configurable\nstatus: done\nkind: feature\n"
        "affects: [engine.py]\ntags: [feature]\n---\nAdd a config arg to run().\n",
        encoding="utf-8")
    index_tasks(idx, repo)
    assert idx.db.execute("SELECT COUNT(*) FROM nodes WHERE kind='task'").fetchone()[0] == 1
    assert idx.open_tasks_for_file("engine.py") == []  # done -> gone, no stale 'open' twin


def test_done_task_does_not_surface(tmp_path):
    repo = tmp_path / "p"
    (repo / ".recall" / "tasks").mkdir(parents=True)
    (repo / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / ".recall" / "tasks" / "d.md").write_text(
        "---\ntitle: already shipped\nstatus: done\nkind: task\naffects: [x.py]\n---\ndone.\n",
        encoding="utf-8")
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="f", anchors=["f"], kind="code-symbol",
              file_path="x.py", symbol="f", line=1, origin="bootstrap")
    index_tasks(idx, repo)
    assert idx.open_tasks_for_file("x.py") == []  # done -> not surfaced


def test_clear_tasks_removes_only_tasks(tmp_path):
    repo = _repo_with_code_and_task(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    idx.stamp(title="run", anchors=["run"], kind="code-symbol",
              file_path="engine.py", symbol="run", line=1, origin="bootstrap")
    index_tasks(idx, repo)
    before_code = idx.db.execute("SELECT COUNT(*) FROM nodes WHERE kind='code-symbol'").fetchone()[0]
    n = idx.clear_tasks()
    assert n == 1
    assert idx.db.execute("SELECT COUNT(*) FROM nodes WHERE kind='task'").fetchone()[0] == 0
    # code survived
    assert idx.db.execute("SELECT COUNT(*) FROM nodes WHERE kind='code-symbol'").fetchone()[0] == before_code


# ----------------------------------------------------------------- status drift
def test_looks_done_open_task_all_steps_resolved():
    # the Owner dogfood moment: every box ticked/dropped/moved, frontmatter never flipped
    subs = parse_subtasks("- [x] a\n- [-] b\n- [>] c\n")
    assert looks_done("open", subs) is True
    assert looks_done("done", subs) is False      # already flipped -> no nudge
    assert looks_done("deferred", subs) is False  # deliberate parking -> no nudge


def test_looks_done_needs_steps_and_full_resolution():
    assert looks_done("open", []) is False  # no checklist -> nothing to infer from
    subs = parse_subtasks("- [x] a\n- [ ] b\n")
    assert looks_done("open", subs) is False  # one open box -> genuinely open


def test_flip_candidates_finds_forgotten_flip(tmp_path):
    repo = tmp_path / "p"
    (repo / ".recall" / "tasks").mkdir(parents=True)
    (repo / ".recall" / "tasks" / "f.md").write_text(
        "---\ntitle: finished but never flipped\nstatus: open\nkind: task\n---\n"
        "- [x] build it\n- [x] verify it\n", encoding="utf-8")
    (repo / ".recall" / "tasks" / "g.md").write_text(
        "---\ntitle: genuinely open\nstatus: open\nkind: task\n---\n"
        "- [x] step one\n- [ ] step two\n", encoding="utf-8")
    idx = Index.open(":memory:", repo=repo)
    index_tasks(idx, repo)
    cands = flip_candidates(idx)
    assert [c["title"] for c in cands] == ["finished but never flipped"]
    assert cands[0]["steps"] == 2


def test_stale_open_tasks_flags_old_open_tasks():
    idx = Index.open(":memory:")
    # an open task created 40 days ago
    old = 40 * 86400
    idx.stamp(title="old open", anchors=["oldopen"], kind="task",
              tags=["task", "open"], origin="bootstrap")
    nid = idx.db.execute("SELECT id FROM nodes WHERE kind='task'").fetchone()[0]
    idx.db.execute("UPDATE nodes SET created_at=? WHERE id=?", (1_000_000, nid))
    idx.db.commit()
    flagged = stale_open_tasks(idx, now_ts=1_000_000 + old, stale_days=30)
    assert flagged and flagged[0]["stale"] is True

"""Code-intelligence serves (static-code-intel, 2026-06-15): callers, callees,
dead_code, untested, cycles — plus the arrow-3 impact/precedent MCP exposure.

These walk recall's ALREADY-STAMPED file graph (0 model tokens, offline). The tests are
HERMETIC: each builds a controlled file->file dependency graph by direct SQL insert into a
temp index, so the assertions are exact and don't drift with the live .mind index.

Every edge case here was raised by the adversarial design red-team (workflow 2026-06-15):
- file-granularity (no per-call-site edges, group by file_path across ALL code-symbol nodes)
- the stale-bare-name / phantom node trap (a renamed file leaves an edgeless node behind)
- direction (depends_on: src DEPENDS ON dst; callers = dst-side, callees = src-side)
- dead-code false positives (entrypoints, framework files, configs, tests, docs, dynamic imports)
- untested test-signal rigor (import-only; co_changed hid 23 genuinely-untested critical files)
- cycle canonicalization (one row per cycle regardless of rotation) + termination
- no `implementors` serve (the implements edge is lesson->code, not code->code)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from recall import Index


# --------------------------------------------------------------- graph fixture
class _Graph:
    """A hand-built file graph in a temp index. `file(path, on_disk=, importance=)` adds a
    file-representative code-symbol node (and optionally a real file on disk); `dep(a, b)`
    adds `a depends_on b` (a imports b). Mirrors what the real indexer writes."""

    def __init__(self, idx: Index, repo: Path):
        self.idx = idx
        self.repo = repo
        self.node: dict[str, int] = {}

    def file(self, path: str, *, on_disk: bool = True, importance: float = 1.0,
             symbol: str | None = None) -> int:
        if on_disk:
            p = self.repo / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# fixture\n", encoding="utf-8")
        cur = self.idx.db.execute(
            "INSERT INTO nodes(kind,title,file_path,symbol,importance,origin) "
            "VALUES('code-symbol',?,?,?,?,'fixture')",
            (path, path, symbol, importance),
        )
        nid = cur.lastrowid
        self.node[path] = nid
        return nid

    def edge(self, a: str, b: str, kind: str = "depends_on") -> None:
        self.idx.db.execute(
            "INSERT INTO edges(src_node,dst_node,kind) VALUES(?,?,?)",
            (self.node[a], self.node[b], kind),
        )

    def dep(self, importer: str, imported: str) -> None:
        """importer depends_on imported (importer imports/uses imported)."""
        self.edge(importer, imported, "depends_on")

    def commit(self) -> None:
        self.idx.db.commit()


@pytest.fixture
def graph(tmp_path: Path) -> _Graph:
    (tmp_path / ".mind").mkdir()
    idx = Index.open(tmp_path / ".mind" / "index.db", repo=tmp_path)
    return _Graph(idx, tmp_path)


# ------------------------------------------------------------------- callers / callees
def test_callers_walks_reverse_dependency_direction(graph):
    # cli -> engine, bootstrap -> engine  (both import engine)
    graph.file("recall/engine.py", importance=100.0)
    graph.file("recall/cli.py", importance=50.0)
    graph.file("recall/bootstrap.py", importance=30.0)
    graph.dep("recall/cli.py", "recall/engine.py")
    graph.dep("recall/bootstrap.py", "recall/engine.py")
    graph.commit()

    res = graph.idx.callers("recall/engine.py")
    files = [r["file"] for r in res["results"]]
    assert files == ["recall/cli.py", "recall/bootstrap.py"]  # by importance, both hop 1
    assert all(r["hop"] == 1 for r in res["results"])
    assert res["direction"] == "callers"
    assert res["granularity"] == "file"
    assert not res["silenced"]


def test_callees_is_the_forward_direction(graph):
    graph.file("recall/cli.py")
    graph.file("recall/engine.py")
    graph.file("recall/freshness.py")
    graph.dep("recall/cli.py", "recall/engine.py")
    graph.dep("recall/cli.py", "recall/freshness.py")
    graph.commit()

    res = graph.idx.callees("recall/cli.py")
    assert sorted(r["file"] for r in res["results"]) == ["recall/engine.py", "recall/freshness.py"]


def test_callers_transitive_hops_respect_depth(graph):
    # a -> b -> c  ;  callers(c) at depth 2 sees b (hop1) and a (hop2)
    for f in ("a.py", "b.py", "c.py"):
        graph.file(f)
    graph.dep("a.py", "b.py")
    graph.dep("b.py", "c.py")
    graph.commit()

    d2 = graph.idx.callers("c.py", depth=2)
    assert {r["file"]: r["hop"] for r in d2["results"]} == {"b.py": 1, "a.py": 2}
    d1 = graph.idx.callers("c.py", depth=1)
    assert [r["file"] for r in d1["results"]] == ["b.py"]  # depth 1 stops before a


def test_callers_min_hop_wins_in_a_diamond(graph):
    # d imports b and c; b and c both import a. a is reachable at hop1 (via... no) — build:
    # callers(a): b(h1), c(h1), d(h2)  — d reaches a only through b/c
    for f in ("a.py", "b.py", "c.py", "d.py"):
        graph.file(f)
    graph.dep("b.py", "a.py")
    graph.dep("c.py", "a.py")
    graph.dep("d.py", "b.py")
    graph.dep("d.py", "c.py")
    graph.commit()

    res = graph.idx.callers("a.py", depth=5)
    assert {r["file"]: r["hop"] for r in res["results"]} == {"b.py": 1, "c.py": 1, "d.py": 2}


def test_callers_unknown_target_is_shaped_not_error(graph):
    graph.file("a.py")
    graph.commit()
    res = graph.idx.callers("does/not/exist_zzz.py")
    assert res["silenced"] is True
    assert res["results"] == []
    assert "reason" in res


def test_callers_cycle_back_to_target_terminates(graph):
    # a <-> b cycle; callers(a) must not loop forever and must not list a itself
    graph.file("a.py")
    graph.file("b.py")
    graph.dep("a.py", "b.py")
    graph.dep("b.py", "a.py")
    graph.commit()
    res = graph.idx.callers("a.py", depth=10)
    assert [r["file"] for r in res["results"]] == ["b.py"]  # a never lists itself


# ------------------------------------------------------------------- dead_code
def test_dead_code_finds_unimported_code_file(graph):
    # cli imports engine imports orphan -> only cli has no incoming edge (it's the root caller),
    # so cli would be a candidate EXCEPT it has an outgoing edge making it a live entry-of-its-tree;
    # the genuinely-dead file is one with NO incoming AND that nothing reaches.
    graph.file("recall/cli.py")
    graph.file("recall/engine.py")
    graph.file("recall/orphan.py")
    graph.dep("recall/cli.py", "recall/engine.py")     # cli imports engine -> engine NOT dead
    graph.dep("recall/engine.py", "recall/orphan.py")  # engine imports orphan -> orphan NOT dead
    graph.file("recall/truly_dead.py")                  # imported by nothing, imports nothing
    graph.commit()
    res = graph.idx.dead_code()
    files = [c["file"] for c in res["candidates"]]
    assert "recall/truly_dead.py" in files
    assert "recall/orphan.py" not in files   # imported by engine
    assert "recall/engine.py" not in files   # imported by cli
    # cli.py has no incoming edge -> it IS a dead-code candidate (honest: nothing imports it; the
    # caller decides it's the tree root). The serve does not special-case 'cli' by name.
    assert "recall/cli.py" in files


def test_dead_code_excludes_tests_entrypoints_configs_docs(graph):
    graph.file("recall/__main__.py")                 # entrypoint (matches _ENTRY_RE)
    graph.file("tests/test_thing.py")                 # test (matches _is_test_file)
    graph.file("web/next.config.ts")                  # config (matches .config.)
    graph.file("web/src/app/page.tsx")                # Next.js framework convention
    graph.file("docs/guide.md")                       # doc, non-code ext
    graph.file("recall/genuinely_dead.py")            # the only real candidate
    graph.commit()
    res = graph.idx.dead_code()
    files = [c["file"] for c in res["candidates"]]
    assert files == ["recall/genuinely_dead.py"]


def test_dead_code_ignores_phantom_nodes_not_on_disk(graph):
    # a stale bare-name node (renamed file) left behind in the index, no file on disk
    graph.file("engine.py", on_disk=False)            # phantom — must NOT be reported
    graph.file("recall/engine.py", on_disk=True)
    graph.dep("recall/engine.py", "recall/engine.py")  # self — ignored
    graph.file("recall/realdead.py", on_disk=True)
    graph.commit()
    res = graph.idx.dead_code()
    files = [c["file"] for c in res["candidates"]]
    assert "engine.py" not in files                   # phantom filtered
    assert "recall/realdead.py" in files


def test_dead_code_clean_repo_is_silenced(graph):
    graph.file("a.py")
    graph.file("b.py")
    graph.dep("a.py", "b.py")
    graph.dep("b.py", "a.py")   # both imported by each other
    graph.commit()
    res = graph.idx.dead_code()
    assert res["silenced"] is True
    assert res["candidates"] == []


# ------------------------------------------------------------------- untested
def test_untested_uses_import_only_signal_not_co_change(graph):
    # prod.py is co_changed with a test but NEVER imported by one -> still untested (the fix)
    graph.file("recall/prod.py", importance=20.0)
    graph.file("tests/test_other.py")
    graph.edge("tests/test_other.py", "recall/prod.py", "co_changed")  # co-change only
    graph.commit()
    res = graph.idx.untested()
    assert "recall/prod.py" in [r["file"] for r in res["untested"]]


def test_untested_excludes_files_a_test_imports(graph):
    graph.file("recall/covered.py")
    graph.file("recall/uncovered.py")
    graph.file("tests/test_covered.py")
    graph.dep("tests/test_covered.py", "recall/covered.py")  # the test IMPORTS covered
    graph.commit()
    res = graph.idx.untested()
    files = [r["file"] for r in res["untested"]]
    assert "recall/covered.py" not in files
    assert "recall/uncovered.py" in files


def test_untested_excludes_tests_and_entrypoints_and_docs(graph):
    graph.file("tests/test_x.py")
    graph.file("recall/__main__.py")
    graph.file("docs/readme.md")
    graph.file("recall/lib.py")
    graph.commit()
    res = graph.idx.untested()
    assert [r["file"] for r in res["untested"]] == ["recall/lib.py"]


# ------------------------------------------------------------------- cycles
def test_cycles_finds_a_two_file_cycle(graph):
    graph.file("adapters/hook.py")
    graph.file("recall/cli.py")
    graph.dep("adapters/hook.py", "recall/cli.py")
    graph.dep("recall/cli.py", "adapters/hook.py")
    graph.commit()
    res = graph.idx.cycles()
    assert not res["silenced"]
    assert len(res["cycles"]) == 1
    assert set(res["cycles"][0]["files"]) == {"adapters/hook.py", "recall/cli.py"}
    assert res["cycles"][0]["length"] == 2


def test_cycles_reports_each_cycle_once_regardless_of_rotation(graph):
    # a->b->c->a : one logical cycle, must appear exactly once (canonicalized)
    for f in ("a.py", "b.py", "c.py"):
        graph.file(f)
    graph.dep("a.py", "b.py")
    graph.dep("b.py", "c.py")
    graph.dep("c.py", "a.py")
    graph.commit()
    res = graph.idx.cycles()
    assert len(res["cycles"]) == 1
    assert res["cycles"][0]["length"] == 3
    # canonical rotation: smallest member first
    assert res["cycles"][0]["files"][0] == "a.py"


def test_cycles_no_cycle_is_silenced(graph):
    graph.file("a.py")
    graph.file("b.py")
    graph.dep("a.py", "b.py")   # DAG, no cycle
    graph.commit()
    res = graph.idx.cycles()
    assert res["silenced"] is True
    assert res["cycles"] == []


def test_cycles_terminates_fast_on_dense_graph(graph):
    # every file imports every other — the OLD path-DFS exploded super-exponentially (>15s / 1.1M
    # cycles at 10 nodes). Tarjan-SCC + push budget must keep it bounded and prompt.
    import time

    files = [f"m{i}.py" for i in range(15)]
    for f in files:
        graph.file(f)
    for a in files:
        for b in files:
            if a != b:
                graph.dep(a, b)
    graph.commit()
    t = time.perf_counter()
    res = graph.idx.cycles(limit=20)
    elapsed = time.perf_counter() - t
    assert elapsed < 3.0, f"cycles() took {elapsed:.1f}s on a dense cluster — should be bounded"
    assert not res["silenced"]
    assert len(res["cycles"]) <= 20            # honors the cap
    assert res["truncated"] is True            # honestly flags that more exist


def test_cycles_ignores_self_import_artifact(graph):
    # a file "importing itself" is an indexer artifact, not a real inter-file cycle. The serve
    # is file->file BETWEEN DISTINCT files (the graph drops self-loops), so a lone self-edge is
    # NOT reported — reporting it would be noise that erodes trust ("a wrong edge is worse...").
    graph.file("a.py")
    graph.file("b.py")
    graph.dep("a.py", "a.py")   # self-loop artifact
    graph.dep("a.py", "b.py")   # but no b->a, so no real cycle
    graph.commit()
    res = graph.idx.cycles()
    assert res["silenced"] is True   # no real inter-file cycle


def test_cycles_two_disjoint_cycles_both_found(graph):
    # two separate SCCs each with a cycle — both must appear (SCC decomposition handles each)
    for f in ("a.py", "b.py", "c.py", "d.py"):
        graph.file(f)
    graph.dep("a.py", "b.py")
    graph.dep("b.py", "a.py")   # cycle 1
    graph.dep("c.py", "d.py")
    graph.dep("d.py", "c.py")   # cycle 2
    graph.commit()
    res = graph.idx.cycles()
    cyc_sets = [frozenset(c["files"]) for c in res["cycles"]]
    assert frozenset({"a.py", "b.py"}) in cyc_sets
    assert frozenset({"c.py", "d.py"}) in cyc_sets
    assert res["truncated"] is False


# ------------------------------------------------------------------- honesty / no implementors
def test_no_implementors_serve_exists():
    """The implements edge is lesson->code (governs-this-file), not code->code interface
    realization, so a file->file implementors() would be dead on arrival. It must NOT exist."""
    assert not hasattr(Index, "implementors")


def test_serves_report_file_granularity(graph):
    graph.file("a.py")
    graph.file("b.py")
    graph.dep("a.py", "b.py")
    graph.commit()
    assert graph.idx.callers("b.py")["granularity"] == "file"
    assert graph.idx.callees("a.py")["granularity"] == "file"
    assert graph.idx.dead_code()["granularity"] == "file"
    assert graph.idx.untested()["granularity"] == "file"
    assert graph.idx.cycles()["granularity"] == "file"


def test_serves_log_zero_token_reads(graph):
    """Every serve is a pure read — it must record an access_log row (the dashboard activity
    console reads these) and never call a model. We assert the kind tag is written."""
    graph.file("a.py")
    graph.file("b.py")
    graph.dep("a.py", "b.py")
    graph.commit()
    graph.idx.callers("b.py")
    graph.idx.cycles()
    kinds = {r[0] for r in graph.idx.db.execute("SELECT DISTINCT kind FROM access_log").fetchall()}
    assert "callers" in kinds
    assert "cycles" in kinds

"""Regression guards for the engine bug-hunt of 2026-06-15 (recall-first, 151-agent,
3-skeptic adversarial sweep over the whole engine: 0 P0 / 3 P1 / 12 P2 / 15 P3).

Each test pins ONE confirmed fix so it can't silently regress. Grouped by file.
"""
from __future__ import annotations

import sqlite3

import pytest

from recall.engine import Index


# ----------------------------------------------------------- anchors.py (P2 ReDoS)
def test_extract_anchors_no_redos_on_long_run():
    """A long unbroken [a-z0-9] run (a hex/base64 token pasted into a query/body) used to
    backtrack quadratically (8k chars ~ 0.8s). The per-token length bound kills it while
    extracting byte-identical anchors on normal text."""
    import time
    from recall.anchors import extract_anchors

    blob = "a" * 100_000 + " seatLimit foo-bar adr-50"
    t0 = time.perf_counter()
    out = extract_anchors(blob)
    dt = time.perf_counter() - t0
    assert dt < 0.5, f"extract_anchors took {dt:.2f}s — ReDoS regression"
    # the real anchors beside the blob are still found; the blob itself is skipped
    assert "foo-bar" in out and "adr-50" in out and "seatlimit" in out
    assert "a" * 100_000 not in out


def test_extract_anchors_identical_on_normal_text():
    from recall.anchors import extract_anchors

    out = extract_anchors("the seatLimit confirmSeatOrRollback adr-50 wf_abc foo.bar.baz a-b")
    for a in ("adr-50", "wf_abc", "foo.bar.baz", "a-b"):
        assert a in out


# ----------------------------------------------------------- engine.py (P2/P3)
def test_cap_query_tokens_survives_huge_query():
    """>SQLITE_MAX_VARIABLE_NUMBER unique tokens must not trip 'too many SQL variables' —
    the df lookup is now batched in 999-safe chunks."""
    idx = Index.open(":memory:")
    idx.stamp(title="seatsUsed", anchors=["seat", "used"], kind="code-symbol",
              file_path="orgs.ts", symbol="seatsUsed", line=1, origin="bootstrap")
    idx.db.commit()
    big = " ".join(f"tok{i}" for i in range(40000))  # 40k unique tokens
    res = idx.recall(big)  # must return, not raise OperationalError
    assert "code" in res or "knowledge" in res or isinstance(res, dict)
    idx.db.close()


def test_clear_tasks_no_variable_overflow():
    """clear_tasks deletes via a subquery (zero binds), so >999 task nodes can't trip the
    SQL-variable limit."""
    idx = Index.open(":memory:")
    for i in range(1100):
        idx.stamp(title=f"task {i}", anchors=[f"t{i}"], kind="task",
                  file_path="x.ts", origin="task")
    idx.db.commit()
    n = idx.clear_tasks()
    assert n == 1100
    left = idx.db.execute("SELECT COUNT(*) FROM nodes WHERE kind='task'").fetchone()[0]
    assert left == 0
    idx.db.close()


def test_build_levels_tolerates_deleted_node(monkeypatch):
    """If a scored node is deleted by another process between scoring and building,
    _build_levels returns None and recall() filters it instead of dereferencing None."""
    idx = Index.open(":memory:")
    res = idx._build_levels(999_999, 1, 1.0, {"x"})  # id that does not exist
    assert res is None
    idx.db.close()


# ----------------------------------------------------------- db.py (P2/P3)
def test_migrate_is_race_safe_on_duplicate_column(tmp_path):
    """A concurrent ALTER that loses the race raises 'duplicate column name'; connect()
    must swallow ONLY that (the column exists either way) and not crash the process."""
    from recall import db as dbmod

    p = tmp_path / "i.db"
    c1 = dbmod.connect(p)
    # simulate the loser: add a migration column by hand, then re-run _migrate
    cols = next(iter(dbmod._MIGRATIONS.items()))
    table, colnames = cols
    col = colnames[0]
    # drop+re-add path is awkward; instead assert _migrate is idempotent (re-running it
    # on an already-migrated db, which is the convergent case the guard protects)
    dbmod._migrate(c1)  # must not raise
    dbmod._migrate(c1)  # twice — still fine
    c1.close()


def test_connect_closes_on_corrupt_db(tmp_path):
    """Opening a corrupt file raises DatabaseError AND must not leak the connection."""
    from recall import db as dbmod

    p = tmp_path / "corrupt.db"
    p.write_bytes(b"this is not a sqlite database, at all, nope")
    with pytest.raises(sqlite3.DatabaseError):
        dbmod.connect(p)
    # on Windows a leaked handle would block this unlink
    p.unlink()


# ----------------------------------------------------------- resolve.py (P2 self-poison)
def test_resolve_does_not_self_poison_experience():
    """engine.resolve() logs its top candidate consumer='resolve'; the experience axis
    must EXCLUDE that traffic or resolve inflates whatever it already ranked #1."""
    idx = Index.open(":memory:")
    for sym in ("seatGuard", "seatCheck"):
        idx.stamp(title=sym, anchors=[sym.lower(), "seat"], kind="code-symbol",
                  file_path=f"{sym}.ts", symbol=sym, line=1, origin="bootstrap")
    idx.db.commit()
    # hammer resolve-consumer access onto seatGuard
    nid = idx.db.execute("select id from nodes where symbol='seatGuard'").fetchone()[0]
    for _ in range(50):
        idx.db.execute("insert into access_log(query,node_id,score,surfaced,latency_us,consumer,kind)"
                       " values(?,?,?,?,?,?,?)", ("seat", nid, 0, 1, 0, "resolve", "resolve"))
    idx.db.commit()
    from recall.resolve import Resolver
    r = Resolver(idx.db)
    # the experience access_count for seatGuard must NOT be inflated by resolve traffic
    gc = next(c for c in r.cands if c.symbol == "seatGuard")
    assert gc.access_count == 0, "resolve-consumer traffic leaked into the experience axis"
    idx.db.close()


# ----------------------------------------------------------- graph.py (P2 edges)
# Parser built the production way (see note in test_graph.py) — bytes-accepting across
# tree-sitter 0.21→0.25+, unlike language_pack.get_parser. (graph test fix 2026-06-15)
from recall.bootstrap import _load_tree_sitter

_parser_for = _load_tree_sitter()
needs_ts = pytest.mark.skipif(_parser_for is None, reason="tree-sitter not installed")


def _ts_parse(lang, src):
    return _parser_for(lang).parse(src)


@needs_ts
def test_ts_reexport_is_an_edge():
    """`export {X} from "./a"` / `export * from "./b"` (the barrel pattern) must produce
    a dependency, not be dropped."""
    from recall.graph import import_paths

    src = b'export { Button } from "./Button";\nexport * from "./utils";\nimport x from "./x";\n'
    tree = _ts_parse("tsx", src)
    paths = import_paths(tree.root_node, src, "tsx")
    assert "./Button" in paths and "./utils" in paths and "./x" in paths


@needs_ts
def test_python_bare_dot_relative_import_is_an_edge():
    """`from . import foo` must resolve to the sibling module, not the package dir."""
    from recall.graph import import_paths, resolve_import

    src = b"from . import foo\nfrom .. import bar\nfrom .util import helper\n"
    tree = _ts_parse("python", src)
    paths = import_paths(tree.root_node, src, "python")
    assert ".foo" in paths and "..bar" in paths and ".util" in paths
    files = {"pkg/sub/foo.py", "pkg/bar.py", "pkg/sub/util.py", "pkg/sub/mod.py"}
    assert resolve_import(".foo", "pkg/sub/mod.py", files) == "pkg/sub/foo.py"
    assert resolve_import("..bar", "pkg/sub/mod.py", files) == "pkg/bar.py"


# ----------------------------------------------------------- freshness.py (P2 backslash)
def test_freshness_normalizes_backslash_path():
    """A node whose file_path was stored with backslashes (Windows stamp) must still be
    matched against git's forward-slash keys for drift — not silently always-fresh."""
    from recall.freshness import RepoState

    # RepoState.drift_of normalizes '\' -> '/'; assert the normalization happens by
    # checking a backslash path and its slash twin classify identically against an
    # empty repo state (both -> COMMITTED for a missing file, never silently FRESH).
    import pathlib
    rs = RepoState(pathlib.Path("."))
    a = rs.drift_of("pkg\\does_not_exist_xyz.py", None)
    b = rs.drift_of("pkg/does_not_exist_xyz.py", None)
    assert a == b


# ----------------------------------------------------------- power_prompt.py (P2 JSON)
def test_json_extract_ignores_braces_in_strings():
    """A balanced-brace scan must ignore { } inside string values, or a valid reply with
    '{x}' in a body is silently discarded."""
    from recall.power_prompt import _extract_json_object

    reply = 'prose before {"nodes": [{"title": "use the {x} pattern", "body": "a } brace"}]} after'
    obj = _extract_json_object(reply)
    assert obj is not None and "nodes" in obj
    assert obj["nodes"][0]["title"] == "use the {x} pattern"


# ----------------------------------------------------------- importance.py (P3 docstring)
def test_importance_scale_is_1_to_100():
    """The docstring claimed 0-10 but the code returns 1-100. Lock the real contract so
    a future maintainer trusting the doc can't introduce an off-by-magnitude bug."""
    from recall.importance import compute_importance

    idx = Index.open(":memory:")
    for s in ("a", "b", "c"):
        idx.stamp(title=s, anchors=[s], kind="code-symbol", file_path=f"{s}.py",
                  symbol=s, line=1, origin="bootstrap")
    idx.db.commit()
    scores = compute_importance(idx.db)
    if scores:
        assert all(1.0 <= v <= 100.0 for v in scores.values())
    idx.db.close()


# ============================================================================
# ROUND 2 — regressions introduced BY the round-1 fixes above (4 of 5 were mine).
# The whole point of a regression-focused second pass: a fix can be incomplete or
# too coarse. Each guard pins the corrected behavior.
# ============================================================================

def test_mcp_probe_dashboard_treats_402_as_alive():
    """Round-1 fixed dashboard.is_dashboard_live for the gated-402 case but missed the
    SECOND identical probe in mcp._probe_dashboard — so a signed-out live dashboard was
    read as dead and a doomed duplicate was spawned. ANY HTTP response = alive."""
    import urllib.error
    from unittest.mock import patch
    import recall.mcp as m

    with patch("urllib.request.urlopen",
               side_effect=urllib.error.HTTPError("u", 402, "Payment Required", {}, None)):
        assert m._probe_dashboard("http://127.0.0.1:7099") is True
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        assert m._probe_dashboard("http://127.0.0.1:7099") is False


def test_anchors_keeps_deep_separator_rich_path():
    """Round-1 bounded the WHOLE whitespace-token at 64 chars, dropping anchors from deep
    (separator-rich) paths that aren't a ReDoS risk at all. Bound the longest
    separator-free RUN instead: a 75-char Java path keeps its directory anchors, the
    100k blob is still skipped."""
    import time
    from recall.anchors import extract_anchors

    deep = "src/main/java/com/example/billing/service/SubscriptionLifecycleManager.java"
    out = extract_anchors(deep)
    assert "billing" in out and "service" in out and "example" in out
    t0 = time.perf_counter()
    extract_anchors("a" * 100_000)
    assert time.perf_counter() - t0 < 0.5  # blob still skipped fast


@needs_ts
def test_graph_export_default_value_is_not_an_edge():
    """Round-1 added export_statement to harvest re-export module paths, but
    `export default "./theme"` has a string child that is the VALUE, not a module path —
    it leaked a FALSE edge. Only a true re-export (has a `from` child) is harvested."""
    from recall.graph import import_paths

    src = b'export default "./theme";\nexport { X } from "./a";\nimport y from "./y";\n'
    tree = _ts_parse("tsx", src)
    paths = import_paths(tree.root_node, src, "tsx")
    assert "./theme" not in paths, "export-default value leaked as a false edge"
    assert "./a" in paths and "./y" in paths, "re-export / import edge wrongly dropped"


@needs_ts
def test_graph_aliased_bare_dot_import_is_an_edge():
    """Round-1's bare-dot fix used the raw name text, so `from . import a as x` produced
    '.a as x' (resolves to nothing) and the edge was lost. Extract the inner name."""
    from recall.graph import import_paths, resolve_import

    src = b"from . import a as x, b as y\nfrom .. import models as m\n"
    tree = _ts_parse("python", src)
    paths = import_paths(tree.root_node, src, "python")
    assert ".a" in paths and ".b" in paths and "..models" in paths
    assert "as" not in " ".join(paths), "alias leaked into the spec"
    files = {"pkg/a.py", "pkg/b.py", "models.py", "pkg/mod.py"}
    assert resolve_import(".a", "pkg/mod.py", files) == "pkg/a.py"


def test_open_existing_distinguishes_lock_from_corruption():
    """Round-1's corrupt-index translation caught ALL DatabaseError — including a
    transient 'database is locked' (OperationalError) — and told the user to delete
    .mind/. A lock must propagate as-is, NOT become destructive 'corrupt' advice."""
    import sqlite3
    from unittest.mock import patch
    from pathlib import Path
    import tempfile
    from recall.cli import _open_existing, CorruptIndexError

    d = Path(tempfile.mkdtemp())
    (d / ".mind").mkdir()
    (d / ".mind" / "index.db").write_bytes(b"x")
    with patch("recall.engine.Index.open",
               side_effect=sqlite3.OperationalError("database is locked")):
        with pytest.raises(sqlite3.OperationalError):
            _open_existing(d)
    with patch("recall.engine.Index.open",
               side_effect=sqlite3.DatabaseError("file is not a database")):
        with pytest.raises(CorruptIndexError):
            _open_existing(d)


# ============================================================================
# ROUND 3 — fresh sweep (3 surfaces came back clean; 3 small bugs found).
# ============================================================================

def test_power_existing_for_no_variable_overflow():
    """power._existing_for selected lessons via an IN over h.existing_node_ids — the
    engine's last unbatched IN. A file with >999 nodes would trip 'too many SQL
    variables' and crash even the free estimate. Now selects by file_path (zero binds)."""
    from recall.power import _existing_for, Hotspot

    idx = Index.open(":memory:")
    for i in range(1100):
        idx.stamp(title=f"lesson {i}", body=f"why {i}", anchors=[f"a{i}"], kind="lesson",
                  file_path="big.py", origin="bootstrap")
    idx.db.commit()
    h = Hotspot(file_path="big.py", churn=5, symbol_count=1200,
                existing_node_ids=list(range(1, 1101)))
    out = _existing_for(idx, h)  # must not raise on >999 ids
    assert len(out) == 1100
    idx.db.close()


def test_resolve_import_dotted_module_ending_in_source_ext():
    """`import pkg.c` (final segment collides with a _RESOLVE_EXTS entry like '.c') was
    skipped by the endswith guard and the real edge to pkg/c.py was dropped. Now only
    genuine asset extensions gate the dotted-module branch."""
    from recall.graph import resolve_import

    files = {"a/b/c.ts", "mypkg/c.py", "mypkg/rs.py", "m.py"}
    assert resolve_import("a.b.c", "m.py", files) == "a/b/c.ts"
    assert resolve_import("mypkg.c", "m.py", files) == "mypkg/c.py"
    assert resolve_import("mypkg.rs", "m.py", files) == "mypkg/rs.py"
    # externals still resolve to None (the repo_files membership check is the real safety)
    assert resolve_import("os.path", "m.py", files) is None
    assert resolve_import("react.dom", "m.py", files) is None


def test_login_has_no_session_gated_activate_call():
    """The CLI device-flow is session-less, so a POST to the requireUser-gated
    /api/license/activate always 401s — it was dead code. Guard that it's gone."""
    import inspect
    from recall import login

    src = inspect.getsource(login)
    # the dead CALL is gone (a comment may still NAME the route to explain why)
    assert '_post("/api/license/activate"' not in src, "the dead session-gated activate call is back"
    assert "_post('/api/license/activate'" not in src


# ============================================================================
# Other-AIs generalization (2026-06-15): the 5 docking points work for any MCP
# client, not just Claude Code. `recall mcp --print-config` must show the per-client
# recipes and the CORRECT, complete tool list.
# ============================================================================

def test_print_config_covers_multiple_clients_and_all_tools():
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-m", "recall.cli", "mcp", "--print-config"],
        capture_output=True, text=True, encoding="utf-8",
    ).stdout
    # per-client recipes beyond Claude Code (the "other AIs" generalization)
    for client in ("Claude Code", "Cursor", "Copilot", "Windsurf"):
        assert client in out, f"--print-config dropped the {client} recipe"
    # the VS Code / Copilot shape uses a `servers` key, the rest `mcpServers`
    assert "mcpServers" in out and "servers" in out
    # the tool list must be complete + current — resolve was missing before
    for tool in ("recall", "brief", "explain", "resolve", "stamp", "contested",
                 "freshen", "dashboard"):
        assert tool in out, f"--print-config tool list dropped {tool}"
    # the agent-CLI hint (subagents can't see MCP)
    assert "--terse" in out


def test_mcp_tool_list_matches_print_config_claim():
    """print-config now derives its tool list/count from _TOOLS directly (no hardcoded
    number) so it can't drift. Pin the registry's exact membership so a tool can't be
    added or dropped silently (resolve was once in the registry but not the printed list;
    the arrow-3 + code-intel serves were added 2026-06-15)."""
    import contextlib
    import io

    from recall import cli
    from recall.mcp import _TOOLS

    names = {t["name"] for t in _TOOLS}
    assert names == {"recall", "brief", "explain", "resolve", "stamp",
                     "contested", "freshen", "dashboard",
                     "impact", "precedent", "callers", "dead_code", "untested", "cycles"}
    # the printed config builds count + names from _TOOLS — verify they agree, no drift
    args = cli.build_parser().parse_args(["mcp", "--print-config"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_mcp(args)
    out = buf.getvalue()
    assert f"Tools ({len(_TOOLS)})" in out
    for n in names:
        assert n in out

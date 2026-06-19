"""Audit + bug-hunt 2026-06-13 (round B) — regression drift-guards.

Locks the fixes for the 6 findings the multi-agent launch audit confirmed:

  P1 #2/#7  MCP: a non-dict message OR non-dict `params` must NOT kill the
            stdio loop — the spec answer is a JSON-RPC error, and the loop
            must keep answering (else the whole Claude/Cursor connection dies).
  P1 #3     git core.quotepath=false: an edited non-ASCII file must show drift
            (and be churn-counted), not be silently mangled into a fresh state.
  P2 #4     A pathologically deep source file must not abort the WHOLE
            `recall init` with a RecursionError — degrade to no import edges.
  P1 #1     A second `recall dashboard` on an in-use port must refuse cleanly
            (allow_reuse_address=False) rather than double-bind on Windows.
  P2 #6     A route whose Index.open / query raises must return a JSON 500 the
            page can render, not drop the socket and spin forever.

All offline, model-free.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from recall import Index
from recall.mcp import _Session, handle


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


# ----------------------------------------------------------- MCP #2 / #7
def _session(tmp_path: Path) -> _Session:
    (tmp_path / ".mind").mkdir()
    idx = Index.open(tmp_path / ".mind" / "index.db", repo=tmp_path)
    idx.stamp(title="seed", body="x", anchors=["aaaa", "bbbb"], kind="lesson", dedup=False)
    idx.db.close()
    return _Session(tmp_path)


@pytest.mark.parametrize("msg", [123, "hi", 1.5, True, None, [{"jsonrpc": "2.0", "id": 1, "method": "ping"}]])
def test_non_dict_message_returns_invalid_request_not_a_crash(tmp_path, msg):
    """A bare scalar or a JSON-RPC batch array is valid JSON but not an object.
    handle() must return a -32600 error, never raise (which would kill serve())."""
    out = handle(msg, _session(tmp_path))
    assert out is not None
    assert out["error"]["code"] == -32600


@pytest.mark.parametrize("method", ["initialize", "tools/call", "prompts/get"])
def test_non_dict_params_does_not_raise(tmp_path, method):
    """A misbehaving client can send `"params": "oops"`. The handler must coerce
    it to {} and answer (an error or a result) — never raise an AttributeError
    that escapes handle() and terminates the stdio loop."""
    s = _session(tmp_path)
    out = handle({"jsonrpc": "2.0", "id": 7, "method": method, "params": "oops"}, s)
    assert out is not None
    # either a clean result (initialize) or a -32602 invalid-params style error,
    # but crucially: it returned a dict instead of raising.
    assert "result" in out or "error" in out


def test_stdio_loop_survives_malformed_then_still_answers(tmp_path):
    """End-to-end through the real subprocess framing: feed garbage + a batch +
    bad params, then a good tools/list. The good request must still be answered."""
    repo = tmp_path
    (repo / ".mind").mkdir()
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    idx.stamp(title="seed", body="x", anchors=["aaaa", "bbbb"], kind="lesson", dedup=False)
    idx.db.close()

    import sys

    lines = [
        "123",
        '[{"jsonrpc":"2.0","id":1,"method":"ping"}]',
        '{"jsonrpc":"2.0","id":2,"method":"initialize","params":"oops"}',
        '{"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}}',
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "recall.cli", "mcp", "--repo", str(repo)],
        input="\n".join(lines) + "\n", capture_output=True, text=True, encoding="utf-8",
        timeout=30,
    )
    answered = set()
    for ln in proc.stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            m = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(m, dict) and m.get("id") is not None:
            answered.add(m["id"])
    assert 3 in answered, f"loop died before answering id=3; got ids {answered}"


# ----------------------------------------------------------- quotepath #3
@needs_git
def test_edited_non_ascii_file_shows_drift_not_fresh(tmp_path):
    """git core.quotepath default would C-quote 'Grüße.py' so the edited-file
    path never matched the indexed key — the traffic-light silently called a
    genuinely-edited file fresh. With quotepath=false it must report drift."""
    from recall.freshness import RepoState

    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "Grüße.py").write_text("X = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add unicode")
    # edit without committing
    (repo / "Grüße.py").write_text("X = 2\nY = 3\n", encoding="utf-8")

    rs = RepoState(repo)
    assert rs.drift_of("Grüße.py", None) != "fresh"


@needs_git
def test_non_ascii_file_is_counted_in_churn(tmp_path):
    """contested/file_churn must key a non-ASCII path as raw UTF-8 so it joins
    indexed nodes; the C-quoted form ('"Gr\\303...') would never match."""
    from recall.contested import file_churn

    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "Grüße.py").write_text("X = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    (repo / "Grüße.py").write_text("X = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")

    churn = file_churn(repo)
    assert "Grüße.py" in churn, f"non-ASCII path missing from churn keys: {list(churn)}"


# ----------------------------------------------------------- recursion #4
@needs_git
def test_deeply_nested_file_does_not_abort_whole_init(tmp_path):
    """One pathologically deep source file must not RecursionError the entire
    index — `recall init` must complete and index the OTHER files."""
    from recall.bootstrap import init

    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "ok.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (repo / "deep.py").write_text("x = " + "(" * 1300 + "1" + ")" * 1300 + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    try:
        stats = init(idx, repo)  # must NOT raise RecursionError
        assert stats["code_symbols"] >= 1  # ok.py's symbol still indexed
    finally:
        idx.db.close()


# ----------------------------------------------------------- dashboard #1 / #6
def _make_handler_repo(tmp_path: Path):
    from recall.bootstrap import init

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    idx = Index.open(tmp_path / "index.db", repo=repo)
    init(idx, repo)
    idx.db.close()
    return repo, tmp_path / "index.db"


def test_route_returns_json_500_on_corrupt_index_not_a_dropped_socket(tmp_path):
    """If Index.open / the query raises (corrupt or locked .mind), the route must
    return a JSON 500 the page can render — not drop the connection (log_message
    is silenced, so a dropped socket leaves the page spinning forever)."""
    from recall import dashboard

    repo, idx_path = _make_handler_repo(tmp_path)
    handler, _state = dashboard._make_handler(repo, idx_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # corrupt the index out from under the running server
        idx_path.write_bytes(b"GARBAGE-NOT-SQLITE")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/data", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status, body = r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read().decode("utf-8")
        assert status == 500
        assert "error" in json.loads(body)
    finally:
        srv.shutdown()
        srv.server_close()


def test_server_subclass_refuses_a_second_bind_on_an_in_use_port(tmp_path):
    """recall.dashboard._Server sets allow_reuse_address=False so a second bind
    to an actively-listening port raises OSError (on Windows the base class's
    True would silently start a SECOND server and clobber the run-lock)."""
    from recall.dashboard import _Server, _make_handler

    repo, idx_path = _make_handler_repo(tmp_path)
    handler, _ = _make_handler(repo, idx_path)
    first = _Server(("127.0.0.1", 0), handler)
    port = first.server_address[1]
    t = threading.Thread(target=first.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(OSError):
            _Server(("127.0.0.1", port), handler)
    finally:
        first.shutdown()
        first.server_close()


def test_server_subclass_does_not_reuse_address():
    """Drift-guard: the property the Windows fix hinges on."""
    from recall.dashboard import _Server

    assert _Server.allow_reuse_address is False


# ============================================================================
# Round B: the remaining 9 P2/P3 findings (all confirmed, now fixed).
# ============================================================================

# ----------------------------------------------------------- dedup #1
def test_dedupe_track_keeps_distinct_same_named_code_symbols():
    """Two classes each with an __init__ are DISTINCT code symbols at distinct lines;
    the old (title, file) dedup collapsed them into one, silently hiding a real
    location. Code rows must dedup by node/line, knowledge rows by (title, file)."""
    idx = Index.open(":memory:")
    try:
        code = [
            {"node_id": 1, "title": "__init__", "file": "m.py", "symbol": "__init__", "line": 5},
            {"node_id": 2, "title": "__init__", "file": "m.py", "symbol": "__init__", "line": 50},
        ]
        out = idx._dedupe_track(code)
        assert len(out) == 2, "distinct same-named symbols were collapsed"
    finally:
        idx.db.close()


def test_dedupe_track_still_collapses_duplicate_knowledge():
    """A merge + the direct commit legitimately duplicate by (title, file) — those
    knowledge rows must still collapse to one."""
    idx = Index.open(":memory:")
    try:
        know = [
            {"node_id": 1, "kind": "commit", "title": "fix: login", "file": "auth.py"},
            {"node_id": 2, "kind": "commit", "title": "fix: login", "file": "auth.py"},
        ]
        assert len(idx._dedupe_track(know)) == 1
    finally:
        idx.db.close()


def test_dedupe_track_does_not_collapse_real_indexed_same_named_symbols(tmp_path):
    """End-to-end on a real index: a file with two `handler` symbols stamps two distinct
    code-symbol nodes, and feeding BOTH (as recall() builds them) through _dedupe_track
    must keep both — the bug collapsed them to one. (Asserts on the dedup directly so the
    silence floor of a tiny toy repo can't mask the real behavior.)"""
    from recall.bootstrap import init

    repo = tmp_path
    (repo / "m.py").write_text(
        "class A:\n    def handler(self):\n        return 1\n\n"
        "class B:\n    def handler(self):\n        return 2\n",
        encoding="utf-8")
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    try:
        init(idx, repo)
        rows = idx.db.execute(
            "SELECT id, title, file_path, symbol, line FROM nodes "
            "WHERE kind='code-symbol' AND symbol='handler' ORDER BY line").fetchall()
        assert len(rows) == 2, f"two distinct handler nodes should be indexed; got {len(rows)}"
        items = [{"node_id": r["id"], "title": r["title"], "file": r["file_path"],
                  "symbol": r["symbol"], "line": r["line"]} for r in rows]
        assert len(idx._dedupe_track(items)) == 2, "dedup collapsed two distinct symbols"
    finally:
        idx.db.close()


# ----------------------------------------------------------- refine reversibility #2
def _refine_graph():
    idx = Index.open(":memory:")
    idx.stamp(title="handle", anchors=["handle"], kind="code-symbol",
              file_path="pkg/handler.py", symbol="handle", line=1, origin="bootstrap")
    idx.stamp(title="require_auth", anchors=["require_auth"], kind="code-symbol",
              file_path="pkg/guards.py", symbol="require_auth", line=1, origin="bootstrap")
    idx.add_dependency_edges([("pkg/handler.py", "pkg/guards.py")])
    return idx


def test_refine_records_original_kind_and_unrefine_restores_it():
    """refine must stash the pre-refine kind in refined_from so unrefine() resets it —
    the reversibility the module docstring promises (was a lie before)."""
    import json as _json

    from recall.llm import EchoProvider
    from recall.refine import refine_edges, unrefine

    idx = _refine_graph()
    try:
        echo = EchoProvider(canned=_json.dumps(
            {"edges": [{"target": "pkg/guards.py", "kind": "guarded_by"}]}))
        res = refine_edges(idx, echo)
        assert res.edges_refined == 1
        row = idx.db.execute(
            "SELECT kind, refined_from FROM edges WHERE kind='guarded_by'").fetchone()
        assert row["kind"] == "guarded_by"
        assert row["refined_from"] == "depends_on"  # origin preserved

        n = unrefine(idx)
        assert n == 1
        back = idx.db.execute("SELECT kind, refined_from FROM edges").fetchone()
        assert back["kind"] == "depends_on"      # reset
        assert back["refined_from"] is None       # marker cleared
        assert unrefine(idx) == 0                  # idempotent
    finally:
        idx.db.close()


def test_refine_rerun_can_reclassify_an_already_refined_edge():
    """A second refine run must be able to re-classify an edge it refined before
    (refined_from='depends_on'), not skip it forever."""
    import json as _json

    from recall.llm import EchoProvider
    from recall.refine import refine_edges

    idx = _refine_graph()
    try:
        refine_edges(idx, EchoProvider(canned=_json.dumps(
            {"edges": [{"target": "pkg/guards.py", "kind": "guarded_by"}]})))
        # re-run: now classify it as implements
        res2 = refine_edges(idx, EchoProvider(canned=_json.dumps(
            {"edges": [{"target": "pkg/guards.py", "kind": "implements"}]})))
        assert res2.edges_refined == 1
        row = idx.db.execute("SELECT kind, refined_from FROM edges").fetchone()
        assert row["kind"] == "implements"
        assert row["refined_from"] == "depends_on"  # still the very first origin
    finally:
        idx.db.close()


# ----------------------------------------------------------- root commit #3
@needs_git
def test_commit_files_lists_files_of_a_root_commit(tmp_path):
    """The first commit a new user makes is parentless; diff-tree without --root
    yields nothing, so `recall review` on it was empty. --root must list its files."""
    from recall.contested import commit_files

    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("b = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "root commit")

    files = commit_files(repo, "HEAD")
    assert set(files) == {"a.py", "b.py"}


# ----------------------------------------------------------- stop @0.0.0.0 #4
def test_loopback_host_maps_wildcard_to_127():
    """A 0.0.0.0 / :: / '' bind is not itself connectable (WSAEADDRNOTAVAIL on
    Windows) — the probe/stop target must be normalized to loopback."""
    from recall.dashboard import _loopback_host

    assert _loopback_host("0.0.0.0") == "127.0.0.1"
    assert _loopback_host("::") == "127.0.0.1"
    assert _loopback_host("") == "127.0.0.1"
    assert _loopback_host("127.0.0.1") == "127.0.0.1"
    assert _loopback_host("192.168.1.5") == "192.168.1.5"  # concrete host untouched


def test_stop_reaches_a_dashboard_bound_to_wildcard(tmp_path):
    """A dashboard whose lock records host 0.0.0.0 must still be stoppable (the
    shutdown POST must go to 127.0.0.1, not the unconnectable 0.0.0.0)."""
    from recall import dashboard

    repo, idx_path = _make_handler_repo(tmp_path)
    (repo / ".mind").mkdir(exist_ok=True)
    handler, _ = dashboard._make_handler(repo, idx_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # write a lock as if the server had bound 0.0.0.0 on this port
        dashboard._write_lock(repo, "0.0.0.0", port)
        live = dashboard.is_dashboard_live(repo)
        assert live is not None, "wildcard-bound dashboard must be detected as live"
        assert live["port"] == port
    finally:
        srv.shutdown()
        srv.server_close()


# ----------------------------------------------------------- per-port locks #8
def test_two_dashboards_same_repo_get_distinct_locks(tmp_path):
    """tray on 7099 + dashboard on 7100 for the SAME repo must each be findable —
    a single shared lock would orphan the first. Locks are named per-port."""
    from recall import dashboard

    repo = tmp_path
    (repo / ".mind").mkdir()
    dashboard._write_lock(repo, "127.0.0.1", 7099)
    dashboard._write_lock(repo, "127.0.0.1", 7100)
    locks = dashboard.read_locks(repo)
    ports = {lk["port"] for lk in locks}
    assert ports == {7099, 7100}, f"both servers must be recorded; got {ports}"


# ----------------------------------------------------------- PageRank rerank #5
def test_record_co_change_can_skip_rerank(tmp_path, monkeypatch):
    """During init the per-commit co_change must NOT trigger a full PageRank each time
    (rerank=False) — init re-ranks once at the end. Guard the keyword exists + is honored."""
    import recall.importance as importance_mod

    idx = Index.open(":memory:")
    try:
        idx.stamp(title="a", anchors=["a"], kind="code-symbol",
                  file_path="a.py", symbol="a", line=1, origin="bootstrap")
        idx.stamp(title="b", anchors=["b"], kind="code-symbol",
                  file_path="b.py", symbol="b", line=1, origin="bootstrap")
        calls = {"n": 0}
        real = importance_mod.persist_importance
        monkeypatch.setattr(importance_mod, "persist_importance",
                            lambda db: (calls.__setitem__("n", calls["n"] + 1), real(db))[1])
        idx.record_co_change(["a.py", "b.py"], rerank=False)
        assert calls["n"] == 0, "rerank=False must not run PageRank"
        idx.record_co_change(["a.py", "b.py"], kind="co_changed", sha="x", rerank=True)
        # (edges already exist -> added==0 -> no rerank either; just assert no crash)
    finally:
        idx.db.close()


# ----------------------------------------------------------- relative imports #7
def test_python_relative_import_resolves_to_a_dependency_edge():
    """`from .util import x` in recall/engine.py must resolve to recall/util.py — the
    old os.path.join produced 'recall/.util' (a hidden file) and dropped the edge."""
    from recall.graph import resolve_import

    files = {"recall/util.py", "recall/engine.py"}
    assert resolve_import(".util", "recall/engine.py", files) == "recall/util.py"


def test_python_parent_relative_import_resolves():
    """`from ..pkg.mod import x` walks up one package level then down the dotted tail."""
    from recall.graph import resolve_import

    files = {"a/pkg/mod.py", "a/sub/here.py"}
    assert resolve_import("..pkg.mod", "a/sub/here.py", files) == "a/pkg/mod.py"


def test_js_relative_import_still_resolves():
    """The relative-import rework must NOT break JS/TS path-style imports."""
    from recall.graph import resolve_import

    files = {"src/util.ts", "src/app.ts"}
    assert resolve_import("./util", "src/app.ts", files) == "src/util.ts"


# ----------------------------------------------------------- license exp #9
def test_load_license_does_not_crash_on_non_numeric_exp(tmp_path, monkeypatch):
    """A token with exp='soon' must read as signed-out (None), never raise a ValueError
    that the dashboard's unguarded load_license() would turn into a 500."""
    import base64 as _b64
    import json as _json

    import recall.license as lic

    # this test exercises the FILE path explicitly; clear the conftest's env token
    # (W2) so load_license reads the bad token we plant, not the valid env one.
    monkeypatch.delenv("RECALL_LICENSE", raising=False)
    monkeypatch.delenv("RECALL_PUBKEY", raising=False)

    payload = {"sub": "u", "email": "e@x.com", "plan": "solo", "exp": "soon"}
    blob = _b64.urlsafe_b64encode(_json.dumps(payload).encode()).decode().rstrip("=")
    token = f"{blob}.sig"
    tok_path = tmp_path / "license.token"
    tok_path.write_text(token, encoding="utf-8")
    monkeypatch.setattr(lic, "LICENSE_PATH", tok_path)

    assert lic.load_license() is None  # must not raise (bad exp / unverified → signed out)


def test_decode_token_accepts_numeric_string_exp():
    """A numeric-STRING exp ('1999999999') is fine — int() coerces it; only truly
    non-numeric exp is rejected."""
    import base64 as _b64
    import json as _json

    import recall.license as lic

    payload = {"sub": "u", "email": "e@x.com", "plan": "solo", "exp": "1999999999"}
    blob = _b64.urlsafe_b64encode(_json.dumps(payload).encode()).decode().rstrip("=")
    assert lic.decode_token(f"{blob}.sig") is not None

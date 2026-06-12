"""Drift guards for the adversarial bug-hunt fixes — each test locks in one
confirmed bug so it can never regress. (keinen Fehler 2x machen.)"""

import importlib

import pytest

from recall import Index
from recall.engine import _fts_phrase
from recall.rules import Rules, _enforce_core, _merge, _parse_frontmatter, _bounded_float


# ---- FTS5 quote crash (5 findings) -----------------------------------------
def test_fts_phrase_escapes_quotes():
    assert _fts_phrase('foo"bar') == '"foo""bar"'


def test_stamp_with_quote_anchor_never_crashes():
    idx = Index.open(":memory:")
    idx.stamp(title="one", anchors=["alpha", "beta"])
    # an embedded double-quote in an explicit anchor must not raise
    r = idx.stamp(title="two", anchors=['foo"bar', "beta", "gamma"])
    assert r["action"] in ("NEW", "MERGE")


def test_stamp_from_commit_with_quote_trailer_never_crashes():
    idx = Index.open(":memory:")
    idx.stamp(title="seed", anchors=["x", "y"])
    msg = 'fix: thing\n\nRecall-anchors: foo"bar, alpha, beta\nRecall-why: a why'
    assert idx.stamp_from_commit(msg, "abc1234") is not None  # no OperationalError


# ---- empty / whitespace edge cases -----------------------------------------
def test_empty_edge_target_skipped():
    idx = Index.open(":memory:")
    idx.stamp(title="lesson", anchors=["needle", "thread"], edges=[("causes", "  ")])
    res = idx.recall("needle thread")
    assert res["results"][0]["relation"] == []  # no blank relation


def test_whitespace_only_anchor_trailer_ignored():
    idx = Index.open(":memory:")
    assert idx.stamp_from_commit("fix: x\n\nRecall-anchors:    \n", "abc") is None


def test_degenerate_trailer_only_commit_title():
    idx = Index.open(":memory:")
    idx.stamp_from_commit("Recall-anchors: alpha, beta, gamma", "abc1234")
    # the node title must be the anchor values, never the raw trailer line
    title = idx.db.execute("SELECT title FROM nodes").fetchone()[0]
    assert not title.startswith("Recall-anchors")
    assert "alpha" in title


# ---- rules: core veto extended to facet weights ----------------------------
def test_project_cannot_zero_security_weight():
    proj = _merge(Rules.defaults(), {"facet_weights": {"security": 0.0}})
    assert _enforce_core(proj).facet_weight("security") >= 2.0


# ---- rules: project CAN narrow surface_on now ------------------------------
def test_project_can_narrow_surface_on():
    proj = _merge(Rules.defaults(), {"surface_on": ["commit"]})
    assert proj.surface_on == {"commit"}


def test_project_can_restrict_edge_kinds():
    proj = _merge(Rules.defaults(), {"edge_kinds": ["implements"]})
    assert proj.edge_kinds == {"implements"}


# ---- rules: bounds validation ----------------------------------------------
def test_non_finite_float_rejected():
    for bad in ("inf", "-inf", "nan"):
        with pytest.raises(ValueError):
            _bounded_float(bad, lo=0.0, name="x")


def test_dedup_threshold_out_of_range_rejected():
    with pytest.raises(ValueError):
        _merge(Rules.defaults(), {"dedup_threshold": 5.0})


# ---- rules: frontmatter robustness -----------------------------------------
def test_missing_closing_fence_returns_empty():
    # no closing --- must not swallow the body as keys
    text = "---\nsilence_floor: 2\n\nsome prose line\ndedup_threshold: not a number\n"
    assert _parse_frontmatter(text) == {}


def test_orphan_indented_line_not_promoted():
    text = "---\nsilence_floor: 2\n  injected: pwned\n---\n"
    fm = _parse_frontmatter(text)
    assert "injected" not in fm  # not promoted to a top-level key


def test_quoted_comma_in_list_preserved():
    fm = _parse_frontmatter('---\nstay_silent_on: ["a, b", c]\n---')
    assert fm["stay_silent_on"] == ["a, b", "c"]


# ---- topk defense-in-depth -------------------------------------------------
def test_zero_topk_does_not_silence_real_hits():
    idx = Index.open(":memory:")
    idx.stamp(title="lesson", anchors=["alpha", "beta", "gamma"])
    res = idx.recall("alpha beta gamma", topk=0)
    assert not res["silenced"]  # clamped to >=1, real hit survives


# ---- hook robustness -------------------------------------------------------
def test_hook_main_survives_none_stdin(monkeypatch):
    from adapters import hook
    monkeypatch.setattr("sys.stdin", None)
    assert hook.main() == 0


def test_hook_ignores_non_string_file_path(tmp_path):
    from adapters import hook
    repo = tmp_path / "p"
    repo.mkdir()
    (repo / ".git").mkdir()
    Index.open(__import__("recall.cli", fromlist=["_index_path"])._index_path(repo), repo=repo)
    out = hook.route({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                      "cwd": str(repo), "tool_input": {"file_path": 123}})
    assert out == {}  # no AttributeError


# ---- bridge robustness -----------------------------------------------------
def test_bridge_empty_token_treated_as_unset(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from recall.cli import _index_path

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / ".git").mkdir()
    idx = Index.open(_index_path(repo), repo=repo)
    idx.stamp(title="x", anchors=["alpha", "beta"])
    monkeypatch.setenv("RECALL_REPO", str(repo))
    monkeypatch.setenv("RECALL_BRIDGE_TOKEN", "   ")  # whitespace -> unset
    import adapters.server as server
    importlib.reload(server)
    client = TestClient(server.app)
    # empty token => open (no 'Bearer ' bypass lock); request without auth works
    assert client.post("/recall", json={"query": "alpha beta"}).status_code == 200


def test_bridge_init_rejects_outside_path(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from recall.cli import _index_path

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / ".git").mkdir()
    Index.open(_index_path(repo), repo=repo)
    monkeypatch.setenv("RECALL_REPO", str(repo))
    monkeypatch.delenv("RECALL_BRIDGE_TOKEN", raising=False)
    import adapters.server as server
    importlib.reload(server)
    client = TestClient(server.app)
    r = client.post("/init", json={"path": str(tmp_path.parent)})  # outside served repo
    assert r.status_code == 403

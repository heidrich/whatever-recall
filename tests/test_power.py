"""Drift-guards for STEP 6 — end-to-end Power-Mode orchestration (offline, ADR-008).

run_power against the EchoProvider (zero network): proves a run creates origin='power'
nodes/edges/synonyms tagged to its run number, writes the meta record (estimate +
actual + synonym ledger), and — the whole point of STEP 2+6 together — that undo
round-trips the entire run cleanly, leaving bootstrap/live untouched.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from recall import Index
from recall.llm import EchoProvider
from recall.power import run_power

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "auth.py").write_text("def login(): return 1\n", encoding="utf-8")
    (repo / "src" / "pay.py").write_text("def charge(): return 2\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    return repo


def _bootstrapped_index(repo) -> Index:
    idx = Index.open(":memory:", repo=str(repo))
    idx.stamp("login", anchors=["login"], kind="code-symbol",
              file_path="src/auth.py", origin="bootstrap")
    idx.stamp("charge", anchors=["charge"], kind="code-symbol",
              file_path="src/pay.py", origin="bootstrap")
    return idx


def _reply(title, anchors, tags=None, edges=None):
    node = {"title": title, "why": f"why {title}", "anchors": anchors}
    if tags:
        node["tags"] = tags
    if edges:
        node["edges"] = edges
    return json.dumps({"nodes": [node]})


@needs_git
def test_run_power_creates_tagged_power_nodes(tmp_path):
    repo = _repo(tmp_path)
    idx = _bootstrapped_index(repo)
    nodes_before = idx.stats()["nodes"]
    echo = EchoProvider(model="echo", responses=[
        _reply("auth insight", ["authentication", "session"], tags=["security"]),
        _reply("payment insight", ["billing", "stripe"], tags=["backend"]),
    ])

    res = run_power(idx, repo, provider=echo)

    assert res.run == 1
    assert res.nodes_added == 2
    # both new nodes are origin='power' tagged to run 1
    power_nodes = idx.db.execute(
        "SELECT title, power_run, base_sha FROM nodes WHERE origin='power' ORDER BY title"
    ).fetchall()
    assert [r[0] for r in power_nodes] == ["auth insight", "payment insight"]
    assert all(r[1] == 1 for r in power_nodes)  # power_run tag
    assert all(r[2] for r in power_nodes)  # base_sha pinned
    assert idx.stats()["nodes"] == nodes_before + 2


@needs_git
def test_run_power_reports_progress(tmp_path):
    """The dashboard's live progress bar needs run_power to call progress(done, total):
    once at the start (0, total) and once per hotspot. A raising callback must not break
    the run (the dashboard wraps it, but run_power guards it too)."""
    repo = _repo(tmp_path)
    idx = _bootstrapped_index(repo)
    echo = EchoProvider(model="echo", responses=[_reply("x", ["xx"]), _reply("y", ["yy"])])
    seen = []
    run_power(idx, repo, provider=echo, progress=lambda d, t: seen.append((d, t)))
    assert seen, "progress was never called"
    total = seen[0][1]
    assert seen[0][0] == 0  # first tick is (0, total)
    assert seen[-1][0] == total  # last tick reaches total
    assert all(t == total for _, t in seen)  # total is stable

    # a callback that raises must NOT break the run
    idx2 = _bootstrapped_index(_repo(tmp_path / "b"))
    echo2 = EchoProvider(model="echo", responses=[_reply("z", ["zz"])])
    def _boom(d, t):
        raise RuntimeError("ui blew up")
    res = run_power(idx2, repo, provider=echo2, progress=_boom)  # must not raise
    assert res.run >= 1


@needs_git
def test_run_power_writes_meta_record(tmp_path):
    repo = _repo(tmp_path)
    idx = _bootstrapped_index(repo)
    echo = EchoProvider(model="claude-opus-4-8", responses=[
        _reply("a", ["alpha"]), _reply("b", ["beta"]),
    ])
    run_power(idx, repo, provider=echo)

    info = idx.power_run_info(1)
    assert info is not None
    assert info["status"] == "done"
    assert info["model"] == "claude-opus-4-8"
    assert info["nodes_added"] == 2
    assert info["est_input_tokens"] > 0
    assert "added_anchors" in info  # the synonym ledger key exists (empty here)


@needs_git
def test_run_power_then_undo_round_trips(tmp_path):
    repo = _repo(tmp_path)
    idx = _bootstrapped_index(repo)
    bootstrap_nodes = idx.stats()["nodes"]
    echo = EchoProvider(model="echo", responses=[
        _reply("auth insight", ["authentication"], edges=[{"kind": "implements", "target": "login"}]),
        _reply("pay insight", ["billing"]),
    ])
    run_power(idx, repo, provider=echo)
    assert idx.stats()["nodes"] > bootstrap_nodes

    undo = idx.undo_power_run(1)

    assert undo["nodes_removed"] == 2
    # exactly back to the bootstrap baseline — nothing of origin='power' survives
    assert idx.db.execute("SELECT COUNT(*) FROM nodes WHERE origin='power'").fetchone()[0] == 0
    assert idx.stats()["nodes"] == bootstrap_nodes
    # bootstrap code-symbols untouched
    assert idx.db.execute(
        "SELECT COUNT(*) FROM nodes WHERE origin='bootstrap'"
    ).fetchone()[0] == 2


@needs_git
def test_run_power_records_synonyms_for_undo(tmp_path):
    """The real Power-Mode case: the model proposes a node that merges onto an EXISTING
    bootstrap lesson (high anchor overlap), grafting a new synonym. That synonym can't
    CASCADE on undo, so the run ledger must capture it. Then undo removes precisely it,
    leaving the bootstrap lesson's original anchors intact."""
    repo = _repo(tmp_path)
    idx = Index.open(":memory:", repo=str(repo))
    idx.stamp("login", anchors=["login"], kind="code-symbol",
              file_path="src/auth.py", origin="bootstrap")
    # an existing bootstrap LESSON about auth.py — the power node will merge onto it
    boot = idx.stamp("auth flow", anchors=["authentication", "session", "login"],
                     kind="lesson", file_path="src/auth.py", origin="bootstrap")

    # the model's reply has heavy overlap (auth/session/login) + ONE new synonym
    echo = EchoProvider(model="echo", responses=[
        _reply("auth flow enriched", ["authentication", "session", "login", "credentials"]),
    ])
    res = run_power(idx, repo, provider=echo, scope="src/auth", top_n=1)

    assert res.synonyms_added >= 1  # 'credentials' grafted onto the bootstrap node
    info = idx.power_run_info(1)
    assert str(boot["node_id"]) in info["added_anchors"]
    assert "credentials" in info["added_anchors"][str(boot["node_id"])]

    # undo removes exactly the synonym; the bootstrap lesson + its originals survive
    idx.undo_power_run(1)
    surviving = {
        r[0] for r in idx.db.execute(
            "SELECT DISTINCT term FROM fts_anchors WHERE node_id=?", (boot["node_id"],)
        ).fetchall()
    }
    assert surviving == {"authentication", "session", "login"}  # credentials gone, rest kept
    assert idx.db.execute(
        "SELECT origin FROM nodes WHERE id=?", (boot["node_id"],)
    ).fetchone()[0] == "bootstrap"


@needs_git
def test_dry_run_leaves_no_meta_record(tmp_path):
    repo = _repo(tmp_path)
    idx = _bootstrapped_index(repo)
    echo = EchoProvider(model="echo", responses=[_reply("a", ["alpha"]), _reply("b", ["beta"])])

    res = run_power(idx, repo, provider=echo, dry_run=True)

    assert res.dry_run is True
    # a dry run on a throwaway index still stamps in-memory, but writes NO meta record
    assert idx.power_run_info(1) is None


@needs_git
def test_run_power_re_validates_hallucinated_vocab(tmp_path):
    """The model proposing a junk tag/edge must not poison the index — power.py runs
    every reply through parse_stamp_instructions (STEP 4) before stamping."""
    repo = _repo(tmp_path)
    idx = _bootstrapped_index(repo)
    echo = EchoProvider(model="echo", responses=[
        _reply("x", ["xx"], tags=["security", "blockchain"], edges=[{"kind": "teleports", "target": "y"}]),
        _reply("z", ["zz"]),
    ])
    res = run_power(idx, repo, provider=echo)

    assert res.dropped_tags >= 1  # 'blockchain' dropped
    assert res.dropped_edges >= 1  # 'teleports' dropped
    # the stamped node carries only the valid tag
    facets = idx.db.execute(
        "SELECT facets FROM nodes WHERE title='x'"
    ).fetchone()[0]
    assert "security" in facets and "blockchain" not in facets


@needs_git
def test_run_power_counts_schema_mismatch_replies_loudly(tmp_path):
    """THE dogfood bug as a run-level guard: a provider that answers valid JSON with the
    WRONG schema ({file, role, facts}) must NOT silently stamp 0 and look fine. run_power
    has to count the failed reply in responses_discarded so the CLI can warn the owner.
    Two hotspots: one good reply, one schema-mismatch reply -> exactly 1 node, 1 discarded."""
    repo = _repo(tmp_path)
    idx = _bootstrapped_index(repo)
    echo = EchoProvider(model="echo", responses=[
        _reply("good node", ["alpha"], tags=["security"]),
        json.dumps({"file": "pay.py", "role": "charge handler", "facts": ["takes money"]}),
    ])

    res = run_power(idx, repo, provider=echo)

    assert res.nodes_added == 1            # only the well-formed reply stamped
    assert res.responses_discarded == 1    # the schema-mismatch reply is COUNTED, not silent
    assert res.alt_keys_seen == {}         # {file, role, facts} is not even a near-miss key


@needs_git
def test_run_power_forgives_near_miss_top_level_key(tmp_path):
    """A provider that writes 'lessons' instead of 'nodes' still lands (bounded tolerance),
    and the off-schema key is recorded in alt_keys_seen — forgiven AND visible."""
    repo = _repo(tmp_path)
    idx = _bootstrapped_index(repo)
    echo = EchoProvider(model="echo", responses=[
        json.dumps({"lessons": [{"title": "n1", "why": "w", "anchors": ["a1"]}]}),
        json.dumps({"lessons": [{"title": "n2", "why": "w", "anchors": ["a2"]}]}),
    ])

    res = run_power(idx, repo, provider=echo)

    assert res.nodes_added == 2
    assert res.responses_discarded == 0
    assert res.alt_keys_seen == {"lessons": 2}

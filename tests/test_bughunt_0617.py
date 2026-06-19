"""Drift-guards for the 2026-06-17 Mac bug-hunt fixes (engine / login / rules / power).

Each test pins the invariant a fix established so a future edit can't silently re-open it.
Web-side fixes (orgs.ts authz, llms.txt gate, stripe rotation, weekly→hourly copy) are
covered by tsc+lint+build and the static guards in test_w2_trial_key_enforcement.py /
test_web_security_audit_0614.py; MCP prompts/get + conftest device_id have behavioral
guards in test_mcp.py / the conftest itself. This file covers the Python engine seam.
"""
from __future__ import annotations

import time

import pytest

from recall import Index


# ---- engine: relation walk is cycle-safe (UNION, not UNION ALL) -------------
def _clique_index(n: int = 6) -> Index:
    """An index whose n code-symbols all co_changed together — a fully-connected
    bidirectional clique, the shape that made the old UNION ALL walk blow up."""
    idx = Index.open(":memory:")
    files = [f"pkg/f{i}.py" for i in range(n)]
    for i, f in enumerate(files):
        idx.stamp(title=f, anchors=[f"sym{i}"], kind="code-symbol",
                  file_path=f, symbol=f"sym{i}", line=1, origin="bootstrap")
    idx.record_co_change(files)  # inserts both directions for every pair → clique
    return idx


def test_relation_walk_terminates_on_a_co_changed_clique():
    """The Level-3 relation walk must TERMINATE quickly on a fully-connected
    bidirectional clique — the shape that made the old UNION ALL walk enumerate every
    path. UNION dedups the per-hop rows so it can't multiply within a level (the hop<3
    bound already caps depth). Output equivalence to the bounded UNION-ALL walk was
    verified byte-for-byte on the real index; here we lock termination + a sane result."""
    idx = _clique_index(8)
    node_id = idx.db.execute(
        "SELECT id FROM nodes WHERE file_path='pkg/f0.py' LIMIT 1"
    ).fetchone()[0]
    t0 = time.perf_counter()
    level = idx._build_levels(node_id, hits=1, score=1.0, toks=set())
    dt = time.perf_counter() - t0
    assert level is not None
    assert dt < 0.5, f"relation walk too slow ({dt*1000:.0f}ms) — cycle not terminated?"
    # every other file in the clique is reachable (the walk still surfaces the relations)
    targets = {r["target"] for r in level["relation"]}
    for i in range(1, 8):
        assert f"pkg/f{i}.py" in targets, f"f{i} missing — the walk lost a real relation"


def test_relation_walk_query_uses_union_not_union_all():
    """Source guard: the read-path relation CTE must use UNION (cycle-terminating),
    never UNION ALL (which enumerated every path through a clique)."""
    import inspect

    src = inspect.getsource(Index._build_levels)
    # the recursive relation CTE — find the WITH RECURSIVE walk(...) block
    assert "WITH RECURSIVE walk" in src
    walk = src.split("WITH RECURSIVE walk")[1].split('"""')[0]
    assert "UNION ALL" not in walk, "the relation walk is back on UNION ALL — cycles re-explode"
    assert "UNION" in walk


# ---- login: clock-rollback can't defeat the seat-check throttle -------------
def test_future_check_stamp_is_due_now(tmp_path, monkeypatch):
    """A seat-check stamp in the FUTURE (clock set forward then back) must read as due,
    not as 'never due' — else online enforcement is disabled until token crypto-exp."""
    import recall.login as login

    monkeypatch.setattr(login, "_SEAT_CHECK_PATH", tmp_path / "seat_check.ts")
    # stamp one hour in the FUTURE
    (tmp_path / "seat_check.ts").write_text(str(int(time.time()) + 3600), encoding="utf-8")
    assert login._seat_check_due() is True, "a future check stamp must be treated as due now"


def test_future_grace_start_reads_as_expired(tmp_path, monkeypatch):
    """A grace that 'started' in the future (clock rolled back) must read as 0 seconds
    left, not a fresh full hour — else the same rollback tops the grace up forever."""
    import recall.login as login

    monkeypatch.setattr(login, "_SEAT_GRACE_PATH", tmp_path / "seat_grace.ts")
    (tmp_path / "seat_grace.ts").write_text(str(int(time.time()) + 3600), encoding="utf-8")
    assert login.seat_grace_seconds_left() == 0


# ---- rules: the security tag can't be dropped from the closed vocabulary ----
def test_project_rules_cannot_drop_the_security_tag(tmp_path):
    """A project rules.md that REPLACES allowed_tags without 'security' must NOT be able
    to strip the security tag — _enforce_core forces core facet names back in, so a
    hostile/careless layer can't silence security lessons via the vocabulary knob."""
    from recall.rules import load_rules

    recall_dir = tmp_path / ".recall"
    recall_dir.mkdir()
    (recall_dir / "rules.md").write_text(
        "---\nallowed_tags: [feature, bugfix, docs]\n---\n", encoding="utf-8"
    )
    rules = load_rules(tmp_path)
    assert "security" in rules.allowed_tags, "a project layer dropped the security tag"
    # and the weight floor still holds
    assert rules.facet_weight("security") >= 2.0


# ---- power: the cost preview prices the REAL ceiling (true upper bound) ------
def test_power_estimate_prices_the_send_ceiling():
    """The previewed output budget must equal the run's real max_tokens
    (output_budget × OUTPUT_CAP_MULTIPLIER), so --yes approves a true upper bound."""
    from recall import power

    assert power.OUTPUT_CAP_MULTIPLIER >= 2
    # the estimate output = hotspots × budget × cap; pin the relationship via source so a
    # future edit can't drop the cap from one side and leave the other (the 2x understatement)
    import inspect

    est_src = inspect.getsource(power.estimate_tokens)
    run_src = inspect.getsource(power.run_power)
    assert "output_budget * OUTPUT_CAP_MULTIPLIER" in est_src, "estimate dropped the cap multiplier"
    assert "output_budget * OUTPUT_CAP_MULTIPLIER" in run_src, "the send dropped the cap multiplier"


def test_power_run_reuses_the_estimated_prompts():
    """TOCTOU: run_power must reuse estimate.prompts rather than re-reading the file, so
    the bytes sent match the bytes priced even if the file changed after the preview."""
    import inspect

    from recall import power

    run_src = inspect.getsource(power.run_power)
    assert "estimate.prompts[" in run_src, "run_power re-reads the file instead of reusing the priced prompt"

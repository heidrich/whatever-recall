"""Causal importance of a code node — 'if this breaks, how much breaks with it?'

This is the Owner's ranking idea (ADR-016): every code symbol earns a weight 0-100
INSIDE its causal chain (granular: a 100 = break it and the chain collapses; a leaf
nothing depends on sits near 1). CORE systems are high by nature. The recall read-path
then
splits a code-track (ranked by this) from a knowledge-track (ranked by text relevance),
so a noisy commit can never bury the central code symbol a query is really about.

Computed from the dependency graph alone — PageRank over depends_on / co_changed /
implements / guarded_by edges. Pure arithmetic: no model, deterministic, offline. This
keeps the read-path LLM-free (ADR-014) and makes importance a free by-product of the
graph we already build at write-time.

Why PageRank, not a raw in-degree count: the Owner's own words are 'important when
IMPORTANT nodes depend on me' — that is recursive. A util called by 50 throwaway tests
should NOT outrank the engine that only 3 *core* places need. PageRank captures CORE
systems naturally, without anyone hand-marking paths as core.
"""
from __future__ import annotations

import sqlite3

# Edge kinds that mean 'A leans on B' for importance. co_changed is symmetric and counts
# both ways (we store both directions), so a tight co-evolving pair lifts each other.
_DEP_KINDS = ("depends_on", "implements", "guarded_by", "co_changed", "relates_to")

_DAMPING = 0.85          # classic PageRank teleport factor
_ITERATIONS = 40         # plenty for convergence on code-graph sizes
_MAX_SCORE = 100.0       # the Owner's 1-100 dial — granular (0 = no graph signal)

# Feedback nudge (ADR-016): the graph LEADS, feedback only adjusts within +/-_FEEDBACK_SPAN.
# So a single stray click can never push a pillar down to 1, but consistent 'this hit was
# useful' steadily lifts a node and 'surfaced-but-never-used' gently lowers it. Bounded,
# deterministic, model-free — the read-path stays LLM-free (ADR-014).
_FEEDBACK_SPAN = 0.20    # +/-20% of the node's graph importance
_FEEDBACK_SATURATE = 5   # this many net-useful signals reaches the full +20%


def compute_importance(db: sqlite3.Connection) -> dict[int, float]:
    """Return {node_id: importance 1-100} for every code-symbol node.

    Importance flows ALONG dependency edges toward what is depended-upon: if A depends
    on B, B accrues importance from A (B is load-bearing for A). We therefore run
    PageRank on the REVERSED graph (rank flows from dependent -> dependency).
    """
    # Only code symbols carry importance; commits/lessons live in the knowledge-track.
    code_ids = [r[0] for r in db.execute(
        "SELECT id FROM nodes WHERE kind='code-symbol'").fetchall()]
    if not code_ids:
        return {}
    code = set(code_ids)
    n = len(code_ids)

    # Build reversed adjacency: edge A --depends_on--> B becomes B <- A, so rank flows
    # from the dependent (A) into its dependency (B). Restrict to code<->code edges.
    placeholders = ",".join("?" for _ in _DEP_KINDS)
    out: dict[int, list[int]] = {i: [] for i in code_ids}   # dependent -> [dependencies]
    for src, dst in db.execute(
        f"SELECT src_node, dst_node FROM edges WHERE kind IN ({placeholders})",
        _DEP_KINDS,
    ).fetchall():
        if src in code and dst in code and src != dst:
            out[src].append(dst)

    # Standard PageRank with dangling-node redistribution.
    rank = {i: 1.0 / n for i in code_ids}
    base = (1.0 - _DAMPING) / n
    for _ in range(_ITERATIONS):
        dangling = sum(rank[i] for i in code_ids if not out[i])
        nxt = {i: base + _DAMPING * dangling / n for i in code_ids}
        for i in code_ids:
            outs = out[i]
            if outs:
                share = _DAMPING * rank[i] / len(outs)
                for j in outs:
                    nxt[j] += share
        rank = nxt

    return _to_scale(rank)


def _to_scale(rank: dict[int, float]) -> dict[int, float]:
    """Map raw PageRank to the Owner's 1-100 dial (granular).

    A flat graph (every node equal, e.g. no edges at all) must NOT all read as 100 — that
    would be a meaningless badge. We scale by the SPREAD above the floor: the least-
    connected node sits near 1, the most-connected near 100, and a graph with no signal
    collapses to a low constant. Uses the min..max range of raw ranks.
    """
    if not rank:
        return {}
    vals = list(rank.values())
    lo, hi = min(vals), max(vals)
    span = hi - lo
    if span <= 1e-12:
        # No differentiation in the graph (no edges / fully symmetric). Everyone is a
        # leaf: a low, honest constant (1) rather than a misleading top score.
        return {i: 1.0 for i in rank}
    out: dict[int, float] = {}
    for i, v in rank.items():
        # 1..100: floor at 1 so a connected-but-low node still reads as 'on the graph'.
        out[i] = round(1.0 + (v - lo) / span * (_MAX_SCORE - 1.0), 1)
    return out


def _feedback_factor(db: sqlite3.Connection) -> dict[int, float]:
    """{node_id: multiplier in [1-span, 1+span]} from the deterministic feedback counter.

    net = useful - missed; factor = 1 + span * clamp(net / saturate, -1, +1). A node with
    no feedback gets 1.0 (no change). Pure counting — no model."""
    out: dict[int, float] = {}
    for nid, useful, missed in db.execute(
        "SELECT node_id, useful_count, missed_count FROM node_feedback"
    ).fetchall():
        net = (useful or 0) - (missed or 0)
        frac = max(-1.0, min(1.0, net / _FEEDBACK_SATURATE))
        out[nid] = 1.0 + _FEEDBACK_SPAN * frac
    return out


def persist_importance(db: sqlite3.Connection, scores: dict[int, float] | None = None) -> int:
    """Compute (if not given) and write importance onto the code nodes. Returns rows set.

    Importance = graph PageRank (the backbone) * feedback factor (the gentle learner).
    Idempotent: recomputes from the current graph + current feedback, so calling it after
    new edges OR new feedback land simply refreshes the dial. Model-free; safe on the
    read-path's write-side (bootstrap / incremental update / session heal / feedback)."""
    if scores is None:
        scores = compute_importance(db)
    if not scores:
        return 0
    fb = _feedback_factor(db)
    rows = []
    for i, s in scores.items():
        adjusted = round(min(_MAX_SCORE, s * fb.get(i, 1.0)), 1)
        rows.append((adjusted, i))
    db.executemany("UPDATE nodes SET importance=? WHERE id=?", rows)
    db.commit()
    return len(scores)

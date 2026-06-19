"""Arrow 3 — precedent (the `recall` arrow, core → AI): given a SITUATION the AI is about
to act in, serve the most ANALOGOUS past decisions/lessons, each tagged with its OUTCOME, so
the AI generalizes from THIS repo's lived experience instead of its priors.

What sets precedent() apart from recall()'s knowledge track:
  - it is SCOPED to the deliberate record (kind decision/lesson — not code/commits/tasks), and
  - it attaches the OUTCOME (superseded_by → the rule that governs now; became_landmine;
    drift) — the part that turns a search hit into a precedent.
A SUPERSEDED precedent is KEPT, not dropped (the opposite of a landmine): "we tried X and
reversed it" is exactly the lesson. These tests pin all of that.
"""

import os
import tempfile

from recall import Index
from recall.cli import _format_precedent_for_prompt


def _idx():
    """In-memory index over a throwaway repo (no git needed — precedent is pure graph)."""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "auth.py"), "w", encoding="utf-8") as f:
        f.write("def login(u):\n    return u\n")
    return Index.open(":memory:", repo=d)


def _decide(ix, title, anchors, *, kind="decision", body=None, file_path=None,
            importance=None, edges=None):
    """Stamp a deliberate node with controlled identity (dedup off so tests own the graph)."""
    r = ix.stamp(title=title, anchors=anchors, kind=kind, body=body,
                 file_path=file_path, edges=edges, dedup=False)
    nid = r["node_id"]
    if importance is not None:
        ix.db.execute("UPDATE nodes SET importance=? WHERE id=?", (importance, nid))
        ix.db.commit()
    return nid


def _supersede(ix, older_id, newer_id):
    """Wire (newer) -supersedes-> (older) directly, as the engine reads it."""
    ix.db.execute("INSERT INTO edges(src_node,dst_node,kind,stamped_at_sha) VALUES(?,?,?,?)",
                  (newer_id, older_id, "supersedes", "x"))
    ix.db.commit()


def test_precedent_serves_an_analogous_decision():
    ix = _idx()
    _decide(ix, "chose JWT over server sessions for the mobile clients",
            ["jwt", "auth", "token", "sessions"],
            body="stateless tokens let the native app work offline")
    res = ix.precedent("we are switching auth to jwt tokens")
    assert not res["silenced"], "an analogous decision exists — must not be silent"
    titles = [p["title"] for p in res["precedents"]]
    assert any("chose JWT" in t for t in titles), "the analogous past decision must surface"
    assert res["precedents"][0]["what"], "the body (what was decided) must come through"


def test_precedent_is_scoped_to_decisions_and_lessons():
    """A code-symbol, a commit and a task that match the SAME tokens must NOT be served as
    precedent — precedent is the deliberate judgement record only."""
    ix = _idx()
    _decide(ix, "decision: prefer JWT auth tokens", ["jwt", "auth", "token", "decword"])
    _decide(ix, "lesson: JWT auth token expiry must be short", ["jwt", "auth", "token", "lesword"],
            kind="lesson")
    _decide(ix, "jwtAuthToken", ["jwt", "auth", "token", "symword"], kind="code-symbol",
            file_path="auth.py")
    _decide(ix, "feat: add jwt auth token endpoint", ["jwt", "auth", "token", "commitword"],
            kind="commit")
    _decide(ix, "wire up jwt auth token refresh", ["jwt", "auth", "token", "taskword"], kind="task")
    res = ix.precedent("jwt auth token")
    kinds = {p["kind"] for p in res["precedents"]}
    assert kinds <= {"decision", "lesson"}, f"precedent leaked a non-deliberate kind: {kinds}"
    titles = " | ".join(p["title"] for p in res["precedents"])
    assert "decision:" in titles and "lesson:" in titles
    assert "jwtAuthToken" not in titles and "feat:" not in titles and "refresh" not in titles


def test_precedent_attaches_superseded_outcome():
    ix = _idx()
    old = _decide(ix, "OLD: use long-lived API keys", ["apikey", "auth", "longlived", "oldrule"])
    new = _decide(ix, "NEW: rotate short-lived tokens", ["apikey", "auth", "rotate", "newrule"])
    _supersede(ix, old, new)
    res = ix.precedent("apikey auth longlived oldrule")
    p = next(p for p in res["precedents"] if "OLD:" in p["title"])
    assert p["outcome"] == "superseded"
    assert p["superseded_by"] and "rotate short-lived" in p["superseded_by"]["title"], \
        "the precedent must name the decision that governs now"


def test_precedent_walks_supersession_chain_to_the_current_head():
    """A->B->C: querying A's situation must report the CURRENT rule C (the chain head), not
    the intermediate B that was itself later replaced."""
    ix = _idx()
    a = _decide(ix, "A: store sessions in memory", ["session", "memory", "storage", "ruleA"])
    b = _decide(ix, "B: store sessions in redis", ["session", "redis", "storage", "ruleB"])
    c = _decide(ix, "C: store sessions in signed cookies", ["session", "cookie", "storage", "ruleC"])
    _supersede(ix, a, b)
    _supersede(ix, b, c)
    res = ix.precedent("session memory storage ruleA")
    p = next(p for p in res["precedents"] if p["title"].startswith("A:"))
    assert p["superseded_by"]["title"].startswith("C:"), \
        "must resolve to the terminal of the supersedes chain (the rule that governs now)"


def test_precedent_flags_a_decision_that_became_a_landmine():
    ix = _idx()
    _decide(ix, "never disable RLS to fix a query", ["rls", "disable", "query", "landrule"],
            edges=[("warns_about", "auth.py")])
    res = ix.precedent("rls disable query landrule")
    p = next(p for p in res["precedents"] if "disable RLS" in p["title"])
    assert p["became_landmine"] is True, "a decision that now warns_about code must be flagged"


def test_a_superseded_precedent_is_kept_not_dropped():
    """The opposite of a landmine: _landmines DROPS a superseded warning, but precedent KEEPS
    a superseded decision — 'we tried X and reversed it' is the whole point of a precedent."""
    ix = _idx()
    old = _decide(ix, "OLD: poll the API every second", ["poll", "api", "interval", "polrule"])
    new = _decide(ix, "NEW: subscribe to webhooks", ["webhook", "subscribe", "push", "subrule"])
    _supersede(ix, old, new)
    res = ix.precedent("poll api interval polrule")
    assert any("OLD:" in p["title"] for p in res["precedents"]), \
        "a superseded decision must still be served as precedent (kept, not dropped)"


def test_precedent_ranks_by_relevance_then_importance():
    """Equal relevance (same query-matching anchors, same anchor count) → importance breaks
    the tie, so the load-bearing decision ranks first. Outcome must NEVER reorder."""
    ix = _idx()
    _decide(ix, "trivial note about caching", ["cache", "ttl", "store", "trivword"], importance=1)
    _decide(ix, "load-bearing caching invariant", ["cache", "ttl", "store", "loadword"],
            importance=80)
    res = ix.precedent("cache ttl store")
    assert res["precedents"][0]["title"] == "load-bearing caching invariant", \
        "importance must break a relevance tie"


def test_precedent_is_silent_on_no_match():
    ix = _idx()
    _decide(ix, "chose JWT auth", ["jwt", "auth", "token", "x1"])
    res = ix.precedent("completely unrelated quantum banana telescope")
    assert res["silenced"] is True
    assert res["precedents"] == []


def test_precedent_standing_decision_has_no_superseded_by():
    ix = _idx()
    _decide(ix, "use Postgres as the system of record", ["postgres", "database", "record", "pgrule"])
    res = ix.precedent("postgres database record pgrule")
    p = res["precedents"][0]
    assert p["outcome"] == "standing"
    assert p["superseded_by"] is None
    assert p["became_landmine"] is False


def test_precedent_renders_outcome_in_the_prompt_block():
    """The fleet/web-AI surface must carry the OUTCOME line — that is the value a plain
    search cannot give. A superseded precedent must name the current rule."""
    ix = _idx()
    old = _decide(ix, "OLD: ship config in the bundle", ["config", "bundle", "ship", "cfgold"])
    new = _decide(ix, "NEW: load config from env at boot", ["config", "env", "boot", "cfgnew"])
    _supersede(ix, old, new)
    res = ix.precedent("config bundle ship cfgold")
    block = _format_precedent_for_prompt(res)
    assert "OLD: ship config" in block
    assert "superseded" in block and "load config from env" in block, \
        "the prompt block must surface the outcome (the current rule)"

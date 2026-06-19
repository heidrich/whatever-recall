"""Arrow 2 — the deviation push: a `warns_about` landmine fires UNPROMPTED before an edit.

The gap (dogfood 2026-06-15): recall served landmines only on a PULL — `recall()` finds
them when you ask — but `brief()` (which the PreToolUse hook AND every fleet subagent call
before an edit) never fetched `warns_about`, so the warning never PUSHED. These tests pin
the fix: brief() now carries a `warns` track, and it renders in the push surface.

Empirical "before" (one-off, not a committed test, see the session report): for a landmine
pinned to ANOTHER file, `brief(target)` had no `warns` key and the warning was absent from
`why`, while `recall()` still found it — pull worked, push didn't.
"""

import os
import tempfile

from recall import Index
from recall.cli import _format_brief_for_prompt


def _idx():
    """In-memory index over a throwaway repo (no git needed — brief() degrades to fresh)."""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "auth.py"), "w", encoding="utf-8") as f:
        f.write("def login(u):\n    return u\n\n\ndef logout(u):\n    return None\n")
    return Index.open(":memory:", repo=d)


def _landmine_on(ix, target, *, title, anchors, home="docs/incidents.md"):
    """Stamp a warning that LIVES in `home` but `warns_about` `target` — the case `why`
    (file_path-pinned only) can never reach, so it isolates the push from the pin."""
    ix.stamp(title=title, anchors=anchors, file_path=home,
             edges=[("warns_about", target)])


def test_landmine_surfaces_in_brief_as_a_push():
    ix = _idx()
    _landmine_on(ix, "auth.py", title="never bypass RLS in auth — incident 2026-03",
                 anchors=["rls", "bypass", "incident", "authsec"])
    b = ix.brief("auth.py")
    assert "warns" in b, "brief() must carry a warns track (arrow 2)"
    titles = [w["title"] for w in b["warns"]]
    assert any("bypass RLS" in t for t in titles), "the landmine must surface in brief()"
    # the distinguishing value: it is NOT pinned to auth.py, so it is NOT in `why` —
    # only the warns_about push reaches it.
    assert not any("bypass RLS" in w["title"] for w in b["why"]), \
        "the landmine lives elsewhere — it must come via warns, not the file pin"


def test_no_false_alarm_on_an_unrelated_file():
    ix = _idx()
    _landmine_on(ix, "auth.py", title="auth landmine", anchors=["rls", "bypass", "x1", "x2"])
    assert ix.brief("payments.py")["warns"] == [], "a landmine on auth must not fire for payments"


def test_symbol_level_landmine_surfaces_for_its_file():
    """A warning that warns_about a SYMBOL (not a path) must still fire for the symbol's
    file — the edge resolves to the existing code-symbol node, which carries file_path."""
    ix = _idx()
    ix.stamp(title="login", anchors=["loginsym", "authdef", "fnword"], file_path="auth.py",
             kind="code-symbol", symbol="login", line=1, origin="bootstrap")
    _landmine_on(ix, "login", title="login must never log the raw password",
                 anchors=["password", "logging", "leak", "authpw"])
    titles = [w["title"] for w in ix.brief("auth.py")["warns"]]
    assert any("raw password" in t for t in titles), "a symbol-targeted landmine must fire for its file"


def test_one_warning_about_two_symbols_appears_once():
    """GROUP BY ns.id: a single warning that warns_about two symbols in the same file is
    ONE landmine, not two — the source is what matters, not how many edges it has."""
    ix = _idx()
    for sym, ln, a in [("login", 1, "loginsym"), ("logout", 5, "logoutsym")]:
        ix.stamp(title=sym, anchors=[a, "authdef", f"fn{sym}"], file_path="auth.py",
                 kind="code-symbol", symbol=sym, line=ln, origin="bootstrap")
    ix.stamp(title="auth funcs must check the session first",
             anchors=["session", "guard", "authcheck", "order"],
             file_path="docs/incidents.md",
             edges=[("warns_about", "login"), ("warns_about", "logout")])
    warns = ix.brief("auth.py")["warns"]
    matches = [w for w in warns if "session first" in w["title"]]
    assert len(matches) == 1, f"a single warning must appear once, got {len(matches)}"


def test_landmine_renders_in_the_fleet_push_surface():
    """The fleet/subagent path: `recall brief <file> --terse` → _format_brief_for_prompt.
    The landmine must render verbatim in BOTH terse and rich modes — it is the highest-value
    signal, kept like WHY."""
    ix = _idx()
    _landmine_on(ix, "auth.py", title="never disable RLS row-level checks here",
                 anchors=["rls", "disable", "danger", "authrls"])
    b = ix.brief("auth.py")
    terse = _format_brief_for_prompt(b, terse=True)
    rich = _format_brief_for_prompt(b, terse=False)
    assert "LANDMINE" in terse and "disable RLS" in terse, "terse push must surface the landmine"
    assert "LANDMINE" in rich and "disable RLS" in rich, "rich push must surface the landmine"


def test_push_did_not_break_pull():
    """Sanity: adding the push must not regress recall() — the landmine is still findable."""
    ix = _idx()
    _landmine_on(ix, "auth.py", title="never bypass RLS in auth",
                 anchors=["rls", "bypass", "incident", "authsec"])
    hits = ix.recall("rls bypass incident auth")["results"]
    assert any("bypass RLS" in h["title"] for h in hits), "recall() (pull) must still find it"


# ─────────────────────── adversarial-sweep regressions (2026-06-15) ───────────────────────
# Each pins a defect the 6-dimension adversarial sweep confirmed; see
# internal/deviation-push-adversarial-findings.md.

def test_symbol_targeted_landmine_surfaces_via_symbol_branch():
    """#1 false-negative (HIGH): the DOCUMENTED way to mark a landmine is `warns_about ->
    <symbol>`. When the symbol's codemap node has a QUALIFIED title (auth.login) the bare
    target ('login') resolves to an ORPHAN node keyed by the symbol name, which the old
    path-only query missed. brief() must still surface it via the symbol branch."""
    ix = _idx()
    ix.stamp(title="auth.login", anchors=["authloginsym", "qualtitle", "fnq"],
             file_path="auth.py", kind="code-symbol", symbol="login", line=1, origin="bootstrap")
    _landmine_on(ix, "login", title="login must reject an empty user",
                 anchors=["emptyuser", "reject", "validate", "loginguard"])
    titles = [w["title"] for w in ix.brief("auth.py")["warns"]]
    assert any("reject an empty user" in t for t in titles), \
        "a symbol-targeted landmine (the documented form) must surface for its file"


def test_superseded_landmine_is_dropped():
    """#2 false-positive: a warning whose source was SUPERSEDED must stop firing."""
    ix = _idx()
    _landmine_on(ix, "auth.py", title="OLD: never use approach X",
                 anchors=["approachx", "old", "deprecated", "authx"])
    wid = ix.db.execute("SELECT id FROM nodes WHERE title LIKE 'OLD:%'").fetchone()[0]
    ix.stamp(title="NEW: approach X is fine now", anchors=["approachx2", "newrule", "ok2", "authx2"],
             file_path="docs/new.md")
    nid = ix.db.execute("SELECT id FROM nodes WHERE title LIKE 'NEW:%'").fetchone()[0]
    ix.db.execute("INSERT INTO edges(src_node,dst_node,kind,stamped_at_sha) VALUES(?,?,?,?)",
                  (nid, wid, "supersedes", "x"))
    ix.db.commit()
    assert not any("OLD:" in w["title"] for w in ix.brief("auth.py")["warns"]), \
        "a superseded warning must not fire as a live landmine"


def test_landmine_not_duplicated_in_why_and_warns():
    """#3 dedup: a lesson BOTH pinned to the file AND warning about it shows ONCE (as a
    landmine), never twice (also as a why)."""
    ix = _idx()
    ix.stamp(title="auth: always set workspace_id on writes",
             anchors=["workspaceid", "rls", "writes", "authwid"],
             file_path="auth.py", edges=[("warns_about", "auth.py")])
    b = ix.brief("auth.py")
    assert any("workspace_id" in w["title"] for w in b["warns"]), "must appear as a landmine"
    assert not any("workspace_id" in w["title"] for w in b["why"]), "must NOT also appear in why"


def test_landmine_ranked_by_importance_over_recency():
    """#4 ranking (HIGH): a load-bearing OLD landmine must outrank trivial NEWER ones — not
    be buried by pure recency."""
    ix = _idx()
    _landmine_on(ix, "auth.py", title="CRITICAL old landmine",
                 anchors=["critical", "old", "sev1", "authcrit"])
    wid = ix.db.execute("SELECT id FROM nodes WHERE title LIKE 'CRITICAL%'").fetchone()[0]
    ix.db.execute("UPDATE nodes SET importance=99 WHERE id=?", (wid,))
    ix.db.commit()
    _landmine_on(ix, "auth.py", title="trivial newer one", anchors=["triv1", "n1", "x1", "authn1"])
    _landmine_on(ix, "auth.py", title="trivial newer two", anchors=["triv2", "n2", "x2", "authn2"])
    warns = ix.brief("auth.py")["warns"]
    assert warns and warns[0]["title"] == "CRITICAL old landmine", \
        "importance must rank above recency"


def test_stale_landmine_flagged_in_terse_render():
    """#5 render fidelity: the terse/fleet surface must flag a stale landmine, like the others."""
    b = {
        "known": True, "file": "auth.py", "symbols": [], "why": [], "open_tasks": [],
        "breaks": [], "depends_on": [], "drift": "fresh",
        "warns": [{"node_id": 1, "kind": "lesson", "title": "X", "sha": "abc1234",
                   "drift": "committed", "why": "w"}],
    }
    out = _format_brief_for_prompt(b, terse=True)
    assert "check freshness" in out, "a stale landmine must be flagged in the terse push too"

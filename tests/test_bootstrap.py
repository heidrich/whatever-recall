"""bootstrap.py — init() on a mini fixture repo: code map + git + knowledge."""

import shutil
import subprocess

import pytest

from recall import Index
from recall.bootstrap import init, update_incremental


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _require_codemap():
    """Skip ONLY when the [codemap] extra is genuinely not installed.

    Launch-audit lesson (2026-06-13): tests that `skip`/`return` on `code_symbols == 0`
    are BLIND — they can't tell 'tree-sitter absent' from 'tree-sitter present but
    silently yielding 0 symbols' (the get_parser/bytes regression that shipped a broken
    parser past a green suite). So: skip up front iff the extra is truly absent; then the
    caller ASSERTS code_symbols > 0, turning a silent-zero parser into a FAILING test."""
    pytest.importorskip("tree_sitter", reason="[codemap] extra not installed")
    pytest.importorskip("tree_sitter_language_pack", reason="[codemap] extra not installed")


def _make_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def login(user):\n    return user\n\nclass AuthGuard:\n    def check(self):\n        return True\n",
        encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v1 — auth hardening\n\nWe fixed the workspace_id NULL bug in the "
        "RLS cutover so uploads stop vanishing for the owner after the legacy drop.\n",
        encoding="utf-8",
    )
    has_git = shutil.which("git") is not None
    if has_git:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t.t")
        _git(repo, "config", "user.name", "t")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m",
             "feat: add login and auth guard\n\nRecall-anchors: login, auth, guard\nRecall-why: guard checks every request")
    return repo, has_git


def test_bootstrap_indexes_code_and_knowledge(tmp_path):
    repo, has_git = _make_repo(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    st = init(idx, repo)
    s = idx.stats()
    assert s["nodes"] > 0
    # code symbols only if tree-sitter is present; knowledge always.
    assert st["lessons"] >= 1
    # the CHANGELOG lesson is recallable
    res = idx.recall("workspace_id rls cutover uploads")
    assert not res["silenced"]


def test_bootstrap_stamps_commit_trailer(tmp_path):
    repo, has_git = _make_repo(tmp_path)
    if not has_git:
        pytest.skip("git not available")
    idx = Index.open(":memory:", repo=repo)
    st = init(idx, repo)
    assert st["trailers"] >= 1  # the trailer commit self-stamped
    res = idx.recall("login auth guard request")
    assert not res["silenced"]


def test_bootstrap_no_git_fallback(tmp_path):
    """A folder with no .git still indexes code + knowledge (env adaptation)."""
    repo = tmp_path / "nogit"
    repo.mkdir()
    (repo / "MEMORY.md").write_text(
        "# Memory\n\n## tenancy lesson\n\nThe RLS cutover requires writers to set workspace_id "
        "on every insert path or new rows are invisible.\n",
        encoding="utf-8",
    )
    idx = Index.open(":memory:", repo=repo)
    st = init(idx, repo)
    assert st["commits"] == 0  # no git, no commits
    assert st["lessons"] >= 1
    assert not idx.recall("workspace_id insert writers tenancy")["silenced"]


def test_commit_files_become_co_changed_and_useful(tmp_path):
    """Heal while coding (ADR-016): the source files a commit touched TOGETHER get
    co_changed edges + implicit useful feedback — fully automatic, no manual call,
    no model. Catches relations the AST import graph never sees."""
    repo = tmp_path / "co"
    repo.mkdir()
    if shutil.which("git") is None:
        pytest.skip("git not available")
    # two source files that do NOT import each other but change together
    (repo / "handler.py").write_text("def handle(req):\n    return guard(req)\n", encoding="utf-8")
    (repo / "guard.py").write_text("def guard(req):\n    return True\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: handler calls guard")  # both in ONE commit

    _require_codemap()
    idx = Index.open(":memory:", repo=repo)
    init(idx, repo)
    cc = idx.db.execute("SELECT COUNT(*) FROM edges WHERE kind='co_changed'").fetchone()[0]
    # with [codemap] installed, a 0-symbol result means the parser silently broke — FAIL.
    assert idx.stats()["by_kind"].get("code-symbol", 0) > 0, \
        "codemap installed but 0 code symbols — the parser regressed (silent zero)"
    assert cc >= 2  # symmetric handler<->guard
    # the relation surfaces both ways
    pairs = {(a, b) for a, b in idx.db.execute(
        "SELECT na.file_path, nb.file_path FROM edges e "
        "JOIN nodes na ON na.id=e.src_node JOIN nodes nb ON nb.id=e.dst_node "
        "WHERE e.kind='co_changed'").fetchall()}
    assert ("handler.py", "guard.py") in pairs and ("guard.py", "handler.py") in pairs
    # implicit feedback: the touched files' code nodes were marked useful
    useful = idx.db.execute("SELECT SUM(useful_count) FROM node_feedback").fetchone()[0]
    assert (useful or 0) >= 2


def test_sweep_commit_does_not_co_change_everything(tmp_path):
    """A broad commit (many files) is a sweep, not a focused relation — it must NOT
    co-link everything it touched (that would be noise)."""
    repo = tmp_path / "sweep"
    repo.mkdir()
    if shutil.which("git") is None:
        pytest.skip("git not available")
    for i in range(20):  # > _CO_CHANGE_MAX_FILES
        (repo / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: format everything")
    idx = Index.open(":memory:", repo=repo)
    init(idx, repo)
    cc = idx.db.execute("SELECT COUNT(*) FROM edges WHERE kind='co_changed'").fetchone()[0]
    assert cc == 0  # the sweep was skipped


def test_init_is_idempotent_re_running_does_not_grow_the_index(tmp_path):
    """Drift-guard: code symbols are dedup=False, and the live watcher re-inits on every
    commit. Without a rebuild that would DUPLICATE the whole code map each run. init()
    must clear the prior bootstrap layer first, so node count is stable across re-runs."""
    repo, _ = _make_repo(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    init(idx, repo)
    n1 = idx.stats()["nodes"]
    st2 = init(idx, repo)
    n2 = idx.stats()["nodes"]
    assert n2 == n1, f"re-init grew the index {n1}->{n2} (bootstrap layer not cleared)"
    assert st2["cleared"] >= 1  # the second run actually cleared the prior layer


def test_clear_bootstrap_spares_live_and_power_nodes(tmp_path):
    """clear_bootstrap() must remove ONLY origin='bootstrap' — live stamps and Power
    nodes (a re-index must never wipe earned knowledge) survive."""
    repo, _ = _make_repo(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    init(idx, repo)
    # a live stamp (default origin='live') and a power-run stamp must both survive
    idx.stamp("a live lesson about billing", anchors=["billing", "charge"])
    idx.stamp("a power lesson", anchors=["power", "hotspot"], power_run=1, origin="live")
    live_before = idx.db.execute(
        "SELECT count(*) FROM nodes WHERE origin!='bootstrap'").fetchone()[0]
    cleared = idx.clear_bootstrap()
    live_after = idx.db.execute(
        "SELECT count(*) FROM nodes WHERE origin!='bootstrap'").fetchone()[0]
    boot_after = idx.db.execute(
        "SELECT count(*) FROM nodes WHERE origin='bootstrap'").fetchone()[0]
    assert cleared >= 1 and boot_after == 0  # bootstrap gone
    assert live_after == live_before  # nothing non-bootstrap was touched
    assert not idx.recall("billing charge")["silenced"]  # the live lesson still recalls


def test_incremental_update_only_touches_changed_files(tmp_path):
    """The watcher's incremental path: a new commit that adds ONE file re-parses just
    that file, the rest of the code map is left byte-for-byte alone (not rebuilt)."""
    repo, has_git = _make_repo(tmp_path)
    if not has_git:
        pytest.skip("git not available")
    idx = Index.open(tmp_path / "idx.db", repo=repo)
    init(idx, repo)
    base = _git_head(repo)
    app_ids_before = _symbol_ids(idx, "app.py")  # untouched file's node ids

    # new commit: add billing.py (one new symbol), don't touch app.py
    (repo / "billing.py").write_text("def charge(amount):\n    return amount\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: billing\n\nRecall-anchors: billing, charge")
    res = update_incremental(idx, repo, base)

    assert res["ok"] is True and res["files_changed"] >= 1
    # the new file's symbol is now indexed
    assert _symbol_ids(idx, "billing.py"), "incremental update missed the new file"
    # the untouched file's nodes were NOT rebuilt (same ids → not cleared+re-added)
    assert _symbol_ids(idx, "app.py") == app_ids_before, "untouched file was needlessly rebuilt"
    idx.db.close()


def test_incremental_update_reparses_a_modified_file_without_duplicating(tmp_path):
    """Modifying a file re-parses it: the symbol count for that file stays correct
    (old symbols cleared first), never doubled."""
    repo, has_git = _make_repo(tmp_path)
    if not has_git:
        pytest.skip("git not available")
    idx = Index.open(tmp_path / "idx.db", repo=repo)
    init(idx, repo)
    base = _git_head(repo)
    before = len(_symbol_ids(idx, "app.py"))

    # modify app.py: add one function
    (repo / "app.py").write_text(
        "def login(user):\n    return user\n\ndef logout(user):\n    return None\n\n"
        "class AuthGuard:\n    def check(self):\n        return True\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "feat: add logout")
    update_incremental(idx, repo, base)
    after = len(_symbol_ids(idx, "app.py"))
    assert after == before + 1, f"expected +1 symbol, got {before}->{after} (dup or miss)"
    idx.db.close()


def test_incremental_falls_back_to_full_init_on_unknown_sha(tmp_path):
    """An unknown/rewritten baseline SHA can't be diffed — update_incremental must fall
    back to a correct full rebuild (ok=False) rather than silently doing nothing."""
    repo, has_git = _make_repo(tmp_path)
    if not has_git:
        pytest.skip("git not available")
    idx = Index.open(tmp_path / "idx.db", repo=repo)
    init(idx, repo)
    res = update_incremental(idx, repo, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    assert res.get("ok") is False  # signalled a fallback
    assert idx.stats()["nodes"] > 0  # still a correct, populated index
    idx.db.close()


def test_clear_file_symbols_only_touches_that_file(tmp_path):
    """clear_file_symbols(rel) removes ONLY that file's code-symbols, leaving others."""
    repo, _ = _make_repo(tmp_path)
    idx = Index.open(tmp_path / "idx.db", repo=repo)
    (repo / "other.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    init(idx, repo)
    other_before = _symbol_ids(idx, "other.py")
    removed = idx.clear_file_symbols("app.py")
    assert removed >= 1
    assert not _symbol_ids(idx, "app.py")            # app.py's symbols gone
    assert _symbol_ids(idx, "other.py") == other_before  # other.py untouched
    idx.db.close()


def test_bootstrap_records_the_git_author(tmp_path):
    """v3: nodes carry the git author (WHO wrote it) — for the team overview + filter."""
    repo, has_git = _make_repo(tmp_path)
    if not has_git:
        pytest.skip("git not available")
    idx = Index.open(tmp_path / "idx.db", repo=repo)
    init(idx, repo)
    authors = [r[0] for r in idx.db.execute(
        "SELECT DISTINCT author FROM nodes WHERE author IS NOT NULL AND author!=''").fetchall()]
    assert "t" in authors  # _make_repo commits as user.name "t"
    # at least one lesson/commit node carries it
    n = idx.db.execute(
        "SELECT COUNT(*) FROM nodes WHERE author='t' AND kind IN ('lesson','commit')").fetchone()[0]
    assert n >= 1
    idx.db.close()


def test_incremental_update_records_author_of_new_commit(tmp_path):
    """A new commit picked up incrementally also carries its author."""
    repo, has_git = _make_repo(tmp_path)
    if not has_git:
        pytest.skip("git not available")
    idx = Index.open(tmp_path / "idx.db", repo=repo)
    init(idx, repo)
    base = _git_head(repo)
    _git(repo, "config", "user.name", "bob")
    (repo / "x.py").write_text("def g():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: x\n\nRecall-anchors: ecks, gee")
    update_incremental(idx, repo, base)
    got = idx.db.execute(
        "SELECT author FROM nodes WHERE author='bob' LIMIT 1").fetchone()
    assert got is not None  # bob's commit was attributed
    idx.db.close()


# ---- drift-guards: the auto-tag floor + docstring capture (the precision wave) ----
# These pin three invariants discovered while measuring recall quality:
#   1. every bootstrap node earns a source-tag, so facet_weights can grade it
#      (an untagged node weighs 1.0 — the loud default that let a .gitignore
#      commit outrank an ADR);
#   2. a function's docstring is folded into the code-symbol (body + anchors), so
#      the symbol is findable by what it DOES, not just by its name;
#   3. housekeeping commits land on the quiet 'chore' tag.

def test_tree_sitter_parser_accepts_bytes_and_yields_symbols(tmp_path):
    """Regression (audit 2026-06-13): on a fresh `pip install .[codemap]` the very
    first command `recall init .` crashed — tree-sitter 0.25.x's get_parser()
    wrapper rejects `bytes` ("'bytes' object is not an instance of 'str'") and the
    code map silently produced ZERO symbols. Every other code-symbol test SKIPS
    when code_symbols==0, so the break stayed invisible. This test REQUIRES the
    codemap extra and FAILS (not skips) if the parser path is broken."""
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_language_pack")
    from recall.bootstrap import _load_tree_sitter
    parser_for = _load_tree_sitter()
    assert parser_for is not None, "codemap extra installed but _load_tree_sitter returned None"
    parser = parser_for("python")
    assert parser is not None, "no python parser built from the language pack"
    # the whole symbol pipeline slices raw bytes — parse(bytes) MUST work
    tree = parser.parse(b"def add(a, b):\n    return a + b\n")
    assert tree.root_node.type == "module"
    assert len(tree.root_node.children) >= 1

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def add(a, b):\n    return a + b\n\nclass Bank:\n    def deposit(self, n):\n        self.balance += n\n",
        encoding="utf-8",
    )
    idx = Index.open(":memory:", repo=repo)
    n = init(idx, repo)
    # with the codemap extra present, init MUST find real symbols — not 0
    assert n["code_symbols"] >= 3, f"expected add/Bank/deposit, got {n['code_symbols']}"
    syms = {r[0] for r in idx.db.execute(
        "SELECT symbol FROM nodes WHERE kind='code-symbol'").fetchall()}
    assert {"add", "Bank", "deposit"} <= syms


def test_docstring_is_captured_into_the_code_symbol(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "m.py").write_text(
        'def normalize_widget(raw):\n'
        '    """Map a raw widget onto the canonical shape and drop unknown keys."""\n'
        '    return raw\n',
        encoding="utf-8",
    )
    _require_codemap()
    idx = Index.open(":memory:", repo=repo)
    n = init(idx, repo)
    assert n["code_symbols"] > 0, \
        "codemap installed but 0 code symbols — the parser regressed (silent zero)"
    row = idx.db.execute(
        "SELECT body FROM nodes WHERE kind='code-symbol' AND symbol='normalize_widget'"
    ).fetchone()
    assert row and row[0] and "canonical shape" in row[0]  # docstring became the body
    # …and the symbol is now findable by what it does, not just its name
    res = idx.recall("map widget onto canonical shape drop unknown keys")
    assert not res["silenced"]
    assert any(r.get("symbol") == "normalize_widget" for r in res["results"])


def test_bootstrap_nodes_get_a_source_tag(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "decisions.md").write_text(
        "# Decisions\n\n## ADR-001 — write-time stamping\n\nWe stamp the meaning at "
        "commit time instead of guessing it at read time, because the author knows "
        "best right then.\n",
        encoding="utf-8",
    )
    idx = Index.open(":memory:", repo=repo)
    n = init(idx, repo)
    # decisions.md content carries the 'foundation' floor (a valid vocab tag)
    adr = idx.db.execute(
        "SELECT facets FROM nodes WHERE kind='lesson' "
        "AND REPLACE(file_path,'\\','/') LIKE '%decisions.md'").fetchone()
    assert adr and "foundation" in (adr[0] or "")
    if n["code_symbols"]:
        sym = idx.db.execute(
            "SELECT facets FROM nodes WHERE kind='code-symbol' LIMIT 1").fetchone()
        assert sym and "new-code" in (sym[0] or "")  # code map carries new-code


def test_housekeeping_commit_lands_on_chore(tmp_path):
    repo, has_git = _make_repo(tmp_path)
    if not has_git:
        pytest.skip("git not available")
    idx = Index.open(tmp_path / "idx.db", repo=repo)
    init(idx, repo)
    base = _git_head(repo)
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: ignore node_modules")
    update_incremental(idx, repo, base)
    row = idx.db.execute(
        "SELECT facets FROM nodes WHERE kind='commit' AND title LIKE 'chore%'").fetchone()
    assert row and "chore" in (row[0] or "")  # housekeeping is tagged quiet
    idx.db.close()


def _git_head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _symbol_ids(idx, filename):
    return sorted(r[0] for r in idx.db.execute(
        "SELECT id FROM nodes WHERE kind='code-symbol' "
        "AND REPLACE(file_path,'\\','/') LIKE ?", (f"%{filename}",)).fetchall())


def _make_import_repo(tmp_path):
    """A repo where one module imports another, so the AST dependency graph has an edge."""
    repo = tmp_path / "depproj"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "guards.py").write_text(
        "def require_auth(req):\n    return req\n", encoding="utf-8")
    (repo / "pkg" / "handler.py").write_text(
        "from pkg.guards import require_auth\n\ndef handle(req):\n    return require_auth(req)\n",
        encoding="utf-8")
    if shutil.which("git"):
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t.t")
        _git(repo, "config", "user.name", "t")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "feat: handler + guard")
    return repo


def test_bootstrap_builds_dependency_edges(tmp_path):
    """handler.py imports guards.py -> a depends_on edge between their code-symbols.
    Skips cleanly if tree-sitter is absent (no code map, no edges)."""
    _require_codemap()
    repo = _make_import_repo(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    st = init(idx, repo)
    assert st["code_symbols"] > 0, \
        "codemap installed but 0 code symbols — the parser regressed (silent zero)"
    assert st.get("dep_edges", 0) >= 1
    # the edge points handler.py -> pkg/guards.py
    rows = idx.db.execute(
        "SELECT ns.file_path, nd.file_path FROM edges e "
        "JOIN nodes ns ON ns.id=e.src_node JOIN nodes nd ON nd.id=e.dst_node "
        "WHERE e.kind='depends_on'"
    ).fetchall()
    assert any("handler.py" in a and "guards.py" in b for a, b in rows)


def test_dependency_edges_are_idempotent_on_reinit(tmp_path):
    _require_codemap()
    repo = _make_import_repo(tmp_path)
    idx = Index.open(":memory:", repo=repo)
    assert init(idx, repo)["code_symbols"] > 0, \
        "codemap installed but 0 code symbols — the parser regressed (silent zero)"
    n1 = idx.db.execute("SELECT COUNT(*) FROM edges WHERE kind='depends_on'").fetchone()[0]
    init(idx, repo)  # rebuild
    n2 = idx.db.execute("SELECT COUNT(*) FROM edges WHERE kind='depends_on'").fetchone()[0]
    assert n1 == n2 and n1 >= 1  # rebuild does not duplicate edges


# --------------------------------------------- heading-pollution filter (hygiene wave)
def test_heading_only_section_is_not_a_lesson(tmp_path):
    """A long heading with no own prose (structure, not knowledge) gets no node."""
    from recall.bootstrap import _own_prose, _MIN_OWN_PROSE
    sec = ("## [Unreleased] — 2026-06-09 — a very long heading line that easily clears "
           "the sixty char gate\n### sub-heading only below\n#### another heading\n")
    assert _own_prose(sec) < _MIN_OWN_PROSE


def test_own_prose_skips_structure_lines():
    from recall.bootstrap import _own_prose
    sec = ("## title line\n"
           "---\n"
           "[a link only line](https://example.com)\n"
           "\n"
           "### sub\n"
           "real prose that counts toward the threshold\n")
    assert _own_prose(sec) == len("real prose that counts toward the threshold")


def test_h1_preamble_and_stub_sections_skipped_real_bootstrap(tmp_path):
    """End-to-end: a knowledge file with an H1 preamble + a heading-only stub + one
    real section yields exactly ONE lesson."""
    from recall import bootstrap as bs
    from recall.engine import Index
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\nAll notable changes, loosely keep-a-changelog, this preamble "
        "is long enough to pass the old sixty char gate easily.\n\n"
        "## [v1] — 2026-01-01 — heading-only stub entry that is long but hollow\n\n"
        "## [v2] — 2026-01-02 — the real entry\n\n"
        "The watcher now re-indexes new commits by itself in the background thread, "
        "respecting the power lock and staying model-free per the seam guard rule. "
        "This is genuine prose with plenty of substance to clear the threshold.\n",
        encoding="utf-8")
    idx = Index.open(":memory:", repo=repo)
    stats = {"lessons": 0}
    bs._bootstrap_knowledge(idx, repo, stats, has_git=False)
    titles = [r[0] for r in idx.db.execute(
        "SELECT title FROM nodes WHERE kind='lesson'").fetchall()]
    assert stats["lessons"] == 1, titles
    assert titles == ["[v2] — 2026-01-02 — the real entry"]


def test_knowledge_files_order_decisions_before_changelog():
    """Dedup-merge keeps the FIRST node's title — ADR titles must win over their
    CHANGELOG twins, so decisions.md is imported first."""
    from recall.bootstrap import _KNOWLEDGE_FILES
    assert _KNOWLEDGE_FILES.index("docs/decisions.md") < _KNOWLEDGE_FILES.index("CHANGELOG.md")


def test_single_h1_knowledge_file_still_indexed(tmp_path):
    """Review finding: a knowledge file written as ONE `# Title` + prose (no ##/###
    sections) must still yield a lesson — the H1-preamble skip only applies when the
    file actually has subheading sections."""
    from recall import bootstrap as bs
    from recall.engine import Index
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "feedback.md").write_text(
        "# Standing instructions\n\n"
        "Always run the suite before saying done, never push without the changelog, "
        "and treat every open task on a touched file like a failing test that must be "
        "addressed or explicitly deferred with a reason.\n",
        encoding="utf-8")
    idx = Index.open(":memory:", repo=repo)
    stats = {"lessons": 0}
    bs._bootstrap_knowledge(idx, repo, stats, has_git=False)
    assert stats["lessons"] == 1


# --------------------------------------------- import-rules version (self-heal seam)
def test_init_stamps_the_import_rules_version(tmp_path):
    from recall.bootstrap import BOOTSTRAP_RULES_VERSION, _stored_rules_version
    repo, _ = _make_repo(tmp_path)
    idx = Index.open(tmp_path / "idx.db", repo=repo)
    init(idx, repo)
    assert _stored_rules_version(idx) == BOOTSTRAP_RULES_VERSION
    idx.db.close()


def test_incremental_rebootstraps_an_index_built_under_old_rules(tmp_path):
    """An index that predates the current import rules (no/old stamp) must get ONE
    full idempotent rebuild on the next incremental update — heading stubs imported
    under old rules would otherwise linger forever (the watcher only ever adds)."""
    repo, has_git = _make_repo(tmp_path)
    if not has_git:
        pytest.skip("git not available")
    idx = Index.open(tmp_path / "idx.db", repo=repo)
    init(idx, repo)
    base = _git_head(repo)
    # simulate "built by an older engine": wipe the stamp
    idx.db.execute("DELETE FROM meta WHERE key='bootstrap_rules_version'")
    idx.db.commit()

    (repo / "billing.py").write_text("def charge(a):\n    return a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: billing")
    res = update_incremental(idx, repo, base)

    assert res.get("rebootstrapped") is True and res.get("ok") is True
    assert _symbol_ids(idx, "billing.py")        # the new file landed anyway
    from recall.bootstrap import BOOTSTRAP_RULES_VERSION, _stored_rules_version
    assert _stored_rules_version(idx) == BOOTSTRAP_RULES_VERSION  # healed + restamped
    idx.db.close()


def test_incremental_stays_incremental_on_a_current_index(tmp_path):
    """With a current stamp the cheap path must NOT silently turn into full rebuilds."""
    repo, has_git = _make_repo(tmp_path)
    if not has_git:
        pytest.skip("git not available")
    idx = Index.open(tmp_path / "idx.db", repo=repo)
    init(idx, repo)
    base = _git_head(repo)
    (repo / "billing.py").write_text("def charge(a):\n    return a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: billing")
    res = update_incremental(idx, repo, base)
    assert "rebootstrapped" not in res and res.get("ok") is True
    idx.db.close()


# ---------------------------------------------- knowledge grouping (rules v3)
def test_terse_keepachangelog_release_groups_into_one_lesson(tmp_path):
    """Classic keep-a-changelog: `## [1.0.0]` is hollow and every `### Added` is a
    one-liner — each part fails the prose floor alone, but the RELEASE is exactly
    one lesson. Measured on keep-a-changelog itself: the floor ate 28/44 sections
    and whole releases vanished (review follow-up 2026-06-11)."""
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [1.0.0] - 2020-01-01\n\n"
        "### Added\n\n- workspace login guard for the owner accounts\n\n"
        "### Fixed\n\n- uploads vanishing after the legacy schema drop\n\n"
        "### Removed\n\n- the deprecated session cookie fallback path\n",
        encoding="utf-8",
    )
    idx = Index.open(tmp_path / "idx.db", repo=repo)
    init(idx, repo)
    titles = [r[0] for r in idx.db.execute(
        "SELECT title FROM nodes WHERE kind='lesson'").fetchall()]
    assert any("[1.0.0]" in t for t in titles), f"release lesson missing: {titles}"
    assert not any(t in ("Added", "Fixed", "Removed") for t in titles), \
        f"title-less category stubs leaked: {titles}"
    body = idx.db.execute(
        "SELECT body FROM nodes WHERE kind='lesson' AND title LIKE '%[1.0.0]%'"
    ).fetchone()[0]
    assert "workspace login guard" in body and "legacy schema drop" in body
    idx.db.close()


def test_substantial_subsection_stands_alone_with_parent_context(tmp_path):
    """A ### section that clears the floor on its own is its OWN lesson — but a
    child of a HOLLOW parent carries the parent heading in its title (otherwise a
    keep-a-changelog repo imports six lessons all called "Added"). The hollow
    parent itself emits nothing extra."""
    prose = ("The retrieval path stays structurally LLM-free because the seam "
             "guard forbids any provider import outside power.py and llm.py, "
             "which keeps every recall at zero model tokens for the whole team.")
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text(
        f"# Doc\n\n## Architecture notes\n\n### The seam guard decision\n\n{prose}\n",
        encoding="utf-8",
    )
    idx = Index.open(tmp_path / "idx.db", repo=repo)
    init(idx, repo)
    titles = [r[0] for r in idx.db.execute(
        "SELECT title FROM nodes WHERE kind='lesson'").fetchall()]
    assert "Architecture notes — The seam guard decision" in titles, titles
    assert "Architecture notes" not in titles  # hollow parent, no terse children
    idx.db.close()

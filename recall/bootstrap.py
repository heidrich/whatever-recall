"""Bootstrap / cold-start — give an existing repo base intelligence, token-free.

Three sources, all free (no model):
  A) code map   — tree-sitter: one code-symbol node per function/class (file, line).
                  Falls back to git-path anchors if tree-sitter isn't installed.
  B) git log    — full-stamp existing Recall-* trailers; normal commits become
                  weak anchors on the files they touch.
  C) knowledge  — CHANGELOG / ADRs / docs / feedback / MEMORY -> one lesson each.

No-git fallback: if there's no .git, step B is skipped and freshness keys off a
file hash instead of a SHA (environment adaptation, per the concept).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

from recall.anchors import extract_anchors
from recall.engine import Index

# The version of the IMPORT RULES (what becomes a lesson, how files are read).
# Bump it whenever those rules change — init() stamps it into meta, and
# update_incremental() escalates to ONE full idempotent rebuild when the index
# predates the current rules. Without this, the watcher path only ever ADDS:
# lessons imported under OLD rules (e.g. pre-filter heading stubs) would linger
# until someone happens to run `recall init` again (review follow-up 2026-06-11).
#   2 = heading-stub filter (_MIN_OWN_PROSE + H1 skip) + decisions-before-CHANGELOG
#   3 = terse ### children group under a hollow ## parent (keep-a-changelog releases)
BOOTSTRAP_RULES_VERSION = 3

# Source file extensions tree-sitter knows how to map (language-pack names).
_LANG_BY_EXT = {
    ".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".java": "java",
    ".rb": "ruby", ".php": "php", ".c": "c", ".cpp": "cpp", ".cs": "c_sharp",
}

# Directories never worth indexing.
_SKIP_DIRS = {
    "node_modules", ".git", ".next", "dist", "build", ".venv", "venv",
    "__pycache__", ".mypy_cache", "coverage", ".turbo", "out", "vendor", ".mind",
}

# Knowledge files worth importing as lessons (relative globs, checked in order).
# ORDER MATTERS for dedup-merge: when an ADR section and its CHANGELOG twin merge,
# the node created FIRST keeps the title — decisions/concept before CHANGELOG so ADR
# titles win (ADR-018..024 used to be invisible as decisions because their CHANGELOG
# twin had won the title).
_KNOWLEDGE_FILES = [
    "docs/decisions.md",
    "docs/concept.md",
    "CHANGELOG.md",
    "MEMORY.md",
    ".claude/docs/feedback.md",
    ".claude/docs/session-handoff.md",
    "feedback.md",
]

# Auto-tag floor: a bootstrap node with no explicit Recall-tags still earns a
# source-tag, so facet_weights can grade it (an untagged node weighs 1.0 — the
# loud default that lets a .gitignore commit outrank an ADR). The weights live
# in rules.md; this only decides WHICH bucket each cold-start node lands in.
# Commit trailers (Recall-tags) always override this floor — write-time wins.
def _knowledge_facets(rel: str) -> list[str]:
    # Only tags in the closed vocabulary (anchors.DEFAULT_ALLOWED_TAGS) survive
    # canonicalization — anything else is silently dropped, leaving the node
    # untagged (weight 1.0). So we map to vocabulary terms deliberately:
    #   foundation (1.0) for ADRs/concept — neutral but classified;
    #   docs (0.7) for reference docs — quieter than the working log;
    #   CHANGELOG and the rest stay untagged ⇒ neutral 1.0, which is correct for
    #   the working log (a release note shouldn't outweigh nor be muted vs code).
    low = rel.lower()
    if "decisions" in low or "concept" in low:
        return ["foundation"]
    if "memory" in low or "session-handoff" in low or "feedback" in low:
        return ["docs"]
    return []  # CHANGELOG etc. — neutral working log, untagged == weight 1.0


def _commit_facets(title: str, files: list[str]) -> list[str]:
    """Grade a cold-start commit by its conventional-commit prefix, falling back
    to 'chore' for housekeeping (lock/.gitignore-only) so it stays quiet."""
    m = re.match(r"^(\w+)", title.strip())
    prefix = (m.group(1).lower() if m else "")
    if prefix == "feat":
        return ["feature"]
    if prefix == "fix":
        return ["bugfix"]
    if prefix in {"chore", "build", "ci", "style", "test", "docs"}:
        return ["chore"]
    # No conventional prefix: housekeeping-only diffs (lockfiles, ignore rules,
    # config) are chore; anything else stays a neutral, untagged commit.
    real = [f for f in files if os.path.basename(f).lower()
            not in {".gitignore", ".gitattributes"}
            and not f.lower().endswith((".lock", "lock.json", ".toml"))]
    return ["chore"] if (files and not real) else []


def init(index: Index, repo: str | Path, *, max_commits: int = 400, code_map: bool = True,
         rebuild: bool = True) -> dict[str, Any]:
    """Index an existing repo. Returns counts. Idempotent: re-running rebuilds cleanly.

    The bootstrap layer (code symbols, commits, imported lessons) is regenerable from
    code + git. Code symbols are stamped dedup=False (intentionally distinct nodes), so
    a naive re-init would DUPLICATE the whole code map — which the live watcher does on
    every commit. rebuild=True (the default) clears the prior origin='bootstrap' layer
    first, so init() is a true rebuild. Live stamps and Power-Mode nodes are untouched.
    Pass rebuild=False only for the very first build (nothing to clear)."""
    repo = Path(repo)
    stats = {"commits": 0, "code_symbols": 0, "lessons": 0, "trailers": 0, "cleared": 0}

    if rebuild:
        stats["cleared"] = index.clear_bootstrap()

    has_git = (repo / ".git").exists()
    if has_git:
        _bootstrap_git(index, repo, max_commits, stats)
    if code_map:
        _bootstrap_code_map(index, repo, stats)
    _bootstrap_knowledge(index, repo, stats, has_git)

    # heal while coding (ADR-016): now that code-symbol nodes exist, turn the recent
    # commits' file-sets (collected during the git walk) into co_changed edges.
    for fileset in stats.pop("_co_change_sets", []):
        _co_change_from_commit(index, fileset, stats)

    # tasks & plans (ADR-017): index .recall/tasks + discovered docs/plans|tasks|roadmap
    # as wired task nodes. After the code map so affects -> relates_to edges resolve.
    from recall.tasks import index_tasks
    index_tasks(index, repo, stats)

    # Importance (ADR-016): now that the dependency graph exists, rank every code node
    # by causal weight (PageRank over the dep edges). Model-free, write-time — so the
    # recall read-path can split a code-track from a knowledge-track. Idempotent.
    from recall.importance import persist_importance
    stats["ranked"] = persist_importance(index.db)

    # stamp the import-rules version this index was built under (self-heal seam)
    index.db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('bootstrap_rules_version', ?)",
        (str(BOOTSTRAP_RULES_VERSION),))

    index.db.commit()
    return stats


def _stored_rules_version(index: Index) -> int:
    """Which import-rules version this index was last (re)built under. 0 = pre-stamp
    (an index built before the seam existed — exactly the ones that need healing)."""
    try:
        row = index.db.execute(
            "SELECT value FROM meta WHERE key='bootstrap_rules_version'").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def update_incremental(index: Index, repo: str | Path, since_sha: str) -> dict[str, Any]:
    """Update the index for ONLY the files a new commit changed — not the whole repo.

    The live watcher calls this when HEAD moves: a one-file commit should re-parse one
    file, not the entire tree (which clear_bootstrap + full re-init does). It:
      1. asks git which paths changed in since_sha..HEAD (added / modified / deleted),
      2. for each changed source file: clears that file's old code-symbols, re-parses it,
      3. for deleted files: clears their symbols,
      4. stamps the new commits' Recall-* trailers + weak commit nodes,
      5. re-freshens drift.
    Returns {files_changed, code_symbols, commits, trailers, lessons, ok}. Falls back to
    a full init() (ok=False) when git can't answer (rewritten history, missing SHA) so
    the caller still ends up correct, just less cheaply."""
    repo = Path(repo)
    out: dict[str, Any] = {"files_changed": 0, "code_symbols": 0, "commits": 0,
                           "trailers": 0, "lessons": 0, "ok": True}
    if not (repo / ".git").exists() or not since_sha:
        return init(index, repo)  # no git / no baseline → a full (idempotent) rebuild

    # self-heal after an engine upgrade: when the index was built under OLDER import
    # rules, adding on top would keep their artifacts (heading stubs, stale titles)
    # alive forever. One full idempotent rebuild here clears them; init() stamps the
    # current version so every later update is incremental again.
    if _stored_rules_version(index) < BOOTSTRAP_RULES_VERSION:
        res = init(index, repo)
        res["ok"] = True
        res["rebootstrapped"] = True
        return res

    diff, rc, _err = _git(repo, "diff", "--name-status", "-z", f"{since_sha}..HEAD")
    if rc != 0:
        # unknown/rewritten SHA → can't diff cheaply; fall back to a correct full rebuild
        res = init(index, repo)
        res["ok"] = False
        return res

    # parse `--name-status -z`: STATUS\0PATH\0 (rename = R\0OLD\0NEW\0)
    toks = [t for t in diff.split("\x00") if t != ""]
    changed: set[str] = set()
    deleted: set[str] = set()
    i = 0
    while i < len(toks):
        status = toks[i]
        if status[:1] == "R" and i + 2 < len(toks):  # rename: old, new
            deleted.add(toks[i + 1].replace("\\", "/"))
            changed.add(toks[i + 2].replace("\\", "/"))
            i += 3
        elif i + 1 < len(toks):
            path = toks[i + 1].replace("\\", "/")
            (deleted if status[:1] == "D" else changed).add(path)
            i += 2
        else:
            break

    parser_for = _load_tree_sitter()
    knowledge_changed = False
    changed_imports: dict[str, list[str]] = {}  # rel -> import specifiers (for dep edges)
    for rel in changed | deleted:
        # 1) code files: re-parse (changed) or just clear (deleted)
        ext = Path(rel).suffix.lower()
        if ext in _LANG_BY_EXT:
            index.clear_file_symbols(rel)
            if rel in changed and parser_for is not None:
                p = repo / rel
                if p.exists():
                    # _index_one_file records the symbol count into out itself; the
                    # return value is the file's imports (used for dep edges below).
                    imps = _index_one_file(index, repo, p, parser_for, out)
                    changed_imports[rel] = imps
            out["files_changed"] += 1
        # 2) knowledge files changed → re-import them (dedup=True merges, never dupes)
        low = rel.lower()
        if rel in changed and (low.endswith(".md") or "/docs/" in low or low.endswith("memory.md")):
            knowledge_changed = True

    if knowledge_changed:
        _bootstrap_knowledge(index, repo, out, True)

    # 2b) refresh dependency edges for the changed files (their old edges CASCADE-dropped
    # with the cleared symbols above; re-resolve against the index's known files). Skipped
    # if no code file changed — keeps a docs-only commit edge-free + fast.
    if changed_imports:
        from recall.graph import dependency_edges

        repo_files = {
            r[0] for r in index.db.execute(
                "SELECT DISTINCT file_path FROM nodes "
                "WHERE file_path IS NOT NULL AND file_path != ''"
            ).fetchall()
        }
        pairs = list(dependency_edges(changed_imports, repo_files))
        out["dep_edges"] = index.add_dependency_edges(pairs)

    # 3) stamp the new commits in since_sha..HEAD (trailers + weak commit nodes)
    _stamp_commits_range(index, repo, since_sha, out)

    # 3b) refresh tasks (ADR-017): clear + re-index so a changed status/affects lands
    # without duplicating (tasks are dedup=False). Cheap full-scan of the small task dir.
    from recall.tasks import index_tasks
    index.clear_tasks()
    index_tasks(index, repo, out)

    # 3c) the graph changed (new edges/nodes) -> refresh importance (model-free, ADR-016).
    from recall.importance import persist_importance
    out["ranked"] = persist_importance(index.db)

    index.db.commit()
    # 4) drift is now stale for every touched file — re-freshen (token-free)
    try:
        index.freshen(repo)
    except Exception:
        pass
    return out


def _stamp_commits_range(index: Index, repo: Path, since_sha: str, stats: dict) -> None:
    """Stamp every commit in since_sha..HEAD (newest set), exactly like the bootstrap
    git walk but bounded to the new range — so a new commit's Recall-* trailers land."""
    NUL = "\x00"
    out, rc, _err = _git(
        repo, "log", f"{since_sha}..HEAD", "-z", "--name-only", "--format=%H%x00%an%x00%s%x00%b%x00"
    )
    if rc != 0 or not out.strip():
        return
    tokens = out.split(NUL)
    i = 0
    while i + 3 < len(tokens):
        sha, author = tokens[i].strip(), tokens[i + 1].strip()
        title, body = tokens[i + 2], tokens[i + 3].strip()
        i += 4
        files: list[str] = []
        while i < len(tokens) and not _looks_like_sha(tokens[i].strip()):
            tok = tokens[i].strip()
            if tok:
                files.append(tok)
            i += 1
            if i < len(tokens) and tokens[i] == "":
                i += 1
                break
        if not sha:
            continue
        try:
            _stamp_one_commit(index, sha, title.strip(), body, files, stats, author=author)
            # heal while coding: the files this commit touched are co_changed (ADR-016).
            _co_change_from_commit(index, files, stats)
        except Exception:
            stats["skipped_commits"] = stats.get("skipped_commits", 0) + 1


# Skip files larger than this before read_bytes + full parse — such files are
# almost always generated/minified/vendored bundles, not hand-written source.
_MAX_SOURCE_BYTES = 2 * 1024 * 1024  # 2 MB
_MAX_DOC_CHARS = 600  # cap a captured docstring (matches the knowledge-node body cap)

# Definition node types worth a code-symbol, keyed by language. The grammars
# differ wildly (Rust uses *_item, Ruby uses method/class/module), so a single
# global set silently missed entire languages — validated per grammar.
_DEF_TYPES = {
    "python": {"function_definition", "class_definition"},
    "typescript": {
        "function_declaration", "method_definition", "class_declaration",
        "interface_declaration", "type_alias_declaration", "arrow_function",
        "function_expression", "function",
    },
    "tsx": {
        "function_declaration", "method_definition", "class_declaration",
        "interface_declaration", "type_alias_declaration", "arrow_function",
        "function_expression", "function",
    },
    "javascript": {
        "function_declaration", "method_definition", "class_declaration",
        "arrow_function", "function_expression", "function",
    },
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "rust": {"function_item", "struct_item", "enum_item", "trait_item", "impl_item", "mod_item"},
    "ruby": {"method", "class", "module", "singleton_method"},
    "java": {"method_declaration", "class_declaration", "interface_declaration", "enum_declaration"},
    "php": {"function_definition", "method_declaration", "class_declaration", "interface_declaration"},
    "c": {"function_definition", "struct_specifier"},
    "cpp": {"function_definition", "class_specifier", "struct_specifier"},
    "c_sharp": {"method_declaration", "class_declaration", "interface_declaration", "struct_declaration"},
}

# Anonymous function-expression nodes whose bound name lives on the parent.
_ANON_FN = {"arrow_function", "function_expression", "function"}


# ----------------------------------------------------------------- A) code map
def _bootstrap_code_map(index: Index, repo: Path, stats: dict) -> None:
    parser_for = _load_tree_sitter()
    if parser_for is None:
        return  # tree-sitter not installed — git-path anchors already cover files
    file_imports: dict[str, list[str]] = {}
    repo_files: set[str] = set()
    for path in _walk_source(repo):
        rel = path.relative_to(repo).as_posix()
        repo_files.add(rel)
        # _index_one_file stamps symbols and (cheaply, from the same parse) returns the
        # file's raw import specifiers, so the dependency graph needs no second parse.
        imports = _index_one_file(index, repo, path, parser_for, stats)
        if imports:
            file_imports[rel] = imports
    _bootstrap_dependency_graph(index, file_imports, repo_files, stats)


def _bootstrap_dependency_graph(index: Index, file_imports: dict, repo_files: set,
                                stats: dict) -> None:
    """Stamp the static depends_on graph from the imports collected during the code map.

    Deterministic, model-free (recall.graph): resolves each import to an indexed repo
    file and links the two files' representative code-symbol nodes. The LLM einordnung
    layer may later refine a depends_on into implements/guarded_by."""
    from recall.graph import dependency_edges

    pairs = list(dependency_edges(file_imports, repo_files))
    stats["dep_edges"] = index.add_dependency_edges(pairs)


def _index_one_file(index: Index, repo: Path, path: Path, parser_for, stats: dict) -> list[str]:
    """Parse ONE source file: stamp its code-symbol nodes AND return its import specifiers.

    Shared by the full code-map bootstrap and the incremental watcher update, so the
    two can never drift in what a 'symbol' is. The caller is responsible for clearing a
    file's old symbols first when re-indexing (code symbols are dedup=False). The symbol
    count is recorded into stats["code_symbols"] here; the RETURN value is the file's raw
    import specifiers (for the dependency graph) — from the same parse, no second read."""
    ext = path.suffix.lower()
    lang = _LANG_BY_EXT.get(ext)
    if not lang:
        return []
    parser = parser_for(lang)
    if parser is None:
        return []
    try:
        src = path.read_bytes()
    except OSError:
        return []
    tree = parser.parse(src)
    rel = path.relative_to(repo).as_posix()
    n = 0
    for name, line, doc in _symbols(tree.root_node, src, lang):
        # The explanatory text the author already wrote next to the definition is
        # write-time knowledge — fold it into anchors (so the symbol is findable by
        # what it DOES, not just its name) and keep it as the node body.
        anchors = list(extract_anchors(f"{name} {rel}")) + [name.lower()]
        if doc:
            anchors += list(extract_anchors(doc[:_MAX_DOC_CHARS]))
        index.stamp(
            title=name,
            body=(doc[:_MAX_DOC_CHARS] if doc else None),
            kind="code-symbol",
            anchors=anchors,
            tags=["new-code"],  # source-tag floor: the code map, gradeable by weight
            file_path=rel,
            symbol=name,
            line=line,
            origin="bootstrap",
            dedup=False,  # code symbols are intentionally distinct nodes
        )
        n += 1
    stats["code_symbols"] += n
    from recall.graph import import_paths
    try:
        return import_paths(tree.root_node, src, lang)
    except RecursionError:
        # The import collectors walk the AST recursively; one pathologically
        # deeply-nested file would otherwise raise RecursionError and abort the
        # ENTIRE `recall init` with an empty index. Degrade to "no import edges
        # for this one file" — symbols are already stamped above.
        return []


def _symbols(node, src: bytes, lang: str):
    """Yield (name, line, doc) for definitions, recursively, using the language's
    definition node types. Recovers names of `const x = () => {}` from the parent.
    `doc` is the explanatory text the author wrote next to the definition (docstring
    or leading block comment), or None."""
    def_types = _DEF_TYPES.get(lang, set())
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in def_types:
            name = _node_name(n, src)
            if name is None and n.type in _ANON_FN:
                name = _bound_name(n, src)  # climb to the variable/assignment parent
            if name:
                yield name, n.start_point[0] + 1, _docstring(n, src, lang)
        stack.extend(n.children)


def _docstring(n, src: bytes, lang: str) -> str | None:
    """The explanatory text attached to a definition. Python: the first string
    literal in the body. C-family (JS/TS/Go/...): the leading block comment.
    Returns a cleaned, non-empty string or None. Best-effort — never raises."""
    try:
        if lang == "python":
            body = next((c for c in n.children if c.type == "block"), None)
            if body is None or not body.children:
                return None
            first = body.children[0]
            # The docstring is the first statement: either a bare `string` node
            # or one wrapped in `expression_statement` (grammar-version dependent).
            str_node = None
            if first.type == "string":
                str_node = first
            elif first.type == "expression_statement" and first.children \
                    and first.children[0].type == "string":
                str_node = first.children[0]
            if str_node is None:
                return None
            # Prefer the inner string_content (no quote fences) when present.
            content = next((c for c in str_node.children
                            if c.type == "string_content"), None)
            raw = (content or str_node).text.decode("utf-8", "replace")
            return _clean_doc(raw.strip().strip('"\'').strip())
        # C-family: a comment node immediately preceding the definition.
        prev = n.prev_sibling
        if prev is not None and prev.type == "comment":
            raw = prev.text.decode("utf-8", "replace")
            return _clean_doc(raw)
    except Exception:
        return None
    return None


def _clean_doc(s: str) -> str | None:
    s = re.sub(r"^/\*+|\*+/$", "", s)        # strip /* */ fences
    s = re.sub(r"(?m)^\s*(\*|//|#)\s?", "", s)  # strip per-line comment markers
    s = " ".join(s.split())                   # collapse whitespace
    return s if len(s) >= 12 else None        # ignore trivial one-word docs


def _bound_name(n, src: bytes) -> str | None:
    """For an anonymous function expression, recover the identifier it's bound to
    (variable_declarator / assignment / field) by climbing to the parent."""
    parent = n.parent
    hops = 0
    while parent is not None and hops < 3:
        if parent.type in (
            "variable_declarator", "assignment_expression", "public_field_definition",
            "field_definition", "pair", "property_signature",
        ):
            return _node_name(parent, src) or _first_identifier(parent, src)
        parent = parent.parent
        hops += 1
    return None


def _first_identifier(n, src: bytes) -> str | None:
    for c in n.children:
        if c.type in ("identifier", "property_identifier", "type_identifier"):
            return src[c.start_byte:c.end_byte].decode("utf-8", "replace")
    return None


def _node_name(n, src: bytes) -> str | None:
    name_node = n.child_by_field_name("name")
    if name_node is None:
        # `constant` covers Ruby class/module names; the rest cover most grammars.
        for c in n.children:
            if c.type in ("identifier", "type_identifier", "property_identifier", "constant"):
                name_node = c
                break
    if name_node is None:
        return None
    return src[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace")


def _load_tree_sitter():
    """Return a `parser_for(lang)` callable, or None if tree-sitter is absent.

    The parsers this returns MUST accept `bytes` from `parser.parse(...)` — the
    whole symbol pipeline (`_symbols`, `_docstring`, `import_paths`) slices the
    raw `src` bytes by node byte-offsets.

    We build parsers from upstream `tree_sitter.Parser(get_language(lang))`, NOT
    `language_pack.get_parser(lang)`: on tree-sitter 0.25.x the convenience
    `get_parser` returns a wrapper whose `Parser.parse` rejects `bytes` (wants a
    `str`) and whose `Tree.root_node` is a zero-arg method — i.e. it breaks the
    bytes contract and the Node API. `Parser(get_language(...))` is the stable,
    bytes-accepting path across 0.21 → 0.25+. (Found by a fresh-venv install
    audit 2026-06-13: `recall init .` crashed on the very first command with
    "argument 'source': 'bytes' object is not an instance of 'str'".)"""
    try:
        from tree_sitter import Parser
        from tree_sitter_language_pack import get_language
    except Exception:
        return None
    _cache: dict[str, Any] = {}

    def _build(lang: str):
        language = get_language(lang)
        try:
            return Parser(language)            # tree-sitter 0.22+: Language in ctor
        except TypeError:
            parser = Parser()                  # 0.21 fallback: assign the language
            parser.set_language(language)
            return parser

    def parser_for(lang: str):
        if lang not in _cache:
            try:
                _cache[lang] = _build(lang)
            except Exception:
                _cache[lang] = None
        return _cache[lang]

    return parser_for


def _walk_source(repo: Path):
    for root, dirs, files in os.walk(repo):  # followlinks=False: no dir-symlink loops
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if Path(f).suffix.lower() not in _LANG_BY_EXT:
                continue
            p = Path(root) / f
            try:
                st = p.stat()  # follows symlink to target
            except OSError:
                continue
            # Skip non-regular files (FIFO/device/socket would block read_bytes) and
            # oversized generated/vendored bundles that aren't worth symbol-mapping.
            if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_SOURCE_BYTES:
                continue
            yield p


# ------------------------------------------------------------------- B) git log
def _git(repo: Path, *args: str) -> tuple[str, int, str]:
    """Run git, returning (stdout, returncode, stderr). returncode 127 = git absent."""
    try:
        # core.quotepath=false: index non-ASCII file paths as raw UTF-8 so the
        # anchors/co-change keys match what the freshness + contested readers
        # see later (and what's on disk). Default C-quoting would mismatch both.
        p = subprocess.run(
            ["git", "-C", str(repo), "-c", "core.quotepath=false", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return p.stdout, p.returncode, p.stderr
    except OSError as e:
        return "", 127, str(e)


def _bootstrap_git(index: Index, repo: Path, max_commits: int, stats: dict) -> None:
    # NUL terminators: %x00 cannot occur inside git object data, so a commit
    # subject/body containing control chars (U+001E/U+001F etc.) can't desync the
    # split — the old \x1e/\x1f delimiters could collide with message text.
    NUL = "\x00"
    # %an = author name (WHO wrote it) — added so nodes carry an author for the
    # team overview + person filter (v3). Field order: sha, author, subject, body.
    out, rc, err = _git(
        repo, "log", f"-{max_commits}", "-z", "--name-only", f"--format=%H%x00%an%x00%s%x00%b%x00"
    )
    if rc != 0:
        # git present (.git exists) but refused to run — dubious-ownership /
        # safe.directory / locked index. Surface it instead of silently indexing 0.
        stats["git_error"] = (err or "git failed").strip().splitlines()[0] if err else "git failed"
        return

    # With -z each record's fields and the trailing file list are NUL-separated;
    # records are delimited by the empty field that %x00 after %b produces, then
    # the file names (also NUL-terminated under -z). We parse by walking tokens.
    tokens = out.split(NUL)
    i = 0
    commit_idx = 0  # git log is newest-first; only the recent window seeds co_changed
    while i + 3 < len(tokens):
        sha, author = tokens[i].strip(), tokens[i + 1].strip()
        title, body = tokens[i + 2], tokens[i + 3].strip()
        i += 4
        # Collect file tokens until the next record (a token that looks like a 40-hex SHA)
        files: list[str] = []
        while i < len(tokens) and not _looks_like_sha(tokens[i]):
            t = tokens[i].strip()
            if t:
                files.append(t)
            i += 1
        if not sha:
            continue
        try:
            _stamp_one_commit(index, sha, title.strip(), body, files, stats, author=author)
            # Defer co_changed: code-symbol nodes don't exist yet (code map runs after the
            # git walk). Collect the recent window's file-sets; apply once the map exists.
            if commit_idx < _CO_CHANGE_HISTORY:
                stats.setdefault("_co_change_sets", []).append(list(files))
            commit_idx += 1
        except Exception:
            # One bad commit (e.g. a pathological trailer) must never abort the
            # whole cold-start. Skip it and keep going.
            stats["skipped_commits"] = stats.get("skipped_commits", 0) + 1


def _looks_like_sha(token: str) -> bool:
    t = token.strip()
    return len(t) == 40 and all(c in "0123456789abcdef" for c in t.lower())


# A commit's files were, by definition, changed TOGETHER — the deterministic source for
# the 'heal while coding' co_changed graph (ADR-016). We link source files a commit
# touched so the invisible co-evolution relation (files that change together but don't
# import each other) is captured for free, no model. Guards:
#  - only SOURCE files (record_co_change skips files with no code-symbol node anyway);
#  - skip giant commits (a sweeping refactor / format run is noise, not a real relation);
#  - drop pure docs/config so a CHANGELOG bump doesn't co-link everything it rode with.
_CO_CHANGE_MAX_FILES = 12   # above this, the commit is a sweep, not a focused change
_CO_CHANGE_HISTORY = 60     # cold-start: only the recent commit window seeds co_changed
_CO_CHANGE_SKIP_SUFFIX = (".md", ".json", ".lock", ".lockb", ".txt", ".yml", ".yaml", ".toml")


def _source_files_for_co_change(files: list[str]) -> list[str]:
    """The hand-written source files in a commit, normalised — the co_change candidates."""
    out = []
    for f in files:
        low = f.lower()
        if low.endswith(_CO_CHANGE_SKIP_SUFFIX) or "/docs/" in low:
            continue
        out.append(f.replace("\\", "/"))
    return out


def _co_change_from_commit(index: Index, files: list[str], stats: dict) -> None:
    """Link the source files of ONE commit as co_changed + mark them useful (model-free).

    Two free signals from one commit (ADR-016): the files belong together (co_changed),
    AND they were actually worked on (implicit positive feedback -> a gentle importance
    lift). A sweep (too many files) is skipped — it relates nothing and shouldn't reward
    everything it touched."""
    src = _source_files_for_co_change(files)
    if len(src) < 2 or len(src) > _CO_CHANGE_MAX_FILES:
        return  # nothing to relate, or a sweep too broad to mean 'these belong together'
    added = index.record_co_change(src, rerank=False)  # init() re-ranks once at the end
    if added:
        stats["co_changed"] = stats.get("co_changed", 0) + added
    # implicit feedback: a touched file's code nodes proved worth surfacing. rerank=False
    # — init()/update_incremental() re-rank importance ONCE at the end (PageRank is
    # O(graph); re-running per node would be a perf bug).
    for rel in src:
        for (nid,) in index.db.execute(
            "SELECT id FROM nodes WHERE file_path=? AND kind='code-symbol'", (rel,)
        ).fetchall():
            index._bump_feedback(nid, useful=1, rerank=False)


def _stamp_one_commit(index: Index, sha: str, title: str, body: str, files: list[str],
                      stats: dict, author: str | None = None) -> None:
    full = f"{title}\n\n{body}"
    # A commit carrying Recall-* trailers stamps itself richly.
    if "Recall-anchors:" in full:
        if index.stamp_from_commit(full, sha[:7], author=author) is not None:
            stats["trailers"] += 1
            return
    # Otherwise: a weak 'commit' node with anchors from subject + body + files.
    anchors = extract_anchors(f"{title} {body} {' '.join(files)}")
    main_file = None
    for f in files:
        base = os.path.basename(f).rsplit(".", 1)[0].lower()
        if len(base) >= 3:
            anchors.add(base)
        if main_file is None and not f.lower().endswith((".md", ".json", ".lock")) and "/docs/" not in f.lower():
            main_file = f
    if not anchors:
        return
    index.stamp(
        title=title or sha[:7],
        body=body or None,
        anchors=list(anchors),
        tags=_commit_facets(title, files),  # feat/fix/chore floor; trailers override
        kind="commit",
        file_path=main_file or (files[0] if files else None),
        sha=sha[:7],
        origin="bootstrap",
        author=author or None,
        dedup=False,  # commits are historical facts, never merged
    )
    stats["commits"] += 1


def _file_last_author(repo: Path, rel: str, has_git: bool) -> str | None:
    """WHO last touched this file, per git (one cheap call). None without git."""
    if not has_git:
        return None
    out, rc, _e = _git(repo, "log", "-1", "--format=%an", "--", rel)
    name = out.strip().splitlines()[0] if (rc == 0 and out.strip()) else ""
    return name or None


# ----------------------------------------------------------------- C) knowledge
# A heading section only becomes a lesson when it carries this many chars of OWN
# paragraph prose (headings, blank lines, rules and link-only lines don't count).
# Measured on the live corpus: kills the 33 structural sections (H1 preambles,
# heading-only stubs that polluted explain's "must-know decisions" and the wiki)
# while every real ADR clears it 6x over (min own prose 631).
# Re-verified on a terse OSS repo (keep-a-changelog itself, 2026-06-11): the floor
# alone ate whole releases — `## [1.1.0]` is hollow (prose 0) and every `### Added`
# is 38-95 chars, so 28/44 sections died and the survivors were title-less "Added"
# stubs. The fix is GROUPING, not a lower floor: terse ### children combine under
# their hollow ## parent into one release-lesson (see _bootstrap_knowledge).
_MIN_OWN_PROSE = 100

_HR_RE = re.compile(r"^[-*_]{3,}\s*$")
_LINK_ONLY_RE = re.compile(r"^\[[^\]]*\]\([^)]*\)[.,;]?\s*$")


def _own_prose(sec: str) -> int:
    """How much of this section is its OWN prose — the substance test for lessons."""
    n = 0
    for line in sec.splitlines():
        s = line.strip()
        if not s or re.match(r"^#{1,6}\s", s) or _HR_RE.match(s) or _LINK_ONLY_RE.match(s):
            continue
        n += len(s)
    return n


def _bootstrap_knowledge(index: Index, repo: Path, stats: dict, has_git: bool) -> None:
    for rel in _KNOWLEDGE_FILES:
        path = repo / rel
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        author = _file_last_author(repo, rel, has_git)  # who wrote this knowledge file
        facets = _knowledge_facets(rel)  # source-tag floor by which file it came from
        sections = [s.strip() for s in re.split(r"\n(?=#{2,3}\s)", text)]
        multi = len(sections) > 1
        # Sections that clear the floor on their own become lessons exactly as
        # before (same titles). NEW (rules v3): a hollow ## parent collects its
        # terse ### children — classic keep-a-changelog releases (`## [1.1.0]`
        # prose 0, every `### Added` a one-liner) failed the floor part by part
        # and the whole release vanished; COMBINED under the version heading they
        # are exactly one lesson. Substantial children still stand alone.
        units: list[tuple[str | None, str]] = []  # (title override, section text)
        bucket: list[str] = []  # a hollow ## parent + its terse ### children

        def _head(sec: str) -> str:
            return sec.splitlines()[0].lstrip("# ").strip()

        def _flush_bucket() -> None:
            if len(bucket) > 1:
                combined = "\n".join(bucket)
                if _own_prose(combined) >= _MIN_OWN_PROSE:
                    units.append((None, combined))
            bucket.clear()

        for sec in sections:
            # H1 preamble (the document title chunk) is never a standalone lesson —
            # the doc itself is the node's file_path, its title carries no claim.
            # BUT only when the file actually has ##/### sections: a knowledge file
            # written as ONE `# Title` + prose (no subheadings) IS its own lesson —
            # skipping it would silently drop the whole file from the index.
            if sec.startswith("# ") and multi:
                _flush_bucket()
                continue
            substantial = len(sec) >= 60 and _own_prose(sec) >= _MIN_OWN_PROSE
            if sec.startswith("## "):
                _flush_bucket()
                if substantial:
                    units.append((None, sec))
                else:
                    bucket.append(sec)       # hollow parent — children may fill it
            else:                            # an ### child (or a single-# file)
                if substantial:
                    # a substantial child of a HOLLOW parent carries no context of
                    # its own — without the prefix a keep-a-changelog repo imports
                    # six lessons all titled "Added". Children of a parent that is
                    # itself a lesson keep their plain title (status quo).
                    if bucket:
                        units.append((f"{_head(bucket[0])} — {_head(sec)}", sec))
                    else:
                        units.append((None, sec))
                elif bucket:
                    bucket.append(sec)       # terse child joins the release bucket
        _flush_bucket()

        for t_override, sec in units:
            title = (t_override or sec.splitlines()[0].lstrip("# ").strip())[:120]
            anchors = extract_anchors(sec[:2000])
            if len(anchors) < 3:
                continue
            index.stamp(
                title=title,
                body=sec[:600],
                anchors=list(anchors),
                tags=facets,
                kind="lesson",
                file_path=rel,
                origin="bootstrap",
                author=author,
                dedup=True,  # knowledge sections CAN restate each other — merge
            )
            stats["lessons"] += 1

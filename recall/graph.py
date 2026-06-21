"""Static dependency graph — AST-derived edges, ZERO model, fully deterministic.

The lexical index (anchors) finds nodes by what words they contain; it has no idea
how files RELATE. This module reads the same tree-sitter AST the code-map already
parses and extracts the one relation the source states outright: `import` / module
dependency. A file that imports another DEPENDS_ON it.

This is the deterministic floor of the graph-intelligence direction (the LLM semantic
edges — guarded_by / supersedes — layer on top later). It runs at write-time only; the
recall() read path stays LLM-free and now also graph-aware (Level-3 relations get real
edges instead of an empty walk).

Pure-ish: the only I/O is reading source bytes (the parser is passed in). Returns
plain data (import facts); the caller stamps edges so this module never touches the DB.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

# Extensions we try, in order, when an import has no suffix (TS/JS resolution).
_RESOLVE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs", ".java",
                 ".rb", ".php", ".c", ".cpp", ".cs")
# index files an import of a directory resolves to (import './foo' -> ./foo/index.ts).
_INDEX_BASENAMES = ("index", "__init__", "mod")
# genuine NON-source asset extensions — a dotted spec ending in one of these is an
# asset import, not a `import a.b.c` python module. We must NOT reuse _RESOLVE_EXTS for
# this gate: it contains source extensions ('.c','.rs','.ts',…) that collide with real
# final module segments (`import pkg.c`). (bug-hunt round 3, 2026-06-15.)
_NONSOURCE_EXTS = (".css", ".scss", ".sass", ".less", ".json", ".svg", ".png", ".jpg",
                   ".jpeg", ".gif", ".webp", ".md", ".txt", ".yaml", ".yml", ".toml")


def import_paths(root_node, src: bytes, lang: str) -> list[str]:
    """Every module path this file imports, as written in source (the string literal).

    Covers the common shapes across languages: ES/TS `import ... from "x"` and bare
    `import "x"`, plus Python `from x import y` / `import x`. Returns raw specifiers
    (e.g. './util', '@/components/Thing', 'react', 'os.path') — resolution is separate."""
    out: list[str] = []
    if lang in ("typescript", "tsx", "javascript"):
        _collect_ts_imports(root_node, src, out)
    elif lang == "python":
        _collect_py_imports(root_node, src, out)
    # other languages: no import extraction yet (deterministic-only; add as measured)
    return out


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _collect_ts_imports(node, src: bytes, out: list[str]) -> None:
    # `import ... from "x"` always carries a module path. A re-export
    # (`export {X} from "./a"`, `export * from "./b"`) does too — the barrel/index.ts
    # pattern — and is an `export_statement` node. BUT `export default "./theme";` also
    # has a `string` child that is the exported VALUE, not a module path; harvesting it
    # produced a FALSE depends_on edge ("a wrong edge is worse than no edge"). So for an
    # export_statement, only collect strings when it is a true re-export — i.e. it has a
    # `from` keyword child. (Round-1 collected every export string — P2 regression, round 2.)
    if node.type == "import_statement":
        for c in node.children:
            if c.type == "string":
                out.append(_text(c, src).strip("\"'`"))
    elif node.type == "export_statement" and any(c.type == "from" for c in node.children):
        for c in node.children:
            if c.type == "string":
                out.append(_text(c, src).strip("\"'`"))
    # dynamic import() and require() are call_expressions — cheap to also catch
    elif node.type == "call_expression":
        fn = node.child_by_field_name("function")
        if fn is not None and _text(fn, src) in ("import", "require"):
            args = node.child_by_field_name("arguments")
            if args is not None:
                for c in args.children:
                    if c.type == "string":
                        out.append(_text(c, src).strip("\"'`"))
    for c in node.children:
        _collect_ts_imports(c, src, out)


def _collect_py_imports(node, src: bytes, out: list[str]) -> None:
    if node.type == "import_from_statement":
        mod = node.child_by_field_name("module_name")
        if mod is not None:
            spec = _text(mod, src)
            # Bare-dot form `from . import foo` / `from .. import bar`: module_name is
            # just dots (the PACKAGE), and the real sibling modules are the imported
            # names. Appending '.' alone resolved to the package dir -> edge dropped.
            # Emit '<dots>name' per imported name so resolution finds the sibling.
            # `from .util import x` already names a module in module_name -> as-is.
            # (P2 bug-hunt 2026-06-15.)
            if spec and set(spec) == {"."}:
                names = node.children_by_field_name("name")
                if names:
                    for nm in names:
                        # `from . import a as x`: the name child is an `aliased_import`
                        # whose verbatim text is 'a as x' -> spec '.a as x' resolves to a
                        # nonexistent path and the edge is lost. Extract the inner name,
                        # exactly as the import_statement branch below does. (Round-1 used
                        # the raw text -> P2 regression on aliased imports, round 2.)
                        inner = nm.child_by_field_name("name") if nm.type == "aliased_import" else nm
                        if inner is not None:
                            out.append(spec + _text(inner, src))
                else:
                    out.append(spec)
            else:
                out.append(spec)
    elif node.type == "import_statement":
        for c in node.children:
            if c.type in ("dotted_name", "aliased_import"):
                name = c.child_by_field_name("name") if c.type == "aliased_import" else c
                if name is not None:
                    out.append(_text(name, src))
    for c in node.children:
        _collect_py_imports(c, src, out)


def resolve_import(spec: str, from_rel: str, repo_files: set[str],
                   alias_roots: tuple[str, ...] = ("src/", "")) -> str | None:
    """Resolve an import specifier to a repo-relative file path, or None if it points
    outside the repo (a node_module / stdlib / unresolved alias). repo_files is the set
    of indexed file paths (posix, repo-relative) so resolution needs no extra disk I/O.

    Handles: relative ('./x', '../y'), TS path alias ('@/x' -> src/x or x), and Python
    dotted ('a.b.c' -> a/b/c). Tries suffix + index-file completion. Conservative: an
    ambiguous or external spec returns None (a wrong edge is worse than no edge)."""
    spec = spec.strip()
    if not spec:
        return None

    candidates: list[str] = []
    if spec.startswith("."):
        base = os.path.dirname(from_rel)
        if "/" in spec or spec.startswith("./") or spec.startswith("../"):
            # JS/TS path-style relative import: './util', '../foo/bar' — join verbatim.
            joined = os.path.normpath(os.path.join(base, spec)).replace("\\", "/")
            candidates.append(joined)
        else:
            # Python package-relative import: '.util', '..pkg.mod'. Leading dots are
            # directory levels (1 = current package, 2 = parent, ...); the dotted tail
            # is a module path. os.path.join(base, '.util') would wrongly yield a hidden
            # file 'base/.util', so the edge was always dropped — split it out properly.
            n_dots = len(spec) - len(spec.lstrip("."))
            tail = spec[n_dots:].replace(".", "/")  # '.util'->'util', '..a.b'->'a/b'
            up = base
            for _ in range(n_dots - 1):  # first dot = current dir; each extra = one up
                up = os.path.dirname(up)
            joined = os.path.normpath(os.path.join(up, tail)).replace("\\", "/")
            candidates.append(joined)
    elif spec.startswith("@/"):
        rest = spec[2:]
        # `@/x` is the TS path alias `"@/*": ["./src/*"]`, defined PER tsconfig — and a
        # monorepo has several (web/, admin/, app/ each ship their own). So `@/` is
        # relative to the importing file's APP ROOT, not the repo root. We don't read
        # tsconfigs; instead we walk every ancestor dir of `from_rel` as a candidate app
        # root and offer `<root>/src/<rest>` (the standard layout) + `<root>/<rest>`
        # (src-less). Nearest root first so web/src wins over repo-root for a web file.
        # The membership check in _complete() is the safety net: only a real indexed file
        # resolves, so the extra candidates never invent a wrong edge. Repo-root `@/`
        # (the original ("src/","") behaviour) is still covered — root "" is the last
        # ancestor. (monorepo @/-alias fix 2026-06-20: web/admin had ~0 edges.)
        roots: list[str] = []
        d = os.path.dirname(from_rel)
        while True:
            roots.append(d + "/" if d else "")
            if not d:
                break
            d = os.path.dirname(d)
        for r in roots:
            candidates.append(r + "src/" + rest)
            candidates.append(r + rest)
    elif "/" not in spec and "." in spec and not spec.endswith(_NONSOURCE_EXTS):
        # python dotted module: a.b.c -> a/b/c (also try under src/). The guard was
        # `endswith(_RESOLVE_EXTS)` — which includes '.c'/'.rs'/'.go'/'.ts'/… — so
        # `import mypkg.c` (final segment 'c') was wrongly treated as having a source
        # suffix and skipped -> the real edge to mypkg/c.py was DROPPED. The membership
        # check in _complete is the actual safety (an external like os.path resolves to
        # None because os/path isn't in repo_files), so we only exclude genuine asset
        # extensions here, not source-language ones. (P3 bug-hunt round 3, 2026-06-15.)
        dotted = spec.replace(".", "/")
        candidates += [dotted] + [r + dotted for r in alias_roots]
    else:
        # bare specifier ('react', 'os') -> external; or an alias root we don't know
        return None

    for cand in candidates:
        hit = _complete(cand, repo_files)
        if hit:
            return hit
    return None


def _complete(path_no_ext: str, repo_files: set[str]) -> str | None:
    """Turn a path that may lack a suffix / point at a dir into an actual indexed file."""
    if path_no_ext in repo_files:
        return path_no_ext
    for ext in _RESOLVE_EXTS:
        if path_no_ext + ext in repo_files:
            return path_no_ext + ext
    for base in _INDEX_BASENAMES:
        for ext in _RESOLVE_EXTS:
            cand = f"{path_no_ext}/{base}{ext}"
            if cand in repo_files:
                return cand
    return None


def dependency_edges(file_imports: dict[str, list[str]], repo_files: set[str],
                     alias_roots: tuple[str, ...] = ("src/", "")) -> Iterator[tuple[str, str]]:
    """Yield (from_file, to_file) dependency pairs, deduped, self-edges removed.

    file_imports maps each repo-relative source file to its raw import specifiers.
    Only edges whose target resolves to an INDEXED repo file are emitted (external deps
    are dropped — they have no node to point at). The result is the static depends_on
    graph: deterministic, model-free, exactly what the source states."""
    seen: set[tuple[str, str]] = set()
    for from_rel, specs in file_imports.items():
        for spec in specs:
            to_rel = resolve_import(spec, from_rel, repo_files, alias_roots)
            if to_rel and to_rel != from_rel:
                pair = (from_rel, to_rel)
                if pair not in seen:
                    seen.add(pair)
                    yield pair

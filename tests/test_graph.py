"""Drift-guards for recall/graph.py — the static AST dependency extractor (deterministic,
model-free). Pins import extraction + path resolution + edge dedup, so the graph layer
can't silently regress. tree-sitter is required for the extraction tests (skipped if absent).
"""

from __future__ import annotations

import pytest

from recall.graph import resolve_import, dependency_edges, import_paths

# Build a tree-sitter parser the SAME way production does (recall.bootstrap.
# _load_tree_sitter), NOT via language_pack.get_parser: on tree-sitter 0.25.x
# get_parser's Parser.parse rejects bytes and Tree.root_node is a zero-arg method,
# while the whole graph pipeline slices raw `src` bytes by node byte-offsets. The
# production path accepts bytes across 0.21→0.25+, so these tests exercise the exact
# codemap parser and can't skew from it. (graph test fix 2026-06-15)
from recall.bootstrap import _load_tree_sitter

_parser_for = _load_tree_sitter()
needs_ts = pytest.mark.skipif(_parser_for is None, reason="tree-sitter not installed")


def _ts_parse(lang: str, src: bytes):
    return _parser_for(lang).parse(src)


# ----------------------------------------------------------------- resolution
def _files(*paths):
    return set(paths)


def test_resolves_relative_import_with_suffix_completion():
    files = _files("src/a.ts", "src/util.ts")
    assert resolve_import("./util", "src/a.ts", files) == "src/util.ts"


def test_resolves_parent_relative():
    files = _files("src/feat/a.ts", "src/lib/helper.ts")
    assert resolve_import("../lib/helper", "src/feat/a.ts", files) == "src/lib/helper.ts"


def test_resolves_at_alias_to_src():
    files = _files("src/components/Thing.tsx", "src/a.ts")
    assert resolve_import("@/components/Thing", "src/a.ts", files) == "src/components/Thing.tsx"


def test_resolves_at_alias_per_monorepo_app_root():
    # A monorepo ships several Next.js apps (web/, admin/, app/) each with its OWN
    # tsconfig `"@/*": ["./src/*"]`. `@/x` must resolve to the IMPORTING file's app
    # root, not the repo root — before this fix web/admin had ~0 edges (all @/ dropped).
    web = _files("web/src/lib/api.ts", "web/src/page.tsx")
    assert resolve_import("@/lib/api", "web/src/page.tsx", web) == "web/src/lib/api.ts"
    admin = _files("admin/src/components/Table.tsx", "admin/src/page.tsx")
    assert resolve_import("@/components/Table", "admin/src/page.tsx", admin) == "admin/src/components/Table.tsx"


def test_at_alias_does_not_cross_app_boundary():
    # An admin file's `@/only` must NOT resolve to a same-named file in web/ — the
    # nearer app root wins and the repo-root candidate isn't present here ("a wrong
    # edge is worse than no edge").
    files = _files("web/src/only.ts", "admin/src/page.tsx")
    assert resolve_import("@/only", "admin/src/page.tsx", files) is None


def test_resolves_directory_to_index_file():
    files = _files("src/a.ts", "src/lib/index.ts")
    assert resolve_import("./lib", "src/a.ts", files) == "src/lib/index.ts"


def test_external_specifier_is_none():
    files = _files("src/a.ts")
    assert resolve_import("react", "src/a.ts", files) is None
    assert resolve_import("@scope/pkg", "src/a.ts", files) is None  # unknown alias root


def test_unresolvable_relative_is_none():
    files = _files("src/a.ts")
    assert resolve_import("./does-not-exist", "src/a.ts", files) is None


def test_python_dotted_module_resolves():
    files = _files("pkg/a.py", "pkg/sub/mod.py")
    assert resolve_import("pkg.sub.mod", "pkg/a.py", files) == "pkg/sub/mod.py"


# ----------------------------------------------------------------- edge building
def test_dependency_edges_dedupe_and_drop_self():
    file_imports = {
        "src/a.ts": ["./b", "./b", "./a"],  # dup + self
        "src/b.ts": [],
    }
    files = _files("src/a.ts", "src/b.ts")
    edges = list(dependency_edges(file_imports, files))
    assert edges == [("src/a.ts", "src/b.ts")]  # deduped, no self-edge


def test_dependency_edges_drop_external():
    file_imports = {"src/a.ts": ["react", "./b"]}
    files = _files("src/a.ts", "src/b.ts")
    edges = list(dependency_edges(file_imports, files))
    assert edges == [("src/a.ts", "src/b.ts")]  # react has no node -> dropped


# ----------------------------------------------------------------- AST extraction
@needs_ts
def test_extracts_ts_imports():
    src = (b'import { foo } from "./util";\n'
           b'import Default from "@/c/Thing";\n'
           b'import "./side.css";\n')
    tree = _ts_parse("tsx", src)
    paths = import_paths(tree.root_node, src, "tsx")
    assert "./util" in paths and "@/c/Thing" in paths and "./side.css" in paths


@needs_ts
def test_extracts_python_imports():
    src = b"from pkg.sub import thing\nimport os.path\n"
    tree = _ts_parse("python", src)
    paths = import_paths(tree.root_node, src, "python")
    assert any("pkg.sub" in p for p in paths)


@needs_ts
def test_extracts_dynamic_import_and_require():
    src = b'const x = await import("./lazy");\nconst y = require("./cjs");\n'
    tree = _ts_parse("tsx", src)
    paths = import_paths(tree.root_node, src, "tsx")
    assert "./lazy" in paths and "./cjs" in paths

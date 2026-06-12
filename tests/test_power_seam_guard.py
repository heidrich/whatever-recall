"""The sacred-principle guard (power-mode-plan.md STEP 8, pulled forward to STEP 3).

The recall() READ path MUST stay LLM-free: 0 tokens, offline, local-LLM-friendly.
The seam that reaches a model is power.py + llm.py; cli.py and dashboard.py expose
`recall power` / the dashboard's power endpoints and so may reach the seam — but ONLY
lazily inside the relevant function, never at module top level (so plain `recall
"<query>"`, or loading the dashboard page / read endpoints, never imports an LLM).

Two checks:
  1. every read-path module (engine, recall, freshness, bootstrap, anchors, rules, db,
     hook, bridge) is LLM-free, full stop;
  2. each adapter's only reference to the seam is a lazy import inside a function, not
     a top-level one (a module-level `from recall.llm import ...` would pull an LLM into
     the read path on every CLI invocation / dashboard request).

Source-text based (not import-based) so it also catches a lazy `import anthropic`
buried inside a function — the same structural discipline as 360's
viewer-no-editor-import guard.
"""

from __future__ import annotations

import re
from pathlib import Path

_RECALL_DIR = Path(__file__).resolve().parent.parent / "recall"

# the seam: the only modules that may reach a model unconditionally
_SEAM = {"power.py", "llm.py"}
# the adapters expose Power Mode (CLI / dashboard) and may reach the seam — lazily only
_ADAPTER = {"cli.py", "dashboard.py"}

# things that mean "this module reached an LLM": the anthropic SDK, or the seam modules
_FORBIDDEN_PATTERNS = [
    re.compile(r"^\s*import\s+anthropic\b", re.MULTILINE),
    re.compile(r"^\s*from\s+anthropic\b", re.MULTILINE),
    re.compile(r"^\s*(from|import)\s+ollama\b", re.MULTILINE),
    re.compile(r"from\s+recall\.llm\b", re.MULTILINE),
    re.compile(r"from\s+recall\s+import\s+[^\n]*\bllm\b", re.MULTILINE),
]


def test_read_path_modules_reach_no_llm():
    """Every module except the seam + the CLI adapter must be totally LLM-free."""
    offenders: list[str] = []
    for path in _RECALL_DIR.glob("*.py"):
        if path.name in _SEAM or path.name in _ADAPTER:
            continue
        src = path.read_text(encoding="utf-8")
        for pat in _FORBIDDEN_PATTERNS:
            if pat.search(src):
                offenders.append(f"{path.name} matches {pat.pattern!r}")
    assert not offenders, (
        "the recall read path must stay LLM-free — only power.py/llm.py (the seam) "
        f"and cli.py (lazily) may reach a model. Offenders: {offenders}"
    )


def test_adapters_reach_the_seam_only_lazily():
    """The adapters (cli.py, dashboard.py) may offer Power Mode, but every llm/power
    import must be INSIDE a function, never at module top level — else plain `recall
    "<query>"`, or merely serving the dashboard page, would drag in an LLM."""
    for name in ("cli.py", "dashboard.py"):
        src = (_RECALL_DIR / name).read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), start=1):
            if re.match(r"\s*(from\s+recall\.llm|from\s+recall\.power|import\s+anthropic|from\s+anthropic)\b", line):
                indent = len(line) - len(line.lstrip())
                assert indent > 0, (
                    f"{name} line {i} imports the LLM seam at module top level — it must "
                    f"be a lazy import inside the power function so the read path stays "
                    f"LLM-free: {line!r}"
                )


def test_the_seam_modules_actually_exist_and_are_covered():
    """Guard the guard: if power.py/llm.py get renamed, _ALLOWED must follow, or this
    test silently protects nothing. llm.py exists now; power.py arrives in STEP 6."""
    assert (_RECALL_DIR / "llm.py").exists()
    # connect.py is the config, not the seam — it must NOT import an LLM either.
    connect_src = (_RECALL_DIR / "connect.py").read_text(encoding="utf-8")
    assert "import anthropic" not in connect_src

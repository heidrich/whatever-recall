"""Drift-guard: no test may go BLIND by skipping/returning on `code_symbols == 0`.

Twice now (launch-audit 2026-06-13, then the pre-launch sweep) the same anti-pattern
shipped a broken tree-sitter parser past a green suite: a test that does
`if code_symbols == 0: pytest.skip(...)` / `: return` can't distinguish "the [codemap]
extra is genuinely absent" from "the extra is installed but the parser silently yields
0 symbols" — the second is a buyer-breaking regression that MUST fail, not skip.

The correct shape is `_require_codemap()` (importorskip the extra) THEN `assert
code_symbols > 0`. This guard fails the build if the blind shape reappears anywhere in
tests/, so the lesson is structural, not a thing a reviewer has to remember.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).parent

# a skip/return guarded by a zero-symbol / zero-code-node check, on ONE or TWO lines
_BLIND = re.compile(
    r"""(code_symbols|by_kind"?\)?\.get\(\s*["']code-symbol["']\s*,\s*0\)|code-symbol["']\s*,\s*0\))"""
    r"""[^\n]*==\s*0[^\n]*\n?\s*(pytest\.skip|return)\b""",
    re.VERBOSE,
)

# this guard file and the helper's own docstring legitimately mention the pattern
_ALLOW = {"test_no_blind_codemap_skips.py"}


def test_no_test_skips_blindly_on_zero_code_symbols():
    offenders: list[str] = []
    for f in TESTS.glob("test_*.py"):
        if f.name in _ALLOW:
            continue
        text = f.read_text(encoding="utf-8")
        for m in _BLIND.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{f.name}:{line}")
    assert not offenders, (
        "blind code_symbols==0 skip/return found — use `_require_codemap()` then "
        "`assert code_symbols > 0` so a silent-zero parser FAILS, not skips: "
        + ", ".join(offenders)
    )

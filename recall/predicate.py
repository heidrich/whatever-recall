"""Arrow 1 (SHIPPED) — the predicate: store a re-runnable CHECK, not a bare conclusion.

north-star §2: recall's read path is 0-token because the expensive "why" is written
once at stamp time. But a "why" written at the moment of the change is exactly when
the AI is most likely confidently *wrong* (consequences unseen), and freshness
(`freshness.py`) only ever measures whether the FILE MOVED — never whether the CLAIM
still HOLDS. So a wrong-from-start "why" on an unmoved file is certified 🟢 forever
(GAP A), and a still-true claim on a moved file is falsely 🟡 (GAP B). Proven empirically
in tests/test_freshness_predicate_gap.py.

The fix is to give a claim-bearing node a PREDICATE: a tiny, deterministic, re-runnable
derivation that can be re-confirmed in <1 ms with no model. The AI's real edge over a
human is not recall — it is *cheap re-verification*. A predicate turns "trust this why"
into "re-check this why, free, every commit."

This module is the engine for that check: a PURE FUNCTION with no index/schema/CLI
coupling (the power-seam guard scans it), so the evaluator stays model-free and testable
in isolation. The wiring it once deferred is now SHIPPED (ADR-039, "predicate = epistemic
trust layer"): the check is stored in a `predicate` column on nodes, captured at write time
from a `Recall-predicate:` commit trailer or `stamp(--predicate)` (nudged, never mandatory),
re-run by `freshness.evaluate_predicate` on every freshen, and folded into the drift ampel
by `freshness.merge_signal` — where BROKEN wins outright and an unscoped CONFIRMED is
downgraded to UNKNOWN so a whole-file check can never raise a false 🟢. The verdict rides
the `brief()` return and is rendered as a loud 🔴 trust-flag on every pushed fact.

PREDICATE GRAMMAR (minimal on purpose — reuses recall's grep-able anchor philosophy,
no new expression language): a claim is checked against the CURRENT text of its pinned
file.
    contains:<regex>   the claim HOLDS iff the pattern is present in the file
    absent:<regex>     the claim HOLDS iff the pattern is NOT present in the file
A predicate may chain several clauses with ' && ' — ALL must hold (a claim like "login
lowercases AND never logs the password" = `contains:u\\.lower\\(\\) && absent:print\\(.*password`).

VERDICTS:
    CONFIRMED 🟢  every clause holds — the claim is re-verified true, right now
    BROKEN    🔴  a clause fails — the code no longer matches the claim (the case
                  SHA-drift is structurally blind to)
    UNKNOWN   ⚪  no predicate, no file, or an unparseable clause — unverifiable, so we
                  fall back to drift and NEVER raise a false alarm (mirrors freshness's
                  "can't prove drift => fresh" discipline)

Stdlib only (re + pathlib). No LLM, no git, no tokens — safe in the read path (the
power-seam guard scans this module too).
"""

from __future__ import annotations

import re
from pathlib import Path

# Verdicts, ordered worst-last so a max()-style merge picks the loudest signal.
CONFIRMED = "confirmed"  # 🟢 the check still holds — claim re-verified true
BROKEN = "broken"        # 🔴 a clause failed — code no longer matches the claim
UNKNOWN = "unknown"      # ⚪ unverifiable — fall back to drift, never a false alarm

_VERDICT_RANK = {CONFIRMED: 0, UNKNOWN: 1, BROKEN: 2}

_CLAUSE = re.compile(r"^\s*(contains|absent)\s*:\s*(.+?)\s*$", re.DOTALL)

# READ-PATH SAFETY BOUNDS (adversarial review 2026-06-15). evaluate_predicate runs in the
# token-free read path — freshen() calls it per claim on every dashboard tick (~1.5s poll)
# and every post-commit hook. Two self-inflicted-DoS / footgun seams the review reproduced:
#
#   ReDoS — a stored predicate like `contains:(a+)+$` parses fine, passes stamp()'s
#   length bound (it's short), and then catastrophic-backtracks for >30s on a 40-char file,
#   wedging the read path. A Python-thread timeout does NOT help (re.search holds the GIL).
#   So we (a) cap the text we ever feed to .search() and (b) reject the known catastrophic
#   nested-quantifier shapes at parse time. Predicates are author/AI-written (local trust),
#   never network-attacker-supplied, so a write-time screen + read-time length cap is the
#   right weight — not a heavyweight process-isolated matcher.
#
#   Path traversal — file_rel comes from the node's stored file_path (a raw `--file` /
#   MCP `file` / slash-anchor), never validated. `Path(repo)/'../secret'` reads OUTSIDE the
#   repo on every freshen. We confine the resolved path to the repo and return UNKNOWN (never
#   read, never a false alarm) if it escapes — symmetric with how a missing file is handled.
_MAX_SCOPE_CHARS = 200_000  # a line-scoped check never needs more; truncation is safe
# nested unbounded quantifiers — (x+)+, (x*)*, (x+)*, (x*)+ — the classic ReDoS shapes.
_REDOS_SHAPE = re.compile(r"\([^()]*[+*]\)[+*]")


class _Clause:
    """One `contains:<re>` / `absent:<re>` test. `want_present` is what makes the
    claim HOLD: True for contains (pattern must be there), False for absent."""

    __slots__ = ("want_present", "pattern", "raw")

    def __init__(self, want_present: bool, pattern: "re.Pattern[str]", raw: str):
        self.want_present = want_present
        self.pattern = pattern
        self.raw = raw

    def holds(self, text: str) -> bool:
        found = self.pattern.search(text) is not None
        return found is self.want_present


def parse_predicate(expr: str | None) -> list[_Clause] | None:
    """Parse a predicate string into clauses. Returns None if there is nothing to
    check (empty) or any clause is malformed — the caller treats None as UNKNOWN and
    falls back to drift, so a typo can never silently certify a claim as confirmed."""
    if not expr or not expr.strip():
        return None
    clauses: list[_Clause] = []
    for part in expr.split(" && "):
        m = _CLAUSE.match(part)
        if not m:
            return None  # unparseable -> UNKNOWN, never a false CONFIRMED
        op, body = m.group(1), m.group(2)
        if _REDOS_SHAPE.search(body):
            return None  # catastrophic-backtracking shape -> reject as if malformed (UNKNOWN)
        try:
            pat = re.compile(body)
        except re.error:
            return None
        clauses.append(_Clause(op == "contains", pat, part.strip()))
    return clauses or None


def _slice_lines(text: str, line_range: tuple[int, int] | None) -> str:
    """Narrow `text` to a 1-based, inclusive line range — the pinned symbol's span.

    SCOPING IS WHAT MAKES THE PREDICATE TRUSTWORTHY. A whole-file `contains:u\\.lower\\(\\)`
    is CONFIRMED if the pattern lives in ANY function — including one the claim isn't
    about — which is a false confirm (proven in test_predicate.py). recall already knows
    each code node's file_path + line, so a claim about `login` can be checked against
    login's lines, not the whole file.

    Degrades gracefully, never crashes: a missing / inverted / out-of-bounds range falls
    back to the whole text (UNKNOWN-style safety — a stale end-line can't raise)."""
    if not line_range:
        return text
    start, end = line_range
    if start is None or end is None or start > end:
        return text
    lines = text.splitlines()
    lo = max(1, start) - 1          # 1-based inclusive -> 0-based slice start
    hi = min(len(lines), end)       # clamp: a stale end past EOF can't over-read
    if lo >= hi:
        return text                 # nothing sensible to slice -> whole file
    return "\n".join(lines[lo:hi])


def evaluate_predicate(
    repo: str | Path,
    file_rel: str | None,
    expr: str | None,
    line_range: tuple[int, int] | None = None,
) -> str:
    """Re-run a claim's predicate against the CURRENT file text. Pure, deterministic.

    `line_range` (1-based, inclusive) scopes the check to the pinned symbol's span so a
    pattern in an unrelated function can't false-confirm the claim; omit it to check the
    whole file. UNKNOWN (never a false alarm) when there's nothing to check: no predicate,
    no pinned file, the file is gone from disk, or the predicate is malformed. Otherwise
    CONFIRMED iff every clause holds, BROKEN the moment one fails — independent of whether
    the file's SHA moved (that's freshness's job; this answers the orthogonal 'does the
    claim still hold' question).
    """
    clauses = parse_predicate(expr)
    if clauses is None or not file_rel:
        return UNKNOWN
    # Path containment (security): the predicate must only ever read a file INSIDE the repo.
    # An absolute or '..'-escaping file_rel resolves outside repo_root — return UNKNOWN (never
    # read, never a false alarm), symmetric with the missing-file case below.
    try:
        repo_root = Path(repo).resolve()
        path = (repo_root / file_rel).resolve()
        path.relative_to(repo_root)  # ValueError if path is outside the repo
    except (ValueError, OSError):
        return UNKNOWN
    if not path.is_file():
        return UNKNOWN  # can't read -> can't prove broken; defer to drift
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return UNKNOWN
    scope = _slice_lines(text, line_range)
    # ReDoS length cap: a line-scoped check never needs the whole of a huge file; bounding
    # the input is the cheap half of the defence (the parse-time shape screen is the other).
    if len(scope) > _MAX_SCOPE_CHARS:
        scope = scope[:_MAX_SCOPE_CHARS]
    return CONFIRMED if all(c.holds(scope) for c in clauses) else BROKEN


def merge_signal(drift_level: str, verdict: str) -> str:
    """How a predicate verdict combines with the SHA-drift level into one light.

    The predicate is the STRONGER signal because it speaks to truth, not movement:
      • BROKEN always wins — a failed check means the claim is wrong NOW, louder than
        any file-movement color (this is what catches GAP A).
      • CONFIRMED overrides 🟡 COMMITTED down to 🟢 fresh — a still-holding check means an
        unrelated *committed* file move is NOT a real staleness signal (this fixes GAP B's
        false 🟡). But it does NOT touch 🟠 UNCOMMITTED: that is a LIVENESS signal (the file
        is being edited RIGHT NOW), categorically different from the staleness 🟡. A
        predicate re-verifies ONE clause; it cannot vouch for the rest of the live diff (the
        edit may have changed something the predicate doesn't cover). So an open working-tree
        edit keeps its 🟠 — exactly the warning brief() leads with before you edit.
        (Adversarial review 2026-06-15: CONFIRMED used to suppress 🟠 too, hiding a genuine
        dirty-tree edit when the predicate-bearing claim was the file's only pinned node.)
      • UNKNOWN defers entirely to drift — we learned nothing, so don't change the light.
    Returned as a freshness level (fresh/committed/uncommitted) plus the literal
    'broken', so existing consumers keep working and only gain the new red.
    """
    if verdict == BROKEN:
        return BROKEN
    if verdict == CONFIRMED:
        # quiet a committed-drift false alarm, but never a live uncommitted-edit warning
        return drift_level if drift_level == "uncommitted" else "fresh"
    return drift_level  # UNKNOWN -> whatever drift said

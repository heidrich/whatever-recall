"""THE needle-rank judge — one copy (review follow-up 2026-06-11).

Four experiment scripts each grew their own ``_hit_rank``; the signatures drifted
(result dicts vs node ids vs candidate dicts) but the MEANING never did:
"at which 1-based rank does the first item matching the needle sit?".
The meaning lives here; a caller only says how to turn ITS item into text.

Usage:
    from judge import hit_rank, code_rank, node_text
    rank = hit_rank(results, "drift|traffic light")             # result dicts
    rank = hit_rank(node_ids, needle, text=node_text(idx))      # bare node ids
    rank = code_rank(items, case["accept"])                     # exact (file,symbol)
"""
from __future__ import annotations


def _norm(p: str | None) -> str:
    return (p or "").replace("\\", "/")


def hit_rank(items, needle: str, text=None) -> int:
    """1-based rank of the first item whose text contains ANY ``a|b|c`` alternative
    of the needle; 0 = miss. ``text(item)`` builds the searchable blob — the default
    reads a result dict's title + symbol + file."""
    alts = [a.strip().lower() for a in str(needle).split("|") if a.strip()]
    if text is None:
        def text(it):  # noqa: E306 — the default adapter for engine result dicts
            return f"{it.get('title') or ''} {it.get('symbol') or ''} {_norm(it.get('file'))}"
    for i, it in enumerate(items, 1):
        blob = str(text(it)).lower()
        if any(a in blob for a in alts):
            return i
    return 0


def node_text(idx):
    """A ``text=`` adapter for callers that rank bare node ids (sim/sweep scripts)."""
    def text(nid):
        if isinstance(nid, dict):           # sweep candidates carry {"id": …}
            nid = nid["id"]
        row = idx.db.execute(
            "SELECT title, symbol, file_path FROM nodes WHERE id=?", (nid,)).fetchone()
        return " ".join(x or "" for x in row) if row else ""
    return text


def code_rank(items, accept) -> int:
    """1-based rank of the first code item EXACTLY matching an accepted
    (file, symbol) pair; 0 = miss. Substring judging measured false hits
    (needle ``dashboard.py`` matched tests/test_dashboard.py) — never go back."""
    pairs = {(_norm(a["file"]), a["symbol"]) for a in accept}
    for i, it in enumerate(items, 1):
        if (_norm(it.get("file")), it.get("symbol")) in pairs:
            return i
    return 0

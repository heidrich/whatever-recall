"""The hard pre-edit gate (owner 2026-06-17: "wenn DU als Dev recall nutzt, dann MÜSSEN
die Agents recall nutzen — damit wird recall noch schärfer").

recall's pre-edit briefing used to be a SUGGESTION: the PreToolUse hook appended the
file's why/landmines/tasks as context and trusted the AI to read it. Measured (the
silent-contract fleet, 2026-06-17): even WITH the warning surfaced, ~60% of agents still
made the contradicting edit — failure-to-ACT, not failure-to-call. The fix is to make the
gate REAL: when recall has knowledge for a file, the first edit is DENIED with that
knowledge as the reason, and the agent must explicitly `recall ack <file>` (proving it saw
the briefing) before the edit is allowed. No ack → no edit. recall is now IN the edit path,
not beside it.

The ack is per-file, time-boxed (a short working window), stored next to the index so it
is per-project and never leaks across repos. Acking is cheap and deterministic (no model).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# How long an ack covers a file before recall re-gates it. Long enough for a working
# burst of edits to the same file, short enough that a new session/new intent re-briefs.
ACK_TTL_S = 15 * 60


def _ack_path(mind_dir: Path) -> Path:
    return Path(mind_dir) / "edit_acks.json"


def _norm(repo: Path, file_path: str) -> str:
    """A stable per-repo key for a file: repo-relative, forward slashes, lowercased on
    case-insensitive platforms. Falls back to the raw string if it isn't under the repo."""
    try:
        rel = Path(file_path).resolve().relative_to(Path(repo).resolve()).as_posix()
    except (ValueError, OSError):
        rel = str(file_path).replace("\\", "/")
    return rel.lower() if os.name == "nt" else rel


def _load(mind_dir: Path) -> dict[str, float]:
    try:
        data = json.loads(_ack_path(mind_dir).read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save(mind_dir: Path, acks: dict[str, float]) -> None:
    try:
        Path(mind_dir).mkdir(parents=True, exist_ok=True)
        _ack_path(mind_dir).write_text(json.dumps(acks), encoding="utf-8")
    except OSError:
        pass


def is_acked(mind_dir: Path, repo: Path, file_path: str, *, now: float | None = None) -> bool:
    """True if this file was ack'd within the TTL (so an edit may proceed)."""
    now = time.time() if now is None else now
    acks = _load(mind_dir)
    ts = acks.get(_norm(repo, file_path))
    return ts is not None and (now - ts) < ACK_TTL_S


def ack(mind_dir: Path, repo: Path, file_path: str, *, now: float | None = None,
        log=None) -> None:
    """Record that the briefing for this file was seen — unlocks the next edit(s) within
    the TTL. Also prunes expired entries so the store stays small.

    Optional `log` callback (workstream C): a best-effort closure the caller passes to record
    the ack as a usage event in ITS own store. editgate stays dependency-free (it never imports
    the index); instrumentation must never break the ack, so a failing callback is swallowed."""
    now = time.time() if now is None else now
    acks = _load(mind_dir)
    acks = {k: v for k, v in acks.items() if (now - v) < ACK_TTL_S}  # prune expired
    acks[_norm(repo, file_path)] = now
    _save(mind_dir, acks)
    if log is not None:
        try:
            log()
        except Exception:
            pass  # the ack already succeeded — instrumentation is never load-bearing


def clear(mind_dir: Path, repo: Path, file_path: str | None = None) -> None:
    """Drop one file's ack (or all). Used after a stamp changes a file's knowledge so the
    next edit re-briefs against the new truth."""
    if file_path is None:
        _save(mind_dir, {})
        return
    acks = _load(mind_dir)
    acks.pop(_norm(repo, file_path), None)
    _save(mind_dir, acks)

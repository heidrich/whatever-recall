"""Adapter B — the IDE hook (Claude Code).

Write-time in, read-time out, no extra step:

  PreToolUse(Edit/Write)  -> recall() on the edited file's anchors, inject the
                             most relevant past lesson as context BEFORE the edit.
  Stop / PostToolUse      -> parse the latest commit message for Recall-* trailers
                             and stamp it (the self-sustaining loop).

Reads a Claude-Code hook JSON object on stdin, writes a hook JSON object on stdout.
It respects rules.md (surface_on / silence_floor) so it never turns into Clippy:
below the floor it injects nothing.

Wire it in .claude/settings.json, e.g.:
  { "hooks": {
      "PreToolUse": [{ "matcher": "Edit|Write|MultiEdit",
        "hooks": [{ "type": "command", "command": "python -m adapters.hook" }] }],
      "Stop": [{ "hooks": [{ "type": "command", "command": "python -m adapters.hook" }] }]
  } }
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# The CLI already solves index location + utf-8 console; reuse it.
from recall.cli import MIND_DIR, _find_repo, _index_path
from recall.engine import Index


def _read_event() -> dict[str, Any]:
    # Broad catch on purpose: a hook must never break the host tool. None stdin
    # (pythonw / detached launchers), a broken pipe (OSError), or malformed JSON
    # all degrade to an empty event, never a traceback.
    try:
        if sys.stdin is None:
            return {}
        raw = sys.stdin.read()
        return json.loads(raw) if raw and raw.strip() else {}
    except Exception:
        return {}


def _open_index(cwd: str | None) -> tuple[Index | None, Path]:
    repo = _find_repo(cwd or ".")
    idx_path = _index_path(repo)
    if not idx_path.exists():
        return None, repo
    return Index.open(idx_path, repo=repo), repo


def _edited_path(event: dict[str, Any]) -> str | None:
    ti = event.get("tool_input") or {}
    p = ti.get("file_path") or ti.get("path") or ti.get("notebook_path")
    return p if isinstance(p, str) else None  # ignore non-string file_path


# ---------------------------------------------------------------- PreToolUse
def handle_pre_edit(event: dict[str, Any]) -> dict[str, Any]:
    """Brief the AI on the file it is about to edit (ADR-018).

    The hook is exactly the moment the MUST-CHECK in rules.md exists for: before an
    edit, recall injects the file's *pre-edit briefing* — its open tasks (standing
    intent), what BREAKS if it changes (blast radius), WHY it is the way it is, and the
    most relevant past lessons. So an AI cannot silently undo a decision it never saw.
    All read-only, token-free, no model (idx.brief() + idx.recall() are pure SQL)."""
    idx, _repo = _open_index(event.get("cwd"))
    if idx is None:
        return {}
    path = _edited_path(event)
    if not path:
        return {}
    if "edit" not in idx.rules.surface_on and "task_start" not in idx.rules.surface_on:
        return {}

    # The briefing: file-scoped, deterministic (open tasks + blast radius + why).
    try:
        brief = idx.brief(path)
    except Exception:
        brief = None

    # The lessons: relevance-ranked recall on the path + the edit's own text. edit_context
    # boosts the matching facet. Keep the path intact (compound anchors like workspace-api
    # match) AND add a segment-split form so individual words count too.
    ti = event.get("tool_input") or {}
    edit_text = " ".join(
        str(ti.get(k, "")) for k in ("new_string", "content", "old_string")
    )[:2000]
    query = f"{path} {path.replace('/', ' ')} {edit_text}"
    res = idx.recall(query, edit_context=path, topk=2, consumer="hook")

    context = _format_pre_edit(path, brief, res)
    if not context:
        return {}  # nothing to say (unknown file + below the floor) — stay silent

    # Claude Code injects `additionalContext` from a PreToolUse hook.
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }


def _format_pre_edit(path: str, brief: dict[str, Any] | None,
                     res: dict[str, Any]) -> str:
    """The injected block: lead with standing intent (open tasks) and the blast radius,
    then the why, then relevance-ranked lessons. Compact — it rides every edit, so it
    must earn its space; stays silent when there is genuinely nothing to surface."""
    lines: list[str] = []
    b = brief if (brief and brief.get("known")) else None
    if b:
        drift = {
            "committed": "⚠ this file changed since some of its knowledge was stamped — verify before trusting it",
            "uncommitted": "⚠ this file has uncommitted edits right now",
        }.get(b.get("drift"))
        if drift:
            lines.append(drift)
        tasks = b.get("open_tasks") or []
        if tasks:
            lines.append("📋 OPEN TASKS on this file — standing intent, read first:")
            for t in tasks[:4]:
                lines.append(f"  - {t['title']}")
        breaks = b.get("breaks") or []
        if breaks:
            shown = ", ".join(x["file"] for x in breaks[:5])
            more = f" (+{len(breaks) - 5} more)" if len(breaks) > 5 else ""
            lines.append(f"⚠ {len(breaks)} file(s) depend on this — changing it may break: {shown}{more}")
        why = b.get("why") or []
        if why:
            lines.append("WHY it is the way it is:")
            for w in why[:3]:
                sha = f" (sha {w['sha']})" if w.get("sha") else ""
                lines.append(f"  - [{w['kind']}] {w['title']}{sha}")

    # The relevance-ranked lessons (the original behaviour), only above the floor.
    if not res.get("silenced") and res.get("results"):
        lines.append("📌 most relevant past lessons:")
        for r in res["results"]:
            lines.append(f"  • {r['title']}")
            if r["why"]:
                lines.append(f"    why: {r['why']}")
            stale = any(not e["verified"] for e in r["relation"])
            if r["sha"] or stale:
                warn = " ⚠ check freshness" if stale else ""
                sha = f"sha {r['sha']}" if r["sha"] else ""
                lines.append(f"    ({sha}{warn})".replace("()", ""))

    if not lines:
        return ""
    header = f"[recall · pre-edit briefing for {path}] — read-only, 0 tokens"
    return header + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------- Stop
def handle_post_commit(event: dict[str, Any]) -> dict[str, Any]:
    """Stamp the latest commit if it carries Recall-* trailers."""
    idx, repo = _open_index(event.get("cwd"))
    if idx is None:
        return {}
    msg = _latest_commit_message(repo)
    if not msg or "Recall-anchors:" not in msg:
        return {}
    sha = _latest_commit_sha(repo)
    result = idx.stamp_from_commit(msg, sha)
    if result is None:
        return {}
    # Surface a tiny confirmation in the transcript (non-blocking).
    verb = "merged into" if result["action"] == "MERGE" else "stamped"
    target = result.get("into", f"#{result.get('node_id')}")
    return {
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": f"🧠 recall {verb} {target} (sha {sha[:7]})",
        }
    }


def _latest_commit_message(repo: Path) -> str:
    return _git(repo, "log", "-1", "--format=%B")


def _latest_commit_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip() or "unknown"


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout
    except OSError:
        return ""


# --------------------------------------------------------------------- router
def route(event: dict[str, Any]) -> dict[str, Any]:
    name = event.get("hook_event_name") or event.get("hookEventName") or ""
    tool = event.get("tool_name", "")
    if name == "PreToolUse" and tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return handle_pre_edit(event)
    if name in ("Stop", "SubagentStop", "PostToolUse"):
        return handle_post_commit(event)
    return {}


# ----------------------------------------------------- git post-commit install
# The "real" write-time path: a git post-commit hook that auto-stamps every commit
# carrying Recall-* trailers, AND re-freshens the drift ampel. Installable from the
# CLI (`recall hook --install`) or the dashboard toggle ("auto-stamp on commit").
#
# We write a tiny shell stub into .git/hooks/post-commit that calls
# `recall stamp-commit` (a token-free, model-free CLI command). The stub carries a
# marker line so we can detect/uninstall OUR hook without clobbering a user's own.

_HOOK_MARKER = "# >>> recall auto-stamp (post-commit) >>>"
_HOOK_END = "# <<< recall auto-stamp (post-commit) <<<"


def _post_commit_stub() -> str:
    """The shell body we write. POSIX sh — git runs hooks via sh even on Windows
    (Git for Windows ships an sh). Calls the installed `recall` entry point; if the
    CLI isn't on PATH it falls back to `python -m recall.cli`. Fails silent so a
    commit is never blocked by a recall hiccup (a hook must never break the host)."""
    return (
        "#!/bin/sh\n"
        f"{_HOOK_MARKER}\n"
        "# Auto-stamp this commit into the recall index (token-free, offline).\n"
        "# Installed by `recall hook --install`. Safe to remove.\n"
        'if command -v recall >/dev/null 2>&1; then\n'
        '  recall stamp-commit >/dev/null 2>&1 || true\n'
        'else\n'
        '  python -m recall.cli stamp-commit >/dev/null 2>&1 || true\n'
        'fi\n'
        f"{_HOOK_END}\n"
    )


# The pre-commit counterpart (Wave D, ADR-021): warn — never block — when the staged
# change touches load-bearing code. Same stub shape, different marker + body.
_PRE_MARKER = "# >>> recall pre-commit warning (pre-commit) >>>"
_PRE_END = "# <<< recall pre-commit warning (pre-commit) <<<"


def _pre_commit_stub() -> str:
    """Calls `recall precommit-check`, which prints a warning when a staged file is
    load-bearing and ALWAYS exits 0 — a pre-commit hook that exited non-zero would
    BLOCK the commit, and a memory tool must never get between you and git. We force
    `exit 0` here too as a belt-and-braces guard."""
    return (
        "#!/bin/sh\n"
        f"{_PRE_MARKER}\n"
        "# Warn (never block) when staged files are load-bearing. Installed by\n"
        "# `recall hook --install --pre-commit`. Safe to remove. Always exits 0.\n"
        'if command -v recall >/dev/null 2>&1; then\n'
        '  recall precommit-check || true\n'
        'else\n'
        '  python -m recall.cli precommit-check || true\n'
        'fi\n'
        "exit 0\n"
        f"{_PRE_END}\n"
    )


def hook_status(repo: Path) -> dict[str, Any]:
    """Is the recall post-commit hook installed in this repo? (for the dashboard).

    Also reports the pre-commit warning hook (Wave D) under `pre_commit` so the
    dashboard can show both toggles without a second round-trip."""
    hooks_dir = repo / ".git" / "hooks"
    target = hooks_dir / "post-commit"
    pre = hooks_dir / "pre-commit"
    has_git = (repo / ".git").exists()
    installed = target.exists() and _HOOK_MARKER in _safe_read(target)
    pre_installed = pre.exists() and _PRE_MARKER in _safe_read(pre)
    return {"has_git": has_git, "installed": installed, "path": str(target),
            "pre_commit": pre_installed, "pre_commit_path": str(pre)}


def install_post_commit(repo: Path) -> dict[str, Any]:
    """Install (idempotently) the recall post-commit hook. Returns a status dict.

    If a non-recall post-commit hook already exists we DON'T overwrite it — we tell
    the caller so the UI can warn instead of silently clobbering the user's hook."""
    if not (repo / ".git").exists():
        return {"ok": False, "reason": "no .git in this project"}
    hooks_dir = repo / ".git" / "hooks"
    target = hooks_dir / "post-commit"
    existing = _safe_read(target)
    if existing and _HOOK_MARKER not in existing:
        return {"ok": False, "reason": "a different post-commit hook already exists",
                "path": str(target)}
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(_post_commit_stub(), encoding="utf-8", newline="\n")
        # make it executable where that matters (POSIX); harmless no-op on Windows.
        try:
            import os
            import stat
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass
        return {"ok": True, "installed": True, "path": str(target)}
    except OSError as e:
        return {"ok": False, "reason": str(e), "path": str(target)}


def uninstall_post_commit(repo: Path) -> dict[str, Any]:
    """Remove ONLY our hook (recognised by the marker). Leaves a foreign hook alone."""
    target = repo / ".git" / "hooks" / "post-commit"
    body = _safe_read(target)
    if not body:
        return {"ok": True, "installed": False}
    if _HOOK_MARKER not in body:
        return {"ok": False, "reason": "the post-commit hook is not recall's", "path": str(target)}
    try:
        target.unlink()
        return {"ok": True, "installed": False, "path": str(target)}
    except OSError as e:
        return {"ok": False, "reason": str(e), "path": str(target)}


def install_pre_commit(repo: Path) -> dict[str, Any]:
    """Install (idempotently) the recall pre-commit WARNING hook (Wave D, ADR-021).

    Same guarantees as the post-commit installer: never overwrites a foreign hook,
    sets the executable bit on POSIX. The hook only warns — it cannot block a commit."""
    if not (repo / ".git").exists():
        return {"ok": False, "reason": "no .git in this project"}
    hooks_dir = repo / ".git" / "hooks"
    target = hooks_dir / "pre-commit"
    existing = _safe_read(target)
    if existing and _PRE_MARKER not in existing:
        return {"ok": False, "reason": "a different pre-commit hook already exists",
                "path": str(target)}
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(_pre_commit_stub(), encoding="utf-8", newline="\n")
        try:
            import stat
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass
        return {"ok": True, "installed": True, "path": str(target)}
    except OSError as e:
        return {"ok": False, "reason": str(e), "path": str(target)}


def uninstall_pre_commit(repo: Path) -> dict[str, Any]:
    """Remove ONLY our pre-commit hook (recognised by its marker). Foreign hook untouched."""
    target = repo / ".git" / "hooks" / "pre-commit"
    body = _safe_read(target)
    if not body:
        return {"ok": True, "installed": False}
    if _PRE_MARKER not in body:
        return {"ok": False, "reason": "the pre-commit hook is not recall's", "path": str(target)}
    try:
        target.unlink()
        return {"ok": True, "installed": False, "path": str(target)}
    except OSError as e:
        return {"ok": False, "reason": str(e), "path": str(target)}


def stamp_latest_commit(repo: Path) -> dict[str, Any]:
    """Stamp HEAD if it carries Recall-* trailers, then re-freshen drift. Token-free,
    no model — this is what the post-commit hook and the dashboard auto-index both
    call. Returns {stamped, action, into, freshened}. Safe to call any time."""
    repo = Path(repo)
    idx_path = _index_path(repo)
    if not idx_path.exists():
        return {"stamped": False, "reason": "no index"}
    idx = Index.open(idx_path, repo=repo)
    try:
        out: dict[str, Any] = {"stamped": False}
        msg = _latest_commit_message(repo)
        if msg and "Recall-anchors:" in msg:
            sha = _latest_commit_sha(repo)
            result = idx.stamp_from_commit(msg, sha)
            if result is not None:
                out = {"stamped": True, "action": result.get("action"),
                       "into": result.get("into") or result.get("node_id")}
        try:
            idx.freshen()  # keep the ampel honest after every commit
            out["freshened"] = True
        except Exception:
            out["freshened"] = False
        return out
    finally:
        idx.db.close()


def _safe_read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    try:
        out = route(_read_event())
    except Exception:
        # A hook must never break the host tool. Fail silent. (Covers _read_event
        # too — it's now inside the guard, so None stdin / broken pipe can't crash.)
        out = {}
    if out:
        sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

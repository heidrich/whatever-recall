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
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# The CLI already solves index location + utf-8 console; reuse it.
from recall.cli import _find_repo, _index_path
from recall.engine import Index

# Path-MENTION extraction for the situational push (workstream A). This is a path-STRING
# regex ONLY — deliberately NOT an AST/tokenize/semantic parser (the import drift-guard in
# tests/test_push_situational_0618.py fails the build if this module ever imports a parser):
# a file-path-shaped token (a/b/c.ext or bare name.ext). Unknown mentions are filtered later
# by brief()'s known flag, so a false match costs nothing.
_PATH_MENTION = re.compile(r"(?<![\w./-])([\w][\w./-]*\.[A-Za-z][A-Za-z0-9]{0,4})(?![\w/])")


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

    # THE HARD GATE (owner 2026-06-17): when recall has knowledge for this file, the edit
    # is DENIED until the agent has explicitly `recall ack`'d it — proving it saw the
    # briefing — rather than merely appending the briefing as skimmable context. Measured:
    # surfaced-but-optional briefings are ignored ~60% of the time (failure-to-act). An ack
    # is per-file + time-boxed; once ack'd (within the TTL) the edit passes and we fall back
    # to context injection (so a working burst on the same file isn't re-gated every edit).
    from recall import editgate

    mind = _index_path(_repo).parent  # …/.mind
    if editgate.is_acked(mind, _repo, path):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": context,
            }
        }
    reason = (
        context
        + "\n\n— recall GATE: you are editing a file recall has knowledge about. Read the "
        "briefing above, then run:\n"
        f"    recall ack {path}\n"
        "and retry the edit. This exists so a deliberate decision is never silently undone."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# ------------------------------------------------------------ UserPromptSubmit (workstream A)
def handle_user_prompt(event: dict[str, Any]) -> dict[str, Any]:
    """When the prompt names files recall knows, or matches stamped knowledge above the floor,
    PUSH a scoped situational block (landmines + live BROKEN trust-status + the relevant why)
    as additionalContext — 0 tool calls. NEVER denies (a prompt is not a mutation).

    Gated by surface_on 'prompt' AND the silence-floor/match INSIDE render_situational_block
    (owner decision: NOT Clippy): a prompt that names no known file and has no above-floor hit
    gets the static fallback, which we DON'T re-inject (it already rides the system prompt)."""
    idx, _repo = _open_index(event.get("cwd"))
    if idx is None:
        return {}
    if "prompt" not in idx.rules.surface_on:
        return {}
    prompt = event.get("prompt") or event.get("user_prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return {}
    mentions: list[str] = []
    for m in _PATH_MENTION.finditer(prompt):
        p = m.group(1)
        if p not in mentions:
            mentions.append(p)
    try:
        block = idx.render_situational_block(
            focus_file=mentions[0] if mentions else None,
            diff_files=mentions[1:5] or None,
            task=prompt[:2000],
        )
    except Exception:
        return {}
    # inject ONLY genuine situational content; if the renderer fell back to the repo-static
    # block (no above-floor match), stay silent — that block is already in the system prompt.
    if not block.startswith("## recall — situational memory"):
        return {}
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": block}}


# ------------------------------------------------------------- SessionStart (workstream A)
def handle_session_start(event: dict[str, Any]) -> dict[str, Any]:
    """Hand a fresh session the repo-static state block as additionalContext — the same memory
    sync-context writes into the instruction file, for harnesses that read SessionStart context
    but don't load the file. Gated by surface_on 'session_start'. Never denies, writes no file."""
    idx, _repo = _open_index(event.get("cwd"))
    if idx is None:
        return {}
    if "session_start" not in idx.rules.surface_on:
        return {}
    try:
        block = idx.render_state_block()
    except Exception:
        return {}
    if not block.strip():
        return {}
    return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": block}}


def _format_pre_edit(path: str, brief: dict[str, Any] | None,
                     res: dict[str, Any]) -> str:
    """The injected block: lead with standing intent (open tasks) and the blast radius,
    then the why, then relevance-ranked lessons. Compact — it rides every edit, so it
    must earn its space; stays silent when there is genuinely nothing to surface."""
    lines: list[str] = []
    b = brief if (brief and brief.get("known")) else None
    if b:
        # Landmines lead — the conscience signal (arrow 2). A past mistake marked on this
        # file must be the FIRST thing seen, louder than drift/tasks, so it can't be missed.
        warns = b.get("warns") or []
        if warns:
            lines.append("🔴 LANDMINES — past mistakes warn about this file; heed them before editing:")
            for w in warns[:4]:
                why = f" — {w['why']}" if w.get("why") else ""
                stale = " ⚠ check freshness" if w.get("drift") in ("committed", "uncommitted") else ""
                lines.append(f"  • [{w['kind']}] {w['title']}{why}{stale}")
        # A claim that FAILS its own re-check NOW is the loudest signal — louder than a
        # stale-SHA ⚠ — so it leads, right under the landmines (workstream B, arrow 1).
        if b.get("drift") == "broken":
            lines.append("🔴 a stamped claim about this file FAILS its re-check NOW — "
                         "treat its WHY as wrong until re-verified")
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
                # a why whose own predicate FAILS now is wrong until re-verified — render it
                # loud and show WHAT failed (verdict + predicate already ride the brief return).
                broke = w.get("drift") == "broken"
                flag = " 🔴 BROKEN — its own re-check FAILS now" if broke else ""
                lines.append(f"  - [{w['kind']}] {w['title']}{sha}{flag}")
                if broke and w.get("predicate"):
                    lines.append(f"    failing check: {w['predicate']}")

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
    """Stamp the latest commit if it carries Recall-* trailers, then re-sync the
    in-the-path state block into the repo's AI instruction file (the adoption fix)."""
    idx, repo = _open_index(event.get("cwd"))
    if idx is None:
        return {}
    msg = _latest_commit_message(repo)
    sha = _latest_commit_sha(repo)
    # 1) Stamp a trailer-bearing commit FIRST, so its predicate/why is in the DB before the
    #    re-check below runs (a brand-new BROKEN claim then shows on THIS commit, not next).
    result = idx.stamp_from_commit(msg, sha) if (msg and "Recall-anchors:" in msg) else None
    # 2) Re-freshen the drift ampel against the new HEAD, THEN (3) regenerate the in-the-path
    #    state block — CHAINED in that order (workstream B) so the pushed 🔴 BROKEN section is
    #    never one commit stale. The ADOPTION FIX (2026-06-17): the AI's system prompt always
    #    carries the live memory with no tool call. Both run on EVERY commit (an edit can break
    #    a claim with no trailer). Best-effort + non-blocking: a failure never breaks the hook.
    try:
        idx.freshen()  # keep the ampel honest BEFORE the block is rendered from it
    except Exception:
        pass
    try:
        import subprocess as _sp
        _sp.run([sys.executable, "-m", "recall.cli", "sync-context", "--quiet"],
                cwd=str(repo), stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=20)
    except Exception:
        pass
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
    if name == "UserPromptSubmit":
        return handle_user_prompt(event)
    if name == "SessionStart":
        return handle_session_start(event)
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
    """Two pre-commit steps, in order of authority:

    1. `recall check-leak` — the LEAK GUARD. If a staged brain file holds private
       notes it exits non-zero and we propagate that, ABORTING the commit. This is the
       one place a memory tool MUST get between you and git: shipping private knowledge
       is a security failure, not a style nit. Honors [share].block_raw_mind_commit
       (fail-closed: only an explicit `false` disables it).
    2. `recall precommit-check` — the load-bearing WARNING, which never blocks (forced
       `|| true`); a risky-file warning must not stop a legitimate commit.

    Installed by `recall hook --install --pre-commit`. Safe to remove."""
    return (
        "#!/bin/sh\n"
        f"{_PRE_MARKER}\n"
        "# 1) leak guard (BLOCKS): refuse a commit that stages a brain with private notes.\n"
        "# 2) load-bearing warning (never blocks). Installed by recall; safe to remove.\n"
        'if command -v recall >/dev/null 2>&1; then\n'
        '  recall check-leak || exit 1\n'
        '  recall precommit-check || true\n'
        'else\n'
        '  python -m recall.cli check-leak || exit 1\n'
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


# --------------------------------------------- opt-in client hook installer (workstream A)
# Wire recall's situational-push hooks into an AI client's settings.json. Sentinel-guarded,
# array-of-objects UPSERT: recall owns ONLY the entry whose command carries the sentinel; every
# foreign matcher object — and any foreign hook inside a matcher recall shares — is preserved.
_HOOK_SENTINEL = "# recall-managed"
_CLIENT_SETTINGS = {"claude": ".claude/settings.json"}  # cursor/windsurf flagged below
# event → matcher (None = no matcher, the whole-event shape Claude uses for prompt/session)
_CLIENT_EVENTS: dict[str, str | None] = {
    "PreToolUse": "Edit|Write|MultiEdit|NotebookEdit",
    "UserPromptSubmit": None,
    "SessionStart": None,
}


def _client_command() -> str:
    """The hook command we install — the absolute interpreter recall runs under (robust across
    `python`/`python3` machines) routing through adapters.hook, tagged with the sentinel (a shell
    comment, inert at runtime) so we can find/remove ONLY our own entry later."""
    return f"{sys.executable} -m adapters.hook  {_HOOK_SENTINEL}"


def _entry_is_recall(entry: dict) -> bool:
    return any(isinstance(h, dict) and _HOOK_SENTINEL in (h.get("command") or "")
               for h in (entry.get("hooks") or []))


def _entry_is_unmanaged_recallish(entry: dict) -> bool:
    """A hand-rolled recall-like hook (mentions adapters.hook) WITHOUT our sentinel — ambiguous,
    so we refuse to touch it rather than double-install."""
    for h in (entry.get("hooks") or []):
        c = isinstance(h, dict) and (h.get("command") or "")
        if c and _HOOK_SENTINEL not in c and "adapters.hook" in c:
            return True
    return False


def install_client_hooks(repo: Path, client: str = "claude") -> dict[str, Any]:
    """Opt-in (owner-gated): UPSERT recall's situational-push hooks into the client's settings.json,
    identified by the sentinel — preserving EVERY foreign entry. Idempotent (a second run is a
    no-op diff). Refuses (no write) on invalid JSON, a non-object/non-array shape, or a hand-rolled
    unmanaged recall-like hook. Only Claude Code's settings shape is supported today."""
    rel = _CLIENT_SETTINGS.get(client)
    if rel is None:
        return {"ok": False, "reason": f"client {client!r} not supported yet — Claude Code only for now"}
    target = repo / rel
    raw = _safe_read(target)
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {"ok": False, "reason": "settings.json is not valid JSON — refusing to overwrite", "path": str(target)}
    if not isinstance(data, dict):
        return {"ok": False, "reason": "settings.json is not a JSON object — refusing", "path": str(target)}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return {"ok": False, "reason": "settings.json 'hooks' is not an object — refusing", "path": str(target)}
    cmd = _client_command()
    # validate first (no partial writes), then UPSERT
    for event in _CLIENT_EVENTS:
        arr = hooks.get(event, [])
        if not isinstance(arr, list):
            return {"ok": False, "reason": f"hooks.{event} is not an array — refusing", "path": str(target)}
        if any(isinstance(e, dict) and _entry_is_unmanaged_recallish(e) for e in arr):
            return {"ok": False, "path": str(target),
                    "reason": f"a hand-installed recall-like hook exists in hooks.{event} — remove it "
                              f"or add the '{_HOOK_SENTINEL}' comment, then retry"}
    for event, matcher in _CLIENT_EVENTS.items():
        arr = hooks.setdefault(event, [])
        new_entry: dict[str, Any] = {"hooks": [{"type": "command", "command": cmd}]}
        if matcher:
            new_entry["matcher"] = matcher
        recall_entry = next((e for e in arr if isinstance(e, dict) and _entry_is_recall(e)), None)
        if recall_entry is None:
            arr.append(new_entry)  # coexists alongside any foreign entry (sentinel disambiguates)
        else:
            recall_entry.clear()
            recall_entry.update(new_entry)  # UPSERT in place — foreign entries untouched
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "reason": str(e), "path": str(target)}
    return {"ok": True, "installed": True, "path": str(target)}


def uninstall_client_hooks(repo: Path, client: str = "claude") -> dict[str, Any]:
    """Remove ONLY recall's sentinel-tagged entries; leave every foreign entry intact. Prunes an
    event array that recall empties, and the 'hooks' key if it ends up empty."""
    rel = _CLIENT_SETTINGS.get(client)
    if rel is None:
        return {"ok": False, "reason": f"client {client!r} not supported yet — Claude Code only for now"}
    target = repo / rel
    raw = _safe_read(target)
    if not raw.strip():
        return {"ok": True, "installed": False, "path": str(target)}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"ok": False, "reason": "settings.json is not valid JSON — refusing", "path": str(target)}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return {"ok": True, "installed": False, "path": str(target)}
    for event in list(hooks):
        arr = hooks.get(event)
        if not isinstance(arr, list):
            continue
        kept = [e for e in arr if not (isinstance(e, dict) and _entry_is_recall(e))]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]  # recall emptied it → prune the key
    if not hooks:
        del data["hooks"]
    try:
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "reason": str(e), "path": str(target)}
    return {"ok": True, "installed": False, "path": str(target)}


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

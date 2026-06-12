"""Tasks & plans as first-class, wired wiki nodes (ADR-017).

The Owner's 'next big thing': my memory (and your instructions to me) can be lost on a
reset. A task written INTO the repo cannot — it is versioned, shared, survives every
reset, and is readable by any AI. So a plan / feature / roadmap / sprint / task becomes a
real node in the index, wired to the files it affects (relates_to edges), with a STATUS
lifecycle (open / done / dropped / deferred) that the dashboard can show and alert on.

Format — a markdown file with YAML-ish frontmatter, in .recall/tasks/ (tool default) or
docs/plans|tasks|roadmap (discovered in existing repos / Obsidian-style vaults):

    ---
    title: Wire the editor curve onto the canvas
    status: open            # open | done | dropped | deferred
    kind: task              # task | plan | feature | roadmap | sprint
    affects: [recall/engine.py, recall/dashboard.py]   # files this touches
    tags: [feature, ui]
    ---
    The body explains the plan / intent in prose. Dependencies + tags live up here so the
    plan is self-describing and indexes itself.

The read path stays LLM-free (ADR-014): parsing is plain text, wiring is deterministic
(affects -> relates_to edges), the status DRIFT suggestion is pure file/SHA arithmetic.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Where tasks live. The first is the tool's own home; the rest are discovered so existing
# repos (and Obsidian-style vaults) light up without moving anything.
TASKS_DIR = ".recall/tasks"
_DISCOVER_DIRS = ("docs/plans", "docs/tasks", "docs/roadmap", "docs/sprints")

VALID_STATUS = ("open", "done", "dropped", "deferred")
VALID_KIND = ("task", "plan", "feature", "roadmap", "sprint")
_STALE_DAYS = 30  # an open task untouched this long is flagged stale (drift alert)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse a leading --- ... --- YAML-ish block. Pure stdlib (no PyYAML dep): handles
    scalars and inline [a, b] lists — enough for the task format. Returns (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    meta: dict[str, Any] = {}
    for line in block.splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            items = [v.strip().strip("'\"") for v in val[1:-1].split(",")]
            meta[key] = [v for v in items if v]
        else:
            meta[key] = val.strip("'\"")
    return meta, body


_CHECK_RE = re.compile(r"^\s*[-*]\s*\[([ xX>-])\]\s+(.*\S)\s*$")
# Owner finding 2026-06-10: done tasks showed "2/3" bars because a step that was
# deliberately dropped or moved to another task had no syntax — it stayed `- [ ]` and
# read as forgotten. Two extra states close that gap; both count as RESOLVED (a closed
# task may carry them), only `- [ ]` left under status:done is a real inconsistency.
_CHECK_STATE = {" ": "open", "x": "done", "-": "dropped", ">": "moved"}


def parse_subtasks(body: str) -> list[dict[str, Any]]:
    """Markdown checklist items in the body become sub-tasks with their own state.

    `- [ ]` open · `- [x]` done · `- [-]` dropped (won't do — say why inline) ·
    `- [>]` moved (lives on in another task — name the [[target]] inline).
    This is how a roadmap/plan shows real, checkable progress: the Owner ticks a box
    in the file (versioned, shared), and the dashboard renders the list + a progress
    bar. Pure text — stays LLM-free (ADR-014). `done` stays a bool for callers that
    predate the states. The label is trimmed of leading markdown emphasis so it reads
    clean in the UI. A checklist item wrapped over several INDENTED lines (the roadmap
    writes long items that way) is folded back into one whole label — a blank or
    column-0 line ends it."""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None  # the item currently collecting continuation lines

    def _flush():
        if cur and cur["_parts"]:
            text = re.sub(r"\*{1,2}|_{1,2}|`", "", " ".join(cur["_parts"])).strip()
            if text:
                out.append({"text": text[:400], "done": cur["state"] == "done",
                            "state": cur["state"]})

    for line in (body or "").splitlines():
        m = _CHECK_RE.match(line)
        if m:
            _flush()
            cur = {"state": _CHECK_STATE[m.group(1).lower()],
                   "_parts": [m.group(2).strip()]}
        elif cur is not None and line[:1] in (" ", "\t") and line.strip():
            # an INDENTED non-empty line right after an item is a wrapped continuation of it;
            # fold it in so the item reads whole. A blank/column-0 line ends the item.
            cur["_parts"].append(line.strip())
        else:
            _flush()
            cur = None
    _flush()
    return out


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).replace(",", " ").split() if s.strip()]


def parse_task(text: str, *, rel_path: str) -> dict[str, Any] | None:
    """Turn a task markdown file into a normalised dict, or None if it isn't a task.
    Validates status/kind against the closed vocabularies (unknown -> safe default)."""
    meta, body = _split_frontmatter(text)
    title = (meta.get("title") or "").strip()
    if not title:
        # fall back to the first heading / first line so a frontmatter-less note still works
        for line in body.splitlines():
            s = line.lstrip("# ").strip()
            if s:
                title = s[:120]
                break
    if not title:
        return None
    status = str(meta.get("status", "open")).lower()
    if status not in VALID_STATUS:
        status = "open"
    kind = str(meta.get("kind", "task")).lower()
    if kind not in VALID_KIND:
        kind = "task"
    affects = [a.replace("\\", "/") for a in _as_list(meta.get("affects"))]
    tags = _as_list(meta.get("tags")) or [kind]  # at least the kind tag
    if kind not in tags:
        tags.append(kind)
    subtasks = parse_subtasks(body)
    return {
        "title": title[:120],
        "status": status,
        "task_kind": kind,
        "affects": affects,
        "tags": tags,
        "subtasks": subtasks,
        # a plan/roadmap body legitimately carries a checklist + rationale; 1200 chars
        # truncated real checklists mid-list. 6000 holds a full plan (stored once, read rarely).
        "body": body.strip()[:6000],
        "rel_path": rel_path.replace("\\", "/"),
    }


def discover_task_files(repo: str | Path) -> list[Path]:
    """Every task/plan markdown file in the repo — the tool's dir + discovered ones."""
    repo = Path(repo)
    out: list[Path] = []
    for d in (TASKS_DIR, *_DISCOVER_DIRS):
        base = repo / d
        if base.is_dir():
            out.extend(sorted(p for p in base.rglob("*.md") if p.is_file()))
    return out


def index_tasks(index, repo: str | Path, stats: dict | None = None) -> dict[str, Any]:
    """Index every task/plan file as a node wired (relates_to) to the files it affects.

    Idempotent ON ITS OWN: it clears every existing task node first (clear_tasks), then
    rebuilds from the files — so a status change (open -> done) or an edited/removed task is
    honored on the very next index, WITHOUT needing a full re-init. (This was the dogfood bug
    the Owner caught: tasks finished in the file still showed 'open' in the tool, because a
    standalone re-index stamped a SECOND node with dedup=False instead of replacing the old
    one. The bootstrap path already cleared via clear_bootstrap; the standalone path did not.)
    Model-free."""
    from recall.anchors import extract_anchors

    repo = Path(repo)
    stats = stats if stats is not None else {}
    # wipe the old task nodes so a re-index REPLACES rather than DUPLICATES (tasks are
    # dedup=False — without this, open+done copies of the same task both live in the graph).
    index.clear_tasks()
    n_tasks = 0
    for path in discover_task_files(repo):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(repo)).replace("\\", "/")
        task = parse_task(text, rel_path=rel)
        if task is None:
            continue
        anchors = extract_anchors(f"{task['title']} {task['body']}")
        anchors.update({task["status"], task["task_kind"], "task"})
        for a in task["affects"]:
            base = a.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
            if len(base) >= 3:
                anchors.add(base)
        # the STATUS goes in the tags too, so it persists as a facet on the node and the
        # dashboard / drift can read the lifecycle back (open/done/dropped/deferred).
        node_tags = list(dict.fromkeys([*task["tags"], task["status"]]))
        res = index.stamp(
            title=task["title"],
            body=task["body"] or None,
            anchors=list(anchors),
            tags=node_tags,
            kind="task",
            file_path=rel,
            origin="bootstrap",
            dedup=False,  # a task is a distinct artifact, never merged into a lesson
        )
        node_id = res.get("node_id")
        # wire the task to every file it affects (relates_to) — so editing that file
        # surfaces the task (the blast-radius reminder), and the wiki shows the link.
        if node_id is not None and task["affects"]:
            index.link_task_to_files(node_id, task["affects"])
        n_tasks += 1
    stats["tasks"] = n_tasks
    return stats


# ---------------------------------------------------------------- status drift
def stale_open_tasks(index, *, now_ts: int, stale_days: int = _STALE_DAYS) -> list[dict]:
    """Open tasks whose file hasn't changed in `stale_days` — drift candidates for the
    dashboard alert. Deterministic (timestamps only), model-free.

    `now_ts` is passed in (the engine has no clock in scripts) so this stays pure."""
    rows = index.db.execute(
        "SELECT id, title, file_path, facets, created_at FROM nodes "
        "WHERE kind='task'"
    ).fetchall()
    out: list[dict] = []
    cutoff = now_ts - stale_days * 86400
    for nid, title, fp, facets, created in rows:
        status = _status_from_facets(facets)
        if status != "open":
            continue
        age_days = max(0, (now_ts - (created or now_ts)) // 86400)
        out.append({
            "node_id": nid, "title": title, "file": fp, "status": status,
            "age_days": int(age_days), "stale": (created or now_ts) < cutoff,
        })
    return out


def _status_from_facets(facets: str | None) -> str:
    fs = set((facets or "").split(","))
    for s in VALID_STATUS:
        if s in fs:
            return s
    return "open"

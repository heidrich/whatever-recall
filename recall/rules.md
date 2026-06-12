---
# whatever-recall — default governance (rules.md, layer 0)
# Projects override key-by-key in <repo>/.recall/rules.md. The `core:` block
# is a hard floor no layer can weaken. Facts live in commit trailers; LOGIC
# lives here (human-steered, once).

silence_floor: 2
dedup_threshold: 0.45
context_multiplier: 1.5

# Facet weights — security lessons fire louder, chrome quieter (engine_proto2).
facet_weights:
  security: 2.0
  logic: 1.5
  math: 1.5
  bugfix: 1.3
  backend: 1.2
  frontend: 1.0
  ui: 0.8
  docs: 0.7
  # chore (housekeeping: lockfiles, ignore rules) surfaces only as a last resort
  chore: 0.3
  # task/plan lifecycle (ADR-017): tasks have their OWN surfaces since the 3-track
  # split (the open_tasks track + the pre-edit briefing) — in the knowledge track a
  # loud task weight let a growing task corpus bury the WHY answers (measured
  # 2026-06-11: knowledge r@3 17/25 at 1.8 -> 22/25 at 1.0, code track unchanged).
  task: 1.0
  plan: 1.6
  roadmap: 1.4
  sprint: 1.4

# Context boost — editing an auth/rls file prefers security lessons, etc.
context_boost:
  auth: security
  rls: security
  login: security
  migration: backend
  sql: backend
  component: ui
  css: ui
  math: math

# Tabu — never surface these facets unprompted (the Clippy killer).
stay_silent_on: [chore]

# When the hook is allowed to speak at all.
surface_on: [edit, task_start, commit]

# The closed vocabulary for typed edges.
edge_kinds: [implements, decided_by, supersedes, guarded_by, warns_about, recurs_with, presents, relates_to, depends_on, co_changed]

# Unoverridable minimum — a project cannot silence the engine into uselessness
# nor make it shout at score 0.
core:
  silence_floor_min: 1
---

# Governance

This file is the **logic** layer of whatever-recall. The engine reads its
thresholds and weights from the frontmatter above — nothing is hardcoded.

## How layering works

1. **Defaults** (this file, shipped with the engine).
2. `~/.recall/rules.md` — your global preferences.
3. `<repo>/.recall/rules.md` — the project's rules.

Each layer overrides scalars key-by-key and *adds* to the tabu/edge/tag sets.
The `core:` block is the exception: `silence_floor_min` is a hard floor, and
tabus only ever accumulate — a later layer can never weaken a safety rule.

## What each knob does

- **silence_floor** — minimum raw anchor hits before recall surfaces anything.
  This is the anti-Clippy guard: below the floor, recall stays silent.
- **dedup_threshold** — anchor overlap (distinct) at which a new stamp merges
  into an existing node instead of creating a near-duplicate (ADR-005).
- **facet_weights** — multiply a node's score by its loudest facet. A security
  lesson outranks a styling note even on identical anchors.
- **context_boost** — when you're editing a file whose path matches a key, the
  mapped facet gets an extra multiplier (`context_multiplier`).
- **stay_silent_on** — facets that are never surfaced unprompted.
- **edge_kinds** — the typed relations a stamp may declare.

## MUST-CHECK — non-negotiable for any AI working in this repo (ADR-017)

These are **hard obligations, not suggestions.** They are what makes recall worth far more
than the near-zero tokens it costs: it keeps you from re-deriving context, silently undoing
deliberate decisions, or missing what a change breaks — and the read path is **structurally
LLM-free (0 model tokens)**, so following these rules makes you *faster and cheaper*, not
slower. Treat them like a test suite you cannot skip.

### Rule 0 — THE TASK LAW: an instruction must never fall out of memory

**The moment the user gives you any instruction, decision, plan, "remember to…", "always
do X", "next we…", a bug to fix, or a UI tweak — you WRITE IT AS A TASK in `.recall/tasks/`
BEFORE you do anything else with it.** A task is a markdown file with frontmatter
(`title`, `status: open`, `kind: task`, `affects: [paths]`, `tags`). This is bomb-proof
anchoring: the task is versioned in git, shows in the dashboard's Tasks tab + Wiki, and
**comes back to you automatically** (the `open_tasks` track) the next time anyone touches
the files it `affects:`. Memory you merely "hold" is lost on the next reset/compact — a
task is not. **If you ever think "I'll keep that in mind," stop and write the task instead.**
Mark it `done` only when truly finished. NEVER let an instruction live only in chat.

### Rule 0b — At SESSION START, always wake recall up

On a fresh session, before substantive work:

1. Run `recall explain` (or open the dashboard's "Start here" tab) for the orientation
   path: load-bearing files, must-know decisions/ADRs, what's in progress, where time burns.
   Read-only, 0 tokens (ADR-020). `recall explain --for-prompt` gives a paste-in-any-AI block.
2. **Check the git hooks are installed** (`recall hook`): the post-commit auto-stamp AND
   the pre-commit risk-warning. If either is missing, **install it and/or remind the user**
   — without the hooks the memory silently stops staying fresh.
3. **Start the local dashboard** (`recall dashboard`) so the user can SEE the living wiki
   and you can use the graph. It is the window onto everything recall knows.

### The rest, while you work

1. **Before editing a file, brief yourself on it.** Run `recall brief <file>` (or open
   the file in the dashboard — the pre-edit briefing panel sits above the code). The
   briefing bundles, for that one file: open tasks wired to it (the user's standing
   intent), WHY it is the way it is (its commits/lessons/ADRs), what BREAKS if you change
   it (blast radius), and what it depends on — all read-only, 0 tokens (ADR-018). If an
   open task exists, **read it first** and treat it like a failing test: address it or
   say why you are not. (Under the hood this is `Index.brief()`; the older path is to
   `recall "<query>"` and read its `open_tasks` track.) For the team-level view — *which*
   files are uncertainty hotspots, not what one file holds — `recall contested` ranks the
   code the team keeps changing (churn × entanglement, ADR-019); a high-ranked file is one
   to touch with extra care.
2. **When you finish work a task described, update its status.** Set `status: done`
   (or `dropped` / `deferred`) in the task file under `.recall/tasks/`. A task left
   `open` after it is done is a lie the whole team inherits — the same staleness this
   tool exists to kill. The dashboard's Tasks tab flags open tasks untouched > 30 days.
   **Closing rule:** before setting `status: done`, resolve EVERY checklist item —
   tick it `- [x]`, drop it `- [-]` (won't do — say why inline), or move it `- [>]`
   (name the `[[target-task]]` inline). A done task with `- [ ]` left reads as a
   contradiction (green DONE over a 2/3 bar) and the dashboard flags it as an error
   (Owner finding 2026-06-10).
3. **Re-confirming the TASK LAW (Rule 0):** the instant the user gives a standing
   instruction, plan, bug, or tweak — **write it as a task in `.recall/tasks/`** with
   `affects:` the files it concerns. Never let it live only in chat or "in mind"; it
   surfaces automatically next time those files are touched, and survives every reset.
4. **For a multi-step plan/roadmap, use a markdown checklist in the body.** Each
   `- [ ] step` is a tracked sub-task; tick it `- [x]` as you finish, `- [-]` to drop
   it (say why inline), `- [>]` to move it (name the `[[target-task]]`). The dashboard
   renders the list + a progress bar, so a roadmap shows real, checkable progress
   instead of a flat note. Update the boxes in the same edit that does the work.
5. **Before committing a change, review what it can break.** Run `recall review <sha>`
   (or `recall review` for HEAD) — it bundles, per touched file, the blast radius, the
   decisions behind it, and any open task, and flags the RISK files (load-bearing, many
   dependents, or carrying an open task). `--for-prompt` emits a PR-markdown block to
   drop into the pull request. The pre-commit hook (`recall hook --install --pre-commit`)
   warns automatically when a staged file is load-bearing — it only warns, it never
   blocks (ADR-021). And when an ADR's referenced code has moved on a lot since it was
   stamped, `recall freshen` (and the dashboard Drift tab) flag it as a possibly-stale
   decision to re-check (ADR-022). All read-only, 0 tokens.
6. **Drift does NOT heal itself — you resolve it (with the owner's OK). Drift must trend to
   ZERO, never just grow.** `recall freshen` only *detects* and flags drift (🟢 fresh /
   🟡 the file got new commits since the note was stamped / 🟠 the file has uncommitted
   edits) — it never silently rewrites a note. **Only CLAIM-BEARING knowledge can drift**
   — lessons, decisions/ADRs, tasks/plans. The auto-regenerated code map (code-symbols) and
   immutable commit facts are never flagged: a commit can't "go stale", and a re-index
   rebuilds the symbol map. So a non-zero drift count is always a real curated note whose
   statement may no longer match the code — not commit noise. A growing pile of 🟡/🟠 is a
   failure state, not normal. When you see drift on a file you touch (the briefing shows it;
   the Drift tab lists it):
   - **Open the diff since the stamp** and decide which of two things is true (ADR-006
     Self-Heal). Then **offer the user the resolution — never change a pinned note without
     their OK** (95% rule):
     - **Reality moved on, the note is now wrong** → rewrite the note's core sentence in
       ONE line to match today's code and re-stamp it (`recall stamp …` against the new
       SHA) → drift goes back to 🟢. The note was the thing that fell behind.
     - **The note was right, the CODE drifted from a deliberate decision** → that is a
       regression/bug → flag it, don't "heal" it away. The code is wrong, not the note.
   - **For 🟠 (uncommitted edits):** it usually clears itself on the next commit (the
     post-commit hook re-freshens). If it persists, the working tree has unsaved changes
     under a pinned note — verify the note still holds before trusting it.
   What this means for each party: **System** = flags honestly, heals nothing on its own.
   **You (the AI)** = treat drift like a failing test — resolve or escalate it, drive the
   count down, don't let it accumulate. **User** = stays the boss of every pinned note;
   approves each heal. The whole promise ("the wiki can't silently lie") only holds if
   drift is *acted on*, not merely displayed.

7. **The knowledge corpus is ENGLISH-ONLY (Owner decision 2026-06-10).** ADRs,
   CHANGELOG entries, task files, commit messages and `Recall-why` trailers are written
   in English. The FTS stemmer is porter (English) — a German entry is second-class in
   retrieval (measured: the English query "why is retrieval LLM-free" missed the
   German-titled ADR-014 entirely). If you find German knowledge text, treat it like
   drift: offer the user a translation + restamp.

Tasks live in `.recall/tasks/*.md` (tool default) or `docs/plans|tasks|roadmap`
(discovered). Frontmatter: `title`, `status` (open/done/dropped/deferred), `kind`
(task/plan/feature/roadmap/sprint), `affects: [paths]`, `tags`. The body may hold a
checklist whose items become tracked sub-tasks with a progress bar: `- [ ]` open,
`- [x]` done, `- [-]` dropped (won't do), `- [>]` moved to another task.

## What does NOT belong here

Per-commit facts (anchors, the why, tags) belong in the commit trailer, not in
rules.md. This file is steered by a human, rarely, and on purpose.

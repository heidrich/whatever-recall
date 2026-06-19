---
# whatever-recall — default governance (rules.md, layer 0)
# Projects override key-by-key in <repo>/.recall/rules.md. The `core:` block
# is a hard floor no layer can weaken. Facts live in commit trailers; LOGIC
# lives here (human-steered, once).

silence_floor: 2
dedup_threshold: 0.45
context_multiplier: 1.5

# Pre-commit predicate nudge (Arrow 1, ADR-039): when a staged file recall has a why for
# carries no re-check yet, print a model-free `contains:<anchor>` suggestion. Warn-only,
# never blocks the commit. Set false to silence it.
predicate_nudge: true

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
surface_on: [edit, task_start, commit, prompt, session_start]

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

## THE GATE — do this before you touch code (ADR-017, ADR-032)

> **Before editing ANY code file, run `recall brief <file>` first. No exceptions.**
> This is a hard gate, not a step in a list. The briefing is read-only and costs
> **0 model tokens** — skipping it never saves tokens, it only loses the open tasks,
> the *why*, and the blast radius that keep you from breaking something on purpose.
>
> **ENFORCED, not suggested (2026-06-17):** when the dev installs the recall PreToolUse
> hook, an edit to any file recall has knowledge about is **DENIED** until you run
> `recall ack <file>` (after reading the briefing). recall is in the edit path, not
> beside it — so a deliberate decision can't be silently undone even by an agent that
> would otherwise skim past the context. (The ack is per-file, time-boxed; a new stamp
> on the file re-gates it.)

**recall and grep/read are not alternatives — they run in order:**

1. **Orient** — `recall brief <file>` (or `recall "<question>"`): *what do I need to know
   before I touch this?* — open tasks, why it's like this, what breaks, what it depends on.
   This is the step grep/read **cannot** do; it is where recall earns its place.
2. **Locate** — `grep`/`Read`: the exact string, the full file, the precise line. recall is
   semantic and may miss a literal; for an exact match or a whole-file read, grep/Read win.
   **Before you grep a name you're GUESSING** (a function/symbol you think exists but haven't
   confirmed), run `recall resolve <guess>` first (ADR-037, search-inversion): it maps the term
   you'd type to what THIS repo actually calls it (`seatLimit` → `confirmSeatOrRollback`), so you
   don't burn a grep round on a name that doesn't exist here. It re-ranks and annotates but never
   hides a match (grep stays the complete recall), and gets sharper as the repo is worked in.
3. **Edit** — now, with both the *why* and the *where* in hand.

So: never reach for grep "instead of" recall — reach for recall **first** (orient), then grep
(locate), then edit. The only time you skip step 1 is a non-code mechanical lookup (a literal
string, a config value, live DB/deploy state) where there is nothing to orient on.

## THE AGENT RULE — subagents reach recall via the CLI, not MCP

If you **spawn subagents** (a Task/agent tool, a review fleet, an audit workflow), each one
starts BLANK — it does not inherit your session's memory or MCP connection. **MCP servers are
session-scoped, so a spawned subagent CANNOT see the recall MCP tools** (measured 2026-06-14).
The reliable transport for an agent is therefore the **recall CLI via the shell**:

```sh
recall brief <file> --terse      # the machine-first pre-edit briefing
recall "<concept>" --terse       # locate code by concept
recall explain --terse           # cold-start orientation
```

`--terse` keeps the WHY verbatim and only compresses the structural lists — the right shape for
an agent prompt. **Every finder/reviewer/auditor agent you spawn must run `recall brief <file>
--terse` on a file before it judges that file.** It is the same gate as above, pushed down to
the fleet: a blank agent re-derives intent from code alone and raises false alarms about
decisions that were made on purpose (measured: a recall-less agent raised ~4–5 false alarms a
recall-first agent didn't, while the recall-first one also found a real bug the other missed).
On Windows the CLI needs `PYTHONIOENCODING=utf-8`. MCP stays the path for the main interactive
session and real users in Claude/Cursor.

## THE 6 DIMENSIONS — the review raster after every feature, before every push

After **every** feature and before **every** push, walk the six audit dimensions in
`docs/audit-dimensions.md` (the canonical, single-source list): **Auth-Guard · Audit-Log ·
Zod/Validation · Error-Handling (`res.ok`) · State-Updater · No-Client-Secrets/Cleanup**, plus
the standing security lenses (business-logic/race/money, injection/takeover). A finding in any
dimension is a blocker, not a TODO. Run it recall-first and verify findings adversarially before
treating them as confirmed (3 skeptics, majority-real survives — most raised findings are false
alarms). Don't fork the list — link to `docs/audit-dimensions.md`.

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

**Signing in opens the dashboard.** A successful `recall login` brings the dashboard up
on its own (in the background, reusing one that's already running) — being signed in and
having the dashboard visible are one and the same act. So if you (or the user) just signed
in, the window onto recall is already there; you don't need to start it by hand.

**At session END / before a compact:** if work is in flight, run
`recall handoff "<where I am right now>" --files <touched files>`. It stamps the volatile
state as a snapshot that surfaces in the next session's `recall explain` and in the per-file
brief of each touched file — so the next session rebuilds from recall, not an ad-hoc summary
that dies with the context. (Durable instructions are still TASKS, Rule 0; handoff is the
in-flight 'where I am', not a standing intent.)

### The rest, while you work

1. **The pre-edit briefing (THE GATE above) is rule one.** `recall brief <file>` bundles,
   for that one file: open tasks wired to it (the user's standing intent), WHY it is the way
   it is (its commits/lessons/ADRs), what BREAKS if you change it (blast radius), and what it
   depends on — all read-only, 0 tokens (ADR-018). If an open task exists, **read it first**
   and treat it like a failing test: address it or say why you are not. (Under the hood this
   is `Index.brief()`; the older path is to `recall "<query>"` and read its `open_tasks`
   track.) For the team-level view — *which* files are uncertainty hotspots, not what one file
   holds — `recall contested` ranks the code the team keeps changing (churn × entanglement,
   ADR-019); a high-ranked file is one to touch with extra care.
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

8. **Never make the same mistake twice (Owner law, 2026-06-13).** When you cause, hit,
   or fix a bug — or the user corrects you — **stamp the lesson** (commit trailer or
   `recall stamp`) anchored to the files it concerns, so it surfaces in the next
   pre-edit briefing on exactly those files. The mirror duty: the briefing exists so
   you CHECK before you act — a mistake recall already knows about, made again, is a
   process failure, not bad luck. If you catch yourself (or the user catches you)
   repeating one, say so, strengthen the lesson (sharper anchors, a `warns_about`
   edge), and only then continue.

Tasks live in `.recall/tasks/*.md` (tool default) or `docs/plans|tasks|roadmap`
(discovered). Frontmatter: `title`, `status` (open/done/dropped/deferred), `kind`
(task/plan/feature/roadmap/sprint), `affects: [paths]`, `tags`. The body may hold a
checklist whose items become tracked sub-tasks with a progress bar: `- [ ]` open,
`- [x]` done, `- [-]` dropped (won't do), `- [>]` moved to another task.

## What does NOT belong here

Per-commit facts (anchors, the why, tags) belong in the commit trailer, not in
rules.md. This file is steered by a human, rarely, and on purpose.

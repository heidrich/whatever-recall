---
title: Governance & drift
slug: governance
order: 12
summary: rules.md is the logic layer; drift is flagged, never silently healed.
---

# Governance & drift

## rules.md — the logic layer

The engine reads its thresholds and weights from `recall/rules.md` frontmatter —
nothing is hardcoded. Layering, each overriding the previous key-by-key:

1. **defaults** (`recall/rules.md`, shipped with the engine)
2. `~/.recall/rules.md` — your global preferences
3. `<repo>/.recall/rules.md` — the project's rules

The `core:` block is a hard floor no layer can weaken (e.g. `silence_floor_min`),
and tabu/edge sets only ever accumulate — a later layer can never weaken a safety
rule. **The human writes the constitution; the system executes it deterministically.**

Key knobs: `silence_floor` (the anti-Clippy guard — below it recall stays silent),
`dedup_threshold` (when a new stamp merges instead of duplicating),
`facet_weights` (a security lesson outranks a styling note), `context_boost`
(editing an auth file prefers security lessons), `stay_silent_on`, `edge_kinds`.

## Drift does not heal itself

`recall freshen` (and the dashboard Drift tab) only **detect** drift; nothing is
silently rewritten. Only **claim-bearing** knowledge can drift — lessons,
decisions/ADRs, tasks. The auto-regenerated code map and immutable commit facts are
never flagged. States: 🟢 fresh / 🟡 the file got new commits since the note was
stamped / 🟠 the file has uncommitted edits.

When you see drift on a file you touch, open the diff since the stamp and decide —
then **offer the user the resolution; never change a pinned note without their OK**:

- **Reality moved on, the note is wrong** → rewrite the note's core sentence to match
  today's code and re-stamp → 🟢.
- **The note was right, the CODE drifted from a deliberate decision** → that's a
  regression/bug → flag it, don't "heal" it away.

Drift must trend toward **zero**, not grow. A growing pile of 🟡/🟠 is a failure
state, not normal.

## The task law

The moment the user gives an instruction, decision, plan, or bug — write it as a
**task** in `.recall/tasks/` BEFORE doing anything else with it. A task is versioned
in git, shows in the dashboard, and comes back automatically (the open-tasks track)
the next time anyone touches the files it `affects:`. Memory you merely "hold" is
lost on the next reset; a task is not.

## English-only knowledge

ADRs, CHANGELOG entries, task files, commit messages and `Recall-why` trailers are
written in **English** (the FTS stemmer is English; a German entry is second-class
in retrieval). Conversation can be in any language — the *corpus* is English.

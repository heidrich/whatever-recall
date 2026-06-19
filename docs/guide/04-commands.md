---
title: Core commands
slug: commands
order: 4
summary: Every recall command — what it returns and when to reach for it.
---

# Core commands

All read commands are **read-only and cost 0 model tokens** (a SQLite/FTS5 lookup,
no LLM). Add `--for-prompt` for a copy-paste block to drop into any web AI, or
`--terse` for the compact, machine-first block an AI agent reads via the shell.

## `recall init [path]`

Index a repo — builds `.mind/` (code map + history + knowledge). Run once; the git
hooks keep it fresh after that.

## `recall brief <file>`

**The gate.** Run BEFORE editing a file. Returns everything recall knows about it:

- **open tasks** wired to it (the standing intent — read first, treat like a failing test)
- **why** it is the way it is (its commits, lessons, ADRs)
- **what breaks** if you change it (the blast radius)
- **what it depends on**
- its **symbols**

```sh
recall brief src/lib/server/orgs.ts --terse
```

## `recall push`

The **situational push** for agents and hookless harnesses. One block: the scoped
brief for what you're working on now + the *landmines* recall surfaces unasked +
the live `🔴 BROKEN` trust-status of any claim that the code currently contradicts.
Where `brief` is per-file and pull-style, `push` is task-aware and built for a
subagent that can't run the pre-edit hook — it gets the same warning the gate would
have shown, over the shell, at 0 model tokens.

## `recall ack <file>`

Acknowledge a file's briefing so the **hard pre-edit gate** lets the edit through.
The gate's contract is "read the brief before you touch load-bearing code"; `ack`
is how you confirm you did (it opens a short edit window for that file).

## `recall "<question>"`

Ask by concept. Returns four independent tracks, each ranked on its own axis so
they never compete: **code** (where, by importance), **knowledge** (the why),
**blast radius**, **open tasks**.

## `recall resolve <guess>`

**Search-inversion** (ADR-037). Correct a search term you're *guessing* into what
THIS repo actually calls it, before you grep — `seatLimit` → `confirmSeatOrRollback`.
See [Search-inversion](search-inversion).

## `recall explain`

Orientation for a fresh session: load-bearing files, must-know decisions, what's in
progress, where the team burns time. Start here in an unfamiliar repo.

## `recall stamp "<text>" --anchors <files/terms>`

Record a decision, lesson, or gotcha so every future session knows it. Anchors are
the terms it should be found by. (Usually you don't call this by hand — the
post-commit hook stamps from your commit trailer.)

Add **`--private`** to keep a note on this machine only — it never travels in an
export or a shared brain. A note can only become more private, never less. The
project default is set in `.recall/config.toml`. See
[Private knowledge stays local](privacy).

```sh
recall stamp "chose Stripe-hosted checkout to stay out of PCI scope" --private
```

## `recall export --out <file>`

Write a **shareable copy** of the brain with every `--private` note (and its traces)
removed — for a team that chooses to share its memory. A fail-closed gate runs before
anything is written: if any private content would survive, it aborts and deletes the
half-written file. See [Private knowledge stays local](privacy).

## `recall check-leak`

The **commit guard**: blocks a commit that stages a brain still holding private notes,
so a private decision can't reach a teammate's clone or a public repo by accident.
`recall hook --install` wires it into your pre-commit hook as a blocking step.

## `recall handoff "<state>" --files a,b`

At session end / before a compact: snapshot the in-flight state so the next session
rebuilds from recall (its `explain` + the per-file brief) rather than an ad-hoc
summary that dies with the context.

## `recall review [sha]`

Before committing: per touched file, the blast radius + the decisions behind it +
any open task, and it flags the RISK files. `--for-prompt` emits a PR-markdown block.

## `recall contested`

Where the team burns time: the files with high churn AND high entanglement. A
high-ranked file is one to touch with extra care.

## `recall freshen`

Re-check pinned notes for drift against git (🟢 fresh / 🟡 the file changed since
the note / 🟠 uncommitted edits). It only *flags*; you resolve drift with the
owner's OK (see [Governance & drift](governance)).

## The experience layer — what no indexer has

These build on the typed edge graph recall already stamped at write-time, so they
are still **read-only and 0 model tokens** — a pure graph walk, not an LLM call.
They are what makes a generic code indexer unnecessary (see
[Architecture](architecture)).

### `recall precedent <situation>`

The most analogous **past decisions** for what you're about to do, each with how it
turned out — superseded (→ the rule that governs now), became-a-landmine, or drifted.
A reversed decision is kept, not dropped: "we tried X and undid it" *is* the lesson.

```sh
recall precedent "switching auth to JWT" --terse
```

### `recall impact <file|symbol>`

"If I touch this, what's affected?" — the **0-token call-hierarchy replacement**. It
fuses *empirical* co-change (what git proves moves together — a signal no static tool
has) with *structural* transitive dependents, ranked by importance and annotated with
landmines/drift.

```sh
recall impact src/lib/server/orgs.ts --depth 2
```

## Code intelligence (file-granular, 0 tokens)

Static-graph serves over the same `depends_on` edges — file-granular, offline, and
honest about what they are (candidates to verify, not verdicts).

### `recall callers <file|symbol>` / `recall callees <file|symbol>`

Who depends on this (`callers`, the call-hierarchy) and the inverse — what this
depends on (`callees`, forward). Hop-ranked; `--depth` controls how far it walks.

```sh
recall callers src/lib/license.ts --depth 2
recall callees src/lib/license.ts
```

### `recall dead-code`

Code files **nothing imports** — dead-code *candidates*. Filtered against what's on
disk (no phantom nodes) with tests / entry-points / config / docs excluded. Verify
before deleting.

### `recall untested`

Code files with **no recorded test edge** — file-granular. Uses import edges only
(not co-change), so it doesn't hide a genuinely untested critical file.

### `recall cycles`

File→file **import cycles** in the `depends_on` graph (Tarjan SCC, bounded — emits a
`truncated` flag rather than hang on a dense graph).

## `recall receipt`

The **money-receipt**: the loop recall was in over a rolling window — in *measured
counts* and the per-call size it emitted, nothing more. No token estimate, no
dollar figure, no invented denominator. It answers "how much did recall actually do
for me lately?" with numbers it can stand behind, not a marketing multiplier.

## `recall sync-context`

Write recall's live state into your AI instruction file (`CLAUDE.md` / `AGENTS.md` /
`.github/copilot-instructions.md`) so **every** AI client loads it at the start of a
session **without a tool call**. This is the adoption fix: an assistant that never
learns to call a tool still gets recall's orientation, because it's already in the
file it always reads. The post-commit hook keeps the block fresh; the rest of the
file is yours and is never touched.

## `recall --version`

Print the installed version (e.g. `recall 1.0.0`).

## `recall dashboard` / `recall hook` / `recall mcp`

- **dashboard** — the browsable brain + causal-chain graph (a local server).
- **hook --install** — the pre-commit risk-warning + the post-commit auto-stamp.
- **mcp** — exposes recall as native MCP tools for Claude/Cursor/any MCP client
  (14 tools: recall, brief, explain, resolve, stamp, contested, freshen, dashboard,
  impact, precedent, callers, dead_code, untested, cycles). See [Your AI](agents).

Every read command also takes `--for-prompt` (a copy-paste block for a web AI) and
`--terse` (the compact, machine-first block an agent reads over the shell).

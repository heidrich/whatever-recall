---
title: Stamps & edges
slug: stamps-and-edges
order: 6
summary: The two things recall stores — what a stamp is, the kinds of node, and the typed edges that connect them.
---

# Stamps & edges

> **In the chain (the data behind link ②).** A stamp is *how* write-time capture
> actually happens — the anchors and typed edges written at commit time. Everything
> downstream (the [6 dimensions](six-dimensions), [search-inversion](search-inversion),
> [self-healing](self-healing)) is just reading this data back. No stamps, no chain.
> See [Why it all connects](why-it-connects).

Everything recall knows is made of two things: **nodes** (a stamped piece of
knowledge or code) and **edges** (a typed relationship between two nodes). That's
the whole data model — small on purpose, so the read path can stay a dumb, fast
lookup.

## What a stamp is

A **stamp** writes one node into the index. It carries:

- a **title** — the one-line claim ("we refuse a plan downgrade below occupied seats")
- an optional **body** — the longer "why"
- **anchors** — the rare, load-bearing terms it should be *found* by (a migration
  number, a symbol name, an ADR id). Anchors are the search surface; pick the words
  someone would actually grep for.
- a **kind** (see below), optional **tags**, an optional **file_path**/symbol/line,
  the **SHA** it was stamped at (for drift), and optional **edges**.

You rarely stamp by hand — the **post-commit hook** stamps automatically from your
commit trailer, so the "why" is captured at the exact moment you made the decision,
while the context is still in your head. `recall stamp "<text>" --anchors …` is the
manual escape hatch.

## The kinds of node

Every node has a `kind`. The ones recall uses:

| kind | what it is | where it comes from |
|------|-----------|---------------------|
| **code-symbol** | a function/class/route/component in the code map | the tree-sitter parser, on `recall init` (auto-regenerated, never drifts) |
| **commit** | an immutable commit fact (sha, message, files) | git history; can't go stale |
| **lesson** | a learned thing — a decision, a gotcha, a "why" | you / the AI, via `stamp` or a commit trailer |
| **decision** | an architecture decision (ADR) | stamped lessons that clear the ADR prose floor |
| **task** | standing intent — open work wired to files it `affects` | `.recall/tasks/*.md`, surfaced in the open-tasks track |
| **file** | a file-representative node | the code map, for files without a parsed symbol |

Only **claim-bearing** kinds (lesson, decision, task) can *drift* — the code map and
commit facts are regenerated or immutable, so they're never flagged stale.

There are also lightweight **access kinds** (recall, brief, explain, resolve, stamp)
— these aren't knowledge nodes, they tag read-path activity in the access log so the
dashboard's live console can show usage and the search flywheel can learn.

## The typed edges

An edge says *how* two nodes relate. The closed vocabulary (extendable per-project
in `rules.md` `edge_kinds`):

| edge | meaning |
|------|---------|
| **implements** | this node implements that decision/spec |
| **decided_by** | this is governed by that ADR/decision |
| **supersedes** | this replaces an older decision (the chain that keeps history honest) |
| **guarded_by** | this code is protected by that test/check |
| **warns_about** | this lesson warns about that code/pattern (a landmine marker) |
| **recurs_with** | this problem keeps coming back with that thing |
| **presents** | this surfaces/renders that |
| **relates_to** | a soft, untyped association |
| **depends_on** | this leans on that (drives the blast radius) |
| **co_changed** | these move together in history (mined from commits) |

The intelligence lives in the **edge**, not the retriever. `depends_on` +
`co_changed` are what `recall brief` reads to tell you *what breaks if you change
this file*; `supersedes` is what keeps a decision log from lying; `warns_about` is
what makes a past bug resurface on exactly the file where it bit.

## Why this matters

Because the knowledge is **structured** (typed nodes + typed edges) rather than a
blob of prose, recall can answer *relational* questions a doc folder can't: "what
depends on this", "what decision governs this", "what replaced ADR-12", "what
landmine is on this file". And because each claim-bearing node is SHA-anchored, the
moment the code moves past a stamped reason, recall flags it — it can't silently lie.

See also: [How it works](how-it-works) (write-time vs read-time) and
[Governance & drift](governance) (how drift is flagged, never auto-healed).

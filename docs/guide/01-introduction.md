---
title: Introduction
slug: introduction
order: 1
summary: What whatever-recall is, and the one idea everything follows from.
---

# Introduction

**With whatever-recall, your code becomes smart and self-aware.** The decisions,
the lessons, the "why", the plan, what breaks if you touch it, and the *right* name
to search for — all of it lives **in the code itself**, written at commit time and
read back offline in milliseconds, at **zero model tokens**. The code stops being a
dumb pile of text your AI has to re-figure-out every session, and starts knowing
its own reasons.

## The one principle: the code is the single source of truth

Every other tool keeps **two things**: the code, and a description next to it (an
Obsidian vault, a Confluence space, a `docs/` folder). Two separate places someone
has to keep in sync — and they never stay in sync.

> whatever-recall removes the second place. **The code is the one true home for
> knowledge, decisions, planning and truth** — not a doc store *next to* the code,
> but the code itself, aware of its own history and reasons.

```text
  EVERYONE ELSE:   Code  ║  docs/wiki/notes     two places, never in sync
  whatever-recall: Code = the truth             ONE place, the code knows itself
```

Because the knowledge lives *in* the code, it **can't go stale** — but that's a
**result**, not the pitch. The headline is simpler: your code is now smart.

## The two points in time

- **write-time** (expensive AI, once per piece of knowledge): while working, the AI
  stamps **anchors** — the technical terms at stake (migration numbers, symbol
  names, ADR IDs) — onto a knowledge edge. Nearly free; the context is already in
  its head.
- **read-time** (a dead-simple retriever, millions of times): on every edit / task
  start, a SQLite + FTS5 lookup returns the finished edge. No model, no tokens,
  sub-millisecond.

The intelligence lives in the **edge**, not in the retriever. That's why the
retriever can be dumb and lightning-fast — and why reading memory costs **0
tokens**.

## Who it's for

- **You**, in any AI coding session — so the AI doesn't re-derive context, silently
  undo a deliberate decision, or miss what a change breaks.
- **Your team** — a new teammate (or a fresh AI session) gets oriented from the code
  itself, not a stale onboarding doc.
- **Your AI agents** — recall docks onto every point where an AI fetches code
  context (see [Search-inversion](search-inversion) and the ecosystem map).

## Next

- **[Why it all connects](why-it-connects) — read this first.** The causal chain: how
  this one principle makes every feature follow, and why they reinforce each other.
- [Quickstart](quickstart) — hand the repo to your AI, or install it manually.
- [Core commands](commands) — `brief`, `recall`, `resolve`, `explain`, `stamp`.
- [Stamps & edges](stamps-and-edges) — the data model: kinds of node + the typed edges.
- [The 6 dimensions](six-dimensions) — the full picture you get before you edit.
- [Search-inversion](search-inversion) — why guessing the search term is the real cost.
- [Working with AI agents](agents) — the recall-first discipline; how AI ecosystems plug in.
- [Architecture](architecture) — what's under the hood (the engine is open source).
- [The dashboard](dashboard) — the browsable window onto everything recall knows.

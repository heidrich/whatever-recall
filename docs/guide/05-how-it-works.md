---
title: How it works
slug: how-it-works
order: 5
summary: Write-time stamping vs read-time retrieval, and why the read path is free.
---

# How it works

> **In the chain (links ② + ③).** This page is the engine of the whole product:
> *because the code is the truth, the AI stamps the meaning at write-time (②), which
> is exactly why reading it back is a 0-token lookup (③).* Free reading is what makes
> the [6 dimensions](six-dimensions) and [search-inversion](search-inversion)
> affordable on every edit. See [Why it all connects](why-it-connects).

## Write-time stamping, not read-time guessing (ADR-001)

The foundation. Other tools (and raw RAG) try to *understand the code at read time*
— every query re-derives meaning with an expensive model. recall flips it: the
expensive AI writes the meaning down **once, at commit time**, while the context is
already in its head. Reading it back is then a dumb, fast database lookup.

```text
  write-time (once, ~free):   AI stamps the WHY + anchors onto a knowledge edge
  read-time (millions, 0 tok): SQLite + FTS5 returns the finished edge, sub-ms
```

## The `.mind` index

`recall init` builds `.mind/index.db` (git-ignored), holding:

- the **code map** — symbols, files, importance, dependency edges (tree-sitter,
  no model);
- **commit facts** — immutable, can't go stale;
- **knowledge** — lessons, decisions/ADRs, tasks: the claim-bearing notes that CAN
  drift, and so are the only things drift-tracking watches.

## The three (now four) tracks

A query returns independent tracks, each ranked on its own axis so a loud one never
buries another (ADR-028): **code** (where, by importance), **knowledge** (why),
**blast radius** (what breaks), **open tasks** (standing intent). Search-inversion
(`resolve`) adds a vocabulary track on top.

## Anchors and edges

A stamp carries **anchors** — the rare, load-bearing terms (a migration number, a
symbol name, an ADR id) by which it should be found — and optionally **typed edges**
(`supersedes`, `guarded_by`, `depends_on`, …). The intelligence lives in the edge;
the retriever stays dumb.

## Why the read path is 0 tokens

There is no LLM in the read path — it's FTS5 ranking over text the AI already wrote.
That's the whole economic argument: orienting before an edit is **free**, so
skipping it never saves tokens; it only loses the why, the open tasks, and the
blast radius.

## Drift can't hide

Because there's one truth (the code) and the notes are SHA-anchored, a note whose
file moved on is flagged 🟡/🟠 — never silently trusted, never silently rewritten.
See [Governance & drift](governance).

## Measured, not claimed

The numbers (retrieval quality, token savings vs grep-and-read) are reproducible via
the scripts in `experiments/` and recorded in `docs/benchmarks.md`. Search-inversion
in particular: blind grep of a hallucinated term finds the real symbol 0/12 of the
time — that gap is what `resolve` closes.

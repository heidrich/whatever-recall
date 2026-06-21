---
title: The roadmap & the evolution
slug: evolution
order: 14
summary: Where whatever-recall is going — the release roadmap, and the long-horizon evolution to graph-native code structure. Today's folders and files are built for human reading; an AI navigates the dependency graph, not the tree. The horizon turns it around: the graph becomes the source of truth and the file layout becomes a view rendered from it, so an AI can structure and design code the way it thinks while recall renders it back to ordinary files for the compiler, the team and git.
---

# The roadmap & the evolution

> **This is the roadmap, not today's product.** Everything past v1.0 is where we are
> *going* — it is **not shipped yet**, and we'll always say so plainly. The numbers
> and order can shift as we harden on real user feedback.

## The release roadmap

We alternate **build** releases with **harden** releases — ship a layer, then
stabilise it on real feedback before the next one. The big leap (a native app and
graph-native structure) comes only after the foundation has paid for itself.

| Release | Focus | What it brings |
| --- | --- | --- |
| **v1.0** | **The foundation — shipping now** | Write-time memory, 0-token reads, the pre-edit gate, search-inversion, the predicate trust-flag. The real, proven product. |
| **v1.1** | **Hardening** | Priority bugs, polish, and the first wave of user feedback — no new surface, just solid. |
| **v1.2** | **The app & deeper features** | A real native app (not the local dashboard), with more — including write-side features, real project planning, and a proper team wiki. |
| **v1.3** | **Hardening** | Stabilise the app on feedback from real teams. |
| **v1.4** | **The evolution — graph-native** | The horizon below: the graph as the source of truth, files rendered from it. The deepest change, taken only when the ground under it is solid. |

The rest of this page explains the last step — **the evolution** — because it's the
one that changes how AI reasons about code.

> **This is a horizon, not a v1.0 checkbox.** It's where we're headed once the app
> exists and the foundation has earned it.

## The idea in one line

Today your code lives in **folders and files** — a structure invented for *human*
reading. An AI doesn't navigate by folders; it navigates the **graph** of how the
code actually connects. recall already holds that graph. The evolution is to make
**the graph the source of truth, and the file layout a view rendered from it.**

https://www.youtube.com/watch?v=5YOvo4bZjFg

*Early preview of v1.2 — not shipped yet. The blocks are files and symbols, the lines
are recall's real edges (`depends_on`, `co_changed`, `implements`, `guarded_by`,
`relates_to`, `decided_by`), and the flowing light is data moving along them.*

## Why this is the logical next step

It's the same move as [search-inversion](search-inversion), one layer deeper:

- **Search-inversion** said: the AI shouldn't have to guess the human *name* for a
  thing — recall corrects it to the repo's real vocabulary.
- **Structure-as-render** says: the AI shouldn't have to inhabit the human
  *structure* either — file boundaries, one-symbol-per-file, folder trees are human
  reading crutches, not how an AI reasons about code.

recall is uniquely able to do this because it already collected the structural truth
at write-time: `depends_on`, `co_changed`, `decided_by`, `implements`, importance.
The folder tree is, today, only the human *projection* of that graph. We invert the
projection.

## How it would work (honestly staged)

We build this in steps, each useful on its own — no big-bang:

1. **Now / cheap — recall *suggests* structure.** Instead of imposing anything,
   recall points out where code semantically belongs: *"this symbol lives in file Y
   but co-changes 80% with cluster X."* A read over the graph recall already has.
2. **A graph-first view.** The dashboard groups code by meaning (the dependency and
   decision graph), not by folder. The folder tree becomes *a* filter, not *the*
   truth.
3. **Structure as a render (v1.2).** The AI works the graph; recall renders it back
   to ordinary files for your compiler, your team and git. The execution layer is
   still file-based — Python imports by path, bundlers by module, git diffs lines —
   so this only works by **rendering back to files at build/commit time.** That
   render/round-trip layer is the real engineering, and it's the feature.

## What it does *not* change

- **Your repo stays a normal repo.** It still compiles, still diffs in git, still
  reads as files for any human who opens it. The graph-native view is *added*, the
  file view is *rendered* — never taken away.
- **recall still never edits your code without you.** The same hands-off contract as
  v1.0: nothing is restructured silently; you approve, as always.

## The honest boundary

This is a horizon we're committing to, not a checkbox that's done. When parts of it
ship, they'll land in the [changelog](https://whatever-recall.com/changelog) with the
same rule as everything else: only measured, only real, and clearly marked.

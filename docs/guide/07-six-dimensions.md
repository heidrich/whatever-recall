---
title: The 6 dimensions
slug: six-dimensions
order: 7
summary: The full picture recall gives you before you touch a file — and the review raster every change runs through.
---

# The 6 dimensions

> **In the chain (link ④).** You only get to read *six* dimensions before an edit
> because the [read path is free](how-it-works) — if orienting cost tokens, nobody
> would pull the full picture every time. This is the payoff of links ② and ③: a
> complete profile, affordably, on every file. See [Why it all connects](why-it-connects).

"The 6 dimensions" mean two related things in recall: **the picture you read before
you edit a file**, and **the raster every change is reviewed against**. Same six
lenses, once for understanding, once for checking.

## 1. The pre-edit picture — what recall tells you before the first keystroke

When you run `recall brief <file>`, it answers six questions at once — the complete
profile of a file *before* you change it, at zero model tokens:

```text
                         ┌──────────────────────────┐
                         │      recall brief X       │
                         └────────────┬─────────────┘
        ┌───────────────┬─────────────┼─────────────┬───────────────┐
        ▼               ▼             ▼             ▼               ▼
   ① WHAT is it    ② WHERE       ③ WHY        ④ WHAT BREAKS    ⑤ OPEN TASKS
   (kind/role)     (the file +   (decisions/   (blast radius,   (standing
                    its symbols)  lessons/SHA)  depends_on)      intent)
                                        │
                                        ▼
                                  ⑥ THE TRAIL
                              (how this fits the
                               subsystem — the path
                               to understanding)
```

- **WHAT** — the identity/role (function, route, migration, component…).
- **WHERE** — the address: the file, its symbols, its territory.
- **WHY** — the decisions, lessons and the SHA they were stamped at.
- **WHAT BREAKS** — the blast radius: what depends on it, what it leans on.
- **OPEN TASKS** — the standing intent wired to the file: read it first, treat it
  like a failing test.
- **THE TRAIL** — the route from "confused" to "I get it" for the subsystem this
  file lives in (the deepest dimension; built up over time).

The point: a classic wiki gives you a paragraph *next to* the code. recall gives you
the full six-sided profile *of the code itself*, so you never silently undo a
deliberate decision or miss what a change breaks.

## 2. The review raster — the six checks every change runs through

The same idea, turned into a checklist. After every feature and before every push,
recall's audit discipline (and the recall-first audit workflow) walks six review
dimensions — the canonical list lives in `docs/audit-dimensions.md`:

```text
   ┌─ 1 Auth-Guard ──────── is every mutation authenticated AND authorized?
   ├─ 2 Audit-Log ───────── does every state change record who/what/target?
   ├─ 3 Validation ──────── is every input shape/type checked before use?
   ├─ 4 Error-Handling ──── does every call check its result and fail safely?
   ├─ 5 State-Updater ───── is client state updated correctly (no stale/no side-fx)?
   └─ 6 No-Secrets/Cleanup ─ no secret to the client; timers/locks released?
        + standing lenses: business-logic/race/money · injection/takeover
```

A finding in any dimension is a **blocker, not a TODO**. The raster is run
recall-first (orient before you judge — a "smell" that's a stamped decision is a
false alarm) and findings are verified adversarially (most raised findings are false
alarms — three skeptics, majority-real survives).

## Why six, and why it compounds

Each dimension on its own is a known good practice. Read **together, before the
first line of code**, they form a profile no single tool gives you — and because
they're read from the code's own write-time memory, they get **richer with every
commit**: more decisions stamped, more edges drawn, more trail recorded. Old code
starts with a thin profile; worked-in code has a deep one. The picture sharpens
exactly where the most work happens.

See [Stamps & edges](stamps-and-edges) for the data behind the dimensions and
[Working with AI agents](agents) for how the raster is run by an agent fleet.

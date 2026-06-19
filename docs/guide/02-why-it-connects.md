---
title: Why it all connects
slug: why-it-connects
order: 2
summary: The causal chain — how one principle (the code is the truth) makes every feature follow, and why they reinforce each other instead of just sitting side by side.
---

# Why it all connects — the causal chain

This is the most important page to read. Everything else in recall —
[stamps](stamps-and-edges), the [6 dimensions](six-dimensions),
[search-inversion](search-inversion), [self-healing](self-healing) — is **not a
bag of separate features**. It's **one idea, and a chain of consequences that each
follow from the one before**. If you understand the chain, the rest of the product
reads itself.

> The point of this page: not just *what* each piece does, but **why it has to be
> there** — what would break in the chain if it were missing.

## The one cause: the code is the single source of truth

Everything starts here. recall keeps knowledge **in the code**, not in a doc store
next to it. That single decision is the cause; every feature below is an *effect* of
it. So whenever you wonder "why does recall do X?", the answer always walks back to:
*because the truth lives in the code.*

## The chain, link by link

```text
  ① THE CODE IS THE TRUTH
        │  (so the knowledge must be written where the code is touched…)
        ▼
  ② WRITE-TIME STAMPING            the expensive AI writes the WHY + anchors
        │   once, at commit time — context already in its head
        │  (so reading it back needs no model…)
        ▼
  ③ 0-TOKEN READ PATH             a dumb SQLite+FTS5 lookup returns the finished
        │   edge in sub-ms — orienting is FREE
        │  (free + complete ⇒ you can afford to read the WHOLE picture…)
        ▼
  ④ THE 6 DIMENSIONS              what/where/why/what-breaks/tasks/trail —
        │   the full profile before the first keystroke
        │  (the AI still has to NAME what it wants to find…)
        ▼
  ⑤ SEARCH-INVERSION             fix the hallucinated search term into THIS
        │   repo's real vocabulary, before the grep
        │  (code moves on, so some notes will go out of date…)
        ▼
  ⑥ SELF-HEALING                 drift is flagged and corrected at the next
            commit — the memory gets TRUER the more you work
            │
            └─► feeds back into ② — the loop closes, the brain compounds
```

Read the arrows out loud — each one is a *"so…"*. That's the whole product in one
breath: **the code is the truth, so you stamp at write-time, so reading is free, so
you can read all six dimensions, so the AI needs the right name, so you invert the
search, so the notes must stay true, so they self-heal — which makes the next stamp
better.** A loop, not a list.

## Why each link *needs* the one before it

This is the part "what does each feature do" pages never tell you. Each feature is
only possible **because** of the link above it — pull one out and the chain breaks:

| Link | Why it exists | What breaks without the link above it |
|------|---------------|----------------------------------------|
| ② Write-time stamping | The truth is in the code, so the knowledge has to be captured *where and when the code is touched.* | If knowledge lived in a separate doc, it would drift the instant the code changed — the thing every wiki dies of. |
| ③ 0-token read path | Because the meaning was already written at write-time, read-time needs no model — just a lookup. | Without write-time capture, every read would re-run an expensive model. You'd ration reading; you'd skip orienting; you'd lose the why. |
| ④ The 6 dimensions | Reading is **free and complete**, so you can afford the *whole* picture before editing, not a snippet. | If reading cost tokens, nobody would pull six dimensions every edit. The full profile is only affordable *because* the read path is free. |
| ⑤ Search-inversion | The picture is only useful if the AI can **find** the right thing — and an AI invents names from its training, not this repo. | Without the repo's real vocabulary (collected at write-time), the AI greps a hallucinated name, misses, and burns the loop. The 6 dimensions are worthless if you can't reach them. |
| ⑥ Self-healing | One truth + SHA-anchored notes ⇒ a note that no longer matches the code is **detectable**, so it can be fixed at the next commit. | With two sources of truth there's nothing to check a note *against*. Drift would be invisible, and the memory would slowly lie. |

## Why it compounds (the flywheel) — and why that matters to you

Look at the loop: ⑥ feeds back into ②. **Correcting a note happens when you touch
the code** — so the most-worked-on code gets the truest memory, fastest, and the
vocabulary the search learns grows from what *actually worked*. Nothing decays into
lies; mistakes have a half-life. **The longer the project lives and the more it's
worked in, the smarter its code becomes — on its own.**

That's the difference between a *list of features* and a *self-reinforcing system*:
a feature is a thing you have; this is a thing that gets **better while you work**,
because every link feeds the next.

## So when you read the rest of the docs…

Each following section is one link in this chain, and it will say so. Hold this
picture in your head and every page lands as *"oh — that's why this exists":*

- [How it works](how-it-works) → links ② and ③ (write-time vs read-time).
- [Stamps & edges](stamps-and-edges) → the data that makes ② possible.
- [The 6 dimensions](six-dimensions) → link ④.
- [Search-inversion](search-inversion) → link ⑤.
- [Self-healing](self-healing) → link ⑥ and how it closes the loop.
- [Architecture](architecture) → how the chain is built (the engine is open source).

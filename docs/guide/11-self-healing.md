---
title: Self-healing
slug: self-healing
order: 11
summary: How the memory repairs itself — when a note no longer matches the code, it gets noticed, flagged, and actively corrected at the next commit.
---

# Self-healing

> **In the chain (link ⑥ — and it closes the loop).** Healing is only *possible*
> because there's **one** truth to check a note against (the code) and notes are
> SHA-anchored. With two sources of truth there'd be nothing to detect drift against.
> And because correction happens at the next commit, ⑥ feeds back into write-time
> stamping (②) — the loop closes and the brain compounds. See
> [Why it all connects](why-it-connects).

A normal wiki rots: the code moves on, the doc beside it quietly goes wrong, and
nobody notices until it bites. recall's memory does the opposite — it **heals
itself**. When a stamped reason no longer matches the code, that mismatch gets
*noticed*, *flagged*, and *actively corrected* — instead of silently lying.

## What it is

Self-healing is the loop that keeps the memory true to the code:

1. **Notice** — while working (often during a search or a pre-edit briefing), the AI
   reads a stamped note and sees it no longer fits the code in front of it.
2. **Flag** — drift detection marks the note (🟡 the file changed since it was
   stamped / 🟠 uncommitted edits). It's never silently trusted.
3. **Correct at the next commit** — the fix is applied *at write-time*, the moment
   the code is touched: the note is rewritten to match today's code and re-stamped
   against the new SHA → back to 🟢.

The key idea: the correction happens **where and when the work happens** — at the
commit — not in some separate "doc cleanup" task that never gets done.

## A concrete example (the search case)

You ask recall to find something, and `recall resolve` / the briefing surfaces a
stamped note that says "the seat ceiling is enforced in `confirmSeatOrRollback`."
But you're looking at the code and the real enforcement has moved to a database RPC.
The note is now **wrong**.

Self-healing means you don't just shrug and move on:

- recall has already **flagged** that note as drifted (its SHA no longer matches).
- You **note** the mismatch and, when you make your next commit touching that area,
  you **correct the stamp** — one sentence, re-anchored to the real code.
- From then on, everyone (and every future AI session) gets the *right* answer.

The wrong information had a **half-life**, not a permanent home.

## Where and when it happens

- **Where:** in the code, at the stamp — never in a separate document. The note
  lives where the truth lives, so healing it is part of editing the code.
- **When:** at the next commit that touches the area (the post-commit hook re-checks
  freshness; the drift light clears). Drift is surfaced continuously — in the
  pre-edit briefing, the dashboard Drift tab, `recall freshen` — so you see it the
  moment you're in the relevant file.

## Why it works — the flywheel

Because correction is tied to *touching the code*, the most-worked-on (and therefore
most-important) code gets correct **fastest**, and code nobody touches stays as-is
(and nobody's relying on it anyway). Mistakes don't accumulate — they decay. The
longer a project lives and the more it's worked in, the truer its memory becomes.
That's the same flywheel behind [search-inversion](search-inversion): the brain gets
smarter with every commit, on its own.

## The one rule: never heal silently

recall **detects and proposes**, it does **not** rewrite a pinned note on its own.
When drift is found, the AI offers the resolution and the human decides (the 95%
rule):

- **Reality moved on, the note is wrong** → rewrite the note to match today's code,
  re-stamp → 🟢.
- **The note was right, the CODE drifted from a deliberate decision** → that's a
  regression/bug → fix the *code*, don't "heal" the note away.

Either way the human stays in charge of the truth. See
[Governance & drift](governance) for the full drift policy.

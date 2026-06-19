---
title: Private knowledge stays local
slug: privacy
order: 13
summary: Your decisions never leave your machine unless you choose to share them.
---

# Private knowledge stays local

recall's whole value is the **why** behind your code — the decisions, the trade-offs,
the "we tried X and undid it." That's also the most sensitive thing about a codebase.
So recall is built on one rule: **your knowledge never leaves your machine unless you
say so.** Not the code, not the reasons behind it.

This isn't a setting you have to find. It's how the engine is shaped.

## Where the "why" lives

Every decision you stamp lives in the **brain** — the local `.mind/` index — *not* in
your source files. Your code stays byte-for-byte what it was; the reasoning sits beside
it, never inside it. That means there is nothing to scrub out of your code before you
ship it, because the knowledge was never in the code to begin with.

`.mind/` is git-ignored by default. It does not get pushed, it does not get published,
it does not end up in a release.

## Per-note visibility: team vs. private

Each note carries a **visibility**:

- **team** (the default) — shareable. If your team chooses to share a brain, these
  travel with it.
- **private** — yours alone. Never included in an export, never in a shared brain.

```sh
recall stamp "we pay Stripe extra to avoid PCI scope — revisit at 5k MRR" --private
```

A note can only get *more* private, never less: re-stamping a private note keeps it
private. You can't accidentally widen something you meant to keep to yourself.

Set the default for a whole project in `.recall/config.toml` (see
[Build & share settings](#build--share-settings)) — e.g. a client repo where
*everything* should stay local unless you opt a note out.

## Sharing a brain — without leaking

Teams that *want* shared memory get it, on their terms. `recall export` writes a
**shareable copy** of the brain with every private note — and its traces — removed:

```sh
recall export --out .mind/shared.db
```

Whether your team shares that file at all is your team's call. What recall guarantees
is *what's in it*: team knowledge, never private. The export runs a fail-closed gate
before it writes a single byte — if anything private would survive, it aborts and
deletes the half-written file rather than risk a leak.

## Two waterproof guards

The promise is enforced in two independent places, both **fail-closed** (a missing or
malformed config never loosens them):

1. **Export gate** — `recall export` refuses to produce a brain that still contains
   private content. No survivors, no orphaned traces, or it aborts.
2. **Commit guard** — `recall check-leak` blocks a commit that stages a brain holding
   private notes. Wire it into your pre-commit hook (`recall hook --install` does this)
   and a private brain physically cannot enter git.

Either guard alone would do the job; together they mean a private decision can't reach
a teammate's clone or a public repo by mistake — not through export, not through a
stray `git add`.

## Build & share settings

The project's sharing rules live in one git-tracked file, `.recall/config.toml`, under
a `[share]` block:

| Key | What it does | Safe default |
| --- | --- | --- |
| `default_visibility` | `team` or `private` — the visibility a new stamp gets when you don't pass `--private` | `team` |
| `block_raw_mind_commit` | the commit guard — block staging a brain with private notes | `true` |
| `dry_run_before_export` | run the no-private gate before any shareable write | `true` |
| `export_path` | where `recall export` writes the shareable brain | `.mind/shared.db` |

Because it's the *one* source of truth, every surface reads the same values — the CLI,
the pre-commit guard, and the **Build Settings** panel in the dashboard (the button by
the console; it goes red and pulses when a security guard is off). And when you set
`default_visibility = private`, recall injects that into your AI instruction file, so
every assistant session knows new notes stay local here **without being told**.

## What this means for you

- You can let recall capture the real reasoning — bluntly, the way you'd say it to a
  colleague — because it never has to be sanitised for shipping.
- A solo developer ships a clean codebase by construction.
- A team shares the knowledge it *wants* to share, and nothing it doesn't.
- An open-source maintainer can keep the engine public and the war stories private.

Your code is the public artifact. Your reasoning is yours. recall keeps them apart on
purpose.

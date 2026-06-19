---
title: Search-inversion
slug: search-inversion
order: 8
summary: Correct a hallucinated search term into this repo's real vocabulary, before the grep.
---

# Search-inversion

> Everyone else optimizes the **hit** — "find what was asked for, better". recall
> optimizes the moment **before**: *what is even searched for.*

<!-- -->

> **In the chain (link ⑤).** The [6 dimensions](six-dimensions) are worthless if the
> AI can't *reach* them — and an AI invents names from its training, not from this
> repo. Search-inversion exists because the repo's real vocabulary was collected at
> write-time (②); no grep and no embedding has it. It's the bridge from "the picture
> is there" to "the AI actually finds it." See [Why it all connects](why-it-connects).

## Why this matters more than ever now

In an AI coding session, search is no longer a human typing a word they half-remember
— it's a model **inventing** a name from its training and grepping for it. The model
knows the *general* world, not *this* repo, so it guesses `enforceSeats` when the repo
says `confirmSeatOrRollback`. The grep misses, it guesses again, it burns tokens and
wall-clock on a loop that produces nothing. That loop — guess → grep → miss → guess —
is the **guessing tax** on every AI coding session, and it's invisible because it
looks like "the AI is working".

recall is the only tool positioned to end it, because the fix has to come from
*write-time*: only recall has collected what this repo really calls things, and why.
grep matches strings; embeddings guess similarity; **only recall knows the repo's
actual vocabulary and its lived experience.** That's why the search isn't a feature
bolted on — it's the natural consequence of the code being smart and self-aware.

## The problem: vocabulary mismatch

An AI invents a search term from its training — `enforceSeats`, `seatLimit`,
`checkSeats` — but THIS repo calls it `confirmSeatOrRollback`. So the grep finds
nothing, the AI guesses again, and tokens burn. This is a 30-year-old IR problem
called **vocabulary mismatch** (an average query misses 30–40% of the relevant
documents). Measured here: blind grep of a hallucinated term finds the real symbol
**0 / 12** of the time.

The known fix is query expansion — but everyone expands from text statistics or
embeddings (guessing similarity). recall expands from the repo's **write-time lived
experience**, which no grep and no embedding has. That's the recall-shaped gap.

## `recall resolve <guess>`

```sh
$ recall resolve seatLimit
recall · resolve  seatLimit   [warm, index warmth 53%]
  no exact 'seatLimit' here — this repo most likely means:
  1. seatsUsed          (web/src/lib/server/orgs.ts:249)
  2. seatsFor           (web/src/app/api/billing/webhook/route.ts:32)
  → this repo says 'seatsUsed', not 'seatLimit'.
```

## How it ranks (two layers, never blended)

1. **Vocabulary RANKS.** Tokenize the guess (camelCase/snake split + a tiny
   plural stemmer — the CamelCase boundary is exactly where blind grep returns 0),
   then score by **IDF-weighted coverage**: sharing the rare token (`seat`)
   outweighs a common one (`limit`). A literal-similarity trigram adds a little.
2. **Experience is a TIE-BREAK only**, never an override (the hard ADR-028 lesson:
   folding experience into the headline rank buries the vocabulary-right symbol
   under experienced-but-wrong ones). access_log + node_feedback + importance.

## The flywheel: learned synonyms

A WARM repo also resolves **synonyms it learned from what actually worked** — guesses
that historically landed on a symbol with **zero spelling overlap**:

```sh
$ recall resolve cancel        # shares no token with the target
  → lapseOrgByCustomer   (learned: searches for 'cancel' landed here)
```

A COLD repo has none of this — it gets pure vocabulary correction, honestly ("I know
the words, not their history yet"). The cold→warm climb is the flywheel, and it's
measurable.

## The anti-hiding gate

Experience as a ranking signal has one danger: it could bury fresh, unstamped code
that has no history. So `resolve` **re-ranks and annotates but NEVER drops a
candidate** — grep stays the complete recall; resolve only adds order + the why on
top. A new symbol with a real token match still appears.

## Where it docks

This is docking point #3 of the ecosystem. Exposed as the CLI (`--terse` /
`--for-prompt`), the MCP `resolve` tool + `/resolve` prompt, and `Index.resolve()`.
The deeper stages — intent refinement, the "trail" understanding-path dimension, and
the cross-language typed-edge foundation that makes the SQL RPC the parser never
sees findable — are the post-launch block. See ADR-037 in `docs/decisions.md`.

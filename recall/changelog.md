# Changelog

What changed, in plain words. This file ships with the engine — the dashboard's
**Changelog** tab and [whatever-recall.com/changelog](https://whatever-recall.com/changelog)
render exactly this text, so the tool and the site can never tell different stories.

Versioning follows [semver](https://semver.org): MAJOR.MINOR.PATCH. Anything that
could break your existing `.mind/` index or your workflow lands only in a MAJOR
release and is called out explicitly.

## Coming next — the roadmap

We alternate **build** releases with **harden** releases — ship a layer, then
stabilise it on real feedback before the next. Nothing below is shipped yet, and
we'll always say so plainly.

- **v1.1 — hardening.** Bug-fixing and polish from real-world use. No workflow
  changes; your `.mind/` index stays exactly as it is.
- **v1.2 — the app & deeper features.** A real native app (not just the local
  dashboard), with more: write-side features, real project planning, and a proper
  team wiki.
- **v1.3 — hardening.** Stabilise the app on feedback from real teams.
- **v1.4 — structure as a render (the horizon).** Today your code lives in folders
  and files — a structure built for *human* reading. An AI navigates by the graph,
  not the tree. recall already holds that graph (every dependency, every decision,
  every link). This step turns it around: **the graph becomes the truth, and the
  folder/file layout becomes a view rendered from it** — so the AI can organize and
  design code the way it actually thinks, while recall renders it back to ordinary
  files for your compiler, your team and git. The same move as search-inversion, one
  layer deeper. The deepest change, taken only when the ground under it is solid.

## v1.0.3 — your notes keep their own identity

A fix and a small feature, both about one thing: **a note is its own note.**

- **Fixed — two different notes on the same file no longer merge into one.** When you
  stamped a second decision onto a file that already had one, recall could fold it into
  the first and lose what you just wrote. Now a merge only happens for a real
  re-statement (the same note, said again) — a new title is a new note, even on the same
  file. Nothing you write gets quietly swallowed.
- **New — `recall stamp --id <n>` edits exactly one note.** Like editing a task by its
  id instead of its name: same name allowed across notes, the id is the identity. Fast,
  unambiguous, no guessing which note you meant.

## v1.0.0 — whatever-recall is live 🎉

This is the launch. **whatever-recall is now publicly available** — give your code a
memory, and let any AI read it back in milliseconds at zero tokens. Everything below
is in this first release; nothing here is "coming soon".

**Start here:** [Install](https://whatever-recall.com/install) ·
[Docs — the full guide](https://whatever-recall.com/docs) ·
[How it works](https://whatever-recall.com/how-it-works) ·
[Pricing](https://whatever-recall.com/pricing) ·
[Source on GitHub](https://github.com/heidrich/whatever-recall) ·
[Report an issue](https://github.com/heidrich/whatever-recall/issues)

Here's everything you get on day one.

### The engine

- **`recall init .`** builds your project's memory locally: code map, decisions,
  lessons, commits and tasks in one SQLite file inside your repo (`.mind/`).
  No cloud, no telemetry — delete the folder and it's gone.
- **Reading costs zero tokens.** Recall answers from the index in milliseconds,
  with three parallel tracks: the relevant *code* (ranked by importance), the
  *knowledge* behind it, and the *blast radius* — what breaks if you change it.
- **The memory keeps itself fresh.** Git hooks stamp every commit; the watcher
  re-indexes new commits on its own; drifted knowledge is flagged, never
  silently trusted.
- **Power Mode (optional):** connect *your* AI (Claude Code CLI, Ollama, an
  Anthropic key, or any OpenAI-compatible endpoint) to enrich the index with
  semantic summaries. With a subscription CLI it costs nothing extra.
- **Search inversion (`recall resolve`):** an AI often *guesses* the wrong search
  term from its training. recall corrects the term into your repo's real
  vocabulary *before* anything is grepped — the moment everyone else ignores.
- **Static code intelligence — token-free, offline.** `recall callers` /
  `callees` (who depends on a file, and what it depends on), `impact` (what a
  change touches: empirical co-change + structural dependents — a 0-token
  stand-in for call-hierarchy), `precedent` (the most analogous past decisions,
  with how each turned out), plus `dead-code`, `untested` and `cycles`
  candidates — all read straight off the index, no model, no tokens.
- **Private knowledge stays local.** The *why* behind your code lives in the
  local brain (`.mind/`), never in your source — so your code ships clean by
  construction, with nothing to scrub before you publish. Every note carries a
  visibility: **team** (shareable) or **private** (`recall stamp "…" --private`,
  yours alone). `recall export` writes a shareable brain with all private notes
  stripped, and two fail-closed guards make it physical — the export aborts
  rather than leak, and `recall check-leak` blocks a commit that would stage a
  private brain. Your decisions never leave your machine unless you choose to
  share them.

### The dashboard

- **`recall dashboard`** — the browsable wiki: Overview, Wiki (causal chains
  down to the diff), Tasks, Git, Drift, Code (with the pre-edit briefing),
  Product tree, Search, and this Changelog. Live pulse, one design system.
- **Tasks police their own status.** A task with every step ticked but still
  marked `open` gets flagged — in the dashboard and as a one-line nudge on
  every commit — until you flip it to done. Your backlog can't quietly rot.
- **`recall shortcut`** — a desktop launcher (with the recall icon) that starts
  the dashboard with a double-click. No terminal, no AI needed. It runs the
  dashboard in the background (`recall tray` — a system-tray icon with the
  `[tray]` extra, otherwise a console that loudly warns *don't close me*), so a
  closed window can't silently take the server down. `recall stop` ends it
  cleanly. And if the server ever blinks (a restart), the dashboard's live pill
  reconnects in about a second instead of hanging.
- **GitHub and Discord** are one click away — icons in the dashboard header link
  straight to the repo and the community.
- **Built with** — the About tab credits the stack recall stands on (Vercel,
  Supabase, Claude Code, Next.js, React, TypeScript, Python, Stripe, Resend),
  with offline inline logos.

### The doors for your AI

- **MCP server** (`recall mcp`) — 14 native tools in Claude Code, Cursor and any
  MCP client: recall, brief, explain, resolve, stamp, contested, freshen,
  dashboard, impact, precedent, callers, dead_code, untested, cycles.
- **CLI** — `recall "<question>"`, `brief <file>` (read before you edit),
  `explain` (onboarding), `review <commit>`, `contested`, `precommit-check`,
  plus the code-intelligence commands (`callers`, `callees`, `impact`,
  `precedent`, `resolve`, `dead-code`, `untested`, `cycles`). `recall --version`
  reports the installed release.
- **Built for agents and hookless harnesses.** `recall push` gives a subagent the
  same task-scoped brief + landmines + live `🔴 BROKEN` warnings the pre-edit gate
  would show, over the shell; `recall ack <file>` confirms a briefing so the gate
  lets the edit through. `recall sync-context` writes recall's live state into your
  AI instruction file (`CLAUDE.md` / `AGENTS.md` / Copilot) so every client loads it
  with no tool call. `recall receipt` reports what recall actually did over a
  window — in measured counts, never an invented token or dollar figure.
- **rules.md** — the transparent, downloadable contract that tells an AI agent
  exactly how to use the memory. No hidden prompts.

### Accounts & licensing

- **14-day full trial, no card.** Sign up with email or Google / GitHub /
  Discord. One plan — **Developer**, every feature included; you only buy seats:
  $29/seat·month or $290/seat·year (2 months free yearly), minimum 1 seat.
- **Offline license tokens** (Ed25519): the tool verifies your license without
  ever seeing your code — when a trial ends, reading your memory keeps working;
  stamping new knowledge needs a plan.
- The full source stays public on GitHub (Business Source License 1.1) — read
  every line that touches your repo before you run it. Noncommercial use is
  free; commercial production use needs a plan; and every version becomes
  Apache 2.0 open source three years after its release.

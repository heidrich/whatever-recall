# Changelog

What changed, in plain words. This file ships with the engine — the dashboard's
**Changelog** tab and [whatever-recall.com/changelog](https://whatever-recall.com/changelog)
render exactly this text, so the tool and the site can never tell different stories.

Versioning follows [semver](https://semver.org): MAJOR.MINOR.PATCH. Anything that
could break your existing `.mind/` index or your workflow lands only in a MAJOR
release and is called out explicitly.

## v1.0.0 — the launch release *(upcoming)*

The first public release — everything below ships on day one.

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

### The doors for your AI

- **MCP server** (`recall mcp`) — recall, brief, explain, stamp, contested and
  freshen as native tools in Claude Code, Cursor and any MCP client.
- **CLI** — `recall "<question>"`, `brief <file>` (read before you edit),
  `explain` (onboarding), `review <commit>`, `contested`, `precommit-check`.
- **rules.md** — the transparent, downloadable contract that tells an AI agent
  exactly how to use the memory. No hidden prompts.

### Accounts & licensing

- **14-day full trial, no card.** Sign up with email or Google / GitHub /
  Discord. Three paid tiers that differ only in seats: Solo · Team · Studio.
- **Offline license tokens** (Ed25519): the tool verifies your license without
  ever seeing your code — when a trial ends, reading your memory keeps working;
  stamping new knowledge needs a plan.
- The full source stays public on GitHub (Business Source License 1.1) — read
  every line that touches your repo before you run it. Noncommercial use is
  free; commercial production use needs a plan; and every version becomes
  Apache 2.0 open source three years after its release.

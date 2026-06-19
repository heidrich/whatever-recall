---
title: The dashboard
slug: dashboard
order: 15
summary: The browsable window onto everything recall knows — every tab, explained.
---

# The dashboard

`recall dashboard` opens a local web app — the window onto everything recall knows.
It's the same model-free read path as the CLI, made visual and navigable. Nothing
leaves your machine; it's a local server reading `.mind`.

```sh
recall dashboard          # opens on localhost
recall tray               # same, with a tray icon / don't-close console
recall stop               # stop the server
```

## The tabs

- **Start here** — the orientation path for a fresh session (same as `recall
  explain`): the load-bearing files, the must-know decisions, what's in flight,
  where the team burns time. Open this first in an unfamiliar repo.
- **Overview** — the dashboard home: recent activity, "recently learned" knowledge,
  the connection/health state, and a live activity console streaming reads
  (brief/recall/explain/stamp) across the CLI, MCP and dashboard as they happen.
- **Brain** — the knowledge feed: every lesson/decision, newest first, rendered with
  its "why", its SHA, and a "what this file touches" badge. Click a lesson to walk
  it as a **causal-chain story** — decision → consequence → the pre-edit briefing of
  the file it names → what breaks. Every file path is clickable.
- **Tasks** — the standing intent: open tasks with their `affects` files and a
  progress bar for checklist items. Flags tasks left open too long, or marked done
  with unchecked items (the contradiction the tool exists to kill).
- **Drift** — the honesty light: claim-bearing notes whose file has moved on since
  they were stamped (🟢 fresh / 🟡 changed / 🟠 uncommitted edits). Drift is
  *flagged*, never auto-healed — you resolve it with the owner's OK
  ([Governance & drift](governance)).
- **Search** — ask the index by concept. Renders the tracks side by side: code (by
  importance), knowledge (by relevance), blast radius. Every row is clickable —
  code/blast open the file, knowledge opens the brain entry.
- **Code** — the code map + file tree: symbols, importance, the dependency edges.
  The structural half of what `brief` reads.
- **Graph** — the dependency / causal graph: the typed edges drawn as a navigable
  map, so the blast radius and the decision chains are something you can *see*.
- **Changelog** — the same release notes the website `/changelog` shows, served
  from the engine's own changelog (one source, no drift).
- **About** — what recall is, the version, the links (GitHub, Discord), and the
  "built with" credits.

## The connection pill + live console

The header shows a single `connection · n/4` pill folding the health of the read
modes; hover for the exact, manual fix command per mode (it never auto-fixes). The
floating **activity console** streams every read across all three surfaces (CLI,
MCP, dashboard) over one shared `.mind` — proof, in real time, that the memory is
being used and costs nothing to read.

## Why a dashboard at all

The CLI and MCP are for *acting*; the dashboard is for *seeing*. It turns the
abstract ("the code remembers") into something walkable — the brain as a story, the
blast radius as a graph, drift as a light. It's also the fastest way to show a new
teammate (or yourself, after a break) what the project actually knows.

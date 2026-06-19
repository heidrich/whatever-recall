---
title: Working with AI agents
slug: agents
order: 9
summary: The recall-first discipline — and why subagents reach recall via the CLI, not MCP.
---

# Working with AI agents

recall is built to sit at **every point where an AI fetches code context** — not
just the human-in-the-loop session. Most tools help the human; recall plugs into the
AI's whole workflow, so the engine you and your team build gets stronger with every
commit instead of being re-derived from scratch each session.

## How an AI ecosystem plugs in — the five docking points

There are five moments where an AI reaches for code context. recall docks onto each:

| # | Point | What recall does |
|---|-------|-----------------|
| 1 | **Subagents** | a spawned review/audit agent starts blank → it runs `recall brief --terse` first, so it inherits the why instead of re-guessing it |
| 2 | **Pre-edit** | `recall brief <file>` before any edit — the gate below |
| 3 | **Search / locate** | `recall resolve <guess>` corrects the hallucinated term before the grep ([Search-inversion](search-inversion)) |
| 4 | **Compaction / handoff** | `recall handoff` snapshots the in-flight state so the next session rebuilds from recall, not an ad-hoc summary |
| 5 | **Git hooks** | pre-commit risk-warning + post-commit auto-stamp |

The full map + status lives in `docs/ecosystem-docking.md`. Claude Code is the first
complete client — the same MCP + CLI surface works for any AI agent (every AI has
the same blank-subagent and vocabulary-mismatch gaps).

## Wiring recall into your AI client

recall speaks two universal protocols, so it docks into **any** AI coding tool — not
just one. The same engine, two transports:

- **MCP** (Model Context Protocol) — for the interactive session and real users. Every
  MCP client (Claude Code, Cursor, VS Code / GitHub Copilot, Windsurf, Zed, Cline, …)
  registers recall with the **same server block**; only the config file location
  differs. Run `recall mcp --print-config` to print the exact snippet for each:

  | Client | Where the config lives |
  | --- | --- |
  | Claude Code | `claude mcp add recall -- recall mcp`, or a checked-in `.mcp.json` |
  | Cursor | `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project) |
  | VS Code / Copilot | `.vscode/mcp.json` (uses a `servers` key) |
  | Windsurf / Zed / Cline / other | the same `mcpServers` block, that client's path |

  That exposes recall's 14 tools (`recall`, `brief`, `explain`, `resolve`, `stamp`,
  `contested`, `freshen`, `dashboard`, `impact`, `precedent`, `callers`, `dead_code`,
  `untested`, `cycles`) as native tools the AI can call.

- **CLI** — for spawned subagents, which **can't** see MCP (it's session-scoped, see
  below). Any agent in any framework reaches recall through the shell:
  `recall brief <file> --terse`. No integration needed — if the agent can run a
  command, it can use recall.

The point: recall isn't a plugin for one assistant. It's a memory layer under the whole
ecosystem — whatever AI (or mix of AIs) your team uses, they all read and write the
*same* project brain, so the engine gets stronger no matter who's driving.

## The gate: orient before you edit

Before editing any code file, run `recall brief <file>` first. recall and grep are
not alternatives — they run in order:

1. **Orient** — `recall brief <file>` (or `recall "<question>"`): what must I know
   before I touch this? Open tasks, the why, the blast radius. The step grep can't do.
2. **Locate** — `grep` / read: the exact string, the whole file. And before you grep
   a name you're *guessing*, run `recall resolve <guess>` ([Search-inversion](search-inversion)).
3. **Edit** — now, with both the why and the where in hand.

## The agent rule: CLI, not MCP

If you **spawn subagents** (a review fleet, an audit workflow), each one starts
blank — it doesn't inherit your session's memory or MCP connection. **MCP servers
are session-scoped, so a spawned subagent cannot see the recall MCP tools**
(measured). So an agent reaches recall through the **CLI via the shell**:

```sh
recall brief <file> --terse      # the machine-first pre-edit briefing
recall "<concept>" --terse       # locate by concept
recall resolve <guess> --terse   # correct a guessed term
```

**Every finder/reviewer/auditor agent you spawn must `recall brief <file> --terse`
before it judges that file.** A blank agent re-derives intent from code alone and
raises false alarms about decisions made on purpose. Measured A/B (2026-06-14): a
recall-less agent raised ~4–5 false alarms a recall-first agent didn't — while the
recall-first agent *also* found a real bug the other missed. recall-first is less
noise AND not blind.

MCP stays the path for the main interactive session and real users in Claude/Cursor.

## The 6 dimensions

After every feature and before every push, walk the canonical audit raster in
`docs/audit-dimensions.md`: **Auth-Guard · Audit-Log · Zod/Validation ·
Error-Handling (`res.ok`) · State-Updater · No-Client-Secrets/Cleanup**, plus the
standing security lenses (business-logic/race/money, injection/takeover). Run it
recall-first; verify findings adversarially (most raised findings are false alarms).

## The flywheel

Every commit makes the code smarter and every suggestion richer — passively. The
search-inversion synonym layer literally learns from what searches landed; the
trail/territory work (post-launch) deepens it. Old human code is dumb at first, so
the suggestions are dumb at first; they warm with use, and the curve is measurable.

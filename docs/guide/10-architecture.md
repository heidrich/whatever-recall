---
title: Architecture
slug: architecture
order: 10
summary: What's under the hood — the engine is open source, here's exactly how it works.
---

# Architecture

The engine is **open source** (the public repo is the tool itself). Nothing here is
a black box — this page is the honest map of how recall works under the hood, so you
can read the source and trust it.

## The shape of it

```text
   write-time (rare, AI-assisted)            read-time (constant, 0 model tokens)
   ─────────────────────────────            ────────────────────────────────────
   git commit ──► post-commit hook          recall brief / "<q>" / resolve / explain
                     │                                   │
                     ▼                                   ▼
              stamp(node + edges)              SQLite + FTS5 ranking  (sub-ms, no LLM)
                     │                                   │
                     ▼                                   ▼
              .mind/index.db  ◄──────────────────────────┘
              (the project's memory, git-tracked)
```

The whole bet: do the expensive thinking **once, at write-time** (while the context
is in the AI's head), and make read-time a dumb, fast database lookup. That's why
reading memory costs **0 model tokens** — there is no model in the read path.

## The pieces

- **The index — `.mind/index.db`.** A local SQLite database at the repo root,
  git-tracked (so the memory travels with the code) but `.vercelignore`/
  `.dockerignore`-d (so it never deploys). Tables: `nodes` (knowledge + code map),
  `edges` (typed relations), `anchors` + `node_anchors` (the FTS search surface),
  `access_log` (read-path activity → the search flywheel), `node_feedback`
  (useful/missed signal).
- **The code map — tree-sitter.** On `recall init`, a tree-sitter parse builds the
  `code-symbol` nodes (functions/classes/routes) and the `depends_on`/`co_changed`
  edges. Model-free, re-generated on demand — so it can never drift.
- **Ranking — SQLite FTS5 + BM25.** Queries rank over the anchors with BM25
  relevance; importance (a PageRank-style score over the dependency graph) is the
  tie-break, never the headline rank (ADR-028). The three/four tracks
  (code/knowledge/blast/tasks) are each ranked on their own axis so a loud one never
  buries another.
- **Governance — `rules.md`.** Thresholds and weights (silence floor, dedup, facet
  weights, edge vocabulary) are read from frontmatter, not hardcoded. See
  [Governance & drift](governance).
- **Surfaces.** The same model-free read path is exposed three ways: the **CLI**
  (`recall …`), the **MCP server** (`recall mcp` → native tools for Claude/Cursor),
  and the **dashboard** (`recall dashboard` → the browsable view). See
  [The dashboard](dashboard).

## Pure standard library, on purpose

The engine is **pure Python stdlib** — no heavy ML dependency, no vector database,
no external service. SQLite ships with Python; tree-sitter is the one parser. This
is a deliberate house rule: the read path must be installable anywhere, run offline,
and stay fast and auditable. (The web backend is a separate Next.js app; the
*engine* you install is just Python + SQLite.)

## Why "0 tokens" is real, not marketing

There is genuinely no LLM call when you read memory — it's FTS5 ranking over text
the AI already wrote at commit time. You can verify it: pull the network cable and
`recall brief` still works in milliseconds. That's the economic argument in one
line: orienting before an edit is **free**, so skipping it never saves tokens — it
only loses the why, the open tasks, and the blast radius.

## Read the source

It's open. Start with the load-bearing files (run `recall explain` in the repo):
`recall/engine.py` (the index + tracks), `recall/resolve.py` (search-inversion),
`recall/cli.py` (the commands), `recall/dashboard.py` (the server), `recall/rules.py`
(governance). The architecture decisions behind each are in `docs/decisions.md`.

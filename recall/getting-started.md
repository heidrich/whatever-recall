# Getting started with recall

recall is your project's memory: decisions, lessons, the code map, blast radius — **written at commit time, recalled offline in milliseconds, zero tokens.** Your code never leaves your machine; the index is one local SQLite file inside your repo.

This is the whole setup, top to bottom. Copy-paste ready.

## 1 · Install (2 minutes)

You need **Python 3.10+** and **git**.

```bash
git clone https://github.com/heidrich/whatever-recall.git
cd whatever-recall
pip install -e ".[codemap]"
```

`[codemap]` adds tree-sitter, so recall maps every function/class → file → line during indexing. (A plain `pip install whatever-recall` from PyPI ships with launch; until then `pip install git+https://github.com/heidrich/whatever-recall.git` works too.)

Check it worked:

```bash
recall -h
```

## 2 · Index your project

```bash
cd /path/to/your/project
recall init .
```

Token-free, offline, takes seconds to a few minutes depending on history size. Everything lands in `.mind/index.db` **inside your project** — deploy-safe, no cloud, delete it any time. Re-running `init` is safe (idempotent).

## 3 · See it — the dashboard

```bash
recall dashboard
```

Opens `http://127.0.0.1:7099` — the browsable wiki: knowledge, freshness traffic-light, tasks, the code map, git history. It watches your repo and re-indexes new commits by itself. The header pills show what's active: **rules.md · pre-commit · post-commit · mcp** — each hook pill is also the on/off switch.

Prefer one click over a terminal? `recall shortcut` puts a launcher on your Desktop (Windows `.bat` / macOS `.command` / Linux `.desktop`) that starts this dashboard for the project — no AI, no shell needed. `recall shortcut --remove` takes it away again.

## 4 · Plug it into your AI (MCP)

One line inside your project:

```bash
claude mcp add recall -- recall mcp
```

…or click the **mcp pill** in the dashboard header — it writes the same thing into a `.mcp.json` you can commit, so every teammate's AI gets recall offered automatically. (Cursor and most other clients use the same JSON shape; `recall mcp --print-config` prints all snippets.)

Your AI now has native tools — it calls them on its own when it needs memory:

| Tool | What it answers |
| --- | --- |
| `recall` | where is X · why is it like this · what breaks if I change it |
| `brief` | everything known about ONE file — *the call before every edit* |
| `explain` | repo orientation for a fresh session |
| `stamp` | write a decision/lesson into the memory |
| `contested` | where the team burns time (churn × entanglement) |
| `freshen` | re-check every note against the current git state |
| `dashboard` | start/find the local dashboard, returns the URL |

And you get **slash commands** (MCP prompts — type `/` in Claude Code):

```text
/mcp__recall__recall     query=why is the cache invalidated
/mcp__recall__brief      file=src/auth/session.ts
/mcp__recall__explain
/mcp__recall__dashboard
```

Each one runs offline against your local index and drops the answer straight into the conversation.

## 5 · The git hooks (the write-time loop)

Click the **pre-commit / post-commit pills** in the dashboard header, or:

```bash
recall hook --install               # post-commit: auto-stamps Recall-* trailers
recall hook --install --pre-commit  # pre-commit: warns before load-bearing changes (never blocks)
```

With the post-commit hook every commit feeds the memory by itself — working on the code IS maintaining the wiki.

## 6 · Daily use

In the terminal:

```bash
recall "why is there no default AI provider"     # ask anything — the 3 tracks answer
recall brief recall/engine.py                    # BEFORE you edit a file
recall stamp "RLS: writers must set workspace_id" --body "insert path forgot the scope column" --anchors rls,workspace_id
recall contested                                 # where time burns
recall explain                                   # new here? start with this
recall freshen                                   # re-check notes against git
```

In your AI chat: just work — the AI calls `brief` before edits and `recall` for questions on its own (the MCP server tells it to). Or use the slash commands above.

recall **stays silent when it doesn't know** — no guessing, ever. A silent answer means "nothing reliable stored", which you can trust.

## 7 · Turn things off / uninstall

Everything is reversible, nothing is hidden:

- **MCP:** click the mcp pill (or remove the `recall` entry from `.mcp.json`).
- **Hooks:** `recall hook --uninstall` (add `--pre-commit` for the warning hook).
- **The memory itself:** delete `.mind/` in your project — it is one local SQLite file, nothing else exists anywhere.
- **The package:** `pip uninstall whatever-recall`.

---

*The read path is pure stdlib — 0 tokens, 0 model, offline. Optional Power Mode (`recall connect` + `recall power`) lets YOUR AI enrich the index once; it never runs without an explicit cost estimate and `--yes`.*

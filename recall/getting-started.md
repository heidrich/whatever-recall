# Getting started with recall

recall is your project's memory: decisions, lessons, the code map, blast radius — **written at commit time, recalled offline in milliseconds, zero tokens.** Your code never leaves your machine; the index is one local SQLite file inside your repo.

This is the whole setup, top to bottom. Copy-paste ready.

## 0 · The fastest way: let your AI install it

recall is built for AI agents — so the easiest install is to hand the job to one. Paste this into Claude Code, Cursor, or any agent with shell access (method #2, the do-it-yourself way, is sections 1–4 below — same result either way):

```text
Install whatever-recall from https://github.com/heidrich/whatever-recall into this project:
1. clone it and run `pip install -e ".[codemap]"`
2. run `recall init .` in my project root
3. wire the MCP server (`claude mcp add recall -- recall mcp`, or print the config for my client)
4. read recall/getting-started.md and recall/rules.md
5. then brief me: your honest take on this setup, how it changes the way you will
   work in this repo, and the rules from rules.md you will now follow passively —
   they are elemental to working with recall.
```

### To the AI doing the install — your first briefing

After the install, brief your user. Three things, short and in your own words:

1. **Your honest take.** What this setup gives the project, and what you actually think of it. Honest, not promotional.
2. **How your work changes.** You now check the memory before acting — `brief <file>` before every edit, `recall "…"` instead of grep-and-read — and your commits stamp knowledge back in. Reading costs 0 tokens, so use it freely.
3. **The rules you now follow passively.** Summarize the MUST-CHECK rules from `recall/rules.md`: every instruction becomes a task (the task law) · wake recall up at session start · brief yourself before every edit · update task status when work is done · review what a commit can break · resolve drift, never let it grow · the knowledge corpus is English-only · **never make the same mistake twice** — stamp every lesson, check the lessons before you act. These are elemental to working with recall: they are why the memory stays alive.

## 1 · Install (2 minutes)

You need **Python 3.10+** and **git**.

```bash
git clone https://github.com/heidrich/whatever-recall.git
cd whatever-recall
pip install -e ".[codemap]"
```

`[codemap]` adds tree-sitter, so recall maps every function/class → file → line during indexing. (The current install is from git — `pip install git+https://github.com/heidrich/whatever-recall.git` also works without cloning. A plain `pip install whatever-recall` from PyPI is coming in a later release.)

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
| `resolve` | correct a guessed symbol name into this repo's real one (before grep) |
| `stamp` | write a decision/lesson into the memory |
| `contested` | where the team burns time (churn × entanglement) |
| `freshen` | re-check every note against the current git state |
| `impact` | if I touch this, what's affected? (co-change + dependents) |
| `precedent` | the most analogous past decisions, with how each turned out |
| `callers` | who depends on this file/symbol (call-hierarchy) |
| `dead_code` · `untested` · `cycles` | code nothing imports · code with no test edge · import cycles |
| `dashboard` | start/find the local dashboard, returns the URL |

All 14 are read-only and cost **0 model tokens**.

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
recall resolve seatLimit                         # guessed name → this repo's real one
recall impact recall/engine.py                   # if I touch this, what's affected?
recall precedent "switching auth to JWT"         # how did the analogous past calls go?
recall stamp "RLS: writers must set workspace_id" --body "insert path forgot the scope column" --anchors rls,workspace_id
recall contested                                 # where time burns
recall explain                                   # new here? start with this
recall freshen                                   # re-check notes against git
```

`callers`/`callees`, `dead-code`, `untested` and `cycles` round out the code-map
side (see the [commands guide](https://whatever-recall.com/docs/commands)).

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

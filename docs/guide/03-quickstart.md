---
title: Quickstart
slug: quickstart
order: 3
summary: Install, index a repo, and ask it a question — in a couple of minutes.
---

# Quickstart

## The fast way: hand it to your AI

Paste this to your coding AI (Claude Code, Cursor, …) — it clones, installs, and
indexes in one go:

```text
Install whatever-recall from https://github.com/heidrich/whatever-recall:
clone it, `pip install -e .`, then run `recall init .` in my repo and
`recall explain` to orient. From now on, run `recall brief <file>` before
editing any file.
```

That's it — your AI does the rest, and from the first commit your code starts
remembering its own reasons.

## The manual way

recall is a Python package — install it from the clone (or the wheel once published):

```sh
git clone https://github.com/heidrich/whatever-recall
cd whatever-recall
pip install -e .
# or, once published:
pip install whatever-recall
```

On Windows, the CLI expects UTF-8 output:

```sh
set PYTHONIOENCODING=utf-8           # cmd
$env:PYTHONIOENCODING = "utf-8"      # PowerShell
export PYTHONIOENCODING=utf-8        # bash
```

## Index your repo

From the repo root:

```sh
recall init .
```

This builds the `.mind/` index (a local SQLite database, git-ignored): the code
map (tree-sitter), the commit history, and any decisions/lessons it can read. It
costs **0 model tokens** — it's a parser and a database, not an LLM.

## Wake it up at session start

```sh
recall explain          # orientation: load-bearing files, decisions, what's in flight
recall dashboard        # opens the browsable brain + graph in your browser
recall hook --install   # the pre-commit risk-warning + post-commit auto-stamp
```

## The everyday loop

```sh
recall brief src/server/orgs.ts     # BEFORE you edit a file: why, what breaks, open tasks
recall "where is the seat ceiling enforced"   # ask by concept
recall resolve seatLimit            # correct a guessed name into this repo's real vocabulary
recall review                       # before committing: what this change can break
```

All of those are read-only and cost **0 model tokens**.

## Stamp what you learn

After a deliberate decision or a tricky fix:

```sh
recall stamp "we refuse a plan downgrade below occupied seats — it would strand members" \
  --anchors src/server/orgs.ts
```

It surfaces automatically in the next `brief` on that file — so the next session
(yours or a teammate's) inherits the reason.

## Next

- [Core commands](commands) — every command, what it returns, when to reach for it.
- [How it works](how-it-works) — write-time vs read-time, the 0-token read path.

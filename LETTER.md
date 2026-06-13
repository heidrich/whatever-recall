# I got sick of code being dumb in 2026. So I built whatever-recall.

*A letter from Christian — why this exists.*

Hey everyone,

I need to be honest with you: I am not a trained software engineer. I didn't study computer science. I am a project manager who taught myself how to code — or better, how to truly *understand* code.

My DNA was shaped during my days at Blizzard and Travian, where everything revolved around one single, obsessive goal: creating the perfect user experience. I don't care about engineering dogmas; I care about building products that simply feel incredible to use, eliminate friction, and solve real problems.

And right here, I ran into a massive, frustrating wall that I just couldn't ignore.

We are in 2026. We have AI technology that feels like pure science fiction. Yet the code we write is still as dead, flat, and dumb as it was thirty years ago. A source file contains nothing but raw syntax. It cannot tell another piece of code why it exists, what it intends, or how it connects causally to the rest of the software world.

So what happens? We unleash incredibly brilliant AI agents onto these blind, silent files. The AI stumbles, forgets architectural constraints, hallucinates, and burns through ~214,000 tokens for just three structural questions, because it has to brute-force grep-and-read its way through everything.

As a product person, this broke my heart. It's a terrible user experience for developers, and a financial disaster for companies. So I built **[whatever-recall](https://whatever-recall.com)**.

It is 2026, and it is finally time to change how code — and the information surrounding it — gets intelligent. It is time that causal relationships are anchored directly within the code itself. Because here is the truth we must accept: AIs read, measure, evaluate, and write code entirely differently than humans do. The way knowledge must be wired in the future is **machine-first** — and only then translated back into human language.

With recall, every new line of code is born smart — the causal chain is embedded the moment it is written. And every existing codebase grows smarter with every single commit your AI makes. Passively. Put 100 developers on a legacy project, and you have **100 streams of causal knowledge accruing in the same repository, every single day** — because every developer works on the exact same code, and **all of it lives inside the code itself**: dependencies, sprints, tasks, causal chains, rules. Everything in the code, no external tools. Every developer's AI client pulls the repo and instantly inherits the knowledge every other developer added passively via their commits.

Developers **and** project managers finally have a live, crystal-clear view of what actually happens when code is changed or removed. No more guessing. No more *"I need three days, four meetings, and four departments to investigate this."* On a live production repo, recall caught **75 of 75 real dependents** of a change — the naive baseline caught **zero**. Every connected AI client, on every branch, sees what was learned before, what to respect — and feeds its own learnings back in, just by working.

Just open the damn dashboard and watch your codebase get smarter with every commit.

## 💥 The business bangers (what CEOs & CFOs need to know)

If you are running a software team in 2026, whatever-recall changes your balance sheet by anchoring causal intelligence:

- **The financial implosion of AI costs.** We measured a **~1,400× cut** in context-retrieval token waste. Stop paying LLM vendors to re-read your own repository thousands of times a day just to reconstruct causal links.
- **The causal safety net — no accidental rollbacks.** The biggest threat to modern codebases is an AI agent refactoring a file and blindly destroying a hard-fought architectural decision because it "didn't see the context." recall briefs the agent *before* every edit — no more undoing a deliberate decision it has never seen.
- **Instant developer onboarding.** When a new dev (or a new AI agent) joins your team, they don't spend weeks guessing dependencies. They run one command and inherit the project's entire causal memory.
- **IP protection & compliance.** Zero cloud lock-in. Your entire causal knowledge graph lives inside your git repository. It stays local, secure, and yours.

## 🛠️ The tech stack (what your CTO will audit)

For the technical leaders who want to verify the ground truth — this is a zero-dependency infrastructure layer built for causal awareness:

- **Write-time causal ingestion.** Instead of expensive read-time guessing, recall captures intent while the AI is coding and already holds the context — and stamps those causal relationships directly onto the git commit SHA.
- **A millisecond, offline reader.** The read path is completely model-free. It tokenizes queries into structural anchors and queries a local SQLite FTS5 (BM25) index. A full three-track answer takes **~2.18 ms median** — ~67× faster than grep-and-read — and a raw lookup stays at **0.25 ms median** even at 108,627 anchors.
- **A pure-stdlib MCP server.** recall ships a native Model Context Protocol server built entirely on the Python standard library. No dependency hell. Check `.mcp.json` into git, and your whole team's Claude/Cursor clients are wired to the same causal engine.
- **Anti-rot architecture.** Notes are tied to git SHAs. If code moves or changes, recall detects the structural drift instantly (🟢 fresh · 🟡 moved · 🟠 gone) and offers one-command healing — you approve, it re-stamps.

## 📊 The measured proof

Cold-start on live production repos (the largest: 108,627 anchors), three real architectural questions:

| Metric | grep-and-read | whatever-recall |
|---|---|---|
| tokens for the same 3 answers | ~214,000 | **152** |
| time to the answer | ~147 ms | **~2.18 ms** |
| read-time model calls | every prompt | **0 — fully offline** |

The code is public. It is free for personal use, education and research, and source-available for everyone under the **Business Source License 1.1**; commercial production use takes a [subscription](COMMERCIAL.md). And every released version becomes true open source (**Apache 2.0**) three years after its release — your stack can never end up in a proprietary dead end. You can run the verification benchmarks yourself: `python experiments/bench_v2.py`.

Let's stop feeding brilliant AIs dumb text files. Let's make code causally clever.

I honestly don't know if this will solve all your problems — or whether it will even be helpful for you at all. I don't think like a developer. I am a project manager who simply works differently, and who has different expectations of what "today" should feel like in 2026.

I would love to hear your honest thoughts on the machine-first future of software development.

— Christian, co-founder, McCain Digital

[whatever-recall.com](https://whatever-recall.com) · a product & service of [McCain Digital](https://mccain-digital.com)

P.S. — I know the question is coming: *why not fully open source?* Simple. A nasty infection and the long illness that followed cost me my company, my employees, my house, my car — everything. Right now I'm scraping by on €2,400 a month and a 7-to-5 day job, getting back on my feet. I finally want to do what I love again: **build products.** And that only works if I am financially independent and can devote all of my time to this one thing.

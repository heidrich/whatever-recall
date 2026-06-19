# I got sick of code being dumb in 2026. So I built whatever-recall.

*A letter from Christian — why this exists.*

Hey everyone,

I need to be honest with you: I am not a trained software engineer. I didn't study computer science. I am a project manager who taught myself how to code — or better, how to *understand* code.

My DNA was shaped during my days at Blizzard and Travian, where everything revolved around one single, obsessive goal: creating the perfect user experience. I don't care about engineering dogmas; I care about building products that simply feel incredible to use, eliminate friction, and solve real problems.

And right here, I ran into a massive, frustrating wall that I just couldn't ignore.

We are in 2026. We have AI technology that feels like pure science fiction. Yet the code we write is still as dead, flat, and dumb as it was thirty years ago. A source file contains nothing but raw syntax. It cannot tell another piece of code why it exists, what it intends, or how it connects causally to the rest of the software world.

So what happens? We unleash incredibly brilliant AI agents onto these blind, silent files. The AI stumbles, forgets architectural constraints, hallucinates, and burns through ~214,000 tokens for just three structural questions, because it has to brute-force grep-and-read its way through everything. When you use AI for coding, it feels like you're talking to Mr. Spock or Data from Star Trek: they know everything, can do anything, and never lie. Well — after some very long, often frustrating, intense months of hardcore digging into AI, it turns out they're more like Sheldon Cooper.

As a product person, this broke my heart. It's a terrible user experience for developers, and a financial disaster for companies. So I built **[whatever-recall](https://whatever-recall.com)**.

It is 2026, and it is finally time to change how code — and the information surrounding it — gets intelligent. It is time that causal relationships are anchored directly within the code itself. Because here is the truth we must accept: AIs read, measure, evaluate, and write code entirely differently than humans do. The way knowledge must be wired in the future is **machine-first** — and only then translated back into human language.

And I want to be blunt about *why* that order is the whole game, because everyone else gets it backwards. If you build for the human first and then ask the AI to live in it, the AI has to *guess* its way back to the machine truth — and guessing is exactly where the hallucinations come from. But if the facts are captured machine-first, the moment the code is written, then translating them back into clean human language is the *easy* half. **You can always translate AI-truth down into human words once the facts exist. You can almost never translate human words up into machine-truth without the AI inventing the gap.** That is not a stylistic choice. That is the difference between a tool that hallucinates and a tool that doesn't.

With recall, every new line of code is born smart — the causal chain is embedded the moment it is written. And every existing codebase grows smarter with every single commit your AI makes. Passively. Put 100 developers on a legacy project, and you have **100 streams of causal knowledge accruing in the same repository, every single day** — because every developer works on the exact same code, and **all of it lives inside the code itself**: dependencies, sprints, tasks, causal chains, rules. Everything in the code, no external tools. Every developer's AI client pulls the repo and instantly inherits the knowledge every other developer added passively via their commits.

Developers **and** project managers finally have a live, crystal-clear view of what actually happens when code is changed or removed. No more guessing. No more *"I need three days, four meetings, and four departments to investigate this."* On recall's own repo, across the ten hottest files, the blast-radius track surfaced **98 of 114 real dependents** an edit would touch — the naive baseline (the file in front of you, nothing else) catches **zero**. Every connected AI client, on every branch, sees what was learned before, what to respect — and feeds its own learnings back in, just by working.

## 🔁 The part nobody else is building: we don't make search better. We turn it around.

Here is where this whole way of seeing it comes from, and I think it's the real reason AI-for-code still feels like it's missing the point. Back in the day, I was a Senior Support Agent at Blizzard, and I trained the people who did it.

If there is one thing you learn in high-stakes customer support, it's this: the entire game is won or lost in *how you frame the question.* You don't just ask a user "What is your problem?" — that opens an infinite field of blind guessing. The real trick is to structure the question so precisely that the person answering can only move in the right direction.

Picture a player stuck on a quest. The amateur asks, "What's wrong?" The Game Master answers: *"Ah, I see you're stuck on this quest. The way I see it, you've either talked to the wrong NPC, or the quest isn't actually complete yet — did you check your quest log?"* Why two answers and not twenty? Because the GM knows the system inside out. The player's problem is real, but I — the GM — know it can only be one of two things. So I hand him the only two answers that can possibly be right, and the guessing is over before it began.

That is the whole move. You eliminate the uncertainty *in the question itself*, long before the answer is even formed. The support professional knows exactly what the system can do and where its hidden bugs sit — and they use that knowledge to pre-structure the user's query.

Flip the roles, apply it to AI and coding, and the mechanics map perfectly:

| The Blizzard support legacy | The whatever-recall paradigm |
|---|---|
| **The user** — has a problem, doesn't know how to ask correctly. | **The AI agent** — wants to build or fix code, hallucinates the search term. |
| **The support agent** — knows the system, its limits, and its past bugs. | **The code (recall)** — knows its own vocabulary, its contested spots, and what broke here before. |
| **The smart question** — narrows down the guessing *before* the user answers. | **Intent-autocomplete** — focuses the search intent *before* the agent greps. |

This is why my brain has worked completely differently ever since — even as a product or project manager. Everyone else is trying to build a better *responder* — smarter search hits, bigger RAG contexts. I am building a better *questioner*: the support professional who structures the query so that guessing never happens in the first place. **The codebase becomes the Senior Support Agent for your AI.** It helps the AI focus and refine its own question, because the code passively knows its own system and its own past failures.

And this is exactly why embeddings or brute-force grep can never do this. A support agent who doesn't know the deep, historical truth of the system cannot guide the user. Embeddings only guess at similarity. grep only finds static strings. Only recall has captured the real, lived truth of your repo *at write-time*. The code must use its own passive self-knowledge to focus the AI. That is the Blizzard methodology, one to one.

So here is the whole industry's blind spot, said plainly. Everyone — Copilot, Cursor, every code-search tool — is optimizing the *same* moment: you search, and they try to hand you a better result. Smarter answers to the question you typed. That's the *wrong half* of the problem. **The hardest, most expensive, most error-prone step in coding was never the searching. It's knowing what to search for in the first place** — exactly the step a good support agent solves *for* you.

It turns out this isn't just my gut feeling — it has a name. Computer scientists have called it the **vocabulary mismatch** problem for thirty years: the searcher uses different words than the ones actually written in the thing they're looking for. The research is brutally clear about it — an average search term is missing from **30–40% of the documents that are genuinely relevant.** And the punchline from the literature is exactly the wall I kept hitting: *"a user should not be expected to know the exact content of what they hope to retrieve."* Right. So why do we build every tool as if they do? And when the "user" is an AI that *invents* the search term out of its training, the mismatch isn't 30% — it's a coin flip every single time.

Watch what an AI agent actually does. Before it can grep, it has to *invent* a search term out of its training — `enforceSeats`? `seatLimit`? `checkSeats`? Three guesses, and in *your* repo maybe one of them is even real. So it greps, finds nothing or the wrong thing, guesses again, burns tokens, and the whole time its training is actively *misleading* it — because it knows the *general* world, not *your* repository. That guessing loop is the tax we all pay, every single prompt.

recall knows your repository. It knows it's called `enforceSeats` here, that it lives in one specific file, that it's a fragile spot where a bug was already fixed once. So the leap is simple and, once you see it, obvious: **recall should hand the AI the right search term before it ever guesses wrong.** Autocomplete for the *intent* of the search — like writing CSS and the editor proposes the values that are actually valid, except here the "valid values" are the real, lived truth of your codebase.

This flips a thirty-year-old assumption that nobody questions. grep isn't dumb because of its mechanics; it's dumb because of its *premise* — that the searcher already knows what they're looking for and types it correctly. For an AI that hallucinates the term from probabilities, that premise is doubly false. recall breaks it: **the search intent is no longer formed alone in the head of whoever's searching — it's co-formed by the system, out of what it actually knows about this one repo.** You start to ask; the tool completes your question with the truth of the code; *then* it answers.

And only recall can do this. Not because we're cleverer — because recall is the only tool that captures the real vocabulary and the real, hard-won experience of *your* repo at **write time**. Embeddings can guess what *sounds* similar. grep finds strings. Only recall knows what genuinely exists here, and why it's dangerous to touch. That's the moment recall stops being a memory sitting *next to* your code and becomes the way you *enter* it.

Just open the damn dashboard and watch your codebase get smarter with every commit. If you'd rather read first, the whole thing — every command, how the edges work, how private knowledge stays local — is in the **[docs](https://whatever-recall.com/docs)**.

To wrap it all up and put it simply: with recall, your code grows smarter with every single commit, because the causal chain is documented the exact millisecond it is born. And as a direct result, every search intent the system proposes becomes richer, more targeted, and more precise with every iteration. It is a passive, self-optimizing loop — a flywheel of structural intelligence that turns your repository into a living, learning asset.

## 💥 The business bangers (what CEOs & CFOs need to know)

If you are running a software team in 2026, whatever-recall changes your balance sheet by anchoring causal intelligence:

- **The financial implosion of AI costs.** We measured a **~1,400× cut** in context-retrieval token waste. Stop paying LLM vendors to re-read your own repository thousands of times a day just to reconstruct causal links.
- **The causal safety net — no accidental rollbacks.** The biggest threat to modern codebases is an AI agent refactoring a file and blindly destroying a hard-fought architectural decision because it "didn't see the context." recall briefs the agent *before* every edit — no more undoing a deliberate decision it has never seen.
- **The end of the guessing tax.** Your AI stops burning tokens inventing search terms that don't exist in your repo. It asks recall what's really there, and goes straight to the right place — the single most repeated waste in every AI coding session, gone.
- **Instant developer onboarding.** When a new dev (or a new AI agent) joins your team, they don't spend weeks guessing dependencies. They run one command and inherit the project's entire causal memory.
- **IP protection & compliance.** Zero cloud lock-in. Your causal knowledge graph lives in a local brain, never in your source — so your code ships clean by construction, with nothing to scrub before you publish. Every note is **team** or **private**; `recall export` shares a brain with all private notes stripped, and two fail-closed guards make it physical — the export aborts rather than leak, and a commit guard blocks a private brain from ever entering git. **Your decisions never leave your machine unless you choose to share them.**

## 🛠️ The tech stack (what your CTO will audit)

For the technical leaders who want to verify the ground truth — this is a zero-dependency infrastructure layer built for causal awareness:

- **Write-time causal ingestion.** Instead of expensive read-time guessing, recall captures intent while the AI is coding and already holds the context — and stamps those causal relationships directly onto the git commit SHA.
- **A millisecond, offline reader.** The read path is completely model-free. It tokenizes queries into structural anchors and queries a local SQLite FTS5 (BM25) index. A full three-track answer takes **~2.18 ms median** — ~67× faster than grep-and-read — and a raw lookup stays at **0.25 ms median** even at 108,627 anchors.
- **0-token code intelligence, offline.** The same typed edge graph powers a full set of code-intel reads with no model and no tokens: `callers` / `callees` (the call hierarchy, forward and reverse), `impact` (the blast radius — empirical co-change *and* structural dependents fused), `precedent` (the most analogous past decisions, and how each turned out), plus `dead-code`, `untested` and `cycles` candidates. It's the everyday "what depends on this, what breaks, what's been decided here" — answered from the local index, instantly, for free.
- **A pure-stdlib MCP server.** recall ships a native Model Context Protocol server built entirely on the Python standard library — 14 tools, no dependency hell. Check `.mcp.json` into git, and your whole team's Claude/Cursor clients are wired to the same causal engine.
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

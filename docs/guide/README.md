# docs/guide — the SSOT documentation

These files are the **single source of truth** for the whatever-recall product
documentation. They live in the code (so they can't drift from it) and the public
**Help / Docs** page on the website renders them directly at build time — the site
shows exactly these Markdown files, nothing forked, nothing copied.

## How it's wired

- Each `NN-slug.md` has YAML frontmatter: `title`, `slug`, `order`, `summary`.
- The Next.js `/docs` route reads this directory at build time, builds the
  left sidebar from `order` + `title`, and renders the body. One source → both
  the repo docs and the website.
- Cross-links use the bare slug, e.g. `[Search-inversion](search-inversion)`.

## Editing rules (docs-in-code)

- **This is the SSOT.** Don't duplicate product docs elsewhere; link here.
- Keep claims **measured** — numbers come from `experiments/` + `docs/benchmarks.md`.
- Architecture decisions live in `docs/decisions.md` (ADR log); the guide links to
  them, it doesn't restate them.
- A change to a command/behavior updates the matching guide section in the SAME
  change — drift between code and this guide is a bug, not a doc debt.

## Sections

1. [Introduction](01-introduction.md)
2. [Why it all connects](02-why-it-connects.md) — the causal chain
3. [Quickstart](03-quickstart.md)
4. [Core commands](04-commands.md)
5. [How it works](05-how-it-works.md)
6. [Stamps & edges](06-stamps-and-edges.md)
7. [The 6 dimensions](07-six-dimensions.md)
8. [Search-inversion](08-search-inversion.md)
9. [Working with AI agents](09-agents.md)
10. [Architecture](10-architecture.md)
11. [Self-healing](11-self-healing.md)
12. [Governance & drift](12-governance.md)
13. [v1.2 — The evolution](13-evolution.md) — the next milestone
14. [The dashboard](14-dashboard.md)

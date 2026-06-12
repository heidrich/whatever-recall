"""Anchor extraction + tag canonicalization.

Anchors are the technical terms a query/lesson is *about*. We extract generously
(the silence floor + multi-hit logic sieve the noise later) but canonicalize tags
against a closed vocabulary, the ADR-005 anti-sludge guard: one concept, one term.

Pure stdlib. The STOP set and extraction rules are lifted verbatim from the proven
real_repo_test.py so the on-disk engine matches what we measured.
"""

from __future__ import annotations

import re

# Stopwords (DE + EN) + git/process noise that would otherwise become false anchors.
STOP: set[str] = set(
    """
    der die das und oder ist im in den dem ein eine auf mit fuer von zu the a an of to
    in on for and or is with neue was wie ich soll nicht mehr aus als am bei nach vor wird
    wurde this that these those it its as at by be are were has have had not no yes can will
    would should owner claude code gates tests build test lint tsc commit feat fix docs
    refactor chore merge branch
    """.split()
)


def extract_anchors(text: str) -> set[str]:
    """Extract anchors from free text.

    Three passes: technical symbols (snake/kebab/dotted), domain IDs (ADR-50,
    migration #34, alpha.219), and plain words >= 4 chars that aren't stopwords.
    """
    text = text.lower()
    toks: set[str] = set()
    # technical symbols: foo_bar, foo-bar, foo.bar, .ed-root, --ed-token, wf_abc, z-260
    for m in re.findall(r"[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)+", text):
        toks.add(m)
    # domain IDs: adr-50, #38, alpha.219, migration #34, phase 2
    for m in re.findall(r"(?:adr|alpha|migration|chunk|phase|wave)[-\s#]*\d+", text):
        toks.add(m.replace(" ", "").replace("#", ""))
    # plain words >= 4 chars
    for m in re.findall(r"[a-z][a-z0-9]{3,}", text):
        if m not in STOP:
            toks.add(m)
    return toks


# Query-ONLY stopwords (BM25 wave). Interrogative/filler words that pass the extractor's
# 4-char gate and used to count as full hits ("what is still open" scored 6). Evidence-
# based: every word here has zero load-bearing compounds in the corpus; words like
# `open`, `done`, `task`, `wiki`, `only` are deliberately NOT stopped (status facets /
# domain terms — IDF handles their commonness). This set must NEVER be merged into STOP:
# STOP also governs stamping, and dropping these from stored prose would change the
# index. Query-side only = zero migration, zero write-path change.
#
# DECIDED (review follow-up, 2026-06-11): the write/read asymmetry STAYS. Stamped
# QUERY_STOP anchors (~0.6% of anchor rows measured) are query-unreachable, but
# filtering them at write time would break the documented invariant above for a
# negligible IDF/size win, and a retrieval fallback for all-stopword queries would
# break the anti-Clippy silence ("what is still" MUST stay silent, not guess).
# Projects can ADD stopwords via rules.md `query_stopwords:` (additive only).
QUERY_STOP: frozenset[str] = frozenset(
    """
    what where when which whose does been being have having still then than
    them they there here about into some just very much many such also
    warum wieso weshalb wann welche welcher welches macht machen gemacht
    haben hatte kann koennen muss muessen sollte sollten immer noch schon
    dann denn auch gibt geht alle jetzt heute
    """.split()
)


def tokenize_query(query: str) -> list[str]:
    """Anchors for a query — same extractor minus query-only stopwords (order irrelevant).
    An all-stopword query yields [] and recall() answers with its 'no tokens' silence."""
    return [t for t in extract_anchors(query) if t not in QUERY_STOP]


# A model-provided anchor longer than this many words is treated as a phrase/sentence
# (or a pasted code line) rather than a search term, and is exploded into its tokens.
_ANCHOR_PHRASE_WORDS = 2


def clean_anchor_terms(raw_anchors) -> set[str]:
    """Turn model-provided anchors into SEARCHABLE terms — symmetric with how queries
    are tokenized (extract_anchors). Without this, Power Mode stored whole phrases like
    "heavy modals are rendered lazily" or code lines like "const x = dynamic(" as ONE
    FTS term; a user typing "lazy modal" never matched them (the v4 stemmer helps with
    morphology, but a 6-word term is still the wrong unit). Strategy per anchor:

      - a short term (<= 2 words) that already looks like a search token is KEPT as-is
        AND, if it carries internal structure (spaces / code punctuation), ALSO exploded
        so both the literal and its parts are findable (e.g. "next/dynamic" -> the term
        plus "next", "dynamic");
      - a longer anchor (a phrase or a pasted code line) is EXPLODED via the same
        extractor queries use, so its meaningful tokens become individually searchable.

    Returns a deduped lowercase set. Empty input -> empty set (caller then falls back to
    extract_anchors over title+body, as before)."""
    out: set[str] = set()
    for raw in raw_anchors or []:
        term = str(raw).strip().lower()
        if not term:
            continue
        words = term.split()
        if len(words) <= _ANCHOR_PHRASE_WORDS:
            # keep the clean short term itself (unless it's a lone stopword)…
            if not (len(words) == 1 and term in STOP):
                out.add(term)
            # …and also surface its structural tokens so it matches plain queries.
            out.update(extract_anchors(term))
        else:
            # a phrase / code line — keep only its searchable tokens, not the whole blob.
            out.update(extract_anchors(term))
    return {t for t in out if t}


# --- ADR-005: closed vocabulary. Tags are canonicalized; free anchors are not,
# because anchors carry the long tail of real symbols. Tags are the coarse facets
# that rules.md weights, so they MUST be a small fixed set.

# Default alias map. Projects extend it via rules.md `tag_aliases`. Keys are
# variants, values are the canonical tag.
DEFAULT_TAG_ALIASES: dict[str, str] = {
    "sec": "security", "auth": "security", "rls": "security", "xss": "security",
    "perf": "performance", "speed": "performance", "latency": "performance",
    "a11y": "accessibility", "i18n": "localization", "l10n": "localization",
    "db": "backend", "sql": "backend", "migration": "backend", "api": "backend",
    "css": "ui", "style": "ui", "layout": "ui", "design": "ui", "component": "ui",
    "bug": "bugfix", "hotfix": "bugfix", "patch": "bugfix",
    "feat": "feature", "feature": "feature",
    "doc": "docs", "documentation": "docs",
    # task/plan lifecycle (ADR-017): the user's intent + our plans become first-class,
    # wired wiki nodes — not lost memory. Synonyms fold to the canonical tag.
    "plans": "plan", "spec": "plan", "design-doc": "plan",
    "roadmaps": "roadmap", "milestone": "roadmap",
    "sprints": "sprint", "iteration": "sprint",
    "tasks": "task", "todo": "task", "todos": "task", "ticket": "task",
}

# The canonical tag set rules.md may weight. Anything not here (after aliasing)
# is dropped from facets — it lives on as a free anchor instead.
DEFAULT_ALLOWED_TAGS: set[str] = {
    "security", "performance", "accessibility", "localization",
    "backend", "frontend", "ui", "bugfix", "feature", "docs",
    "logic", "math", "infra", "test", "new-code", "foundation",
    "chore",  # housekeeping (lockfiles, ignore rules): real, but quiet by weight
    # task/plan lifecycle (ADR-017) — forward-looking knowledge, weighted LOUD so an
    # open plan/task surfaces over routine code when it's relevant.
    "plan", "roadmap", "sprint", "task",
    # task STATUS as facets so the lifecycle (open/done/dropped/deferred) survives on the
    # node and the dashboard/drift can read it back. Neutral weight (1.0 via default).
    "open", "done", "dropped", "deferred",
}


def canonicalize_tags(
    raw_tags,
    aliases: dict[str, str] | None = None,
    allowed: set[str] | None = None,
) -> list[str]:
    """Map raw tags to the canonical vocabulary, drop unknowns, dedupe.

    >>> canonicalize_tags(["sec", "Auth", "css", "wat"])
    ['security', 'ui']
    """
    aliases = aliases if aliases is not None else DEFAULT_TAG_ALIASES
    allowed = allowed if allowed is not None else DEFAULT_ALLOWED_TAGS
    out: list[str] = []
    seen: set[str] = set()
    for t in raw_tags:
        t = t.strip().lower()
        if not t:
            continue
        canon = aliases.get(t, t)
        if canon in allowed and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out

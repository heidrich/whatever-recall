"""Deterministic prompt construction + parse for Power Mode (STEP 4).

Zero network, fully unit-testable. This module never calls a model — it only
builds the text we WILL send (power.py sends it via the llm.py seam) and parses
what comes back. Two halves:

  - build_understanding_prompt: the system prompt HARDCODES the closed-vocabulary
    contract (ADR-005). The model is told it MAY only use tags from rules.allowed_tags
    and edge kinds from rules.edge_kinds — and that we re-validate anyway.
  - parse_stamp_instructions: parses the model's JSON and RE-VALIDATES every tag via
    the existing canonicalize_tags() + every edge kind against rules.edge_kinds. A
    hallucinated tag is dropped, a hallucinated edge kind is dropped — never trusted.
    Governance lives in the engine; the model is advisory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from recall.anchors import canonicalize_tags, clean_anchor_terms
from recall.rules import Rules


@dataclass
class StampInstruction:
    """One re-validated node the model proposed. Safe to hand to index.stamp()."""

    title: str
    body: str = ""
    anchors: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)  # already canonical (unknowns dropped)
    edges: list[tuple[str, str]] = field(default_factory=list)  # (kind, target), kinds valid
    # provenance of what the model proposed but we rejected — surfaced in --dry-run so
    # the owner sees the model tried to hallucinate vocabulary (transparency).
    dropped_tags: list[str] = field(default_factory=list)
    dropped_edges: list[tuple[str, str]] = field(default_factory=list)


# the ONLY off-schema top-level keys we tolerate (a near-miss the model sometimes makes
# instead of "nodes"). Anything else is a failed reply — recorded, never silently used.
# NB: deliberately NOT "facts" — in the dogfood bug {file, role, facts:[...]} that was a
# list of sentence STRINGS, not node objects; treating it as a node array would misparse
# the very payload we want to reject. Container-shaped synonyms only.
_ALT_NODE_KEYS = ("nodes", "lessons", "results", "items")
# per-node field aliases — same idea, bounded, so {{file, role}} still lands as title/body.
_TITLE_ALIASES = ("title", "name", "summary", "headline", "label")
_BODY_ALIASES = ("why", "body", "role", "description", "detail", "fact", "text")


@dataclass
class ParseReport:
    """What parse_with_report produced — including WHY a reply yielded nothing.

    The dogfood bug was silent: 45/50 replies parsed as JSON but had no 'nodes' key, so
    each quietly stamped 0 and nobody noticed until the totals looked wrong. This report
    makes the failure mode loud — run_power counts `yielded_nothing` replies and surfaces
    them, so a schema mismatch can never hide as 'just a quiet run' again.
    """

    instructions: list[StampInstruction] = field(default_factory=list)
    # one of: "" (ok, nodes produced), "not-json" (nothing parsed), "empty" (valid
    # {"nodes": []} — the model legitimately found nothing), "no-node-key" (JSON parsed
    # but no recognized node array — the schema-mismatch bug), "wrong-shape" (node array
    # found but every entry was unusable).
    reason: str = ""
    # the off-schema top-level key we had to fall back to, if any (for transparency).
    used_alt_key: str = ""

    @property
    def yielded_nothing(self) -> bool:
        """True when the reply produced NO usable nodes AND that wasn't legitimate silence.
        i.e. a real parse failure / schema mismatch, not an honest {"nodes": []}."""
        return not self.instructions and self.reason not in ("", "empty")


# ----------------------------------------------------------------- prompt build
# The schema is HARDCODED here AND re-enforced by the parser. The dogfood bug was a
# model that answered valid JSON but with the WRONG keys ({{file, role, facts}}) — the
# parser found no "nodes" key and silently stamped 0. So the prompt now (a) shows one
# fully-worked example, (b) lists the exact wrong shapes as anti-examples, and (c) states
# that the ONLY top-level key is "nodes". The parser still normalizes a few near-misses
# and NEVER discards silently — but the prompt's job is to make near-misses rare.
_SYSTEM_TEMPLATE = """\
You are a JSON API that stamps code memory. You read ONE source file plus what is \
already known about it, and you emit knowledge worth remembering write-time.

CRITICAL OUTPUT CONTRACT — obey exactly or the system fails:
  - Output a SINGLE JSON object and ABSOLUTELY NOTHING ELSE.
  - NO markdown, NO headings, NO prose, NO code fences (```), NO commentary before or
    after the JSON. The first character of your reply MUST be {{ and the last }}.
  - The ONLY top-level key is "nodes" (an array). Do NOT invent other top-level keys.

The object is shaped EXACTLY like this — this is the ONLY accepted schema:
{{"nodes": [
  {{"title": "short human name (NOT the file path)",
    "why": "1-3 plain-language sentences: the thing a newcomer would ask",
    "anchors": ["short", "lowercase", "search terms", "1-2 words each", "what a teammate would TYPE"],
    "tags": ["pick from the allowed set below"],
    "edges": [{{"kind": "pick from the allowed set", "target": "a symbol or file"}}]}}
]}}

ANCHORS are search keywords, NOT sentences and NOT code. Each anchor is 1-2 lowercase
words a teammate would type into a search box (e.g. "lazy load", "modal", "rate limit",
"ssrf", "audit log"). Do NOT put full sentences, file paths, or code lines like
"const x = dynamic(" in anchors — those are useless for search and get split apart.

A fully-worked example of a CORRECT reply (copy this shape exactly):
{{"nodes": [
  {{"title": "login validates session before redirect",
    "why": "The handler refuses to redirect until the session token is verified, so a stale tab cannot bounce a logged-out user into the app.",
    "anchors": ["login", "session", "redirect guard", "auth check"],
    "tags": ["security", "backend"],
    "edges": [{{"kind": "implements", "target": "auth.py"}}]}}
]}}

WRONG — these will be treated as a FAILED reply. Never use them:
  - {{"file": "...", "role": "...", "facts": [...]}}   ← no "nodes" key
  - {{"lessons": [...]}} or {{"results": [...]}} or {{"items": [...]}}   ← wrong top-level key
  - a bare array [ {{...}} ] with no surrounding object
  - per-node keys other than title / why / anchors / tags / edges
The top-level key is "nodes". The per-node keys are title, why, anchors, tags, edges. Nothing else.

Closed vocabulary — anything outside it is discarded by the system, so don't bother:
  - tags: ONLY from: {allowed_tags}
  - edge kinds: ONLY from: {edge_kinds}

If the file holds nothing worth remembering, output exactly {{"nodes": []}}.
Remember: your ENTIRE reply is parsed as JSON. One stray word breaks it."""


def build_understanding_prompt(
    *,
    file_path: str,
    source: str,
    existing: list[dict[str, Any]] | None,
    rules: Rules,
) -> tuple[str, str]:
    """Build (system, user). Deterministic — same inputs, same bytes (sorted vocab)."""
    allowed = ", ".join(sorted(rules.allowed_tags)) or "(none)"
    edge_kinds = ", ".join(sorted(rules.edge_kinds)) or "(none)"
    system = _SYSTEM_TEMPLATE.format(allowed_tags=allowed, edge_kinds=edge_kinds)

    known = ""
    if existing:
        lines = [
            f"- {e.get('title', '?')}: {e.get('why') or e.get('body') or ''}".strip()
            for e in existing
        ]
        known = "Already known about this file (enrich, don't duplicate):\n" + "\n".join(lines)

    user = (
        f"FILE: {file_path}\n"
        f"{known}\n\n" if known else f"FILE: {file_path}\n\n"
    ) + f"SOURCE:\n{source}" + _user_contract_tail(allowed)
    return system, user


# Agent CLIs (notably Claude Code via `claude --print`) treat the file + "emit knowledge"
# as a coding task and answer in their OWN code-description shape ({file, purpose,
# public_api, ...}) — overriding even a strict --system-prompt. The dogfood bug and the
# 2026-06-07 re-probe both showed this. The reliable fix is RECENCY: repeat the exact
# output contract at the very END of the user message, after the source. In the live
# probe this flipped claude-cli from {file, purpose, ...} (0 nodes) to a clean
# {"nodes": [...]} with 4 real nodes. Keep this tail in sync with _SYSTEM_TEMPLATE.
def _user_contract_tail(allowed_tags: str) -> str:
    return (
        "\n\n---\n"
        "NOW RESPOND. Output ONLY a single JSON object and NOTHING else — no prose, no "
        "code fence. The top-level key MUST be \"nodes\" (an array). Do NOT answer with "
        "keys like file / purpose / public_api / one_liner / role / facts — that is a "
        "FAILED reply.\n"
        "Fill exactly this shape:\n"
        '{"nodes": [{"title": "...", "why": "...", "anchors": ["..."], '
        '"tags": ["from: ' + allowed_tags + '"], "edges": []}]}\n'
        "If nothing is worth remembering, output exactly {\"nodes\": []}."
    )


# ----------------------------------------------------------------- parse + revalidate
def _extract_json_object(text: str) -> dict | None:
    """Pull the JSON object out of an LLM reply that may be wrapped in prose or a
    ```json fence — the common shape from agent CLIs (claude --print etc.), which add
    a sentence or a code fence even when asked for strict JSON. Strategy, in order:
      1) the whole reply parses as JSON (the ideal, strict-JSON case);
      2) the content of the FIRST ```...``` fenced block parses;
      3) the first balanced {...} span parses.
    Returns the dict, or None if nothing parses. Pure, offline, never raises."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    # 1) pure JSON
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    # 2) a fenced code block ```json ... ``` (or any ``` ... ```)
    fence_start = s.find("```")
    if fence_start != -1:
        rest = s[fence_start + 3:]
        # drop an optional language tag on the same line (json, JSON, …)
        nl = rest.find("\n")
        if nl != -1 and rest[:nl].strip().isalpha():
            rest = rest[nl + 1:]
        fence_end = rest.find("```")
        block = rest[:fence_end] if fence_end != -1 else rest
        try:
            obj = json.loads(block.strip())
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            pass
    # 3) first balanced {...} span
    start = s.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(s)):
            c = s[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except (json.JSONDecodeError, TypeError):
                        break
    return None


def _extract_json_array(text: str) -> list | None:
    """Pull a top-level JSON ARRAY out of a reply (the model dropped the {"nodes":...}
    wrapper and returned a bare [ {...} ]). Returns the list, or None. Pure, never raises.
    A bare array is a tolerated near-miss; an empty one is honest silence (handled upstream)."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, list) else None
    except (json.JSONDecodeError, TypeError):
        pass
    start = s.find("[")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                    return obj if isinstance(obj, list) else None
                except (json.JSONDecodeError, TypeError):
                    return None
    return None


def parse_stamp_instructions(llm_json: str, rules: Rules) -> list[StampInstruction]:
    """Parse the model's JSON and re-validate against the closed vocabulary.

    Backward-compatible thin wrapper over parse_with_report — returns just the list.
    Tolerant of a malformed/partial response (returns [] rather than crashing — a bad
    LLM response must never break `recall power`). Use parse_with_report when you need to
    know WHY a reply yielded nothing (run_power does, to surface schema mismatches)."""
    return parse_with_report(llm_json, rules).instructions


def _find_node_array(data: dict) -> tuple[list | None, str]:
    """Locate the node array in the parsed object. Returns (array, used_alt_key).

    The schema is "nodes". We ALSO accept a bounded set of near-miss top-level keys
    (lessons/results/items/facts/...) so a model that drifts one word still lands —
    but we record which off-schema key we fell back to, so a drift is never invisible.
    Anything outside the set is rejected (returns (None, "")) — the model does not get
    to invent arbitrary shapes; it gets a small, known set of forgivable typos."""
    for key in _ALT_NODE_KEYS:
        val = data.get(key)
        if isinstance(val, list):
            return val, ("" if key == "nodes" else key)
    return None, ""


def _first_alias(raw: dict, aliases: tuple[str, ...]) -> str:
    """First non-empty value among a node's accepted field aliases (title/body drift)."""
    for k in aliases:
        v = raw.get(k)
        if v is None:
            continue
        # a body alias may itself be a list (e.g. "facts": ["a", "b"]) — join it.
        if isinstance(v, list):
            joined = " ".join(str(x).strip() for x in v if str(x).strip())
            if joined:
                return joined
        else:
            s = str(v).strip()
            if s:
                return s
    return ""


def parse_with_report(llm_json: str, rules: Rules) -> ParseReport:
    """Parse + re-validate, and report WHY the reply yielded what it did.

    The JSON may be wrapped in prose or a ```json fence (agent CLIs do this even when
    asked for strict JSON) — we extract it. The node array is found under "nodes" or a
    bounded near-miss key; per-node title/body accept a bounded alias set. Every tag goes
    through canonicalize_tags (drops unknowns), every edge kind is checked against
    rules.edge_kinds (drops unknowns). The model never gets to widen governance — and it
    never gets to fail SILENTLY: a schema mismatch comes back as reason='no-node-key'."""
    data = _extract_json_object(llm_json)
    if not isinstance(data, dict):
        # a bare top-level array of node dicts is a tolerated near-miss (but empty [] is
        # honest silence, handled below by yielding 0 with reason 'wrong-shape' -> nothing).
        arr = _extract_json_array(llm_json)
        if arr is None:
            return ParseReport(reason="not-json")
        nodes, used_alt = arr, "(bare-array)"
    else:
        nodes, used_alt = _find_node_array(data)
        if nodes is None:
            # JSON parsed, but no recognized node array — THE dogfood bug. Loud, not silent.
            return ParseReport(reason="no-node-key")

    if not nodes:
        # an explicit empty array — the model legitimately found nothing. Honest silence.
        return ParseReport(reason="empty", used_alt_key=used_alt if used_alt != "(bare-array)" else "")

    out: list[StampInstruction] = []
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        title = _first_alias(raw, _TITLE_ALIASES)
        if not title:
            continue  # a node with no title is unusable
        why = _first_alias(raw, _BODY_ALIASES)
        # Clean anchors into SEARCHABLE terms (symmetric with how queries tokenize): a
        # model that returns a phrase or a pasted code line as an "anchor" would otherwise
        # be unfindable. Falls back to title+body tokens (in stamp()) if it leaves nothing.
        anchors = sorted(clean_anchor_terms(_as_list(raw.get("anchors"))))

        raw_tags = [str(t) for t in _as_list(raw.get("tags"))]
        tags = canonicalize_tags(raw_tags, rules.tag_aliases, rules.allowed_tags)
        dropped_tags = [
            t for t in raw_tags
            if rules.tag_aliases.get(t.strip().lower(), t.strip().lower()) not in rules.allowed_tags
            and t.strip()
        ]

        edges, dropped_edges = _validate_edges(raw.get("edges"), rules)

        out.append(
            StampInstruction(
                title=title,
                body=why,
                anchors=anchors,
                tags=tags,
                edges=edges,
                dropped_tags=dropped_tags,
                dropped_edges=dropped_edges,
            )
        )
    if not out:
        # the array held entries, but none were usable (no titles, all junk) — loud.
        return ParseReport(reason="wrong-shape", used_alt_key=used_alt if used_alt != "(bare-array)" else "")
    return ParseReport(instructions=out, used_alt_key=used_alt if used_alt != "(bare-array)" else "")


def _validate_edges(raw_edges, rules: Rules) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    edges: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []
    for e in _as_list(raw_edges):
        if isinstance(e, dict):
            kind = str(e.get("kind") or "").strip()
            target = str(e.get("target") or "").strip()
        elif isinstance(e, (list, tuple)) and len(e) == 2:
            kind, target = str(e[0]).strip(), str(e[1]).strip()
        else:
            continue
        if not (kind and target):
            continue
        if kind in rules.edge_kinds:
            edges.append((kind, target))
        else:
            dropped.append((kind, target))  # hallucinated edge kind — recorded, not used
    return edges, dropped


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]

"""LLM edge refinement — turn the deterministic `depends_on` graph into UNDERSTANDING.

The AST graph (recall.graph) states the WHAT: file A imports file B. This layer adds the
WHY: is B a thing A *implements* (a type/contract), a guard A is *guarded_by* (auth /
validation), or just a plain dependency? A small LOCAL model does this reliably **when the
task is decomposed and grounded** — it doesn't invent edges, it only classifies the ones
the AST already found, picking from a closed label set (proven: even a 3B model nails it;
free-invention overwhelms it). Write-time only; the recall() read path stays LLM-free.

Reversible + safe by construction:
  - it ONLY ever changes the `kind` of existing depends_on edges — never creates edges,
    never deletes, never touches semantic edges a human/commit declared;
  - every label is re-validated against rules.edge_kinds (a hallucinated label is ignored);
  - the original kind is recorded in edges.refined_from before the first overwrite, so
    `unrefine()` (and `recall unrefine`) resets every refined edge in full, loss-free;
  - re-running re-classifies already-refined edges (refined_from='depends_on'), so a
    better model run can correct an earlier label without a re-index.

This is the optional intelligence layer. With no provider connected, the graph stays at
the deterministic depends_on floor — still useful (measured: 68-79% of hits get a chain).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# the labels the model may assign to a dependency. Subset of rules.edge_kinds that makes
# sense for a STATIC import relation (a commit-only kind like 'supersedes' is excluded —
# the model is classifying "what is this dependency", not editing history).
_REFINE_LABELS = ("depends_on", "implements", "guarded_by", "relates_to")

# max output tokens per file classification — ONE constant feeds both the real call
# (_classify_file) and the cost preview (estimate_refine_cost) so the previewed cost is a
# true upper bound that can't drift from the send (ADR-008 cost-before-spend).
_REFINE_OUTPUT_CAP = 600

_SYSTEM = """\
You classify ONE source file's dependencies. You are given the file's source and the list \
of local modules it imports. For EACH import, pick the single best relation label:
  - guarded_by : the import enforces a rule / auth / validation the file relies on
  - implements : the file implements a type / interface / contract from the import
  - depends_on : a plain functional dependency (the safe default)
  - relates_to : weak or uncertain association
Output ONE JSON object and NOTHING else:
{"edges": [{"target": "<import path verbatim>", "kind": "<one label>"}]}
Use ONLY those four labels. Copy each target VERBATIM from the import list. If unsure, \
use depends_on. Do not add imports that are not in the list."""


@dataclass
class RefineResult:
    files_seen: int = 0
    edges_considered: int = 0
    edges_refined: int = 0       # depends_on -> a more specific kind
    edges_unchanged: int = 0     # stayed depends_on (model said so, or low confidence)
    dropped_labels: int = 0      # model returned a label outside the closed set
    call_failures: int = 0       # model calls that errored (provider down) — NOT "no change"
    by_kind: dict[str, int] = field(default_factory=dict)


def refine_edges(index, provider, *, file_byte_cap: int = 6000,
                 progress=None) -> RefineResult:
    """Classify every depends_on edge's nature with the connected (ideally local) model.

    Groups edges by source file (one model call per file, not per edge), gives the model
    the file's source + its dependency targets, and rewrites each edge's kind to the
    model's validated label. Idempotent in spirit: re-running re-classifies (a depends_on
    that became implements stays classifiable). Returns a RefineResult for transparency.
    """
    res = RefineResult()
    repo = Path(index._repo) if getattr(index, "_repo", None) else None

    # gather refinable edges grouped by source FILE (via the src node's file_path).
    # Includes edges ALREADY refined once (refined_from='depends_on') so a re-run can
    # re-classify them — e.g. a fix that changes implements -> guarded_by. A pristine
    # depends_on edge has refined_from IS NULL; a refined one carries its origin there.
    rows = index.db.execute(
        """
        SELECT e.id, ns.file_path AS src_file, nd.file_path AS dst_file
          FROM edges e
          JOIN nodes ns ON ns.id = e.src_node
          JOIN nodes nd ON nd.id = e.dst_node
         WHERE (e.kind = 'depends_on' OR e.refined_from = 'depends_on')
           AND ns.file_path IS NOT NULL AND nd.file_path IS NOT NULL
        """
    ).fetchall()
    by_file: dict[str, list[tuple[int, str]]] = {}
    for eid, src_file, dst_file in rows:
        by_file.setdefault(src_file, []).append((eid, dst_file))

    total = len(by_file)
    for n, (src_file, edges) in enumerate(by_file.items(), start=1):
        res.files_seen += 1
        res.edges_considered += len(edges)
        labels = _classify_file(provider, repo, src_file, [d for _, d in edges], file_byte_cap)
        if labels is None:  # the model call failed — count it, leave edges untouched
            res.call_failures += 1
            res.edges_unchanged += len(edges)
            if progress is not None:
                try:
                    progress(n, total)
                except Exception:
                    pass
            continue
        for eid, dst_file in edges:
            kind = labels.get(dst_file)
            if kind is None:
                res.edges_unchanged += 1
                continue
            if kind not in _REFINE_LABELS or kind not in index.rules.edge_kinds:
                res.dropped_labels += 1
                res.edges_unchanged += 1
                continue
            if kind == "depends_on":
                # the model says plain dependency. If this edge was refined before,
                # reset it (and forget the marker) so it's a clean depends_on again.
                index.db.execute(
                    "UPDATE edges SET kind='depends_on', refined_from=NULL "
                    "WHERE id=? AND refined_from IS NOT NULL", (eid,))
                res.edges_unchanged += 1
            else:
                # record the ORIGINAL kind before the first overwrite (COALESCE keeps
                # the very first origin across re-runs), so `unrefine` can reset it.
                index.db.execute(
                    "UPDATE edges SET refined_from=COALESCE(refined_from, kind), kind=? "
                    "WHERE id=?", (kind, eid))
                res.edges_refined += 1
            res.by_kind[kind] = res.by_kind.get(kind, 0) + 1
        if progress is not None:
            try:
                progress(n, total)
            except Exception:
                pass
    index.db.commit()
    return res


@dataclass
class RefineEstimate:
    files: int = 0
    edges: int = 0
    input_tokens: int = 0
    est_output_tokens: int = 0
    est_cost_usd: float = 0.0
    model: str = ""


def estimate_refine_cost(index, provider, *, file_byte_cap: int = 6000) -> RefineEstimate:
    """The ADR-008 mandatory preview for `recall refine` — builds each file's EXACT
    classification prompt and measures it with provider.count_tokens (ZERO completion
    calls; count_tokens never spends). One call per source file at _REFINE_OUTPUT_CAP
    output tokens, priced via the provider's own rate (0.0 for local Ollama / the CLI
    subscription). Mirrors power.estimate_tokens so refine has the same cost-before-spend
    guarantee (bug-hunt MEDIUM, 2026-06-17 — refine spent real money per file with no
    preview/--yes gate)."""
    repo = Path(index._repo) if getattr(index, "_repo", None) else None
    rows = index.db.execute(
        """
        SELECT e.id, ns.file_path AS src_file, nd.file_path AS dst_file
          FROM edges e
          JOIN nodes ns ON ns.id = e.src_node
          JOIN nodes nd ON nd.id = e.dst_node
         WHERE (e.kind = 'depends_on' OR e.refined_from = 'depends_on')
           AND ns.file_path IS NOT NULL AND nd.file_path IS NOT NULL
        """
    ).fetchall()
    by_file: dict[str, list[str]] = {}
    for _eid, src_file, dst_file in rows:
        by_file.setdefault(src_file, []).append(dst_file)

    total_input = 0
    for src_file, targets in by_file.items():
        source = ""
        if repo is not None:
            try:
                source = (repo / src_file).read_text(encoding="utf-8", errors="replace")[:file_byte_cap]
            except OSError:
                source = ""
        user = (f"FILE: {src_file}\n\nIMPORTS (classify each):\n"
                + "\n".join(f"- {t}" for t in targets)
                + (f"\n\nSOURCE:\n{source}" if source else ""))
        total_input += provider.count_tokens(_SYSTEM) + provider.count_tokens(user)

    est_output = len(by_file) * _REFINE_OUTPUT_CAP
    # Use the provider's OWN rate (every LLMProvider sets cost_per_token: (0,0) for local
    # Ollama / the CLI subscription, the Anthropic-tier rate for paid). We deliberately do
    # NOT import the llm cost table here — refine.py is not the LLM seam, and the read-path
    # seam guard (test_power_seam_guard) forbids importing the llm module outside power/llm.
    rate = getattr(provider, "cost_per_token", (0.0, 0.0)) or (0.0, 0.0)
    cost = total_input * rate[0] + est_output * rate[1]
    return RefineEstimate(
        files=len(by_file), edges=len(rows), input_tokens=total_input,
        est_output_tokens=est_output, est_cost_usd=cost, model=provider.model,
    )


def unrefine(index) -> int:
    """Reset every refined edge back to its original kind — the reverse of refine_edges.

    Restores `kind = refined_from` and clears the marker for all edges that carry one.
    Model-free, loss-free, idempotent (a second run finds nothing to reset). This is the
    reversibility the module promises: a refine run that mislabeled edges can be undone
    in full without re-indexing. Returns the number of edges reset."""
    cur = index.db.execute(
        "UPDATE edges SET kind=refined_from, refined_from=NULL "
        "WHERE refined_from IS NOT NULL")
    index.db.commit()
    return cur.rowcount


def _classify_file(provider, repo, src_file: str, targets: list[str],
                   byte_cap: int) -> dict[str, str] | None:
    """One model call: classify each dependency target of src_file. Returns {target: kind}
    on success ({} = a valid reply with no refinements), or None when the model call
    ITSELF failed (provider down) — the caller counts that as a failure, not "no change",
    so a fully-down provider can't masquerade as a healthy zero-refinement run."""
    source = ""
    if repo is not None:
        try:
            source = (repo / src_file).read_text(encoding="utf-8", errors="replace")[:byte_cap]
        except OSError:
            source = ""
    user = (f"FILE: {src_file}\n\nIMPORTS (classify each):\n"
            + "\n".join(f"- {t}" for t in targets)
            + (f"\n\nSOURCE:\n{source}" if source else ""))
    try:
        resp = provider.complete(_SYSTEM, user, max_tokens=_REFINE_OUTPUT_CAP)
    except Exception:
        return None  # call failed (network/provider) — distinct from an empty reply
    return _parse_labels(resp.text, set(targets))


def _parse_labels(text: str, valid_targets: set[str]) -> dict[str, str]:
    """Extract {target: kind} from the model JSON. Only targets we actually asked about
    are accepted (the model can't relabel something we didn't give it). Tolerant of a
    prose-wrapped or fenced reply."""
    data = _loads(text)
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for e in data.get("edges", []) or []:
        if not isinstance(e, dict):
            continue
        target = str(e.get("target") or "").strip()
        kind = str(e.get("kind") or "").strip().lower()
        if target in valid_targets and kind:
            out[target] = kind
    return out


def _loads(text: str) -> Any:
    if not isinstance(text, str):
        return None
    s = text.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # a fenced or prose-wrapped object — grab the first balanced {...}
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except Exception:
                    return None
    return None

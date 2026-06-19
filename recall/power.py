"""Power Mode orchestration (ADR-008) — the ONE module that reaches the LLM seam.

STEP 5 (this part): the deterministic, FREE work — choosing what the AI will read
and estimating the token cost BEFORE a single token is spent (the ADR-008 mandate).

  - select_hotspots: token-free. churn from freshness.RepoState (per-file commit
    counts, gathered in 3 git reads, no new subprocess) + code-symbol density and
    the existing bootstrap nodes from the index. Ranks by churn × density.
  - estimate_tokens: builds the EXACT prompt per hotspot (source capped to a byte
    budget) and measures it with provider.count_tokens() — ZERO complete() calls.

STEP 6 (run_power) lands here next; it's the only code that calls provider.complete().
This module is on the LLM-allowed list of the seam guard (test_power_seam_guard.py).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from recall.freshness import RepoState
from recall.llm import LLMProvider, cost_for
from recall.power_prompt import build_understanding_prompt, parse_with_report

# Defaults (plan open-decision #1): top-N hotspots by churn, each file's source
# capped so one huge file can't blow the token budget. Both overridable per call.
DEFAULT_TOP_N = 50
DEFAULT_FILE_BYTE_CAP = 8_192  # 8 KB of source per hotspot
DEFAULT_OUTPUT_BUDGET = 400  # expected completion tokens per hotspot
# The provider's max_tokens per hotspot is this multiple of the budget — i.e. the model
# may legitimately emit up to OUTPUT_CAP_MULTIPLIER × the expected budget. The ADR-008
# preview MUST price against this real ceiling, not the expected budget, or the previewed
# cost the user approves with --yes understates the worst-case output bill by exactly this
# factor (bug-hunt MEDIUM, 2026-06-17). One constant feeds BOTH estimate() and the
# complete() call so the preview is a provable upper bound that can't drift from the send.
OUTPUT_CAP_MULTIPLIER = 2

# Source files worth understanding. Keep it boring + explicit — bootstrap already
# indexed these, we just rank a subset for the deep read.
_CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".rb", ".java", ".kt",
    ".c", ".h", ".cpp", ".cs", ".php", ".swift", ".sql",
}


@dataclass
class Hotspot:
    """A file the AI should read once, with why it ranked + its known nodes."""

    file_path: str
    churn: int  # commits that touched this file (from RepoState)
    symbol_count: int  # code-symbol nodes bootstrap found here
    existing_node_ids: list[int] = field(default_factory=list)

    @property
    def score(self) -> float:
        # churn is the strongest signal (a file edited often holds the live decisions);
        # symbol density breaks ties toward files with real structure over config blobs.
        return self.churn * 2.0 + self.symbol_count


@dataclass
class Estimate:
    hotspots: int
    input_tokens: int
    est_output_tokens: int
    est_cost_usd: float
    model: str
    per_file: list[tuple[str, int]] = field(default_factory=list)  # (path, input_tokens)
    # The EXACT (system, user) prompts measured during estimation, one per hotspot in
    # hotspot order. run_power reuses these instead of re-reading each file from disk, so
    # the approved estimate is provably the prompt that gets sent — closing the TOCTOU
    # window where an editor save / the dashboard watcher re-indexing between estimate and
    # send made the billed prompt differ from the previewed one (bug-hunt LOW, 2026-06-17).
    prompts: list[tuple[str, str]] = field(default_factory=list, repr=False)


# ----------------------------------------------------------------- hotspot choice
def select_hotspots(
    index,
    repo: str | Path,
    *,
    scope: str | None = None,
    top_n: int = DEFAULT_TOP_N,
) -> list[Hotspot]:
    """Rank the files worth a deep read — token-free, no completion, no new git calls.

    churn comes from RepoState (3 git reads for the whole repo); symbol density +
    the existing bootstrap nodes come from the index. `scope` filters by path prefix
    (e.g. 'src/auth'); top_n caps the count (the token budget knob)."""
    repo = Path(repo)
    state = RepoState(repo)

    # per-file code-symbol density + which bootstrap nodes live there (for enrichment)
    symbols: dict[str, int] = {}
    node_ids: dict[str, list[int]] = {}
    for nid, fp in index.db.execute(
        "SELECT id, file_path FROM nodes "
        "WHERE file_path IS NOT NULL AND file_path != ''"
    ).fetchall():
        symbols[fp] = symbols.get(fp, 0) + 1
        node_ids.setdefault(fp, []).append(nid)

    # candidate files = everything bootstrap pinned a node to, that is a code file,
    # exists on disk, and (if scope given) sits under the scope prefix.
    norm_scope = scope.replace("\\", "/").strip("/") if scope else None
    hotspots: list[Hotspot] = []
    for fp in symbols:
        norm = fp.replace("\\", "/")
        if Path(fp).suffix.lower() not in _CODE_EXTS:
            continue
        if norm_scope and not norm.startswith(norm_scope):
            continue
        if not (repo / fp).exists():
            continue  # gone from disk -> nothing to read
        churn = len(state._touch.get(fp, ())) if state.has_git else 0
        hotspots.append(
            Hotspot(
                file_path=fp,
                churn=churn,
                symbol_count=symbols[fp],
                existing_node_ids=node_ids.get(fp, []),
            )
        )

    hotspots.sort(key=lambda h: (-h.score, h.file_path))  # deterministic tie-break by path
    return hotspots[: max(0, top_n)]


# ----------------------------------------------------------------- token estimate
def estimate_tokens(
    index,
    repo: str | Path,
    hotspots: list[Hotspot],
    provider: LLMProvider,
    *,
    file_byte_cap: int = DEFAULT_FILE_BYTE_CAP,
    output_budget: int = DEFAULT_OUTPUT_BUDGET,
) -> Estimate:
    """The ADR-008 mandatory preview. Builds each hotspot's EXACT prompt and measures
    it — ZERO completion calls (provider.complete() is never touched here). Cost uses
    the llm.py table (0.0 for local Ollama)."""
    repo = Path(repo)
    total_input = 0
    per_file: list[tuple[str, int]] = []
    prompts: list[tuple[str, str]] = []
    for h in hotspots:
        system, user = _prompt_for_hotspot(index, repo, h, file_byte_cap)
        n = provider.count_tokens(system) + provider.count_tokens(user)
        total_input += n
        per_file.append((h.file_path, n))
        prompts.append((system, user))  # captured ONCE; run_power reuses these exact bytes

    # Price against the REAL ceiling the run uses (output_budget × the cap multiplier),
    # not the expected budget — so the previewed cost is a true upper bound (ADR-008).
    est_output = len(hotspots) * output_budget * OUTPUT_CAP_MULTIPLIER
    return Estimate(
        hotspots=len(hotspots),
        input_tokens=total_input,
        est_output_tokens=est_output,
        est_cost_usd=_estimate_cost(provider, total_input, est_output),
        model=provider.model,
        per_file=per_file,
        prompts=prompts,
    )


def _estimate_cost(provider: LLMProvider, input_tokens: int, output_tokens: int) -> float:
    """The provider's OWN rate is authoritative — a local/free provider reports
    (0.0, 0.0) and must cost exactly 0, never the model-name guess. Only when a
    provider has no rate of its own do we fall back to the model table (so an
    unknown *paid* Anthropic model still estimates a paid cost, never silently 0)."""
    rate = getattr(provider, "cost_per_token", None)
    if rate is not None:
        return input_tokens * rate[0] + output_tokens * rate[1]
    return cost_for(provider.model, input_tokens, output_tokens)


def _prompt_for_hotspot(index, repo: Path, h: Hotspot, byte_cap: int) -> tuple[str, str]:
    """Build the exact (system, user) we WILL send for a hotspot — source capped.

    Shared by estimate (count only) and run_power (send), so the estimate matches the
    real call byte-for-byte. A read error yields an empty source rather than crashing
    the whole estimate over one unreadable file."""
    try:
        source = (repo / h.file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        source = ""
    if len(source.encode("utf-8")) > byte_cap:
        source = source.encode("utf-8")[:byte_cap].decode("utf-8", errors="ignore")
    existing = _existing_for(index, h)
    from recall.rules import load_rules

    rules = index.rules if getattr(index, "rules", None) else load_rules(repo)
    return build_understanding_prompt(
        file_path=h.file_path, source=source, existing=existing, rules=rules
    )


def _existing_for(index, h: Hotspot) -> list[dict]:
    """The bootstrap knowledge already attached to this file — fed to the prompt so
    the model enriches rather than duplicates.

    Query the file's lessons directly by file_path — NOT via an IN over
    h.existing_node_ids. A file with >999 indexed nodes (a big generated stub) would
    otherwise bind one '?' per id and trip SQLite's variable limit ("too many SQL
    variables"), crashing even the mandatory free estimate. This was the engine's last
    unbatched IN; selecting by file_path needs zero binds and is what we actually want.
    (P2 bug-hunt round 3, 2026-06-15.)"""
    if not h.file_path:
        return []
    rows = index.db.execute(
        "SELECT title, body FROM nodes WHERE file_path = ? AND kind='lesson'",
        (h.file_path,),
    ).fetchall()
    return [{"title": r[0], "why": (r[1] or "").splitlines()[0] if r[1] else ""} for r in rows]


# ----------------------------------------------------------------- orchestration
@dataclass
class PowerResult:
    run: int
    estimate: Estimate
    nodes_added: int = 0
    edges_added: int = 0
    synonyms_added: int = 0
    dropped_tags: int = 0
    dropped_edges: int = 0
    files: int = 0
    dry_run: bool = False
    # how many provider replies yielded NO usable nodes due to a real failure (bad JSON
    # or a schema mismatch — NOT honest {"nodes": []} silence). The dogfood bug was 45 of
    # these hiding silently; now they're counted and surfaced so a run can't look fine.
    responses_discarded: int = 0
    # the off-schema top-level keys the model used instead of "nodes" (forgiven, recorded).
    alt_keys_seen: dict[str, int] = field(default_factory=dict)


def run_power(
    index,
    repo: str | Path,
    *,
    provider: LLMProvider,
    scope: str | None = None,
    top_n: int = DEFAULT_TOP_N,
    file_byte_cap: int = DEFAULT_FILE_BYTE_CAP,
    output_budget: int = DEFAULT_OUTPUT_BUDGET,
    dry_run: bool = False,
    progress=None,
) -> PowerResult:
    """The end-to-end Power-Mode run — the ONLY code that calls provider.complete().

    `progress`, if given, is called as progress(done, total) once before the first
    hotspot (0, total) and after each one — so a UI (the dashboard) can show a live
    bar. It must never raise into the run; the caller wraps it defensively.

    Steps: choose hotspots (free) -> estimate (free) -> for each hotspot build the
    exact prompt, complete() it, parse + re-validate, stamp origin='power' tagged to
    this run. Every NEW node carries power_run=N (undo lifts it by tag); every MERGE
    onto a pre-existing node records its added synonyms in the run ledger (undo removes
    exactly those). The run's bookkeeping (estimate + actual) lands in meta.

    dry_run=True must be driven by a :memory: index by the caller (the CLI), so the
    real .mind/index.db is never touched — here we only flag the result + skip the
    meta record on the real index."""
    repo = Path(repo)
    head = _head_sha(repo)
    run = index.next_power_run()

    hotspots = select_hotspots(index, repo, scope=scope, top_n=top_n)
    estimate = estimate_tokens(
        index, repo, hotspots, provider,
        file_byte_cap=file_byte_cap, output_budget=output_budget,
    )

    result = PowerResult(run=run, estimate=estimate, files=len(hotspots), dry_run=dry_run)
    added_anchors: dict[str, list[str]] = {}  # the synonym-undo ledger (node_id -> terms)

    total = len(hotspots)

    def _tick(done: int) -> None:
        if progress is not None:
            try:
                progress(done, total)
            except Exception:
                pass  # a UI progress hook must never break the run

    _tick(0)
    # Track outcome so a provider failure mid-run is recorded (status "partial") rather
    # than leaving orphan nodes with no run record. The CLI/dashboard still see the error
    # (re-raised after recording), but `recall power --list` and `recall undo` now know
    # about the partial run and its synonym ledger.
    status = "done"
    run_error: BaseException | None = None
    try:
        for n, h in enumerate(hotspots, start=1):
            # Reuse the EXACT prompt measured during estimation (estimate.prompts is in
            # hotspot order) rather than re-reading the file — so the bytes sent match the
            # bytes priced byte-for-byte, even if the file changed since the preview
            # (TOCTOU, bug-hunt LOW 2026-06-17). Fall back to a fresh build only if the
            # capture is somehow short (defensive; should never happen).
            if n - 1 < len(estimate.prompts):
                system, user = estimate.prompts[n - 1]
            else:
                system, user = _prompt_for_hotspot(index, repo, h, file_byte_cap)
            resp = provider.complete(system, user, max_tokens=output_budget * OUTPUT_CAP_MULTIPLIER)
            report = parse_with_report(resp.text, index.rules)
            # surface schema mismatches loudly: a reply that yielded nothing for a REAL reason
            # (bad JSON / no recognized node key) is counted, never swallowed (the dogfood bug).
            if report.yielded_nothing:
                result.responses_discarded += 1
            if report.used_alt_key:
                result.alt_keys_seen[report.used_alt_key] = (
                    result.alt_keys_seen.get(report.used_alt_key, 0) + 1
                )
            for inst in report.instructions:
                res = index.stamp(
                    inst.title,
                    body=inst.body,
                    anchors=inst.anchors,
                    tags=inst.tags,
                    edges=inst.edges,
                    kind="lesson",
                    file_path=h.file_path,
                    sha=head,
                    origin="power",
                    power_run=run,
                    base_sha=head,
                )
                if res["action"] == "NEW":
                    result.nodes_added += 1
                    result.edges_added += len(inst.edges)
                elif res["action"] == "MERGE":
                    # synonyms grafted onto a pre-existing node can't CASCADE on undo —
                    # record exactly what we added so undo_power_run removes precisely them.
                    syns = res.get("added_anchors") or []
                    if syns:
                        added_anchors.setdefault(str(res["node_id"]), []).extend(syns)
                        result.synonyms_added += len(syns)
                result.dropped_tags += len(inst.dropped_tags)
                result.dropped_edges += len(inst.dropped_edges)
            _tick(n)  # one hotspot done -> update the live progress bar
    except (KeyboardInterrupt, Exception) as e:  # noqa: BLE001 — record then re-raise
        status = "interrupted" if isinstance(e, KeyboardInterrupt) else "partial"
        run_error = e
    finally:
        # Persist the run record (estimate vs actual + the synonym ledger) even on a
        # partial/interrupted run, so the already-stamped nodes are listed and fully
        # undoable (with their synonym ledger). Skipped on a dry run so a throwaway
        # :memory: index leaves no trace — the caller shows the result and discards it.
        if not dry_run:
            index.record_power_run(run, {
                "base_sha": head,
                "scope": scope,
                "model": provider.model,
                "status": status,
                "est_input_tokens": estimate.input_tokens,
                "est_output_tokens": estimate.est_output_tokens,
                "est_cost_usd": estimate.est_cost_usd,
                "nodes_added": result.nodes_added,
                "edges_added": result.edges_added,
                "synonyms_added": result.synonyms_added,
                "files": result.files,
                "added_anchors": added_anchors,
            })
    if run_error is not None:
        raise run_error
    return result


def _head_sha(repo: Path) -> str | None:
    """Current HEAD, or None if not a git repo (Power Mode pins to the read base)."""
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        return p.stdout.strip() if p.returncode == 0 else None
    except OSError:
        return None

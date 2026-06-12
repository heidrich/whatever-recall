"""Governance — rules.md loading + compilation.

Three layers (most specific wins, except the core veto which is unoverridable):
    1. built-in defaults (Rules.defaults)
    2. ~/.recall/rules.md            (user-global)
    3. <repo>/.recall/rules.md       (project)

The frontmatter is parsed by a tiny dependency-free reader (no PyYAML): scalars,
flat lists, and one level of `key:` blocks of `name: value` pairs — enough for our
schema. The `core:` block is special: its floor is a hard minimum no layer can
lower, and its tabus/vetoes are additive only.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field, replace
from pathlib import Path

from recall.anchors import DEFAULT_ALLOWED_TAGS, DEFAULT_TAG_ALIASES

# Facets whose weight a project layer may not lower below the core default — so a
# careless/hostile project rules.md cannot silence security lessons by zeroing
# their weight (the core veto, extended beyond silence_floor).
_CORE_FACET_FLOOR = {"security": 2.0}


@dataclass
class Rules:
    silence_floor: int = 2
    dedup_threshold: float = 0.45
    context_multiplier: float = 1.5
    facet_weights: dict[str, float] = field(default_factory=dict)
    context_boost: dict[str, str] = field(default_factory=dict)
    stay_silent_on: set[str] = field(default_factory=set)
    surface_on: set[str] = field(default_factory=set)
    edge_kinds: set[str] = field(default_factory=set)
    allowed_tags: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWED_TAGS))
    tag_aliases: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TAG_ALIASES))
    # Query-side stopwords a project ADDS on top of anchors.QUERY_STOP (the shipped
    # EN+DE set stays — additive only, so a layer can localize ("quoi", "encore")
    # but never un-stop the defaults). Lowercased on merge; applied in recall().
    query_stopwords: frozenset[str] = field(default_factory=frozenset)
    # core veto — a hard minimum the project layer cannot weaken.
    core_silence_floor_min: int = 1

    def facet_weight(self, facet: str) -> float:
        return self.facet_weights.get(facet, 1.0)

    @classmethod
    def defaults(cls) -> "Rules":
        return cls(
            facet_weights={
                "security": 2.0, "logic": 1.5, "math": 1.5, "backend": 1.2,
                "bugfix": 1.3, "frontend": 1.0, "ui": 0.8, "docs": 0.7,
                "chore": 0.3,  # housekeeping surfaces only when nothing better matches
                # task/plan lifecycle (ADR-017): the original 1.8 task loudness predates
                # the 3-track split — it was meant to lift tasks over routine CODE in the
                # old mixed list. Since ADR-016 tasks have their OWN surfaces (open_tasks
                # track, brief), and in the knowledge track 1.8 let a growing task corpus
                # bury the WHY answers (measured 2026-06-11: knowledge r@3 17/25 with 1.8
                # -> 22/25 with 1.0; the code track unchanged). Neutral weight, own lane.
                "task": 1.0, "plan": 1.6, "roadmap": 1.4, "sprint": 1.4,
            },
            context_boost={
                "auth": "security", "rls": "security", "login": "security",
                "migration": "backend", "sql": "backend",
                "component": "ui", "css": "ui", ".tsx": "ui",
                "math": "math",
            },
            edge_kinds={
                "implements", "decided_by", "supersedes", "guarded_by",
                "warns_about", "recurs_with", "presents", "relates_to",
                # depends_on: the static dependency edge (A imports B). Stamped
                # deterministically from the AST at write-time (recall.graph); the
                # LLM einordnung-layer may refine it to implements/guarded_by later.
                "depends_on",
                # co_changed: files edited together in one coding session. NOT from the
                # AST — it catches the *invisible* relation (two files that must change
                # together but don't import each other). Captured for free as a by-product
                # of the LLM coding (ADR-015: heal while coding). Weak by default; the
                # einordnung-layer may refine it to relates_to/guarded_by when the why is
                # known. The recall read-path stays LLM-free (ADR-014).
                "co_changed",
            },
            surface_on={"edit", "task_start", "commit"},
        )


# --------------------------------------------------------------------- loading
def load_rules(repo: str | Path | None = None) -> Rules:
    """Layer defaults < ~/.recall/rules.md < <repo>/.recall/rules.md.

    The core veto is enforced last: silence_floor can never drop below
    core_silence_floor_min, and stay_silent_on accumulates (never shrinks).
    """
    rules = Rules.defaults()
    rules = _apply_file(rules, _bundled_rules_path())  # the shipped rules.md
    rules = _apply_file(rules, Path.home() / ".recall" / "rules.md")
    if repo:
        rules = _apply_file(rules, Path(repo) / ".recall" / "rules.md")
    return _enforce_core(rules)


def _bundled_rules_path() -> Path:
    return Path(__file__).resolve().parent / "rules.md"


def _apply_file(rules: Rules, path: Path) -> Rules:
    if not path.exists():
        return rules
    try:
        fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
        if not fm:
            return rules
        return _merge(rules, fm)
    except (OSError, ValueError) as e:
        # A malformed rules.md must never crash engine startup — keep the prior
        # layer and warn. (ValueError comes from the bounds validators.)
        import sys
        print(f"recall: ignoring invalid {path}: {e}", file=sys.stderr)
        return rules


def _enforce_core(rules: Rules) -> Rules:
    floor = max(rules.silence_floor, rules.core_silence_floor_min)
    # Floor core-critical facet weights so a project can't zero out security lessons.
    weights = dict(rules.facet_weights)
    for facet, minimum in _CORE_FACET_FLOOR.items():
        weights[facet] = max(weights.get(facet, minimum), minimum)
    return replace(rules, silence_floor=floor, facet_weights=weights)


# ------------------------------------------------------------------- merging
def _merge(base: Rules, fm: dict) -> Rules:
    out = replace(base)
    if "silence_floor" in fm:
        out.silence_floor = _bounded_int(fm["silence_floor"], lo=0, name="silence_floor")
    if "dedup_threshold" in fm:
        out.dedup_threshold = _bounded_float(fm["dedup_threshold"], lo=0.0, hi=1.0, name="dedup_threshold")
    if "context_multiplier" in fm:
        out.context_multiplier = _bounded_float(fm["context_multiplier"], lo=0.0, name="context_multiplier")
    if isinstance(fm.get("facet_weights"), dict):
        out.facet_weights = {
            **out.facet_weights,
            **{k: _bounded_float(v, lo=0.0, name=f"facet_weights.{k}") for k, v in fm["facet_weights"].items()},
        }
    if isinstance(fm.get("context_boost"), dict):
        out.context_boost = {**out.context_boost, **{str(k): str(v) for k, v in fm["context_boost"].items()}}
    if isinstance(fm.get("tag_aliases"), dict):
        out.tag_aliases = {**out.tag_aliases, **{str(k): str(v) for k, v in fm["tag_aliases"].items()}}
    # stay_silent_on is additive only — silence is the safe direction, a later
    # layer may add tabus but never drop one (the core veto for tabus).
    out.stay_silent_on = out.stay_silent_on | _as_set(fm.get("stay_silent_on"))
    # query_stopwords is additive only for the same reason: a project can quiet
    # extra filler (other languages, domain noise) but never un-stop the shipped
    # defaults (anchors.QUERY_STOP is a module constant, untouchable from here).
    out.query_stopwords = out.query_stopwords | {s.lower() for s in _as_set(fm.get("query_stopwords"))}
    # surface_on / edge_kinds / allowed_tags are REPLACE-if-present: a project must
    # be able to NARROW the surface (quiet the hook) and restrict the closed
    # vocabularies. Additive-only here would only ever let projects add junk.
    if "surface_on" in fm:
        out.surface_on = _as_set(fm["surface_on"])
    if "edge_kinds" in fm:
        out.edge_kinds = _as_set(fm["edge_kinds"])
    if "allowed_tags" in fm:
        out.allowed_tags = _as_set(fm["allowed_tags"])
    if isinstance(fm.get("core"), dict) and "silence_floor_min" in fm["core"]:
        out.core_silence_floor_min = max(
            out.core_silence_floor_min, _bounded_int(fm["core"]["silence_floor_min"], lo=0, name="core.silence_floor_min")
        )
    return out


def _bounded_int(v, *, lo: int, hi: int | None = None, name: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"rules.md: {name} must be an integer, got {v!r}")
    if n < lo or (hi is not None and n > hi):
        raise ValueError(f"rules.md: {name}={n} out of range [{lo}, {hi if hi is not None else '∞'}]")
    return n


def _bounded_float(v, *, lo: float, hi: float | None = None, name: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"rules.md: {name} must be a number, got {v!r}")
    if not math.isfinite(f):
        raise ValueError(f"rules.md: {name} must be finite, got {f!r}")
    if f < lo or (hi is not None and f > hi):
        raise ValueError(f"rules.md: {name}={f} out of range [{lo}, {hi if hi is not None else '∞'}]")
    return f


def _as_set(v) -> set[str]:
    if v is None:
        return set()
    if isinstance(v, (list, tuple, set)):
        return {str(x).strip() for x in v if str(x).strip()}
    return {str(v).strip()}


# ----------------------------------------------------- tiny frontmatter reader
def _parse_frontmatter(text: str) -> dict:
    """Read the leading `--- ... ---` YAML-ish block. No PyYAML.

    Supports: `key: scalar`, `key: [a, b, c]`, and one level of nested
    `key:` blocks whose children are `  name: value` (2-space indent).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    body: list[str] = []
    closed = False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        body.append(line)
    if not closed:
        # No closing fence — treat as no-frontmatter rather than swallowing the
        # whole document body as keys (which could crash _merge on prose).
        return {}

    result: dict = {}
    current_block: str | None = None
    for raw in body:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indented = raw[0] in (" ", "\t")
        key, _, val = raw.strip().partition(":")
        key, val = key.strip(), val.strip()
        if indented:
            if current_block is None:
                # Orphan indented line (no open block) — skip it rather than
                # silently promoting it to a top-level governance key.
                continue
            result[current_block][key] = _coerce(val)
            continue
        current_block = None
        if val == "":  # a block header like `facet_weights:`
            result[key] = {}
            current_block = key
        else:
            result[key] = _coerce(val)
    return result


def _coerce(val: str):
    val = val.strip()  # tolerate trailing/leading whitespace before type detection
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        # Quote-aware split so a comma inside "a, b" stays one item.
        items = next(csv.reader([inner], skipinitialspace=True))
        return [x.strip().strip("'\"") for x in items if x.strip()]
    low = val.lower()
    if low in ("true", "false"):
        return low == "true"
    # int() / float() accept '_' separators, 'inf', 'nan' etc.; _merge's bounds
    # checks reject the non-finite/out-of-range ones, so leniency here is safe.
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val.strip("'\"")

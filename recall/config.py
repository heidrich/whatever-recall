"""Build & share settings — the single source of truth the dashboard modal, the
CLI and the hooks all read.

One file, git-tracked: `<repo>/.recall/config.toml`, `[share]` table. The dashboard
"Build Settings" modal writes it; `recall stamp` / `recall export` / the commit hooks
read it. Tracking it in git means a team shares the same build rules — what may leave
the machine is a project decision, not a per-machine accident.

SAFE DEFAULTS (fail-closed): a missing or malformed config never loosens anything.
Default visibility is `team` (most knowledge is meant to be shared) but the two
LEAK GUARDS — block_raw_mind_commit and dry_run_before_export — default ON, so the
absence of a config can never enable a leak path.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, asdict
from pathlib import Path

CONFIG_REL = ".recall/config.toml"

# the only two accepted visibility values; anything else falls back to the safe one
_VALID_VISIBILITY = {"team", "private"}


@dataclass(frozen=True)
class BuildConfig:
    # --- Privacy & sharing (the security core) ---
    # default visibility for a NEW stamp when --private is not passed
    default_visibility: str = "team"
    # refuse a git commit that stages the raw index DB or an un-purged brain copy
    block_raw_mind_commit: bool = True
    # before an export/share write, show the private-node count and require it be clean
    dry_run_before_export: bool = True
    # --- Export ---
    # default destination for `recall export` (overridable by --out)
    export_path: str = ".mind/shared.db"
    # extra tags/anchors to drop from a shared brain on top of private nodes
    export_exclude: tuple[str, ...] = ()

    def to_public_dict(self) -> dict:
        """A plain dict for the dashboard / state block (lists, not tuples)."""
        d = asdict(self)
        d["export_exclude"] = list(self.export_exclude)
        return d


def _coerce(raw: dict) -> BuildConfig:
    """Build a BuildConfig from a parsed [share] table, clamping to safe values.
    Unknown keys are ignored; bad types fall back to the default (never crash)."""
    d = BuildConfig()  # defaults
    vis = str(raw.get("default_visibility", d.default_visibility)).strip().lower()
    if vis not in _VALID_VISIBILITY:
        vis = "team"
    # leak guards: only an explicit, correctly-typed `false` can turn them off
    block = raw.get("block_raw_mind_commit", d.block_raw_mind_commit)
    dry = raw.get("dry_run_before_export", d.dry_run_before_export)
    path = str(raw.get("export_path", d.export_path)).strip() or d.export_path
    exc = raw.get("export_exclude", [])
    exc = tuple(str(x).strip() for x in exc if str(x).strip()) if isinstance(exc, list) else ()
    return BuildConfig(
        default_visibility=vis,
        block_raw_mind_commit=bool(block) if isinstance(block, bool) else True,
        dry_run_before_export=bool(dry) if isinstance(dry, bool) else True,
        export_path=path,
        export_exclude=exc,
    )


def config_path(repo: str | Path) -> Path:
    return Path(repo) / CONFIG_REL


def load_build_config(repo: str | Path) -> BuildConfig:
    """Read <repo>/.recall/config.toml [share]. Missing/malformed → safe defaults.
    NEVER raises: a broken config must not break stamp/export/commit — it just falls
    back to the safest behaviour (the leak guards stay ON)."""
    p = config_path(repo)
    if not p.is_file():
        return BuildConfig()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return BuildConfig()  # fail-closed: a corrupt config never loosens anything
    share = data.get("share", {})
    if not isinstance(share, dict):
        return BuildConfig()
    return _coerce(share)


def write_build_config(repo: str | Path, cfg: BuildConfig) -> Path:
    """Persist a BuildConfig to <repo>/.recall/config.toml [share] (the dashboard
    modal's writer). Hand-rolled TOML (no extra dep) — the schema is tiny + flat."""
    p = config_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# whatever-recall build & share settings — the dashboard 'Build Settings'",
        "# modal + the CLI/hooks all read this. Git-tracked so a team shares the rules.",
        "[share]",
        f'default_visibility = "{cfg.default_visibility}"',
        f"block_raw_mind_commit = {str(cfg.block_raw_mind_commit).lower()}",
        f"dry_run_before_export = {str(cfg.dry_run_before_export).lower()}",
        f'export_path = "{cfg.export_path}"',
        "export_exclude = [" + ", ".join(f'"{x}"' for x in cfg.export_exclude) + "]",
        "",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p

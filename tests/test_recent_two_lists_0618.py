"""Drift-guard: the project switcher routes projects into two lists, by path.

Owner-found bug (2026-06-18): the dashboard's "recent projects" switcher filled
up with `tmp…` folders. Root cause — the test suite and the proof/ harness spawn
dashboards on throwaway repos under the OS temp dir, and those landed in the same
list as the developer's real projects.

The owner's fix is TWO LISTS, routed automatically by the repo path itself, so the
rule is identical for every AI / test / harness — no flag anyone can forget to set:

  * a real project           → the PRODUCTION list (recent.json)
  * an ephemeral repo        → the TEST list       (recent_test.json)
    (under the OS temp dir, or inside a pytest sandbox)

The modal's Production⇄Test toggle picks which to show; production is the default.
These pins lock the routing so it can't silently regress (e.g. someone "simplifies"
_remember_recent back to a single list and the owner's switcher fills with junk
again).

LOCATION-INDEPENDENT BY DESIGN: these tests must pass no matter where the suite is
checked out — including from a temp dir (CI, a fresh clone staged under tmp/). So
the "is this a real project?" cases never assume the repo root itself is
non-ephemeral; the pure-logic cases use constructed absolute paths, and the
filesystem cases re-point `tempfile.gettempdir()` at an isolated root so a dir we
create OUTSIDE it is genuinely classified as a real project. (The earlier version
used `Path(__file__).parents[1]` as "a real project" and inverted when the suite
ran from a temp checkout — exactly the kind of flake we won't ship.)
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from recall import dashboard as dash


@pytest.fixture
def real_dir():
    """A genuinely NON-ephemeral directory, portable across machines.

    `_is_ephemeral_path` rejects two things: a path under `tempfile.gettempdir()`,
    and any path with a `pytest-…` segment. pytest's own `tmp_path` always has a
    `pytest-of-<user>` ancestor, so it can NEVER stand in for a real project. We
    create a uniquely-named dir under the user's home (no temp root, no pytest
    segment) and remove it after — the only location that satisfies the classifier
    the same way a developer's actual project would, on any machine."""
    base = Path.home() / ".recall-test-real-dirs"
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="proj-", dir=base))
    # sanity: this really is classified as a real project everywhere
    assert not dash._is_ephemeral_path(d)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
        try:
            base.rmdir()  # drop the parent too if we were the last user
        except OSError:
            pass


# --------------------------------------------------------------- classifier
def test_is_ephemeral_flags_tempdir_and_pytest_paths(tmp_path):
    tmp_root = Path(tempfile.gettempdir())
    # under the OS temp dir → ephemeral
    assert dash._is_ephemeral_path(tmp_root / "tmpbp5oua1y")
    assert dash._is_ephemeral_path(tmp_root / "tmpbaq7sxvj" / "proj")
    # a pytest sandbox segment anywhere in the path → ephemeral (tmp_path always
    # carries a `pytest-of-<user>` ancestor under real pytest)
    assert dash._is_ephemeral_path(tmp_path)


def test_is_ephemeral_keeps_real_project_paths(real_dir):
    assert real_dir.is_dir()
    assert not dash._is_ephemeral_path(real_dir)


# --------------------------------------------------------------- routing
def test_recent_path_for_routes_ephemeral_to_test_list(tmp_path):
    """An ephemeral repo derives the *_test sibling of the production base."""
    base = tmp_path / "store" / "recent.json"
    sandbox = Path(tempfile.gettempdir()) / "tmpwhatever" / "proj"
    routed = dash._recent_path_for(sandbox, base=base)
    assert routed == base.with_name("recent_test.json")


def test_recent_path_for_routes_real_to_production_base(real_dir):
    """A real repo routes straight to the production base, untouched."""
    base = real_dir / "store" / "recent.json"
    routed = dash._recent_path_for(real_dir, base=base)
    assert routed == base


# --------------------------------------------------------------- write side
def test_remember_routes_temp_repo_to_test_list_not_production(tmp_path, monkeypatch):
    """A dashboard started on a temp repo lands in the TEST list — never production.

    tmp_path carries a `pytest-…` ancestor, so the repo under it is a sandbox. The
    production file must stay empty; the derived test file must hold the repo.
    """
    prod = tmp_path / "store" / "recent.json"
    monkeypatch.setattr(dash, "RECENT_PATH", prod)

    sandbox_repo = tmp_path / "proj"  # ephemeral via the pytest segment
    sandbox_repo.mkdir()
    dash._remember_recent(sandbox_repo, sandbox_repo / ".mind" / "index.db")

    test_file = prod.with_name("recent_test.json")
    prod_items = json.loads(prod.read_text(encoding="utf-8")) if prod.exists() else []
    test_items = json.loads(test_file.read_text(encoding="utf-8")) if test_file.exists() else []

    assert prod_items == [], "a temp/sandbox repo must NOT pollute the production list"
    assert [r["path"] for r in test_items] == [str(sandbox_repo.resolve())], (
        "the temp repo must be recorded in the test list"
    )


def test_remember_routes_real_repo_to_production_list(real_dir, monkeypatch):
    """A real (non-ephemeral) repo lands in production, leaving the test list empty."""
    prod = real_dir / "store" / "recent.json"
    monkeypatch.setattr(dash, "RECENT_PATH", prod)

    proj = real_dir / "my-app"  # under a real home dir → not ephemeral
    proj.mkdir()
    dash._remember_recent(proj, proj / ".mind" / "index.db")

    test_file = prod.with_name("recent_test.json")
    prod_items = json.loads(prod.read_text(encoding="utf-8")) if prod.exists() else []
    test_items = json.loads(test_file.read_text(encoding="utf-8")) if test_file.exists() else []

    assert [r["path"] for r in prod_items] == [str(proj.resolve())], (
        "a real repo must be recorded in the production list"
    )
    assert test_items == [], "a real repo must NOT leak into the test list"


def test_two_lists_stay_isolated(real_dir, tmp_path, monkeypatch):
    """Recording into both lists never cross-contaminates."""
    prod = real_dir / "store" / "recent.json"
    monkeypatch.setattr(dash, "RECENT_PATH", prod)

    proj = real_dir / "my-app"        # real
    sandbox = tmp_path / "sandbox"    # ephemeral (pytest segment)
    proj.mkdir()
    sandbox.mkdir()

    dash._remember_recent(proj, proj / ".mind" / "index.db")
    dash._remember_recent(sandbox, sandbox / ".mind" / "index.db")

    prod_paths = {r["path"] for r in dash._load_recent(prod)}
    test_paths = {r["path"] for r in dash._load_recent(prod.with_name("recent_test.json"))}

    assert str(proj.resolve()) in prod_paths and str(proj.resolve()) not in test_paths
    assert str(sandbox.resolve()) in test_paths and str(sandbox.resolve()) not in prod_paths


# --------------------------------------------------------------- read side
def test_load_recent_prunes_vanished_dirs(tmp_path):
    """A recorded path whose directory is gone is dropped on read (self-pruning)."""
    recent = tmp_path / "recent.json"
    gone = tmp_path / "deleted"   # never created
    here = tmp_path / "present"
    here.mkdir()
    recent.write_text(json.dumps([
        {"name": "deleted", "path": str(gone)},
        {"name": "present", "path": str(here)},
    ]) + "\n", encoding="utf-8")

    listed = {r["path"] for r in dash._load_recent(recent)}
    assert str(here) in listed, "an existing project must survive the read"
    assert str(gone) not in listed, "a vanished directory must be pruned on read"

"""The adoption fix (2026-06-17): recall's state lands in the instruction file every
client loads, so the AI carries the memory in its system prompt WITHOUT a tool call.
These guards pin: the block renders negation-first, it's idempotent, it never destroys
user content, and the post-commit hook re-syncs it.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from recall.engine import Index


def _seed(ix):
    """A load-bearing core + a must-know ADR (mirrors test_onboarding's setup)."""
    ix.stamp(title="run", anchors=["core", "run"], kind="code-symbol",
             file_path="src/core.ts", symbol="run", line=1, origin="bootstrap")
    ix.stamp(title="a", anchors=["a"], kind="code-symbol",
             file_path="src/a.ts", symbol="a", line=1, origin="bootstrap")
    ix.add_dependency_edges([("src/a.ts", "src/core.ts")])
    ix.stamp(title="ADR-007: the core owns the run loop",
             body="Every entry point routes through core.run so the guard runs once.",
             anchors=["adr", "core", "run"], tags=["foundation"],
             kind="lesson", file_path="docs/decisions.md", sha="dec1234", dedup=False)
    ix.rerank_importance()


@pytest.fixture
def idx():
    """In-memory index for the render-block tests."""
    ix = Index.open(":memory:")
    _seed(ix)
    return ix


@pytest.fixture
def repo(tmp_path):
    """An on-disk repo with a real .mind/index.db, so the CLI command (which opens the
    index from disk) finds it. Returns the repo path."""
    (tmp_path / ".mind").mkdir()
    ix = Index.open(tmp_path / ".mind" / "index.db", repo=tmp_path)
    _seed(ix)
    ix.close() if hasattr(ix, "close") else None
    return tmp_path


def test_state_block_is_negation_first(idx):
    """The block must contest the 'recall is search' prior — that's the whole point."""
    block = idx.render_state_block()
    assert "NOT search" in block, "the block must lead with the negation of the search prior"
    assert "0 model tokens" in block
    assert "recall brief" in block  # tells the AI the precise per-file move


def _run_sync(repo_path):
    from recall.cli import cmd_sync_context
    import types
    args = types.SimpleNamespace(path=str(repo_path), repo=None, quiet=True)
    return cmd_sync_context(args)


def test_sync_creates_agents_md_when_no_instruction_file(repo):
    rc = _run_sync(repo)
    assert rc == 0
    agents = repo / "AGENTS.md"
    assert agents.exists(), "with no instruction file present, sync must create AGENTS.md"
    assert Index.STATE_BEGIN in agents.read_text(encoding="utf-8")


def test_sync_is_idempotent_and_preserves_user_content(repo):
    claude = repo / "CLAUDE.md"
    claude.write_text("# My rules\n\nDo not break the build.\n", encoding="utf-8")

    _run_sync(repo)
    after_first = claude.read_text(encoding="utf-8")
    assert "# My rules" in after_first and "Do not break the build." in after_first
    assert after_first.count(Index.STATE_BEGIN) == 1

    _run_sync(repo)
    after_second = claude.read_text(encoding="utf-8")
    assert after_second.count(Index.STATE_BEGIN) == 1, "second sync must not duplicate the block"
    assert "# My rules" in after_second, "user content must survive re-sync"


def test_block_replaced_in_place_not_appended(repo):
    """A re-sync replaces the OLD block, so a stale block can't linger above a fresh one."""
    claude = repo / "CLAUDE.md"
    stale = f"{Index.STATE_BEGIN}\nOLD STALE STATE\n{Index.STATE_END}\n"
    claude.write_text("# Top\n\n" + stale, encoding="utf-8")
    _run_sync(repo)
    txt = claude.read_text(encoding="utf-8")
    assert "OLD STALE STATE" not in txt, "the stale block must be replaced, not left behind"
    assert txt.count(Index.STATE_BEGIN) == 1
    assert txt.index("# Top") < txt.index(Index.STATE_BEGIN), "user content stays on top"


def test_post_commit_hook_calls_sync_context():
    """The Claude-Code Stop/PostToolUse hook path must re-sync the state."""
    src = (Path(__file__).resolve().parent.parent / "adapters" / "hook.py").read_text(encoding="utf-8")
    assert "sync-context" in src, "post-commit hook must re-sync the instruction-file state"


def test_stamp_commit_resyncs_the_block():
    """The GIT post-commit path (what `recall hook --install` writes → `recall stamp-commit`)
    must re-sync the block — this is the path that reaches EVERY client, not just Claude Code.
    Pinning it: the 2026-06-17 e2e found the block only regenerated once this was wired here."""
    src = (Path(__file__).resolve().parent.parent / "recall" / "cli.py").read_text(encoding="utf-8")
    # cmd_stamp_commit must invoke the sync path
    body = src[src.index("def cmd_stamp_commit"):src.index("def cmd_hook")]
    assert "cmd_sync_context" in body, (
        "cmd_stamp_commit (the git-hook entry point) must re-sync the state block — "
        "else a normal git user's instruction file never regenerates"
    )

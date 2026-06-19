"""Drift-guards for STEP 5 — hotspot choice + token estimate (offline, ADR-008).

The two guarantees this layer makes:
  - the estimate is computed with ZERO completion calls (the EchoProvider records
    every complete(), and we assert it stayed empty) — the ADR-008 "show the cost
    BEFORE spending a token" mandate, proven structurally;
  - hotspot selection is token-free + deterministic: ranked by churn × density,
    scope filters by path prefix, top_n caps the budget.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from recall import Index
from recall.llm import EchoProvider
from recall.power import (
    DEFAULT_FILE_BYTE_CAP,
    estimate_tokens,
    select_hotspots,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path):
    """A repo where auth.py churns 3× and util.py once — so churn ranking is testable."""
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "auth.py").write_text("def login(): return 1\n", encoding="utf-8")
    (repo / "src" / "util.py").write_text("def helper(): return 2\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    for i in range(2):  # two more commits touch auth.py only -> churn 3 vs 1
        (repo / "src" / "auth.py").write_text(f"def login(): return {i}\n", encoding="utf-8")
        _git(repo, "commit", "-aqm", f"auth change {i}")
    return repo


def _index_with_symbols(repo) -> Index:
    """Stamp bootstrap code-symbol nodes for both files, as `recall init` would."""
    idx = Index.open(":memory:", repo=str(repo))
    idx.stamp("login", anchors=["login"], kind="code-symbol",
              file_path="src/auth.py", origin="bootstrap")
    idx.stamp("auth lesson", anchors=["auth", "session"], kind="lesson",
              file_path="src/auth.py", body="why auth matters", origin="bootstrap")
    idx.stamp("helper", anchors=["helper"], kind="code-symbol",
              file_path="src/util.py", origin="bootstrap")
    return idx


@needs_git
def test_hotspots_ranked_by_churn(tmp_path):
    repo = _repo(tmp_path)
    idx = _index_with_symbols(repo)
    hs = select_hotspots(idx, repo)
    assert [h.file_path for h in hs] == ["src/auth.py", "src/util.py"]  # auth churns more
    assert hs[0].churn == 3 and hs[1].churn == 1
    assert hs[0].symbol_count == 2  # 1 code-symbol + 1 lesson pinned to auth.py


@needs_git
def test_scope_filters_by_path_prefix(tmp_path):
    repo = _repo(tmp_path)
    idx = _index_with_symbols(repo)
    # add a file outside src/ to prove scope excludes it
    (repo / "root.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "root")
    idx.stamp("root sym", anchors=["root"], kind="code-symbol",
              file_path="root.py", origin="bootstrap")
    hs = select_hotspots(idx, repo, scope="src/auth")
    assert [h.file_path for h in hs] == ["src/auth.py"]


@needs_git
def test_top_n_caps_the_budget(tmp_path):
    repo = _repo(tmp_path)
    idx = _index_with_symbols(repo)
    hs = select_hotspots(idx, repo, top_n=1)
    assert len(hs) == 1 and hs[0].file_path == "src/auth.py"


@needs_git
def test_estimate_spends_zero_completion_calls(tmp_path):
    """The core ADR-008 guard: the cost preview must not call the model."""
    repo = _repo(tmp_path)
    idx = _index_with_symbols(repo)
    hs = select_hotspots(idx, repo)
    echo = EchoProvider(model="echo")

    est = estimate_tokens(idx, repo, hs, echo)

    assert echo.complete_calls == []  # NOT ONE completion was made
    assert est.hotspots == 2
    assert est.input_tokens > 0
    # bug-hunt MEDIUM (2026-06-17): the preview prices the REAL ceiling the run uses
    # (max_tokens = output_budget × OUTPUT_CAP_MULTIPLIER), not just the expected budget,
    # so --yes approves a true upper bound. 2 hotspots × 400 budget × 2 cap = 1600.
    assert est.est_output_tokens == 2 * 400 * 2
    assert est.est_cost_usd == 0.0  # echo is free (like ollama)
    assert len(est.per_file) == 2


@needs_git
def test_estimate_cost_nonzero_for_paid_provider(tmp_path):
    """A paid provider reports a paid rate -> the estimate shows a cost. Uses the
    Anthropic provider's OWN cost_per_token; estimate only calls count_tokens (a
    heuristic), never complete() — so this stays fully offline, no SDK, no key."""
    repo = _repo(tmp_path)
    idx = _index_with_symbols(repo)
    hs = select_hotspots(idx, repo)
    from recall.llm import AnthropicProvider

    paid = AnthropicProvider(model="claude-opus-4-8")
    est = estimate_tokens(idx, repo, hs, paid)
    assert est.est_cost_usd > 0.0


@needs_git
def test_local_provider_costs_exactly_zero(tmp_path):
    """The flip side: a local/free provider must cost exactly 0, even if its model
    name happens to collide with a paid one — the provider's rate is authoritative."""
    repo = _repo(tmp_path)
    idx = _index_with_symbols(repo)
    hs = select_hotspots(idx, repo)
    from recall.llm import OllamaProvider

    free = OllamaProvider(model="claude-opus-4-8")  # absurd name on a free local provider
    est = estimate_tokens(idx, repo, hs, free)
    assert est.est_cost_usd == 0.0  # free is free, name be damned


@needs_git
def test_source_is_capped_to_byte_budget(tmp_path):
    repo = _repo(tmp_path)
    # a huge file must not blow the estimate — cap applies
    big = "x = 1\n" * 5000  # ~30 KB
    (repo / "src" / "auth.py").write_text(big, encoding="utf-8")
    idx = _index_with_symbols(repo)
    hs = select_hotspots(idx, repo)
    echo = EchoProvider(model="echo")
    est = estimate_tokens(idx, repo, hs, echo, file_byte_cap=1024)
    # auth.py's contribution is bounded by the 1KB cap, not its 30KB on disk. The count
    # is (prompt boilerplate + capped source); the 30KB file uncapped would be ~5000
    # word-tokens, so anything well under that proves the cap held. The prompt scaffold
    # (schema contract + recency tail) is a fixed ~hundreds of tokens on top of the cap.
    auth_tokens = dict(est.per_file)["src/auth.py"]
    assert auth_tokens < 1500  # capped source + fixed scaffold; nowhere near the 5000 uncapped

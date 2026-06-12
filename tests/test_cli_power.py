"""Drift-guards for STEP 7 — the Power-Mode CLI surface (offline).

connect / power / undo / forget through main(). Two hard rules pinned:
  - `recall power` with NO connection does NOTHING but point at `recall connect`
    (ADR-012), and WITH a connection shows the estimate and STOPS unless --yes (ADR-008);
  - connect never touches the user's real ~/.recall (we redirect CONNECT_PATH to tmp).

The connection + provider are faked so the whole test stays offline (EchoProvider).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import recall.connect as connect_mod
from recall import cli
from recall.engine import Index
from recall.llm import EchoProvider

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


@pytest.fixture
def isolated_connect(tmp_path, monkeypatch):
    """Redirect the connection file to tmp so tests never read/write the real one."""
    p = tmp_path / "connect.json"
    monkeypatch.setattr(connect_mod, "CONNECT_PATH", p)
    return p


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo_with_index(tmp_path) -> Path:
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "auth.py").write_text("def login(): return 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    # build a real .mind/index.db the CLI will open
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    idx.stamp("login", anchors=["login"], kind="code-symbol",
              file_path="src/auth.py", origin="bootstrap")
    idx.db.commit()
    idx.db.close()
    return repo


# ----------------------------------------------------------------- connect
def test_connect_set_show_clear(isolated_connect, capsys):
    assert cli.main(["connect", "--provider", "ollama", "--model", "llama3"]) == 0
    assert "connected" in capsys.readouterr().out.lower()

    assert cli.main(["connect", "--show"]) == 0
    out = capsys.readouterr().out
    assert "ollama" in out and "llama3" in out

    assert cli.main(["connect", "--clear"]) == 0
    assert "disconnect" in capsys.readouterr().out.lower()
    assert not isolated_connect.exists()


def test_connect_anthropic_stores_only_key_env_name(isolated_connect, capsys):
    cli.main(["connect", "--provider", "anthropic", "--model", "claude-opus-4-8"])
    raw = json.loads(isolated_connect.read_text(encoding="utf-8"))
    assert raw["api_key_env"] == "ANTHROPIC_API_KEY"  # name only, never a key
    assert all("key" not in k.lower() or k == "api_key_env" for k in raw)


# ----------------------------------------------------------------- power gate
@needs_git
def test_power_without_connection_does_nothing(isolated_connect, tmp_path, capsys):
    repo = _repo_with_index(tmp_path)
    rc = cli.main(["power", "--repo", str(repo)])
    assert rc == 1  # refuses
    out = capsys.readouterr().out.lower()
    assert "not connected" in out and "recall connect" in out


@needs_git
def test_power_with_connection_shows_estimate_and_stops(isolated_connect, tmp_path, monkeypatch, capsys):
    repo = _repo_with_index(tmp_path)
    cli.main(["connect", "--provider", "ollama", "--model", "llama3"])
    capsys.readouterr()
    # fake the provider so no real model is hit
    monkeypatch.setattr("recall.llm.get_provider", lambda conn=None: EchoProvider(model="llama3"))

    rc = cli.main(["power", "--repo", str(repo)])  # no --yes
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "estimate" in out and "--yes" in out  # showed cost, told how to run, DID NOT run
    # nothing was stamped — still no power nodes
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    assert idx.db.execute("SELECT COUNT(*) FROM nodes WHERE origin='power'").fetchone()[0] == 0


@needs_git
def test_power_yes_runs_and_undo_reverses(isolated_connect, tmp_path, monkeypatch, capsys):
    repo = _repo_with_index(tmp_path)
    cli.main(["connect", "--provider", "ollama", "--model", "llama3"])
    capsys.readouterr()
    reply = json.dumps({"nodes": [{"title": "auth insight", "why": "w", "anchors": ["authn"]}]})
    monkeypatch.setattr(
        "recall.llm.get_provider",
        lambda conn=None: EchoProvider(model="llama3", responses=[reply]),
    )

    assert cli.main(["power", "--repo", str(repo), "--yes"]) == 0
    out = capsys.readouterr().out.lower()
    assert "power run #1" in out

    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    assert idx.db.execute("SELECT COUNT(*) FROM nodes WHERE origin='power'").fetchone()[0] == 1
    idx.db.close()

    # list shows the run
    assert cli.main(["power", "--repo", str(repo), "--list"]) == 0
    assert "#1" in capsys.readouterr().out

    # undo reverses it
    assert cli.main(["undo", "--power-run", "1", "--repo", str(repo)]) == 0
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    assert idx.db.execute("SELECT COUNT(*) FROM nodes WHERE origin='power'").fetchone()[0] == 0


@needs_git
def test_power_dry_run_does_not_persist(isolated_connect, tmp_path, monkeypatch, capsys):
    repo = _repo_with_index(tmp_path)
    cli.main(["connect", "--provider", "ollama", "--model", "llama3"])
    capsys.readouterr()
    reply = json.dumps({"nodes": [{"title": "x", "why": "w", "anchors": ["xx"]}]})
    monkeypatch.setattr(
        "recall.llm.get_provider",
        lambda conn=None: EchoProvider(model="llama3", responses=[reply]),
    )

    assert cli.main(["power", "--repo", str(repo), "--dry-run"]) == 0
    assert "dry-run" in capsys.readouterr().out.lower()
    # the real index gained nothing
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    assert idx.db.execute("SELECT COUNT(*) FROM nodes WHERE origin='power'").fetchone()[0] == 0
    assert idx.power_run_info(1) is None  # no run recorded


# ----------------------------------------------------------------- forget
@needs_git
def test_forget_refuses_bootstrap_without_force(tmp_path, capsys):
    repo = _repo_with_index(tmp_path)
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    nid = idx.db.execute("SELECT id FROM nodes WHERE origin='bootstrap' LIMIT 1").fetchone()[0]
    idx.db.close()

    assert cli.main(["forget", str(nid), "--repo", str(repo)]) == 1  # refused
    assert "sacred" in capsys.readouterr().out.lower()

    assert cli.main(["forget", str(nid), "--force", "--repo", str(repo)]) == 0  # forced
    assert "forgot" in capsys.readouterr().out.lower()

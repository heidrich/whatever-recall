"""M3 / Workstream C (2026-06-18) — the money-receipt, COUNTS-ONLY.

Turn access_log + the new pre-edit ack event into a per-session receipt in HONEST MEASURED units.
M3 ships counts-only: NO token/$ figure — that may live ONLY under a future receipt['modeled']
(the measured/modeled wall). These tests pin the counts, the wall, the interactive-only filter,
the ack instrumentation (one row per cmd_ack, no short-circuit), the DB-free gate fast-path, the
read-only contract, and the deliberate no-receipt-in-MCP non-change.
"""

import inspect
import subprocess
import time
from types import SimpleNamespace

import pytest

from recall import editgate
from recall.engine import Index
from recall.cli import _index_path, cmd_ack, cmd_receipt


def _mem(tmp_path):
    return Index.open(":memory:", repo=tmp_path)


def _seed(idx):
    """Seed a mix of read-path, ack, and machine rows."""
    idx._log("auth.py", None, 0.0, 1, 0, "ack", kind="ack")
    idx._log("cli.py", None, 0.0, 1, 0, "ack", kind="ack")
    idx._log("login query", 1, 0.5, 1, 0, "cli", kind="recall")
    idx._log("seat query", 1, 0.5, 1, 0, "hook", kind="recall")     # 'hook' is KEPT
    idx._log("miss query", None, 0.0, 0, 0, "cli", kind="recall")   # surfaced=0
    idx._log("auth.py", None, 0.0, 1, 0, "cli", kind="brief")
    idx._log("HEAD", None, 0.0, 1, 0, "commit", kind="stamp")       # EXCLUDED (machine)
    idx._log("explain", None, 0.0, 1, 0, "state", kind="explain")   # EXCLUDED (state block)
    idx.db.commit()


# ---------------------------------------------------------------- 1. measured counts are exact
def test_receipt_measured_counts_are_exact(tmp_path):
    idx = _mem(tmp_path)
    _seed(idx)
    m = idx.receipt()["measured"]
    assert m["briefed_edits"] == 2
    assert m["distinct_files_briefed"] == 2                          # auth.py + cli.py (from query string)
    assert m["recall_calls"] == 4                                    # 3 recall + 1 brief
    assert m["surfaced_calls"] == 3                                  # 2 recall-hit + 1 brief (the miss excluded)
    assert m["per_kind"] == {"ack": 2, "recall": 3, "brief": 1}     # commit/state filtered out
    assert m["total_events"] == 6                                    # the 2 machine rows excluded


# --------------------------------------------------------- 2. counts-only: no modeled block ships
def test_receipt_ships_no_modeled_block(tmp_path):
    idx = _mem(tmp_path)
    _seed(idx)
    r = idx.receipt()
    assert "modeled" not in r                                        # the wall: no token/$ figure in M3
    # and no $/token-looking key leaks into measured
    flat = str(r["measured"]).lower()
    assert "$" not in flat and "token" not in flat


# ------------------------------------------------ 3. interactive_only filter (commit/state out, hook in)
def test_interactive_only_excludes_machine_keeps_hook(tmp_path):
    idx = _mem(tmp_path)
    _seed(idx)
    assert idx.receipt(interactive_only=True)["measured"]["total_events"] == 6
    assert idx.receipt(interactive_only=False)["measured"]["total_events"] == 8
    # the kept 'hook' recall is counted in interactive mode
    assert idx.receipt()["measured"]["per_kind"].get("recall") == 3


# ---------------------------------------------------- 4. ack instrumentation (one row, no short-circuit)
def _git_repo_with_index(tmp_path):
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git unavailable")
    repo = tmp_path / "proj"
    repo.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@t.t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    (repo / "auth.py").write_text("def login():\n    return 1\n", encoding="utf-8")
    idx = Index.open(_index_path(repo), repo=repo)
    idx.stamp(title="login note", anchors=["login"], file_path="auth.py", kind="lesson", body="why")
    idx.db.close()
    return repo


def test_cmd_ack_logs_one_event_and_writes_the_sidecar(tmp_path):
    repo = _git_repo_with_index(tmp_path)
    assert cmd_ack(SimpleNamespace(repo=str(repo), file="auth.py")) == 0
    idx = Index.open(_index_path(repo), repo=repo)
    acks = idx.db.execute("SELECT COUNT(*) FROM access_log WHERE kind='ack'").fetchone()[0]
    idx.db.close()
    assert acks == 1
    # the editgate sidecar JSON was updated (the gate will now let an edit through)
    assert editgate.is_acked(_index_path(repo).parent, repo, "auth.py")


def test_cmd_ack_has_no_short_circuit_second_ack_logs_again(tmp_path):
    repo = _git_repo_with_index(tmp_path)
    cmd_ack(SimpleNamespace(repo=str(repo), file="auth.py"))
    cmd_ack(SimpleNamespace(repo=str(repo), file="auth.py"))          # within TTL — still logs
    idx = Index.open(_index_path(repo), repo=repo)
    acks = idx.db.execute("SELECT COUNT(*) FROM access_log WHERE kind='ack'").fetchone()[0]
    idx.db.close()
    assert acks == 2


# ------------------------------------------------------- 5. the gate fast-path (is_acked) is DB-free
def test_is_acked_is_db_free(tmp_path):
    """The gate's fast-path reads ONLY the sidecar JSON — it must work with NO index DB present
    (so the hot pre-edit gate never pays an index open). C's instrumentation lives in cmd_ack."""
    mind = tmp_path / ".mind"
    mind.mkdir()
    repo = tmp_path
    assert not (mind / "index.db").exists()
    editgate.ack(mind, repo, "auth.py")                              # writes only edit_acks.json
    assert (mind / "edit_acks.json").exists()
    assert not (mind / "index.db").exists()                          # ack created NO index db
    assert editgate.is_acked(mind, repo, "auth.py") is True          # reads JSON, no db needed


# ------------------------------------------------------------------ 6. receipt() is read-only
def test_receipt_is_read_only(tmp_path):
    idx = _mem(tmp_path)
    _seed(idx)
    def counts():
        return tuple(idx.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ("nodes", "edges", "meta", "access_log"))
    before = counts()
    idx.receipt()
    idx.receipt(interactive_only=False)
    assert counts() == before                                        # pure SELECT — writes nothing


# ---------------------------------------- 7. deliberate non-change: no receipt in the MCP surface
def test_mcp_has_no_receipt_payload():
    """axis-2: the receipt is NEVER a lingering MCP payload — no tool, no prompt, no instruction."""
    import recall.mcp as M
    src = inspect.getsource(M)
    assert '"receipt"' not in src and "'receipt'" not in src
    assert "receipt" not in (M._INSTRUCTIONS.lower() if hasattr(M, "_INSTRUCTIONS") else "")

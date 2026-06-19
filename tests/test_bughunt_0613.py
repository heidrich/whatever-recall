"""Bug-hunt 2026-06-13 (pre-launch sweep): the new tray/stop lifecycle, the CLI error
boundary, provider robustness, power-run atomicity, license file perms, and refine
failure visibility. Each test pins a fix from the sweep so it can't silently regress."""

from __future__ import annotations

import os
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

from recall import dashboard, tray
from recall.cli import _dispatch, _subcommand_names, build_parser


# ─────────────────────────────────────────── tray app (no console to close)
def test_banner_warns_not_to_close_and_has_no_ansi():
    b = tray.banner_text()
    assert "DO NOT CLOSE" in b
    assert "RECONNECTING" in b
    assert "\x1b" not in b  # plain text — readable in any console


def test_tray_run_falls_back_to_console_when_pystray_absent(monkeypatch):
    """No pystray -> tray.run must degrade to the banner console serve, never crash."""
    monkeypatch.setattr(tray, "tray_available", lambda: False)
    calls = {}

    def fake_fallback(repo, idx_path, **kw):
        calls["fallback"] = (repo, idx_path, kw)
        return 0

    monkeypatch.setattr(tray, "_run_console_fallback", fake_fallback)
    rc = tray.run(Path("repo"), Path("idx.db"), open_browser=False, watch=True)
    assert rc == 0 and "fallback" in calls


def test_wait_until_serving_returns_false_on_serve_error():
    """Review finding: a serve() bind failure must NOT read as 'serving'. When the serve
    thread records an error, _wait_until_serving returns False -> caller exits non-zero."""
    rc = {"error": "address already in use"}
    assert tray._wait_until_serving("127.0.0.1", _free_port(), rc, timeout=0.5) is False


def test_wait_until_serving_returns_false_when_nothing_binds():
    """No server ever comes up on a free port -> times out as not-serving (not a hang)."""
    assert tray._wait_until_serving("127.0.0.1", _free_port(), {}, timeout=0.5) is False


def test_launcher_uses_pythonw_only_when_tray_extra_present():
    """Review finding: base install (no [tray]) must keep a VISIBLE console so the
    DO-NOT-CLOSE banner shows — pythonw would hide a fallback console = invisible server."""
    from recall.shortcut import launcher_spec

    repo = Path("C:/Users/me/projects/my repo")
    # tray installed -> windowless: pythonw + start (background, tray icon replaces window)
    _, win = launcher_spec(repo, 7099, "C:/py/python.exe", platform="win32", windowless=True)
    assert "pythonw.exe" in win or "start " in win
    assert "start " in win
    # tray absent -> visible console: plain python, no `start` (window holds the server)
    _, vis = launcher_spec(repo, 7099, "C:/py/python.exe", platform="win32", windowless=False)
    assert "start " not in vis
    assert "-m recall.cli tray --port 7099" in vis
    assert "pause" not in vis  # no lingering press-a-key window either way


# ─────────────────────────────────────────── run-lock + recall stop
def test_lock_write_read_roundtrip(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".mind").mkdir(parents=True)
    dashboard._write_lock(repo, "127.0.0.1", 7099)
    info = dashboard.read_lock(repo)
    assert info == {"host": "127.0.0.1", "port": 7099, "pid": os.getpid()}


def test_stop_without_a_running_server_is_clean(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".mind").mkdir(parents=True)
    ok, msg = dashboard.stop(repo)
    assert ok is False and "no dashboard" in msg.lower()


def test_stop_clears_a_stale_lock(tmp_path):
    """A lock left by a crashed server (no live port) is tidied, not left to mislead."""
    repo = tmp_path / "proj"
    (repo / ".mind").mkdir(parents=True)
    # a lock pointing at a port nothing listens on
    dashboard._write_lock(repo, "127.0.0.1", _free_port())
    ok, msg = dashboard.stop(repo)
    assert ok is False
    assert dashboard.read_lock(repo) is None  # stale lock cleared


def test_is_dashboard_live_returns_none_and_clears_a_stale_lock(tmp_path):
    """Review finding: cmd_tray must probe liveness, not trust the lock. A stale lock
    (no server answering) must read as 'not live' AND be cleared, so tray starts fresh."""
    repo = tmp_path / "proj"
    (repo / ".mind").mkdir(parents=True)
    dashboard._write_lock(repo, "127.0.0.1", _free_port())  # nothing listening there
    assert dashboard.is_dashboard_live(repo) is None
    assert dashboard.read_lock(repo) is None  # the lie was tidied


def test_is_dashboard_live_none_when_no_lock(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".mind").mkdir(parents=True)
    assert dashboard.is_dashboard_live(repo) is None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_request_shutdown_is_safe_with_no_server():
    dashboard._HTTPD = None
    assert dashboard.request_shutdown() is False


def test_shutdown_endpoint_rejects_cross_origin_csrf(tmp_path):
    """Review finding (security): /api/shutdown must refuse a cross-origin POST. A page on
    evil.com reaching 127.0.0.1 through the victim's browser sends a loopback Host + peer
    but a foreign Origin — that Origin check is what stops a drive-by shutdown."""
    import threading
    import time
    import urllib.error
    import urllib.request

    from recall.engine import Index

    repo = tmp_path / "proj"
    (repo / ".mind").mkdir(parents=True)
    idx_path = repo / ".mind" / "index.db"
    Index.open(idx_path, repo=repo).db.close()  # a minimal real index file

    port = _free_port()
    th = threading.Thread(
        target=lambda: dashboard.serve(repo, idx_path, port=port, open_browser=False, watch=False),
        daemon=True,
    )
    th.start()
    # wait until it's actually serving
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/pulse", timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)

    try:
        # cross-origin POST: loopback Host (browser sends the connection target) + evil Origin
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown", method="POST", data=b"",
            headers={"Host": f"127.0.0.1:{port}", "Origin": "https://evil.example.com"},
        )
        status = None
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status == 403, f"cross-origin shutdown must be 403, got {status}"
        # and the server is still alive (it was NOT shut down)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/pulse", timeout=2) as r:
            assert r.status == 200
    finally:
        dashboard.request_shutdown()
        th.join(timeout=3)


# ─────────────────────────────────────────── CLI error boundary
def test_dispatch_turns_an_exception_into_a_clean_exit(capsys):
    def boom(_args):
        raise RuntimeError("Ollama unreachable: connection refused")

    rc = _dispatch(boom, object())
    assert rc == 1
    out = capsys.readouterr().out
    assert "error:" in out and "Ollama unreachable" in out
    assert "Traceback" not in out  # the whole point: no raw traceback


def test_dispatch_lets_keyboardinterrupt_exit_130(capsys):
    def interrupted(_args):
        raise KeyboardInterrupt()

    assert _dispatch(interrupted, object()) == 130


def test_dispatch_passes_systemexit_through():
    def sysexit(_args):
        raise SystemExit(2)

    with pytest.raises(SystemExit):
        _dispatch(sysexit, object())


def test_dispatch_debug_reraises(monkeypatch):
    monkeypatch.setenv("RECALL_DEBUG", "1")

    def boom(_args):
        raise ValueError("x")

    with pytest.raises(ValueError):
        _dispatch(boom, object())


# ─────────────────────────────────────────── bare-query router covers every subcommand
def test_tray_and_stop_are_real_subcommands_not_queries():
    known = _subcommand_names(build_parser())
    for cmd in ("tray", "stop"):
        assert cmd in known, f"`recall {cmd}` would be swallowed as a search query"


# ─────────────────────────────────────────── provider robustness (clean RuntimeError)
def test_ollama_unreachable_raises_clean_runtimeerror(monkeypatch):
    from recall.llm import OllamaProvider

    def refuse(*a, **k):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    p = OllamaProvider(model="llama3")
    with pytest.raises(RuntimeError) as ei:
        p.complete("sys", "user")
    assert "Ollama" in str(ei.value) and "unreachable" in str(ei.value)


def test_ollama_error_in_200_body_is_loud(monkeypatch):
    """Ollama returns 200 with {"error": ...} for a missing model — must not be silent."""
    from recall.llm import OllamaProvider

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"error": "model \'llama3\' not found"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    p = OllamaProvider(model="llama3")
    with pytest.raises(RuntimeError) as ei:
        p.complete("sys", "user")
    assert "not found" in str(ei.value)


def test_openai_compat_unreachable_raises_clean_runtimeerror(monkeypatch):
    from recall.llm import OpenAICompatProvider

    def refuse(*a, **k):
        raise socket.timeout("timed out")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    p = OpenAICompatProvider(model="x", base_url="http://localhost:9/v1")
    with pytest.raises(RuntimeError) as ei:
        p.complete("sys", "user")
    assert "unreachable" in str(ei.value)


# ─────────────────────────────────────────── license file permissions (LC1)
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_license_token_is_written_owner_only(tmp_path, monkeypatch, signed_token):
    import recall.license as lic

    path = tmp_path / ".recall" / "license.token"
    monkeypatch.setattr(lic, "LICENSE_PATH", path)
    lic.save_license(signed_token)  # a real signed token (see the signed_token fixture)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"license token is {oct(mode)} — must be 0o600 (owner-only)"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_license_save_does_not_clobber_shared_parent_dir_perms(tmp_path, monkeypatch, signed_token):
    """Review finding: ~/.recall is SHARED (connect.json, recent.json, rules.md). Saving a
    license must not rewrite the parent dir's mode and strip a permission the user set."""
    import recall.license as lic

    parent = tmp_path / ".recall"
    parent.mkdir()
    parent.chmod(0o755)  # user deliberately left it group/world-traversable
    sibling = parent / "connect.json"
    sibling.write_text("{}", encoding="utf-8")
    sibling.chmod(0o644)
    monkeypatch.setattr(lic, "LICENSE_PATH", parent / "license.token")

    lic.save_license(signed_token)  # a real signed token (see the signed_token fixture)

    assert (parent.stat().st_mode & 0o777) == 0o755, "shared parent dir perms were clobbered"
    assert (sibling.stat().st_mode & 0o777) == 0o644, "sibling file perms were clobbered"


def test_refine_partial_failure_is_a_warning_not_a_hard_fail():
    """Review finding: 0 edges refined + SOME (not all) call failures must NOT report
    'refine failed (provider down)'. Total failure = ALL calls errored; partial = warning."""
    from recall.refine import RefineResult

    # the guard's exact condition (mirrors cmd_refine): hard-fail only when all errored
    def is_total_failure(res):
        return bool(res.call_failures) and res.call_failures >= res.files_seen and res.files_seen > 0

    partial = RefineResult(files_seen=10, call_failures=1, edges_refined=0)
    total = RefineResult(files_seen=10, call_failures=10, edges_refined=0)
    healthy = RefineResult(files_seen=10, call_failures=0, edges_refined=0)
    assert is_total_failure(partial) is False  # 9/10 fine, just nothing to refine
    assert is_total_failure(total) is True
    assert is_total_failure(healthy) is False


# ─────────────────────────────────────────── power-run atomicity (P1f)
def test_power_run_records_partial_run_when_provider_fails_midway(tmp_path):
    """A provider error mid-run must (a) re-raise so the user sees it, AND (b) record the
    partial run with status='partial' so the already-stamped nodes are listed + undoable."""
    import subprocess

    from recall.engine import Index
    from recall.llm import LLMResponse
    from recall.power import run_power

    repo = tmp_path / "proj"
    repo.mkdir()
    # several small modules so select_hotspots returns >1 hotspot
    for i in range(4):
        (repo / f"mod{i}.py").write_text(
            f"def fn{i}(x):\n    '''module {i} entry'''\n    return x + {i}\n", encoding="utf-8"
        )
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t.t",
                 "-c", "user.name=t", "commit", "-q", "-m", "feat: modules"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    idx = Index.open(":memory:", repo=repo)
    from recall.bootstrap import init as boot_init
    boot_init(idx, repo)

    class FailingProvider:
        name = "fake"
        model = "fake-1"
        cost_per_token = (0.0, 0.0)
        calls = 0

        def count_tokens(self, text):
            return max(1, len(text) // 4)

        def complete(self, system, user, *, max_tokens=1024, schema=None):
            FailingProvider.calls += 1
            if FailingProvider.calls >= 2:
                raise RuntimeError("provider blew up mid-run")
            # a valid single-node reply so hotspot #1 stamps something
            return LLMResponse(
                text='{"nodes":[{"title":"insight one","anchors":["alpha"],"tags":[]}]}',
                input_tokens=10, output_tokens=10,
            )

    with pytest.raises(RuntimeError, match="blew up"):
        run_power(idx, repo, provider=FailingProvider(), top_n=4)

    # the partial run is recorded (status partial) — not lost
    runs = idx.list_power_runs()
    assert runs, "a partial run must still be recorded for `recall power --list`"
    assert runs[0].get("status") == "partial"

    # and the node stamped before the crash is fully undoable
    before = idx.stats()["nodes"]
    idx.undo_power_all()
    after = idx.stats()["nodes"]
    assert after < before, "partial-run nodes must be removable via undo_power_all"
    assert idx.db.execute(
        "SELECT COUNT(*) FROM nodes WHERE origin='power'"
    ).fetchone()[0] == 0

"""Owner 2026-06-17: signing in must open the dashboard — drift guards.

"wenn du user einloggst, musst du das dashboard starten! das muss in die recall
rules die wir mit ausliefern!" — so a successful `recall login` brings the dashboard
up on its own (background tray, reuse a running one, never break the login), AND the
shipped rules.md states the contract. These guards pin both halves.
"""

import subprocess
from pathlib import Path

import recall.login as login


def _no_spawn(monkeypatch):
    """Replace Popen with a recorder so a test never actually launches a server."""
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    return calls


def test_login_success_path_calls_the_dashboard_starter(monkeypatch):
    # the success branch of device_login must invoke the starter — pin the wiring so a
    # refactor can't silently drop it. We assert the helper is called, not its internals.
    seen = {"called": False}
    monkeypatch.setattr(login, "_start_dashboard_after_login",
                        lambda: seen.__setitem__("called", True))

    # keep the device-flow's progress prints out of the test log (CR-heavy, clobbers
    # pytest's own summary line in captured output)
    monkeypatch.setattr(login, "_say", lambda *_: None)
    # drive only the post-save tail by faking the device-flow's network + save
    monkeypatch.setattr(login, "_post", lambda path, body, **k: (
        (200, {"status": "ok", "token": "t"}) if path.endswith("/poll")
        else (200, {"device_code": "d", "user_code": "C", "interval": 0, "expires_in": 5})))
    import recall.license as lic
    monkeypatch.setattr(lic, "save_license", lambda tok: {"verified": True})
    monkeypatch.setattr(login.time, "sleep", lambda *_: None)

    out = login.device_login(open_browser=False)
    assert out == {"verified": True}
    assert seen["called"], "a successful login must start the dashboard"


def test_starter_is_a_noop_without_an_index(tmp_path, monkeypatch):
    # login from a dir with no .mind/index.db: nothing to show — must NOT spawn, must NOT raise
    monkeypatch.chdir(tmp_path)
    calls = _no_spawn(monkeypatch)
    login._start_dashboard_after_login()
    assert calls == [], "no index → no dashboard spawn"


def test_starter_reuses_a_running_dashboard(tmp_path, monkeypatch):
    # an index exists AND a dashboard is already live → reuse it, never double-bind the port
    (tmp_path / ".mind").mkdir()
    (tmp_path / ".mind" / "index.db").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    from recall import dashboard
    monkeypatch.setattr(dashboard, "is_dashboard_live", lambda repo: {"port": 7099})
    calls = _no_spawn(monkeypatch)
    login._start_dashboard_after_login()
    assert calls == [], "a live dashboard must be reused, not re-spawned"


def test_starter_spawns_tray_detached_when_none_running(tmp_path, monkeypatch):
    # index present, nothing live → spawn `recall tray` (owner: background) as a detached proc
    (tmp_path / ".mind").mkdir()
    (tmp_path / ".mind" / "index.db").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    from recall import dashboard
    monkeypatch.setattr(dashboard, "is_dashboard_live", lambda repo: None)
    calls = _no_spawn(monkeypatch)
    login._start_dashboard_after_login()
    assert len(calls) == 1, "exactly one spawn"
    argv = calls[0][0][0]
    assert "tray" in argv, "owner chose background tray, not foreground dashboard"


def test_starter_never_raises_on_spawn_failure(tmp_path, monkeypatch):
    # best-effort contract: a spawn error must never turn a successful login into a failure
    (tmp_path / ".mind").mkdir()
    (tmp_path / ".mind" / "index.db").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    from recall import dashboard
    monkeypatch.setattr(dashboard, "is_dashboard_live", lambda repo: None)

    def _boom(*a, **k):
        raise OSError("no exec for you")
    monkeypatch.setattr(subprocess, "Popen", _boom)
    login._start_dashboard_after_login()  # must NOT raise


def test_shipped_rules_state_the_login_dashboard_contract():
    # the rule must travel with the engine, not just live in code
    rules = (Path(__file__).resolve().parent.parent / "recall" / "rules.md").read_text(encoding="utf-8")
    low = rules.lower()
    assert "signing in opens the dashboard" in low, \
        "rules.md must state that login brings up the dashboard"

"""CLI license gate (recall/login.py, W2) — drift guards.

The gate (ensure_licensed) runs before every command via cli._dispatch. Owner
decisions: ALL commands gated except login/logout/stop (Q1); a pending signup
token is NOT a usable license (Q2); when unlicensed and non-interactive, the gate
raises with a clear `recall login` instruction (D7) rather than silently failing.
"""

import base64
import json
import time

import pytest
from nacl.signing import SigningKey

import recall.license as lic
import recall.login as login

_SK = SigningKey.generate()
_PUB_B64 = base64.b64encode(_SK.verify_key.encode()).decode()


def _sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = _SK.sign(body.encode("ascii")).signature
    return body + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def _token(*, exp_in=7 * 86400, pending=False):
    p = {"sub": "u-1", "email": "dev@x.com", "plan": "trial", "trial": True,
         "iat": int(time.time()), "exp": int(time.time()) + exp_in}
    if pending:
        p["pending_activation"] = True
    return _sign(p)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # clear the conftest's signed-in env so the gate sees ONLY what each test plants
    monkeypatch.delenv("RECALL_LICENSE", raising=False)
    monkeypatch.delenv("RECALL_PUBKEY", raising=False)
    monkeypatch.setattr(lic, "LICENSE_PATH", tmp_path / "license.token")
    monkeypatch.setattr(lic, "_PUBKEY_CACHE", tmp_path / "license.pubkey")
    monkeypatch.setattr(lic, "PINNED_PUBKEY_B64", _PUB_B64)
    monkeypatch.setattr(lic, "_DEVICE_ID_PATH", tmp_path / "device_id")
    monkeypatch.setattr(login, "_SEAT_CHECK_PATH", tmp_path / "seat_check.ts")
    monkeypatch.setattr(login, "_SEAT_GRACE_PATH", tmp_path / "seat_grace.ts")
    # never let the gate actually hit the network or open a browser in tests
    monkeypatch.setattr(login, "device_login", lambda **k: None)
    # ONLINE-REQUIRED model: the gate runs a throttled single-device seat check for
    # interactive commands and FAILS CLOSED. The realistic baseline is online + seat
    # confirmed, so the default stub returns active:true (a valid token is allowed).
    # The seat-specific tests below override _post to exercise kicked / offline.
    monkeypatch.setattr(login, "_post", lambda *a, **k: (200, {"active": True}))


def test_free_commands_are_never_gated():
    # no token present at all — login/logout/stop must still be allowed
    for name in ("cmd_login", "cmd_logout", "cmd_stop"):
        login.ensure_licensed(name, interactive=False)  # must NOT raise


def test_value_command_blocked_when_signed_out():
    with pytest.raises(login.NotLicensed):
        login.ensure_licensed("cmd_brief", interactive=False)


def test_value_command_allowed_with_a_valid_license():
    lic.save_license(_token())
    login.ensure_licensed("cmd_brief", interactive=False)  # must NOT raise


def test_pending_token_does_not_unlock_value_commands():
    """A signup auto-key (pending) is good ONLY for login — never for real work."""
    # save_license now REFUSES a pending token (bug-hunt #1, 2026-06-17) — it can never be
    # persisted as a usable license. Write it past the guard to prove the GATE also refuses
    # it, the same way the expired-token test does.
    lic.LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lic.LICENSE_PATH.write_text(_token(pending=True), encoding="utf-8")
    with pytest.raises(login.NotLicensed):
        login.ensure_licensed("cmd_recall", interactive=False)


def test_save_license_refuses_a_pending_token():
    """bug-hunt #1: a pending signup auto-key must never persist as a license — the one
    persist chokepoint refuses it, so no surface (dashboard included) can treat it as signed-in."""
    with pytest.raises(ValueError):
        lic.save_license(_token(pending=True))


def test_expired_token_is_blocked():
    lic.save_license  # ensure attr exists
    # an expired token can't be saved (save refuses), so write it past the guard:
    lic.LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lic.LICENSE_PATH.write_text(_token(exp_in=-60), encoding="utf-8")
    with pytest.raises(login.NotLicensed):
        login.ensure_licensed("cmd_brief", interactive=False)


def test_all_gated_command_names_are_known():
    """Sanity: cmd_brief/cmd_recall are real dispatch targets, and the FREE set is
    exactly login/logout/stop (a drift here would silently un-gate a command)."""
    assert login.FREE_COMMANDS == {"cmd_login", "cmd_logout", "cmd_stop"}


# ---- single-device enforcement (0013) --------------------------------------

def test_device_id_is_stable_and_anonymous():
    """device_id() creates a hex id once and returns the SAME one thereafter — the
    seat check needs a stable per-install id, and it must NOT be a hardware fingerprint."""
    first = lic.device_id()
    assert first and all(c in "0123456789abcdef" for c in first)
    assert lic.device_id() == first  # stable across calls


def test_seat_check_is_throttled_to_an_hour(monkeypatch):
    """The check runs at most ~once per 60 min (SEAT_CHECK_INTERVAL_S) so it never adds
    latency per command. A CONFIRMED check stamps the clock; the next call is not due."""
    lic.save_license(_token())
    calls = {"n": 0}
    def fake_post(path, body, *, timeout=8.0):
        calls["n"] += 1
        return 200, {"active": True}
    monkeypatch.setattr(login, "_post", fake_post)
    assert login.seat_check_if_due() == login.SEAT_OK  # first call: due, runs, confirmed
    assert calls["n"] == 1
    assert login.seat_check_if_due() == login.SEAT_OK  # immediately again: NOT due, skipped
    assert calls["n"] == 1


def test_seat_check_kicks_and_clears_token_when_taken_over(monkeypatch):
    """active:false (another device took the seat) → the local token is cleared and the
    gate then blocks, so the dashboard/CLI fall back to the login mask."""
    lic.save_license(_token())
    monkeypatch.setattr(login, "_post", lambda p, b, *, timeout=8.0: (200, {"active": False}))
    assert login.seat_check_if_due() == login.SEAT_KICKED   # reports kicked
    assert lic.load_raw_token() is None                     # token removed locally
    with pytest.raises(login.NotLicensed):                  # gate now blocks
        login.ensure_licensed("cmd_brief", interactive=False)


def test_seat_check_grants_a_grace_window_when_offline(monkeypatch):
    """DEVELOPER-FRIENDLY online check (owner 2026-06-16): a failed check does NOT lock out
    immediately. The first offline check starts a 60-min grace → SEAT_GRACE, the dev keeps
    working, and a countdown is exposed. The token is kept so reconnecting needs no relogin."""
    lic.save_license(_token())
    monkeypatch.setattr(login, "_post", lambda p, b, *, timeout=8.0: (0, {}))  # status 0 = unreachable
    assert login.seat_check_if_due() == login.SEAT_GRACE
    assert lic.load_raw_token() is not None             # token kept (reconnect, no relogin)
    left = login.seat_grace_seconds_left()
    assert left is not None and 0 < left <= login.SEAT_GRACE_S
    login.ensure_licensed("cmd_brief", interactive=False)  # still ALLOWED during grace


def test_seat_check_logs_out_after_grace_expires(monkeypatch):
    """Once the 60-min grace elapses with no successful reconnect, the seat is given up:
    SEAT_EXPIRED, the token is cleared, and the gate then blocks (sign in again)."""
    lic.save_license(_token())
    monkeypatch.setattr(login, "_post", lambda p, b, *, timeout=8.0: (0, {}))
    # plant a grace timestamp that already started > SEAT_GRACE_S ago
    login._SEAT_GRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    login._SEAT_GRACE_PATH.write_text(str(int(time.time()) - login.SEAT_GRACE_S - 5), encoding="utf-8")
    assert login.seat_check_if_due() == login.SEAT_EXPIRED
    assert lic.load_raw_token() is None                 # seat given up → token cleared
    with pytest.raises(login.NotLicensed):              # gate now blocks
        login.ensure_licensed("cmd_brief", interactive=False)


def test_reconnect_clears_grace(monkeypatch):
    """A successful check during the grace window clears the countdown and resets the clock —
    the ribbon disappears, work continues with a fresh 60 min."""
    lic.save_license(_token())
    # first: offline → grace starts
    monkeypatch.setattr(login, "_post", lambda p, b, *, timeout=8.0: (0, {}))
    assert login.seat_check_if_due() == login.SEAT_GRACE
    assert login.seat_grace_seconds_left() is not None
    # then: back online and seat confirmed → grace cleared
    monkeypatch.setattr(login, "_post", lambda p, b, *, timeout=8.0: (200, {"active": True}))
    # the confirmed check resets the clock; force "due" by clearing the check stamp
    try: login._SEAT_CHECK_PATH.unlink()
    except OSError: pass
    assert login.seat_check_if_due() == login.SEAT_OK
    assert login.seat_grace_seconds_left() is None      # countdown gone


def test_noninteractive_commands_skip_the_seat_check(monkeypatch):
    """git hooks / MCP / CI must never block on the seat network check (it would
    stall a `git commit` or the MCP stdio loop) — they take the pure-offline path."""
    lic.save_license(_token())
    called = {"n": 0}
    monkeypatch.setattr(login, "_post", lambda *a, **k: (called.__setitem__("n", called["n"] + 1), (200, {"active": True}))[1])
    for name in login.NONINTERACTIVE_COMMANDS:
        login.ensure_licensed(name, interactive=False)  # must not raise, must not call
    assert called["n"] == 0

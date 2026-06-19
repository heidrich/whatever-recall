"""Shared fixtures. Make the package importable without an install step."""

import base64
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A test signing keypair (W2). The whole suite runs "signed in" by default: the
# license gate (recall/login.py) blocks every value command + MCP call, so without
# a valid license almost every CLI/MCP test would fail with NotLicensed. We pin the
# test public key + drop a freshly-signed valid token into the isolated LICENSE_PATH
# so the gate passes — mirroring the normal signed-in state. Tests that exercise the
# SIGNED-OUT path (test_cli_gate, test_license) override this with their own fixtures.
from nacl.signing import SigningKey

_TEST_SK = SigningKey.generate()
_TEST_PUB_B64 = base64.b64encode(_TEST_SK.verify_key.encode()).decode()


def _signed_test_token() -> str:
    payload = {
        "sub": "test-user", "email": "test@whatever-recall.test", "plan": "trial",
        "trial": True, "iat": int(time.time()), "exp": int(time.time()) + 7 * 86400,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = _TEST_SK.sign(body.encode("ascii")).signature
    return body + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path_factory, monkeypatch):
    """No test may touch the REAL per-user state in ~/.recall.

    The owner's project switcher filled up with pytest tmp projects because
    dashboard tests called serve()/switch against the real recent.json. Every
    test gets throwaway paths instead — structurally, so no future test can
    forget to monkeypatch.

    The user-state dir is a DEDICATED tmp dir (not the per-test `tmp_path`), so a
    test that uses tmp_path as a git repo + `git add -A` can't accidentally commit
    the planted license token (W2 conftest-artifact fix).
    """
    import recall.connect as conn
    import recall.dashboard as dash
    import recall.license as lic
    import recall.login as login

    user = tmp_path_factory.mktemp("recall_user")
    monkeypatch.setattr(dash, "RECENT_PATH", user / "recent.json")
    monkeypatch.setattr(lic, "LICENSE_PATH", user / "license.token")
    monkeypatch.setattr(lic, "_PUBKEY_CACHE", user / "license.pubkey")
    # device_id() reads/writes ~/.recall/device_id, and the seat-check body
    # (`device_id=L.device_id()`) evaluates it on essentially every gated command,
    # so without this redirect the suite would read/create the DEVELOPER'S REAL
    # device-id file — exactly the per-user-state pollution this fixture promises to
    # make impossible (bug-hunt MEDIUM, 2026-06-17). Patch it onto the throwaway dir
    # alongside the other six paths so "no test touches real ~/.recall" is structural.
    monkeypatch.setattr(lic, "_DEVICE_ID_PATH", user / "device_id")
    monkeypatch.setattr(conn, "CONNECT_PATH", user / "connect.json")
    # seat-check + grace timestamps live next to the token — keep them off the real ~/.recall
    monkeypatch.setattr(login, "_SEAT_CHECK_PATH", user / "seat_check.ts")
    monkeypatch.setattr(login, "_SEAT_GRACE_PATH", user / "seat_grace.ts")

    # W2: run signed-in by default so the license gate doesn't block every CLI/MCP
    # test. Pin the test key (offline verify) + plant a valid token. Also export
    # them so SUBPROCESS tests (`recall mcp`) inherit a signed-in environment.
    monkeypatch.setattr(lic, "PINNED_PUBKEY_B64", _TEST_PUB_B64)
    lic.LICENSE_PATH.write_text(_signed_test_token(), encoding="utf-8")
    monkeypatch.setenv("RECALL_LICENSE", _signed_test_token())
    monkeypatch.setenv("RECALL_PUBKEY", _TEST_PUB_B64)

    # No test may hit the LIVE network. The license gate's single-device heartbeat
    # (seat_check_if_due → _post /api/license/check) is the one call that otherwise reaches
    # whatever-recall.com on every value command. ONLINE-REQUIRED model (owner 2026-06-16:
    # "ES GIBT KEIN OFFLINE arbeiten mehr"): the gate now FAILS CLOSED, so an "unreachable"
    # stub would refuse every command. Stub _post to a CONFIRMED seat (200 active:true) so
    # the suite's planted token is the realistic "signed in + online + seat held" baseline.
    # Tests that exercise the heartbeat itself (test_cli_gate) re-mock _post in their body.
    monkeypatch.setattr(login, "_post", lambda *a, **k: (200, {"active": True}))

    # Make the no-live-network guarantee STRUCTURAL, not just per-seam (bug-hunt LOW,
    # 2026-06-17). Stubbing login._post covers the seat heartbeat, but the dashboard
    # device-flow (recall.dashboard _do_account_login*) and the MCP pulse probe
    # (recall.mcp _probe_dashboard) call urllib.request.urlopen directly against
    # API_BASE, which DEFAULTS to https://whatever-recall.com. Point the API base at an
    # unroutable loopback so any UNstubbed outbound fails fast locally instead of
    # silently reaching production from CI. We set BOTH the env (subprocesses + any
    # fresh read) AND the already-bound module constant (license.API_BASE, read at
    # import). dashboard._do_account_login* do `from recall.license import API_BASE`
    # AT CALL TIME so they pick up the patched attribute; login._post uses L.API_BASE,
    # the same object.
    monkeypatch.setenv("RECALL_API", "http://127.0.0.1:0")
    monkeypatch.setattr(lic, "API_BASE", "http://127.0.0.1:0")


@pytest.fixture
def signed_token() -> str:
    """A properly-signed, non-expired test license token with every required claim
    (sub/email/plan/exp), minted with the same key conftest pins. Use this whenever a
    test needs save_license()/a verified token to SUCCEED: a hand-built
    `body.b64('sig')` no longer passes decode_token (it requires all of _REQUIRED_CLAIMS)
    nor verify_token (it needs a real Ed25519 signature). The autouse fixture above
    already pins the matching pubkey, so this token verifies offline."""
    return _signed_test_token()

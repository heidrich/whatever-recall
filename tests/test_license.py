"""License token storage + Ed25519 verification (recall/license.py, ADR-030 / W2).

The token shape is `base64url(json-payload) + "." + base64url(sig)`, exactly what
whatever-recall.com/api/license issues. W2: the signature is now VERIFIED with
PyNaCl. These tests sign with a real test keypair pinned via PINNED_PUBKEY_B64, so
they exercise the true verify path (forgery, tamper, pending, expiry).
"""

import base64
import json
import time

import pytest

import recall.license as lic
from recall.dashboard import _load_recent

# a real test keypair — sign like the web server does, pin its public half so the
# CLI verifies offline with no network.
from nacl.signing import SigningKey

_SK = SigningKey.generate()
_PUB_B64 = base64.b64encode(_SK.verify_key.encode()).decode()


def _sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = _SK.sign(body.encode("ascii")).signature
    return body + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def make_token(plan="trial", exp_in=14 * 86400, **extra):
    """A REAL, signed token (verifies against the pinned test key)."""
    return _sign({
        "sub": "u-1", "email": "dev@studio.com", "plan": plan,
        "trial": plan == "trial", "iat": int(time.time()),
        "exp": int(time.time()) + exp_in, **extra,
    })


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Never touch the real ~/.recall token, and pin the test verify key so no
    network is needed. Clear the conftest's env token/key so THIS file's pinned key
    + planted tokens are what's verified."""
    monkeypatch.delenv("RECALL_LICENSE", raising=False)
    monkeypatch.delenv("RECALL_PUBKEY", raising=False)
    monkeypatch.setattr(lic, "LICENSE_PATH", tmp_path / "license.token")
    monkeypatch.setattr(lic, "_PUBKEY_CACHE", tmp_path / "license.pubkey")
    monkeypatch.setattr(lic, "PINNED_PUBKEY_B64", _PUB_B64)


def test_decode_round_trip():
    p = lic.decode_token(make_token())
    assert p is not None and p["email"] == "dev@studio.com" and p["plan"] == "trial"


@pytest.mark.parametrize("bad", ["", "garbage", "a.b.c", "onlyonepart", ".", "x.", ".y"])
def test_decode_rejects_wrong_shapes(bad):
    assert lic.decode_token(bad) is None


def test_decode_rejects_missing_claims():
    payload = {"email": "x@y.z"}  # no sub/plan/exp
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    assert lic.decode_token(body + ".sig") is None


def test_verify_accepts_a_real_signature():
    state = lic.verify_token(make_token())
    assert state is not None and state["verified"] is True and state["expired"] is False


def test_verify_rejects_a_tampered_payload():
    tok = make_token()
    body, sig = tok.split(".")
    # flip a byte in the payload — signature no longer matches
    bad_body = body[:-1] + ("A" if body[-1] != "A" else "B")
    assert lic.verify_token(bad_body + "." + sig) is None


def test_verify_rejects_a_foreign_key_forgery():
    """A token signed by a DIFFERENT key (an attacker who can't reach our private
    key) must NOT verify — this is the whole point of the gate."""
    other = SigningKey.generate()
    body = base64.urlsafe_b64encode(json.dumps({
        "sub": "x", "email": "e@e.e", "plan": "studio", "trial": False,
        "iat": 1, "exp": int(time.time()) + 999999,
    }).encode()).rstrip(b"=").decode()
    sig = other.sign(body.encode("ascii")).signature
    forged = body + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    assert lic.verify_token(forged) is None


def test_pending_token_surfaces_pending_flag():
    state = lic.verify_token(make_token(pending_activation=True))
    assert state is not None and state["pending"] is True


def test_save_load_clear_round_trip():
    saved = lic.save_license(make_token())
    assert saved["verified"] is True and saved["expired"] is False
    loaded = lic.load_license()
    assert loaded is not None and loaded["email"] == "dev@studio.com"
    assert loaded["days_left"] == 14 and loaded["verified"] is True
    assert lic.clear_license() is True
    assert lic.load_license() is None
    assert lic.clear_license() is False  # nothing left to remove


def test_save_refuses_expired_garbage_and_forged():
    with pytest.raises(ValueError):
        lic.save_license(make_token(exp_in=-60))
    with pytest.raises(ValueError):
        lic.save_license("not a token")
    # a real-shaped but foreign-signed token must also be refused
    other = SigningKey.generate()
    body = base64.urlsafe_b64encode(json.dumps({
        "sub": "x", "email": "e@e.e", "plan": "studio", "trial": False,
        "iat": 1, "exp": int(time.time()) + 999999,
    }).encode()).rstrip(b"=").decode()
    forged = body + "." + base64.urlsafe_b64encode(other.sign(body.encode()).signature).rstrip(b"=").decode()
    with pytest.raises(ValueError):
        lic.save_license(forged)
    assert lic.load_license() is None  # nothing was persisted


def test_trial_clamp_days_left_counts_partial_days_up():
    p = lic.save_license(make_token(exp_in=86400 // 2))  # half a day left
    assert p["days_left"] == 1 and p["expired"] is False


def test_recent_prunes_vanished_projects(tmp_path):
    """The switcher only lists projects that still exist (owner 2026-06-11:
    pytest temp repos flooded the menu)."""
    real = tmp_path / "real-repo"
    real.mkdir()
    gone = tmp_path / "deleted-repo"  # never created
    reg = tmp_path / "recent.json"
    reg.write_text(json.dumps([
        {"name": "real-repo", "path": str(real)},
        {"name": "deleted-repo", "path": str(gone)},
    ]), encoding="utf-8")
    names = [r["name"] for r in _load_recent(reg)]
    assert names == ["real-repo"]

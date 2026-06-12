"""License token storage + decode (recall/license.py, ADR-030) — drift guards.

The token shape is `base64url(json-payload) + "." + base64url(sig)`, exactly
what whatever-recall.com/api/license issues. decode is display-only (verified
stays False until the Ed25519 gate wave) — these tests defend that honesty.
"""

import base64
import json
import time

import pytest

import recall.license as lic
from recall.dashboard import _load_recent


def make_token(plan="trial", exp_in=14 * 86400, **extra):
    payload = {
        "sub": "u-1", "email": "dev@studio.com", "plan": plan,
        "trial": plan == "trial", "iat": int(time.time()),
        "exp": int(time.time()) + exp_in, **extra,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    return body.decode() + "." + base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()


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


def test_save_load_clear_round_trip():
    saved = lic.save_license(make_token())
    assert saved["verified"] is False and saved["expired"] is False
    loaded = lic.load_license()
    assert loaded is not None and loaded["email"] == "dev@studio.com"
    assert loaded["days_left"] == 14 and loaded["verified"] is False
    assert lic.clear_license() is True
    assert lic.load_license() is None
    assert lic.clear_license() is False  # nothing left to remove


def test_save_refuses_expired_and_garbage():
    with pytest.raises(ValueError):
        lic.save_license(make_token(exp_in=-60))
    with pytest.raises(ValueError):
        lic.save_license("not a token")
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

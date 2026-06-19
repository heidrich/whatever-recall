"""License token storage + Ed25519 verification (ADR-030 / W2).

One file: `~/.recall/license.token` — the signed token issued at
whatever-recall.com/account. `recall login` and the dashboard both write the
same file (the connect.json pattern, ADR-012).

The token is `base64url(json-payload) + "." + base64url(sig)`. W2: the Ed25519
signature is now VERIFIED here with PyNaCl (a hard dependency — every gated CLI
command checks it offline). The public key is pinned (PINNED_PUBKEY_B64) and/or
fetched once from `GET <API>/api/license` and cached. Byte contract (confirmed
against the Node signer in web/src/lib/server/license.ts): the signature is over
the ASCII bytes of the base64url `body` string, and the verify key is the raw
32-byte Ed25519 key (the last 32 bytes of the SPKI DER the API returns as PEM).
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import time
import urllib.request
from pathlib import Path

LICENSE_PATH = Path.home() / ".recall" / "license.token"
_PUBKEY_CACHE = Path.home() / ".recall" / "license.pubkey"  # cached raw-32 (base64)
# Single-device enforcement (owner 2026-06-14): a stable, ANONYMOUS per-machine id —
# a random UUID, NOT a hardware fingerprint (privacy: we never want machine identity,
# only "is this the same install as last login"). Sent on `recall login` and on the
# 60-min seat check so the server can tell "same device re-logging" (keep) from "a new
# device took the seat" (the other one goes offline). One account ≠ unlimited devs.
_DEVICE_ID_PATH = Path.home() / ".recall" / "device_id"


def device_id() -> str:
    """The stable anonymous device id for this install, creating it once on first use.
    Falls back to an ephemeral id if the file can't be written (the check then simply
    can't pin this device, which fails OPEN — never blocks a legitimate user)."""
    try:
        existing = _DEVICE_ID_PATH.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    import uuid
    new_id = uuid.uuid4().hex
    try:
        _DEVICE_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_private(_DEVICE_ID_PATH, new_id)
    except OSError:
        pass
    return new_id

# The recall web API base. Overridable for dev (test against a local server).
API_BASE = os.environ.get("RECALL_API", "https://whatever-recall.com").rstrip("/")

# Pinned production verify key (raw 32-byte Ed25519, base64). Empty until the owner
# pins the prod key; when empty the CLI fetches+caches it from the API on first use.
# Pinning is strictly stronger (no trust-on-first-use); fetch is the bootstrap path.
# RECALL_PUBKEY env overrides it (headless/CI: pin without editing source).
PINNED_PUBKEY_B64 = ""

# Headless/CI: supply the license token directly via env instead of `recall login`
# (the token is still Ed25519-verified — this is a delivery channel, not a bypass).
_ENV_TOKEN = "RECALL_LICENSE"

_REQUIRED_CLAIMS = ("sub", "email", "plan", "exp")


class LicenseError(Exception):
    """Verification could not be performed (e.g. no public key reachable)."""


def decode_token(token: str) -> dict | None:
    """The payload half of the token as a dict, or None if the shape is wrong.

    NO signature check (stdlib has no Ed25519) — treat the result as display
    data, never as an authorization decision.
    """
    parts = token.strip().split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    try:
        pad = "=" * (-len(parts[0]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[0] + pad))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(payload, dict):
        return None
    if not all(k in payload for k in _REQUIRED_CLAIMS):
        return None
    # exp must be int-coercible: a hand-edited / buggy token with exp="soon" would
    # otherwise make int(exp) raise downstream in _with_state() (load_license is
    # called unguarded by the dashboard -> a 500 / dropped connection). Reject here
    # so a corrupt token reads as "signed out", not a crash.
    try:
        int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return None
    return payload


def _raw32_from_spki_pem(pem: str) -> bytes:
    """Extract the raw 32-byte Ed25519 public key from an SPKI PEM (what the API
    returns). Ed25519 SPKI = 12-byte header + the 32-byte key, so the key is the
    DER tail."""
    b64 = "".join(line for line in pem.splitlines() if "-----" not in line)
    der = base64.b64decode(b64)
    if len(der) < 32:
        raise LicenseError("malformed public key")
    return der[-32:]


def _fetch_pubkey_raw32() -> bytes:
    """Fetch the verify key from the API (GET /api/license -> {public_key: PEM}),
    cache the raw-32 base64 for offline reuse. Raises LicenseError if unreachable
    AND nothing is cached."""
    try:
        req = urllib.request.Request(f"{API_BASE}/api/license", method="GET")
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 (https URL)
            data = json.loads(r.read().decode("utf-8"))
        raw = _raw32_from_spki_pem(str(data["public_key"]))
        try:
            _PUBKEY_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _write_private(_PUBKEY_CACHE, base64.b64encode(raw).decode("ascii"))
        except OSError:
            pass
        return raw
    except Exception as e:  # network, JSON, key shape — fall back to cache
        try:
            return base64.b64decode(_PUBKEY_CACHE.read_text(encoding="utf-8").strip())
        except OSError:
            raise LicenseError(
                "could not reach whatever-recall.com to verify your license, and no "
                "verification key is cached — connect once to finish signing in"
            ) from e


def _verify_key_raw32() -> bytes:
    """The raw-32 Ed25519 verify key: RECALL_PUBKEY env > pinned prod key > fetched+cached."""
    env = os.environ.get("RECALL_PUBKEY")
    if env:
        return base64.b64decode(env)
    if PINNED_PUBKEY_B64:
        return base64.b64decode(PINNED_PUBKEY_B64)
    return _fetch_pubkey_raw32()


def verify_token(token: str) -> dict | None:
    """Cryptographically verify the Ed25519 signature, then return the payload with
    state (verified=True), or None if the signature is invalid / token malformed.

    Raises LicenseError ONLY when verification can't be performed at all (no key
    reachable + nothing cached) — the caller distinguishes "invalid" (None) from
    "couldn't check" (raise) so an offline first-run gives a clear message."""
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey

    parts = token.strip().split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    body, sig = parts
    try:
        sig_bytes = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    except (ValueError, binascii.Error):
        return None
    # building the verify key can fail two ways: _verify_key_raw32 raises LicenseError
    # when it genuinely can't reach a key (offline first-run — let that propagate so the
    # caller can say "connect once"); but a MALFORMED pinned/env/cached key (bad base64
    # or wrong length) is a config error that must fail CLOSED as "invalid token" (None),
    # never throw out of load_license (audit 2026-06-14, P2).
    try:
        raw = _verify_key_raw32()
    except LicenseError:
        raise
    except Exception:
        return None
    try:
        vk = VerifyKey(raw)
        # byte contract: signature is over the ASCII bytes of the base64url body
        vk.verify(body.encode("ascii"), sig_bytes)
    except (BadSignatureError, ValueError, TypeError):
        # bad signature, a wrong-length signature/key (PyNaCl raises ValueError),
        # or any malformed input — all mean "not a valid token", never crash.
        return None
    payload = decode_token(token)  # shape + exp-coercible check
    if payload is None:
        return None
    return _with_state(payload, verified=True)


def save_license(token: str) -> dict:
    """Verify the signature, refuse expired/unverified/pending tokens, persist, return state."""
    payload = decode_token(token)
    if payload is None:
        raise ValueError("that does not look like a recall license token")
    verified = verify_token(token)  # may raise LicenseError if no key reachable
    if verified is None:
        raise ValueError(
            "this license token failed signature verification — it was not issued by "
            "whatever-recall. Get a fresh one with `recall login`."
        )
    if int(payload.get("exp", 0)) <= int(time.time()):
        raise ValueError(
            "this token is already expired — sign in again with `recall login`"
        )
    # Refuse a PENDING signup auto-key (bug-hunt #1, 2026-06-17): it is good ONLY to log
    # in with, never a usable license. Persisting it let the dashboard's weaker client gate
    # (which checked only !expired) boot on a token the CLI gate + is_licensed() both refuse
    # — the gate predicate had drifted from the single source of truth. Refuse it at the
    # one persist chokepoint so no surface can treat a pending token as signed-in.
    if payload.get("pending_activation"):
        raise ValueError(
            "this is a pending signup key — it can only be used to sign in, not as a "
            "license. Run `recall login` to activate your seat."
        )
    LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_private(LICENSE_PATH, token.strip())
    return verified


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` as an owner-only (0o600) file with NO world-readable window.

    The token carries the buyer's email + plan — a real credential. We open with mode
    0o600 from the start (os.open with O_CREAT) so the bytes are never group/world-readable
    even briefly (the write_text-then-chmod TOCTOU gap). We do NOT touch the parent dir's
    mode: ~/.recall is shared with connect.json/recent.json/rules.md, and clobbering its
    perms on every save would strip a permission the user set on purpose. On Windows the
    POSIX mode is ignored by the OS (the dev's own platform) — harmless, NTFS ACLs apply."""
    data = text.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    # O_CREAT honours the mode only when CREATING; an existing file keeps its old mode,
    # so re-assert 0o600 on overwrite (still no widening — owner-only either way).
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_license() -> dict | None:
    """The stored payload + state (expired/days_left/verified/pending), or None when
    signed out. W2: a token whose Ed25519 signature does NOT verify is treated as
    signed-out (None) — never as a usable license. If verification can't be
    performed (offline first-run, no cached key) we fall back to the decoded state
    with verified=False so an already-valid offline session keeps working within
    its window, but the gate (require_license) still refuses verified=False."""
    # headless/CI: an env-supplied token wins over the file (still verified).
    token = os.environ.get(_ENV_TOKEN, "").strip()
    if not token:
        try:
            token = LICENSE_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    if not token:
        return None
    try:
        verified = verify_token(token)
        if verified is None:
            return None  # present but forged/invalid → signed out
        return verified
    except LicenseError:
        # couldn't verify (offline, no cached key). Return decoded state, unverified;
        # the gate decides whether to allow within the offline window.
        payload = decode_token(token)
        return _with_state(payload, verified=False) if payload else None


def load_raw_token() -> str | None:
    """The stored token string as-is (for the renew call), or None when signed out."""
    try:
        return LICENSE_PATH.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def clear_license() -> bool:
    """Sign out: remove the stored token. True if a file was actually removed."""
    try:
        LICENSE_PATH.unlink()
        return True
    except OSError:
        return False


def _with_state(payload: dict, *, verified: bool = False) -> dict:
    now = int(time.time())
    try:
        exp = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        exp = 0  # unparseable exp -> treat as already expired, never raise
    out = dict(payload)
    out["expired"] = exp <= now
    out["days_left"] = max(0, (exp - now + 86399) // 86400)
    out["verified"] = verified
    # pending: a signup auto-key, good ONLY for `recall login` — never a usable
    # license (the gate refuses it for every real command).
    out["pending"] = bool(payload.get("pending_activation"))
    return out

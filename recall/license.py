"""License token storage + decode (ADR-030).

One file: `~/.recall/license.token` — the signed token issued at
whatever-recall.com/account. The dashboard's Account tab writes it today;
`recall login` will write the same file when the CLI gate wave lands, so the
two stay byte-for-byte compatible (the connect.json pattern, ADR-012).

stdlib-only: the token is `base64url(json-payload) + "." + base64url(sig)`.
The payload is DECODED here for display and day-counting; cryptographic
verification of the Ed25519 signature needs the `cryptography` extra and
ships with the CLI gate wave. Every payload returned by this module carries
`verified: False` so no caller can mistake a decode for a verification.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import time
from pathlib import Path

LICENSE_PATH = Path.home() / ".recall" / "license.token"

_REQUIRED_CLAIMS = ("sub", "email", "plan", "exp")


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


def save_license(token: str) -> dict:
    """Validate the shape, refuse already-expired tokens, persist, return the payload."""
    payload = decode_token(token)
    if payload is None:
        raise ValueError("that does not look like a recall license token")
    if int(payload.get("exp", 0)) <= int(time.time()):
        raise ValueError(
            "this token is already expired — issue a fresh one at whatever-recall.com/account"
        )
    LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_private(LICENSE_PATH, token.strip())
    return _with_state(payload)


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
    """The stored payload + computed state (expired/days_left), or None when signed out."""
    try:
        token = LICENSE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    payload = decode_token(token)
    if payload is None:
        return None
    return _with_state(payload)


def clear_license() -> bool:
    """Sign out: remove the stored token. True if a file was actually removed."""
    try:
        LICENSE_PATH.unlink()
        return True
    except OSError:
        return False


def _with_state(payload: dict) -> dict:
    now = int(time.time())
    try:
        exp = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        exp = 0  # unparseable exp -> treat as already expired, never raise
    out = dict(payload)
    out["expired"] = exp <= now
    out["days_left"] = max(0, (exp - now + 86399) // 86400)
    out["verified"] = False  # Ed25519 check ships with the CLI gate wave
    return out

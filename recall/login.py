"""CLI license gate + `recall login` (browser device-flow) + `recall logout` (W2).

The gate (`ensure_licensed`) runs before every value command (D3: hard enforcement,
Q1: everything is gated — init included). A signup auto-key is a PENDING token,
good ONLY to log in; the gate refuses it for real work (Q2). When the user isn't
signed in, the gate STARTS the device-flow itself (D7: "the login mask comes to
you", not a cryptic error).

ONLINE-REQUIRED model (owner 2026-06-16: "ES GIBT KEIN OFFLINE arbeiten mehr").
There is no offline license window. The single-device seat check (migration 0013)
runs at most once every 60 min ("es sind alle 60 minuten jetzt dabei bleibt es")
and is the hard gate: when it is DUE and the server cannot confirm THIS device
still holds the seat — kicked, unreachable, or error — the gate FAILS CLOSED and
the command is refused (sign in again). One active device per seat, always
re-confirmed online, so a shared token can never run on a fleet. The token's exp
is only a short crypto backstop, never an "offline grace".
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from recall import license as L


def _cli_version() -> str:
    try:
        import importlib.metadata as md
        return md.version("whatever-recall")
    except Exception:
        return "0"


def _post(path: str, body: dict, *, timeout: float = 8.0) -> tuple[int, dict]:
    """POST JSON to the recall API. Returns (status, parsed-json-or-{})."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{L.API_BASE}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (https)
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        # connection refused / DNS / timeout / bad JSON → a soft failure the caller
        # handles (status 0), never a traceback through the CLI error boundary.
        return 0, {}


# commands that never require a license: the login/logout flow itself + pure
# process teardown + help/version (argparse handles -h/--version before dispatch).
FREE_COMMANDS = {"cmd_login", "cmd_logout", "cmd_stop"}

# commands that are gated but must NEVER block on an interactive device-flow:
#  - git hooks (cmd_hook installs them; cmd_precommit_check / cmd_stamp_commit ARE the
#    hooks) run inside `git commit` with no usable TTY — a 10-minute poll would hang
#    every commit (audit 2026-06-14, P1). They must fail fast with a clear message.
#  - the MCP server speaks JSON-RPC on stdio; it has its own in-band not-signed-in
#    answer (mcp.py) and must not try to open a browser / print to the protocol pipe.
#  - the dashboard / tray are long-lived servers: they're allowed to LAUNCH unlicensed
#    (so the login view renders); their value routes are gated per-request server-side.
NONINTERACTIVE_COMMANDS = {
    "cmd_hook", "cmd_precommit_check", "cmd_stamp_commit", "cmd_mcp",
    # `recall push` is the subagent / hookless situational-push path (workstream A) — it runs
    # in a non-TTY subagent shell, so it must FAIL FAST with a clear `recall login` line, never
    # poll the device flow. One-seat-one-device blocks cross-machine subagents (documented).
    "cmd_push",
}
ALLOW_LAUNCH_COMMANDS = {"cmd_dashboard", "cmd_tray"}


def _stdout_ok() -> bool:
    """A real, writable stdout we can print prompts to. pythonw / a detached tray
    process has stdout=None or a closed stream — printing there crashes (audit P2)."""
    out = getattr(sys, "stdout", None)
    return out is not None and not getattr(out, "closed", False)


def _is_tty() -> bool:
    """Interactive only when BOTH stdin and stdout are real TTYs — a piped `recall`,
    a CI job, or a hook is NOT interactive and must not enter the polling flow (P2)."""
    try:
        return bool(_stdout_ok() and sys.stdin and sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


class NotLicensed(Exception):
    """Raised to abort a gated command when the user couldn't be signed in."""


def _say(msg: str) -> None:
    """Print only if we have a usable stdout (pythonw/detached has none — P2). A Windows
    cp1252 console can't encode some glyphs (✓/⚠/—) and would raise UnicodeEncodeError mid-
    gate; fall back to an ASCII-safe rendering so a friendly message never crashes a command."""
    if not _stdout_ok():
        return
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


def _start_dashboard_after_login() -> None:
    """Owner 2026-06-17: signing in must bring up the dashboard — it is the window onto
    everything recall knows (and the surface for the v1.2 graph-native view). Best-effort:
    a failure here NEVER breaks the login, so the whole body is guarded.

    Started in the BACKGROUND (`recall tray`, owner's choice) as a DETACHED process so it
    survives this CLI exiting / a closed window — the same cross-platform spawn the MCP
    `dashboard` tool uses. We only spawn when no dashboard is already serving for this repo;
    `recall tray` itself re-probes `is_dashboard_live()` and just opens the URL if one is up,
    so a race here can at worst open the browser twice, never double-bind the port."""
    try:
        import os
        import subprocess

        from recall import dashboard
        from recall.cli import _find_repo, _index_path

        repo = _find_repo(".")  # walk up to the repo root, so login from a subdir still works
        # no index here means `recall init` hasn't run — nothing to show; tray would just
        # error out. Stay silent: login still succeeded, the dashboard simply has nothing yet.
        if not _index_path(repo).exists():
            return
        if dashboard.is_dashboard_live(repo):
            return  # already up — don't spawn a second one

        kw: dict = {"cwd": str(repo), "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")  # detached Windows console glyph-safety
        kw["env"] = env
        subprocess.Popen([sys.executable, "-m", "recall.cli", "tray", "--port", "7099"], **kw)
        _say("  ↳ opening your dashboard…")
    except Exception:
        # truly best-effort: a spawn failure must never turn a successful login into a failure.
        pass


def device_login(*, open_browser: bool = True) -> dict | None:
    """Run the browser device-flow. Returns the verified license state on success,
    None if the user cancelled / it timed out. Prints progress (stdout-safe)."""
    # send this machine's anonymous device id so the server can enforce one active
    # device per seat (single-device, 0013): this login takes the seat; the previous
    # machine goes offline at its next 60-min seat check.
    status, start = _post("/api/device/start", {"device_id": L.device_id()})
    if status != 200 or "device_code" not in start:
        _say("could not start the login (is whatever-recall.com reachable?)")
        return None
    device_code = start["device_code"]
    user_code = start.get("user_code", "")
    uri = start.get("verification_uri", f"{L.API_BASE}/activate")
    uri_full = start.get("verification_uri_complete", uri)
    interval = int(start.get("interval", 5))
    expires_in = int(start.get("expires_in", 600))

    _say("")
    _say("  Sign in to whatever-recall to authorize this device:")
    _say(f"    1. open   {uri}")
    _say(f"    2. code   {user_code}")
    _say("")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(uri_full)
        except Exception:
            pass  # headless / no browser — the printed URL + code is the fallback

    _say("  waiting for you to authorize…  (Ctrl-C to cancel)")
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        st, poll = _post("/api/device/poll", {"device_code": device_code})
        state = poll.get("status")
        if state == "ok" and poll.get("token"):
            try:
                saved = L.save_license(str(poll["token"]))
            except (ValueError, L.LicenseError) as e:
                _say(f"  login failed: {e}")
                return None
            # NOTE: no /api/license/activate call here — it's session-gated (requireUser)
            # and the CLI device-flow is deliberately session-less, so that POST ALWAYS
            # 401s. The cli_version + last_seen_at heartbeat is already recorded server-
            # side by the approve route (activateAndIssue), so the call was dead and
            # redundant. (P3 bug-hunt round 3, 2026-06-15.)
            _say("  ✓ signed in — you're all set")
            _start_dashboard_after_login()
            return saved
        if state in ("denied", "expired"):
            _say(f"  login {state}.")
            return None
        # pending / slow_down → keep polling
    _say("  login timed out — run `recall login` again when ready.")
    return None


# single-device enforcement (owner 2026-06-14): one active device per seat. The check is
# throttled so it never adds latency to every command — cadence 60 min (owner 2026-06-16:
# "es sind alle 60 minuten jetzt dabei bleibt es").
#
# ONLINE-REQUIRED, but DEVELOPER-FRIENDLY (owner 2026-06-16: "mach es mehr entwickler-
# freundlich … wenn der check fehlschlägt, kommt oben im header eine nachricht … you get
# logged out in 60 minuten. und ein timer startet. dann hat der nochmal 60 minuten zeit …
# button reconnect. das ist human"). So it is NOT a hard instant lockout:
#   • check confirms        → work, clock reset, any grace cleared.
#   • check fails (offline) → a 60-min GRACE starts; the dev KEEPS WORKING and gets a
#     header warning + live countdown + a Reconnect button. A confirm during grace clears it.
#   • grace (60 min) elapses with no confirm → THEN the seat is given up (logged out).
#   • another device took the seat → immediate logout (the seat is genuinely gone).
# Worst case a shared token survives ~2h (one interval + one grace), never 7 days.
import os as _os
from pathlib import Path as _Path
_SEAT_CHECK_PATH = _Path.home() / ".recall" / "seat_check.ts"   # last CONFIRMED check
_SEAT_GRACE_PATH = _Path.home() / ".recall" / "seat_grace.ts"   # when the grace started
SEAT_CHECK_INTERVAL_S = 60 * 60
SEAT_GRACE_S = 60 * 60  # extra hour to reconnect after a failed check before logout

# seat_check result codes
SEAT_OK = "ok"          # confirmed online (or not yet due) → work
SEAT_KICKED = "kicked"  # another device took the seat → signed out locally, logout now
SEAT_GRACE = "grace"    # check failing but within the 60-min grace → keep working + warn
SEAT_EXPIRED = "expired"  # grace elapsed with no reconnect → give up the seat (logout)


def _seat_check_due() -> bool:
    """True if it's been ≥60 min since the last CONFIRMED seat check (or there was none).

    A stamp in the FUTURE counts as due NOW (bug-hunt MEDIUM, 2026-06-17). Without this,
    setting the system clock far forward once stamps a future `last`; then `now - last` is
    negative and the check is never due — online seat enforcement is disabled until the
    token crypto-expires (~1 day), voiding the single-active-device guarantee for that
    window. Treating a future stamp as due forces a fresh online confirm and the server's
    authoritative clock takes over."""
    try:
        last = float(_SEAT_CHECK_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return last > time.time() or (time.time() - last) >= SEAT_CHECK_INTERVAL_S


def _mark_seat_checked() -> None:
    """Stamp a SUCCESSFUL confirm and clear any grace. Only a confirmed seat resets the
    clock — a failed check must NOT stamp, or offline work would silently get a free hour."""
    try:
        _SEAT_CHECK_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SEAT_CHECK_PATH.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass
    try:
        _SEAT_GRACE_PATH.unlink()  # reconnected → cancel the countdown
    except OSError:
        pass


def _grace_started_at() -> float | None:
    """When the current grace period began, or None if no grace is active."""
    try:
        return float(_SEAT_GRACE_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _begin_grace_if_needed() -> float:
    """Start the grace clock on the FIRST failed check (idempotent). Returns its start ts."""
    started = _grace_started_at()
    if started is not None:
        return started
    now = time.time()
    try:
        _SEAT_GRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SEAT_GRACE_PATH.write_text(str(int(now)), encoding="utf-8")
    except OSError:
        pass
    return now


def seat_grace_seconds_left() -> int | None:
    """Seconds remaining in the active grace countdown (for the header timer), or None when
    no grace is running. 0 means the grace has elapsed (the next check expires the seat).

    A grace that began in the FUTURE (clock rolled back after grace started) reads as
    EXPIRED, not as a fresh full hour (bug-hunt MEDIUM, 2026-06-17) — otherwise the same
    clock-rollback that defeats the check throttle could also keep the grace topped up
    forever. So a future `started` -> 0 (the next check then gives up the seat)."""
    started = _grace_started_at()
    if started is None:
        return None
    if started > time.time():
        return 0
    return max(0, int(SEAT_GRACE_S - (time.time() - started)))


def seat_check_if_due() -> str:
    """Single-device heartbeat with a developer-friendly grace. When due (≥60 min) ask the
    server whether THIS device still holds the seat.
      - not due / confirmed active  → SEAT_OK     (work; clock reset, grace cleared)
      - another device took it       → SEAT_KICKED  (token cleared → logout now)
      - unreachable / server error   → SEAT_GRACE   while within the 60-min grace (keep
                                         working + warn), then SEAT_EXPIRED (give up seat)
    A confirmed check always clears the grace. Only a confirmed check stamps the clock, so
    while offline every command keeps re-evaluating the grace until it reconnects or expires.
    """
    if not _seat_check_due():
        return SEAT_OK
    token = L.load_raw_token()
    if not token:
        return SEAT_EXPIRED  # nothing to confirm → not usable
    status, body = _post("/api/license/check", {"token": token, "device_id": L.device_id()}, timeout=6.0)
    if status == 200 and body.get("active") is False:
        L.clear_license()  # the seat was taken by another device → sign out locally
        try:
            _SEAT_GRACE_PATH.unlink()
        except OSError:
            pass
        return SEAT_KICKED
    if status == 200:
        _mark_seat_checked()  # confirmed online → reset the clock + clear grace
        return SEAT_OK
    # unreachable / server error → run the grace clock
    _begin_grace_if_needed()
    if (seat_grace_seconds_left() or 0) > 0:
        return SEAT_GRACE       # still time to reconnect → keep working, warn the user
    L.clear_license()           # grace elapsed with no reconnect → give up the seat
    try:
        _SEAT_GRACE_PATH.unlink()
    except OSError:
        pass
    return SEAT_EXPIRED


def is_licensed() -> bool:
    """True iff there's a verified, in-window, non-pending license. The single source
    of truth used by the CLI gate, the dashboard value routes, and MCP."""
    state = L.load_license()
    return bool(state) and bool(state.get("verified")) and not state.get("expired") and not state.get("pending")


def ensure_licensed(func_name: str, *, interactive: bool | None = None) -> None:
    """The gate. Called before every command. Raises NotLicensed (which the caller
    turns into a clean exit) when the user can't be signed in.

    - FREE commands (login/logout/stop) and LAUNCH-allowed servers (dashboard/tray,
      gated per-request instead) skip the gate entirely.
    - valid + verified token within its window → allow (+ maybe silent renew).
    - pending / expired / forged / absent → D7: in a REAL interactive terminal start
      the device-flow right here; otherwise (git hook, MCP, piped, CI, pythonw) raise
      a clear `recall login` message instead of hanging on the device-flow poll (P1/P2).
    """
    if func_name in FREE_COMMANDS or func_name in ALLOW_LAUNCH_COMMANDS:
        return

    state = L.load_license()
    if state and state.get("verified") and not state.get("expired") and not state.get("pending"):
        # crypto-valid token. ONLINE-REQUIRED but DEVELOPER-FRIENDLY (owner 2026-06-16:
        # "ES GIBT KEIN OFFLINE arbeiten mehr" + "mach es mehr entwicklerfreundlich …
        # 60 min check, fehlschlag → header-warnung + 60 min reconnect-frist + button,
        # das ist human"). The throttled single-device seat check (≤1×/60 min) runs and:
        #   - SEAT_OK      → work (confirmed, or not yet due)
        #   - SEAT_GRACE   → check failing but within the 60-min grace → WARN + keep working
        #   - SEAT_KICKED  → another device took it → token cleared → relogin path
        #   - SEAT_EXPIRED → grace elapsed with no reconnect → token cleared → relogin path
        # EXCEPTION: non-interactive commands (hooks/MCP/CI) skip the check so it can
        # never stall a `git commit` or the MCP stdio loop on a slow network.
        if func_name in NONINTERACTIVE_COMMANDS:
            return
        try:
            result = seat_check_if_due()
        except Exception:
            result = SEAT_OK  # unexpected internal error → don't lock out on our own bug
        if result == SEAT_OK:
            return
        if result == SEAT_GRACE:
            # offline, but the dev still has time → a friendly one-line warning, keep working.
            # ASCII only — a Windows cp1252 console raises UnicodeEncodeError on glyphs.
            left = seat_grace_seconds_left() or 0
            mins = max(1, (left + 59) // 60)
            _say(f"  ! couldn't reach whatever-recall to confirm your seat - reconnect "
                 f"within ~{mins} min or you'll be signed out. (run `recall login` to reconnect)")
            return
        # SEAT_KICKED / SEAT_EXPIRED → token cleared; re-read and fall through to relogin
        _seat_outcome = result
        state = L.load_license()
    else:
        _seat_outcome = None

    # not licensed → message tuned to the state
    if _seat_outcome == SEAT_KICKED:
        msg = "you were signed out — this seat was taken by another device"
    elif _seat_outcome == SEAT_EXPIRED:
        msg = "your seat couldn't be confirmed online in time — sign in again"
    elif state and state.get("pending"):
        msg = "finish signing in to start your 14-day trial"
    elif state and state.get("expired"):
        msg = "your session has expired — sign in again"
    else:
        msg = "you're not signed in to whatever-recall"

    # decide interactivity: explicit override wins; else a command is interactive only
    # when it isn't a known non-interactive one AND we have a real TTY.
    if interactive is None:
        interactive = func_name not in NONINTERACTIVE_COMMANDS and _is_tty()

    if interactive:
        if _stdout_ok():
            print(f"  {msg}.")
        result = device_login()
        if result is not None:
            return
        raise NotLicensed("sign in with `recall login` to use recall")
    # non-interactive: a clear, fast instruction — never a browser, never a poll
    raise NotLicensed(f"{msg} — run `recall login` (opens your browser) to continue")


def logout() -> bool:
    """`recall logout`: remove the stored token. True if one was removed."""
    return L.clear_license()

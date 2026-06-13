"""Run the dashboard as a background app with a system-tray icon — no console window
to accidentally close (the #1 first-week footgun: closing the window kills the server,
and the open browser tab just shows "reconnecting…" forever).

Two modes, picked automatically so the base install stays zero-dependency (the same
philosophy as codemap/bridge/power being optional extras):

  tray   — `pip install whatever-recall[tray]` brings pystray + Pillow. The dashboard
           runs in a background thread; a tray icon (next to the clock) offers
           Open / Re-index status / Quit. No console window exists at all.
  banner — no pystray? Fall back to a normal console run, but print a big, loud
           "DO NOT CLOSE THIS WINDOW" banner first. The window IS the server; closing
           it stops the server. Never a silent crash — the user always has a signal.

`recall tray` is what the Desktop launcher calls (via pythonw on Windows, so no
console flashes up). `recall stop` asks a running dashboard to shut itself down.
"""

from __future__ import annotations

import sys
import threading
import webbrowser
from pathlib import Path

ASSETS = Path(__file__).parent / "assets"

# ── ANSI-free, width-bounded banner (console fallback) ───────────────────────────
_BANNER_LINES = [
    "+-------------------------------------------------------------+",
    "|                                                             |",
    "|   recall dashboard is RUNNING in this window                |",
    "|                                                             |",
    "|   >>> DO NOT CLOSE THIS WINDOW <<<                           |",
    "|   Closing it STOPS the server. Your open dashboard tab      |",
    "|   would then just show 'RECONNECTING...' forever.           |",
    "|                                                             |",
    "|   Minimise it instead. To stop on purpose: press Ctrl-C,    |",
    "|   or run `recall stop` in another terminal.                 |",
    "|                                                             |",
    "|   Tip: `pip install whatever-recall[tray]` runs this with   |",
    "|   NO window at all — a tray icon next to the clock instead.  |",
    "|                                                             |",
    "+-------------------------------------------------------------+",
]


def banner_text() -> str:
    """The console-fallback warning. Pure, so a test can pin its content."""
    return "\n".join(_BANNER_LINES)


def tray_available() -> bool:
    """True iff the optional tray extra (pystray + Pillow) is importable."""
    try:
        import PIL  # noqa: F401
        import pystray  # noqa: F401

        return True
    except Exception:
        return False


def _icon_image():
    """Load the brand icon for the tray. Falls back to a tiny generated square so the
    tray never fails just because the asset is missing from a packaging build."""
    from PIL import Image

    for name in ("recall.png", "recall.ico"):
        p = ASSETS / name
        if p.exists():
            try:
                return Image.open(p).convert("RGBA")
            except Exception:
                pass
    # last resort: a 32×32 oxblood square so the icon is always *something*
    return Image.new("RGBA", (32, 32), (166, 41, 31, 255))


def run(
    repo: Path,
    idx_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 7099,
    open_browser: bool = True,
    watch: bool = True,
) -> int:
    """Run the dashboard as a tray app if possible, else a banner console run.

    Returns an exit code. This is the single entry point `recall tray` calls; it never
    raises on a missing tray dependency — it degrades to the visible console fallback.
    """
    if not tray_available():
        return _run_console_fallback(
            repo, idx_path, host=host, port=port, open_browser=open_browser, watch=watch
        )
    return _run_tray(
        repo, idx_path, host=host, port=port, open_browser=open_browser, watch=watch
    )


def _run_console_fallback(
    repo: Path, idx_path: Path, *, host: str, port: int, open_browser: bool, watch: bool
) -> int:
    """No pystray: a normal serve(), but lead with the loud DO-NOT-CLOSE banner."""
    from recall import dashboard

    print(banner_text(), flush=True)
    print(flush=True)
    return dashboard.serve(
        repo, idx_path, host=host, port=port, open_browser=open_browser, watch=watch
    )


def _run_tray(
    repo: Path, idx_path: Path, *, host: str, port: int, open_browser: bool, watch: bool
) -> int:
    """The real tray app: serve() on a daemon thread, a pystray icon drives it."""
    import pystray
    from pystray import Menu, MenuItem

    from recall import dashboard

    url = f"http://{host}:{port}"

    # serve() blocks on serve_forever(); run it on a thread so the tray owns the main loop.
    server_rc: dict[str, object] = {}

    def _serve() -> None:
        try:
            server_rc["rc"] = dashboard.serve(
                repo,
                idx_path,
                host=host,
                port=port,
                open_browser=False,  # we open once, after the server is up
                watch=watch,
            )
        except Exception as e:  # a bind failure etc. — surface, don't hang silently
            server_rc["error"] = str(e)

    t = threading.Thread(target=_serve, daemon=True, name="recall-dashboard")
    t.start()

    # Wait until the server is actually serving (or has failed) BEFORE showing a tray icon
    # or opening the browser. A swallowed bind error (port in use) must surface as a clean
    # message + non-zero exit, never a tray icon hovering over a dead server.
    if not _wait_until_serving(host, port, server_rc):
        why = server_rc.get("error") or f"could not bind {host}:{port} (is it already in use?)"
        print(f"recall · dashboard failed to start — {why}", flush=True)
        return 1

    if open_browser:
        _safe_open(url)

    icon_ref: dict[str, object] = {}

    def _open(icon, item):  # noqa: ARG001
        _safe_open(url)

    def _quit(icon, item):  # noqa: ARG001
        try:
            dashboard.request_shutdown()
        except Exception:
            pass
        icon.stop()

    title = f"recall · {repo.name} · {url}"
    menu = Menu(
        MenuItem(f"Open dashboard ({port})", _open, default=True),
        MenuItem("Live: new commits auto-index" if watch else "Read-only viewer", None, enabled=False),
        Menu.SEPARATOR,
        MenuItem("Quit recall", _quit),
    )
    icon = pystray.Icon("recall", _icon_image(), title, menu)
    icon_ref["icon"] = icon

    # icon.run() blocks until _quit() calls icon.stop(); then we tear the server down.
    icon.run()

    try:
        dashboard.request_shutdown()
    except Exception:
        pass
    rc = server_rc.get("rc", 0)
    return rc if isinstance(rc, int) else 0


def _wait_until_serving(host: str, port: int, server_rc: dict, *, timeout: float = 5.0) -> bool:
    """Poll the port until the dashboard answers, the serve thread reports an error, or
    we time out. Returns True iff a server is actually accepting connections — so the
    caller never shows a tray icon over a server that failed to bind."""
    import socket
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "error" in server_rc:  # the serve thread already failed (e.g. bind error)
            return False
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return "error" not in server_rc and _port_open(host, port)


def _port_open(host: str, port: int) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _safe_open(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass

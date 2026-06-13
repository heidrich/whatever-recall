"""Desktop launcher (`recall shortcut`) — every OS variant, plus CLI wiring guards."""

import stat
import sys
from pathlib import Path

import pytest

from recall import shortcut
from recall.cli import build_parser


REPO = Path("C:/Users/me/projects/my repo") if sys.platform == "win32" else Path("/home/me/projects/my repo")


@pytest.mark.parametrize("cmd", ["shortcut", "dashboard", "tray", "stop", "stats", "explain", "contested", "freshen"])
def test_launcher_commands_accept_a_positional_path(cmd):
    """Regression (audit 2026-06-13): `recall shortcut .` errored 'unrecognized
    arguments: .' while `recall init .` worked — only init/power had a positional.
    A user who learned `recall init .` types `recall <cmd> .` and must not hit an
    error. The positional must parse AND reach the repo resolver as `path`."""
    parser = build_parser()
    args = parser.parse_args([cmd, "."])  # must not SystemExit
    assert getattr(args, "path", None) == "."


def test_repo_flag_still_parses_for_launcher_commands():
    parser = build_parser()
    args = parser.parse_args(["shortcut", "--repo", "."])
    assert args.repo == "."


def test_windows_spec_runs_tray_never_pause():
    """The launcher always runs `recall tray`, never the old `dashboard ... pause` (closing
    that window silently killed the server → the tab hung on RECONNECTING). Two modes:
    windowless=True (tray extra) detaches via pythonw+start; windowless=False (base install)
    keeps a VISIBLE console so the DO-NOT-CLOSE banner shows. Neither uses `pause`."""
    name, win = shortcut.launcher_spec(REPO, 7099, "C:/py/python.exe", platform="win32", windowless=True)
    assert name == "recall-dashboard-my-repo.bat"
    assert f'cd /d "{REPO}"' in win
    assert "-m recall.cli tray --port 7099" in win
    assert "start " in win  # detached via `start` (pythonw used when it exists on disk)
    assert "pause" not in win

    _, vis = shortcut.launcher_spec(REPO, 7099, "C:/py/python.exe", platform="win32", windowless=False)
    assert "-m recall.cli tray --port 7099" in vis
    assert "start " not in vis  # visible console holds the server (banner must be seen)
    assert "pause" not in vis
    assert vis.startswith("@echo off")


def test_darwin_spec_is_a_command_file():
    name, content = shortcut.launcher_spec(REPO, 7100, "/usr/bin/python3", platform="darwin")
    assert name == "recall-dashboard-my-repo.command"
    assert content.startswith("#!/bin/sh")
    assert f'cd "{REPO}"' in content
    assert 'exec "/usr/bin/python3" -m recall.cli dashboard --port 7100' in content


def test_linux_spec_is_a_desktop_entry():
    name, content = shortcut.launcher_spec(REPO, 7099, "/usr/bin/python3", platform="linux")
    assert name == "recall-dashboard-my-repo.desktop"
    assert "[Desktop Entry]" in content
    assert "Terminal=true" in content
    assert f"Path={REPO}" in content
    assert "--repo" in content  # .desktop Exec has no cwd guarantee — repo is explicit


def test_slug_strips_unsafe_characters():
    assert shortcut._slug("we ird/näme!") == "we-ird-n-me"
    assert shortcut._slug("---") == "project"


def test_create_and_remove_roundtrip(tmp_path):
    repo = tmp_path / "my repo"
    repo.mkdir()
    desktop = tmp_path / "desktop"
    path = shortcut.create_shortcut(repo, port=7099, desktop=desktop)
    assert path.exists() and path.parent == desktop
    if sys.platform == "win32":
        # Desktop carries the icon .lnk; the engine .bat sits in .mind/
        assert path.suffix == ".lnk"
        assert (repo / ".mind" / "dashboard-launcher.bat").exists()
    else:
        assert path.stat().st_mode & stat.S_IXUSR
    # overwrite in place, no duplicates
    again = shortcut.create_shortcut(repo, port=7099, desktop=desktop)
    assert again == path and len(list(desktop.iterdir())) == 1
    assert shortcut.remove_shortcut(repo, desktop=desktop) is True
    assert shortcut.remove_shortcut(repo, desktop=desktop) is False


def test_windows_create_replaces_the_legacy_bare_bat(tmp_path):
    if sys.platform != "win32":
        return  # the legacy cleanup is a Windows-only path
    repo = tmp_path / "proj"
    repo.mkdir()
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    legacy = desktop / "recall-dashboard-proj.bat"
    legacy.write_text("@echo off\r\n")
    shortcut.create_shortcut(repo, desktop=desktop)
    assert not legacy.exists()


def test_assets_ship_with_the_package():
    assert (shortcut.ASSETS / "recall.ico").exists()
    assert (shortcut.ASSETS / "recall.png").exists()


def test_cli_knows_the_subcommand():
    """Drift-guard: bare-query routing must not swallow a real subcommand as a search.
    `recall shortcut` resolves to cmd_shortcut, not to a query for the word 'shortcut'."""
    from recall import cli

    parser = cli.build_parser()
    args = parser.parse_args(["shortcut", "--port", "7042"])
    assert args.func is cli.cmd_shortcut and args.port == 7042


def test_bare_query_router_covers_every_subcommand():
    """Drift-guard (2026-06-13): the bare-query router used a hand-kept allowlist that
    drifted — `tray`/`stop` fell through and were parsed as search queries. The known
    set is now derived from the parser, so EVERY registered subcommand must be routed
    as a command (never swallowed). This guard fails the moment that coupling breaks."""
    from recall import cli

    parser = cli.build_parser()
    known = cli._subcommand_names(parser)
    # the commands a new user is most likely to type and MUST not be eaten as a query
    for cmd in ("tray", "stop", "shortcut", "dashboard", "init", "stats"):
        assert cmd in known, f"`recall {cmd}` would be parsed as a search query, not a command"
    # and the router actually consults the derived set, not a stale literal list
    import inspect

    src = inspect.getsource(cli.main)
    assert "_subcommand_names" in src, "bare-query router must derive known cmds from the parser"

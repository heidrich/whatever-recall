"""Desktop launcher (`recall shortcut`) — every OS variant, plus CLI wiring guards."""

import stat
import sys
from pathlib import Path

from recall import shortcut


REPO = Path("C:/Users/me/projects/my repo") if sys.platform == "win32" else Path("/home/me/projects/my repo")


def test_windows_spec_pins_repo_port_and_interpreter():
    name, content = shortcut.launcher_spec(REPO, 7099, "C:/py/python.exe", platform="win32")
    assert name == "recall-dashboard-my-repo.bat"
    assert f'cd /d "{REPO}"' in content
    assert '"C:/py/python.exe" -m recall.cli dashboard --port 7099' in content
    assert content.startswith("@echo off")
    assert "pause" in content  # errors stay readable after exit


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
    """Drift-guard: bare-query routing must not swallow `recall shortcut` as a search."""
    import inspect

    from recall import cli

    src = inspect.getsource(cli.main)
    assert '"shortcut"' in src, "add 'shortcut' to the known-subcommands set in cli.main"
    parser = cli.build_parser()
    args = parser.parse_args(["shortcut", "--port", "7042"])
    assert args.func is cli.cmd_shortcut and args.port == 7042

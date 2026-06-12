"""Shared fixtures. Make the package importable without an install step."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path, monkeypatch):
    """No test may touch the REAL per-user state in ~/.recall.

    The owner's project switcher filled up with pytest tmp projects because
    dashboard tests called serve()/switch against the real recent.json. Every
    test gets throwaway paths instead — structurally, so no future test can
    forget to monkeypatch.
    """
    import recall.connect as conn
    import recall.dashboard as dash
    import recall.license as lic

    monkeypatch.setattr(dash, "RECENT_PATH", tmp_path / "_user" / "recent.json")
    monkeypatch.setattr(lic, "LICENSE_PATH", tmp_path / "_user" / "license.token")
    monkeypatch.setattr(conn, "CONNECT_PATH", tmp_path / "_user" / "connect.json")

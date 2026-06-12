"""Drift-guards for STEP 3 — the connection config (ADR-012, no provider default).

Pins: nothing connected by default; the file stores only the env-var NAME (never
the key); a malformed file degrades to "not connected" rather than crashing; and
save/load/clear round-trip cleanly. All offline, all against a tmp path.
"""

from __future__ import annotations

import json

import pytest

from recall.connect import (
    Connection,
    clear_connection,
    load_connection,
    save_connection,
)


def test_no_connection_by_default(tmp_path):
    p = tmp_path / "connect.json"
    assert load_connection(p) is None  # ADR-012: per default NOTHING is connected


def test_save_and_load_roundtrip_ollama(tmp_path):
    p = tmp_path / "connect.json"
    save_connection(Connection(provider="ollama", model="llama3"), p)
    got = load_connection(p)
    assert got == Connection(provider="ollama", model="llama3")


def test_save_stores_only_env_name_never_a_key(tmp_path):
    p = tmp_path / "connect.json"
    save_connection(
        Connection(
            provider="anthropic", model="claude-opus-4-8", api_key_env="ANTHROPIC_API_KEY"
        ),
        p,
    )
    raw = json.loads(p.read_text(encoding="utf-8"))
    # only the NAME of the env var is persisted — never a literal key
    assert raw["api_key_env"] == "ANTHROPIC_API_KEY"
    # no key-bearing field may exist; api_key_env (a NAME) is the only key-related field
    key_fields = {k for k in raw if "key" in k.lower()}
    assert key_fields == {"api_key_env"}


def test_malformed_file_is_not_connected_not_a_crash(tmp_path):
    p = tmp_path / "connect.json"
    p.write_text("{ this is not json", encoding="utf-8")
    assert load_connection(p) is None  # degrade, don't raise


def test_partial_file_is_not_connected(tmp_path):
    p = tmp_path / "connect.json"
    p.write_text(json.dumps({"provider": "ollama"}), encoding="utf-8")  # no model
    assert load_connection(p) is None


def test_bad_provider_in_file_is_not_connected(tmp_path):
    p = tmp_path / "connect.json"
    p.write_text(json.dumps({"provider": "openai", "model": "gpt-4"}), encoding="utf-8")
    assert load_connection(p) is None  # only ollama|anthropic are connectable


def test_clear_removes_connection(tmp_path):
    p = tmp_path / "connect.json"
    save_connection(Connection(provider="ollama", model="llama3"), p)
    assert clear_connection(p) is True
    assert load_connection(p) is None
    assert clear_connection(p) is False  # already gone


def test_connection_rejects_unknown_provider():
    with pytest.raises(ValueError):
        Connection(provider="openai", model="gpt-4")


def test_connection_requires_a_model():
    with pytest.raises(ValueError):
        Connection(provider="ollama", model="")


# ---- the two providers added with the connect-modal (ADR-012) ----
def test_claude_cli_is_a_connectable_provider(tmp_path):
    """The Owner's main path: a CLI command, no key, round-trips like the others."""
    p = tmp_path / "connect.json"
    save_connection(Connection(provider="claude-cli", model="claude"), p)
    got = load_connection(p)
    assert got == Connection(provider="claude-cli", model="claude")
    # the CLI path stores NO key field at all
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert "api_key_env" not in raw


def test_custom_requires_a_base_url():
    with pytest.raises(ValueError):
        Connection(provider="custom", model="gpt-4o")  # no base_url -> rejected


def test_custom_roundtrips_with_endpoint_and_optional_key_name(tmp_path):
    p = tmp_path / "connect.json"
    save_connection(
        Connection(
            provider="custom", model="gpt-4o-mini",
            base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY",
        ),
        p,
    )
    got = load_connection(p)
    assert got is not None and got.provider == "custom"
    assert got.base_url == "https://openrouter.ai/api/v1"
    # still names only — a custom key is also stored by env-var NAME, never literal
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert {k for k in raw if "key" in k.lower()} == {"api_key_env"}

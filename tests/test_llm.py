"""Drift-guards for STEP 3 — the LLM provider seam (offline, ADR-012).

Pins: get_provider has NO default (no connection -> clear error, not a silent
fallback); the cost table is right and never silently 0 for a paid model;
EchoProvider is pure-offline and records calls; Ollama goes over a MOCKED urllib
(zero real network); the Anthropic provider degrades cleanly when its SDK / key
is absent (import-guarded, like the bridge).
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from recall.connect import Connection
from recall.llm import (
    AnthropicProvider,
    ClaudeCliProvider,
    EchoProvider,
    LLMProvider,
    OllamaProvider,
    OpenAICompatProvider,
    cost_for,
    get_provider,
    provider_from_connection,
)


def test_get_provider_no_connection_raises_with_hint(tmp_path, monkeypatch):
    # isolate from the real ~/.recall/connect.json: point CONNECT_PATH at a missing file
    import recall.connect as connect_mod
    monkeypatch.setattr(connect_mod, "CONNECT_PATH", tmp_path / "nope.json")
    with pytest.raises(RuntimeError) as e:
        get_provider(conn=None)  # None -> read connection from disk -> nothing connected
    msg = str(e.value).lower()
    assert "recall connect" in msg and "default" in msg  # ADR-012, no silent fallback


def test_get_provider_builds_ollama_from_connection():
    p = get_provider(Connection(provider="ollama", model="llama3"))
    assert isinstance(p, OllamaProvider) and p.model == "llama3"
    assert p.cost_per_token == (0.0, 0.0)  # local is free
    assert isinstance(p, LLMProvider)  # satisfies the Protocol


def test_get_provider_builds_anthropic_from_connection():
    p = provider_from_connection(
        Connection(provider="anthropic", model="claude-opus-4-8", api_key_env="X_KEY")
    )
    assert isinstance(p, AnthropicProvider) and p.api_key_env == "X_KEY"
    assert p.cost_per_token[0] > 0  # paid -> never free


def test_cost_table_known_and_unknown_models():
    # known model: opus-tier in/out
    assert cost_for("claude-opus-4-8", 1_000_000, 0) == pytest.approx(5.0)
    assert cost_for("claude-opus-4-8", 0, 1_000_000) == pytest.approx(25.0)
    # an unknown anthropic-ish model defaults to a PAID estimate, never 0
    assert cost_for("claude-future-9", 1_000_000, 0) > 0


def test_echo_provider_is_offline_and_records_calls():
    echo = EchoProvider(canned=json.dumps({"ok": True}))
    assert echo.complete_calls == []
    # count_tokens must NOT count as a completion
    n = echo.count_tokens("a b c d")
    assert n == 4 and echo.complete_calls == []
    resp = echo.complete("sys", "usr")
    assert json.loads(resp.text) == {"ok": True}
    assert len(echo.complete_calls) == 1  # the one real completion is recorded


def test_ollama_complete_over_mocked_urllib():
    """Ollama uses pure stdlib urllib — we mock it so the test stays 100% offline."""
    fake_body = json.dumps(
        {"message": {"content": "{\"x\": 1}"}, "prompt_eval_count": 12, "eval_count": 3}
    ).encode("utf-8")

    class _Resp:
        def read(self):
            return fake_body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with mock.patch("urllib.request.urlopen", return_value=_Resp()) as m:
        p = OllamaProvider(model="llama3")
        out = p.complete("system", "user")
    assert m.called  # went through urllib, not a real socket
    assert json.loads(out.text) == {"x": 1}
    assert out.input_tokens == 12 and out.output_tokens == 3


def test_anthropic_is_import_guarded_when_sdk_missing():
    """The base install must not depend on the Anthropic SDK. If it's absent, the
    provider raises a clear [power]-extra hint rather than ImportError at module load."""
    import builtins

    real_import = builtins.__import__

    def _no_anthropic(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("no module named anthropic")
        return real_import(name, *args, **kwargs)

    p = AnthropicProvider(model="claude-opus-4-8", api_key_env="X_KEY")
    with mock.patch("builtins.__import__", side_effect=_no_anthropic):
        with pytest.raises(RuntimeError) as e:
            p._client()
    assert "[power]" in str(e.value)


def test_anthropic_missing_key_is_clear_error():
    """SDK present but no key in the env var -> clear message, no crash."""
    p = AnthropicProvider(model="claude-opus-4-8", api_key_env="DEFINITELY_UNSET_KEY_42")
    fake_anthropic = mock.MagicMock()
    with mock.patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        with pytest.raises(RuntimeError) as e:
            p._client()
    assert "DEFINITELY_UNSET_KEY_42" in str(e.value)


# ---- claude-cli provider (the Owner's main path: subprocess, no key) ----
def test_get_provider_builds_claude_cli_and_is_free():
    p = provider_from_connection(Connection(provider="claude-cli", model="claude"))
    assert isinstance(p, ClaudeCliProvider) and p.model == "claude"
    assert p.cost_per_token == (0.0, 0.0)  # a subscription run bills no marginal tokens
    assert isinstance(p, LLMProvider)  # satisfies the Protocol


def test_claude_cli_missing_command_is_a_clear_error():
    """If the CLI isn't installed, the user gets an install hint, not a crash."""
    p = ClaudeCliProvider(model="definitely-not-a-real-cli-xyz")
    with mock.patch("shutil.which", return_value=None):
        with pytest.raises(RuntimeError) as e:
            p.complete("sys", "usr")
    assert "not found" in str(e.value).lower()


def test_claude_cli_runs_subprocess_and_returns_stdout():
    """Resolve the command, run it with --print, return its stdout — fully mocked,
    zero real process spawned, zero network. The system prompt goes via the
    --system-prompt flag (claude supports it); the user prompt goes on stdin."""
    p = ClaudeCliProvider(model="claude")
    done = mock.MagicMock(returncode=0, stdout="  the reply  ", stderr="")
    with mock.patch("shutil.which", return_value="/usr/bin/claude"), \
         mock.patch("subprocess.run", return_value=done) as run:
        out = p.complete("system text", "user text")
    assert out.text == "the reply"  # stripped
    args, kwargs = run.call_args
    argv = args[0]
    assert argv[0] == "/usr/bin/claude" and "--print" in argv
    # known claude CLI -> system rides the flag, user rides stdin
    assert "--system-prompt" in argv and "system text" in argv
    assert kwargs["input"] == "user text"


def test_claude_cli_unknown_command_falls_back_to_stdin_system():
    """A CLI whose name isn't 'claude' gets system+user concatenated on stdin
    (no --system-prompt assumption for an unknown tool)."""
    p = ClaudeCliProvider(model="aider")
    done = mock.MagicMock(returncode=0, stdout="ok", stderr="")
    with mock.patch("shutil.which", return_value="/usr/bin/aider"), \
         mock.patch("subprocess.run", return_value=done) as run:
        p.complete("SYS", "USR")
    args, kwargs = run.call_args
    assert "--system-prompt" not in args[0]
    assert "SYS" in kwargs["input"] and "USR" in kwargs["input"]


def test_claude_cli_nonzero_exit_is_reported():
    p = ClaudeCliProvider(model="claude")
    fail = mock.MagicMock(returncode=2, stdout="", stderr="not logged in")
    with mock.patch("shutil.which", return_value="/usr/bin/claude"), \
         mock.patch("subprocess.run", return_value=fail):
        with pytest.raises(RuntimeError) as e:
            p.complete("s", "u")
    assert "not logged in" in str(e.value)


# ---- custom OpenAI-compatible provider ----
def test_get_provider_builds_custom_with_endpoint():
    p = provider_from_connection(
        Connection(provider="custom", model="gpt-4o-mini",
                   base_url="http://localhost:1234/v1", api_key_env="MY_KEY")
    )
    assert isinstance(p, OpenAICompatProvider)
    assert p.base_url == "http://localhost:1234/v1" and p.api_key_env == "MY_KEY"
    assert p.cost_per_token[0] > 0  # unknown cost -> paid estimate, never silently 0


def test_custom_complete_over_mocked_urllib_openai_shape():
    """OpenAI /chat/completions shape, mocked so the test is 100% offline."""
    fake_body = json.dumps({
        "choices": [{"message": {"content": "{\"y\": 2}"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 4},
    }).encode("utf-8")

    class _Resp:
        def read(self):
            return fake_body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    captured = {}

    def _capture(req, *a, **k):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return _Resp()

    p = OpenAICompatProvider(model="m", base_url="http://x/v1", api_key_env="UNSET_CUSTOM_KEY_9")
    with mock.patch("urllib.request.urlopen", side_effect=_capture):
        out = p.complete("system", "user")
    assert json.loads(out.text) == {"y": 2}
    assert out.input_tokens == 9 and out.output_tokens == 4
    assert captured["url"] == "http://x/v1/chat/completions"
    # the env var is unset -> no Authorization header is sent (fine for a local proxy)
    assert captured["auth"] is None


def test_custom_sends_bearer_when_key_env_is_set(monkeypatch):
    monkeypatch.setenv("PRESENT_CUSTOM_KEY", "sk-secret")
    fake_body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    class _Resp:
        def read(self):
            return fake_body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    captured = {}

    def _capture(req, *a, **k):
        captured["auth"] = req.headers.get("Authorization")
        return _Resp()

    p = OpenAICompatProvider(model="m", base_url="http://x/v1", api_key_env="PRESENT_CUSTOM_KEY")
    with mock.patch("urllib.request.urlopen", side_effect=_capture):
        p.complete("s", "u")
    assert captured["auth"] == "Bearer sk-secret"

"""The connection config (ADR-012) — which AI the user consciously connected.

Per default NOTHING is connected. There is no provider default, no silent
fallback: `recall power` with no connection refuses and points at `recall connect`.
The user chooses local (Ollama, free/offline) OR online (Anthropic, paid) — like
the connect-modal in the CMS, but as a file for now.

Security: the file stores only the NAME of the env var that holds the API key
(`api_key_env`), never the key itself in plaintext. The key stays in the
environment; we read it at call time, we never write it to disk.

One file: `~/.recall/connect.json`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

CONNECT_PATH = Path.home() / ".recall" / "connect.json"

# the providers a user may connect (ADR-012). EchoProvider (tests) is never
# written here — it isn't a user-connectable provider, only a test seam.
#   claude-cli — the Owner's main path: drive an already-installed agent CLI
#                (e.g. `claude`) as a subprocess. Uses the Max/Pro login the CLI
#                already holds — NO API key, no extra per-token spend.
#   ollama     — local model via Ollama. Free, offline, zero deps.
#   anthropic  — the Anthropic API. Paid, per token, needs an sk-ant key. NOT the
#                same as a claude.ai subscription (the API is billed separately).
#   custom     — any OpenAI-compatible endpoint (a proxy, OpenRouter, LM Studio,
#                vLLM, …). `base_url` is the endpoint; `api_key_env` is optional.
PROVIDERS = ("claude-cli", "ollama", "anthropic", "custom")


@dataclass(frozen=True)
class Connection:
    """A resolved connection. `api_key_env` is the env-var NAME (never the key).

    Per provider, the fields mean:
      claude-cli  model = the CLI command to run (e.g. "claude"); base_url unused;
                  no api_key_env (the CLI is already logged in).
      ollama      model = the local model; base_url = Ollama host override.
      anthropic   model = the API model; api_key_env = env var NAME holding sk-ant.
      custom      model = the model name; base_url = the OpenAI-compatible endpoint
                  (required); api_key_env = optional env var NAME for a bearer key.
    """

    provider: str  # "claude-cli" | "ollama" | "anthropic" | "custom"
    model: str
    base_url: str | None = None  # Ollama host / custom endpoint
    api_key_env: str | None = None  # e.g. "ANTHROPIC_API_KEY" — name only

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError(
                f"unknown provider {self.provider!r}; one of {PROVIDERS}"
            )
        if not self.model:
            raise ValueError("a connection needs a model")
        if self.provider == "custom" and not self.base_url:
            raise ValueError("a custom (OpenAI-compatible) connection needs a base_url")


def load_connection(path: Path | None = None) -> Connection | None:
    """The user's connection, or None if they never connected (the default).

    A malformed/partial file is treated as "no connection" rather than crashing —
    `recall connect` rewrites it cleanly. We never invent a default here (ADR-012).

    `path` defaults to the module-level CONNECT_PATH resolved at CALL time (not def
    time), so tests/embedders can redirect it by setting connect.CONNECT_PATH."""
    path = path if path is not None else CONNECT_PATH
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict) or "provider" not in raw or "model" not in raw:
        return None
    try:
        return Connection(
            provider=str(raw["provider"]),
            model=str(raw["model"]),
            base_url=raw.get("base_url") or None,
            api_key_env=raw.get("api_key_env") or None,
        )
    except ValueError:
        return None  # bad provider/model in the file -> treat as not connected


def save_connection(conn: Connection, path: Path | None = None) -> None:
    """Persist the connection. Writes only the env-var name, never the key."""
    path = path if path is not None else CONNECT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in asdict(conn).items() if v is not None}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def clear_connection(path: Path | None = None) -> bool:
    """Disconnect. Returns True if a connection existed and was removed."""
    path = path if path is not None else CONNECT_PATH
    if path.exists():
        path.unlink()
        return True
    return False

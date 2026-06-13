"""The LLM provider seam (ADR-012) — the ONE place recall reaches a model.

Sacred principle (power-mode-plan.md): the recall() read path stays LLM-free. Only
recall/power.py imports this module, only via the explicit `recall power` command.
A drift-guard (STEP 8) breaks the build if anything but power.py / llm.py imports an
LLM. This module itself is import-light: the Anthropic SDK is loaded lazily inside
AnthropicProvider so the base install never pulls an LLM dependency.

Providers (one per connectable AI in connect.PROVIDERS):
  - ClaudeCliProvider   — the Owner's main path. Drives an already-installed agent CLI
                          (e.g. `claude`) as a subprocess. Uses the CLI's existing login
                          (Max/Pro), so NO API key and no per-token spend. Pure stdlib.
  - OllamaProvider      — local, free, offline. Pure stdlib urllib, zero deps (ADR-003).
  - AnthropicProvider   — online, paid. Behind the optional [power] extra, imported lazily.
  - OpenAICompatProvider — any OpenAI-compatible endpoint (proxy/OpenRouter/LM Studio/vLLM).
                          Pure stdlib urllib. Optional bearer key from an env var.
  - EchoProvider        — tests only. Zero network. Returns canned stamp instructions and
                          asserts complete() is never called during a token estimate.

get_provider() reads the user's connection (recall/connect.py). No connection ->
a clear error pointing at `recall connect`, never a silent default (ADR-012).
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


def _http_complete(req, *, timeout: int, what: str) -> dict:
    """POST a urllib Request and return the parsed JSON body, turning every transport
    failure into a clean RuntimeError (mirrors ClaudeCliProvider). Without this a
    provider exception escapes as a raw traceback on the CLI power path (the dashboard
    worker already guards its own call). `what` names the provider for the message."""
    import urllib.request

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"{what} returned HTTP {e.code}: {detail or e.reason}") from e
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as e:
        reason = getattr(e, "reason", e)
        raise RuntimeError(f"{what} unreachable: {reason}") from e
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(f"{what} returned a non-JSON response") from e

from recall.connect import Connection, load_connection

# ---------------------------------------------------------------- cost table
# Rough $/token (input, output). Ollama is local => free. Anthropic prices are an
# ESTIMATE, clearly labelled as such by the CLI; a rules.md override can refine them
# later (plan open-decision #5). Per-million prices / 1_000_000.
_COST_PER_TOKEN: dict[str, tuple[float, float]] = {
    # model substring -> (input $/tok, output $/tok)
    "claude-opus-4-8": (5.00 / 1_000_000, 25.00 / 1_000_000),
    "claude-opus-4": (5.00 / 1_000_000, 25.00 / 1_000_000),
    "claude-sonnet-4": (3.00 / 1_000_000, 15.00 / 1_000_000),
    "claude-haiku-4": (1.00 / 1_000_000, 5.00 / 1_000_000),
}
_DEFAULT_ANTHROPIC_COST = (5.00 / 1_000_000, 25.00 / 1_000_000)  # unknown -> opus-tier


def cost_for(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimated USD for a model run. 0.0 for any local provider (model unknown to
    the table = treated as a paid Anthropic-tier estimate, never silently 0)."""
    rate = None
    for key, r in _COST_PER_TOKEN.items():
        if key in model:
            rate = r
            break
    if rate is None:
        rate = _DEFAULT_ANTHROPIC_COST
    return input_tokens * rate[0] + output_tokens * rate[1]


@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


@runtime_checkable
class LLMProvider(Protocol):
    """Every provider answers these two. count_tokens must NOT spend a completion."""

    name: str
    model: str
    cost_per_token: tuple[float, float]  # (input, output) $/tok; (0,0) for local

    def count_tokens(self, text: str) -> int: ...

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1024, schema: dict | None = None
    ) -> LLMResponse: ...


# --------------------------------------------------------------- Claude CLI
@dataclass
class ClaudeCliProvider:
    """The Owner's main path: drive an already-installed agent CLI (e.g. `claude`)
    as a subprocess. It uses the CLI's existing login (Max/Pro), so there is NO API
    key and no per-token spend — the subscription is already paid. Pure stdlib.

    `model` is the CLI *command* to run (the executable name or absolute path), not
    an API model id. We pass the prompt on stdin and read the completion on stdout in
    print-and-exit mode (`-p`/`--print`), which Claude Code and most agent CLIs honour.
    cost is (0,0): a subscription run costs no marginal tokens we can bill against."""

    model: str  # the CLI command, e.g. "claude" or r"C:\\...\\claude.cmd"
    base_url: str | None = None  # unused; kept for a uniform Connection shape
    name: str = field(default="claude-cli", init=False)
    cost_per_token: tuple[float, float] = field(default=(0.0, 0.0), init=False)
    # base flags for non-interactive use. `--print` makes the CLI emit the reply and
    # exit; `--output-format text` pins plain text (so we don't parse JSON envelopes).
    # The system prompt is passed separately via --system-prompt (see complete()), and
    # the user prompt goes on stdin — verified against `claude --help` and a live run.
    args: tuple[str, ...] = ("--print", "--output-format", "text")
    timeout: int = 180

    def count_tokens(self, text: str) -> int:
        # ~4 chars/token heuristic — only ever used for the estimate, and the estimate
        # is $0 for a subscription run anyway (cost_per_token is (0,0)).
        return max(1, len(text) // 4)

    def _resolve(self) -> str:
        """The CLI command, or a clear error if it isn't on PATH / not a real file."""
        import os.path
        import shutil

        # an absolute/relative path to a real file is used as-is; otherwise look on PATH.
        if os.path.sep in self.model or (os.path.altsep and os.path.altsep in self.model):
            if os.path.isfile(self.model):
                return self.model
        found = shutil.which(self.model)
        if found:
            return found
        raise RuntimeError(
            f"the CLI {self.model!r} was not found on PATH. Install your agent CLI "
            "(e.g. `npm i -g @anthropic-ai/claude-code` for `claude`) and make sure it "
            "is logged in, then connect again. recall never stores your login."
        )

    def _argv(self, cmd: str, system: str) -> list[str]:
        """The full argv. Passes the system prompt as a flag for known agent CLIs
        (claude/claude-code), else leaves it for the stdin fallback in complete()."""
        argv = [cmd, *self.args]
        base = os.path.basename(cmd).lower()
        if system and ("claude" in base):
            argv += ["--system-prompt", system]
        return argv

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1024, schema: dict | None = None
    ) -> LLMResponse:
        import subprocess

        cmd = self._resolve()
        # The CLI takes a system prompt as a flag and the user prompt on stdin. If the
        # CLI doesn't support --system-prompt, the system text still rides along on
        # stdin as a fallback (see _argv). The user prompt always goes on stdin.
        argv = self._argv(cmd, system)
        stdin = f"{system}\n\n{user}" if (system and "--system-prompt" not in argv) else user
        try:
            proc = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                encoding="utf-8",
            )
        except subprocess.TimeoutExpired as e:  # pragma: no cover - timing dependent
            raise RuntimeError(f"the CLI {self.model!r} timed out after {self.timeout}s") from e
        except OSError as e:  # pragma: no cover - exercised via the not-found path
            raise RuntimeError(f"could not run the CLI {self.model!r}: {e}") from e
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise RuntimeError(
                f"the CLI {self.model!r} exited {proc.returncode}: {tail or '(no output)'}"
            )
        text = (proc.stdout or "").strip()
        return LLMResponse(
            text=text,
            input_tokens=self.count_tokens(system + user),
            output_tokens=self.count_tokens(text),
        )


# ----------------------------------------------------------------- Ollama
@dataclass
class OllamaProvider:
    """Local model via Ollama's HTTP API — free, offline, zero deps (stdlib urllib)."""

    model: str
    base_url: str = "http://localhost:11434"
    name: str = field(default="ollama", init=False)
    cost_per_token: tuple[float, float] = field(default=(0.0, 0.0), init=False)

    def count_tokens(self, text: str) -> int:
        # Deterministic heuristic (~4 chars/token). Labelled as an estimate by the
        # CLI; Ollama has no cheap exact token endpoint, and it's free anyway.
        return max(1, len(text) // 4)

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1024, schema: dict | None = None
    ) -> LLMResponse:
        import urllib.request

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": {"num_predict": max_tokens},
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        body = _http_complete(
            req, timeout=120,
            what=f"Ollama at {self.base_url} (is `ollama serve` running?)",
        )
        # Ollama returns a 200 with {"error": ...} for some failures (e.g. model not
        # pulled) — surface that loudly instead of a silent empty completion.
        if body.get("error"):
            raise RuntimeError(f"Ollama error: {body['error']}")
        text = (body.get("message") or {}).get("content", "")
        return LLMResponse(
            text=text,
            input_tokens=body.get("prompt_eval_count", self.count_tokens(system + user)),
            output_tokens=body.get("eval_count", self.count_tokens(text)),
        )


# --------------------------------------------------------------- Anthropic
@dataclass
class AnthropicProvider:
    """Online model via the Anthropic SDK — paid. SDK imported lazily so the base
    install never depends on it (behind the optional [power] extra)."""

    model: str
    api_key_env: str = "ANTHROPIC_API_KEY"
    name: str = field(default="anthropic", init=False)

    def __post_init__(self) -> None:
        self.cost_per_token = next(
            (r for k, r in _COST_PER_TOKEN.items() if k in self.model),
            _DEFAULT_ANTHROPIC_COST,
        )

    def _client(self):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - exercised via import-guard test
            raise RuntimeError(
                "the Anthropic provider needs the [power] extra: "
                "pip install whatever-recall[power]"
            ) from e
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(
                f"no API key in ${self.api_key_env} — export it or `recall connect` "
                "with a different key env var"
            )
        return anthropic.Anthropic(api_key=key)

    def count_tokens(self, text: str) -> int:
        # Heuristic by default to stay offline-safe during estimation; the real
        # count_tokens endpoint can refine this once a client exists.
        return max(1, len(text) // 4)

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1024, schema: dict | None = None
    ) -> LLMResponse:
        client = self._client()
        msg = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(
            b.text for b in msg.content if getattr(b, "type", None) == "text"
        )
        usage = getattr(msg, "usage", None)
        return LLMResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", self.count_tokens(system + user)),
            output_tokens=getattr(usage, "output_tokens", self.count_tokens(text)),
        )


# ---------------------------------------------------------- OpenAI-compatible
@dataclass
class OpenAICompatProvider:
    """Any OpenAI-compatible endpoint — a proxy, OpenRouter, LM Studio, vLLM, etc.
    Pure stdlib urllib (no SDK). `base_url` is the endpoint root; we POST to
    `<base_url>/chat/completions`. A bearer key is read at call time from the env var
    NAMED by `api_key_env` (never stored). Without one, no Authorization header is sent
    (fine for a local proxy). Cost is unknown to us -> treated as a paid estimate."""

    model: str
    base_url: str  # e.g. "https://openrouter.ai/api/v1" or "http://localhost:1234/v1"
    api_key_env: str | None = None  # env var NAME holding a bearer key; optional
    name: str = field(default="custom", init=False)
    cost_per_token: tuple[float, float] = field(
        default=_DEFAULT_ANTHROPIC_COST, init=False
    )

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1024, schema: dict | None = None
    ) -> LLMResponse:
        import urllib.request

        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if key:
                headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        body = _http_complete(req, timeout=180, what=f"the endpoint at {url}")
        choices = body.get("choices") or [{}]
        text = ((choices[0].get("message") or {}).get("content")) or ""
        usage = body.get("usage") or {}
        return LLMResponse(
            text=text,
            input_tokens=usage.get("prompt_tokens", self.count_tokens(system + user)),
            output_tokens=usage.get("completion_tokens", self.count_tokens(text)),
        )


# ------------------------------------------------------------------- Echo
@dataclass
class EchoProvider:
    """Test seam — zero network. Returns canned responses, counts deterministically,
    and records every complete() call so a test can assert estimation spent nothing.

    `canned` is the default reply; `responses` (optional) is a queue consumed one per
    complete() call so an end-to-end test can give each hotspot a DISTINCT reply
    (without it, identical replies would dedup-merge into one node)."""

    canned: str = "{}"
    model: str = "echo"
    responses: list[str] | None = None
    name: str = field(default="echo", init=False)
    cost_per_token: tuple[float, float] = field(default=(0.0, 0.0), init=False)
    complete_calls: list[dict[str, Any]] = field(default_factory=list)

    def count_tokens(self, text: str) -> int:
        return max(1, len(text.split()))  # 1 token per word — exact + offline

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1024, schema: dict | None = None
    ) -> LLMResponse:
        idx = len(self.complete_calls)
        self.complete_calls.append({"system": system, "user": user})
        text = (
            self.responses[idx]
            if self.responses and idx < len(self.responses)
            else self.canned
        )
        return LLMResponse(
            text=text,
            input_tokens=self.count_tokens(system + " " + user),
            output_tokens=self.count_tokens(text),
        )


# --------------------------------------------------------------- resolution
def provider_from_connection(conn: Connection) -> LLMProvider:
    """Build the provider for an explicit connection (no I/O, no default)."""
    if conn.provider == "claude-cli":
        return ClaudeCliProvider(model=conn.model, base_url=conn.base_url)
    if conn.provider == "ollama":
        return OllamaProvider(
            model=conn.model,
            base_url=conn.base_url or "http://localhost:11434",
        )
    if conn.provider == "anthropic":
        return AnthropicProvider(
            model=conn.model,
            api_key_env=conn.api_key_env or "ANTHROPIC_API_KEY",
        )
    if conn.provider == "custom":
        if not conn.base_url:  # Connection.__post_init__ guards this, belt-and-braces
            raise RuntimeError("a custom connection needs a base_url")
        return OpenAICompatProvider(
            model=conn.model,
            base_url=conn.base_url,
            api_key_env=conn.api_key_env,
        )
    raise RuntimeError(f"unknown provider in connection: {conn.provider!r}")


def get_provider(conn: Connection | None = None) -> LLMProvider:
    """The connected provider — or a clear error, NEVER a silent default (ADR-012).

    Pass a Connection to bypass disk (tests/dry-run); otherwise read
    ~/.recall/connect.json. No connection -> raise with the connect hint."""
    conn = conn or load_connection()
    if conn is None:
        raise RuntimeError(
            "no AI connected — run `recall connect`: `--provider claude-cli --model claude` "
            "(your Max/Pro CLI, no key), `--provider ollama --model <m>` (local, free), "
            "`--provider anthropic --model <m>` (paid API), or `--provider custom "
            "--model <m> --base-url <url>` (OpenAI-compatible). Per default recall "
            "connects to nothing (ADR-012)."
        )
    return provider_from_connection(conn)

"""Adapter C — the HTTP bridge (web AI without an IDE).

Same engine, third mouth. A browser-based AI (Custom GPT, Claude connector,
Gemini extension) can't read your disk — so this tiny local server does, and only
the finished 3-level answer leaves the machine, never the code.

  POST /recall  {query, intent?, edit_context?, topk?}  -> 3-level JSON
  POST /stamp   {title, body?, anchors?, tags?, file?}   -> {action, ...}
  POST /init    {path, max_commits?}                      -> bootstrap stats
  GET  /stats                                             -> index stats
  GET  /healthz                                           -> {ok: true}

Security posture (the website-as-gatekeeper hook, per the plan):
  - binds localhost by default;
  - if RECALL_BRIDGE_TOKEN is set, every mutating/reading route requires
    `Authorization: Bearer <token>` — this is where the website's plan/token
    check clamps on for the tunnel case. No token set = open localhost (dev).

Run:  uvicorn adapters.server:app --host 127.0.0.1 --port 7077
   or python -m adapters.server  (uses RECALL_REPO / cwd)

FastAPI/uvicorn are optional extras (`pip install whatever-recall[bridge]`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import hmac

try:
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover - import guard
    raise SystemExit(
        "The HTTP bridge needs FastAPI. Install it with:\n"
        "    pip install whatever-recall[bridge]"
    ) from exc

from recall.cli import _find_repo, _index_path
from recall.engine import Index

# The repo this bridge serves. Default: walk up from cwd (or RECALL_REPO).
_REPO = _find_repo(os.environ.get("RECALL_REPO", "."))
# Empty/whitespace token -> treat as unset (avoids a "looks locked but isn't"
# state where `Authorization: Bearer ` would pass).
_TOKEN = (os.environ.get("RECALL_BRIDGE_TOKEN") or "").strip() or None

app = FastAPI(title="whatever-recall bridge", version="0.1.0")

# No browser origin should script the local bridge (shuts the DNS-rebinding
# surface against 127.0.0.1). Web AIs reach it server-side via the tunnel, not
# from a page's JS, so an empty allow-list is correct.
app.add_middleware(
    CORSMiddleware, allow_origins=[], allow_methods=["*"], allow_headers=["*"],
)


def _index(repo: Path | None = None) -> Index:
    target = repo or _REPO
    idx_path = _index_path(target)
    if not idx_path.exists():
        raise HTTPException(404, f"no index in {target} — run `recall init` first")
    return Index.open(idx_path, repo=target)


def _auth(authorization: str | None = Header(default=None)) -> None:
    """Gatekeeper hook. No token configured -> open (localhost dev). Token set ->
    Bearer required. This is where the website's plan/token check plugs in."""
    if _TOKEN is None:
        return
    expected = f"Bearer {_TOKEN}"
    # Constant-time compare so the token can't be recovered via response timing.
    if not hmac.compare_digest(authorization or "", expected):
        raise HTTPException(401, "missing or invalid bearer token")


class RecallReq(BaseModel):
    query: str
    intent: str | None = None
    edit_context: str | None = None
    topk: int = Field(default=3, ge=1, le=50)


class StampReq(BaseModel):
    title: str
    body: str | None = None
    anchors: list[str] | None = None
    tags: list[str] | None = None
    file: str | None = None


class InitReq(BaseModel):
    # Optional: must resolve INSIDE the served repo. Omit to (re)init _REPO itself.
    path: str | None = None
    max_commits: int = Field(default=400, ge=1, le=10000)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "repo": str(_REPO)}


@app.post("/recall")
def recall(req: RecallReq, _: None = Depends(_auth)) -> dict[str, Any]:
    return _index().recall(
        req.query, intent=req.intent, edit_context=req.edit_context,
        topk=req.topk, consumer="bridge",
    )


@app.post("/stamp")
def stamp(req: StampReq, _: None = Depends(_auth)) -> dict[str, Any]:
    return _index().stamp(
        title=req.title, body=req.body, anchors=req.anchors, tags=req.tags,
        file_path=req.file, origin="live",
    )


@app.post("/init")
def init(req: InitReq, _: None = Depends(_auth)) -> dict[str, Any]:
    from recall.bootstrap import init as bootstrap_init

    # Boundary: the bridge serves exactly one repo. A client cannot point the
    # indexer at an arbitrary directory (filesystem walk / arbitrary write).
    served = _REPO.resolve()
    if req.path is None:
        repo = served
    else:
        repo = Path(req.path).resolve()
        if repo != served and served not in repo.parents:
            raise HTTPException(403, "path must be inside the served repo")
    idx = Index.open(_index_path(repo), repo=repo)
    return bootstrap_init(idx, repo, max_commits=req.max_commits)


@app.get("/stats")
def stats(_: None = Depends(_auth)) -> dict[str, Any]:
    return _index().stats()


def main() -> int:
    import uvicorn

    host = os.environ.get("RECALL_BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("RECALL_BRIDGE_PORT", "7077"))
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

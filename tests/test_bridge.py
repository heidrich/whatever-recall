"""adapters/server.py — the HTTP bridge: routes + token gate."""

import importlib

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from recall import Index  # noqa: E402
from recall.cli import _index_path  # noqa: E402


def _seed(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / ".git").mkdir()
    idx = Index.open(_index_path(repo), repo=repo)
    idx.stamp(
        title="RLS cutover writers set workspace_id",
        body="insert path must set workspace_id",
        anchors=["rls_cutover", "workspace_id", "insert", "tenancy", "uploads"],
        tags=["security"],
        sha="a1b2c3d",
    )
    return repo


def _client(tmp_path, monkeypatch, token=None):
    repo = _seed(tmp_path)
    monkeypatch.setenv("RECALL_REPO", str(repo))
    if token:
        monkeypatch.setenv("RECALL_BRIDGE_TOKEN", token)
    else:
        monkeypatch.delenv("RECALL_BRIDGE_TOKEN", raising=False)
    import adapters.server as server
    importlib.reload(server)  # pick up the env for this test
    return TestClient(server.app), repo


def test_healthz(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_recall_returns_three_levels(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.post("/recall", json={"query": "rls cutover workspace_id uploads"})
    assert r.status_code == 200
    body = r.json()
    assert not body["silenced"]
    assert "workspace_id" in body["results"][0]["matched_anchors"]


def test_recall_silences_nonsense(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.post("/recall", json={"query": "wie ist das wetter in berlin"})
    assert r.json()["silenced"] is True


def test_stamp_then_recall(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    s = client.post("/stamp", json={
        "title": "Stripe webhook idempotency",
        "anchors": ["stripe", "webhook", "idempotency", "unique-index"],
        "tags": ["backend"],
    })
    assert s.status_code == 200 and s.json()["action"] == "NEW"
    r = client.post("/recall", json={"query": "stripe webhook idempotency unique"})
    assert not r.json()["silenced"]


def test_token_gate_blocks_without_bearer(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, token="s3cret")
    assert client.post("/recall", json={"query": "rls cutover"}).status_code == 401
    ok = client.post("/recall", json={"query": "rls cutover workspace_id"},
                     headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_stats(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/stats")
    assert r.status_code == 200 and r.json()["nodes"] >= 1

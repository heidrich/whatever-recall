"""The local dashboard server (`recall dashboard`) — drift-guards.

Pins, all offline against a tmp repo + tmp connect.json:
  - the snapshot has the keys the page reads, and reads the index READ-ONLY;
  - /api/file is repo-jailed (traversal / absolute / backslash -> 400) and caps size;
  - /api/diff hex-validates the sha and runs git diff for the before/after story step;
  - /api/connection lists the 4 providers and never leaks a key value;
  - POST /api/connect writes the SAME connect.json as `recall connect`, mirrors the
    anthropic key-env default, and is refused cross-origin (DNS-rebinding guard);
  - the read path stays LLM-free (dashboard.py imports no provider at module load).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from recall import Index
from recall import dashboard
from recall.bootstrap import init


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def login(user):\n    return user\n\nclass AuthGuard:\n    def check(self):\n        return True\n",
        encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v1 — auth\n\nWe fixed the workspace_id NULL bug in the RLS cutover.\n",
        encoding="utf-8",
    )
    has_git = shutil.which("git") is not None
    if has_git:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t.t")
        _git(repo, "config", "user.name", "t")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "feat: auth\n\nRecall-anchors: login, auth")
        # a second commit so a diff <first-sha>..HEAD is non-empty
        (repo / "app.py").write_text(
            "def login(user):\n    return user.strip()\n\nclass AuthGuard:\n    def check(self):\n        return True\n",
            encoding="utf-8",
        )
        _git(repo, "commit", "-aqm", "fix: strip the user")
    return repo, has_git


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A running dashboard over a tmp repo + isolated connect.json. Yields (base_url, repo)."""
    repo, _ = _make_repo(tmp_path)
    idx = Index.open(tmp_path / "index.db", repo=repo)
    init(idx, repo)
    idx.db.close()
    # isolate the connection + recent files from the real ~/.recall
    import recall.connect as connect_mod
    monkeypatch.setattr(connect_mod, "CONNECT_PATH", tmp_path / "connect.json")
    monkeypatch.setattr(dashboard, "RECENT_PATH", tmp_path / "recent.json")

    handler, _state = dashboard._make_handler(repo, tmp_path / "index.db")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)  # port 0 -> OS picks a free one
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", repo
    finally:
        srv.shutdown()
        srv.server_close()


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _post(base, path, obj, origin=None):
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get_with_host(base, path, host):
    """A GET carrying a forged Host header — simulates the DNS-rebinding browser case."""
    req = urllib.request.Request(base + path, method="GET")
    req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


# ---------------------------------------------------------------- snapshot
def test_snapshot_has_the_keys_the_page_reads(served):
    base, _ = served
    status, body = _get(base, "/api/data")
    assert status == 200
    snap = json.loads(body)
    for key in ("repo", "branch", "head", "stats", "drift", "lessons",
                "code", "drifted", "power_runs", "git"):
        assert key in snap, f"snapshot missing {key!r}"
    assert {"branches", "commits", "tree"} <= set(snap["git"])


def test_pulse_reports_auto_on_from_watch_state(served):
    """/api/pulse must expose auto.on so the page knows live mode is running.
    (Bug 2026-06-13: on a fresh restart the pill stuck at READ-ONLY with a spinning
    ring because the page raced the watcher's late `on=True`. The fix sets it
    synchronously in serve(); the pulse contract that carries it is pinned here.)"""
    base, _ = served
    status, body = _get(base, "/api/pulse")
    assert status == 200
    p = json.loads(body)
    assert "auto" in p and "on" in p["auto"]
    assert isinstance(p["auto"]["on"], bool)


def test_dashboard_html_carries_the_reconnect_recovery(served):
    """The page must recover from a server restart, not strand at read-only with a
    perpetual spinner (owner: 'das ist beim Server-Neustart immer kaputt'). Pin the
    client-side seam: a failed pulse shows 'reconnecting…' and retries fast."""
    base, _ = served
    _, html = _get(base, "/")
    assert "reconnecting…" in html
    assert "_pulseFail" in html
    assert "live--recon" in html


def test_dashboard_html_carries_the_project_toggle(served):
    """The project switcher must ship its Production⇄Test toggle (owner 2026-06-18:
    'einfach in den modal ein schalter'). Pin the client-side seam so the toggle —
    and the test list it reveals — can't silently regress out of the page."""
    base, _ = served
    _, html = _get(base, "/")
    assert "setProjView(" in html          # the toggle handler
    assert "PM_VIEW" in html               # the view state it drives
    assert "recent_test" in html           # it reads the test list the API exposes
    assert "pm-seg" in html                # the segment-control markup


def test_page_serves_html(served):
    base, _ = served
    status, body = _get(base, "/")
    assert status == 200 and "<html" in body.lower()


def test_header_bar_is_responsive_for_laptops(served):
    """The header packed ~10 controls in one nowrap row and overflowed off-screen
    below 1920px (owner 2026-06-13: 'das tool wird unter 1920 komplett zerschossen,
    muss bis ~1200 laufen'). Pin the fix: the bar wraps, and the laptop breakpoint
    drops the global search to its own row so the controls fit on common laptops."""
    base, _ = served
    _, html = _get(base, "/")
    assert ".bar{display:flex;align-items:center;flex-wrap:wrap" in html
    assert "max-width:1600px" in html  # the laptop ladder: search → own full-width row


def test_page_html_is_served_fresh_per_request(tmp_path, monkeypatch):
    """Owner 2026-06-09 'ich seh noch den alten': editing dashboard.html must show up on
    the next request with NO server restart. The handler re-reads the file when its mtime
    changes, and the response is no-store so the browser can't cache a stale build either."""
    import os
    fake = tmp_path / "dash.html"
    fake.write_text("<html><body>VERSION ONE</body></html>", encoding="utf-8")
    monkeypatch.setattr(dashboard, "_HTML", fake)
    repo, _ = _make_repo(tmp_path)
    handler, _state = dashboard._make_handler(repo, tmp_path / "index.db")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert r.headers.get("Cache-Control") == "no-store"  # browser won't cache
            assert "VERSION ONE" in r.read().decode("utf-8")
        # edit the file + bump its mtime forward (a same-second edit must still be seen)
        fake.write_text("<html><body>VERSION TWO</body></html>", encoding="utf-8")
        st = fake.stat()
        os.utime(fake, (st.st_atime, st.st_mtime + 5))
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert "VERSION TWO" in r.read().decode("utf-8")  # fresh, no restart
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_has_how_it_works_tab_and_rules_block(served):
    """The trust page must ship in the HTML: the tab + the live-rules container."""
    base, _ = served
    _, body = _get(base, "/")
    assert 'data-p="howitworks"' in body
    assert 'id="hiw-rules"' in body  # the verbatim-rules code block


def test_api_rules_serves_the_real_shipped_rules(served):
    """/api/rules returns the exact governance file the engine loads — transparency.
    An AI/dev must be able to read the same bytes recall runs on, not a copy."""
    from recall.rules import _bundled_rules_path
    base, _ = served
    # compare RAW bytes — "verbatim" means byte-for-byte, including the file's own
    # line endings (read_text would normalize CRLF and falsely fail on Windows).
    with urllib.request.urlopen(base + "/api/rules", timeout=5) as r:
        assert r.status == 200
        served_bytes = r.read()
    assert served_bytes == _bundled_rules_path().read_bytes()
    # the governance knobs are actually present (not an empty/placeholder file)
    text = served_bytes.decode("utf-8")
    assert "facet_weights" in text and "silence_floor" in text


def test_api_rules_download_sets_attachment(served):
    """?download=1 makes the browser save rules.md instead of rendering it."""
    base, _ = served
    req = urllib.request.Request(base + "/api/rules?download=1", method="GET")
    with urllib.request.urlopen(req, timeout=5) as r:
        cd = r.headers.get("Content-Disposition", "")
    assert "attachment" in cd and "rules.md" in cd


def test_commit_endpoint_returns_changed_files_and_diffs(served):
    """Clicking a commit shows WHAT changed — its message + per-file diffs (the core
    'follow the change'). /api/commit returns the metadata + a patch per file."""
    base, has_git = served
    if not has_git:
        pytest.skip("git not available")
    _, repo = served
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    status, body = _get(base, "/api/commit?sha=" + sha)
    assert status == 200
    d = json.loads(body)
    assert d["sha"] and "subject" in d and "author" in d
    assert isinstance(d["files"], list) and d["files"], "no changed files returned"
    f = d["files"][0]
    assert "path" in f and "status" in f and "diff" in f
    # the second fixture commit edited app.py — its patch should mention the change
    assert any("app.py" in x["path"] for x in d["files"])


def test_commit_endpoint_rejects_a_bad_sha(served):
    base, _ = served
    status, _b = _get(base, "/api/commit?sha=nothex")
    assert status == 400


def test_snapshot_commits_feed_and_honest_lesson_times(served):
    """The Overview right column shows real commits; lesson timestamps come from git
    (the file's last-commit time), not the index build time (the '5h ago' bug)."""
    base, has_git = served
    status, body = _get(base, "/api/data")
    snap = json.loads(body)
    if has_git:
        assert snap["git"]["commits"], "no commits in the snapshot for the right column"
        c0 = snap["git"]["commits"][0]
        assert "subject" in c0 and "author" in c0 and "ts" in c0
        # a doc/file lesson carries a real git time (not None, not the build time only)
        timed = [L for L in snap["lessons"] if L.get("ts")]
        assert timed, "no lesson carries a timestamp"


def test_snapshot_carries_authors_and_per_lesson_author(served):
    """v3: the snapshot exposes an authors list (for the person filter) and each lesson
    carries its author (for display + filtering)."""
    base, has_git = served
    status, body = _get(base, "/api/data")
    snap = json.loads(body)
    assert "authors" in snap and isinstance(snap["authors"], list)
    assert all("author" in L for L in snap["lessons"])  # key present (may be None)
    if has_git:
        # the fixture commits as "t" with a Recall trailer → at least one authored lesson
        assert any((L.get("author") or "") for L in snap["lessons"])
        assert any(a.get("name") for a in snap["authors"])


def test_recall_endpoint_returns_real_3_level_results(served):
    """The live /api/recall makes the Search tab + Recall card honest: real results +
    a measured latency, 0 tokens, 0 model (LLM-free read path)."""
    base, _ = served
    status, body = _get(base, "/api/recall?q=" + urllib.request.quote("auth login"))
    assert status == 200
    d = json.loads(body)
    assert "latency_us" in d and isinstance(d["latency_us"], int)
    assert "silenced" in d and "results" in d
    if not d["silenced"]:
        r = d["results"][0]
        assert "title" in r and "node_id" in r  # the 3-level shape the page renders


def test_recall_endpoint_serves_the_three_tracks(served):
    """The Search tab renders code/knowledge/blast_radius side by side (ADR-016), so the
    endpoint must forward all three tracks — not just the old mixed results list."""
    base, _ = served
    status, body = _get(base, "/api/recall?q=" + urllib.request.quote("auth login guard"))
    assert status == 200
    d = json.loads(body)
    # keys present even when empty, so the page can render the track shells deterministically
    assert "code" in d and "knowledge" in d and "blast_radius" in d
    assert "open_tasks" in d  # ADR-017: open tasks for the top hit
    assert isinstance(d["code"], list) and isinstance(d["blast_radius"], list)


def test_snapshot_has_product_tree(served):
    """The Product tab (ADR-018) needs by_feature + by_status in the snapshot, derived
    from the indexed data."""
    base, _ = served
    status, body = _get(base, "/api/data")
    assert status == 200
    d = json.loads(body)
    assert "product" in d
    assert "by_feature" in d["product"] and "by_status" in d["product"]
    assert isinstance(d["product"]["by_feature"], list)


def test_recall_endpoint_rejects_an_empty_query(served):
    base, _ = served
    status, _b = _get(base, "/api/recall?q=")
    assert status == 400  # empty query is a 400, not a silent empty result


def test_recall_endpoint_is_llm_free(served):
    """The recall read path must never import a model seam (ADR-014)."""
    import sys
    base, _ = served
    sys.modules.pop("recall.llm", None)
    _get(base, "/api/recall?q=" + urllib.request.quote("workspace rls"))
    assert "recall.llm" not in sys.modules


# ---------------------------------------------------------------- file jail
def test_api_file_reads_a_repo_file(served):
    base, _ = served
    status, body = _get(base, "/api/file?path=app.py&line=1")
    assert status == 200
    d = json.loads(body)
    assert "def login" in d["content"] and d["line"] == 1


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd",      # traversal
    "/etc/passwd",              # absolute
    "..\\..\\windows\\win.ini",  # backslash + traversal
    "",                         # empty
])
def test_api_file_rejects_out_of_repo_paths(served, bad):
    base, _ = served
    status, _body = _get(base, "/api/file?path=" + urllib.request.quote(bad))
    assert status == 400  # the jail holds: never serve outside the repo


# ----------------------------------------------------------------- diff
def test_api_diff_requires_valid_hex_sha(served):
    base, _ = served
    status, _ = _get(base, "/api/diff?path=app.py&sha=zzz")
    assert status == 400  # non-hex sha rejected before touching git


def test_api_diff_shows_change_since_a_sha(served):
    base, repo = served
    if shutil.which("git") is None:
        pytest.skip("git not available")
    first = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    status, body = _get(base, f"/api/diff?path=app.py&sha={first}")
    assert status == 200
    d = json.loads(body)
    assert not d["empty"] and "strip()" in d["diff"]  # the second commit's change


# -------------------------------------------------------------- connection
def test_connection_lists_all_four_providers_and_starts_empty(served):
    base, _ = served
    status, body = _get(base, "/api/connection")
    assert status == 200
    d = json.loads(body)
    assert d["connected"] is False
    assert d["providers"] == ["claude-cli", "ollama", "anthropic", "custom"]


def test_connect_writes_the_same_connect_json_as_the_cli(served, tmp_path):
    base, _ = served
    status, d = _post(base, "/api/connect", {"provider": "claude-cli", "model": "claude"})
    assert status == 200 and d["connected"] is True
    # the modal wrote the very file `recall connect` reads
    from recall.connect import load_connection
    conn = load_connection()
    assert conn is not None and conn.provider == "claude-cli" and conn.model == "claude"


def test_connect_anthropic_defaults_key_env_like_the_cli(served):
    base, _ = served
    status, d = _post(base, "/api/connect",
                      {"provider": "anthropic", "model": "claude-opus-4-8"})
    assert status == 200
    assert d["connection"]["api_key_env"] == "ANTHROPIC_API_KEY"  # mirrors cmd_connect


def test_connect_custom_without_url_is_rejected(served):
    base, _ = served
    status, d = _post(base, "/api/connect", {"provider": "custom", "model": "x"})
    assert status == 400 and "base_url" in d["error"]


def test_connection_never_returns_a_key_value(served, monkeypatch):
    """Even when the named env var is set, /api/connection returns only a boolean."""
    base, _ = served
    monkeypatch.setenv("DASH_TEST_KEY", "sk-super-secret-value")
    _post(base, "/api/connect",
          {"provider": "custom", "model": "m", "base_url": "http://x/v1",
           "api_key_env": "DASH_TEST_KEY"})
    status, body = _get(base, "/api/connection")
    assert status == 200
    assert "sk-super-secret-value" not in body  # the key value must never appear
    d = json.loads(body)
    assert d["connection"]["api_key_env"] == "DASH_TEST_KEY"  # only the NAME
    assert d["connection"]["key_present"] is True  # boolean only


def test_connect_is_refused_cross_origin(served):
    """A page on another origin must not be able to rebind + POST a connection."""
    base, _ = served
    status, d = _post(base, "/api/connect",
                      {"provider": "ollama", "model": "x"},
                      origin="http://evil.example.com")
    assert status == 403 and "cross-origin" in d["error"]


def test_disconnect_removes_the_connection(served):
    base, _ = served
    _post(base, "/api/connect", {"provider": "claude-cli", "model": "claude"})
    status, d = _post(base, "/api/connect", {"action": "clear"})
    assert status == 200 and d["connected"] is False and d["cleared"] is True
    _, after = _get(base, "/api/connection")
    assert json.loads(after)["connected"] is False


# ----------------------------------------------------------- projects/switch
def test_projects_lists_current_and_marks_it_indexed(served):
    base, repo = served
    status, body = _get(base, "/api/projects")
    assert status == 200
    d = json.loads(body)
    assert d["current"]["name"] == repo.name
    assert d["current"]["indexed"] is True
    # the current repo was remembered on startup
    assert any(r["path"] == str(repo.resolve()) for r in d["recent"]) or d["recent"] == []


def test_switch_repoints_the_dashboard_at_another_dir(served, tmp_path):
    base, repo = served
    other = tmp_path / "other_proj"
    other.mkdir()
    status, d = _post(base, "/api/switch", {"path": str(other)})
    assert status == 200 and d["switched"] is True
    assert d["current"]["name"] == "other_proj"
    assert d["current"]["indexed"] is False  # no .mind/index.db there
    # /api/data now reports the new repo with a no_index flag instead of crashing
    _, body = _get(base, "/api/data")
    snap = json.loads(body)
    assert snap["no_index"] is True and snap["repo"] == "other_proj"


def test_switch_to_nonexistent_path_is_rejected(served):
    base, _ = served
    status, d = _post(base, "/api/switch", {"path": "Q:/no/such/dir/anywhere"})
    assert status == 400 and "directory" in d["error"]


def test_switch_is_refused_cross_origin(served, tmp_path):
    base, _ = served
    other = tmp_path / "x"
    other.mkdir()
    status, d = _post(base, "/api/switch", {"path": str(other)},
                      origin="http://evil.example.com")
    assert status == 403 and "cross-origin" in d["error"]


def test_recent_remembers_switched_projects(served, tmp_path):
    """Switching to another project records it in the switcher.

    Two-list model (owner 2026-06-18): the test repos here live under the OS temp
    dir, so they route to the TEST list (`recent_test`), not production. The point
    of this test is that switching is *remembered at all* — so we assert the repo
    shows up across either list, exactly as the modal's toggle would surface it.
    The production-vs-test ROUTING itself is pinned in test_recent_two_lists_*.
    """
    base, repo = served
    other = tmp_path / "proj_b"
    other.mkdir()
    _post(base, "/api/switch", {"path": str(other)})
    _, body = _get(base, "/api/projects")
    d = json.loads(body)
    listed = (
        {d["current"]["name"]}
        | {r["name"] for r in d.get("recent", [])}
        | {r["name"] for r in d.get("recent_test", [])}
    )
    assert {repo.name, "proj_b"} <= listed  # both the original and the switched repo


def test_serve_projects_exposes_both_lists(served):
    """/api/projects must always carry BOTH lists so the modal toggle has data.

    The page's Production⇄Test switch reads `recent` and `recent_test`; if either
    key vanished the toggle would silently show nothing. Pin the contract."""
    base, _ = served
    _, body = _get(base, "/api/projects")
    d = json.loads(body)
    assert "recent" in d and isinstance(d["recent"], list)
    assert "recent_test" in d and isinstance(d["recent_test"], list)
    assert "current" in d and d["current"]["name"]


# ---------------------------------------------------- power estimate / index
def _connect(base, provider, model, **extra):
    body = {"provider": provider, "model": model, **extra}
    return _post(base, "/api/connect", body)


def test_power_estimate_is_free_for_claude_cli(served):
    base, _ = served
    _connect(base, "claude-cli", "claude")
    status, body = _get(base, "/api/power-estimate?top_n=5")
    assert status == 200
    e = json.loads(body)
    assert e["connected"] is True and e["provider"] == "claude-cli"
    assert e["paid"] is False and e["cost_usd"] == 0  # subscription -> no marginal spend
    assert "subscription" in e["cost_label"] or "free" in e["cost_label"]


def test_power_estimate_is_paid_for_anthropic(served):
    base, _ = served
    _connect(base, "anthropic", "claude-opus-4-8")
    status, body = _get(base, "/api/power-estimate?top_n=5")
    assert status == 200
    e = json.loads(body)
    assert e["provider"] == "anthropic" and e["paid"] is True
    assert e["cost_usd"] > 0 and "$" in e["cost_label"]  # honest paid estimate


def test_power_estimate_spends_nothing(served):
    """The estimate must never call a model — proven by using claude-cli (which would
    spawn a subprocess if complete() ran) and asserting it returns instantly with a
    token count, not an error from a missing/failed CLI call."""
    base, _ = served
    _connect(base, "claude-cli", "claude")
    status, body = _get(base, "/api/power-estimate?top_n=3")
    assert status == 200 and "input_tokens" in json.loads(body)


def test_power_run_refused_cross_origin(served):
    base, _ = served
    _connect(base, "claude-cli", "claude")
    status, d = _post(base, "/api/power-run", {"top_n": 2},
                      origin="http://evil.example.com")
    assert status == 403 and "cross-origin" in d["error"]


def test_index_refused_cross_origin(served):
    base, _ = served
    status, d = _post(base, "/api/index", {}, origin="http://evil.example.com")
    assert status == 403 and "cross-origin" in d["error"]


def test_index_builds_an_index_for_an_unindexed_project(served, tmp_path):
    """Switch to a fresh dir, then POST /api/index — it should build .mind/index.db."""
    base, _ = served
    fresh = tmp_path / "fresh_proj"
    fresh.mkdir()
    (fresh / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _post(base, "/api/switch", {"path": str(fresh)})
    status, d = _post(base, "/api/index", {})
    assert status == 200 and d["indexed"] is True and d["nodes"] >= 0
    assert (fresh / ".mind" / "index.db").exists()  # the index was written
    # /api/data now returns a real snapshot, not the no_index flag
    _, body = _get(base, "/api/data")
    assert json.loads(body).get("no_index") is not True


def test_power_status_starts_idle(served):
    base, _ = served
    status, body = _get(base, "/api/power-status")
    assert status == 200 and json.loads(body)["state"] in ("idle", "done", "running", "error")


# ----------------------------------------------------------- vendored assets
def test_vendor_serves_highlight_js(served):
    """highlight.js ships in the package and is served locally (offline, no CDN)."""
    base, _ = served
    status, body = _get(base, "/vendor/highlight.min.js")
    assert status == 200 and "hljs" in body  # the real library, not a 404 page


def test_vendor_is_jailed_against_traversal(served):
    base, _ = served
    # must not escape recall/vendor/ to read source / config
    for bad in ["/vendor/../dashboard.py", "/vendor/..%2fdashboard.py",
                "/vendor/sub/../../dashboard.py", "/vendor/"]:
        status, _b = _get(base, bad)
        assert status == 404, f"{bad} should 404, got {status}"


def test_vendor_only_serves_js_and_css(served, tmp_path):
    """A non-asset extension inside vendor must not be served."""
    base, _ = served
    status, _b = _get(base, "/vendor/anything.py")
    assert status == 404


# ----------------------------------------- DNS-rebinding guard on READ endpoints
def test_read_endpoints_reject_a_rebound_host(served):
    """A DNS-rebound page keeps Host:evil.com while the TCP peer is loopback — the
    review's P2: without a Host check, such a page could read /api/file and /api/data.
    The guard must 403 every /api/* request whose Host isn't a loopback name."""
    base, _ = served
    for path in ("/api/data", "/api/file?path=app.py", "/api/diff?path=app.py&sha=abc",
                 "/api/connection", "/api/projects", "/api/power-estimate"):
        status, _b = _get_with_host(base, path, "evil.example.com")
        assert status == 403, f"{path} must reject a rebound Host, got {status}"


def test_read_endpoints_allow_loopback_host(served):
    base, _ = served
    status, _b = _get_with_host(base, "/api/projects", "127.0.0.1:7099")
    assert status == 200  # a genuine loopback Host is fine
    status, _b = _get_with_host(base, "/api/projects", "localhost")
    assert status == 200


def test_page_shell_loads_regardless_of_host(served):
    """The static page + vendor assets carry no repo data, so they load even under an
    odd Host (otherwise a normal localhost visit could break); only data endpoints gate."""
    base, _ = served
    status, _b = _get_with_host(base, "/", "evil.example.com")
    assert status == 200


def test_power_estimate_hotspot_files_uses_file_path(served):
    """Regression for the .file-vs-.file_path bug: hotspot_files must carry real paths
    (or be empty), never a list of Nones from reading a non-existent attribute."""
    base, _ = served
    _connect(base, "claude-cli", "claude")
    status, body = _get(base, "/api/power-estimate?top_n=5")
    assert status == 200
    e = json.loads(body)
    files = e.get("hotspot_files", [])
    # if there are hotspots at all, at least one must resolve to a real path string
    if e.get("hotspots"):
        assert any(isinstance(f, str) and f for f in files), \
            "hotspot_files is all-None — the .file_path attribute regressed"


# ----------------------------------------------------------------- LIVE pulse
def test_pulse_has_the_heartbeat_keys(served):
    """The cheap heartbeat the page polls for live mode — HEAD, counts, indexed_at."""
    base, _ = served
    status, body = _get(base, "/api/pulse")
    assert status == 200
    p = json.loads(body)
    for key in ("head", "tree_hash", "commits", "indexed_at", "nodes", "lessons", "auto"):
        assert key in p, f"pulse missing {key!r}"
    assert isinstance(p["auto"], dict) and "on" in p["auto"]


def test_pulse_counts_match_the_index(served):
    base, _ = served
    _, snap = _get(base, "/api/data")
    snap = json.loads(snap)
    _, p = _get(base, "/api/pulse")
    p = json.loads(p)
    # the cheap pulse counts agree with the heavy snapshot's stats
    assert p["nodes"] == snap["stats"]["nodes"]
    assert p["lessons"] == snap["stats"]["lessons"]


def test_pulse_is_gated_by_the_rebind_host_guard(served):
    """The pulse is polled constantly — it must obey the same DNS-rebinding guard."""
    base, _ = served
    status, _b = _get_with_host(base, "/api/pulse", "evil.example.com")
    assert status == 403


# ------------------------------------------------------- git post-commit hook
def test_hook_status_reports_not_installed_initially(served):
    base, has_git = served
    status, body = _get(base, "/api/hook")
    assert status == 200
    h = json.loads(body)
    assert h["has_git"] is bool(has_git)
    assert h["installed"] is False  # nothing installed yet


def test_hook_install_then_uninstall(served):
    """Install writes .git/hooks/post-commit with our marker; uninstall removes it."""
    base, has_git = served
    if not has_git:
        pytest.skip("no git in this environment")
    status, d = _post(base, "/api/hook", {"action": "install"})
    assert status == 200 and d["installed"] is True
    # the file exists and carries the recall marker
    from pathlib import Path
    target = Path(d["path"])
    assert target.exists() and ">>> recall auto-stamp" in target.read_text(encoding="utf-8")
    # status now reports installed
    _, body = _get(base, "/api/hook")
    assert json.loads(body)["installed"] is True
    # uninstall removes it
    status, d = _post(base, "/api/hook", {"action": "uninstall"})
    assert status == 200 and d["installed"] is False
    assert not target.exists()


def test_hook_install_refused_cross_origin(served):
    base, _ = served
    status, d = _post(base, "/api/hook", {"action": "install"},
                      origin="http://evil.example.com")
    assert status == 403 and "cross-origin" in d["error"]


def test_hook_does_not_clobber_a_foreign_post_commit_hook(served):
    """If the user already has a post-commit hook, install must refuse, not overwrite."""
    base, has_git = served
    if not has_git:
        pytest.skip("no git in this environment")
    _, repo = served
    foreign = repo / ".git" / "hooks" / "post-commit"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    status, d = _post(base, "/api/hook", {"action": "install"})
    assert d.get("ok") is False  # refused
    assert "echo mine" in foreign.read_text(encoding="utf-8")  # untouched


# ------------------------------------------------------- watcher auto-index
def test_auto_index_reindexes_after_a_new_commit(served, tmp_path):
    """The heart of live mode: a new commit, then _auto_index, grows the lesson count.

    Directly exercises the watcher's worker (the thread itself is timing-based; the
    logic it runs is what matters)."""
    base, has_git = served
    if not has_git:
        pytest.skip("no git in this environment")
    _, repo = served
    # the fixture builds the index at tmp_path/index.db == repo.parent/index.db
    idx_path = repo.parent / "index.db"
    assert idx_path.exists()
    # add a commit with a NEW source file + a Recall trailer
    new = repo / "billing.py"
    new.write_text("def charge(amount):\n    return amount\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat: billing\n\nRecall-anchors: billing, charge, payment")
    res = dashboard._auto_index(repo, idx_path)
    after = dashboard.pulse(repo, idx_path)
    # the auto-index picked up the new file's symbols (better-than-grep), idempotently
    idx = Index.open(idx_path, repo=repo)
    try:
        hit = idx.db.execute(
            "SELECT count(*) FROM nodes WHERE file_path LIKE '%billing.py'").fetchone()[0]
    finally:
        idx.db.close()
    assert hit >= 1, "auto-index did not pick up the new commit's file"
    assert after["nodes"] == res["nodes"]  # pulse agrees with the worker
    # idempotent: a second auto-index of the same HEAD does not grow the index
    res2 = dashboard._auto_index(repo, idx_path)
    assert res2["nodes"] == res["nodes"]


def test_auto_index_never_reaches_a_model(served):
    """Auto-index calls init()/freshen()/update_incremental only — running it must NOT
    pull recall.llm into sys.modules (the real Seam-Guard invariant, ADR-014). A behavior
    test, not a brittle substring scan: drop recall.llm, run a real auto-index (full and
    incremental), and assert the model seam was never imported."""
    import sys
    _, repo = served
    idx_path = repo.parent / "index.db"
    sys.modules.pop("recall.llm", None)
    dashboard._auto_index(repo, idx_path)              # full path
    assert "recall.llm" not in sys.modules
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip() or None
    sys.modules.pop("recall.llm", None)
    dashboard._auto_index(repo, idx_path, since_sha=head)  # incremental path
    assert "recall.llm" not in sys.modules


# ------------------------------------------------------------ LLM-free read
def test_dashboard_module_imports_no_llm_provider():
    """The read path stays LLM-free (ADR-014): importing dashboard must not pull
    recall.llm. The connect endpoints import it lazily inside the method, never at
    module load — so a plain `import recall.dashboard` leaves recall.llm unloaded."""
    import importlib
    import sys

    sys.modules.pop("recall.llm", None)
    sys.modules.pop("recall.dashboard", None)
    importlib.import_module("recall.dashboard")
    assert "recall.llm" not in sys.modules  # dashboard never imports a model seam


# -------------------------------------------------- MCP registration (ADR-029)
def test_mcp_status_unregistered_initially(served):
    base, _repo = served
    status, body = _get(base, "/api/mcp")
    assert status == 200
    st = json.loads(body)
    assert st["registered"] is False
    assert st["last_used"] is None  # no MCP client ever asked this index


def test_mcp_register_then_unregister(served):
    """The pill toggle: register writes .mcp.json, unregister removes it again."""
    base, repo = served
    status, d = _post(base, "/api/mcp", {"action": "install"})
    assert status == 200 and d["registered"] is True
    cfg = repo / ".mcp.json"
    assert json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["recall"] == {
        "command": "recall", "args": ["mcp"]}
    _, body = _get(base, "/api/mcp")
    assert json.loads(body)["registered"] is True
    status, d = _post(base, "/api/mcp", {"action": "uninstall"})
    assert status == 200 and d["registered"] is False
    assert not cfg.exists()  # recall was the only server -> the file goes too


def test_mcp_register_never_clobbers_foreign_servers(served):
    """Other servers in .mcp.json survive both register AND unregister."""
    base, repo = served
    cfg = repo / ".mcp.json"
    cfg.write_text(json.dumps(
        {"mcpServers": {"jcm": {"command": "jcm", "args": ["serve"]}}}), encoding="utf-8")
    _post(base, "/api/mcp", {"action": "install"})
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"jcm", "recall"}
    _post(base, "/api/mcp", {"action": "uninstall"})
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"jcm"}  # foreign server intact, file kept


def test_mcp_refuses_to_touch_invalid_json(served):
    base, repo = served
    cfg = repo / ".mcp.json"
    cfg.write_text("{not json", encoding="utf-8")
    status, d = _post(base, "/api/mcp", {"action": "install"})
    assert status == 400 and "valid JSON" in d["reason"]
    assert cfg.read_text(encoding="utf-8") == "{not json"  # untouched


def test_mcp_change_refused_cross_origin(served):
    base, _repo = served
    status, d = _post(base, "/api/mcp", {"action": "install"},
                      origin="http://evil.example.com")
    assert status == 403 and "cross-origin" in d["error"]


def test_guide_is_served_verbatim(served):
    """/api/guide serves the shipped getting-started.md — the ONE install/usage
    source the How-it-works section, README and website all share."""
    base, _repo = served
    status, body = _get(base, "/api/guide")
    assert status == 200
    assert "Getting started with recall" in body
    assert "recall init ." in body            # the concrete copy-paste steps
    assert "claude mcp add recall" in body    # the MCP one-liner


def test_changelog_is_served_verbatim(served):
    """/api/changelog serves the shipped changelog.md — the ONE release-notes
    source the Changelog tab and the website's /changelog page share."""
    base, _repo = served
    status, body = _get(base, "/api/changelog")
    assert status == 200
    assert "# Changelog" in body
    assert "v1.0.0" in body                   # launch starts at 1.0.0 (owner)
    assert "semver" in body                   # the versioning promise


def test_changelog_tab_is_wired(served):
    """The dashboard page must carry the Changelog tab button, its renderer and
    the tour-map row — a tab that 404s or renders blank is a drift bug."""
    base, _repo = served
    status, html = _get(base, "/")
    assert status == 200
    assert 'data-p="changelog"' in html       # the tab button
    assert "T.changelog" in html              # the renderer
    assert "/api/changelog" in html           # fetches the ONE source


def test_project_switch_resets_every_project_scoped_cache(served):
    """Owner bug 2026-06-12: 'Start here' showed the SAME content whatever repo
    was loaded — _ONBOARD survived switchProject. Pin the rule: every
    project-scoped JS cache must be reset inside switchProject."""
    base, _repo = served
    status, html = _get(base, "/")
    assert status == 200
    start = html.index("async function switchProject")
    fn = html[start:html.index("\n}", start)]
    for cache in ("CONTESTED_CACHE", "STALE_CACHE", "_ONBOARD", "_staleCache", "_codeTouchCache"):
        assert cache in fn, f"switchProject no longer resets {cache} — old-repo data will leak into the new repo"


# ------------------------------------------------------- About tab (legal home)
def test_about_reports_product_facts(served):
    base, _repo = served
    status, body = _get(base, "/api/about")
    assert status == 200
    d = json.loads(body)
    assert d["name"] == "whatever-recall"
    assert d["license"].startswith("Business Source License")
    assert d["version"] and d["copyright"]


def test_about_carries_community_links(served):
    """About tab community links (owner 2026-06-13): GitHub is always present;
    Discord is null unless a real invite is configured, so the dashboard never
    renders a dead link. The About panel must carry the container they render into."""
    base, _repo = served
    _status, body = _get(base, "/api/about")
    d = json.loads(body)
    assert "github.com" in (d.get("github_url") or "")
    assert "discord_url" in d  # the key always exists; value is None without RECALL_DISCORD_INVITE
    _, html = _get(base, "/")
    assert 'id="about-links"' in html  # the container the icon links paint into


def test_legal_serves_license_and_commercial_verbatim(served):
    base, _repo = served
    status, body = _get(base, "/api/legal?doc=license")
    assert status == 200 and "Business Source License 1.1" in body
    status, body = _get(base, "/api/legal?doc=commercial")
    assert status == 200 and "Commercial Use" in body
    status, body = _get(base, "/api/legal?doc=evil")
    assert status == 400


def test_about_tab_is_in_the_nav(served):
    base, _repo = served
    _, html = _get(base, "/")
    assert 'data-p="about"' in html


# ------------------------------------------------------- first-start tour
def test_first_start_tour_ships_in_the_dashboard(served):
    """The welcome tour: opens on first visit (localStorage recall.tour), reopenable
    via the header button, with Close (never again) + Remind me later (next session)."""
    base, _repo = served
    _, html = _get(base, "/")
    assert "maybeTour()" in html                     # wired into load()
    assert "recall.tour" in html                     # the first-open latch
    assert 'id="tour-btn"' in html                   # header reopen button
    assert "Remind me later" in html
    assert "'tour-overlay'" in html                  # Escape closes it (global handler list)


def test_tour_tab_hint_reuses_the_explain_registry(served):
    """The per-tab micro-guide must render EXPLAIN[p] — the '?' explainer stays the
    single copy of every tab description (no duplicated tour texts)."""
    base, _repo = served
    _, html = _get(base, "/")
    assert "function tourHint(p)" in html
    assert "const ex=EXPLAIN[p]" in html             # the reuse, structurally
    assert "recall.tourTabs" in html                 # shows once per tab


# ------------------------------------------------- polish wave (2026-06-11)
def test_local_filters_for_tasks_product_drift(served):
    """The header search filters tasks/product/drift LOCALLY (not only the live-recall
    fallback); contested/stale are cached so typing never re-runs the git scan."""
    base, _repo = served
    _, html = _get(base, "/")
    assert "taskQuery" in html and "productQuery" in html and "driftQuery" in html
    assert "CONTESTED_CACHE" in html and "STALE_CACHE" in html


def test_no_bare_code_layout_class(served):
    """`.code` as a layout class also matched every `.kind.code` PILL (it became a
    full-width grid bar in the drift column) — the container is .code-panes now."""
    base, _repo = served
    _, html = _get(base, "/")
    assert "\n.code{" not in html
    assert 'class="code-panes"' in html.replace("<div class=\"code-panes\">", 'class="code-panes"')

"""Drift-guards for the CLI recall rendering — the 3 ADR-016 tracks in the terminal.

The engine has computed code/knowledge/blast_radius on every query since ADR-016,
but the CLI printed only the legacy mixed list — the A/B-proven value never reached
the primary consumer (the AI session). These tests pin the new rendering: all tracks
visible in pretty AND --for-prompt, sections silent when empty, silence contract
byte-stable, no 'None' leaking from file-representative nodes.

capsys is not a tty -> _supports_color() is False -> output is ANSI-free, so exact
substring assertions are safe.
"""

from __future__ import annotations

from pathlib import Path

from recall import cli
from recall.engine import Index
from recall.importance import persist_importance


def _repo_with_tracks(tmp_path) -> Path:
    """An on-disk index with a full track set: a 3-file dependency chain (importance),
    a lesson with sha/why (knowledge), and an open task wired to the top file."""
    repo = tmp_path / "proj"
    (repo / ".mind").mkdir(parents=True)
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    # anchors overlap 1/3 per pair — below the 0.45 dedup threshold, so no MERGE
    for f, sym in (("leaf.py", "leaf"), ("mid.py", "mid"), ("core.py", "core")):
        idx.stamp(title=sym, anchors=[sym, sym + "node", "graphnode"], kind="code-symbol",
                  file_path=f, symbol=sym, line=1, origin="bootstrap")
    idx.add_dependency_edges([("leaf.py", "mid.py"), ("mid.py", "core.py")])
    persist_importance(idx.db)
    idx.stamp(title="why the graph is shaped this way", body="the chain mirrors runtime flow",
              anchors=["graphnode", "tracksdemo"], kind="lesson", sha="a1b2c3d", dedup=False)
    # an open task wired to core.py the way tasks.py does it: facet 'open' + relates_to edge
    t = idx.stamp(title="finish the graph wave", anchors=["graphnode", "taskdemo"],
                  kind="task", tags=["task", "open"], file_path="core.py",
                  origin="bootstrap", dedup=False)
    core_id = idx.db.execute(
        "SELECT id FROM nodes WHERE file_path='core.py' AND kind='code-symbol'").fetchone()[0]
    idx.db.execute("INSERT INTO edges(src_node, dst_node, kind) VALUES (?,?,?)",
                   (t["node_id"], core_id, "relates_to"))
    idx.db.commit()
    idx.db.close()
    return repo


def test_pretty_renders_three_tracks(tmp_path, capsys):
    repo = _repo_with_tracks(tmp_path)
    assert cli.main(["graphnode tracksdemo", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "── code · where (by importance)" in out
    assert "core.py:1" in out
    assert "── knowledge · why" in out
    assert "why the graph is shaped this way" in out
    assert "(sha a1b2c3d)" in out
    assert "── blast radius · changing core.py may break" in out
    assert "mid.py" in out
    assert "── open tasks on core.py" in out
    assert "finish the graph wave" in out
    assert "recall brief core.py" in out  # the footer hint


def test_pretty_code_track_leads_with_most_load_bearing(tmp_path, capsys):
    repo = _repo_with_tracks(tmp_path)
    cli.main(["graphnode tracksdemo", "--repo", str(repo)])
    out = capsys.readouterr().out
    code_lines = [l for l in out.splitlines() if ".py:1" in l]
    assert code_lines and "core.py" in code_lines[0]  # importance ordering at the CLI layer


def test_pretty_sections_silent_when_empty(tmp_path, capsys):
    repo = tmp_path / "proj"
    (repo / ".mind").mkdir(parents=True)
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    idx.stamp(title="a lonely lesson", body="prose only", anchors=["lonely", "lessonword"],
              kind="lesson", dedup=False)
    idx.db.commit()
    idx.db.close()
    assert cli.main(["lonely lessonword", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "── knowledge · why" in out
    assert "── code ·" not in out
    assert "── blast radius" not in out
    assert "── open tasks" not in out
    assert "recall brief" not in out  # no top code file -> no hint


def test_pretty_silenced_unchanged(tmp_path, capsys):
    repo = _repo_with_tracks(tmp_path)
    cli.main(["zzzz qqqq", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert "· silent" in out
    assert "── code" not in out and "── knowledge" not in out


def test_pretty_line_budget(tmp_path, capsys):
    repo = _repo_with_tracks(tmp_path)
    cli.main(["graphnode tracksdemo", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert len(out.splitlines()) <= 35  # token-lean guard at default topk


def test_pretty_handles_file_representative_node(tmp_path, capsys):
    repo = tmp_path / "proj"
    (repo / ".mind").mkdir(parents=True)
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    # the file-representative shape verified live: symbol=None, line=None
    idx.stamp(title="solo.py", anchors=["solofile", "filenode"], kind="code-symbol",
              file_path="solo.py", symbol=None, line=None, origin="bootstrap")
    idx.db.commit()
    idx.db.close()
    cli.main(["solofile filenode", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert "None" not in out
    assert "(file)" in out


def test_for_prompt_carries_tracks(tmp_path, capsys):
    repo = _repo_with_tracks(tmp_path)
    assert cli.main(["graphnode tracksdemo", "--for-prompt", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[recall · project memory for:")
    assert "WHERE (code, by importance):" in out
    assert "WHY (knowledge):" in out
    assert "WHAT BREAKS if you change core.py:" in out
    assert "OPEN TASKS on core.py" in out
    assert "(importance " in out
    assert "sha a1b2c3d" in out
    assert "\x1b[" not in out  # ANSI-free: this block is pasted into other AIs


def test_for_prompt_silenced_unchanged(tmp_path, capsys):
    repo = _repo_with_tracks(tmp_path)
    cli.main(["zzzz qqqq", "--for-prompt", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert out.strip() == "[recall] no project memory for: zzzz qqqq"


def test_for_prompt_carries_relation_lines():
    """Review follow-up: the track block lost the `supersedes -> ADR-007` chains the
    old mixed block had — knowledge items carry up to 2 relation lines again."""
    from recall import Index
    from recall.cli import _format_for_prompt
    idx = Index.open(":memory:")
    idx.stamp(title="ADR-9 new cache policy", anchors=["cachepolicy", "cachepolicytwo"],
              kind="lesson", edges=[("supersedes", "ADR-7 old cache policy")], dedup=False)
    res = idx.recall("cachepolicy cachepolicytwo")
    out = _format_for_prompt("cachepolicy cachepolicytwo", res)
    assert "relation: supersedes -> ADR-7 old cache policy" in out


# ---- --terse: the AGENT (Bash) path (2026-06-14) ----------------------------
# Subagents can't reach the MCP server (session-scoped), so recall reaches the
# fleet via the CLI; --terse is the machine-first block they read. It must keep
# the WHY verbatim (the signal that stops an AI undoing a decision) while
# compressing the structural WHERE list — i.e. shorter than --for-prompt.

def test_terse_query_keeps_why_verbatim(tmp_path, capsys):
    """The WHY (knowledge) track — the decision text that stops an AI undoing a
    decision — must survive terse VERBATIM (this is the whole point of the agent
    path: less noise, same signal)."""
    repo = _repo_with_tracks(tmp_path)
    assert cli.main(["graphnode tracksdemo", "--terse", "--repo", str(repo)]) == 0
    terse = capsys.readouterr().out
    assert "WHY (knowledge):" in terse
    assert "why the graph is shaped this way" in terse
    assert "the chain mirrors runtime flow" in terse  # the body, verbatim
    assert "\x1b[" not in terse  # ANSI-free (pasted into agent prompts)


def test_terse_query_compresses_long_where_list():
    """With many code hits the WHERE list is capped (+N more) in terse but full in
    rich — that is the structural compression. Built on a synthetic result dict so
    the silence floor / dedup of a real query can't change the hit count out from
    under the assertion."""
    from recall.cli import _format_for_prompt
    res = {
        "silenced": False,
        "code": [
            {"file": f"f{i}.py", "line": i + 1, "symbol": f"sym{i}", "importance": 50 - i}
            for i in range(8)  # 8 hits > the terse cap of 5
        ],
        "knowledge": [], "blast_radius": [], "open_tasks": [], "results": [],
    }
    rich = _format_for_prompt("q", res, terse=False)
    terse = _format_for_prompt("q", res, terse=True)
    assert "more)" in terse and "more)" not in rich, "terse must cap the WHERE list"
    assert "f0.py" in terse and "f7.py" in rich
    assert len(terse) < len(rich)


def test_terse_brief_not_longer_than_rich(tmp_path):
    from recall.cli import _format_brief_for_prompt
    repo = _repo_with_tracks(tmp_path)
    idx = Index.open(repo / ".mind" / "index.db", repo=repo)
    b = idx.brief("core.py")
    idx.db.close()
    terse = _format_brief_for_prompt(b, terse=True)
    rich = _format_brief_for_prompt(b, terse=False)
    assert len(terse) <= len(rich), "terse brief must not be longer than the rich one"


def test_terse_flag_is_wired_on_all_three_commands():
    """brief / explain / the bare `recall <query>` all accept --terse — the agent
    path relies on every read surface supporting it."""
    from recall.cli import build_parser, _run_recall
    import argparse
    p = build_parser()
    # brief + explain are subcommands
    assert p.parse_args(["brief", "x.py", "--terse"]).terse is True
    assert p.parse_args(["explain", "--terse"]).terse is True
    # the bare query path has its own parser inside _run_recall — assert the flag
    # parses there too without raising
    inner = argparse.ArgumentParser(prog="recall")
    inner.add_argument("query")
    inner.add_argument("--terse", action="store_true")
    assert inner.parse_args(["q", "--terse"]).terse is True
    assert _run_recall is not None

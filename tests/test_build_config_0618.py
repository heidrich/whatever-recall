"""Build & share config (.recall/config.toml [share]) — the SSOT the dashboard
modal, the CLI and the hooks all read. SAFE DEFAULTS / FAIL-CLOSED: a missing or
malformed config never loosens a leak guard (owner: a 100% waterproof rule).
"""
import subprocess
from types import SimpleNamespace

from recall.config import (
    BuildConfig,
    load_build_config,
    write_build_config,
    config_path,
)


def _git_repo(tmp_path):
    """A real git repo so check-leak can read `git diff --cached`."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


def test_missing_config_gives_safe_defaults(tmp_path):
    c = load_build_config(tmp_path)
    assert c.default_visibility == "team"
    assert c.block_raw_mind_commit is True
    assert c.dry_run_before_export is True


def test_write_read_roundtrip(tmp_path):
    write_build_config(tmp_path, BuildConfig(default_visibility="private", export_path=".mind/out.db"))
    c = load_build_config(tmp_path)
    assert c.default_visibility == "private"
    assert c.export_path == ".mind/out.db"
    assert config_path(tmp_path).is_file()


def test_malformed_config_is_fail_closed(tmp_path):
    """A corrupt config must not crash and must not loosen anything — guards stay ON."""
    p = config_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("this is [not valid toml @@@", encoding="utf-8")
    c = load_build_config(tmp_path)
    assert c.default_visibility == "team"
    assert c.block_raw_mind_commit is True


def test_hostile_types_cannot_disable_guards(tmp_path):
    """A wrong-typed value can never turn a leak guard off or set an invalid visibility."""
    p = config_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '[share]\nblock_raw_mind_commit = "nope"\ndefault_visibility = "public"\n',
        encoding="utf-8",
    )
    c = load_build_config(tmp_path)
    assert c.default_visibility == "team", "an invalid visibility must clamp to team"
    assert c.block_raw_mind_commit is True, "a non-bool must not disable the guard"


def test_explicit_false_can_relax_a_guard(tmp_path):
    """An explicit, correctly-typed false is the ONLY way to relax a guard (the owner's
    deliberate choice) — proving the clamp distinguishes 'malformed' from 'off'."""
    p = config_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[share]\ndry_run_before_export = false\n", encoding="utf-8")
    c = load_build_config(tmp_path)
    assert c.dry_run_before_export is False
    # the OTHER guard, untouched, stays on
    assert c.block_raw_mind_commit is True


def test_check_leak_blocks_a_staged_private_brain(tmp_path):
    """A staged brain holding private nodes must make check-leak return non-zero
    (the commit aborts)."""
    from recall import cli
    from recall.engine import Index
    repo = _git_repo(tmp_path)
    ix = Index.open(repo / "brain.db", repo=repo)
    ix.stamp(title="secret", anchors=["s1", "s2"], visibility="private")
    ix.db.commit()
    ix.db.close()
    subprocess.run(["git", "add", "-f", "brain.db"], cwd=repo, check=True)
    rc = cli.cmd_check_leak(SimpleNamespace(repo=str(repo)))
    assert rc == 1, "a staged private brain must BLOCK the commit"


def test_check_leak_passes_a_clean_export(tmp_path):
    """A staged shareable export (private nodes purged) must pass."""
    from recall import cli
    from recall.engine import Index
    repo = _git_repo(tmp_path)
    ix = Index.open(repo / "index.db", repo=repo)
    ix.stamp(title="team", anchors=["t1", "t2"], visibility="team")
    ix.stamp(title="secret", anchors=["s1", "s2"], visibility="private")
    ix.db.commit()
    # produce a clean export and stage THAT
    dest = Index.open(repo / "shared.db", repo=repo)
    ix.db.backup(dest.db)
    dest.purge_private()
    dest.db.commit()
    dest.assert_no_private()
    dest.db.close()
    subprocess.run(["git", "add", "-f", "shared.db"], cwd=repo, check=True)
    rc = cli.cmd_check_leak(SimpleNamespace(repo=str(repo)))
    assert rc == 0, "a clean export must pass the leak guard"


def test_check_leak_respects_explicit_optout(tmp_path):
    """If the owner sets block_raw_mind_commit = false, the guard stands down."""
    from recall import cli
    from recall.engine import Index
    repo = _git_repo(tmp_path)
    write_build_config(repo, BuildConfig(block_raw_mind_commit=False))
    ix = Index.open(repo / "brain.db", repo=repo)
    ix.stamp(title="secret", anchors=["s1", "s2"], visibility="private")
    ix.db.commit()
    ix.db.close()
    subprocess.run(["git", "add", "-f", "brain.db"], cwd=repo, check=True)
    rc = cli.cmd_check_leak(SimpleNamespace(repo=str(repo)))
    assert rc == 0, "an explicit opt-out must let the commit through"


def test_pre_commit_hook_stub_runs_the_leak_guard_first(tmp_path):
    """The installed pre-commit hook must run `check-leak` as a BLOCKING step
    (exit 1 on a leak) before the non-blocking warning."""
    from adapters.hook import _pre_commit_stub
    stub = _pre_commit_stub()
    assert "check-leak" in stub, "the leak guard is not wired into the pre-commit hook"
    assert "|| exit 1" in stub, "the leak guard must be able to BLOCK the commit"
    # the warning still never blocks
    assert "precommit-check || true" in stub


def test_dashboard_serves_and_saves_build_settings(tmp_path):
    """The dashboard exposes GET + POST /api/build-settings (the modal's backend).
    Static guard: the routes + handlers exist and POST writes through config."""
    from pathlib import Path
    dash = Path(__file__).resolve().parent.parent / "recall" / "dashboard.py"
    src = dash.read_text(encoding="utf-8")
    assert '"/api/build-settings"' in src, "the build-settings API route is gone"
    assert "_serve_build_settings" in src and "_do_build_settings" in src
    # the POST handler writes through write_build_config (the SSOT writer)
    assert "write_build_config" in src


def test_dashboard_html_has_the_build_button_and_modal(tmp_path):
    """The build button sits in the header (next to console) and goes red+pulse on an
    unsafe config; the modal reads/writes via openBuildSettings/saveBuildSettings."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "recall" / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="build-btn"' in html, "the build button is gone from the header"
    assert "openBuildSettings" in html and "saveBuildSettings" in html
    # the alert state: a class that turns the button red + pulses when a guard is off
    assert "proj--alert" in html and "buildAlert" in html
    assert "buildIsUnsafe" in html, "the unsafe-detection that drives the red pulse is gone"


def test_task_steps_carry_per_author_via_blame(tmp_path):
    """parse_subtasks records a line number per step; the dashboard's _blame_task_steps
    reads per-line author+time from git blame — so a task several people worked shows who
    closed which step."""
    from pathlib import Path
    from recall.tasks import parse_subtasks
    # parse_subtasks now tags each step with its body line number
    subs = parse_subtasks("intro\n- [x] first step\n- [ ] second step\n")
    assert subs and all("line" in s for s in subs), "subtasks lost their per-step line number"
    # the dashboard helper exists and parses blame porcelain
    dash = (Path(__file__).resolve().parent.parent / "recall" / "dashboard.py").read_text(encoding="utf-8")
    assert "_blame_task_steps" in dash, "the per-step blame helper is gone"
    assert "author-time" in dash, "the blame parse no longer reads author-time"
    # the dashboard attaches by/at to each subtask
    assert 'st["by"]' in dash and 'st["at"]' in dash, "steps no longer get a by/at author"


def test_task_step_author_not_struck_through_and_wikilinks_clickable(tmp_path):
    """The step author/time line must NOT inherit the done-step line-through, and a
    [[wikilink]] in a task body must render clickable (openWiki)."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "recall" / "dashboard.html").read_text(encoding="utf-8")
    # author line is its own element outside .st-txt + forces no line-through
    assert ".st-by" in html and "st-main" in html, "the step-author element structure changed"
    assert "text-decoration:none!important" in html, "the author line can still be struck through"
    # wikilinks are clickable now (md-wl--link + openWiki)
    assert "md-wl--link" in html and "openWiki(" in html, "[[wikilinks]] are no longer clickable"


def test_state_block_injects_private_default_setting(tmp_path):
    """When the owner sets default_visibility=private, the recall STATE block (which is
    injected into CLAUDE.md/AGENTS.md) must carry it — so every AI session knows new
    notes stay local here without being told."""
    from recall.engine import Index
    ix = Index.open(tmp_path / "index.db", repo=tmp_path)
    # default: no build-settings section (don't bloat the block for everyone)
    assert "Build & share settings" not in ix.render_state_block()
    # owner's deliberate choice: it now appears
    write_build_config(tmp_path, BuildConfig(default_visibility="private"))
    blk = ix.render_state_block()
    assert "Build & share settings" in blk
    assert "PRIVATE" in blk


def test_stamp_uses_config_default_visibility(tmp_path):
    """recall stamp with no --private follows the project's configured default."""
    from recall.engine import Index
    write_build_config(tmp_path, BuildConfig(default_visibility="private"))
    cfg = load_build_config(tmp_path)
    # the CLI reads load_build_config(repo).default_visibility; assert that value
    assert cfg.default_visibility == "private"
    # and a stamp written with it lands private
    ix = Index.open(tmp_path / "index.db", repo=tmp_path)
    r = ix.stamp(title="cfg note", anchors=["zzz", "yyy"], visibility=cfg.default_visibility)
    v = ix.db.execute("SELECT visibility FROM nodes WHERE id=?", (r["node_id"],)).fetchone()[0]
    assert v == "private"

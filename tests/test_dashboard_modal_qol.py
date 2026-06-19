"""Drift-guards for the dashboard QoL wave (2026-06-13):
  - the activity console (v7): access_log.kind logged for brief/explain/stamp.
  - the shared draggable-modal layer: no click-outside on workspace modals, the
    drag helpers exist, one shared remembered position.

These are STATIC assertions over recall/dashboard.html + the engine so a future edit
can't silently re-center modals, re-add click-outside close, or drop the kind logging.
House rule: lock the regression the Owner just signed off on.
"""
from pathlib import Path

import recall.db as dbmod
from recall.engine import Index

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "recall" / "dashboard.html").read_text(encoding="utf-8")


# ---- activity console (v7) -------------------------------------------------

def test_access_log_has_kind_column_v7():
    """The console feed needs access_log.kind; schema bumped to v7."""
    assert dbmod.SCHEMA_VERSION >= 7
    db = dbmod.connect(":memory:")
    cols = {r[1] for r in db.execute("PRAGMA table_info(access_log)").fetchall()}
    assert "kind" in cols, "access_log.kind is what the live console streams"
    db.close()


def test_brief_explain_stamp_log_their_kind(tmp_path):
    """brief/onboarding/stamp must each write a row tagged with their kind, so the
    console can show recall being used (not just bare recall() queries)."""
    p = tmp_path / "index.db"
    idx = Index.open(str(p))
    idx.stamp("guard note", body="why", anchors=["alpha"], origin="live", consumer="cli")
    idx.brief("recall/engine.py", consumer="cli")
    idx.onboarding(consumer="cli")
    kinds = {r[0] for r in idx.db.execute("SELECT DISTINCT kind FROM access_log").fetchall()}
    assert {"stamp", "brief", "explain"} <= kinds
    idx.db.close()


def test_bootstrap_stamps_do_not_flood_the_console(tmp_path):
    """A non-live stamp (bootstrap/power) must NOT log an activity row — else a
    re-index would bury the console in thousands of machine writes."""
    p = tmp_path / "index.db"
    idx = Index.open(str(p))
    idx.stamp("bootstrap node", anchors=["beta"], origin="bootstrap")
    n = idx.db.execute("SELECT COUNT(*) FROM access_log WHERE kind='stamp'").fetchone()[0]
    assert n == 0, "only interactive stamps belong in the activity feed"
    idx.db.close()


def test_commit_replay_stamps_do_not_flood_the_console(tmp_path):
    """stamp_from_commit replays git trailers with origin='live' once per trailer-commit
    during the `git log` walk — it MUST stay out of the console (consumer='commit'), or a
    re-index of a dogfooded repo (1000s of trailer commits) floods access_log on the hot
    path. Self-review P1, 2026-06-13. An interactive `recall stamp` still logs."""
    p = tmp_path / "index.db"
    idx = Index.open(str(p))
    idx.stamp("interactive stamp", anchors=["a1"], origin="live", consumer="cli")
    idx.stamp_from_commit(
        "fix: x\n\nRecall-anchors: foo, bar\nRecall-why: because", "deadbee"
    )
    rows = idx.db.execute(
        "SELECT consumer FROM access_log WHERE kind='stamp'"
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] == "cli", (
        "commit-replay stamps must not reach the console; only the interactive one logs"
    )
    idx.db.close()


def test_activity_endpoint_is_registered():
    assert "/api/activity" in HTML or "_serve_activity" in (
        (ROOT / "recall" / "dashboard.py").read_text(encoding="utf-8")
    )


# ---- shared draggable-modal layer ------------------------------------------

def test_workspace_modals_have_no_click_outside_close():
    """The four file-overlay renders + connect must NOT close on scrim click; the
    Owner wants ✕ + Escape only. The telltale `if(e.target===ov) navClose()` and the
    connect `if(e.target===ov) ov.remove()` must be gone from those creators."""
    assert "if(e.target===ov) navClose()" not in HTML, (
        "a workspace modal still closes on click-outside"
    )
    assert "if(e.target===ov) ov.remove()" not in HTML, (
        "the connect modal still closes on click-outside"
    )


def test_escape_still_closes_workspace_modals():
    """Escape must remain wired for file/connect (now the only non-✕ way out)."""
    assert "'connect-overlay'" in HTML
    assert "'file-overlay'" in HTML
    # the global Escape handler array must still list them
    assert "tour-overlay" in HTML  # array sentinel still present


def test_modals_are_classic_not_draggable():
    """Owner reverted the draggable-modal experiment to 'keep it simple': modals are classic
    centered dialogs again. The drag machinery must be GONE (it was the source of the z-index /
    scrim bugs) — no makeModalDraggable / wireModal / shared-position key left behind."""
    for dead in ("makeModalDraggable", "wireModal(", "MODAL_POS_KEY", "modal-dragging",
                 "bringToFront"):
        assert dead not in HTML, f"leftover from the reverted draggable-modal experiment: {dead}"


def test_workspace_modals_keep_a_dimming_scrim():
    """Classic modals dim the background (rgba scrim) — confirm the scrim is back, not the
    pointer-events:none click-through overlay from the draggable experiment."""
    assert HTML.count("background:rgba(14,14,14,.5)") >= 5  # commit/task/file/diff/connect
    assert "background:none;pointer-events:none;z-index:50" not in HTML

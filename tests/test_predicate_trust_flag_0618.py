"""M1 / Workstream B (2026-06-18) — the predicate VERDICT becomes a loud 🔴 trust-flag on
every pushed fact, the "SPIKE" docstring is retired, and a model-free pre-commit predicate
nudge lands.

The verdict was already computed, persisted (meta `drift:<id>`), and present on the brief()
return — it was INVISIBLE only because the renderers (state block, pre-edit hook, MCP brief)
did not surface it. These tests pin the rendering, the de-SPIKE, the nudge's round-trip
guarantee, and the merge_signal landmine. The single-source guard proves the state block, the
brief field, and drift_counts() all read the SAME meta rows (no second evaluator).
"""

import inspect
import subprocess
from types import SimpleNamespace

import pytest

from recall import predicate as P
from recall.engine import Index
from recall.cli import _index_path, _format_brief_for_prompt, cmd_precommit_check
from recall.freshness import UNCOMMITTED, COMMITTED, FRESH, drift_counts
from adapters.hook import _format_pre_edit


# --------------------------------------------------------------------------- helpers
def _mem_idx(tmp_path):
    return Index.open(":memory:", repo=tmp_path)


def _broken_node(idx, *, file_path="auth.py", title="login lowercases the email",
                 predicate=r"contains:lower"):
    """Stamp a claim-bearing node, then force its persisted drift meta to 'broken' — the
    SINGLE SOURCE freshen()/merge_signal write and _file_drift/_node_drifts read."""
    r = idx.stamp(title=title, anchors=["login"], file_path=file_path,
                  predicate=predicate, kind="lesson",
                  body="the login path lowercases the address")
    nid = r["node_id"]
    idx.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                   (f"drift:{nid}", P.BROKEN))
    idx.db.commit()
    return nid


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _git_repo(tmp_path):
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git unavailable")
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    return repo


# ------------------------------------------------------- 1. state block renders BROKEN
def test_state_block_renders_broken(tmp_path):
    idx = _mem_idx(tmp_path)
    _broken_node(idx, title="apply_downgrade refunds the prorated amount")
    block = idx.render_state_block()
    assert "🔴 BROKEN claims" in block
    assert "apply_downgrade refunds the prorated amount" in block
    assert "recall brief auth.py" in block  # lean: a pointer, not the predicate regex
    assert "contains:lower" not in block    # regex text stays OUT of the cached block


def test_state_block_silent_when_nothing_broken(tmp_path):
    """An all-green repo's block carries NOTHING broken-related (byte-stable vs before)."""
    idx = _mem_idx(tmp_path)
    idx.stamp(title="login note", anchors=["login"], file_path="auth.py", kind="lesson",
              body="why login is the way it is")  # a why, but no broken drift
    block = idx.render_state_block()
    assert "BROKEN" not in block and "🔴" not in block


# ------------------------------------------------- 2. pre-edit hook leads with the red line
def test_pre_edit_leads_with_broken_above_why():
    brief = {
        "file": "auth.py", "known": True, "drift": "broken",
        "warns": [], "open_tasks": [], "breaks": [],
        "why": [{"node_id": 1, "kind": "lesson", "title": "login lowercases the email",
                 "why": "the login path lowercases", "sha": "abc1234",
                 "drift": "broken", "predicate": "contains:lower"}],
    }
    out = _format_pre_edit("auth.py", brief, {})
    assert "FAILS its re-check NOW" in out                      # the loud leading line
    assert "🔴 BROKEN — its own re-check FAILS now" in out      # the why-row flag
    assert "failing check: contains:lower" in out               # the predicate text shown
    # the file-level broken line LEADS — above the WHY section
    assert out.index("FAILS its re-check NOW") < out.index("WHY it is the way it is")


# ---------------------------------------------- 3. MCP brief formatter renders the verdict
def test_mcp_brief_formatter_renders_broken(tmp_path):
    idx = _mem_idx(tmp_path)
    _broken_node(idx)
    b = idx.brief("auth.py")
    for terse in (True, False):
        out = _format_brief_for_prompt(b, terse=terse)
        assert "🔴 BROKEN" in out
        assert "failing check: contains:lower" in out


# --------------------------- 4. FACTUAL: brief() ALREADY carries the verdict (renderers read it)
def test_brief_already_carries_the_verdict(tmp_path):
    """Pins the M0 correction: the verdict rides the brief() return on BOTH axes, so the
    renderers RENDER it, they never re-evaluate."""
    idx = _mem_idx(tmp_path)
    nid = _broken_node(idx)
    b = idx.brief("auth.py")
    assert b["drift"] == P.BROKEN                                   # file-level verdict
    rows = [w for w in b["why"] if w["node_id"] == nid]
    assert rows and rows[0]["drift"] == P.BROKEN                    # per-why verdict
    assert rows[0]["predicate"] == r"contains:lower"               # grammar present too


# ------------------------------------------- 5. suggest_predicate_from_diff: deterministic + round-trip
@pytest.mark.parametrize("added,expected", [
    (["    def apply_downgrade(self):"], "contains:apply_downgrade"),
    (["class PayoutWorker:"], "contains:PayoutWorker"),
    (["export function reserveStock(sku) {"], "contains:reserveStock"),
    (["MAX_RETRIES = 5"], "contains:MAX_RETRIES"),
    (["    x = 1", "# just a tweak", "return y + 1"], None),  # low signal → quiet
])
def test_suggest_predicate_high_and_low_signal(added, expected):
    assert Index.suggest_predicate_from_diff("svc.py", added) == expected
    # deterministic — same input, same output
    assert (Index.suggest_predicate_from_diff("svc.py", added)
            == Index.suggest_predicate_from_diff("svc.py", added))


def test_suggest_predicate_skips_non_source_files():
    assert Index.suggest_predicate_from_diff("notes.md", ["def foo():"]) is None
    assert Index.suggest_predicate_from_diff("data.json", ["MAX = 1"]) is None


def test_every_suggestion_round_trips_through_stamp(tmp_path):
    """The nudge's contract: a suggestion the author pastes MUST parse() and survive stamp()
    — else it silently drops and trains the agent to ignore the nudge."""
    idx = _mem_idx(tmp_path)
    samples = [["def apply_downgrade(self):"], ["class PayoutWorker:"], ["MAX_RETRIES = 5"]]
    for i, added in enumerate(samples):
        sugg = Index.suggest_predicate_from_diff("svc.py", added)
        assert sugg is not None
        assert P.parse_predicate(sugg) is not None                 # parses
        r = idx.stamp(title=f"claim {i}", anchors=[f"a{i}"], file_path="svc.py",
                      predicate=sugg, kind="lesson")               # survives stamp() validators
        assert idx.db.execute("SELECT predicate FROM nodes WHERE id=?",
                              (r["node_id"],)).fetchone()[0] == sugg


# ------------------------------------------------------- 6. the pre-commit nudge (gated, exit 0)
def _seed_claim_repo(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "svc.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    idx = Index.open(_index_path(repo), repo=repo)
    idx.stamp(title="svc enforces the proration rule", anchors=["svc"], kind="lesson",
              file_path="svc.py", body="why svc is the way it is")  # claim, NO predicate
    idx.db.close()
    # stage an edit that adds a def → a high-signal anchor for the suggestion
    (repo / "svc.py").write_text("x = 1\n\ndef apply_proration(amount):\n    return amount\n",
                                 encoding="utf-8")
    _git(repo, "add", "svc.py")
    return repo


def test_precommit_nudge_fires_and_exits_zero(tmp_path, capsys):
    repo = _seed_claim_repo(tmp_path)
    rc = cmd_precommit_check(SimpleNamespace(repo=str(repo)))
    out = capsys.readouterr().out
    assert rc == 0                                                 # never blocks
    assert "recall · predicate" in out
    assert "Recall-predicate: contains:apply_proration" in out


def test_precommit_nudge_silent_when_rule_off(tmp_path, capsys):
    repo = _seed_claim_repo(tmp_path)
    (repo / ".recall").mkdir(exist_ok=True)
    (repo / ".recall" / "rules.md").write_text("---\npredicate_nudge: false\n---\n",
                                               encoding="utf-8")
    rc = cmd_precommit_check(SimpleNamespace(repo=str(repo)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "recall · predicate" not in out and "Recall-predicate:" not in out


# ------------------------------------------------ 7. merge_signal landmine (the pinned dual)
def test_merge_signal_landmine_regression():
    """Adversarial review 2026-06-15: CONFIRMED must NOT suppress a live 🟠 uncommitted edit,
    and BROKEN must ALWAYS win. Both pinned so the regression can never silently return."""
    assert P.merge_signal(UNCOMMITTED, P.CONFIRMED) == UNCOMMITTED   # liveness survives
    assert P.merge_signal(UNCOMMITTED, P.BROKEN) == P.BROKEN         # broken wins outright
    assert P.merge_signal(COMMITTED, P.CONFIRMED) == FRESH           # GAP B: quiets false 🟡
    assert P.merge_signal(COMMITTED, P.UNKNOWN) == COMMITTED         # UNKNOWN defers to drift


# ------------------------------------------------------- 8. de-SPIKE drift-guard (docstring only)
def test_predicate_docstring_de_spiked_no_code_shift():
    doc = P.__doc__ or ""
    assert "SPIKE" not in doc
    assert "deliberately does not decide" not in doc
    assert "Arrow 1 (SHIPPED)" in doc
    # the docstring edit must have shifted NO executable line: the verdict constants and the
    # merge_signal invariant are byte-checked here (a snapshot the edit cannot silently move).
    assert (P.CONFIRMED, P.BROKEN, P.UNKNOWN) == ("confirmed", "broken", "unknown")
    assert P._VERDICT_RANK == {"confirmed": 0, "unknown": 1, "broken": 2}
    src = inspect.getsource(P.merge_signal)
    assert "if verdict == BROKEN:" in src and "return BROKEN" in src


# ------------------------------------------------ single-source guard (one meta, three readers)
def test_broken_is_single_sourced_from_drift_meta(tmp_path):
    """render_state_block's BROKEN list, brief()'s drift field, and drift_counts() all read the
    SAME meta `drift:%` rows — delete them and ALL THREE go quiet (no second evaluator)."""
    idx = _mem_idx(tmp_path)
    nid = _broken_node(idx)

    assert drift_counts(idx)[P.BROKEN] == 1
    ob = idx.onboarding()
    assert ob["counts"]["broken"] == 1
    assert any(b["node_id"] == nid for b in ob["broken"])
    assert idx.brief("auth.py")["drift"] == P.BROKEN

    # remove the ONE source — every reader must fall silent
    idx.db.execute("DELETE FROM meta WHERE key LIKE 'drift:%'")
    idx.db.commit()
    assert drift_counts(idx)[P.BROKEN] == 0
    assert idx.onboarding()["counts"]["broken"] == 0
    assert idx.brief("auth.py")["drift"] is None

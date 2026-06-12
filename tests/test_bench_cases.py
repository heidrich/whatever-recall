"""Drift-guards for bench v2's ground truth — a rename/move must break the bench
LOUDLY (a silently-dangling accept pair would measure 0 forever and read as a
quality regression instead of a stale case)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))

CASES = json.loads((ROOT / "experiments" / "bench_cases_v2.json").read_text(encoding="utf-8"))["cases"]


def test_case_schema():
    for c in CASES:
        assert c["id"] and c["track"] in ("knowledge", "code") and c["lang"] in ("en", "de")
        assert c["question"].strip()
        if c["track"] == "knowledge":
            assert c["needle"].strip()
        else:
            assert c["accept"], c["id"]
            for a in c["accept"]:
                assert a["file"] and a["symbol"], c["id"]


def test_code_ground_truth_symbols_exist_in_source():
    """Every accept pair must point at a symbol that exists in its file — locks the
    bench against silent renames (e.g. the nested _dashboard_html has NO node, which
    this kind of check catches at authoring time)."""
    for c in CASES:
        if c["track"] != "code":
            continue
        for a in c["accept"]:
            src = (ROOT / a["file"]).read_text(encoding="utf-8")
            needle_def = f"def {a['symbol']}"
            needle_cls = f"class {a['symbol']}"
            assert needle_def in src or needle_cls in src, (
                f"{c['id']}: {a['symbol']} not found in {a['file']} — bench case is stale")


def test_judge_no_substring_false_hits():
    # THE shared judge (experiments/judge.py — the 4 per-script copies are gone)
    from judge import code_rank
    accept = [{"file": "recall/dashboard.py", "symbol": "_make_handler"}]
    # the measured false hits a substring judge produced:
    assert code_rank([{"file": "tests/test_dashboard.py", "symbol": "_make_handler"}], accept) == 0
    assert code_rank([{"file": "recall/dashboard.py", "symbol": "other"}], accept) == 0
    assert code_rank([{"file": "recall\\dashboard.py", "symbol": "_make_handler"}], accept) == 1


def test_judge_knowledge_or_alternatives():
    from judge import hit_rank
    items = [{"title": "two-stage drift light", "file": "docs/decisions.md"}]
    assert hit_rank(items, "freshness|drift|two-stage") == 1
    assert hit_rank(items, "nothere") == 0


def test_gate_requires_fresh(monkeypatch, capsys):
    import bench_v2
    monkeypatch.setattr(sys, "argv", ["bench_v2.py", "--gate"])
    assert bench_v2.main() == 2

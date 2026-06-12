"""Bench v2 — per-track recall quality against verified ground truth.

Measures what bench v1 could not see:
  - the CODE track ("where is X?") against exact (file, symbol) pairs — substring
    judging measured false hits (needle `dashboard.py` matched tests/test_dashboard.py)
  - English questions (the corpus language since the EN-only migration)
  - 3 German probes that DOCUMENT the language gap — reported, never gated

Modes:
  default        — the live .mind/index.db (what the Owner experiences; drifts per session)
  --fresh        — temp-dir re-init (reproducible; REQUIRED for --gate)
  --db <path>    — any foreign index
  --gate         — exit 1 below --floor-knowledge / --floor-code (recall@3, en cases)

Run: PYTHONIOENCODING=utf-8 python experiments/bench_v2.py [--fresh] [--gate]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from recall.engine import Index  # noqa: E402
from judge import code_rank, hit_rank  # noqa: E402 — THE one needle judge (judge.py)

CASES_PATH = Path(__file__).with_name("bench_cases_v2.json")


def _fresh_index(repo: Path) -> Index:
    from recall import bootstrap
    tmp = Path(tempfile.mkdtemp(prefix="recall-bench-"))
    idx = Index.open(tmp / "bench.db", repo=repo)
    bootstrap.init(idx, str(repo), rebuild=False)
    return idx


def _head_sha(repo: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "?"
    except OSError:
        return "?"


def run(idx: Index, cases: list[dict]) -> dict:
    rows, t_total = [], 0.0
    for c in cases:
        t0 = time.perf_counter()
        res = idx.recall(c["question"], consumer="bench")
        ms = (time.perf_counter() - t0) * 1000
        t_total += ms
        if c["track"] == "code":
            rank = code_rank(res.get("code", []) if not res["silenced"] else [], c["accept"])
            mixed_rank = 0
        else:
            items = res.get("knowledge", []) if not res["silenced"] else []
            rank = hit_rank(items, c["needle"])
            mixed = res.get("results", []) if not res["silenced"] else []
            mixed_rank = hit_rank(mixed, c["needle"])
        rows.append({"id": c["id"], "track": c["track"], "lang": c["lang"],
                     "q": c["question"], "rank": rank, "mixed_rank": mixed_rank,
                     "silenced": res["silenced"], "ms": ms})
    return {"rows": rows, "total_ms": t_total}


def _bucket(rows: list[dict], track: str, lang: str) -> dict:
    sel = [r for r in rows if r["track"] == track and r["lang"] == lang]
    if not sel:
        return {"n": 0}
    return {
        "n": len(sel),
        "r1": sum(1 for r in sel if r["rank"] == 1),
        "r3": sum(1 for r in sel if 1 <= r["rank"] <= 3),
        "mixed_r3": sum(1 for r in sel if 1 <= r["mixed_rank"] <= 3),
        "silenced": sum(1 for r in sel if r["silenced"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="re-init into a temp dir (reproducible)")
    ap.add_argument("--db", help="path to an index db (default: .mind/index.db)")
    ap.add_argument("--gate", action="store_true", help="exit 1 below the floors (needs --fresh)")
    ap.add_argument("--floor-knowledge", type=float, default=0.75)
    # raised 0.50 -> 0.75 with ADR-028 (relevance-first code track measured 0.83):
    # the gate may give back ONE case, never the regression to importance-first (0.08)
    ap.add_argument("--floor-code", type=float, default=0.75)
    args = ap.parse_args()
    if args.gate and not args.fresh:
        print("--gate requires --fresh (the live DB drifts per session -> flaky gates)")
        return 2

    repo = Path(".").resolve()
    if args.fresh:
        idx = _fresh_index(repo)
        db_label = "fresh temp re-init"
    else:
        db_path = Path(args.db) if args.db else repo / ".mind" / "index.db"
        if not db_path.exists():
            # Index.open CREATES the file — a bench must never plant an empty index
            # that makes the dashboard/CLI no-index detection lie afterwards.
            print(f"no index at {db_path} — run `recall init` first, or use --fresh")
            return 2
        idx = Index.open(db_path, repo=repo)
        db_label = str(db_path)

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    nodes = idx.db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    print(f"bench v2 · db {db_label} · {nodes} nodes · HEAD {_head_sha(repo)}")
    out = run(idx, cases)

    print(f"{'id':<5} {'track':<10} {'lang':<4} {'rank':>4} {'mixed':>5}  {'ms':>7}  question")
    for r in out["rows"]:
        hit = "YES" if 1 <= r["rank"] <= 3 else ("sil" if r["silenced"] else "no")
        print(f"{r['id']:<5} {r['track']:<10} {r['lang']:<4} "
              f"{r['rank'] or '-':>4} {r['mixed_rank'] or '-':>5}  {r['ms']:7.2f}  "
              f"{r['q'][:48]:<50} {hit}")
    print("-" * 100)

    ken = _bucket(out["rows"], "knowledge", "en")
    kde = _bucket(out["rows"], "knowledge", "de")
    code = _bucket(out["rows"], "code", "en")
    mean = out["total_ms"] / max(1, len(out["rows"]))
    print(f"knowledge-en  r@1 {ken['r1']}/{ken['n']}  r@3 {ken['r3']}/{ken['n']} "
          f"({100 * ken['r3'] // max(1, ken['n'])}%)   mixed-continuity r@3 {ken['mixed_r3']}/{ken['n']}")
    print(f"knowledge-de  r@1 {kde.get('r1', 0)}/{kde['n']}  r@3 {kde.get('r3', 0)}/{kde['n']}   (language-gap probes — never gated)")
    print(f"code          r@1 {code['r1']}/{code['n']}  r@3 {code['r3']}/{code['n']} "
          f"({100 * code['r3'] // max(1, code['n'])}%)")
    print(f"mean {mean:.2f} ms/q · silenced {sum(1 for r in out['rows'] if r['silenced'])}")

    if args.gate:
        k_ratio = ken["r3"] / max(1, ken["n"])
        c_ratio = code["r3"] / max(1, code["n"])
        if k_ratio < args.floor_knowledge or c_ratio < args.floor_code:
            print(f"GATE FAIL: knowledge {k_ratio:.2f} (floor {args.floor_knowledge}) "
                  f"code {c_ratio:.2f} (floor {args.floor_code})")
            return 1
        print("gate ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

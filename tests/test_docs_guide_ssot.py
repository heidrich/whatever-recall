"""Drift-guards for the SSOT documentation in docs/guide/ (2026-06-15).

docs/guide/*.md is the single source of truth for the product docs AND the source
the website's /docs page renders at build time (one source -> both). These guards
keep that contract intact: every section has valid frontmatter, slugs are unique,
order is a clean sequence, and every internal cross-link points at a real slug — so
the site build can't break on a typo and the sidebar stays coherent.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "guide"


def _sections():
    return sorted(p for p in GUIDE.glob("*.md") if p.name != "README.md")


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "missing YAML frontmatter"
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def test_guide_dir_exists_with_sections():
    assert GUIDE.is_dir(), "docs/guide is gone — the SSOT docs"
    assert len(_sections()) >= 6, "expected the core guide sections"


def test_every_section_has_required_frontmatter():
    for p in _sections():
        fm = _frontmatter(p.read_text(encoding="utf-8"))
        for key in ("title", "slug", "order", "summary"):
            assert key in fm and fm[key], f"{p.name}: frontmatter missing {key}"
        assert fm["order"].isdigit(), f"{p.name}: order must be an integer"


def test_slugs_are_unique_and_orders_sequential():
    fms = [_frontmatter(p.read_text(encoding="utf-8")) for p in _sections()]
    slugs = [f["slug"] for f in fms]
    assert len(slugs) == len(set(slugs)), "duplicate slug in docs/guide"
    orders = sorted(int(f["order"]) for f in fms)
    assert orders == list(range(1, len(orders) + 1)), f"order is not 1..N: {orders}"


def test_internal_cross_links_point_at_real_slugs():
    fms = [_frontmatter(p.read_text(encoding="utf-8")) for p in _sections()]
    slugs = {f["slug"] for f in fms}
    # markdown links whose target is a bare slug (no / . # ) must resolve
    link_re = re.compile(r"\]\(([a-z0-9-]+)\)")
    for p in _sections():
        for target in link_re.findall(p.read_text(encoding="utf-8")):
            assert target in slugs, f"{p.name}: cross-link to unknown slug '{target}'"


def test_readme_declares_ssot():
    txt = (GUIDE / "README.md").read_text(encoding="utf-8")
    assert "single source of truth" in txt.lower()
    assert "/docs" in txt, "README must explain the website renders these"

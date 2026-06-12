"""rules.py — layered override, core veto, frontmatter reader."""

from recall.rules import Rules, _merge, _parse_frontmatter, load_rules


def test_defaults_have_security_loudest():
    r = Rules.defaults()
    assert r.facet_weight("security") > r.facet_weight("ui")


def test_project_override_lowers_floor_but_core_vetoes(tmp_path):
    # a project tries to set silence_floor below the core minimum
    fm = {"silence_floor": 0, "core": {"silence_floor_min": 1}}
    merged = _merge(Rules.defaults(), fm)
    # _merge applies the raw value; load_rules' _enforce_core clamps it.
    from recall.rules import _enforce_core
    assert _enforce_core(merged).silence_floor >= 1


def test_tabu_is_additive():
    base = Rules.defaults()
    base.stay_silent_on = {"chore"}
    merged = _merge(base, {"stay_silent_on": ["secrets"]})
    assert "chore" in merged.stay_silent_on  # not lost
    assert "secrets" in merged.stay_silent_on  # added


def test_facet_weight_override_merges():
    merged = _merge(Rules.defaults(), {"facet_weights": {"ui": 3.0}})
    assert merged.facet_weight("ui") == 3.0
    assert merged.facet_weight("security") == 2.0  # untouched


def test_frontmatter_parses_blocks_lists_scalars():
    text = """---
silence_floor: 3
allowed_tags: [a, b, c]
facet_weights:
  security: 2.5
  ui: 0.5
---
body
"""
    fm = _parse_frontmatter(text)
    assert fm["silence_floor"] == 3
    assert fm["allowed_tags"] == ["a", "b", "c"]
    assert fm["facet_weights"] == {"security": 2.5, "ui": 0.5}


def test_load_rules_reads_bundled_default():
    # The shipped rules.md must load and carry the security weight.
    r = load_rules(repo=None)
    assert r.facet_weight("security") == 2.0
    assert r.silence_floor >= 1


def test_query_stopwords_are_additive_and_lowercased(tmp_path):
    """rules.md `query_stopwords:` ADDS to the shipped QUERY_STOP — governance for
    the one relevance knob that was hardcoded (also covers non-EN/DE teams)."""
    from recall.rules import load_rules
    proj = tmp_path / ".recall"
    proj.mkdir()
    (proj / "rules.md").write_text(
        "---\nquery_stopwords: [Quoi, encore]\n---\n", encoding="utf-8")
    r = load_rules(tmp_path)
    assert {"quoi", "encore"} <= r.query_stopwords  # lowercased, present
    # defaults stay default: nothing here can un-stop anchors.QUERY_STOP
    from recall.anchors import QUERY_STOP
    assert "what" in QUERY_STOP

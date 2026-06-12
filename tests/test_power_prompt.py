"""Drift-guards for STEP 4 — deterministic prompt build + parse (offline, ADR-005).

Zero network. The point of this layer: the model is ADVISORY, governance stays in
the engine. We pin that a hallucinated tag / edge kind is dropped (never trusted),
that aliases canonicalize, that a malformed response degrades to [] (never crashes
`recall power`), and that the system prompt actually states the closed vocabulary.
"""

from __future__ import annotations

import json

from recall.power_prompt import (
    build_understanding_prompt,
    parse_stamp_instructions,
    parse_with_report,
)
from recall.rules import Rules


def _rules() -> Rules:
    return Rules.defaults()  # allowed_tags + edge_kinds from the canonical vocabulary


# ----------------------------------------------------------------- prompt build
def test_system_prompt_hardcodes_the_closed_vocabulary():
    rules = _rules()
    system, user = build_understanding_prompt(
        file_path="src/auth.py", source="def login(): ...", existing=None, rules=rules
    )
    # a representative tag + edge kind must appear in the contract the model sees
    assert "security" in system and "ui" in system
    assert "implements" in system and "supersedes" in system
    assert "src/auth.py" in user and "def login()" in user


def test_user_message_repeats_the_contract_at_the_end_for_recency():
    """The fix that actually flipped claude-cli (2026-06-07): agent CLIs override even a
    strict --system-prompt with their own code-description shape. Repeating the exact
    output contract at the END of the user message (recency) is what makes them comply.
    This guards that the tail is present, names the wrong keys, and ends on the schema."""
    _, user = build_understanding_prompt(
        file_path="f.py", source="def x(): ...", existing=None, rules=_rules()
    )
    # the contract must come AFTER the source (recency) and name the failure shapes
    assert user.index("SOURCE:") < user.index('"nodes"')
    assert "public_api" in user and "purpose" in user  # the wrong keys, named as FAILED
    assert user.rstrip().endswith('{"nodes": []}.')  # the very last thing the model reads


def test_prompt_is_deterministic():
    rules = _rules()
    a = build_understanding_prompt(file_path="f.py", source="x", existing=None, rules=rules)
    b = build_understanding_prompt(file_path="f.py", source="x", existing=None, rules=rules)
    assert a == b  # same inputs -> identical bytes (sorted vocab, no randomness)


def test_existing_knowledge_is_included_for_enrichment():
    rules = _rules()
    _, user = build_understanding_prompt(
        file_path="f.py",
        source="x",
        existing=[{"title": "old fact", "why": "the prior meaning"}],
        rules=rules,
    )
    assert "old fact" in user and "the prior meaning" in user
    assert "enrich" in user.lower()


# ---- robust JSON extraction (the claude-cli dogfood bug: replies wrapped in prose) ----
_GOOD = '{"nodes":[{"title":"T","why":"w","anchors":["a"],"tags":[],"edges":[]}]}'


def test_parses_pure_json():
    insts = parse_stamp_instructions(_GOOD, _rules())
    assert len(insts) == 1 and insts[0].title == "T"


def test_parses_json_wrapped_in_a_fenced_block():
    """claude --print and other agent CLIs wrap JSON in ```json … ``` even when asked
    for strict JSON — the dogfood run stamped 0 nodes because of exactly this."""
    reply = "Here is the knowledge I extracted:\n\n```json\n" + _GOOD + "\n```\n\nDone."
    insts = parse_stamp_instructions(reply, _rules())
    assert len(insts) == 1 and insts[0].title == "T"


def test_parses_json_embedded_in_prose():
    reply = "I read the file. " + _GOOD + " That's the result."
    insts = parse_stamp_instructions(reply, _rules())
    assert len(insts) == 1 and insts[0].title == "T"


def test_non_json_reply_yields_nothing_not_a_crash():
    assert parse_stamp_instructions("I could not find anything noteworthy.", _rules()) == []
    assert parse_stamp_instructions("", _rules()) == []


# ----------------------------------------------------------------- parse: good
def test_parse_good_response():
    rules = _rules()
    payload = json.dumps({
        "nodes": [{
            "title": "login flow",
            "why": "validates the session before the redirect",
            "anchors": ["login", "session", "Redirect"],
            "tags": ["security", "backend"],
            "edges": [{"kind": "implements", "target": "auth.py"}],
        }]
    })
    out = parse_stamp_instructions(payload, rules)
    assert len(out) == 1
    inst = out[0]
    assert inst.title == "login flow"
    # anchors are cleaned into searchable terms (order irrelevant -> compare as a set)
    assert set(inst.anchors) == {"login", "session", "redirect"}  # lowercased
    assert inst.tags == ["security", "backend"]
    assert inst.edges == [("implements", "auth.py")]
    assert inst.dropped_tags == [] and inst.dropped_edges == []


def test_aliases_are_canonicalized():
    rules = _rules()
    payload = json.dumps({"nodes": [{"title": "t", "tags": ["sec", "css", "perf"]}]})
    out = parse_stamp_instructions(payload, rules)
    assert out[0].tags == ["security", "ui", "performance"]  # sec->security, css->ui, perf->performance


# ----------------------------------------------------------------- parse: hallucinations
def test_hallucinated_tag_is_dropped_and_recorded():
    rules = _rules()
    payload = json.dumps({"nodes": [{"title": "t", "tags": ["security", "blockchain", "vibes"]}]})
    out = parse_stamp_instructions(payload, rules)
    assert out[0].tags == ["security"]  # the two junk tags never survive
    assert set(out[0].dropped_tags) == {"blockchain", "vibes"}  # but they're recorded for transparency


def test_hallucinated_edge_kind_is_dropped():
    rules = _rules()
    payload = json.dumps({"nodes": [{
        "title": "t",
        "edges": [
            {"kind": "implements", "target": "a.py"},
            {"kind": "teleports_to", "target": "b.py"},  # not in edge_kinds
        ],
    }]})
    out = parse_stamp_instructions(payload, rules)
    assert out[0].edges == [("implements", "a.py")]
    assert out[0].dropped_edges == [("teleports_to", "b.py")]


# ----------------------------------------------------------------- parse: malformed
def test_malformed_json_returns_empty_not_crash():
    rules = _rules()
    assert parse_stamp_instructions("{ not json", rules) == []
    assert parse_stamp_instructions("", rules) == []
    assert parse_stamp_instructions("[]", rules) == []  # wrong top-level shape
    assert parse_stamp_instructions(json.dumps({"nodes": "nope"}), rules) == []


def test_node_without_title_is_skipped():
    rules = _rules()
    payload = json.dumps({"nodes": [{"why": "no title here"}, {"title": "ok"}]})
    out = parse_stamp_instructions(payload, rules)
    assert len(out) == 1 and out[0].title == "ok"


def test_empty_nodes_is_valid_silence():
    rules = _rules()
    assert parse_stamp_instructions(json.dumps({"nodes": []}), rules) == []


def test_edge_as_pair_list_also_parses():
    """Tolerate the model returning edges as [kind, target] pairs, not just objects."""
    rules = _rules()
    payload = json.dumps({"nodes": [{"title": "t", "edges": [["implements", "x.py"]]}]})
    out = parse_stamp_instructions(payload, rules)
    assert out[0].edges == [("implements", "x.py")]


# -------------------------------------------------- THE dogfood bug: wrong schema, loudly
def test_prompt_states_nodes_is_the_only_top_level_key_with_a_worked_example():
    """The schema-mismatch bug (claude-cli answered {file, role, facts}) is fought first in
    the prompt: it must show the worked example AND name the wrong shapes as failures."""
    system, _ = build_understanding_prompt(
        file_path="f.py", source="x", existing=None, rules=_rules()
    )
    assert '"nodes"' in system
    assert '"file"' in system and '"facts"' in system  # the wrong shape is named as WRONG
    assert "WRONG" in system
    assert system.count('"title"') >= 2  # the schema block AND the worked example


def test_schema_mismatch_is_reported_not_silent():
    """{file, role, facts} parsed fine as JSON but had no node key — the exact dogfood bug.
    It must come back as a LOUD 'no-node-key', never a silent empty list that hides 45/50."""
    rules = _rules()
    bad = json.dumps({"file": "auth.py", "role": "login handler", "facts": ["validates session"]})
    rep = parse_with_report(bad, rules)
    assert rep.instructions == []
    assert rep.reason == "no-node-key"
    assert rep.yielded_nothing is True  # <- this is what run_power counts and surfaces


def test_honest_empty_is_not_counted_as_a_failure():
    """An explicit {"nodes": []} is the model legitimately finding nothing — NOT a failure.
    yielded_nothing must stay False so honest silence never inflates the discard count."""
    rep = parse_with_report(json.dumps({"nodes": []}), _rules())
    assert rep.instructions == [] and rep.reason == "empty"
    assert rep.yielded_nothing is False


def test_near_miss_top_level_key_is_forgiven_and_recorded():
    """A model that writes "lessons" instead of "nodes" still lands (bounded tolerance),
    but the off-schema key is recorded so the drift is visible, not invisible."""
    rules = _rules()
    payload = json.dumps({"lessons": [{"title": "t", "why": "w", "tags": ["security"]}]})
    rep = parse_with_report(payload, rules)
    assert len(rep.instructions) == 1 and rep.instructions[0].title == "t"
    assert rep.used_alt_key == "lessons"
    assert rep.yielded_nothing is False


def test_per_node_field_aliases_are_mapped():
    """A node using 'name'/'role' instead of 'title'/'why' still maps — bounded aliasing."""
    rules = _rules()
    payload = json.dumps({"nodes": [{"name": "the thing", "role": "what it does"}]})
    out = parse_stamp_instructions(payload, rules)
    assert len(out) == 1
    assert out[0].title == "the thing" and out[0].body == "what it does"


def test_bare_array_of_nodes_is_tolerated():
    """The model dropped the {"nodes": ...} wrapper and returned a bare array — forgiven."""
    rules = _rules()
    payload = json.dumps([{"title": "t", "why": "w"}])
    out = parse_stamp_instructions(payload, rules)
    assert len(out) == 1 and out[0].title == "t"


def test_arbitrary_unknown_top_level_key_is_rejected_loudly():
    """Tolerance is BOUNDED: a key outside the known near-miss set is a failure, not
    accepted. The model cannot invent {"whatever": [...]} and have it silently work."""
    rules = _rules()
    payload = json.dumps({"whatever_random_key": [{"title": "t"}]})
    rep = parse_with_report(payload, rules)
    assert rep.instructions == []
    assert rep.reason == "no-node-key" and rep.yielded_nothing is True

"""anchors.py — extraction + ADR-005 tag canonicalization."""

from recall.anchors import canonicalize_tags, clean_anchor_terms, extract_anchors


def test_extracts_technical_symbols():
    a = extract_anchors("the createPortal call in .ed-root and workspace_id NULL")
    assert "workspace_id" in a
    assert "ed-root" in a          # leading dot dropped — the anchor is the term
    assert "createportal" in a     # lowercased


def test_extracts_domain_ids():
    a = extract_anchors("see ADR-49 and migration #34 in phase 2")
    # ADR-49 keeps its hyphen (query + stamp use the same extractor, so they match);
    # "migration #34" has no hyphen so it collapses to migration34.
    assert "adr-49" in a
    assert "migration34" in a


def test_stopwords_dropped():
    a = extract_anchors("the and or with this that fix commit")
    assert "the" not in a and "commit" not in a


def test_canonicalize_aliases_and_drops_unknown():
    assert canonicalize_tags(["sec", "Auth", "css", "wat"]) == ["security", "ui"]


def test_canonicalize_dedupes():
    # both alias to security -> one entry
    assert canonicalize_tags(["rls", "xss", "auth"]) == ["security"]


def test_canonicalize_empty():
    assert canonicalize_tags([]) == []
    assert canonicalize_tags(["", "  "]) == []


# --- clean_anchor_terms: turn model anchors into searchable terms (the Power-anchor fix) ---
def test_clean_keeps_short_search_terms():
    out = clean_anchor_terms(["modal", "rate limit", "SSRF"])
    assert "modal" in out and "ssrf" in out
    assert "rate limit" in out  # a 2-word term is kept verbatim…
    assert "rate" in out and "limit" in out  # …AND exploded so plain queries match


def test_clean_explodes_a_phrase_anchor():
    """The exact Power bug: the model returned a whole sentence as one anchor, which
    stored as one FTS term that no short query could match. It must be exploded."""
    out = clean_anchor_terms(["heavy modals are rendered lazily"])
    assert "heavy modals are rendered lazily" not in out  # the blob is NOT stored
    assert {"heavy", "modals", "rendered", "lazily"} <= out  # its tokens are


def test_clean_explodes_a_pasted_code_line():
    out = clean_anchor_terms(["const vecontentmodal = dynamic("])
    assert "const vecontentmodal = dynamic(" not in out
    assert "vecontentmodal" in out or "dynamic" in out  # real tokens survive


def test_clean_handles_code_punctuation_terms():
    out = clean_anchor_terms(["next/dynamic"])
    assert "next/dynamic" in out          # the literal term is kept (it's short)…
    assert "next" in out and "dynamic" in out  # …and its parts are searchable


def test_clean_drops_lone_stopwords_and_empties():
    out = clean_anchor_terms(["the", "  ", "", "and"])
    assert out == set()

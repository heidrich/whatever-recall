"""Drift-guards for search-inversion (ADR-037) — recall.resolve + Index.resolve.

The thesis: an AI hallucinates a search term from training; this repo uses a
different word; recall corrects it BEFORE the grep (vocabulary mismatch). These
lock the proven behaviors from the prototype bench:
  - tokenization splits camelCase AND folds singular/plural (the CamelCase boundary
    is where blind grep returns 0);
  - vocabulary RANKS, experience is a TIE-BREAK only (never overrides);
  - the FLYWHEEL: a learned synonym (zero token overlap, mined from access_log)
    surfaces a symbol only when WARM — the cold->warm climb;
  - the ANTI-HIDING gate: resolve re-ranks but NEVER drops a candidate.
"""
from __future__ import annotations

from recall.engine import Index
from recall.resolve import Resolver, tokens, detokenize, _stem


def test_tokenize_splits_camel_and_folds_plural():
    assert tokens("enforceSeats") == ["enforce", "seat"]      # plural folded
    assert tokens("seatLimit") == ["seat", "limit"]
    assert tokens("recall_reserve_seat") == ["recall", "reserve", "seat"]
    assert detokenize("confirmSeatOrRollback") == "confirm seat or rollback"
    # over-stemming guard: 'ss' and short tokens are NOT stripped
    assert "class" in tokens("classRoom")
    assert "is" in tokens("isReady")


def _idx_with_symbols(*syms):
    idx = Index.open(":memory:")
    for i, (sym, fp) in enumerate(syms):
        idx.stamp(title=sym, anchors=[sym.lower(), f"tag{i}"], kind="code-symbol",
                  file_path=fp, symbol=sym, line=i + 1, origin="bootstrap")
    idx.db.commit()
    return idx


def test_vocabulary_correction_surfaces_the_real_term():
    """`seatsUsed` exists, the guess `seatLimit` does not — resolve must surface the
    real seat symbol (shared stemmed token 'seat'), not return nothing."""
    idx = _idx_with_symbols(("seatsUsed", "orgs.ts"), ("rateLimit", "ratelimit.ts"),
                            ("fetchUser", "auth.ts"))
    out = idx.resolve("seatLimit", warmth=0.0, top=5)["candidates"]
    syms = [c["symbol"] for c in out]
    assert "seatsUsed" in syms, "vocabulary correction missed the real seat symbol"
    idx.db.close()


def test_experience_is_tiebreak_not_override():
    """Two symbols with EQUAL vocabulary match — experience may reorder them, but a
    vocabulary-IRRELEVANT but heavily-experienced symbol must NOT jump the rank."""
    idx = _idx_with_symbols(("seatGuard", "a.ts"), ("seatCheck", "b.ts"),
                            ("totallyUnrelated", "c.ts"))
    # pile experience on the unrelated symbol
    nid = idx.db.execute("select id from nodes where symbol='totallyUnrelated'").fetchone()[0]
    for _ in range(50):
        idx.db.execute("insert into access_log(query,node_id,score,surfaced,latency_us,consumer,kind)"
                       " values(?,?,?,?,?,?,?)", ("x", nid, 0, 1, 0, "cli", "recall"))
    idx.db.commit()
    out = idx.resolve("seat", warmth=1.0, top=5)["candidates"]
    syms = [c["symbol"] for c in out]
    # the seat symbols rank above the unrelated one despite its experience
    assert syms and syms[0] in ("seatGuard", "seatCheck")
    if "totallyUnrelated" in syms:
        assert syms.index("totallyUnrelated") > 1, "experience overrode vocabulary"
    idx.db.close()


def test_flywheel_learned_synonym_only_when_warm():
    """`cancel` shares ZERO tokens with `lapseOrgByCustomer`. Cold: no hit. Warm:
    surfaced #1 via a synonym mined from the access_log — the cold->warm climb."""
    idx = _idx_with_symbols(("lapseOrgByCustomer", "billing.ts"), ("setCancel", "ui.tsx"))
    nid = idx.db.execute("select id from nodes where symbol='lapseOrgByCustomer'").fetchone()[0]
    for _ in range(3):
        idx.db.execute("insert into access_log(query,node_id,score,surfaced,latency_us,consumer,kind)"
                       " values(?,?,?,?,?,?,?)", ("cancel subscription", nid, 0, 1, 0, "cli", "recall"))
    idx.db.commit()
    r = Resolver(idx.db)
    assert "cancel" in r._synonyms and "lapseOrgByCustomer" in r._synonyms["cancel"]
    cold = [c.symbol for c in r.resolve("cancel", warmth=0.0, top=3)]
    warm = r.resolve("cancel", warmth=1.0, top=3)
    warm_syms = [c.symbol for c in warm]
    # cold: the synonym target is UNREACHABLE (zero token overlap with 'cancel')
    assert "lapseOrgByCustomer" not in cold, "synonym must NOT fire cold"
    # warm: it surfaces, tagged as a learned synonym — the cold->warm climb. (A
    # symbol that literally CARRIES the word, like setCancel, may still rank above
    # it; the proof is that the synonym appears AT ALL when warm and not when cold.)
    assert "lapseOrgByCustomer" in warm_syms, "synonym must surface warm"
    via = next(c for c in warm if c.symbol == "lapseOrgByCustomer").via_synonym
    assert via == "cancel"
    idx.db.close()


def test_synonyms_ignore_commit_consumer():
    """Auto-stamp traffic (consumer=commit) must NOT be mined as a human search —
    else every commit message poisons the synonym map."""
    idx = _idx_with_symbols(("lapseOrgByCustomer", "billing.ts"))
    nid = idx.db.execute("select id from nodes where symbol='lapseOrgByCustomer'").fetchone()[0]
    idx.db.execute("insert into access_log(query,node_id,score,surfaced,latency_us,consumer,kind)"
                   " values(?,?,?,?,?,?,?)", ("cancel subscription", nid, 0, 1, 0, "commit", "recall"))
    idx.db.commit()
    r = Resolver(idx.db)
    assert "cancel" not in r._synonyms, "commit-consumer traffic leaked into synonyms"
    idx.db.close()


def test_synonyms_ignore_resolve_consumer():
    """resolve's OWN logged traffic (consumer=resolve) must NOT feed the synonym miner
    — else the flywheel learns from its own suggestions and self-poisons (bug-hunt P2,
    2026-06-15). Only genuine human/agent searches (cli/mcp) teach a synonym."""
    idx = _idx_with_symbols(("lapseOrgByCustomer", "billing.ts"))
    nid = idx.db.execute("select id from nodes where symbol='lapseOrgByCustomer'").fetchone()[0]
    idx.db.execute("insert into access_log(query,node_id,score,surfaced,latency_us,consumer,kind)"
                   " values(?,?,?,?,?,?,?)", ("cancel subscription", nid, 0, 1, 0, "resolve", "resolve"))
    idx.db.commit()
    r = Resolver(idx.db)
    assert "cancel" not in r._synonyms, "resolve-consumer traffic self-poisoned the synonyms"
    idx.db.close()


def test_anti_hiding_resolve_never_drops_below_top():
    """The anti-hiding gate: resolve re-ranks but the candidate set is just the top-k
    of the FULL vocabulary match — it never structurally hides a matching symbol.
    A fresh, unstamped symbol with a strong token match still appears."""
    idx = _idx_with_symbols(("seatBrandNewUnstamped", "fresh.ts"), ("seatOld", "old.ts"))
    # give the old one experience; the new one has none
    nid = idx.db.execute("select id from nodes where symbol='seatOld'").fetchone()[0]
    for _ in range(20):
        idx.db.execute("insert into access_log(query,node_id,score,surfaced,latency_us,consumer,kind)"
                       " values(?,?,?,?,?,?,?)", ("seat", nid, 0, 1, 0, "cli", "recall"))
    idx.db.commit()
    out = [c["symbol"] for c in idx.resolve("seat", warmth=1.0, top=5)["candidates"]]
    assert "seatBrandNewUnstamped" in out, "experience hid a fresh symbol — anti-hiding broken"
    idx.db.close()


def test_resolve_reports_index_warmth():
    idx = _idx_with_symbols(("foo", "a.ts"))
    res = idx.resolve("foo", top=3)
    assert "index_warmth" in res and 0.0 <= res["index_warmth"] <= 1.0
    assert "candidates" in res
    idx.db.close()


# --- dogfood hardening (2026-06-15): tokenizer edge cases + degenerate inputs ---

def test_tokenizer_handles_acronym_boundaries():
    """ALLCAPS run -> next-word boundary (the `XMLHttpRequest` case). Locks the regex's
    `(?<=[A-Z])(?=[A-Z][a-z])` rule so an acronym doesn't swallow the following word."""
    assert tokens("HTTPServer") == ["http", "server"]
    assert tokens("getHTTPResponse") == ["get", "http", "response"]
    assert tokens("XMLHttpRequest") == ["xml", "http", "request"]
    assert tokens("parseURL") == ["parse", "url"]


def test_stemmer_does_not_overstrip():
    """The tiny stemmer strips ONE trailing s on len>3 tokens, but never an 'ss' word.
    'business'/'address'/'class' must stay whole (folding them merges unrelated terms)."""
    assert _stem("business") == "business"
    assert _stem("address") == "address"
    assert _stem("class") == "class"
    assert _stem("seats") == "seat"          # the intended plural fold still works
    assert _stem("is") == "is"               # too short to strip


def test_tokenize_empty_and_nonalnum_is_safe():
    """Empty / whitespace / punctuation-only guesses must not crash and yield []."""
    assert tokens("") == []
    assert tokens("   ") == []
    assert tokens("___") == []
    assert tokens("!!!") == []
    assert detokenize("") == ""


def test_resolve_on_empty_guess_does_not_crash():
    """A degenerate guess (empty / whitespace) must return a well-formed, empty-ish
    result, never raise (the CLI/MCP can pass through whatever the AI typed)."""
    idx = _idx_with_symbols(("seatsUsed", "orgs.ts"))
    for g in ("", "   ", "___"):
        res = idx.resolve(g, top=5)
        assert "candidates" in res and isinstance(res["candidates"], list)
        assert "index_warmth" in res
    idx.db.close()


def test_resolve_on_empty_index_does_not_crash():
    """A cold repo with zero stamped symbols must resolve to an empty candidate list,
    not divide-by-zero in the IDF weighting."""
    idx = Index.open(":memory:")
    res = idx.resolve("seatLimit", top=5)
    assert res["candidates"] == []
    idx.db.close()


# --- knowledge-mined synonyms (2026-06-18, drift-guard search-bench gap) ---
# The access_log flywheel only knows synonyms someone ALREADY searched. An AI asks a
# concept question ("what stops weakening an invariant") on day one — zero prior searches.
# The repo, though, wrote those concept words at write-time: a lesson/decision/commit body
# names the concept and is wired to a file. We mine THAT — the dev's own why-prose becomes
# the synonym bridge, generalizing as the corpus grows. These lock that behavior + its
# safety rails (stop/IDF filter, never fires cold, never drops).

def _idx_with_knowledge(symbol, fp, *, title, body):
    """A repo with one code symbol and one knowledge node wired to its file."""
    idx = Index.open(":memory:")
    idx.stamp(title=symbol, anchors=[symbol.lower(), "sym"], kind="code-symbol",
              file_path=fp, symbol=symbol, line=1, origin="bootstrap")
    idx.stamp(title=title, body=body, anchors=["k1", "k2"], kind="lesson",
              file_path=fp, dedup=False)
    idx.db.commit()
    return idx


def test_knowledge_prose_bridges_a_concept_word_to_the_files_symbols():
    """A concept word in a wired lesson body ('drift') that is NOT a code token of the
    file's symbols becomes a synonym bridging to that file's symbol — the write-time
    bridge the access_log flywheel can't know cold."""
    idx = _idx_with_knowledge(
        "checkAuditInvariants", "tests/test_audit.py",
        title="The guard prevents silent drift",
        body="This test stops a future edit from quietly weakening the security invariant.")
    r = Resolver(idx.db)
    # 'drift' shares no token with 'checkAuditInvariants' but the wired prose carries it
    assert "drift" in r._synonyms, "concept word from wired prose did not become a synonym"
    assert "checkAuditInvariants" in r._synonyms["drift"]
    idx.db.close()


def test_knowledge_synonyms_skip_boilerplate_and_own_tokens():
    """Stopwords ('the','code') and the file's OWN symbol tokens must NOT bridge — only
    rare CONCEPT words. (Own tokens already resolve lexically; boilerplate is noise.)"""
    idx = _idx_with_knowledge(
        "auditGuard", "tests/test_audit.py",
        title="The audit guard for the code",
        body="the the the audit guard code")
    r = Resolver(idx.db)
    assert "the" not in r._synonyms, "stopword leaked into synonyms"
    assert "code" not in r._synonyms, "generic boilerplate leaked into synonyms"
    # 'audit'/'guard' are the file's OWN symbol tokens → resolve lexically, not via synonym
    assert "auditGuard" not in r._synonyms.get("audit", []), "own token bridged needlessly"
    idx.db.close()


def test_resolver_is_cached_but_a_stamp_invalidates_it():
    """Perf: resolve() reuses a per-process-warm Resolver (building it loads all symbols +
    mines synonyms, ~55ms — was rebuilt every call). Correctness: a stamp/mutation MUST
    invalidate the cache so a freshly-stamped symbol is immediately resolvable, never stale."""
    idx = _idx_with_symbols(("alphaSym", "a.ts"))
    idx.resolve("alphaSym")            # warms the resolver cache
    assert idx._resolver is not None   # cached
    idx.stamp(title="betaSym", anchors=["beta"], kind="code-symbol",
              file_path="b.ts", symbol="betaSym", line=2, origin="bootstrap")
    idx.db.commit()
    assert idx._resolver is None, "a stamp must drop the cached resolver (else stale results)"
    out = [c["symbol"] for c in idx.resolve("betaSym")["candidates"]]
    assert "betaSym" in out, "freshly-stamped symbol not resolvable — cache went stale"
    idx.db.close()


def test_trigram_sim_sets_matches_the_scalar_form():
    """The precomputed-set trigram Jaccard must be byte-identical to the original scalar
    form — the speedup (precompute each symbol's set once) may not change any score."""
    from recall.resolve import _trigram_sim, _trigram_sim_sets, _trigrams
    pairs = [("rateLimit", "rate limit ip"), ("enforceSeats", "confirmSeatOrRollback"),
             ("downgrade", "downgradeOrgForOwner"), ("", "anything"), ("x", "")]
    for a, b in pairs:
        assert abs(_trigram_sim(a, b) - _trigram_sim_sets(_trigrams(a), _trigrams(b))) < 1e-12


def test_knowledge_synonym_fires_only_when_warm():
    """Like the access_log flywheel, a knowledge-mined synonym (zero token overlap) must
    surface only WHEN WARM — cold is pure vocabulary, no synonym expansion."""
    idx = _idx_with_knowledge(
        "checkInvariants", "tests/test_audit.py",
        title="Prevents drift",
        body="stops a future edit from weakening the invariant")
    cold = [c["symbol"] for c in idx.resolve("drift", warmth=0.0, top=5)["candidates"]]
    warm = [c["symbol"] for c in idx.resolve("drift", warmth=1.0, top=5)["candidates"]]
    assert "checkInvariants" not in cold, "knowledge synonym fired cold"
    assert "checkInvariants" in warm, "knowledge synonym did not surface warm"
    idx.db.close()

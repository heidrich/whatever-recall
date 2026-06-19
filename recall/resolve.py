"""Search-inversion (ADR-037): resolve a HALLUCINATED search term into the real
vocabulary of THIS repo, BEFORE anything greps.

Everyone else optimizes the hit ("find what the human asked for better"). recall
optimizes the moment before: WHAT is even searched for. An AI invents the term
from its training — `enforceSeats` — and in this repo it's actually
`confirmSeatOrRollback`. recall hands over the real term before the grep wastes a
round. The 30-year-old IR name for the problem is *vocabulary mismatch*; the novel
part is that recall corrects it from **write-time repo experience**, not text
statistics or embeddings.

Two owner truths drive the design (the flywheel):
  1. "Old human code is dumb at first — so the suggestions are dumb at first."
  2. "Every commit makes the code smarter, every suggestion richer."

So the resolver has a WARMTH knob (0=cold..1=warm):
  - COLD: pure vocabulary correction (IDF-weighted fuzzy match against the real
    symbols). Already kills the vocabulary mismatch; ranks blind. "I know the
    words, not their history yet."
  - WARM: vocabulary + a re-rank tie-break from lived experience (access_log,
    node_feedback, importance) + a synonym layer mined from what searches actually
    landed (`cancel` → `lapseOrgByCustomer`, zero token overlap). The cold→warm
    climb IS the flywheel.

Hard lesson baked in (ADR-028, engine.py): vocabulary RANKS; experience is a
TIE-BREAK only. Folding experience into the headline rank buries the
vocabulary-right symbol under experienced-but-wrong ones. The anti-hiding gate:
the resolver re-ranks and annotates but NEVER drops a candidate (grep stays the
complete recall) — so fresh, unstamped code can't be hidden, only ranked lower.

Read-only against the index. Pure stdlib. 0 model tokens.
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

# camelCase / snake_case / kebab / PascalCase → lowercase word-pieces. THIS is the
# inversion's first move: an AI guesses one fused token (`enforceSeats`); the repo
# is indexed in words. Splitting the guess is what bridges the two — measured: the
# CamelCase boundary is exactly where blind grep returns 0.
_SPLIT = re.compile(r"[^a-zA-Z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# vocabulary score below this = truly unrelated, not a proposal.
_MIN_VOCAB = 0.04
# a test-file symbol is a soft downweight on the vocabulary axis (mirrors the
# engine's tests-at-half-weight rule).
_TEST_PENALTY = 0.6


def _stem(t: str) -> str:
    """A deliberately tiny stemmer: fold a trailing plural/3rd-person `s` so `seat`
    and `seats`, `enforce` and `enforces` share a token. This is exactly the
    singular/plural half of the CamelCase-boundary break the bench found (`seatLimit`
    guessed vs `seatsUsed` real). Kept minimal on purpose — over-stemming would
    merge unrelated words; we only strip one trailing s on tokens length>3."""
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def tokens(s: str) -> list[str]:
    if not s:
        return []
    return [_stem(t.lower()) for t in _SPLIT.split(s) if t]


def detokenize(guess: str) -> str:
    """`enforceSeats` → `enforce seats` — the word-split form to feed a word index."""
    return " ".join(t for t in _SPLIT.split(guess) if t).lower() if guess else ""


def _trigrams(s: str) -> set[str]:
    s = "  " + re.sub(r"[^a-z0-9]", "", s.lower()) + "  "
    return {s[i : i + 3] for i in range(len(s) - 2)}


def _trigram_sim(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _trigram_sim_sets(ta: set[str], tb: set[str]) -> float:
    """Jaccard over two PRE-COMPUTED trigram sets. Identical math to _trigram_sim, but
    the resolve hot loop precomputes each symbol's set ONCE (in _load) and the guess's
    ONCE per query, instead of rebuilding ~2k sets via re.sub on every single call —
    that rebuild was 84% of cached-resolve latency (profiled 2026-06-18)."""
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / (len(ta) + len(tb) - inter)


@dataclass
class Candidate:
    symbol: str
    file_path: str
    line: int
    node_id: int
    importance: float = 0.0
    access_count: int = 0
    useful: int = 0
    missed: int = 0
    vocab_score: float = 0.0
    exp_score: float = 0.0
    score: float = 0.0
    via_synonym: str = ""  # the learned synonym word that bridged, if any
    why: list[str] = field(default_factory=list)


class Resolver:
    """Resolve a guessed term against the real vocabulary of an open index.

    Pass the SAME sqlite connection the engine uses (Index.db) — read-only here, no
    second handle, no lock contention."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._load()

    def _load(self) -> None:
        self.cands: list[Candidate] = []
        rows = self.db.execute(
            "SELECT id, symbol, file_path, line, importance FROM nodes "
            "WHERE symbol IS NOT NULL AND symbol != ''"
        ).fetchall()
        access = {
            r["node_id"]: r["c"]
            for r in self.db.execute(
                # Experience axis = genuine human/agent searches only. EXCLUDE resolve's
                # OWN logged traffic (consumer='resolve') — engine.resolve() logs its top
                # candidate surfaced=1 on every call, so counting it here lets resolve
                # inflate the experience of whatever it already ranked #1 (self-poison of
                # the experience axis — the same class as the synonym fix). Also exclude
                # 'commit' auto-stamp noise. (P2 bug-hunt 2026-06-15.)
                "SELECT node_id, COUNT(*) c FROM access_log "
                "WHERE surfaced=1 AND consumer NOT IN ('resolve','commit') GROUP BY node_id"
            ).fetchall()
        }
        fb = {
            r["node_id"]: (r["useful_count"], r["missed_count"])
            for r in self.db.execute(
                "SELECT node_id, useful_count, missed_count FROM node_feedback"
            ).fetchall()
        }
        for r in rows:
            u, m = fb.get(r["id"], (0, 0))
            self.cands.append(
                Candidate(
                    symbol=r["symbol"], file_path=r["file_path"] or "",
                    line=r["line"] or 0, node_id=r["id"],
                    importance=float(r["importance"] or 0.0),
                    access_count=access.get(r["id"], 0), useful=u, missed=m,
                )
            )
        self._cand_tokens = [set(tokens(c.symbol)) for c in self.cands]
        # each symbol's trigram set, precomputed ONCE — the resolve hot loop reuses these
        # instead of rebuilding ~2k sets per query (the dominant cost; profiled 2026-06-18).
        self._cand_trigrams = [_trigrams(c.symbol) for c in self.cands]
        # IDF: a shared RARE token (`seat`) means far more than a common one
        # (`get`, `limit`, `test`). Same idf-beats-volume rule the engine bench locks.
        df: Counter[str] = Counter()
        for ts in self._cand_tokens:
            for t in ts:
                df[t] += 1
        n_docs = max(len(self.cands), 1)
        self._idf = {t: math.log(1 + n_docs / (1 + c)) for t, c in df.items()}
        self._max_access = max((c.access_count for c in self.cands), default=1) or 1
        self._max_imp = max((c.importance for c in self.cands), default=1.0) or 1.0
        self._synonyms = self._learn_synonyms()

    def _learn_synonyms(self) -> dict[str, list[str]]:
        """The flywheel layer: mine the access_log for guesses that LED to a symbol
        whose name shares NO token with the guess — a synonym the repo taught us by
        what actually worked (`cancel subscription` → lapseOrgByCustomer). A cold
        repo has none; every recall-coded search makes it richer. We exclude NON-human
        consumers: 'commit' (auto-stamp traffic) AND 'resolve' itself — else the
        flywheel would learn from its OWN suggestions and self-poison (bug-hunt P2,
        2026-06-15). Only genuine human/agent searches (cli/mcp) teach a synonym."""
        syn: dict[str, list[str]] = {}
        try:
            rows = self.db.execute(
                "SELECT a.query q, n.symbol s, COUNT(*) c "
                "FROM access_log a JOIN nodes n ON n.id=a.node_id "
                "WHERE a.query IS NOT NULL AND a.query!='' AND n.symbol IS NOT NULL "
                "AND n.symbol!='' AND a.surfaced=1 AND a.consumer NOT IN ('commit','resolve') "
                "GROUP BY a.query, n.symbol"
            ).fetchall()
        except sqlite3.Error:
            return syn
        for r in rows:
            qt, st = set(tokens(r["q"])), set(tokens(r["s"]))
            if qt and not (qt & st):  # zero token overlap → a learned synonym
                for w in qt:
                    syn.setdefault(w, [])
                    if r["s"] not in syn[w]:
                        syn[w].append(r["s"])
        # SECOND source — the knowledge corpus (ADR-037 + drift-guard bench gap, 2026-06-18):
        # the access_log flywheel only knows synonyms that someone ALREADY searched. But an AI
        # asks "what stops a future edit from weakening an invariant" on day one — zero prior
        # searches, so the flywheel is blind and the lexical match misses (the words aren't code
        # tokens). The repo, however, already wrote those concept words at write-time: every
        # lesson/decision/commit body that names a CONCEPT ("drift", "invariant", "idempotent")
        # is wired to a file via file_path. Mine that — the dev's own why-prose becomes the
        # synonym bridge, and it grows automatically with the corpus. Same anti-self-poison
        # rule (exclude resolve's own traffic — irrelevant here, knowledge isn't access_log)
        # and same "vocabulary ranks" contract (it expands the vocabulary a symbol is KNOWN by,
        # never the experience score; resolve() still floors via-synonym vocab, never drops).
        self._merge_knowledge_synonyms(syn)
        return syn

    # concept words an AI reaches for that rarely appear verbatim as code tokens — the
    # vocabulary-mismatch core. Mined from knowledge prose, not hardcoded as the answer:
    # this set only decides which WORDS are worth bridging, the BRIDGE itself is learned
    # from what the repo actually wrote next to each file.
    _CONCEPT_STOP = {
        "the", "and", "for", "with", "that", "this", "from", "into", "not", "but", "are",
        "was", "has", "had", "its", "our", "you", "your", "all", "any", "can", "use",
        "via", "per", "out", "off", "old", "new", "one", "two", "now", "see", "fix",
        "add", "set", "get", "run", "wip", "feat", "docs", "test", "chore", "refactor",
        "when", "then", "than", "what", "how", "why", "where", "who", "which", "must",
        "code", "file", "files", "line", "lines", "func", "class", "def", "return",
        "recall", "engine", "node", "nodes", "index",  # too generic in THIS repo
    }

    def _merge_knowledge_synonyms(self, syn: dict[str, list[str]]) -> None:
        """For each knowledge node wired to a file, bridge the rare CONCEPT words in its
        title+body to the code symbols in that file. Conservative on purpose: only rare
        words (IDF-worthy), only a few targets per word, only knowledge kinds."""
        try:
            rows = self.db.execute(
                "SELECT title, COALESCE(body,'') body, file_path FROM nodes "
                "WHERE file_path IS NOT NULL AND file_path != '' "
                "AND kind IN ('lesson','decision','commit') "
                "AND (length(COALESCE(body,'')) > 20 OR length(title) > 12)"
            ).fetchall()
        except sqlite3.Error:
            return
        # symbols per file (lowercased file_path → list of real symbol names)
        by_file: dict[str, list[str]] = {}
        for c in self.cands:
            fp = (c.file_path or "").replace("\\", "/").lower()
            if fp:
                by_file.setdefault(fp, []).append(c.symbol)
        if not by_file:
            return
        for r in rows:
            fp = (r["file_path"] or "").replace("\\", "/").lower()
            syms = by_file.get(fp)
            if not syms:
                continue
            # concept words from the prose: rare (idf-known or unseen), not stopwords,
            # not already a code token of the file's own symbols (those resolve lexically).
            own = {t for s in syms for t in tokens(s)}
            prose = set(tokens(f"{r['title']} {r['body']}"))
            concepts = {
                w for w in prose
                if len(w) > 3 and w not in self._CONCEPT_STOP and w not in own
                # rare in the symbol space → a real concept, not boilerplate
                and self._idf.get(w, 99.0) >= 1.0
            }
            for w in concepts:
                bucket = syn.setdefault(w, [])
                for s in syms[:3]:  # cap: a few representative symbols per file
                    if s not in bucket and len(bucket) < 12:
                        bucket.append(s)

    def warmth_of_index(self) -> float:
        """Fraction of symbols with ANY lived experience — how warm this repo is."""
        n = len(self.cands) or 1
        return sum(1 for c in self.cands if c.access_count or c.useful or c.missed) / n

    def resolve(self, guess: str, warmth: float = 1.0, top: int = 5) -> list[Candidate]:
        gtoks = set(tokens(guess))
        # synonym expansion (warm only): a learned synonym word pulls in the symbols
        # it historically led to, even with zero spelling overlap.
        syn_targets: dict[str, str] = {}
        if warmth >= 0.5:
            for w in gtoks:
                for sym in self._synonyms.get(w, []):
                    syn_targets[sym] = w
        g_idf = sum(self._idf.get(t, 1.0) for t in gtoks) or 1.0
        g_tri = _trigrams(guess)  # the guess's trigrams: built ONCE, not per symbol
        out: list[Candidate] = []
        for c, ctoks, ctri in zip(self.cands, self._cand_tokens, self._cand_trigrams):
            shared = gtoks & ctoks
            # guess-COVERAGE (not symmetric Jaccard) so a long real name isn't
            # punished for length; IDF-weighted so a rare shared token dominates.
            coverage = sum(self._idf.get(t, 1.0) for t in shared) / g_idf if gtoks else 0.0
            tri = _trigram_sim_sets(g_tri, ctri)
            vocab = 0.8 * coverage + 0.2 * tri
            via = syn_targets.get(c.symbol, "")
            if via:
                # a learned synonym is strong repo evidence — floor its vocab score
                # so it ranks even with zero spelling overlap (the flywheel payoff).
                vocab = max(vocab, 0.5)
            if vocab < _MIN_VOCAB:
                continue
            acc = c.access_count / self._max_access
            imp = c.importance / self._max_imp
            net_fb = c.useful - c.missed
            fb = 1.0 if net_fb > 0 else (-0.3 if net_fb < 0 else 0.0)
            exp = 0.45 * acc + 0.4 * imp + 0.15 * max(fb, 0.0)

            cc = Candidate(
                symbol=c.symbol, file_path=c.file_path, line=c.line, node_id=c.node_id,
                importance=c.importance, access_count=c.access_count, useful=c.useful,
                missed=c.missed,
            )
            cc.vocab_score = vocab
            cc.exp_score = exp
            cc.score = vocab  # headline rank is vocabulary, period
            cc.via_synonym = via
            cc.why = self._why(cc, warmth)
            out.append(cc)

        def _is_test(p: str) -> bool:
            p = (p or "").lower().replace("\\", "/")
            return "test" in p or "/tests/" in p

        out.sort(key=lambda c: (
            -round(c.vocab_score * (_TEST_PENALTY if _is_test(c.file_path) else 1.0), 4),
            -(c.exp_score if warmth >= 0.5 else 0.0),  # L2 tie-break, warm only
            c.node_id,                                  # determinism
        ))
        return out[:top]

    def _why(self, c: Candidate, warmth: float) -> list[str]:
        w: list[str] = []
        if c.via_synonym:
            w.append(f"learned: searches for {c.via_synonym!r} landed here")
        if warmth > 0:
            if c.access_count > 0:
                w.append(f"surfaced {c.access_count}× before")
            if c.useful > 0:
                w.append(f"marked useful {c.useful}×")
            if c.missed > 0:
                w.append(f"⚠ marked missed {c.missed}×")
            if c.importance >= self._max_imp * 0.4:
                w.append("high structural importance")
        return w

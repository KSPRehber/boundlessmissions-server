"""
data/part_resolver.py — Resolve a loosely-typed part mention to a real installed
part, using the KSP client's uploaded part catalog.

A mission author writes "the Thud engine" or even a typo'd "thudd"; the actual
part has a weird title like 'Mk-55 "Thud" Liquid Fuel Engine' and a stable
internal name 'radialLiquidEngine1-2'. This module fuzzy-matches the mention
against the catalog and returns the part's internal name when it can pin it down
1:1. Ambiguous / low-confidence mentions are handed to an optional AI resolver;
if that can't decide either, it returns None and the caller falls back to loose
substring matching.

Catalog entries are dicts: {"name": <internal>, "title": <display>}.
"""
from __future__ import annotations

import difflib
import logging
import re

log = logging.getLogger(__name__)

# Confidence thresholds (scores are 0-100; see _score).
_HIGH = 80      # a match at/above this can stand on its own ...
_MARGIN = 10    # ... if it also leads the runner-up by this much.
_LOW = 45       # below this, not even worth offering to the AI.
_FUZZY_ACCEPT = 0.86  # difflib ratio that counts as a confident typo match.

_QUOTE_RE = re.compile(r'["“‘\']([^"”’\']{1,40})["”’\']')


_LEADING_ARTICLES = re.compile(r"^(?:the|a|an)\s+")


def _norm(s: str) -> str:
    """Lower-case, drop punctuation, strip a leading article, collapse whitespace."""
    s = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())).strip()
    return _LEADING_ARTICLES.sub("", s)


def _part_keys(part: dict) -> dict:
    """Pre-compute the searchable strings for a catalog part (cached on the dict)."""
    title = part.get("title") or ""
    name = part.get("name") or ""
    tnorm = _norm(title)
    nnorm = _norm(name)
    keys = set()
    if tnorm:
        keys.add(tnorm)
    if nnorm:
        keys.add(nnorm)
    m = _QUOTE_RE.search(title)            # the "Thud" nickname inside the title
    if m:
        nick = _norm(m.group(1))
        if nick:
            keys.add(nick)
    return {"tnorm": tnorm, "nnorm": nnorm, "keys": keys, "twords": set(tnorm.split())}


def _score(loose_norm: str, k: dict) -> int:
    """Match score for a normalised mention against one part's keys."""
    if not loose_norm:
        return 0
    if loose_norm in k["keys"]:
        return 100                          # exact title / name / nickname
    if loose_norm in k["twords"]:
        return 85                           # whole word in the title
    if loose_norm in k["tnorm"] or loose_norm in k["nnorm"]:
        return 65                           # substring
    # Fuzzy (handles typos). Best ratio over keys + individual title words.
    best = 0.0
    for cand in list(k["keys"]) + list(k["twords"]):
        best = max(best, difflib.SequenceMatcher(None, loose_norm, cand).ratio())
    return int(best * 80)                    # capped below "word" so exacts win


def resolve_part(loose: str, catalog: list[dict], ai_resolver=None) -> str | None:
    """
    Resolve `loose` to a single part's internal name, or None if it can't be
    pinned down confidently (caller should then fall back to loose matching).

    `ai_resolver(loose, candidates)` is an optional callable returning a chosen
    internal name (must be one of the candidates' names) or None. It's only
    invoked when the deterministic pass is ambiguous or weak.
    """
    loose_norm = _norm(loose)
    if not loose_norm or not catalog:
        return None

    scored = []
    for part in catalog:
        k = part.get("_keys")
        if k is None:
            k = part["_keys"] = _part_keys(part)
        sc = _score(loose_norm, k)
        if sc > 0:
            scored.append((sc, part))

    if not scored:
        return _ai(loose, [], ai_resolver)

    scored.sort(key=lambda x: -x[0])
    best_sc, best_part = scored[0]
    second_sc = scored[1][0] if len(scored) > 1 else 0

    # Confident, unique winner — no AI needed.
    if best_sc >= _HIGH and (best_sc - second_sc) >= _MARGIN:
        return best_part.get("name")

    # A strong fuzzy match that's clearly ahead also stands on its own (typo case).
    if best_sc >= int(_FUZZY_ACCEPT * 80) and (best_sc - second_sc) >= _MARGIN * 2:
        return best_part.get("name")

    # Ambiguous or weak → let the AI choose among the plausible candidates.
    candidates = [p for sc, p in scored if sc >= _LOW][:12] or [p for _, p in scored[:8]]
    return _ai(loose, candidates, ai_resolver)


def _ai(loose: str, candidates: list[dict], ai_resolver) -> str | None:
    if not ai_resolver:
        return None
    try:
        chosen = ai_resolver(loose, candidates)
    except Exception as exc:
        log.warning("AI part resolution failed for %r: %s", loose, exc)
        return None
    if not chosen:
        return None
    valid = {c.get("name") for c in candidates}
    # When candidates were empty we trust the AI's name as-is (it saw the catalog
    # upstream); otherwise it must pick one we offered.
    if candidates and chosen not in valid:
        return None
    return chosen


def catalog_hash(parts: list[dict]) -> str:
    """Stable hash of a catalog, for caching resolutions and skipping re-uploads."""
    import hashlib
    joined = "\n".join(sorted(f"{p.get('name','')}|{p.get('title','')}" for p in parts))
    return hashlib.sha1(joined.encode("utf-8", "ignore")).hexdigest()

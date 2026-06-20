"""
data/mission_constraints.py — Part-restriction ("mission limit") extraction & verification.

A contract's mission text can carry restrictions on what the craft is allowed to
use, e.g. "You must use a nuclear engine", "You can't use the Thud engine",
"Lqd He3 powered engines only", or "heatshield-less re-entry". This module turns
that natural-language text into a structured `constraints` dict, and verifies a
craft's actually-used parts (reported by the KSP client) against it.

The same canonical schema is enforced in three places:
  • the KSP editor (forbidden parts are hidden — see EditorPartEnforcer.cs)
  • the KSP submit gate (client-side pre-check — see SubmitWindow.cs)
  • the bot's /submit endpoint (authoritative re-check — see api_server.py)

Canonical constraints dict (every key optional; omitted/empty == no restriction):
    {
      "forbidden_parts":              [str],  # title substrings, e.g. "Thud"
      "required_parts":               [str],
      "forbidden_propellants":        [str],  # resource names, e.g. "LqdHe3"
      "required_propellants":         [str],
      "forbidden_engine_categories":  [str],  # semantic: nuclear/ion/solid/...
      "required_engine_categories":   [str],
      "forbidden_part_categories":    [str],  # e.g. "heatshield", "parachute"
      "required_part_categories":     [str],
      "max_parts":                    int,    # part-count ceiling (optional)
      "min_parts":                    int,    # part-count floor (optional)
      "max_dv":                       float,  # vacuum Δv ceiling, m/s (optional)
      "min_dv":                       float,  # vacuum Δv floor, m/s (optional)
      "notes":                        str,    # human-readable summary (optional)
    }

Δv (delta-v) is a whole-craft metric, so unlike the part rules it can't be
enforced by hiding parts in the editor. It's checked only at submit time: the
KSP client reports the craft's stock-calculated vacuum Δv and the bot verifies
it against max_dv/min_dv (the value is client-reported, like used_parts).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# ── Vocabulary ──────────────────────────────────────────────────────────────

LIST_KEYS = (
    "forbidden_parts", "required_parts",
    "forbidden_propellants", "required_propellants",
    "forbidden_engine_categories", "required_engine_categories",
    "forbidden_part_categories", "required_part_categories",
)

# Semantic engine categories the KSP-side PartClassifier can derive. Keep these
# in sync with PartClassifier.cs::GetEngineCategories.
ENGINE_CATEGORIES = {
    "nuclear", "ion", "solid", "chemical", "electric", "monoprop", "rcs",
}

# Natural-language phrase -> canonical engine category.
_ENGINE_CATEGORY_ALIASES = {
    "nuclear": "nuclear", "ntr": "nuclear", "nerv": "nuclear", "fission": "nuclear",
    "fusion": "nuclear", "atomic": "nuclear", "nükleer": "nuclear",
    "ion": "ion", "iyon": "ion",
    "solid": "solid", "srb": "solid", "solid fuel": "solid", "solid booster": "solid",
    "katı yakıt": "solid",
    "chemical": "chemical", "kimyasal": "chemical",
    "electric": "electric", "electrical": "electric", "elektrik": "electric",
    "monoprop": "monoprop", "monopropellant": "monoprop", "mono propellant": "monoprop",
    "rcs": "rcs",
}

# Natural-language phrase -> canonical KSP resource (propellant) name. The KSP
# client matches these case-insensitively against the real resource names burnt
# by each engine, so modded resources work as long as the phrase appears here or
# the text already uses the resource's exact name.
_PROPELLANT_ALIASES = {
    "lqd he3": "LqdHe3", "lqdhe3": "LqdHe3", "liquid he3": "LqdHe3",
    "liquid helium-3": "LqdHe3", "helium-3": "LqdHe3", "helium 3": "LqdHe3", "he3": "LqdHe3",
    "lqd hydrogen": "LqdHydrogen", "liquid hydrogen": "LqdHydrogen", "lh2": "LqdHydrogen",
    "lqd deuterium": "LqdDeuterium", "deuterium": "LqdDeuterium",
    "liquid fuel": "LiquidFuel", "liquidfuel": "LiquidFuel",
    "oxidizer": "Oxidizer",
    "monopropellant": "MonoPropellant", "monoprop": "MonoPropellant",
    "xenon": "XenonGas", "xenon gas": "XenonGas",
    "solid fuel": "SolidFuel", "solidfuel": "SolidFuel",
    "methane": "LqdMethane", "lqd methane": "LqdMethane", "liquid methane": "LqdMethane",
    "argon": "ArgonGas",
}

# Part-category phrases the client can tag (PartClassifier.cs::GetPartCategories).
_PART_CATEGORY_ALIASES = {
    "heatshield": "heatshield", "heat shield": "heatshield", "heat-shield": "heatshield",
    "ablator": "heatshield", "ısı kalkanı": "heatshield",
    "parachute": "parachute", "chute": "parachute", "paraşüt": "parachute",
    "solar panel": "solarpanel", "solar": "solarpanel", "solarpanel": "solarpanel",
    "güneş paneli": "solarpanel",
    "wheel": "wheel", "landing gear": "wheel", "tekerlek": "wheel",
    "ladder": "ladder", "merdiven": "ladder",
    "reaction wheel": "reactionwheel", "rtg": "rtg",
}

# Negation cues. A clause containing any of these is a *forbidding* clause,
# even if it also reads like a requirement ("doesn't use X-powered engines").
# Negation is checked first and dominates, so it flips "powered"/"use" intent.
_NEG_CUES = (
    "n't",          # doesn't / can't / won't / shouldn't / isn't ...
    "doesnt", "dont", "cant", "wont", "shouldnt", "isnt", "arent",
    "does not", "do not", "can not", "will not",
    "without", "never", "avoid", "not allowed", "forbidden", "banned",
    "prohibited", "no use of", "free of", "-less", "lacking",
    " no ", " not ",
    "kullanma", "yasak", "olmadan", "kullanamaz", "izin yok", "olmasın",
)
# Explicit forbid phrases (negation cues above also count as forbidding).
_FORBID_CUES = (
    "can't use", "cant use", "cannot use", "can not use", "without",
    "not allowed", "forbidden", "banned", "don't use", "dont use", "avoid",
    "may not use", "must not", "prohibited", "-less",
    "kullanma", "yasak", "olmadan", "kullanamaz", "izin yok",
)
_REQUIRE_CUES = (
    "must use", "have to use", "only use", "use only", "required", "must be",
    "should use", "needs to use", "powered by", "powered", "only", "must have",
    "kullanmalı", "kullanmak zorunda", "sadece", "zorunlu", "gerek",
)
# Weaker require cues that need whole-word boundaries (so "with" doesn't match
# "within"). Matched against a space-padded clause. "a craft with a Vector
# engine" / "that has SRBs" reads as a requirement.
_REQUIRE_BOUNDARY_CUES = (
    " with ", " has ", " have ", " having ", " using ", " featuring ",
    " equipped ", " ile ", " olan ", " sahip ",
)


# ── Normalisation ────────────────────────────────────────────────────────────

def empty() -> dict:
    """A constraints dict with no restrictions."""
    return {k: [] for k in LIST_KEYS}


def is_empty(constraints: dict | None) -> bool:
    """True when there is nothing to enforce."""
    if not constraints:
        return True
    if any(constraints.get(k) for k in LIST_KEYS):
        return False
    return not (constraints.get("max_parts") or constraints.get("min_parts")
                or constraints.get("max_dv") or constraints.get("min_dv"))


def _as_str_list(val) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        val = [val]
    out = []
    for x in val:
        if x is None:
            continue
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def normalize(raw: dict | None) -> dict:
    """
    Coerce a possibly-AI-produced dict into the canonical schema: every list key
    present, deduped, with categories lower-cased and mapped through the alias
    tables so free-form AI output ("Nuclear", "He-3") lands on canonical tokens.
    """
    raw = raw or {}
    out = empty()

    for key in ("forbidden_parts", "required_parts"):
        out[key] = _dedupe(_as_str_list(raw.get(key)))

    for key in ("forbidden_propellants", "required_propellants"):
        out[key] = _dedupe(_map_tokens(raw.get(key), _PROPELLANT_ALIASES, keep_unknown=True))

    for key in ("forbidden_engine_categories", "required_engine_categories"):
        out[key] = _dedupe(_map_tokens(raw.get(key), _ENGINE_CATEGORY_ALIASES,
                                       allowed=ENGINE_CATEGORIES))

    for key in ("forbidden_part_categories", "required_part_categories"):
        out[key] = _dedupe(_map_tokens(raw.get(key), _PART_CATEGORY_ALIASES, keep_unknown=True,
                                       lower=True))

    for key in ("max_parts", "min_parts"):
        val = raw.get(key)
        if isinstance(val, bool):
            continue
        try:
            iv = int(val)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            out[key] = iv

    for key in ("max_dv", "min_dv"):
        val = raw.get(key)
        if isinstance(val, bool):
            continue
        try:
            fv = float(val)
        except (TypeError, ValueError):
            continue
        if fv > 0:
            out[key] = fv

    notes = raw.get("notes")
    if isinstance(notes, str) and notes.strip():
        out["notes"] = notes.strip()[:300]
    _resolve_conflicts(out)
    return out


# (forbidden, required) key pairs that must not share a token — a craft can't
# both must-use and must-not-use the same thing.
_CONFLICT_PAIRS = (
    ("forbidden_parts", "required_parts"),
    ("forbidden_propellants", "required_propellants"),
    ("forbidden_engine_categories", "required_engine_categories"),
    ("forbidden_part_categories", "required_part_categories"),
)


def _resolve_conflicts(constraints: dict) -> None:
    """Drop any token present in both a forbidden and required list (forbidden
    wins — an explicit ban is rarely a mistake, while a spurious requirement
    would block an otherwise-valid craft). Mutates `constraints` in place."""
    for forbid_key, require_key in _CONFLICT_PAIRS:
        banned = {v.lower() for v in constraints.get(forbid_key, [])}
        if banned:
            constraints[require_key] = [
                v for v in constraints.get(require_key, []) if v.lower() not in banned
            ]


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for it in items:
        k = it.lower()
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def _map_tokens(val, aliases: dict, *, allowed: set | None = None,
                keep_unknown: bool = False, lower: bool = False) -> list[str]:
    result = []
    for tok in _as_str_list(val):
        low = tok.lower().strip()
        mapped = aliases.get(low)
        if mapped is None:
            # Try a contains-match so "nuclear engine" -> "nuclear".
            for phrase, canon in aliases.items():
                if phrase in low:
                    mapped = canon
                    break
        if mapped is not None:
            result.append(mapped)
        elif allowed is not None and low in allowed:
            result.append(low)
        elif keep_unknown:
            result.append(low if lower else tok)
    return result


# ── Heuristic extraction (fallback when no AI / AI failure) ──────────────────

def extract_heuristic(text: str) -> dict:
    """
    Keyword-based constraint extraction. Splits the text into clauses, decides
    whether each clause forbids or requires, and scans it for known engine
    categories, propellants and part categories. Deliberately conservative —
    only emits a restriction when a clause clearly pairs a cue word with a known
    term, so ordinary mission flavour text produces no constraints.
    """
    out = empty()
    if not text:
        return out

    low = text.lower()
    # Split into clauses on sentence / list punctuation.
    import re
    clauses = re.split(r"[.;\n!?]|,(?=\s*(?:no|you|the|only|and|must|can))", low)

    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        forbid = _clause_polarity(clause)
        if forbid is None:
            continue
        eng_key = "forbidden_engine_categories" if forbid else "required_engine_categories"
        prop_key = "forbidden_propellants" if forbid else "required_propellants"
        cat_key = "forbidden_part_categories" if forbid else "required_part_categories"

        for phrase, canon in _ENGINE_CATEGORY_ALIASES.items():
            if _word_in(phrase, clause):
                out[eng_key].append(canon)
        for phrase, canon in _PROPELLANT_ALIASES.items():
            if _word_in(phrase, clause):
                out[prop_key].append(canon)
        for phrase, canon in _PART_CATEGORY_ALIASES.items():
            if _word_in(phrase, clause):
                out[cat_key].append(canon)

    # "heatshield-less" / "X-free" style: forbid even without a separate cue.
    for phrase, canon in _PART_CATEGORY_ALIASES.items():
        if f"{phrase}-less" in low or f"{phrase}less" in low or f"{phrase} free" in low \
                or f"{phrase}-free" in low or f"no {phrase}" in low:
            out["forbidden_part_categories"].append(canon)

    # Named parts, e.g. "can't use the Thud engine" / "use only the Mainsail".
    _extract_named_parts(text, out)

    # Part-count limits, e.g. "max 10 parts" / "at least 5 parts".
    _extract_part_count(text, out)

    # Delta-v limits, e.g. "at least 3000 m/s of delta-v" / "no more than 5 km/s dv".
    _extract_delta_v(text, out)

    return normalize(out)


def _extract_part_count(text: str, out: dict) -> None:
    """Detect min/max part-count limits. Handles inclusive ('at most N', 'up to N')
    and strict ('fewer than N' => N-1, 'more than N' => N+1) bounds, plus
    'N+ parts', 'N parts or more/fewer', and a few Turkish forms. The most
    restrictive bound wins when several appear."""
    import re
    low = text.lower()
    maxes: list[int] = []
    mins: list[int] = []
    P = r"(?:parts?|parça\w*)"  # English "part(s)" or Turkish "parça/parçası"

    def n(m, delta=0):
        return int(m.group(1)) + delta

    # exactly N
    for m in re.finditer(rf"(?:exactly|precisely|tam)\s*(\d+)\s*{P}", low):
        maxes.append(n(m)); mins.append(n(m))
    # range: "between N and M parts" / "N to M parts"
    for m in re.finditer(rf"(?:between\s*)?(\d+)\s*(?:and|to|-|ile)\s*(\d+)\s*{P}", low):
        mins.append(int(m.group(1))); maxes.append(int(m.group(2)))
    # inclusive max
    for m in re.finditer(rf"(?:max(?:imum)?|no more than|at most|up to|no greater than|"
                         rf"en fazla|en çok)\s*(?:of\s*)?(\d+)\s*{P}", low):
        maxes.append(n(m))
    for m in re.finditer(rf"(\d+)\s*{P}\s*or\s*(?:fewer|less)", low):
        maxes.append(n(m))
    # strict max ("fewer/less/under N" => N-1)
    for m in re.finditer(rf"(?:fewer than|less than|under|below|altında)\s*(\d+)\s*{P}", low):
        maxes.append(max(1, n(m, -1)))
    # inclusive min
    for m in re.finditer(rf"(?:min(?:imum)?|at least|no fewer than|no less than|"
                         rf"en az)\s*(?:of\s*)?(\d+)\s*{P}", low):
        mins.append(n(m))
    for m in re.finditer(rf"(\d+)\s*{P}\s*or\s*more", low):
        mins.append(n(m))
    for m in re.finditer(rf"(\d+)\+\s*{P}", low):
        mins.append(n(m))
    # strict min ("more than/over N" => N+1). The "no " lookbehind keeps
    # "no more than N" (an inclusive max) from being read as a minimum.
    for m in re.finditer(rf"(?<!no )(?:more than|over|greater than|above|üzerinde)\s*(\d+)\s*{P}", low):
        mins.append(n(m, 1))

    # Reverse phrasing where the part-count noun precedes the bound and no
    # trailing "parts" follows the number, e.g. "part count above 10",
    # "part count of at least 10", "parça sayısı 10 üzerinde".
    PC = r"(?:parts?\s*count|parça\s*say\w*)"
    CONN = r"(?:\s*(?:of|is|:|=)\s*|\s+)"  # optional "of"/"is"/":"/"=" connector
    for m in re.finditer(rf"{PC}{CONN}(?:more than|over|greater than|above|üzerinde)\s*(\d+)", low):
        mins.append(n(m, 1))
    for m in re.finditer(rf"{PC}{CONN}(?:at least|min(?:imum)?|no fewer than|no less than|en az)\s*(\d+)", low):
        mins.append(n(m))
    for m in re.finditer(rf"{PC}{CONN}(?:fewer than|less than|under|below|altında)\s*(\d+)", low):
        maxes.append(max(1, n(m, -1)))
    for m in re.finditer(rf"{PC}{CONN}(?:at most|max(?:imum)?|no more than|up to|no greater than|en fazla|en çok)\s*(\d+)", low):
        maxes.append(n(m))

    if maxes:
        out["max_parts"] = min(maxes)
    if mins:
        out["min_parts"] = max(mins)


def _extract_delta_v(text: str, out: dict) -> None:
    """Detect min/max vacuum delta-v (Δv) limits, normalised to m/s. Understands
    'at least 3000 m/s of delta-v', 'no more than 5 km/s dv', 'under 4000 m/s Δv',
    the reverse order ('delta-v of at least 3000'), and ranges ('3000-5000 m/s
    dv'). km/s values are converted to m/s. The most restrictive bound wins when
    several appear. A Δv token must be present, so plain numbers in flavour text
    don't trip it."""
    import re
    low = text.lower()
    maxes: list[float] = []
    mins: list[float] = []

    NUM = r"(\d[\d,]*(?:\.\d+)?)"
    UNIT = r"\s*(km/s|km/sec|kps|m/s|m/sec|mps)?"
    # Δ lower-cases to δ; also accept "delta-v", "deltav", standalone "dv".
    DV = r"(?:[δ]\s*-?\s*v|delta[\s\-]*v|deltav|\bdv\b)"
    MAXQ = (r"(?:at most|maximum|max\.?|no more than|up to|no greater than|"
            r"under|below|less than|fewer than|lower than|en fazla|en çok)")
    MINQ = r"(?:at least|minimum|min\.?|no less than|no fewer than|en az)"
    OF = r"\s*(?:of\s*)?"

    def val(m, gi=1):
        x = float(m.group(gi).replace(",", ""))
        unit = m.group(gi + 1) or ""
        return x * 1000.0 if ("km" in unit or "kps" in unit) else x

    # Inclusive max — both word orders.
    for m in re.finditer(rf"{MAXQ}\s*{NUM}{UNIT}{OF}{DV}", low):
        maxes.append(val(m))
    for m in re.finditer(rf"{DV}{OF}{MAXQ}\s*{NUM}{UNIT}", low):
        maxes.append(val(m))
    # Inclusive min — both word orders.
    for m in re.finditer(rf"{MINQ}\s*{NUM}{UNIT}{OF}{DV}", low):
        mins.append(val(m))
    for m in re.finditer(rf"{DV}{OF}{MINQ}\s*{NUM}{UNIT}", low):
        mins.append(val(m))
    # Strict min ("more than/over N"). Δv is continuous, so N (not N+1) is used.
    # The "no " lookbehind keeps "no more than" (an inclusive max) out of here.
    SMIN = r"(?<!no )(?:more than|greater than|over|above)"
    for m in re.finditer(rf"{SMIN}\s*{NUM}{UNIT}{OF}{DV}", low):
        mins.append(val(m))
    for m in re.finditer(rf"{DV}{OF}{SMIN}\s*{NUM}{UNIT}", low):
        mins.append(val(m))
    # Range: "between 3000 and 5000 m/s delta-v" / "3000 to 5000 m/s dv".
    for m in re.finditer(rf"(?:between\s*)?{NUM}{UNIT}\s*(?:and|to|-|–|ile)\s*{NUM}{UNIT}{OF}{DV}", low):
        mins.append(val(m, 1))
        maxes.append(val(m, 3))

    if maxes:
        out["max_dv"] = min(maxes)
    if mins:
        out["min_dv"] = max(mins)


# Tokens that look like a part name but are really a category/generic word —
# captured by the "<name> engine" pattern but should not become a part name.
_GENERIC_PART_WORDS = {
    "the", "a", "an", "any", "this", "that", "your", "single", "one", "main",
    "nuclear", "atomic", "ion", "solid", "chemical", "electric", "liquid",
    "rocket", "jet", "ion", "rcs", "the", "new", "only", "use", "powered",
    "fusion", "fission", "lqd", "no", "only", "must", "type", "kind",
}


def _extract_named_parts(text: str, out: dict) -> None:
    """
    Capture proper-noun part names sitting just before engine/motor/booster/
    thruster, and route them to forbidden_parts/required_parts by the polarity of
    the surrounding text. Quoted names ("Thud") are always captured.
    """
    import re
    low = text.lower()

    # Quoted names: "Thud", 'Mainsail'.
    quoted = re.findall(r'["“‘\']([A-Za-z][A-Za-z0-9 .\-]{1,30})["”’\']', text)

    # The word right before an engine noun (any case, so lowercase "thud engine"
    # is caught), optionally preceded by a Capitalised brand token ("LV-N Nerv").
    # Generic/fuel/category head words are filtered out below.
    pattern = re.compile(
        r'(?:([A-Z][A-Za-z0-9\-]+)\s+)?'                    # optional brand prefix
        r'([A-Za-z][A-Za-z0-9\-]+)\s+'                      # head word before the noun
        r'(?:[Ee]ngine|[Mm]otor|[Bb]ooster|[Tt]hruster|[Rr]ocket)s?\b'
    )
    candidates = []
    for m in pattern.finditer(text):
        prefix, head = m.group(1), m.group(2)
        # Only keep the brand prefix when the head itself isn't a generic/fuel
        # word it would otherwise be glued to (e.g. keep "LV-N" + "Nerv").
        name = (prefix + " " + head) if prefix and not _is_generic_part_word(head) else head
        candidates.append((name, m.start(2)))
    for q in quoted:
        idx = text.find(q)
        candidates.append((q, idx if idx >= 0 else 0))

    for name, pos in candidates:
        clean = name.strip()
        if not clean or clean.lower() in _GENERIC_PART_WORDS:
            continue
        words = clean.split()
        # Skip if every word is a generic/fuel/category word, OR any word is a
        # known fuel/engine-category term (e.g. "He3 Powered" describes a fuel,
        # "Ion engine" a category — not part names).
        if all(_is_generic_part_word(w) for w in words) \
                or any(_is_fuel_or_category_word(w) for w in words):
            continue
        # Polarity from a window of text just before the mention.
        window = low[max(0, pos - 45):pos + len(clean)]
        forbid = _clause_polarity(window)
        if forbid is None:
            continue
        key = "forbidden_parts" if forbid else "required_parts"
        out[key].append(clean)


def _is_generic_part_word(word: str) -> bool:
    return word.lower() in _GENERIC_PART_WORDS


def _is_fuel_or_category_word(word: str) -> bool:
    """True when a token is a known fuel/engine/part-category term, so it should
    not be mistaken for a part *name*."""
    w = word.lower()
    return (w in _PROPELLANT_ALIASES or w in _ENGINE_CATEGORY_ALIASES
            or w in _PART_CATEGORY_ALIASES)


def _clause_polarity(clause: str) -> bool | None:
    """
    True=forbidding clause, False=requiring clause, None=neither cue present.

    Negation is checked first and dominates: "doesn't use deuterium-powered
    engines" forbids deuterium even though it also contains the require-cue
    "powered". Clauses mixing both (e.g. "use only X, no Y") are split upstream.
    """
    padded = f" {clause} "  # so boundary cues (" no ", " not ") match at edges
    if any(cue in padded for cue in _NEG_CUES) or any(cue in clause for cue in _FORBID_CUES):
        return True
    if any(cue in clause for cue in _REQUIRE_CUES) \
            or any(cue in padded for cue in _REQUIRE_BOUNDARY_CUES):
        return False
    return None


def _word_in(phrase: str, clause: str) -> bool:
    """Whole-token containment so 'ion' doesn't match 'station'."""
    import re
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", clause) is not None


# ── Part-name resolution against the real catalog ────────────────────────────

def resolve_parts(constraints: dict | None, resolver) -> dict:
    """
    Resolve the loose `forbidden_parts` / `required_parts` mentions to concrete
    installed parts using `resolver(loose_name) -> internal_name | None`.

    Returns a copy of `constraints` with, for each kind, two derived lists added:
        <kind>_part_names         resolved internal names (match a part exactly)
        <kind>_parts_unresolved   mentions that couldn't be pinned (loose fallback)

    Mentions are deduped by their resolution so the same part isn't listed twice.
    """
    if not constraints:
        return constraints or {}
    out = dict(constraints)
    for kind in ("forbidden", "required"):
        names, unresolved = [], []
        for loose in constraints.get(f"{kind}_parts", []) or []:
            name = None
            try:
                name = resolver(loose)
            except Exception:
                name = None
            if name and name not in names:
                names.append(name)
            elif not name:
                unresolved.append(loose)
        out[f"{kind}_part_names"] = names
        out[f"{kind}_parts_unresolved"] = unresolved
    return out


def _part_match_sets(constraints: dict, kind: str) -> tuple[list[str], list[str]]:
    """(resolved internal names, loose names to substring-match) for a
    forbidden/required kind. Falls back to the loose list when resolution hasn't
    been applied to these constraints."""
    if f"{kind}_part_names" in constraints or f"{kind}_parts_unresolved" in constraints:
        names = constraints.get(f"{kind}_part_names", [])
        loose = constraints.get(f"{kind}_parts_unresolved", [])
    else:
        names = []
        loose = constraints.get(f"{kind}_parts", [])
    return names, loose


# ── Verification (server-side authoritative check) ───────────────────────────

def verify_used_parts(constraints: dict | None, used_parts: list[dict],
                      delta_v: float | None = None) -> list[str]:
    """
    Compare the craft's actually-used parts against the constraints and return a
    list of human-readable violation messages (empty == passes).

    `used_parts` is the per-part summary reported by the KSP client; each item:
        {
          "name":               "radialLiquidEngine1-2",  # internal part name
          "title":              "Mk-55 \"Thud\" Liquid Fuel Engine",
          "propellants":        ["LiquidFuel", "Oxidizer"],
          "engine_categories":  ["chemical"],
          "part_categories":    ["engine"],
        }

    `delta_v` is the craft's stock-calculated vacuum Δv (m/s) as reported by the
    client; the bot can't recompute it, so a min/max-Δv limit is only checked
    when the client supplies a value (None => skip, don't wrongly fail).
    """
    if is_empty(constraints):
        return []
    used_parts = used_parts or []

    # Whole-craft scalar limits (part count + Δv).
    scalar_violations = _check_part_count(constraints, len(used_parts))
    scalar_violations += _check_delta_v(constraints, delta_v)

    if not used_parts:
        return scalar_violations + _missing_required(constraints, [])

    props = _flatten(used_parts, "propellants")
    eng = _flatten(used_parts, "engine_categories")
    cats = _flatten(used_parts, "part_categories")

    violations: list[str] = list(scalar_violations)

    # ── Forbidden parts: match resolved internal names exactly, and any
    #    unresolved mentions by case-insensitive title substring (loose fallback).
    bad_names_list, bad_loose = _part_match_sets(constraints, "forbidden")
    bad_names = {n.lower() for n in bad_names_list}
    for p in used_parts:
        if bad_names and (p.get("name") or "").lower() in bad_names:
            violations.append(f"Craft uses a forbidden part: '{p.get('title') or p.get('name')}'.")
    for bad in bad_loose:
        hit = next((p.get("title") for p in used_parts
                    if bad.lower() in (p.get("title") or "").lower()), None)
        if hit:
            violations.append(f"Craft uses a forbidden part: '{hit}' (matched '{bad}').")

    for bad in constraints.get("forbidden_propellants", []):
        if bad.lower() in props:
            violations.append(f"Craft has an engine powered by forbidden fuel: {bad}.")

    for bad in constraints.get("forbidden_engine_categories", []):
        if bad.lower() in eng:
            violations.append(f"Craft uses a forbidden engine type: {bad}.")

    for bad in constraints.get("forbidden_part_categories", []):
        if bad.lower() in cats:
            violations.append(f"Craft includes a forbidden part category: {bad}.")

    violations.extend(_missing_required(constraints, used_parts))
    return violations


def _missing_required(constraints: dict, used_parts: list[dict]) -> list[str]:
    titles = [(p.get("title") or "").lower() for p in used_parts]
    used_names = {(p.get("name") or "").lower() for p in used_parts}
    props = _flatten(used_parts, "propellants")
    eng = _flatten(used_parts, "engine_categories")
    cats = _flatten(used_parts, "part_categories")
    out: list[str] = []

    need_names, need_loose = _part_match_sets(constraints, "required")
    for need in need_names:
        if need.lower() not in used_names:
            out.append(f"Required part not found: '{need}'.")
    for need in need_loose:
        if not any(need.lower() in t for t in titles):
            out.append(f"Required part not found: '{need}'.")
    for need in constraints.get("required_propellants", []):
        if need.lower() not in props:
            out.append(f"Required: an engine powered by {need}.")
    for need in constraints.get("required_engine_categories", []):
        if need.lower() not in eng:
            out.append(f"Required engine type not found: {need}.")
    for need in constraints.get("required_part_categories", []):
        if need.lower() not in cats:
            out.append(f"Required part category missing: {need}.")
    return out


def _check_part_count(constraints: dict, count: int) -> list[str]:
    out: list[str] = []
    mx = constraints.get("max_parts")
    mn = constraints.get("min_parts")
    if mx and count > mx:
        out.append(f"Too many parts: {count} (max {mx}).")
    if mn and count < mn:
        out.append(f"Too few parts: {count} (min {mn}).")
    return out


# A small slack so a craft that rounds to the limit isn't unfairly rejected
# (the client's Δv and the constraint can differ by sub-percent rounding).
_DV_TOLERANCE = 0.005


def _check_delta_v(constraints: dict, delta_v: float | None) -> list[str]:
    out: list[str] = []
    if delta_v is None:
        return out  # client didn't report Δv — can't check, so don't fail
    mx = constraints.get("max_dv")
    mn = constraints.get("min_dv")
    if mx and delta_v > mx * (1 + _DV_TOLERANCE):
        out.append(f"Too much delta-v: {delta_v:.0f} m/s (max {mx:.0f}).")
    if mn and delta_v < mn * (1 - _DV_TOLERANCE):
        out.append(f"Not enough delta-v: {delta_v:.0f} m/s (min {mn:.0f}).")
    return out


def _flatten(used_parts: list[dict], key: str) -> set[str]:
    out = set()
    for p in used_parts:
        for v in (p.get(key) or []):
            if v:
                out.add(str(v).lower())
    return out


def summary_line(constraints: dict | None) -> str | None:
    """Short one-line description for logs / notifications, or None if empty."""
    if is_empty(constraints):
        return None
    if constraints.get("notes"):
        return constraints["notes"]
    bits = []
    for key in LIST_KEYS:
        vals = constraints.get(key)
        if vals:
            bits.append(f"{key.replace('_', ' ')}: {', '.join(vals)}")
    if constraints.get("max_parts"):
        bits.append(f"max {constraints['max_parts']} parts")
    if constraints.get("min_parts"):
        bits.append(f"min {constraints['min_parts']} parts")
    if constraints.get("max_dv"):
        bits.append(f"max {constraints['max_dv']:.0f} m/s Δv")
    if constraints.get("min_dv"):
        bits.append(f"min {constraints['min_dv']:.0f} m/s Δv")
    return "; ".join(bits) or None

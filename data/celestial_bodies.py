"""
data/celestial_bodies.py — Visual + physical reference data for celestial bodies.

Used by ``orbit_render.py`` to draw stylized 2D bodies for contract-submission
orbit diagrams. Covers the stock Kerbol system plus the most common planet-pack
mods (Outer Planets Mod, Kcalbeloh System, Real Solar System / Real Exoplanets).

Each entry describes how to *draw* the body and a fallback ``radius`` in metres
(used only when the KSP client doesn't report a live ``body_radius`` — e.g. an
old DLL, or a rescaled install). When a body isn't in this table the renderer
falls back to a plain grey sphere with a "?" — see ``UNKNOWN_BODY``.

Keys are matched case-insensitively (see ``get_body``). ``ALIASES`` maps a few
alternate spellings to canonical names.
"""
from __future__ import annotations

from typing import Any

# Visual flags understood by the renderer:
#   color   : (r, g, b) base surface colour
#   accent  : (r, g, b) secondary tone for highlight / band tint
#   bands   : draw faint horizontal cloud bands (gas/ice giants)
#   rings   : draw a planetary ring system
#   star    : draw as a glowing star (no shadow terminator)
#   black_hole : draw as a dark core with a bright accretion glow
#   radius  : fallback equatorial radius in metres

BODIES: dict[str, dict[str, Any]] = {
    # ── Stock Kerbol System ──────────────────────────────────────────────
    "Kerbol":  {"color": (255, 214, 90),  "accent": (255, 248, 200), "star": True,  "radius": 261_600_000},
    "Moho":    {"color": (140, 96, 70),   "accent": (190, 150, 120),                "radius": 250_000},
    "Eve":     {"color": (120, 70, 170),  "accent": (170, 120, 210), "bands": True, "radius": 700_000},
    "Gilly":   {"color": (130, 118, 100), "accent": (170, 158, 140),                "radius": 13_000},
    "Kerbin":  {"color": (60, 120, 190),  "accent": (90, 175, 110),  "bands": True, "radius": 600_000},
    "Mun":     {"color": (120, 120, 125), "accent": (165, 165, 170),                "radius": 200_000},
    "Minmus":  {"color": (150, 205, 190), "accent": (200, 235, 225),                "radius": 60_000},
    "Duna":    {"color": (175, 80, 55),   "accent": (215, 130, 95),  "bands": True, "radius": 320_000},
    "Ike":     {"color": (110, 110, 115), "accent": (155, 155, 160),                "radius": 130_000},
    "Dres":    {"color": (135, 125, 110), "accent": (175, 165, 150),                "radius": 138_000},
    "Jool":    {"color": (95, 170, 70),   "accent": (150, 205, 120), "bands": True, "radius": 6_000_000},
    "Laythe":  {"color": (55, 95, 160),   "accent": (90, 150, 200),  "bands": True, "radius": 500_000},
    "Vall":    {"color": (150, 185, 205), "accent": (205, 225, 240),                "radius": 300_000},
    "Tylo":    {"color": (200, 185, 150), "accent": (230, 220, 190),                "radius": 600_000},
    "Bop":     {"color": (110, 90, 70),   "accent": (150, 125, 100),                "radius": 65_000},
    "Pol":     {"color": (210, 195, 110), "accent": (235, 225, 160),                "radius": 44_000},
    "Eeloo":   {"color": (215, 220, 225), "accent": (245, 248, 250),                "radius": 210_000},

    # ── Outer Planets Mod (OPM) ──────────────────────────────────────────
    "Sarnus":  {"color": (220, 200, 140), "accent": (240, 228, 185), "bands": True, "rings": True, "radius": 5_300_000},
    "Hale":    {"color": (140, 135, 125), "accent": (180, 175, 165),                "radius": 150_000},
    "Ovok":    {"color": (160, 155, 150), "accent": (195, 190, 185),                "radius": 26_000},
    "Slate":   {"color": (90, 95, 105),   "accent": (130, 135, 145),                "radius": 540_000},
    "Tekto":   {"color": (200, 140, 70),  "accent": (230, 180, 120), "bands": True, "radius": 280_000},
    "Urlum":   {"color": (120, 200, 205), "accent": (175, 225, 228), "bands": True, "rings": True, "radius": 2_177_000},
    "Polta":   {"color": (185, 195, 185), "accent": (220, 228, 220),                "radius": 220_000},
    "Priax":   {"color": (150, 145, 140), "accent": (190, 185, 180),                "radius": 74_000},
    "Wal":     {"color": (130, 120, 110), "accent": (170, 160, 150),                "radius": 370_000},
    "Tal":     {"color": (145, 140, 135), "accent": (185, 180, 175),                "radius": 22_000},
    "Neidon":  {"color": (55, 90, 185),   "accent": (110, 150, 220), "bands": True, "radius": 1_900_000},
    "Thatmo":  {"color": (190, 175, 165), "accent": (220, 210, 200),                "radius": 286_000},
    "Nissee":  {"color": (160, 150, 145), "accent": (200, 190, 185),                "radius": 30_000},
    "Plock":   {"color": (150, 130, 115), "accent": (190, 170, 155),                "radius": 189_000},
    "Karen":   {"color": (170, 165, 160), "accent": (205, 200, 195),                "radius": 85_000},

    # ── Kcalbeloh System ─────────────────────────────────────────────────
    "Kcalbeloh": {"color": (20, 15, 30), "accent": (255, 160, 60), "black_hole": True, "radius": 50_000},
    "Suluco":  {"color": (200, 120, 90),  "accent": (230, 165, 130), "bands": True, "radius": 480_000},
    "Yeldo":   {"color": (90, 130, 175),  "accent": (140, 180, 215),                "radius": 320_000},
    "Noyreg":  {"color": (160, 90, 120),  "accent": (200, 140, 165), "bands": True, "radius": 1_500_000},
    "Efil":    {"color": (70, 150, 110),  "accent": (120, 195, 155),                "radius": 410_000},
    "Otsol":   {"color": (175, 180, 195), "accent": (215, 220, 230),                "radius": 250_000},
    "Ambrosh": {"color": (210, 120, 60),  "accent": (235, 165, 110), "bands": True, "radius": 2_000_000},
    "Doru":    {"color": (130, 110, 140), "accent": (170, 150, 180),                "radius": 300_000},
    "Krul":    {"color": (95, 100, 110),  "accent": (140, 145, 155),                "radius": 220_000},
    "Iehus":   {"color": (180, 160, 130), "accent": (215, 200, 175),                "radius": 280_000},
    "Cet":     {"color": (120, 150, 165), "accent": (165, 195, 205),                "radius": 350_000},
    "Lond":    {"color": (150, 130, 160), "accent": (190, 175, 200),                "radius": 400_000},

    # ── Real Solar System (RSS) / Real Exoplanets ────────────────────────
    "Sun":     {"color": (255, 210, 80),  "accent": (255, 245, 190), "star": True,  "radius": 696_340_000},
    "Mercury": {"color": (140, 130, 120), "accent": (180, 170, 160),                "radius": 2_439_700},
    "Venus":   {"color": (220, 195, 140), "accent": (245, 228, 185), "bands": True, "radius": 6_051_800},
    "Earth":   {"color": (60, 110, 185),  "accent": (90, 165, 110),  "bands": True, "radius": 6_371_000},
    "Moon":    {"color": (125, 125, 130), "accent": (170, 170, 175),                "radius": 1_737_400},
    "Mars":    {"color": (185, 85, 55),   "accent": (220, 130, 95),  "bands": True, "radius": 3_389_500},
    "Phobos":  {"color": (95, 85, 78),    "accent": (135, 125, 115),                "radius": 11_267},
    "Deimos":  {"color": (105, 95, 85),   "accent": (145, 135, 125),                "radius": 6_200},
    "Ceres":   {"color": (140, 135, 130), "accent": (180, 175, 170),                "radius": 469_700},
    "Jupiter": {"color": (205, 165, 120), "accent": (235, 205, 165), "bands": True, "rings": True, "radius": 69_911_000},
    "Io":      {"color": (225, 205, 110), "accent": (245, 230, 160),                "radius": 1_821_600},
    "Europa":  {"color": (200, 180, 155), "accent": (230, 215, 195),                "radius": 1_560_800},
    "Ganymede":{"color": (150, 135, 120), "accent": (190, 175, 160),                "radius": 2_634_100},
    "Callisto":{"color": (95, 90, 95),    "accent": (135, 130, 135),                "radius": 2_410_300},
    "Saturn":  {"color": (225, 205, 155), "accent": (245, 230, 195), "bands": True, "rings": True, "radius": 58_232_000},
    "Titan":   {"color": (205, 150, 70),  "accent": (232, 188, 120), "bands": True, "radius": 2_574_700},
    "Enceladus":{"color": (225, 230, 235),"accent": (248, 250, 252),                "radius": 252_100},
    "Rhea":    {"color": (175, 175, 180), "accent": (212, 212, 216),                "radius": 763_800},
    "Dione":   {"color": (180, 180, 185), "accent": (216, 216, 220),                "radius": 561_400},
    "Tethys":  {"color": (190, 192, 196), "accent": (222, 224, 228),                "radius": 531_100},
    "Mimas":   {"color": (165, 165, 170), "accent": (205, 205, 210),                "radius": 198_200},
    "Uranus":  {"color": (130, 205, 210), "accent": (180, 228, 230), "bands": True, "rings": True, "radius": 25_362_000},
    "Miranda": {"color": (160, 158, 162), "accent": (198, 196, 200),                "radius": 235_800},
    "Ariel":   {"color": (175, 172, 170), "accent": (210, 208, 206),                "radius": 578_900},
    "Umbriel": {"color": (110, 108, 110), "accent": (150, 148, 150),                "radius": 584_700},
    "Titania": {"color": (165, 150, 140), "accent": (205, 192, 182),                "radius": 788_400},
    "Oberon":  {"color": (150, 135, 128), "accent": (190, 178, 170),                "radius": 761_400},
    "Neptune": {"color": (55, 95, 200),   "accent": (110, 155, 228), "bands": True, "rings": True, "radius": 24_622_000},
    "Triton":  {"color": (210, 185, 180), "accent": (235, 215, 212),                "radius": 1_353_400},
    "Pluto":   {"color": (175, 150, 125), "accent": (210, 190, 168),                "radius": 1_188_300},
    "Charon":  {"color": (140, 135, 135), "accent": (180, 175, 175),                "radius": 606_000},
    "Eris":    {"color": (215, 215, 220), "accent": (245, 245, 248),                "radius": 1_163_000},
}

# Alternate spellings → canonical key.
ALIASES: dict[str, str] = {
    "kerbol star": "Kerbol",
    "sol": "Sun",
    "the mun": "Mun",
    "luna": "Moon",
}

UNKNOWN_BODY: dict[str, Any] = {
    "color": (90, 92, 98),
    "accent": (135, 138, 145),
    "unknown": True,
    "radius": 300_000,
}


def get_body(name: str | None) -> dict[str, Any]:
    """Return the visual/physical record for a body, case-insensitively.

    Unknown or missing names yield ``UNKNOWN_BODY`` (a grey "?" sphere).
    """
    if not name:
        return dict(UNKNOWN_BODY)
    key = name.strip()
    if key in BODIES:
        return dict(BODIES[key])
    low = key.lower()
    if low in ALIASES:
        return dict(BODIES[ALIASES[low]])
    for canonical, rec in BODIES.items():
        if canonical.lower() == low:
            return dict(rec)
    return dict(UNKNOWN_BODY)


def is_known(name: str | None) -> bool:
    """True if ``name`` resolves to a catalogued (non-fallback) body."""
    if not name:
        return False
    rec = get_body(name)
    return not rec.get("unknown", False)

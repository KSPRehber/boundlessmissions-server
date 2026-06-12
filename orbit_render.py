"""
orbit_render.py — Stylized 2D orbit diagram for contract submissions.

Given the vessel telemetry captured by the KSP client at submission time
(``vessel_data`` — see ``VesselDataCollector.cs``), produce a PNG showing:

  * the celestial body (simple vector-style 2D sphere, name underneath),
  * the vessel's orbit as an ellipse with the body at one focus,
  * apoapsis / periapsis markers with altitudes,
  * an info panel (inclination, eccentricity, period, situation, …).

Bodies are looked up in ``data.celestial_bodies``; unknown bodies fall back to
a grey sphere with a "?". When the vessel is landed/splashed (no orbit) the
diagram shows a surface marker instead of an orbit.

The single public entry point is :func:`render_orbit`. It is intentionally
defensive: any failure (missing data, Pillow not installed, bad numbers)
returns ``None`` so a submission never breaks because of a diagram.
"""
from __future__ import annotations

import io
import logging
import math
import random
from typing import Any

from data.celestial_bodies import get_body

log = logging.getLogger(__name__)

# Canvas geometry
W, H = 880, 520
ORBIT_CX, ORBIT_CY = 300, 268           # centre of the orbit-drawing region
ORBIT_MAX_HALF_W = 210                   # max half-width of the orbit ellipse (px)
ORBIT_MAX_HALF_H = 196                   # max half-height (px)
PANEL_X = 600                            # left edge of the info panel
BODY_MIN_PX, BODY_MAX_PX = 16, 120       # clamp body radius for legibility

SURFACE_SITUATIONS = {"LANDED", "SPLASHED", "PRELAUNCH"}

_BG_TOP = (10, 12, 22)
_BG_BOTTOM = (24, 20, 40)
_ORBIT_COLOR = (120, 200, 255)
_TEXT = (230, 234, 240)
_TEXT_DIM = (150, 156, 168)
_ACCENT = (120, 200, 255)


# ─────────────────────────────────────────────────────────────────────────────
# Font loading (DejaVu ships with Pillow; fall back to the bitmap default)
# ─────────────────────────────────────────────────────────────────────────────
def _load_fonts():
    from PIL import ImageFont
    fonts: dict[str, Any] = {}
    sizes = {"title": 26, "label": 17, "body": 15, "small": 13, "huge": 64}
    try:
        from PIL import ImageFont as _IF
        for key, sz in sizes.items():
            try:
                fonts[key] = _IF.truetype("DejaVuSans.ttf", sz)
            except Exception:
                fonts[key] = _IF.load_default()
        # Bold variant for the title if available
        try:
            fonts["title"] = _IF.truetype("DejaVuSans-Bold.ttf", sizes["title"])
            fonts["label"] = _IF.truetype("DejaVuSans-Bold.ttf", sizes["label"])
        except Exception:
            pass
    except Exception:
        d = ImageFont.load_default()
        fonts = {k: d for k in sizes}
    return fonts


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _num(v: Any) -> float | None:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _fmt_alt(metres: float | None) -> str:
    """Human-readable altitude: m / km / Mm."""
    if metres is None:
        return "—"
    a = abs(metres)
    if a >= 1_000_000_000:
        return f"{metres / 1_000_000_000:.2f} Gm"
    if a >= 1_000_000:
        return f"{metres / 1_000_000:.1f} Mm"
    if a >= 1_000:
        return f"{metres / 1_000:.1f} km"
    return f"{metres:.0f} m"


def _fmt_period(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "—"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _vertical_gradient(size, top, bottom):
    from PIL import Image
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        c = _lerp(top, bottom, y / max(h - 1, 1))
        for x in range(w):
            px[x, y] = c
    return img


def _draw_starfield(img, seed: str):
    rnd = random.Random(seed)
    px = img.load()
    for _ in range(140):
        x = rnd.randint(0, W - 1)
        y = rnd.randint(0, H - 1)
        b = rnd.randint(60, 200)
        px[x, y] = (b, b, min(255, b + 20))


# ─────────────────────────────────────────────────────────────────────────────
# Body sphere
# ─────────────────────────────────────────────────────────────────────────────
def _make_body_sphere(rec: dict, r_px: int):
    """Return an RGBA image (2r × 2r) of a stylized 2D body."""
    from PIL import Image, ImageDraw, ImageFilter

    s = r_px * 2
    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    base = rec["color"]
    accent = rec.get("accent", _lerp(base, (255, 255, 255), 0.4))

    if rec.get("black_hole"):
        # Accretion glow ring + dark core.
        glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse([1, 1, s - 2, s - 2], fill=(*accent, 255))
        glow = glow.filter(ImageFilter.GaussianBlur(r_px * 0.18))
        layer.alpha_composite(glow)
        d.ellipse([r_px * 0.28, r_px * 0.28, s - r_px * 0.28, s - r_px * 0.28],
                  fill=(8, 6, 12, 255))
        return layer

    # Base disc
    d.ellipse([0, 0, s - 1, s - 1], fill=(*base, 255))

    # Faint cloud bands for gas/ice giants
    if rec.get("bands"):
        band = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        bd = ImageDraw.Draw(band)
        n = 6
        for i in range(n):
            y0 = int(s * (i + 0.5) / n - s * 0.04)
            y1 = y0 + int(s * 0.05)
            tint = accent if i % 2 == 0 else _lerp(base, (0, 0, 0), 0.18)
            bd.rectangle([0, y0, s, y1], fill=(*tint, 70))
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, s - 1, s - 1], fill=255)
        layer.paste(band, (0, 0), Image.composite(band.split()[3], Image.new("L", (s, s), 0), mask))

    if not rec.get("star"):
        # Soft highlight (upper-left) and terminator shadow (lower-right).
        hi = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        hd = ImageDraw.Draw(hi)
        hl = _lerp(accent, (255, 255, 255), 0.35)
        hd.ellipse([s * 0.10, s * 0.08, s * 0.62, s * 0.60], fill=(*hl, 130))
        hi = hi.filter(ImageFilter.GaussianBlur(r_px * 0.28))

        sh = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        sd.ellipse([s * 0.42, s * 0.44, s * 1.05, s * 1.08], fill=(0, 0, 0, 150))
        sh = sh.filter(ImageFilter.GaussianBlur(r_px * 0.30))

        layer.alpha_composite(hi)
        layer.alpha_composite(sh)
    else:
        # Star: bright centre bloom.
        bloom = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(bloom).ellipse([s * 0.18, s * 0.18, s * 0.82, s * 0.82],
                                      fill=(*_lerp(base, (255, 255, 255), 0.6), 200))
        bloom = bloom.filter(ImageFilter.GaussianBlur(r_px * 0.22))
        layer.alpha_composite(bloom)

    # Rim light
    d2 = ImageDraw.Draw(layer)
    d2.ellipse([0, 0, s - 1, s - 1], outline=(*_lerp(base, (255, 255, 255), 0.5), 90), width=max(1, r_px // 30))

    # Clip everything to the circle.
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, s - 1, s - 1], fill=255)
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(layer, (0, 0), mask)

    # "?" overlay for unknown bodies.
    if rec.get("unknown"):
        od = ImageDraw.Draw(out)
        try:
            from PIL import ImageFont
            f = ImageFont.truetype("DejaVuSans-Bold.ttf", int(s * 0.5))
        except Exception:
            from PIL import ImageFont
            f = ImageFont.load_default()
        bbox = od.textbbox((0, 0), "?", font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        od.text(((s - tw) / 2 - bbox[0], (s - th) / 2 - bbox[1]), "?",
                font=f, fill=(235, 238, 245))
    return out


def _draw_rings(img, cx, cy, body_r):
    """Draw a simple ring system behind+front of a ringed body."""
    from PIL import Image, ImageDraw
    rw = int(body_r * 2.3)        # ring outer half-width
    rh = int(body_r * 0.55)       # ring half-height (tilt)
    ring = Image.new("RGBA", (rw * 2 + 4, rh * 2 + 4), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    cxr, cyr = rw + 2, rh + 2
    for i, (frac, alpha) in enumerate([(1.0, 90), (0.82, 130), (0.66, 70)]):
        ow, oh = int(rw * frac), int(rh * frac)
        rd.ellipse([cxr - ow, cyr - oh, cxr + ow, cyr + oh],
                   outline=(210, 205, 180, alpha), width=max(2, body_r // 14))
    img.alpha_composite(ring, (cx - cxr, cy - cyr))


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def render_orbit(vessel_data: dict | None) -> bytes | None:
    """Render an orbit diagram PNG from vessel telemetry.

    ``vessel_data`` keys used (all optional, best-effort):
        body, situation, vessel_name, apoapsis, periapsis, sma,
        eccentricity, inclination, period, body_radius, altitude

    Returns PNG bytes, or ``None`` if nothing useful can be drawn.
    """
    if not isinstance(vessel_data, dict):
        return None
    # The KSP client wraps the snapshot: {contract_id, active_vessel:{...},
    # nearby_vessels:[...]}. Unwrap to the flat per-vessel snapshot.
    snap = vessel_data
    if isinstance(vessel_data.get("active_vessel"), dict):
        snap = vessel_data["active_vessel"]
    try:
        return _render(snap)
    except Exception as exc:  # never let a diagram break a submission
        log.warning("orbit_render failed: %s", exc)
        return None


def _render(vd: dict) -> bytes | None:
    from PIL import Image, ImageDraw

    body_name = vd.get("body") or "Unknown"
    rec = get_body(body_name)
    situation = str(vd.get("situation") or "").upper()
    vessel_name = vd.get("vessel_name") or vd.get("vesselName") or "Vessel"

    fonts = _load_fonts()

    # ── Resolve orbit geometry (metres, from body centre) ────────────────
    body_radius = _num(vd.get("body_radius") or vd.get("bodyRadius")) or _num(rec.get("radius"))
    apo_alt = _num(vd.get("apoapsis"))
    peri_alt = _num(vd.get("periapsis"))
    sma = _num(vd.get("sma"))
    ecc = _num(vd.get("eccentricity")) or 0.0
    incl = _num(vd.get("inclination"))
    period = _num(vd.get("period"))

    r_ap = r_pe = None
    if apo_alt is not None and peri_alt is not None and body_radius:
        r_ap = body_radius + apo_alt
        r_pe = body_radius + peri_alt
    elif sma and sma > 0:
        r_ap = sma * (1 + ecc)
        r_pe = sma * (1 - ecc)
        if body_radius:
            apo_alt = r_ap - body_radius
            peri_alt = r_pe - body_radius

    has_orbit = (
        situation not in SURFACE_SITUATIONS
        and r_ap is not None and r_pe is not None
        and r_ap > 0 and r_pe > 0
    )

    # ── Canvas ───────────────────────────────────────────────────────────
    img = _vertical_gradient((W, H), _BG_TOP, _BG_BOTTOM).convert("RGBA")
    _draw_starfield(img, seed=f"{body_name}:{vessel_name}")
    draw = ImageDraw.Draw(img)

    # ── Body size + placement ────────────────────────────────────────────
    if has_orbit:
        a_m = (r_ap + r_pe) / 2.0
        c_m = (r_ap - r_pe) / 2.0
        b_m = math.sqrt(max(a_m * a_m - c_m * c_m, 0.0))
        scale = min(ORBIT_MAX_HALF_W / a_m, ORBIT_MAX_HALF_H / max(b_m, 1.0))
        a_px = a_m * scale
        b_px = b_m * scale
        c_px = c_m * scale
        body_px = int(max(BODY_MIN_PX, min(BODY_MAX_PX, (body_radius or a_m * 0.3) * scale)))
        focus_x = ORBIT_CX + c_px         # body sits at the focus toward periapsis (right)
        focus_y = ORBIT_CY

        # Orbit ellipse (body at right-hand focus): centre is left of the body.
        ell_box = [ORBIT_CX - a_px, ORBIT_CY - b_px, ORBIT_CX + a_px, ORBIT_CY + b_px]

        # Rings (behind body)
        if rec.get("rings"):
            _draw_rings(img, int(focus_x), int(focus_y), body_px)

        # Orbit path
        draw.ellipse(ell_box, outline=_ORBIT_COLOR, width=3)
        # Apsis line (faint dashed)
        _dashed_line(draw, (ORBIT_CX - a_px, ORBIT_CY), (ORBIT_CX + a_px, ORBIT_CY),
                     (90, 110, 140), dash=8, gap=7)

        # Body sphere
        sphere = _make_body_sphere(rec, body_px)
        img.alpha_composite(sphere, (int(focus_x - body_px), int(focus_y - body_px)))
        draw = ImageDraw.Draw(img)

        # Apoapsis (far left) / Periapsis (far right) markers
        ap_pt = (ORBIT_CX - a_px, ORBIT_CY)
        pe_pt = (ORBIT_CX + a_px, ORBIT_CY)
        _marker(draw, ap_pt, (255, 170, 90))
        _marker(draw, pe_pt, (130, 235, 160))
        # Labels sit OUTSIDE the orbit (left of apoapsis, right of periapsis) so
        # they never overlap the orbit line, which runs vertically through both
        # apsis points.
        draw.text((ap_pt[0] - 12, ap_pt[1] - 10), "Ap", font=fonts["label"], fill=(255, 190, 130), anchor="rm")
        draw.text((ap_pt[0] - 12, ap_pt[1] + 11), _fmt_alt(apo_alt), font=fonts["small"], fill=_TEXT, anchor="rm")
        draw.text((pe_pt[0] + 12, pe_pt[1] - 10), "Pe", font=fonts["label"], fill=(150, 240, 180), anchor="lm")
        draw.text((pe_pt[0] + 12, pe_pt[1] + 11), _fmt_alt(peri_alt), font=fonts["small"], fill=_TEXT, anchor="lm")

        # Vessel marker on the orbit (~55°)
        th = math.radians(55)
        vx = ORBIT_CX + a_px * math.cos(th)
        vy = ORBIT_CY - b_px * math.sin(th)
        _vessel_marker(draw, (vx, vy))
    else:
        # Surface / no-orbit view: body centred, marker on the limb.
        body_px = 120
        cx, cy = 300, 250
        if rec.get("rings"):
            _draw_rings(img, cx, cy, body_px)
        sphere = _make_body_sphere(rec, body_px)
        img.alpha_composite(sphere, (cx - body_px, cy - body_px))
        draw = ImageDraw.Draw(img)
        # Lander marker on the surface (top)
        _vessel_marker(draw, (cx, cy - body_px))
        label = "LANDED" if situation in ("LANDED", "PRELAUNCH") else ("SPLASHED" if situation == "SPLASHED" else "ON SURFACE")
        draw.text((cx, cy + body_px + 34), label, font=fonts["label"], fill=(255, 200, 130), anchor="mm")
        focus_x, focus_y = cx, cy

    # ── Body name under the body ─────────────────────────────────────────
    name_y = (focus_y if has_orbit else 250) + (body_px if not has_orbit else body_px) + (14 if has_orbit else 70)
    name_y = min(name_y, H - 24)
    draw.text((focus_x if has_orbit else 300, name_y), str(body_name),
              font=fonts["label"], fill=_TEXT, anchor="mm")

    # ── Info panel ───────────────────────────────────────────────────────
    _draw_panel(draw, fonts, {
        "vessel": vessel_name,
        "body": str(body_name),
        "situation": situation.title() if situation else "—",
        "apoapsis": _fmt_alt(apo_alt) if has_orbit else "—",
        "periapsis": _fmt_alt(peri_alt) if has_orbit else "—",
        "inclination": f"{incl:.1f}°" if incl is not None else "—",
        "eccentricity": f"{ecc:.4f}" if has_orbit else "—",
        "period": _fmt_period(period) if has_orbit else "—",
    }, known=not rec.get("unknown"))

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _dashed_line(draw, p0, p1, color, dash=8, gap=6):
    x0, y0 = p0
    x1, y1 = p1
    length = math.hypot(x1 - x0, y1 - y0)
    if length == 0:
        return
    dx, dy = (x1 - x0) / length, (y1 - y0) / length
    pos = 0.0
    while pos < length:
        seg = min(dash, length - pos)
        a = (x0 + dx * pos, y0 + dy * pos)
        b = (x0 + dx * (pos + seg), y0 + dy * (pos + seg))
        draw.line([a, b], fill=color, width=1)
        pos += dash + gap


def _marker(draw, pt, color, r=5):
    x, y = pt
    draw.ellipse([x - r, y - r, x + r, y + r], fill=color, outline=(20, 24, 32))


def _vessel_marker(draw, pt):
    x, y = pt
    s = 7
    draw.polygon([(x, y - s), (x - s, y + s), (x + s, y + s)], fill=(255, 255, 255), outline=(40, 60, 90))
    draw.ellipse([x - 2, y - 1, x + 2, y + 3], fill=(60, 120, 200))


def _draw_panel(draw, fonts, info: dict, known: bool):
    x = PANEL_X
    draw.line([(x - 18, 60), (x - 18, H - 60)], fill=(60, 66, 80), width=2)

    draw.text((x, 44), info["vessel"][:24], font=fonts["title"], fill=_TEXT)
    sub = info["body"] if known else f"{info['body']} (unknown)"
    draw.text((x, 80), sub, font=fonts["body"], fill=_ACCENT)

    rows = [
        ("Situation", info["situation"]),
        ("Apoapsis", info["apoapsis"]),
        ("Periapsis", info["periapsis"]),
        ("Inclination", info["inclination"]),
        ("Eccentricity", info["eccentricity"]),
        ("Period", info["period"]),
    ]
    y = 128
    for label, value in rows:
        draw.text((x, y), label, font=fonts["small"], fill=_TEXT_DIM)
        draw.text((x, y + 18), str(value), font=fonts["label"], fill=_TEXT)
        y += 54

    draw.text((x, H - 40), "GeneKerman · orbital telemetry", font=fonts["small"], fill=(90, 96, 110))

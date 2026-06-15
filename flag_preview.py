"""
flag_preview.py — Watermarked preview of a submitted flag-design contract.

A flag-design contract's deliverable is an image. Before the issuer pays, they
only get to see a *watermarked* version (near-full resolution, but stamped with
a repeating diagonal "PREVIEW / UNPAID" pattern) so the design can be reviewed
without being usable. The clean full-res image stays gated until acceptance —
mirroring how a craft contract's ``.craft`` file is hidden behind the blueprint.

The single public entry point is :func:`make_watermarked`. Like ``orbit_render``
it is intentionally defensive: any failure (bad image, Pillow not installed)
falls back to a plain placeholder PNG rather than raising, so a submission never
breaks because of the watermark step.
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

# Stamp wording + look
_WM_TEXT = "PREVIEW · UNPAID"
_WM_FILL = (255, 255, 255, 70)      # translucent white
_WM_ANGLE = 30                       # diagonal tilt (degrees)
# Cap the preview's longest side so an issuer can't just screenshot a clean
# full-res copy from the watermarked render.
_MAX_SIDE = 768


def _load_font(px: int):
    from PIL import ImageFont
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except Exception:
            continue
    return ImageFont.load_default()


def _placeholder() -> bytes:
    """Solid card used when the real image can't be processed."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (480, 300), (32, 34, 44))
        d = ImageDraw.Draw(img)
        d.text((40, 130), "🚩 flag preview unavailable", fill=(200, 204, 212))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        # Pillow missing entirely — hand back a 1x1 PNG so callers still get bytes.
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
            b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )


def make_watermarked(image_bytes: bytes) -> bytes:
    """Return PNG bytes of ``image_bytes`` downscaled and stamped with a repeating
    diagonal watermark. Falls back to a placeholder PNG on any failure."""
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        log.warning("flag_preview: Pillow unavailable (%s) — using placeholder", exc)
        return _placeholder()

    try:
        base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    except Exception as exc:
        log.warning("flag_preview: could not open submitted image (%s)", exc)
        return _placeholder()

    try:
        # Downscale so the watermarked copy is never full resolution.
        w, h = base.size
        scale = min(1.0, _MAX_SIDE / max(w, h))
        if scale < 1.0:
            base = base.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                               Image.LANCZOS)
        w, h = base.size

        # Build a transparent tile of repeated text, then rotate and tile it over
        # the image so the stamp covers the whole flag at an angle.
        font = _load_font(max(14, w // 14))
        stamp = Image.new("RGBA", (w * 2, h * 2), (0, 0, 0, 0))
        sd = ImageDraw.Draw(stamp)
        try:
            tw = sd.textlength(_WM_TEXT, font=font)
        except Exception:
            tw = len(_WM_TEXT) * (w // 20 or 8)
        step_y = max(28, h // 6)
        step_x = max(120, int(tw) + 40)
        for yy in range(0, h * 2, step_y):
            offset = (yy // step_y % 2) * (step_x // 2)
            for xx in range(-step_x, w * 2, step_x):
                sd.text((xx + offset, yy), _WM_TEXT, font=font, fill=_WM_FILL)
        stamp = stamp.rotate(_WM_ANGLE, expand=False)
        # Centre-crop the rotated tile back to the image size.
        left = (stamp.width - w) // 2
        top = (stamp.height - h) // 2
        stamp = stamp.crop((left, top, left + w, top + h))

        out = Image.alpha_composite(base, stamp).convert("RGB")
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        log.warning("flag_preview: watermarking failed (%s) — using placeholder", exc)
        return _placeholder()

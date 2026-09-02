"""Image bytes from clients reaching PIL and the network.

  A. decompression bomb below PIL's default MAX_IMAGE_PIXELS (89.5 MP warn,
     179 MP raise): `_shrink_image` / `_looks_like_image` / flag_preview decode a
     tiny PNG into hundreds of MB. Reproduced at 8000x8000 (64 MP, no warning)
     with the resident-set delta measured.
  B. /analyze auto-detect mode fetches the embed image URL of the user's own
     message with a plain aiohttp GET: arbitrary URL (SSRF to localhost /
     metadata) and an unbounded `resp.read()`.
  C. /analyze attachments are read whole with no size check (att.size unused).
"""
import asyncio, io, os, resource, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from _h import check, section, finish, quiet, src, between
from PIL import Image
import api_server
quiet(api_server)

def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

section("A. PIL decompression bomb under the default pixel limit")
side = 8000
im = Image.new("RGBA", (side, side), (200, 30, 30, 255))
buf = io.BytesIO(); im.save(buf, format="PNG", compress_level=9); png = buf.getvalue()
del im
print(f"         -> {side}x{side} RGBA PNG = {len(png)/1024:.0f} KB on the wire, "
      f"{side*side/1e6:.0f} MP; PIL limit {Image.MAX_IMAGE_PIXELS/1e6:.1f} MP (warn) / "
      f"{2*Image.MAX_IMAGE_PIXELS/1e6:.1f} MP (raise)")
check("the bot lowers Image.MAX_IMAGE_PIXELS below PIL's default",
      Image.MAX_IMAGE_PIXELS is not None and Image.MAX_IMAGE_PIXELS < 89_478_485,
      f"default {Image.MAX_IMAGE_PIXELS:,} px; a 13000x13000 PNG (169 MP, ~1 MB) decodes to ~680 MB RGBA + 500 MB RGB copy")
before = rss_mb(); t0 = time.perf_counter()
out, mime = api_server._shrink_image(png)
dt = time.perf_counter() - t0; after = rss_mb()
check("_shrink_image of a sub-limit bomb stays under ~100 MB resident",
      after - before < 100,
      f"+{after-before:.0f} MB RSS, {dt:.2f}s for a {len(png)/1024:.0f} KB upload (scales linearly: "
      f"169 MP ≈ {(after-before)*169/64:.0f} MB); reachable from contract submission screenshots "
      f"(bot-issued review), checkpoint/achievement photos via _looks_like_image, and the Discord "
      f"flag submission via flag_preview.make_watermarked")
api = src("api_server.py")
check("_looks_like_image only verify()s (no full decode)", "im.verify()" in between(api, "def _looks_like_image", "def _read_upload"))
fp = src("flag_preview.py")
check("flag_preview bounds pixels before convert('RGBA')",
      "MAX_IMAGE_PIXELS" in fp or ".size" in between(fp, "Image.open", "convert"),
      "flag_preview.py:70 `Image.open(...).convert(\"RGBA\")` decodes at full size first; the input is a "
      "Discord attachment (up to 25 MB, 500 MB with Nitro) chosen by the contractor")

section("B. /analyze auto-detect fetches user-supplied embed URLs")
hits = []
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        hits.append((self.path, self.headers.get("User-Agent")))
        self.send_response(200); self.send_header("Content-Type", "image/png"); self.end_headers()
        self.wfile.write(b"\x89PNG-internal-secret")
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
import cogs.screenshots as shots
class E:  # a message embed whose image points at an internal host
    def __init__(self, url):
        self.image = type("I", (), {"url": url})(); self.thumbnail = None
msg = type("M", (), {"attachments": [], "embeds": [E(f"http://127.0.0.1:{port}/internal/admin")]})()
images = asyncio.run(shots._extract_all_images(msg))
srv.shutdown()
check("embed image URLs are restricted to Discord CDN / proxy hosts before fetching",
      not hits, (f"bot fetched {hits[0][0]!r} on 127.0.0.1:{port} and got {len(images[0][1])} bytes back "
      f"(SSRF; a user controls embed.image.url by posting a link — the fetched bytes go to Gemini, "
      f"whose description of them is posted publicly)") if hits else "")
ex = between(src("cogs/screenshots.py"), "async def _extract_all_images", "async def _run_gemini")
check("embed fetch bounds the response size", "content_length" in ex or "read(" in ex and "iter_chunked" in ex,
      "`await resp.read()` with no cap: a link to a multi-GB file is read into memory")
check("embed fetch has a timeout", "timeout" in ex, "aiohttp default (5 min total) applies")

section("C. /analyze attachments")
check("attachment size is checked before att.read()", "att.size" in ex or ".size" in between(src("cogs/screenshots.py"), "if direct:", "try:"),
      "3 attachments x up to 500 MB (Nitro) read into memory and sent inline to Gemini")
finish()

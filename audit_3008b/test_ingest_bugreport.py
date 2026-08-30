"""POST /api/v1/bugreport and the request-body plumbing under it.

  A. `_trim_log` keeps the attachment inside Discord's 10 MiB bot upload limit.
  B. body limits: Content-Length middleware + chunked BodyCap exist (control),
     but FastAPI parses the multipart form BEFORE any dependency runs — so an
     unauthenticated, un-rate-limited request has its whole body (up to
     MAX_REQUEST_BYTES = 80 MiB) received and spooled to disk before the
     401 / 429 is decided.
  C. per-request memory: a 60 MB log is held whole in memory before trimming.
"""
import io
from _h import check, section, finish, quiet, src, between
import api_server
from starlette import formparsers

quiet(api_server)

section("A. _trim_log output fits a Discord attachment")
big = b"L" * (300 * 1024 * 1024 // 10)      # 30 MB stands in for the 300 MB modded log
out = api_server._trim_log(big)
check("trimmed log <= 10 MiB (Discord non-boosted upload cap)", len(out) <= 10 * 1024 * 1024,
      f"{len(out):,} bytes")
check("marker names the cut", b"bytes of log omitted" in out)
check("head and tail preserved", out.startswith(b"L" * 100) and out.endswith(b"L" * 100))

section("B. multipart is parsed before auth / rate limit run")
order = []
_orig_parse = formparsers.MultiPartParser.parse
async def spy_parse(self):
    order.append("multipart_parsed")
    return await _orig_parse(self)
formparsers.MultiPartParser.parse = spy_parse
async def spy_token(authorization=""):
    order.append("auth_checked")
    from fastapi import HTTPException
    raise HTTPException(status_code=401, detail="bad token")
api_server.get_user_token_only = spy_token      # get_current_user looks this up by module global

from fastapi.testclient import TestClient
client = TestClient(api_server.app, raise_server_exceptions=False)
payload = b"x" * (3 * 1024 * 1024)
r = client.post("/api/v1/bugreport",
                data={"summary": "s"},
                files={"ksp_log": ("KSP.log", io.BytesIO(payload), "text/plain")},
                headers={"Authorization": "Bearer nope"})
check("unauthenticated bug report is refused", r.status_code in (401, 403), f"HTTP {r.status_code}")
check("the session token is checked before the upload body is parsed/spooled",
      order and order[0] == "auth_checked",
      f"order was {order}: FastAPI (routing.py: `body = await request.form()` precedes "
      f"solve_dependencies) receives the full multipart — up to MAX_REQUEST_BYTES="
      f"{api_server.MAX_REQUEST_BYTES // 2**20} MiB, spooled to a temp file past 1 MiB — "
      f"for a request that has no valid token and has not hit the 3/hour rate limit")
api = src("api_server.py")
check("Content-Length guard exists (control)", "int(cl) > MAX_REQUEST_BYTES" in api)
check("chunked-body cap exists (control)", "app.add_middleware(_BodyCapMiddleware)" in api)

section("C. memory per bug report")
check("log is read whole (MAX_LOG_BYTES) into memory before _trim_log",
      "_trim_log(await _read_upload(ksp_log, MAX_LOG_BYTES))" in api
      and api_server.MAX_LOG_BYTES <= 10 * 1024 * 1024,
      f"MAX_LOG_BYTES={api_server.MAX_LOG_BYTES // 2**20} MiB is buffered per request although the "
      f"client already trims to 9 MB and the server discards everything past head+tail anyway; "
      f"3/hour/user but N users in parallel = N x 60 MiB resident (informational, rate-limited)")
finish()

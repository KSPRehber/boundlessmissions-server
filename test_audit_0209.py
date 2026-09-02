"""
Regression checks for the 2026-09-02 multi-agent audit (0209_security_audit.md).

Pure-function and source-guard checks only — no Firestore, no Discord, no network.
Run with:
    python test_audit_0209.py

Source-guard checks are deliberate here, as in test_audit_3008.py and
test_audit_3108.py: most of these findings are "this call site is missing a
bound", which has no runtime surface to assert against without a live Firestore.
A guard that pins the call site is what stops the fix being quietly removed.

Each check names the finding id it locks, so a failure says which audit item has
regressed rather than merely which line moved.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DISCORD_TOKEN", "x")

import api_server
import api_auth
import api_models as _api_models
import config

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def read(*parts):
    try:
        with open(os.path.join(*parts)) as f:
            return f.read()
    except OSError:
        return ""


SRC = {name: read(HERE, name) for name in (
    "api_server.py", "api_auth.py", "api_models.py", "bot.py", "config.py",
    "requirements.txt", ".gitignore",
)}
MOD = os.path.join(ROOT, "KSP Mod Side", "GeneKerman")

passed = failed = 0


def check(label, cond):
    global passed, failed
    passed += bool(cond)
    failed += (not cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")


def fn_first_statements(src, name, count=6):
    """The first `count` statements of a top-level function, as source text."""
    for node in ast.parse(src).body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            body = node.body
            i = 1 if (isinstance(body[0], ast.Expr)
                      and isinstance(body[0].value, ast.Constant)
                      and isinstance(body[0].value.value, str)) else 0
            return "\n".join(ast.dump(s) for s in body[i:i + count])
    return ""


API = SRC["api_server.py"]
AUTH = SRC["api_auth.py"]

# ══ EC1 — rescue-create rollback double-refunded escrow (coin mint) ═══════════
print("\n[EC1] the rescue rollback cannot refund an escrow ca.cancel already released")
_rescue = fn_first_statements(API, "create_rescue_contract", 40)
check("EC1: the upload is read+metered before the escrow debit",
      API.index("_charge_upload_quota(uid, len(node_bytes))")
      < API.index("Escrow the payment (atomic check-and-deduct"))
check("EC1: the remaining rollback takes the contract lock",
      'async with ca.contract_lock(c["contract_id"]):' in API)
check("EC1: and refunds only while the contract is still PENDING",
      'fresh.get("status") == cdb.PENDING' in API)
# Two legitimate refunds now: the create-failure guard (UP25) and the upload-failure
# rollback under `contract_lock`. The bug was a THIRD, unlocked one on the quota path,
# which no longer exists because the quota is charged before any money moves.
# The quota handler still exists — it just cannot refund any more, because it now
# runs before the escrow moves. That is the property, not its absence: assert that
# the block between the handler and the next `except` moves no money.
_quota_block = API.split("except HTTPException as quota_exc:")[1].split("except ")[0]
check("EC1: the quota refusal refunds nothing (it precedes the debit)",
      "add_balance" not in _quota_block
      and API.index("_charge_upload_quota(uid, len(node_bytes))")
          < API.index("Escrow the payment (atomic check-and-deduct"))

# ══ EC4 — a weekly mission minted before on_ready is unpayable ════════════════
print("\n[EC4] no contract is issued while the bot id is unset")
check("EC4: select_mission refuses when _get_bot_user_id() is 0",
      "if not bot_user_id:" in API and "still starting up" in API)
check("EC4: and releases the selection claim it took",
      "_release_selection(gid, wk, uid, req.mission_id)" in API)

# ══ EC7 — KSP-tier contract transitions had no rate limit ════════════════════
print("\n[EC7] every KSP-tier transition carries a limiter as its first statement")
for name in ("accept_contract", "review_submission", "resolve_dispute",
             "settle_response_from_ksp", "more_time_response_from_ksp",
             "reimport_submission", "cancel_contract", "give_up_contract"):
    check(f"EC7: {name} is rate limited",
          "'ksptx:" in fn_first_statements(API, name, 1)
          or "ksptx:" in fn_first_statements(API, name, 1))

# ══ UP20 — sent_vessels was unbounded, synchronous and unmetered ═════════════
print("\n[UP20] the telemetry render loop is bounded, threaded and metered")
check("UP20: the snapshot list is sliced to the image cap",
      "snaps = snaps[:MAX_SUBMISSION_IMAGES]" in API)
check("UP20: the render runs off the event loop",
      "await asyncio.to_thread(render_orbit, snap)" in API)
check("UP20: each rendered PNG is charged to the upload quota",
      "_charge_upload_quota(uid, len(orbit_png))" in API)

# ══ UP21 — two unrated endpoints each read a whole contract history ══════════
print("\n[UP21] the two full-history readers are rate limited")
check("UP21: finance_overview is limited", "finance:" in fn_first_statements(API, "finance_overview", 2))
check("UP21: get_incoming_contracts is limited",
      "ctincoming:" in fn_first_statements(API, "get_incoming_contracts", 2))

# ══ UP22 / INF3 — Gemini calls on the shared event loop ══════════════════════
print("\n[UP22/INF3] no Gemini call blocks the event loop, and the quota is per call")
_bare = [ln for ln in API.split("\n")
         if "gemini_client.models.generate_content(" in ln
         and "to_thread" not in ln and "lambda" not in ln]
# _ai_resolve_part's site is synchronous by design; its caller is threaded instead.
check("UP22: at most the one deliberately-synchronous call site remains bare",
      len(_bare) <= 1)
check("UP22: _resolve_constraints is called through to_thread",
      "asyncio.to_thread(\n                _summary_constraints" in API
      or "asyncio.to_thread(\n            _summary_constraints" in API
      or "await asyncio.to_thread(\n                _resolve_constraints" in API
      or "_summary_constraints, c, gid, uid, constraints" in API)
check("UP22: the AI allowance is charged inside the resolver, not per builder",
      API.index('def _resolver(') < API.index('_rate_limit(f"gemini:{uid}",'))

# ══ UP25 — unbounded modlist, and escrow lost on a failed create ═════════════
print("\n[UP25] modlist is bounded and a failed create refunds the escrow")
# The bound is still on both models; it is now a shared constant rather than a
# repeated literal, and 8000 rather than 2000 — measured against real heavy installs
# (FAK1's own mod list is 1924 chars, 96% of the old cap) instead of guessed. What
# matters is that neither model is unbounded and both agree.
check("UP25: both request models bound modlist",
      SRC["api_models.py"].count(
          "modlist: Optional[str] = Field(default=None, max_length=MODLIST_MAX_LENGTH)") == 2)
check("UP25: the shared bound clears what a real heavy install produces",
      _api_models.MODLIST_MAX_LENGTH >= 4000)
check("UP25: create_contract failure returns the payment",
      'detail="Contract could not be created"' in API)

# ══ UP26 — the raw upload went to the model ══════════════════════════════════
print("\n[UP26] achievement photos are downscaled before the model sees them")
check("UP26: _shrink_image is applied", "ai_data, _ai_mime = _shrink_image(data)" in API)
check("UP26: and an over-ceiling image is refused, not sent whole",
      "if not ai_data:" in API)

# ══ UP27 — client text interpolated bare into a prompt ═══════════════════════
print("\n[UP27] the single-contract classifier fences its client text")
check("UP27: mission text goes through _client_text_block",
      '_client_text_block("mission", mission_text)' in API)
check("UP27: and is no longer interpolated bare",
      'f"Mission: \\"{mission_text}\\"' not in API)

# ══ AU0209-1 — anonymous Firestore amplification ═════════════════════════════
print("\n[AU0209-1] the config documents are memoised and /version/check is bounded")
# The cache lives in the data layer, not here, so `mver.check()` — which reads the
# document itself — is covered too. A memo in api_server.py would have missed it.
MV = read(HERE, "data", "mod_version.py")
PL = read(HERE, "data", "policy.py")
check("AU0209-1: mod_version memoises get_config", "def get_config(" in MV
      and ("_cache" in MV or "_CACHE" in MV or "_memo" in MV))
check("AU0209-1: policy memoises get_version", "_cache" in PL or "_CACHE" in PL or "_memo" in PL)
check("AU0209-1: both expose an invalidate for the console",
      "def invalidate(" in MV and "def invalidate(" in PL)
check("AU0209-1: version_check is per-IP bounded", '_rate_limit_ip("vercheck_ip"' in API)
check("AU0209-1: the console invalidates after publishing",
      "mver.invalidate()" in API and "policy.invalidate()" in API)
check("AU0209-1: a failed read is not cached as 'nothing published'",
      "fresh" in MV)

# ══ AU0209-2 — snowflake compared against an account id ══════════════════════
print("\n[AU0209-2] challenge ownership accepts either id form")
check("AU0209-2: the helper exists", "def _owns_challenge(" in AUTH)
check("AU0209-2: all three call sites use it",
      AUTH.count("_owns_challenge(data.get(\"user_id\"), acting_user_id)") == 3)
check("AU0209-2: the bare snowflake comparison is gone",
      'str(data.get("user_id")) != str(acting_user_id)' not in AUTH)
check("AU0209-2: a failed lookup does not approve",
      "return False" in AUTH.split("def _owns_challenge")[1].split("def ")[0])

# ══ AU0209-4 — device poll served any caller ════════════════════════════════
print("\n[AU0209-4] the device-approval poll is bound to its owner")
check("AU0209-4: poll_device_challenge takes an owner",
      "def poll_device_challenge(challenge_id: str, owner_id: str | None = None)" in AUTH)
check("AU0209-4: a foreign challenge reads as expired",
      'if owner_id is not None and str(data.get("user_id") or "") != str(owner_id):' in AUTH)
check("AU0209-4: the endpoint passes the caller",
      "owner_id=str(user.get(\"user_id\"))" in API)

# ══ AU0209-5 — any Firebase provider was accepted ═══════════════════════════
print("\n[AU0209-5] web sign-in allow-lists the provider")
check("AU0209-5: the allow-list exists",
      "_ALLOWED_SIGN_IN_PROVIDERS = frozenset({\"password\", \"google.com\"})" in API)
check("AU0209-5: and is enforced before the e-mail check",
      API.index("if provider not in _ALLOWED_SIGN_IN_PROVIDERS:")
      < API.index('if email and not decoded.get("email_verified"):'))

# ══ AU0209-6 — a revoked token was accepted after a restart + a blip ════════
print("\n[AU0209-6] an unreadable revocation version is not read as 'never revoked'")
check("AU0209-6: the exception type exists", "class TokenVersionUnavailable" in AUTH)
check("AU0209-6: raised only when nothing is cached",
      "if cached is None:\n            raise TokenVersionUnavailable" in AUTH)
check("AU0209-6: the dependency answers 503, not 401 or 200",
      "except TokenVersionUnavailable:" in API and "status_code=503" in API)

# ══ INF2 — blocking Firestore in the auth dependency; no account budget ═════
print("\n[INF2] the auth dependency is threaded and carries a floor limiter")
check("INF2: token verification is threaded",
      "await asyncio.to_thread(verify_session_token, token, secrets_)" in API)
check("INF2: the suspension check is threaded",
      'await asyncio.to_thread(enforce_not_suspended, user["user_id"])' in API)
check("INF2: a per-account ceiling applies to every token-gated route",
      "_rate_limit(f\"acct:{user['user_id']}\", max_hits=600, window=60.0)" in API)

# ══ INF5 — the startup banner had fallen behind the gate flags ══════════════
print("\n[INF5] the security-gate banner is derived from one register")
check("INF5: the register exists", "def insecure_gates()" in SRC["config.py"])
for flag in ("KSP_VERSION_CHECK_ENABLED", "KSP_DEVICE_BINDING_ENABLED", "KSP_2FA_ENABLED",
             "KSP_CHEAT_DISQUALIFY_ENABLED", "DEBUG_ENDPOINTS_ENABLED"):
    check(f"INF5: {flag} is registered", flag in SRC["config.py"].split("def insecure_gates")[1])
check("INF5: the debug flag is registered as unsafe-when-true",
      '("DEBUG_ENDPOINTS_ENABLED", cfg.DEBUG_ENDPOINTS_ENABLED, False)' in SRC["config.py"])
check("INF5: bot.py no longer keeps its own list",
      "disabled_gates = insecure_gates()" in SRC["bot.py"])
check("INF5: the register is callable and returns strings",
      all(isinstance(x, str) for x in config.insecure_gates()))

# ══ INF6 / INF7 / INF8 — config-surface hygiene ═════════════════════════════
print("\n[INF6/7/8] secrets, dependencies and the bind address")
check("INF6: .gitignore covers every .env variant", ".env.*" in SRC[".gitignore"])
check("INF6: while keeping the example", "!.env.example" in SRC[".gitignore"])
check("INF7: python-multipart is declared", "python-multipart" in SRC["requirements.txt"])
check("INF8: the API binds loopback by default",
      '_optional("API_HOST", "127.0.0.1")' in SRC["config.py"])

# ══ MK0209-4 / 5 / 6 / 10 / 11 / 12 — moderation surface ════════════════════
print("\n[MK] the moderation surface")
check("MK0209-4: announce refuses @everyone", "if role.is_default():" in API)
check("MK0209-4: announce refuses a managed role", "if role.managed:" in API)
check("MK0209-4: and the send names the only role it may ping",
      "allowed_mentions=discord.AllowedMentions(" in API)
# The gate now lives in `_rate_limit_ip`, which applies the bucket only when
# API_TRUSTED_PROXIES is set. That is stronger than the old inline `if`: it is
# impossible to add a per-IP limiter that forgets it, which is the mistake that had
# been made three separate times.
check("MK0209-5: the vote limiter is gated on trusted proxies",
      '_rate_limit_ip("mkvote_ip"' in API)
check("MK0209-5: the gate lives in the shared helper",
      "def _rate_limit_ip(" in API and "if cfg.API_TRUSTED_PROXY_NETS:" in API)
# No per-IP bucket may call `_rate_limit` directly with a client address: the
# collapsed-bucket mistake is only prevented if the gate cannot be forgotten.
# `link:{ip}` is the one remaining direct call and it carries its own explicit
# `if cfg.API_TRUSTED_PROXIES:` (it also guards a ten-minute LOCKOUT, which under a
# collapsed address is a community-wide kill switch on linking).
# Exactly one place may build a bucket key from a client address, and it is the
# helper that carries the API_TRUSTED_PROXIES gate. Any second occurrence is a
# limiter that forgot it.
check("MK0209-5/CS4: only the shared helper keys a bucket on a client address",
      API.count('_client_ip(request)}"') == 1
      and API.index('_client_ip(request)}"') > API.index("def _rate_limit_ip("))
check("CS4: the link lockout is itself gated on distinguishable addresses",
      API.index("if cfg.API_TRUSTED_PROXY_NETS:\n        if _link_locked_out(ip):") > 0)
check("MK0209-6: ticket open escapes player markdown",
      "_discord.utils.escape_markdown(req.title.strip())" in API)
check("MK0209-6: ticket reply escapes player markdown",
      "_discord.utils.escape_markdown(req.body.strip())[:4000]" in API)
check("MK0209-10: the rating-floor notice uses a string account id",
      'seller_id = str(listing.get("seller_id") or "")' in API)
check("MK0209-11: quicksend craft_name is bounded",
      API.count('craft_name = (craft_name or "").strip()[:100] or "Craft"') == 2)
check("MK0209-12: the policy URLs are https-guarded",
      'must start with https://' in API)

# ══ MK0209-1 / MK0209-9 / EC8 / UP21 — the cross-file halves ════════════════
print("\n[cross-file] the halves that span api_server and the data layer")
import data.suspicion as _susp
import data.friends as _fr
import data.contracts as _cdb
import data.store as _st
check("MK0209-1: release_ticket exists", hasattr(_susp, "release_ticket"))
check("MK0209-1: every bail-out after the claim releases it",
      API.count("susp.release_ticket, gid, uid, reason") == 4)
check("MK0209-1: a refused ticket releases the claim",
      "if channel is None:" in API and "claim released" in API)
check("MK0209-9: decline_all exists", hasattr(_fr, "decline_all"))
check("MK0209-9: both tiers expose it",
      '/api/v1/friends/decline_all' in API and '/api/v1/web/friends/decline_all' in API)
check("MK0209-9: and it is rate limited", "frdeclineall:" in API)
check("UP21: the escrow read is narrowed and capped",
      "statuses=cdb.ESCROW_STATUSES" in API and "limit=500" in API)
check("UP21: iter_user_contracts takes the filter",
      "statuses" in str(__import__("inspect").signature(_cdb.iter_user_contracts))
      and "limit" in str(__import__("inspect").signature(_cdb.iter_user_contracts)))
check("UP21: and the two status sets agree",
      _cdb.ESCROW_STATUSES == {_cdb.PENDING, _cdb.ACTIVE, _cdb.SUBMITTED,
                               _cdb.DISPUTED, _cdb.MOD_REVIEW})
check("EC8: the gross claim exists", hasattr(_st.store, "try_claim_timed_reward_gross"))
check("EC8: the upload bonus reports the net figure",
      "granted, wait, paid = await store.try_claim_timed_reward_gross(" in API)
check("EC8: and names the garnished part",
      "went to your outstanding debt" in API)

# ══ EC2 — a failed store load must not wipe every wallet ════════════════════
print("\n[EC2] a failed load is fatal, not silently empty")
STORE = read(HERE, "data", "store.py")
check("EC2: a load failure is recorded", "_load_failed" in STORE)
check("EC2: the empty-dict reset is gone",
      "self._users = {}" not in STORE.split("def load")[1].split("def ")[0])
check("EC2: save refuses while the flag is set",
      STORE.count("_load_failed") >= 4)
# The re-raise alone was not enough: bot.py's cog loop catches it, so the bot came
# up with cogs.xp unloaded — writes blocked (nothing destroyed) but every balance
# reading 0 and only a log line to say why. The gate is the part that stops it.
check("EC2: the store exposes a positive loaded assertion", "def loaded(" in STORE)
check("EC2: and it is False before any load has run",
      __import__("data.store", fromlist=["store"]).store.loaded is False)
check("EC2: bot.py refuses to start on an unloaded wallet",
      "if not store.loaded:" in SRC["bot.py"] and "will not start" in SRC["bot.py"])
check("EC2: the gate runs after the cog loop, not inside it",
      SRC["bot.py"].index("failed_cogs.append(module)")
      < SRC["bot.py"].index("if not store.loaded:"))

# ══ R-pass: regressions the fix pass itself introduced ══════════════════════
print("\n[review] bugs introduced by the fix pass, now closed")
import asyncio as _aio
from data.store import store as _store
# EC2-R1: the write guards must test the POSITIVE assertion. `_load_failed` is
# False in the never-loaded state, and bot.close() flushes on the way out — so the
# startup gate's own shutdown could have written zeroed records over real ones.
check("EC2-R1: save() gates on `not _loaded`, not `_load_failed`",
      "if not self._loaded:" in STORE
      and "if self._load_failed:" not in STORE.split("async def save")[1].split("async def ")[0])
check("EC2-R1: a never-loaded store does not queue a minted record",
      (lambda: (_store.get_user(0, "rcheck1"), not _store._dirty_users)[1])())
check("EC2-R1: and save() writes nothing in that state",
      (lambda: (_aio.run(_store.save()), not _store._loaded)[1])())
# EC2-R1b: a cost_guard FROZEN must not make the bot permanently unstartable.
check("EC2-R1b: a budget freeze is a distinct state", "def budget_blocked(" in STORE)
check("EC2-R1b: load() splits it from a read failure",
      "except FirebaseBudgetExceeded" in STORE)
check("EC2-R1b: and the startup gate does not exit on it",
      "if not store.loaded and store.budget_blocked:" in SRC["bot.py"])
# MK0209-7: missed entirely by the first pass.
check("MK0209-7: the sale DM escapes both player strings",
      "_esc(str(user['username']))" in API and "allowed_mentions=discord.AllowedMentions.none()" in API)
# UP24: the quicksend blueprint half.
check("UP24: quicksend reads with the blueprint cap",
      "_read_upload(blueprint, MAX_BLUEPRINT_BYTES)" in API)
check("UP24: and validates it as an image before publishing",
      "if bp_data and not _looks_like_image(bp_data):" in API)
# UP21: the incoming-contract query was still unfiltered.
check("UP21: /contracts/incoming filters status in the query",
      '.where("status", "==", cdb.PENDING)' in API)
# EC1/UP25: the rescue create was unwrapped with two unbounded form fields.
# Updated with the R35 fix. This assertion used to require the LITERAL
# `Form(None, max_length=2000)`, which is the defect R35 reported: RB6 raised the cap to
# MODLIST_MAX_LENGTH (8000) at the two api_models sites and left this third one — the one
# path that cannot send a shorter list, since ContractCreation always sends every
# part-contributing folder — pinned at 2000 by a test. Assert the shared constant is used,
# which is a property that survives the next re-sizing; a literal cannot be.
check("UP25: the rescue form fields are bounded",
      "Form(None, max_length=MODLIST_MAX_LENGTH)" in API
      and 'Form("[]", max_length=8000)' in API)
check("EC1-R1: the rescue create refunds on a failed write",
      '"Could not create the rescue. Your payment was returned."' in API)
# EC6: resolving the account id broke the corp lookup for joined accounts.
CORPS = read(HERE, "cogs", "corps.py")
check("EC6-R1: the corp lookup falls back to the linked snowflake",
      "discord_for_account" in CORPS)
# flag_suspicion must not release a claim whose ticket exists.
check("MK0209-1-R3: no release once the ticket was created",
      'if not locals().get("opened"):' in API)

# ══ INF1 — the build channel (fix lives in the mod tree) ════════════════════
print("\n[INF1] a plain build is a production build")
BUILD = read(ROOT, "KSP Mod Side", "build.sh")
check("INF1: the channel defaults to production",
      'CHANNEL="${GK_CHANNEL:-production}"' in BUILD)
check("INF1: a non-production hash is not offered for publishing",
      "do not publish" in BUILD.lower())

# ══ MD9 / MD11 / MD12 — the mod's peer-input bounds ═════════════════════════
print("\n[MD] the mod bounds what a peer can hand it")
VT = read(MOD, "VesselTransfer.cs")
FT = read(MOD, "FlagTransfer.cs")
check("MD9: the fleet vessel count is bounded", "MaxFleetVessels" in VT)
check("MD9: the per-vessel crew count is bounded", "MaxCrewPerVessel" in VT)
check("MD11: received flags are limited to judgeable formats",
      "RECEIVED_FLAG_EXTS" in FT)
check("MD11: a payload's flag count is bounded", "MaxFlagsPerPayload" in FT)
check("MD12: a gift-accepted echo is corroborated locally",
      os.path.exists(os.path.join(MOD, "QuicksendLedger.cs")))

# ══ R2 review: destructive regressions the mod fix pass introduced ══════════
print("\n[review/mod] regressions the mod fix pass introduced, now closed")
VT2 = read(MOD, "VesselTransfer.cs")
CS2 = read(MOD, "ClientState.cs")
SP2 = read(MOD, "SurfacePlacement.cs")
# MD14-R2: SMA = NaN is KSP's own storage for a surface vessel (found in a stock
# scenario). The guard refused every landed/splashed craft, and the refusal acked —
# which on a live quicksend deletes the only remaining copy of the ship.
check("MD14-R2: a surface vessel is not refused for a non-finite orbit",
      "SurfacePlacement.IsOnSurface(innerNode)" in VT2)
check("MD14-R2: IsOnSurface is reachable from the guard",
      "internal static bool IsOnSurface" in SP2)
check("MD14-R2: the orbit parse is culture-invariant",
      "NumberStyles.Float, CultureInfo.InvariantCulture" in VT2)
check("MD14-R2: a node-level refusal does not ack the queue entry",
      "deliberately no /done" in CS2)
check("MD14-R2: a size refusal still acks (its snapshot is not a giveable ship)",
      "if (unpackRefused)" in CS2)
# MD9-R2: the crew cap left the `crew =` reference behind, which silently breaks
# Astronaut Complex hiring for the life of the save.
check("MD9-R2: unfulfilled crew references are stripped",
      "StripUnfulfilledCrewRefs" in VT2)
check("MD9-R2: the strip runs before the ProtoVessel is built",
      VT2.index("StripUnfulfilledCrewRefs(vesselNode, roster, addedNames)")
      < VT2.index("private static void StripUnfulfilledCrewRefs"))
check("MD9-R2: the crew cap is above an honest large station",
      "MaxCrewPerVessel = 200" in VT2)

# ══ Residuals closed after the review (owner-approved) ══════════════════════
print("\n[residuals] the four items the review left open")
import types as _types
import cost_guard as _cg
TIK = read(HERE, "cogs", "tickets.py")
CG = read(HERE, "cost_guard.py")

# UP23-R3: the at-rest estimate only ever rose, and max() made it permanent.
check("UP23-R3: a tier-1 poll clamps the estimate down", "self._stored_bytes = self._auth_stored_bytes" in CG)
def _clamp_probe():
    g = _cg.guard
    g._stored_bytes, g._auth_stored_bytes = 5_000_000_000, 0
    # `present` names the series that actually returned a value. A gauge query can
    # succeed and carry no datapoint — the expected state in the first hours of every
    # UTC month — and adopting that as a true zero wiped the estimate on disk (RB2).
    g.ingest_usage(_types.SimpleNamespace(ok=True, fetched_at=0.0,
                                          stored_bytes=650_000_000, daily={},
                                          present={"stored_bytes"}))
    down = g._stored_bytes == 650_000_000
    g._stored_bytes = 900_000_000
    g.ingest_usage(_types.SimpleNamespace(ok=False, error="403", present=set()))
    failed_kept = g._stored_bytes == 900_000_000
    # ...and an OK poll that simply carried no storage reading must also not clamp.
    g.ingest_usage(_types.SimpleNamespace(ok=True, fetched_at=0.0, stored_bytes=0,
                                          daily={}, present=set()))
    return down and failed_kept and g._stored_bytes == 900_000_000
check("UP23-R3: it clamps on a good poll and not on a failed one", _clamp_probe())

# MK0209-8-R3: the modal was the one ticket door with no per-user allowance.
check("MK0209-8-R3: the modal has a per-user allowance", "_allow_modal_opening" in TIK)
check("MK0209-8-R3: it is charged before the channel is opened",
      TIK.index("if not _allow_modal_opening(interaction.user.id):")
      < TIK.index("channel = await create_ticket("))
def _modal_probe():
    import cogs.tickets as _t
    _t._MODAL_OPENINGS.clear()
    got = [_t._allow_modal_opening(999) for _ in range(5)]
    return got == [True] * _t.TICKET_MODAL_PER_USER_PER_HOUR + \
                  [False] * (5 - _t.TICKET_MODAL_PER_USER_PER_HOUR)
check("MK0209-8-R3: and it actually refuses past the allowance", _modal_probe())
check("MK0209-8-R3: the budget branch alerts, not just logs",
      'reason="budget_spent"' in TIK)

# WB3-R3: entitlement was resolved through two full collection queries.
check("WB3-R3: a single-listing download endpoint exists",
      '/api/v1/web/marketplace/{listing_id}/download' in API)
check("WB3-R3: it reads one document, not a collection",
      "await asyncio.to_thread(mkt.get_listing, gid, listing_id)" in API)
check("WB3-R3: it signs at the default short TTL, not the 7-day max",
      "await asyncio.to_thread(sign_stored, raw)" in API)
check("WB3-R3: and it is rate limited", "mktdl:" in API)
check("WB3-R3: not-entitled and not-found answer alike",
      API.count('detail="That craft isn\'t available."') >= 3)

# ══ Worth-doing tail (owner-approved) ═══════════════════════════════════════
print("\n[tail] the four residuals worth closing")
CTR = read(HERE, "cogs", "contracts.py")
CDB = read(HERE, "data", "contracts.py")
ADM = read(HERE, "cogs", "admin.py")
PRM = read(HERE, "cogs", "perms.py")

# EC4: the contracts already written with issuer_id "0" are repaired.
check("EC4-tail: a bot-issuer repair task exists", "async def repair_bot_issuer" in CTR)
check("EC4-tail: it is started and cancelled with the cog",
      "self.repair_bot_issuer.start()" in CTR and "self.repair_bot_issuer.cancel()" in CTR)
check("EC4-tail: it waits for ready (the bot id is the thing it needs)",
      "@repair_bot_issuer.before_loop" in CTR)
check("EC4-tail: it only rewrites non-terminal contracts",
      "not in cdb.ACTIVE_STATUSES" in CTR)
check("EC4-tail: backed by an indexed issuer query", "def list_by_issuer" in CDB)
check("EC4-tail: and the bot-id check now precedes the claim",
      API.index("bot_user_id = _get_bot_user_id()")
      < API.index("if _save_selection(gid, wk, uid, req.mission_id) is False:"))

# logout_all was the one token-gated route with no per-account budget.
check("AU-tail: logout_all is rate limited", "logoutall:" in API)

# MK0209-3 residual: the shape permission bits cannot express.
check("MK0209-3-tail: a channel-overwrite check exists",
      "def role_opens_private_channel" in PRM)
check("MK0209-3-tail: it is applied at map time", "role_opens_private_channel(" in ADM)
check("MK0209-3-tail: a failed inspection refuses rather than allows",
      "Refusing the mapping rather than guessing" in PRM)
def _overwrite_probe():
    import types as _t
    from cogs import perms as _p
    class _OW:
        def __init__(self, v=None, r=None): self.view_channel=v; self.read_messages=r
    class _Ch:
        def __init__(self, n, o): self.name=n; self._o=o
        def overwrites_for(self, r): return self._o.get(r, _OW())
    class _R:
        def __init__(self, n): self.name=n
    ev = _R("@everyone"); ev.permissions = _t.SimpleNamespace(view_channel=True)
    key = _R("staff-key")
    private = _t.SimpleNamespace(default_role=ev,
                                 channels=[_Ch("staff", {key: _OW(v=True), ev: _OW(v=False)})])
    public = _t.SimpleNamespace(default_role=ev,
                                channels=[_Ch("general", {key: _OW(v=True), ev: _OW(v=True)})])
    return (_p.role_opens_private_channel(private, key) is not None
            and _p.role_opens_private_channel(public, key) is None)
check("MK0209-3-tail: refuses a private-channel key, allows a public one",
      _overwrite_probe())

# MD11 tail: a flag the recipient may not write is re-encoded, not lost.
FT2 = read(MOD, "FlagTransfer.cs")
check("MD11-tail: unwritable formats are transcoded on export", "TranscodeToPng" in FT2)
check("MD11-tail: the export-side format predicate exists", "IsReceivableExt" in FT2)
check("MD11-tail: the content address is computed AFTER the re-encode",
      FT2.index("byte[] png = TranscodeToPng(url);")
      < FT2.index("string newUrl = ComputeContentUrl(data);"))
check("MD11-tail: and after the safety check, so it names what ships",
      FT2.index("if (!ToolActions.ImageIsSafeToDecode(data,")
      < FT2.index("string newUrl = ComputeContentUrl(data);"))
check("MD11-tail: a failed transcode embeds nothing rather than something refusable",
      "not embedding; that reference resets to the stock flag" in FT2)
check("MD11-tail: RECEIVED_FLAG_EXTS was not widened",
      'RECEIVED_FLAG_EXTS = { "png", "jpg", "jpeg" }' in FT2)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

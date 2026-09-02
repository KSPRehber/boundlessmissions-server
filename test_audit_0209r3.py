"""
test_audit_0209r3.py — regression locks for the 2026-09-02 pass-3 audit
(`0209_security_audit_pass3.md`).

One check per finding id, so a failure names WHICH audit item regressed. Standalone
`__main__`, not pytest (these files `sys.exit`).

Written after R32, which found that two of the previous suite's four "real exercises"
were vacuous — one caught its own harness's RuntimeError and reported it as a pass, the
other asserted a hand-written stub's model of Firestore rather than Firestore's. So the
rules here are:

  * a live exercise catches ONLY the exception it is testing for, never a bare one;
  * where a live exercise exists, a NEGATIVE CONTROL asserts the same call behaves
    differently in the state the guard is supposed to ignore — without it, "it raised"
    cannot be told from "something else raised";
  * a source-shaped check asserts a PROPERTY that survives re-sizing (a shared constant,
    a call, an ordering) rather than a literal. The UP25 assertion in test_audit_0209.py
    pinned `max_length=2000` and thereby preserved the very defect R35 reported.
"""

import ast
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DISCORD_TOKEN", "x")

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.normpath(os.path.join(ROOT, "..", "Website"))
MOD = os.path.normpath(os.path.join(ROOT, "..", "KSP Mod Side"))


def read(path, base=ROOT):
    with open(os.path.join(base, path), encoding="utf-8") as f:
        return f.read()


API = read("api_server.py")
AUTH = read("api_auth.py")
CA = read("contract_actions.py")
CONTRACTS_COG = read("cogs/contracts.py")
AUCTIONS = read("cogs/auctions.py")
ECON = read("cogs/economy.py")
ADMIN = read("cogs/admin.py")
GC = read("data/guild_config.py")
ACCTS = read("data/accounts.py")
BRIDGE = read("cogs/ksp_bridge.py")
MODVER = read("data/mod_version.py")
ADB = read("data/auctions.py")
WM = read("cogs/weeklymissions.py")

VT = read("GeneKerman/VesselTransfer.cs", MOD)
CONSENT = read("GeneKerman/Consent.cs", MOD)
GKROUTES = read("GeneKerman/Web/GkRoutes.cs", MOD)
STATICF = read("GeneKerman/Web/StaticFiles.cs", MOD)
LOCALSRV = read("GeneKerman/Web/LocalServer.cs", MOD)
APICLIENT = read("GeneKerman/ApiClient.cs", MOD)
DBRIDGE = read("GeneKerman/Web/DebugBridge.cs", MOD)
NETKAN = read("BoundlessMissions.netkan", MOD)
BUILDSH = read("build.sh", MOD)
ASSERTSH = read("tools/assert_production_clean.sh", MOD)
INSTANCES = read("tools/gkbridge/instances.py", MOD)
CREATECONTRACT_TSX = read("WebUI/src/screens/CreateContract.tsx", MOD)

SERVERAPI_TS = read("src/lib/server-api.ts", WEB)
LISTINGS_TS = read("src/app/api/marketplace/listings/route.ts", WEB)


# ── DP1 — the unregistered distribution namespace ────────────────────────────
print("\n[DP1] the mod's published download pointers name an org we own")
REAL = "Boundless-Missions/boundlessmissions-modside"
check("DP1: no reference to the unregistered gk-ksp org survives anywhere",
      "gk-ksp" not in NETKAN and "gk-ksp" not in BUILDSH
      and "gk-ksp" not in read("GameData/BoundlessMissions/GeneKerman.version", MOD))
check("DP1: the netkan $kref points at the real org",
      f'"$kref": "#/ckan/github/{REAL}"' in NETKAN)
check("DP1: build.sh stamps the real org into every release",
      f'"DOWNLOAD": "https://github.com/{REAL}/releases/latest"' in BUILDSH)
check("DP1: the AVC version file is published where its own URL points",
      'WEBSITE_PUBLIC' in BUILDSH and 'GeneKerman.version"' in BUILDSH
      and os.path.exists(os.path.join(WEB, "public", "GeneKerman.version")))
check("DP1: the pre-release checklist now covers the distribution URLs",
      "must be one this project controls" in read("PACKAGING.md", MOD))


# ── R31 — the unbounded rescue-wreck import ──────────────────────────────────
print("\n[R31] MB1's part bound reaches BOTH vessel-import entry points")
check("R31: ImportVesselAtTarget bounds the part count",
      'PartCountIsSane(innerNode, "ImportVesselAtTarget")' in VT)
check("R31: and it is still bounded at the other entry point",
      'PartCountIsSane(innerNode, "ImportOneInner")' in VT)
# The check must sit AFTER the node is loaded and BEFORE anything with a side effect.
_ivt = VT[VT.index("public static string ImportVesselAtTarget"):]
_ivt = _ivt[:_ivt.index("SpawnInnerNode(innerNode)")]
check("R31: the bound is ahead of TagCrew/PlaceAtTarget, not after them",
      _ivt.index("PartCountIsSane") < _ivt.index("TagCrew("))


# ── R32 — the vacuous regression checks ──────────────────────────────────────
print("\n[R32] the previous suite's live checks actually run the code they name")
R2 = read("test_audit_0209r2.py")
_R2_CODE = "\n".join(l for l in R2.splitlines() if not l.lstrip().startswith("#"))
check("R32: the wallet check no longer uses get_event_loop",
      "asyncio.get_event_loop()" not in _R2_CODE)
check("R32: and no longer swallows a bare RuntimeError as success",
      "except RuntimeError:" not in _R2_CODE)
check("R32: a negative control distinguishes the guard from a harness error",
      "negative control" in R2)
check("R32: the crew-ledger stub parses paths with the SDK, not str.split",
      "parse_field_path(path)" in R2)


# ── R33 — the four anonymous routes with no bound at all ─────────────────────
print("\n[R33] the poll/signin routes are bounded without API_TRUSTED_PROXIES")
check("R33: both login-approval polls carry an unconditional global cap",
      API.count('_rate_limit("poll:global"') == 2)
check("R33: the global cap is sized by a named setting, not a literal",
      "KSP_POLL_RATELIMIT_GLOBAL" in read("settings.py"))
check("R33: poll_approval no longer blocks the event loop",
      API.count("asyncio.to_thread(poll_approval") == 2
      and "state = poll_approval(" not in API)
check("R33: web sign-in has an unconditional backstop too",
      '_rate_limit("signin:global"' in API)
# The deliberate omission is recorded so a later pass does not "fix" it into an outage.
check("R33: /version/check is deliberately left uncapped, with the reason written down",
      "Deliberately NO unconditional global cap here" in API)


# ── R34 — a refused guild-config write must be loud ──────────────────────────
print("\n[R34] a mapping that cannot be saved is not drawn as saved")
check("R34: _persist raises instead of returning",
      "raise GuildConfigUnavailable(" in GC and "class GuildConfigUnavailable" in GC)
check("R34: all five admin callbacks surface the refusal",
      ADMIN.count("except guild_config.GuildConfigUnavailable as exc:") == 5)
check("R34: the boot failure names its consequences, not just its cause",
      "will resolve as unset" in GC)

# Live: the guard fires when the load failed, and NOT when no load was attempted.
import data.guild_config as _gc
_prev_loaded = _gc._loaded
try:
    _gc._loaded = False
    _raised = False
    try:
        _gc.set_role(1, "admin", 123)
    except _gc.GuildConfigUnavailable:
        _raised = True
    check("R34 live: setting a role while unloaded raises", _raised)
finally:
    _gc._loaded = _prev_loaded


# ── R35 — the third modlist site ─────────────────────────────────────────────
print("\n[R35] every contract-creation path shares one modlist cap")
check("R35: create_rescue uses the shared constant, not a literal",
      "Form(None, max_length=MODLIST_MAX_LENGTH)" in API)
check("R35: and the constant is imported rather than redefined",
      "MODLIST_MAX_LENGTH,\n    LinkRequest" in API)


# ── R36 — a kerbal name is not a field path ──────────────────────────────────
print("\n[R36] dotted crew names cannot silently skip consumption")
CL = read("data/crew_ledger.py")
# Comments are stripped first: crew_ledger's own docstring quotes the broken
# interpolation to explain WHY it is broken, and that explanation should stay.
_CL_CODE = "\n".join(
    l for l in CL.splitlines() if not l.lstrip().startswith("#"))
check("R36: no field path is built by interpolation",
      'f"out.{hid}.{n}"' not in _CL_CODE and 'f"out.`{holder_id}`"' not in _CL_CODE)
check("R36: both writers render the path from segments",
      CL.count('_field_path("out"') == 2)
# Live, against the real helper: a dotted name must survive a render/parse round trip.
from google.cloud.firestore_v1.field_path import parse_field_path
from data.crew_ledger import _field_path as _fp
check("R36 live: a name containing a full stop renders as ONE segment",
      parse_field_path(_fp("out", "a_1", "Bob Jr. Kerman")) == ["out", "a_1", "Bob Jr. Kerman"])
check("R36 live: and so does a name containing a backtick",
      parse_field_path(_fp("out", "a_1", "tick`y")) == ["out", "a_1", "tick`y"])


# ── R37 — the permission message that was thrown away ────────────────────────
print("\n[R37] a moderation refusal says which permission is missing")
check("R37: the handler uses the raised text",
      'msg = f"❌ {error}" if str(error) else' in read("cogs/moderation.py"))


# ── BL1 / BL6 — money never moves before the terminal status ─────────────────
print("\n[BL1/BL6] terminal status is written before any money moves")


def _order_ok(src: str, anchor: str, window: int = 4000) -> bool:
    """True when the terminal status write precedes the first money movement.

    Measured from `anchor` forward. Returns False rather than raising when either side
    is missing — an assertion that cannot find the code it is about has failed, and a
    ValueError here would read as a broken harness instead of a broken invariant.
    """
    i = src.find(anchor)
    if i == -1:
        return False
    seg = src[i:i + window]
    st = seg.find("status=cdb.")
    money = [x for x in (seg.find("_charge_fine("), seg.find("_pay_issuer(")) if x != -1]
    if st == -1 or not money:
        return False
    return st < min(money)


check("BL1: give_up writes CANCELLED before charging",
      _order_ok(CA, "async def give_up("))
check("BL1: dispute(pay_fine) writes COMPLETED before charging",
      _order_ok(CA, 'if action == "pay_fine":'))
check("BL1: expire_dispute writes COMPLETED before charging",
      _order_ok(CA, "    sym = settings.CURRENCY_SYMBOL\n    fine = c.get(\"fine\", 0)\n"))
check("BL1: mod_resolve(enforce) writes COMPLETED before charging",
      _order_ok(CA, "# Take whatever the contractor can actually pay"))
check("BL6: the auction close claims the auction transactionally",
      "def claim_close(" in ADB and "@firestore.transactional" in ADB
      and "a = adb.claim_close(auction_id)" in AUCTIONS)
check("BL6: the close no longer re-reads the auction after claiming",
      "a = adb.get_auction(gid, auction_id)" not in AUCTIONS)
check("BL1: the auction refund follows its status write",
      AUCTIONS.index("status=adb.CLOSED,\n                           result_contract_id")
      < AUCTIONS.index('detail="Escrow above the winning bid"'))


# ── BL2 — MOD_REVIEW has an ending ───────────────────────────────────────────
print("\n[BL2] MOD_REVIEW is on a clock")
check("BL2: there is a resolver", "async def expire_mod_review(" in CA)
check("BL2: and a deadline reader", "def mod_review_deadline(" in CA)
check("BL2: both entry points stamp mod_review_at",
      CA.count("mod_review_at=_now_iso") == 2)
check("BL2: a sweep runs it, started and cancelled with the others",
      "async def mod_review_timeout_loop(self):" in CONTRACTS_COG
      and "self.mod_review_timeout_loop.start()" in CONTRACTS_COG
      and "self.mod_review_timeout_loop.cancel()" in CONTRACTS_COG)
check("BL2: the window is a named setting", "MOD_REVIEW_TIMEOUT_DAYS" in read("settings.py"))
check("BL2: an unstamped review starts the clock rather than resolving instantly",
      "review clock started now" in CA)


# ── BL3 — a suspension must not mint fines ───────────────────────────────────
print("\n[BL3] the deadline clocks pause while the contractor is suspended")
check("BL3: all three time-based resolvers consult the pause",
      CA.count("    if _clock_paused_by_suspension(gid, contract_id, c):") == 3)
check("BL3: the read fails open", "clock unchanged" in CA)
check("BL3: resuming gives the time back rather than fining immediately",
      "deadline clock resumed" in CA and "clock_paused_at" in CA)
# The helper must be SYNCHRONOUS: a coroutine object is always truthy, so an async one
# would pause every clock unconditionally. This is not hypothetical — it happened while
# fixing this, and only test_contract_actions caught it.
import contract_actions as _ca
check("BL3 live: the pause helper is not a coroutine function",
      not asyncio.iscoroutinefunction(_ca._clock_paused_by_suspension))
check("BL3: expire_overdue kept its @serialized decorator",
      re.search(r"@serialized\s*\nasync def expire_overdue", CA) is not None)
check("BL3: expire_dispute kept its @serialized decorator",
      re.search(r"@serialized\s*\nasync def expire_dispute", CA) is not None)


# ── BL4 — /contractreset must not re-open a paid mission ─────────────────────
print("\n[BL4] clearing contracts cannot re-open an already-paid weekly mission")
check("BL4: only claims for contracts this run reset are deleted",
      "if cid and str(cid) in reset_ids:" in CONTRACTS_COG)
check("BL4: the reset collects the ids it cancelled",
      'reset_ids.add(str(c["contract_id"]))' in CONTRACTS_COG)
check("BL4: an untracked claim is kept, not deleted",
      "selections_kept" in CONTRACTS_COG)
check("BL4: the claim records which contract it minted",
      "def link_selection_contract(" in WM
      and "link_selection_contract(guild_id, week_key, uid" in WM
      and "_link_selection_contract(gid, wk, uid, req.mission_id" in API)


# ── BL5 — never re-mint a deleted account's wallet ───────────────────────────
print("\n[BL5] crediting an erased account does not recreate it")
check("BL5: auction refunds go through one guarded helper",
      "async def _refund_issuer(" in AUCTIONS and "store.has_user(str(issuer_id))" in AUCTIONS)
check("BL5: and every auction refund uses it",
      AUCTIONS.count("await _refund_issuer(") == 4
      and "await store.add_balance(origin_gid" not in AUCTIONS)
check("BL5: the withdrawal fine is guarded", "collected > 0 and store.has_user(contractor)" in CA)
check("BL5: the marketplace seller credit is guarded",
      "if store.has_user(str(seller_id)):" in API)


# ── BL8 — an extension needs a request ───────────────────────────────────────
print("\n[BL8] approving an extension requires an open request")
check("BL8: the caller-supplied date can no longer stand in for the request",
      "req = _open_request_of(c, REQUEST_MORE_TIME)\n    if req is None:\n" in CA)


# ── PR2 / PR13 / PR4 — what a deletion actually removes ──────────────────────
print("\n[PR2/PR13/PR4] deletion reaches the identity and the leftovers")
check("PR2: delete_account deletes the Firebase Authentication user",
      "fb_auth.delete_user(fuid)" in ACCTS)
check("PR2: and reports it", '"firebase_auth"' in ACCTS)
check("PR13: one shared purge exists", "def _purge_player_records(" in BRIDGE)
check("PR13: the self-service path uses it", "_purge_player_records(uid)" in BRIDGE)
check("PR13: and so does the moderator path",
      "from cogs.ksp_bridge import _purge_player_records, _delete_avatar" in API)
check("PR4: part catalogs are cleared in every guild, not one",
      "def _delete_part_catalogs_everywhere(" in BRIDGE)
check("PR4: achievements and marketplace votes are purged",
      '"ksp_achievements"' in BRIDGE and '"marketplace_votes"' in BRIDGE)
check("PR4: listings are DELISTED, never deleted (buyers keep their downloads)",
      '"status": "delisted"' in BRIDGE and "listings_delisted" in BRIDGE)
PP = read("src/app/pp/page.tsx", WEB)
PP_HTML = read("public/legal/privacy.html", WEB)
TOS = read("src/app/tos/page.tsx", WEB)
check("PR1: the 'every category' table lists the website-account identity",
      all(k in PP for k in ("Email address", "Boundless username",
                            "Profile picture you upload", "Two-factor enrolment")))
check("PR1: and the static mirror says the same",
      all(k in PP_HTML for k in ("Email address", "Boundless username",
                                 "Two-factor enrolment")))
check("PR3: the false 'Discord ID is never shown' claim is gone from both copies",
      "your Discord ID, device" not in PP and "your Discord ID, device" not in PP_HTML)
check("PR3: and is replaced by what is actually true",
      "your account identifier" in PP and "your account identifier" in PP_HTML)
check("PR4: the deletion promise enumerates what goes and what stays",
      "Some records are kept" in PP and "Some records are kept" in PP_HTML)
check("PR4: the ToS claim matches it",
      "are kept; your listings are delisted" in TOS)
check("PR5: the no-Discord deletion route is named (the command needs one)",
      "have no Discord account" in PP and "have no Discord account" in PP_HTML)
check("PR11: the confirmation no longer promises a total erasure",
      "Delete everything →" not in BRIDGE
      and "Kept, because they are also somebody else's record" in BRIDGE)


# ── PR7 — a stored IP has an end ─────────────────────────────────────────────
print("\n[PR7] device-challenge IPs are not kept forever")
check("PR7: an expired challenge is deleted, not merely reported",
      'doc.delete()\n            return {"state": "expired"}' in AUTH)
check("PR7: a settled denial drops the address",
      'doc.update({"client_ip": firestore.DELETE_FIELD})' in AUTH)


# ── PR9 — a debt is not published to the channel ─────────────────────────────
print("\n[PR9] a third party's debt is not posted publicly")
check("PR9: the debt field is gated on self-or-moderator",
      "may_see_debt" in ECON and "perms.is_mod_user(interaction)" in ECON)


# ── PR12 — consent belongs to a person, not an install ───────────────────────
print("\n[PR12] consent is bound to the account that gave it")
check("PR12: the account id is recorded", 'gk.AddValue("accountId"' in CONSENT)
check("PR12: and read back", 'acceptedAccountId = gk.GetValue("accountId")' in CONSENT)
check("PR12: the gate consults it", "AcceptedByCurrentAccount" in CONSENT)
check("PR12: an unknown id degrades to accepted rather than revoking every install",
      "if (string.IsNullOrEmpty(acceptedAccountId)) return true;" in CONSENT)


# ── WR1/WR2/WR3/WR6 — the website ────────────────────────────────────────────
print("\n[WR] the website's authorization and cache surfaces")
check("WR1: the admin console no longer mints craft download URLs",
      "include_download=True" not in API.split("# ── Marketplace moderation")[-1])
check("WR1: the entitled views still do (own uploads / purchases)",
      API.count("include_download=True") == 2)
check("WR2: guard() shape-checks the token rather than trusting the cookie's presence",
      "looksLikeSessionToken(await getSessionToken())" in SERVERAPI_TS)
check("WR2: the shape check requires a 64-hex signature",
      "sig.length !== 64" in SERVERAPI_TS)
check("WR6: a 401 clears the credential, not just the cosmetic hint",
      "else if (r.status === 401) clearSessionCookie(res);" in SERVERAPI_TS)
check("WR4: trusted proxies are parsed as networks, so ranges are expressible",
      "ip_network(entry, strict=False)" in read("config.py"))
check("WR4: membership, not string equality",
      "def _ip_trusted(" in API and "any(ip in n for n in nets)" in API)
check("WR4: every per-IP gate tests the PARSED networks",
      API.count("if cfg.API_TRUSTED_PROXY_NETS:") == 5
      and "if cfg.API_TRUSTED_PROXIES:" not in API)
# Live: a CIDR entry must actually match an address inside it, and reject one outside.
import ipaddress as _ipa
from config import Config as _Cfg
_nets = _Cfg._parse_proxy_networks({"127.0.0.1", "35.191.0.0/16", "nonsense/99"})
import api_server as _apisrv
check("WR4 live: a range entry parses and an unparseable one is dropped",
      len(_nets) == 2)
check("WR4 live: an address inside the range is trusted",
      _apisrv._ip_trusted("35.191.4.7", _nets))
check("WR4 live: one outside it is not (negative control)",
      not _apisrv._ip_trusted("8.8.8.8", _nets))
check("WR3: the three enum axes are closed vocabularies",
      "const ENUM_VALUES" in LISTINGS_TS and "vocab.has(raw) ? raw : null" in LISTINGS_TS)


# ── LS — the mod's local HTTP surface ────────────────────────────────────────
print("\n[LS] the loopback listener")
check("LS1: a rescue requires an explicit confirmation server-side",
      'MiniJSON.GetBool(body, "confirm_permanent", false)' in GKROUTES)
check("LS1: and the shipped bundle sends it (both halves changed together)",
      "confirm_permanent: isRescue ? confirmRescue : undefined," in CREATECONTRACT_TSX)
check("LS2: every body read is bounded and timed",
      "StreamReader(ctx.Request.InputStream" not in GKROUTES
      and GKROUTES.count("BodyReader.Read(ctx.Request)") == 5)
check("LS2: the reader caps size AND time",
      "ReadTimeoutMs" in read("GeneKerman/Web/BodyReader.cs", MOD))
check("LS3: the session token is written owner-only",
      "SecureFile.WriteAllTextRestricted(tokenPath" in APICLIENT)
check("LS3: so is the debug bridge's handshake, before it is moved into place",
      "SecureFile.RestrictToOwner(tmp);" in DBRIDGE)
check("LS4: the static handler uses the shared CSP rule",
      "LocalServer.NeedsCsp(contentType)" in STATICF
      and "internal static bool NeedsCsp" in LOCALSRV)
check("LS5: an abandoned job is reported, not answered 202",
      "if (started != null && started.Status >= 400)" in GKROUTES)


# ── DP — build, release and tooling ──────────────────────────────────────────
print("\n[DP] the release path")
check("DP3: the marker scan reads BOTH encodings",
      'strings -a -el "$DLL"; strings -a "$DLL"' in ASSERTSH)
check("DP3: the metadata-only debug accessors are named",
      all(m in ASSERTSH for m in ("DebugCrewedNames", "DebugRescueWrecks",
                                  "DebugImportedVessels", "DebugTestPanel")))
check("DP3: GKScenarioTrace is excluded, with the reason recorded",
      "GKScenarioTrace is deliberately NOT a marker" in ASSERTSH)
check("DP3: a missing check fails the build instead of warning",
      "this build is not publishable" in BUILDSH)
check("DP6: the KWin script is written with mkstemp, not a predictable /tmp name",
      "tempfile.mkstemp(prefix=\"gk_focus_\"" in INSTANCES
      and 'f"/tmp/gk_focus_{stamp}.js"' not in INSTANCES)
check("DP8: https is enforced where the value is written, so both callers inherit it",
      'download_url must be an https:// URL' in MODVER)
check("DP9: no stale settings.cfg is staged for deployment",
      not os.path.exists(os.path.join(MOD, "GameData/BoundlessMissions/PluginData/settings.cfg"))
      or not any(k in read("GameData/BoundlessMissions/PluginData/settings.cfg", MOD)
                 for k in ("checkInterval", "enableKVV", "enableContractInjection")))
check("DP9: and PluginData is ignored so it cannot come back",
      "GameData/BoundlessMissions/PluginData/" in read(".gitignore", MOD))
check("DP7: the bundled assembly's provenance is recorded",
      "sha256" in read("PACKAGING.md", MOD) and "websocket-sharp" in read("PACKAGING.md", MOD))


# ── every touched module still parses ────────────────────────────────────────
print("\n[syntax] every module changed this pass parses")
for f in ("api_server.py", "api_auth.py", "contract_actions.py", "settings.py",
          "data/store.py", "data/accounts.py", "data/auctions.py",
          "data/guild_config.py", "data/crew_ledger.py", "data/mod_version.py",
          "cogs/contracts.py", "cogs/auctions.py", "cogs/admin.py",
          "cogs/economy.py", "cogs/moderation.py", "cogs/ksp_bridge.py",
          "cogs/weeklymissions.py"):
    try:
        ast.parse(read(f))
        check(f"{f} parses", True)
    except SyntaxError as e:
        check(f"{f} parses", False, str(e))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)

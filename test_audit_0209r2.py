"""
test_audit_0209r2.py — regression locks for the 2026-09-02 pass-2 audit
(`0209_security_audit_pass2.md`).

One check per finding id, so a failure says WHICH audit item regressed rather than
which line moved. Standalone `__main__`, not pytest (these files `sys.exit`).

Mostly source-shaped assertions, like its predecessors: the behaviours here live in
Discord callbacks, Unity code and Firestore transactions that the offline harness
cannot drive. Where a behaviour IS reachable offline (the wallet write gate, the
crew-ledger single use, the listings key canonicaliser) it is exercised for real.
"""

import ast
import os
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


SRC = {p: read(p) for p in [
    "api_server.py", "api_auth.py", "api_models.py", "cost_guard.py",
    "data/store.py", "data/guild_config.py", "data/crew_ledger.py",
    "data/accounts.py", "data/gcp_metrics.py", "data/contracts.py",
    "cogs/economy.py", "cogs/moderation.py", "cogs/auctions.py", "cogs/perms.py",
    "cogs/contract_views.py", "cogs/weeklymissions.py", "cogs/corps.py",
    "cogs/roles.py", "cogs/gkchannels.py", "cogs/xp.py", "cogs/contracts.py",
]}
API = SRC["api_server.py"]

print("\n[RW1/WS1] the craft download is reachable by a navigation")
DL = read("src/app/api/marketplace/download/route.ts", WEB)
check("RW1: the download route does not demand an App Check header",
      "await guard(req)" not in DL)
check("RW1: it requires a session instead, which a navigation can carry",
      "await getSessionToken()" in DL)
check("RW1: the security suite no longer asserts the defect",
      "requires App Check" not in read("test_website_security.mjs", WEB))

print("\n[WS2] the Firebase rules are actually deployed")
DEP = read("deploy.sh", WEB)
check("WS2: deploy.sh ships firestore + storage rules",
      "--only firestore:rules,storage" in DEP)
check("WS2: ...before the hosting deploy, so a failure stops the release",
      DEP.index("firebase deploy --only firestore:rules,storage")
      < DEP.index("firebase deploy --only hosting"))

print("\n[WS3] guard() checks the session, not only App Check")
SA = read("src/lib/server-api.ts", WEB)
check("WS3: guard requires a session by default", "opts.session !== false" in SA)
check("WS3: only the four session-MINTING routes opt out",
      sum(read(f"src/app/api/auth/{r}/route.ts", WEB).count("{ session: false }")
          for r in ("signin", "totp", "link", "link/poll")) == 4)

print("\n[WS4] the listings cache key is bounded in COUNT, not only size")
LS = read("src/app/api/marketplace/listings/route.ts", WEB)
check("WS4: numeric filters are bucketed", "NUMERIC_BUCKET" in LS)
check("WS4: page is clamped", "MAX_PAGE" in LS)
check("WS4: free text is normalised", "MAX_QUERY_LENGTH" in LS)

print("\n[RB1] a budget freeze cannot silently discard wallet writes")
ST = SRC["data/store.py"]
check("RB1: a refused write raises rather than being dropped",
      "class WalletUnavailable" in ST and "_require_writable" in ST)
check("RB1: every balance mutator is gated",
      ST.count("self._require_writable()") >= 5)
check("RB1: the wallet reloads once the freeze clears",
      "async def ensure_loaded" in ST and "await store.ensure_loaded()" in SRC["cogs/xp.py"])
check("RB1: the state is visible to an operator",
      "wallet_budget_blocked" in API and "wallet_warning" in read("cogs/admin.py"))
check("RB1: a refused write answers 503, not 500",
      "WalletUnavailable" in API and '"wallet_unavailable"' in API)

print("\n[RB3] a freeze does not lock the console that lifts it")
check("RB3: the auth read is exempt from the brake",
      "_sessions_col_unguarded" in SRC["api_auth.py"]
      and "_db_unguarded" in SRC["data/store.py"])
check("RB3: ...and is still metered by hand",
      "note_firestore(reads=1)" in SRC["api_auth.py"])

print("\n[RB2] the at-rest clamp needs a real reading")
CG = SRC["cost_guard.py"]
check("RB2: the clamp consults `present`", '"stored_bytes" not in getattr(snap, "present"' in CG)
check("RB2: the authoritative figure is only adopted from a real reading",
      '"stored_bytes" in getattr(snap, "present"' in CG)
check("RB2: a gauge with no datapoint is not marked present",
      "snap.present.add(key)" in SRC["data/gcp_metrics.py"].split("if kind == \"gauge\"")[1].split("continue")[0])

print("\n[RB4/RB5/RB6/RB7] the smaller bot fixes")
check("RB4: escrow queries the issuer side only", 'roles=("issuer_id",)' in API)
check("RB4: iter_user_contracts takes a roles argument", "roles: tuple[str, ...]" in SRC["data/contracts.py"])
check("RB5: the per-channel inspection fails closed", "unreadable += 1" in SRC["cogs/perms.py"])
check("RB6: the modlist bound is shared and measured",
      "MODLIST_MAX_LENGTH = 8000" in SRC["api_models.py"])
check("RB7: the weekly post is inside the rollback",
      "cdb.update_contract(guild_id, c[\"contract_id\"], status=cdb.CANCELLED)"
      in SRC["cogs/weeklymissions.py"])

print("\n[CS1] link codes cannot collide silently")
AA = SRC["api_auth.py"]
check("CS1: codes are claimed with create(), not set()",
      "_claim_unused_code" in AA and ".document(code).set({" not in AA)
check("CS1: a collision is retried", "AlreadyExists" in AA)
check("CS1: the account link challenge too", ".create({" in SRC["data/accounts.py"])

print("\n[CS2] clearing a mapping actually persists")
GC = SRC["data/guild_config.py"]
check("CS2: a removal is an explicit DELETE_FIELD", "firestore.DELETE_FIELD" in GC)
check("CS2: _persist refuses before the boot read completes", "if not _loaded:" in GC)

print("\n[CS3/CS4/CS5] fail-direction and bucket fixes")
check("CS3: the version gate really fails open",
      "Mod version gate: could not read the published config" in API)
check("CS4: every per-IP bucket goes through the gated helper",
      API.count('_client_ip(request)}"') == 1
      and API.index('_client_ip(request)}"') > API.index("def _rate_limit_ip("))
check("CS4: the link lockout is gated too",
      "if cfg.API_TRUSTED_PROXY_NETS:\n        if _link_locked_out(ip):" in API)
check("CS5: a raising purchase claim refunds the buyer",
      "Refund: the purchase could not be completed" in API)

print("\n[CS6] the crew ledger is bounded")
CL = SRC["data/crew_ledger.py"]
check("CS6: recorded crew names are capped", "MAX_RECORDED_CREW" in CL)

print("\n[DC1] moderation actions need the permission they perform")
MOD_SRC = SRC["cogs/moderation.py"]
check("DC1: the gate takes the required permission", 'def mod_only(*, needs' in MOD_SRC)
check("DC1: ban/unban need ban_members", MOD_SRC.count('needs="ban_members"') == 2)
check("DC1: mute/unmute/warn need moderate_members",
      MOD_SRC.count('needs="moderate_members"') == 3)
check("DC1: purge needs manage_messages", 'needs="manage_messages"' in MOD_SRC)
check("DC1: role hierarchy is enforced", "_outranks" in MOD_SRC)
check("DC1: public confirmations cannot ping", MOD_SRC.count("allowed_mentions=NO_MENTIONS") >= 4)

print("\n[DC2] the auction card escapes player markdown")
AU = SRC["cogs/auctions.py"]
check("DC2: mission/issuer/bidder are all escaped", AU.count("_esc(") >= 4)

print("\n[DC3] guild-local authority does not write global records unchecked")
check("DC3: the shared check exists", "def moderatable_here" in SRC["cogs/perms.py"])
check("DC3: applied to every writing mod tool",
      SRC["cogs/economy.py"].count("_no = perms.moderatable_here(") == 3
      and "perms.moderatable_here" in SRC["cogs/xp.py"]
      and "perms.moderatable_here" in SRC["cogs/contracts.py"])

print("\n[DC4/DC5] money and buttons key on account ids")
EC = SRC["cogs/economy.py"]
check("DC4: /pay resolves both ends", "sender_acct = accounts.account_for_discord" in EC
      and "target_acct = accounts.account_for_discord" in EC)
check("DC4: a failed lookup refuses rather than paying the snowflake",
      "eco.pay.lookup_failed" in EC)
check("DC4: the debit and credit use the resolved ids",
      "store.try_debit(gid, sender_acct" in EC and "gid, target_acct, amount" in EC)
check("DC5: contract buttons resolve the actor",
      "accounts.account_for_discord(interaction.user.id)" in SRC["cogs/contract_views.py"])
check("DC5: auction self-bid guards resolve too",
      SRC["cogs/auctions.py"].count("accounts.account_for_discord") >= 3)

print("\n[DC6/DC7/DC8/DC9/DC10] the remaining Discord surface")
WM = SRC["cogs/weeklymissions.py"]
check("DC6: the custom mission caps its fine", "_fine_too_large(coins, fine)" in WM)
check("DC6: and bounds every numeric field", WM.count("app_commands.Range[int") >= 5)
check("DC7: corpsetup bounds the name", "app_commands.Range[str, 1, 32]" in SRC["cogs/corps.py"])
check("DC7: ...is rate limited", "app_commands.checks.cooldown" in SRC["cogs/corps.py"])
check("DC7: ...and answers a deferred interaction", "interaction.followup.send" in SRC["cogs/corps.py"])
check("DC8: the wrong-channel DM has an allowance",
      "_allow_wrong_channel_dm" in SRC["cogs/gkchannels.py"])
check("DC9: a worn role only unlocks a level actually earned",
      "earned_level" in SRC["cogs/roles.py"])
check("DC10: corp delivery pings only the recipient",
      "AllowedMentions(everyone=False, roles=False" in read("contract_actions.py"))

print("\n[MB] the KSP mod")
VT = read("GeneKerman/VesselTransfer.cs", MOD)
CI = read("GeneKerman/CraftInstaller.cs", MOD)
check("MB1: a vessel's PART count is bounded", "MaxPartsPerVessel" in VT)
check("MB1: ...checked before any side effect",
      VT.index("PartCountIsSane(innerNode") < VT.index("InstallEmbeddedCraft(innerNode)"))
check("MB1: the blueprint path is bounded too", "MaxPartsPerVessel" in CI)
check("MB2: GKMODS entries are bounded", "MaxCarriedMods" in read("GeneKerman/CkanGenerator.cs", MOD))
check("MB2: notification bodies are clamped at the funnel",
      "MaxNotifLength" in read("GeneKerman/TextSanitizer.cs", MOD))
check("MB3: the crew attestation is single use", "def consume_homebound" in CL)
check("MB3: ...and a declined return restores it", "def restore_homebound" in CL)
check("MB3: restore strips the tag, or record_handover drops it as borrowed",
      "bare = [strip_tag(n)" in CL)
check("MB3: consumed on send, restored on decline",
      "crew_ledger.consume_homebound" in API and "crew_ledger.restore_homebound" in API)
AS = read("tools/assert_production_clean.sh", MOD)
check("MB4: DebugTestPanel contributes markers", "GK_EVIL" in AS)
check("MB4: the full two-half check is reachable from build.sh",
      "GK_FULL_VERIFY" in read("build.sh", MOD))
SP = read("GeneKerman/SurfacePlacement.cs", MOD)
check("MB5: agl is checked for infinity, not only NaN", "static bool ParseNum" in SP)
check("MB5: the splashed branch honours MaxTrustedAgl",
      "useAgl = (haveAgl && agl <= MaxTrustedAgl)" in SP)
check("RW3: placement parsing is invariant-culture", "CultureInfo.InvariantCulture" in SP)
check("MB6: local-notification actions are local only",
      'GetString(n, "type") != "local"' in read("GeneKerman/LocalNotifActions.cs", MOD))
check("RW2: the refusal is cleared before the scene bail",
      VT.index("BeginImport();\n            if (!CanImport()) return 0;") > 0)

print("\n[live] behaviours exercised for real, not grepped")
from google.cloud.firestore_v1.field_path import parse_field_path
import data.crew_ledger as _cl
_store = {}
class _S:
    def __init__(s, p): s._p = p
    @property
    def exists(s): return s._p in _store
    def to_dict(s): return _store.get(s._p)
class _D:
    def __init__(s, p): s._p = p
    def get(s): return _S(s._p)
    def set(s, payload, merge=False):
        cur = _store.setdefault(s._p, {})
        if not merge: cur.clear()
        for k, v in payload.items():
            if isinstance(v, dict):
                node = cur.setdefault(k, {})
                for kk, vv in v.items():
                    if isinstance(vv, dict): node.setdefault(kk, {}).update(vv)
                    else: node[kk] = vv
            else: cur[k] = v
    def update(s, fields):
        cur = _store.setdefault(s._p, {})
        for path in fields:
            # Split with the SDK's own parser, not str.split("."). The first version
            # of this stub used split("."), which is a hand-written model of Firestore
            # semantics that disagrees with Firestore: it silently "worked" on a path
            # built by f-string interpolation that the real client would have split
            # into four segments. A stub that models the thing under test passes
            # exactly when the real code fails (R36), so borrow the real parser.
            parts = list(parse_field_path(path)); node = cur
            for seg in parts[:-1]: node = node.setdefault(seg, {})
            node.pop(parts[-1], None)
_cl._col = lambda: type("C", (), {"document": staticmethod(lambda i: _D(f"c/{i}"))})()
_cl.record_handover("V", "A", ["Jebediah Kerman"])
_att = _cl.homebound_for("V", "A", ["A's Jebediah Kerman"])
check("MB3 live: an honest return is attested", _att == ["A's Jebediah Kerman"], _att)
_cl.consume_homebound("V", "A", _att)
check("MB3 live: the attestation cannot be replayed",
      _cl.homebound_for("V", "A", ["A's Jebediah Kerman"]) == [])
_cl.restore_homebound("V", "A", _att)
check("MB3 live: a declined return restores it",
      _cl.homebound_for("V", "A", ["A's Jebediah Kerman"]) == ["A's Jebediah Kerman"])

# R36: a kerbal name containing a full stop. A field path is dotted, so building one
# by interpolation splits this name into two segments and consumes NOTHING — leaving
# the attestation replayable, which is the hazard single-use consumption exists to
# remove. Exercised end to end rather than grepped for the helper's name.
_cl.record_handover("V", "A", ["Bob Jr. Kerman"])
_dot = _cl.homebound_for("V", "A", ["A's Bob Jr. Kerman"])
check("R36 live: a dotted crew name is attested", _dot == ["A's Bob Jr. Kerman"], _dot)
_cl.consume_homebound("V", "A", _dot)
check("R36 live: a dotted crew name is actually consumed, not silently skipped",
      _cl.homebound_for("V", "A", ["A's Bob Jr. Kerman"]) == [])

import asyncio
from data.store import store as _st, WalletUnavailable as _WU

# Driven with asyncio.run(), and catching ONLY WalletUnavailable, because the first
# version of this check did neither and was therefore vacuous: it called
# asyncio.get_event_loop(), which on Python 3.12+ raises RuntimeError when no loop is
# running — before the coroutine is ever scheduled — and a bare `except RuntimeError`
# arm recorded that as the fix working. It passed identically with the whole RB1 fix
# reverted. A harness error must fail the check, not satisfy it.
def _try_write():
    """True when the wallet REFUSED the write, False when it accepted it."""
    try:
        asyncio.run(_st.add_balance(0, 999999, 5))
        return False
    except _WU:
        return True

_prev = (_st._load_attempted, _st._loaded, _st._budget_blocked)

# Positive: a load was attempted and did not finish -> the write must be refused.
_st._load_attempted, _st._loaded, _st._budget_blocked = True, False, True
check("RB1 live: a wallet write is refused while unloaded", _try_write())

# Negative control. Without this the check above cannot tell "the guard fired" from
# "something else raised" or "nothing ran". `not _loaded` is ALSO true before any load
# has been tried — the state every offline suite here runs in — and gating on that was
# the first fix's actual bug. So assert the same call SUCCEEDS in that state.
_st._load_attempted, _st._loaded, _st._budget_blocked = False, False, False
check("RB1 live: a never-attempted load still accepts writes (negative control)",
      _try_write() is False)

_st._load_attempted, _st._loaded, _st._budget_blocked = _prev
_st._load_attempted = False   # restore the offline default for any later import

for f in ("api_server.py", "api_auth.py", "data/store.py", "cogs/moderation.py",
          "cogs/economy.py", "cogs/auctions.py", "data/guild_config.py"):
    try:
        ast.parse(SRC.get(f) or read(f))
        check(f"{f} parses", True)
    except SyntaxError as e:
        check(f"{f} parses", False, str(e))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)

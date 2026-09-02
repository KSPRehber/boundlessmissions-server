"""
Regression checks for the 2026-08-31 multi-agent audit (3108_security_audit.md).

Pure-function and source-guard checks only — no Firestore, no Discord, no network.
Run with:
    python test_audit_3108.py

Source-guard checks are deliberate here, as in test_audit_3008.py: most of these
findings are "this call site is missing a limit", which has no runtime surface to
assert against without a live Firestore. A guard that pins the call site is what
stops the fix being quietly removed later.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DISCORD_TOKEN", "x")

import api_server
import api_auth
import settings
import data.contracts as cdb

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = {name: open(os.path.join(HERE, name)).read() for name in (
    "api_server.py", "api_auth.py", "api_models.py", "bot.py", "contract_actions.py",
    "settings.py", "data/contracts.py", "data/marketplace.py", "data/accounts.py",
    "data/twofa.py", "data/auctions.py", "cogs/tickets.py", "cogs/weeklymissions.py",
    "cogs/contracts.py", "cogs/ksp_bridge.py", "cogs/contract_views.py", "data/imports.py",
)}

passed = failed = 0


def check(label, cond):
    global passed, failed
    passed += bool(cond)
    failed += (not cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")


print("\n[UP1] request bodies are size-capped by content type, before auth")
s = SRC["api_server.py"]
check("a JSON-specific ceiling exists and is far below the multipart one",
      api_server.MAX_JSON_BYTES < api_server.MAX_REQUEST_BYTES)
check("_body_limit_for gives multipart the large cap",
      api_server._body_limit_for("multipart/form-data; boundary=x") == api_server.MAX_REQUEST_BYTES)
check("_body_limit_for gives JSON the small cap",
      api_server._body_limit_for("application/json") == api_server.MAX_JSON_BYTES)
check("an absent/unknown content type gets the small cap, not the large one",
      api_server._body_limit_for("") == api_server.MAX_JSON_BYTES)
check("the pre-auth token check is no longer bugreport-only",
      'request.url.path == "/api/v1/bugreport"' not in s
      and "_PUBLIC_BODY_PATHS" in s)
check("the chunked-body middleware uses the same per-type ceiling",
      "min(self.limit, _body_limit_for(ctype))" in s)
check("public auth endpoints are exempt from the pre-auth check",
      "/api/v1/web/auth/signin" in s and "_PUBLIC_BODY_PATHS = frozenset" in s)

print("\n[UP2/EC1/MK3] contract creation is rate-limited and its AI call is charged")
check("contract creation has its own hourly limit",
      'f"ctcreate:{uid}"' in s and hasattr(settings, "CONTRACT_CREATE_PER_HOUR"))
check("_classify_single_contract charges the per-user Gemini allowance",
      "_classify_single_contract" in s
      and s.split("async def _classify_single_contract", 1)[1].split("\ndef ", 1)[0]
           .count('f"gemini:{uid}"') == 1)
check("the classification falls back rather than 500s when the allowance is spent",
      "AI allowance spent" in s)
check("both classify call sites pass the acting account",
      s.count("_classify_single_contract(gid,") >= 2
      and s.count("uid=uid)") + s.count("uid=str(user[\"user_id\"]))") >= 2)
check("the blocking Gemini SDK call is moved off the event loop",
      "await asyncio.to_thread(\n                lambda: gemini_client.models.generate_content(" in s)
check("the classification prompt fences its client text",
      "untrusted data written by a player" in s)

print("\n[UP3] contract reads are bounded")
check("count_active filters status in the query rather than in Python",
      '.where("status", "in", statuses)' in SRC["data/contracts.py"])
check("count_active falls back rather than failing a contract action",
      "fell back to a full history read" in SRC["data/contracts.py"])
check("the contract list endpoint is rate-limited",
      'f"ctactive:{uid}"' in s and hasattr(settings, "CONTRACT_LIST_PER_HOUR"))

print("\n[EC3] a PENDING offer does not fill the recipient's active-contract cap")
c = SRC["data/contracts.py"]
check("the two sides are queried with different status sets",
      '"issuer_id": list(ACTIVE_STATUSES)' in c
      and '"contractor_id": [s for s in ACTIVE_STATUSES if s != PENDING]' in c)
check("the Python fallback applies the same rule",
      'c.get("status") == PENDING' in c and 'str(c.get("contractor_id")) == uid' in c)
check("PENDING is still counted for the issuer, whose escrow is locked",
      cdb.PENDING in cdb.ACTIVE_STATUSES)

print("\n[MK1] every door into the ticket category shares one guild breaker")
check("a shared ticket-open budget exists", "def _limit_ticket_open(" in s)
check("bug reports go through it", 'bucket="bugreport"' in s)
check("website tickets go through it", 'bucket="ticketopen"' in s)
check("moderation reports go through it", 'bucket="report"' in s)
check("the per-guild breaker lives in create_ticket, so no door can skip it",
      "_allow_guild_opening" in SRC["cogs/tickets.py"]
      and "TICKET_GUILD_PER_HOUR" in SRC["cogs/tickets.py"]
      and 'f"ticket_guild:{gid}"' not in s)
# The per-address bucket now goes through `_rate_limit_ip`, which is where the
# API_TRUSTED_PROXIES gate lives — stronger than the old inline form, since a new
# limiter cannot forget the gate by construction.
check("the per-address bucket is in the shared helper",
      '_rate_limit_ip("ticket_ip"' in s and "def _rate_limit_ip(" in s)
check("no endpoint keeps a private per-user-only ticket limit",
      'f"bugreport:{uid}", max_hits=3, window=3600.0)' not in s
      and 'f"ticketopen:{ctx[\'account_id\']}", max_hits=5' not in s)
t = SRC["cogs/tickets.py"]
check("create_ticket refuses before the category is full",
      "TICKET_CATEGORY_SOFT_MAX" in t and "category.channels" in t)
check("the soft cap leaves room under Discord's 50-per-category limit",
      __import__("cogs.tickets", fromlist=["x"]).TICKET_CATEGORY_SOFT_MAX < 50)

print("\n[EC2] the weekly-mission claim is account-scoped, like the wallet")
w = SRC["cogs/weeklymissions.py"]
check("the claim is a top-level collection, not per guild",
      '_db.collection("weekly_selections").document(doc_id)' in w
      and 'collection("weekly_selections")' in w
      and '.document(str(guild_id)).collection("weekly_selections")' not in w)
check("the guild is kept as a field", '"guild_id": str(guild_id),' in w)
check("contractreset reads the same top-level collection",
      '_db.collection("weekly_selections")' in SRC["cogs/contracts.py"]
      and 'document(str(gid)).collection("weekly_selections")' not in SRC["cogs/contracts.py"])

print("\n[UP4] public marketplace images are sanitised")
m = SRC["data/marketplace.py"]
check("marketplace imports safe_content_type", "safe_content_type" in m)
check("the blueprint upload sanitises its content type",
      m.count("content_type=safe_content_type(content_type)") >= 2)
check("no public marketplace upload passes the raw content type",
      "blob.upload_from_string(data, content_type=content_type)" not in m)
check("both images must decode before they are stored",
      "_looks_like_image(bp_data)" in s and "_looks_like_image(thumb_data)" in s)
check("they are read with the blueprint ceiling, not the generic upload cap",
      "_read_upload(blueprint, MAX_BLUEPRINT_BYTES)" in s)

print("\n[UP5] the part catalog is bounded in size and in memory")
check("the model bounds the entry count as a defence, not a behaviour",
      "max_length=30000" in SRC["api_models.py"])
check("the working cap stays a truncation in the handler, not a 422",
      "][:8000]" in SRC["api_server.py"])
check("the model caps the hash", 'hash: str = Field(max_length=128)' in SRC["api_models.py"])
check("each name/title is truncated", "_PART_FIELD_MAX" in s)
check("the in-memory cache is evicted", "_evict_part_catalogs" in s
      and "_PART_CATALOGS_MAX_BYTES" in s)
check("it is bounded by bytes, not by a count of unevenly-sized catalogs",
      "_catalogs_bytes()" in s)


def _fake_catalogs(n, entries):
    """n catalogs of `entries` fat rows each — the shape a count cap mis-measures."""
    api_server._PART_CATALOGS.clear()
    row = {"name": "x" * 128, "title": "y" * 128}
    for i in range(n):
        api_server._PART_CATALOGS[f"k{i}"] = {"hash": "h", "parts": [dict(row)] * entries}


# Well past the byte budget, but only 40 catalogs — a count cap of 500 would not
# have evicted a single one of these.
_fake_catalogs(40, 8000)
over = api_server._catalogs_bytes()
api_server._evict_part_catalogs()
check("eviction bounds the cache by size",
      api_server._catalogs_bytes() <= api_server._PART_CATALOGS_MAX_BYTES
      or len(api_server._PART_CATALOGS) <= api_server._PART_CATALOGS_MIN)
check("the pre-eviction load really was over budget",
      over > api_server._PART_CATALOGS_MAX_BYTES)
check("a few catalogs are always kept, however fat",
      len(api_server._PART_CATALOGS) >= 1)
api_server._PART_CATALOGS.clear()

print("\n[UP6] signed-URL minting is rate-limited")
check("the import queue is limited", 'f"pendingimport:{user[\'user_id\']}"' in s)
check("the gift queue is limited", 'f"pendinggift:{user[\'user_id\']}"' in s)
check("the two use separate buckets, so a client polling both cannot 429 itself",
      'f"pendingimport:' in s and 'f"pendinggift:' in s)

print("\n[UP7/EC8] every client-text AI call site is fenced and clamped")
check("the part resolver fences its inputs",
      '_client_text_block("mission", mission_text)' in s
      and '_client_text_block("installed_candidates", listing)' in s)
check("the resolver's answer must be one of the candidates offered",
      "if ans not in allowed:" in s)

print("\n[UP8/EC5] a lost submission claim leaves nothing behind")
check("every stored file is deleted, not only the non-images",
      'if not f["content_type"].startswith("image/")' not in s)
check("public objects are deleted by path, which delete_stored_file accepts",
      'target = f.get("path") or f["url"]' in s)
check("upload_submission_file can hand its path back",
      "path_out" in SRC["data/contracts.py"])
check("telemetry diagrams are tracked for cleanup", "telemetry_paths" in s)

print("\n[MK2] rankings require participation, and the XP door is not free")
check("a ranking score distinct from the raw net score exists", "_ranked_score" in s)
check("both sorts use it", s.count("_ranked_score(l)") >= 3)
check("the recommended rate uses it", "_ranked_score(l) / (_listing_age_days" in s)
check("ranking damps continuously rather than gating at a threshold",
      getattr(settings, "MARKETPLACE_RANK_CONFIDENCE", 0) > 0
      and not hasattr(settings, "MARKETPLACE_RANK_MIN_VOTES"))
check("the XP shortcut needs real play, not a single message",
      getattr(settings, "MARKETPLACE_VOTE_MIN_XP", 0) > 1
      and 'int(u.get("xp", 0) or 0) >= max(1, min_xp)' in s)
check("an unvoted listing ranks as unrated", api_server._ranked_score({"likes": 0, "dislikes": 0}) == 0)
check("a lightly-voted listing is damped below face value",
      0 <= api_server._ranked_score({"likes": 3, "dislikes": 0}) < 3)
check("a well-voted listing approaches face value",
      api_server._ranked_score({"likes": 40, "dislikes": 0}) > 30)
check("ranking is monotonic in the net score",
      api_server._ranked_score({"likes": 20, "dislikes": 0})
      > api_server._ranked_score({"likes": 8, "dislikes": 0}))
check("the sorts never go blank — a real vote always outranks none",
      api_server._ranked_score({"likes": 2, "dislikes": 0})
      >= api_server._ranked_score({"likes": 0, "dislikes": 0}))

print("\n[MK4/RV14] friend requests are bounded per PAIR, not per recipient")
check("a per-pair bucket exists", 'f"friendreq:{uid}:{target}"' in s)
check("no per-recipient bucket, which anyone could spend on a victim",
      'friendreq_to:' not in s)
check("it is applied after the target is resolved",
      s.index("target, _typed = await _resolve_friend_target(req)")
      < s.index('f"friendreq:{uid}:{target}"'))

print("\n[MK5] user strings are escaped in moderator ticket embeds")
check("the contract report embed escapes its names",
      "_esc(str(contract.get('issuer_name'" in s
      and "_esc(str(contract.get('contractor_name'" in s)
check("the marketplace report embed escapes craft and seller names",
      "_esc(str(listing.get('craft_name'" in s
      and "_esc(str(listing.get('seller_name'" in s)
check("both free-text reasons are escaped", s.count("{_esc(reason)}") == 2)
check("the mission text is escaped", "mission = _esc(mission)" in s)

print("\n[MK6] compatibility respects listing visibility")
check("a non-active listing is 404 for anyone but the seller and its buyers",
      'if str(listing.get("status")) != mkt.ACTIVE:' in s
      and 'uid != str(listing.get("seller_id")) and uid not in buyers' in s)

print("\n[EC4] a settlement request cannot be repeated while it is open")
check("a second settle is refused",
      "_open_request_of(c, REQUEST_SETTLE) is not None" in SRC["contract_actions.py"])

print("\n[EC6] a bid must be a positive amount")
a = SRC["data/auctions.py"]
check("the transaction refuses a non-positive bid", "if amount <= 0:" in a)

print("\n[AU1] a join carries the security state with the identity")
acc = SRC["data/accounts.py"]
check("a suspended account cannot be joined away", "_susp.get_active(_side)" in acc)
check("a suspension read failure refuses the join rather than allowing it",
      "Couldn't check those accounts just now" in acc)
check("the second factor moves with the identity", "_twofa_mod().move(drop, keep)" in acc)
check("two enrolled factors refuse rather than silently dropping one",
      "Both of those accounts have two-factor" in acc)
check("the dropped side's KSP sessions and devices are purged",
      "_purge_ksp(drop)" in acc)
check("twofa.move exists and refuses to overwrite", "def move(" in SRC["data/twofa.py"]
      and "already has a second factor" in SRC["data/twofa.py"])

print("\n[AU2] deletion and ban act on the account, not the raw snowflake")
k = SRC["cogs/ksp_bridge.py"]
check("delete-my-data resolves the account id first",
      "account_id = await asyncio.to_thread(accounts.account_for_discord, u.id)" in k)
check("it refuses rather than deleting the wrong thing when it cannot resolve",
      "I couldn't look your account up just now" in k)
check("the ban hook resolves the account id",
      "uid = accounts.account_for_discord(user.id) or str(user.id)" in k)

print("\n[AU3] enabling a second factor re-proves the primary credential")
check("a re-auth helper exists", "async def _require_fresh_firebase(" in s)
check("2fa/begin requires it", "await _require_fresh_firebase(ctx, req.id_token)" in s)
check("the token must belong to this account", "fuid != mine" in s)
check("check_revoked is used, as at sign-in",
      s.count("check_revoked=True") >= 2)
check("an owner-only recovery path exists for a lost factor",
      "admin_user_clear_2fa" in s and "clear-2fa" in s)

print("\n[AU4] a joined account is still reachable by DM")
check("_discord_id falls back to the account document",
      "accounts.discord_for_account(s)" in s)

print("\n[AU5] an approval challenge is bound to the tier that asked")
au = SRC["api_auth.py"]
check("the challenge records its audience", '"aud": aud,' in au)
check("a poll from the other tier reads as expired",
      'str(data.get("aud") or AUD_KSP) != aud' in au)
check("legacy challenges without the field still work", "AUD_KSP)" in au)
check("both tiers pass their own audience",
      "aud=AUD_KSP)" in s and "aud=AUD_WEB)" in s)
check("the DM names the surface that is actually asking",
      'what = "A web browser" if web else "A KSP client"' in s)

print("\n[AU6] failed link guesses are swept")
check("_LINK_FAILURES is in the sweep", "_LINK_FAILURES[k]" in s
      and "_sweep_rate_buckets" in s)

print("\n[INF2] sockets and concurrency are bounded")
check("the notification hub caps sockets per account",
      "MAX_PER_USER" in s and "over the %d-socket cap" in s)
check("the cap closes the oldest rather than refusing the newest",
      "closed the oldest" in s)
check("ws tickets are rate-limited", 'f"wsticket:{user[\'user_id\']}"' in s)
check("uvicorn has a concurrency ceiling", "limit_concurrency" in SRC["bot.py"])

print("\n[INF4] the stale docstring is corrected")
check("the ws endpoint no longer claims to accept a legacy ?token=",
      "A legacy ?token= is still accepted" not in s)



# ── Second pass: regressions the fixes themselves introduced ─────────────────

print("\n[RV1] a join decides the 2FA question before anything irreversible")
acc2 = SRC["data/accounts.py"]
check("the decision runs before the sign-in is moved",
      acc2.index("move_2fa = _tf.is_enabled(drop)") < acc2.index("code, message = link_firebase("))
check("only the write is left at the end", "if move_2fa:" in acc2)
check("a half-finished enrolment on the survivor is detected",
      "_tf.has_record(keep)" in acc2 and "def has_record(" in SRC["data/twofa.py"])
check("has_record refuses to guess on a read error",
      "TwoFactorUnavailable" in SRC["data/twofa.py"]
      and "raise TwoFactorUnavailable" in SRC["data/twofa.py"].split("def has_record(")[1].split("\ndef ")[0])
check("the caller turns that into a retry message, not a wrong instruction",
      "Couldn't check the security settings" in SRC["data/accounts.py"])

print("\n[RV2] enrolling 2FA is reachable by every kind of account")
check("an account with no Firebase identity is exempt, not refused",
      "if not mine:\n        return" in s)
# The exemption is a knowing trade, not a proof — it leaves AU3 open for
# Discord-origin accounts. Two things must stay true for it to remain acceptable:
# the cost is written down where the next reader will see it, and the damage is
# recoverable. If either regresses, the trade silently becomes an oversight.
check("the cost of the exemption is documented at the exemption",
      "exactly the AU3 attack" in s and "not freshness" in s)
check("the lockout it permits is recoverable", "admin_user_clear_2fa" in s)
check("the client is told which credential to re-prove", 'reauth: str = ""' in SRC["api_models.py"])
check("the provider is recorded at sign-in", "remember_provider" in acc2 and "remember_provider" in s)

print("\n[RV3] the ticket IP bucket only applies where addresses are distinguishable")
check("it is gated on API_TRUSTED_PROXIES", "if cfg.API_TRUSTED_PROXY_NETS:" in s)

print("\n[RV4b] the Discord weekly-mission path keys on the account id")
w2 = SRC["cogs/weeklymissions.py"]
check("_handle_selection resolves the account", "_accounts.account_for_discord" in w2)
check("it refuses rather than falling back to the snowflake",
      "Couldn't look your account up just now" in w2)

print("\n[RV5/RV6/RV12] limits are sized against the real client cadence")
check("the contract list ceiling is above the honest worst case",
      settings.CONTRACT_LIST_PER_HOUR >= 400)
check("ws tickets allow for reconnect storms", 'max_hits=300, window=3600.0' in s)
check("uvicorn's ceiling is above what the per-user socket cap permits",
      "limit_concurrency=2048" in SRC["bot.py"])

print("\n[RV7] the catalog cache is bounded on the read path too")
check("the reloader evicts as well", SRC["api_server.py"].count("_evict_part_catalogs()") >= 2)
check("re-insert makes the ordering LRU", "_PART_CATALOGS.pop(key, None)" in s)

print("\n[RV9] every client-reachable AI call is charged")
check("the part resolver takes a uid", "def _ai_resolve_part(mission_text: str, uid" in s)
check("and charges the same bucket",
      s.split("def _ai_resolve_part(")[1].split("\ndef ")[0].count('f"gemini:{uid}"') == 1)
check("the resolver is passed the caller", "_ai_resolve_part(mission_text, uid=str(uid))" in s)

print("\n[RV10] _discord_id does no Firestore read inside a loop")
check("a lookup-free mode exists", "allow_lookup: bool = True" in s)
check("both loop call sites use it", s.count("allow_lookup=False") >= 2)

print("\n[RV11] a bulk announcement cannot fill the moderation category")
t2 = SRC["cogs/tickets.py"]
check("bulk openings stop earlier", "TICKET_CATEGORY_BULK_MAX" in t2)
check("the announcement path opts out of the reserve", "reserve_capacity=False" in s)

print("\n[RV13] the socket cap evicts the oldest, not an arbitrary one")
check("connections are insertion-ordered", "dict[WebSocket, float]" in s)

print("\n[RV16] an over-size preview is dropped, not a refused listing")
check("the read is caught", "async def _read_preview(f):" in s)

print("\n[RV18] every create endpoint shares the create budget")
check("auctions charge it too", s.count('f"ctcreate:{uid}"') >= 2)

print("\n[RV19] moderator embeds escape user text at every site")
check("the bug report escapes summary and details", "_esc(summary)" in s)
check("flag_suspicion escapes", "escape_markdown(str(username))" in s)
check("the shared contract embed escapes, after fitting rather than before",
      "_esc(_fit_field(mission_text))" in SRC["cogs/contract_views.py"])
check("the AI refusal reason escapes", "escape_markdown(str(reason))" in SRC["contract_actions.py"])
check("the device report escapes", "escape_markdown(str(data.get('username')" in SRC["cogs/ksp_bridge.py"])

print("\n[RV20/RP2] promoting is not cheaper than burying, and the tab still works")
# A hard threshold at the removal quorum satisfied the abuse argument by switching
# the feature off — no craft on a market this size reaches 40 votes, so every
# listing scored 0 and "Recommended" silently became "Newest". Damping keeps the
# ordering meaningful at every count while still discounting a handful of alts.
check("a handful of votes is worth well under face value",
      api_server._ranked_score({"likes": 8, "dislikes": 0}) <= 4)
check("but is still worth more than none",
      api_server._ranked_score({"likes": 8, "dislikes": 0}) > 0)

print("\n[RW5] the public catalog is defended without relying on the CDN")
mk2 = SRC["data/marketplace.py"]
check("list_active is memoised", "_ACTIVE_CACHE" in mk2 and "_ACTIVE_TTL" in mk2)
check("every writer invalidates it", mk2.count("invalidate_active_cache()") >= 5)
check("the endpoint is rate-limited", '_rate_limit_ip("listings_ip"' in s)


# ── Round 3: crew ownership keys on an immutable id, not a display name ──────

print("\n[RM1] transfer ownership is decided by account id, not a display name")
imp_src = SRC["data/imports.py"]
check("the import entry carries an owner account id", '"owner_id"' in imp_src)
check("enqueue accepts it", "owner_id: str | None = None," in imp_src)
check("the name is documented as display-only", "DISPLAY name" in imp_src)
check("every live-vessel enqueue supplies it",
      s.count("owner_id=") >= 5)
check("the rescue delivery attributes the contractor",
      'owner_id=str(c.get("contractor_id", ""))' in s)
check("the issuer restore attributes the issuer", "owner_id=issuer_id," in s)
check("a declined quicksend returns under the sender's id",
      'owner_id=entry.get("owner_id")' in s)
check("the contract summary carries both parties' account ids",
      "issuer_id: str = \"\"" in SRC["api_models.py"]
      and "contractor_id: str = \"\"" in SRC["api_models.py"])
check("every summary builder populates them, not just one",
      s.count('issuer_id=str(c.get("issuer_id"') == 3)
check("the names are documented as display-only at the model",
      "Names stay for display" in SRC["api_models.py"])

print("\n— every touched module still parses —")
for name, text in SRC.items():
    try:
        ast.parse(text)
        ok = True
    except SyntaxError:
        ok = False
    check(f"{name} parses", ok)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

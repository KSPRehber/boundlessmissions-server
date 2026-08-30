"""Offline exercise of rewards + store.award_xp: atomicity, level-ups, the formula.

No Firestore and no Discord — the store's in-memory dict is the whole world here,
and the notification feed is captured in a list. What this checks is the thing
that made `rewards.py` necessary: XP awards used to be a read-modify-write whose
read sat outside the store lock, so two landing together lost one.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import settings
import api_server
import rewards
from data.store import store, level_from_xp, xp_for_level

GID = 1
UID = 4242

FEED = []
api_server._bot_instance = None
api_server._create_notification = (
    lambda gid, uid, kind, title, body, data=None: FEED.append((uid, kind, data)))


# ── assertions ───────────────────────────────────────────────────────────────
FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label} {detail}")


def reset(uid=UID):
    """Zero a user in the in-memory store."""
    u = store.get_user(GID, uid)
    u["xp"] = 0
    u["level"] = 0
    u["balance"] = 0
    FEED.clear()
    return u


async def _old_style_award(gid, uid, amount):
    """The award pattern this change replaced, reproduced exactly.

    `get_user` is synchronous and ran in the caller, *outside* the lock that
    `set_xp` then took — so any await point between the read and the write let a
    second award interleave and overwrite it. The sleep(0) is that await point;
    in production it was the Firestore round-trip inside `add_balance` next door.
    """
    current = store.get_user(gid, uid)["xp"]
    await asyncio.sleep(0)
    await store.set_xp(gid, uid, current + amount)


async def main():
    print("\ncontract_xp")
    per = settings.WEEKLY_COINS_PER_DIFFICULTY
    check("scales with payment (bot-issued)",
          rewards.contract_xp(per * 3, bot_issued=True) == 3 * settings.WEEKLY_XP_PER_DIFFICULTY,
          rewards.contract_xp(per * 3, bot_issued=True))
    check("a payment below one difficulty step earns nothing",
          rewards.contract_xp(per - 1, bot_issued=True) == 0,
          rewards.contract_xp(per - 1, bot_issued=True))
    check("zero payment earns nothing", rewards.contract_xp(0, bot_issued=True) == 0)
    # A player-issued contract is judged by its own issuer, so two friendly
    # accounts could cycle one for unbounded XP (2908 audit, F3). Off by default.
    saved = settings.CONTRACT_XP_HUMAN_ISSUED
    settings.CONTRACT_XP_HUMAN_ISSUED = False
    check("a player-issued contract earns no XP by default",
          rewards.contract_xp(per * 3) == 0, rewards.contract_xp(per * 3))
    settings.CONTRACT_XP_HUMAN_ISSUED = True
    check("…unless the switch is on",
          rewards.contract_xp(per * 3) == 3 * settings.WEEKLY_XP_PER_DIFFICULTY)
    settings.CONTRACT_XP_HUMAN_ISSUED = saved

    print("\naward_xp: the lost-update bug")
    reset()
    await asyncio.gather(*[_old_style_award(GID, UID, 10) for _ in range(20)])
    old_total = store.get_user(GID, UID)["xp"]
    check("the old read-modify-write loses awards", old_total < 200, old_total)

    reset()
    await asyncio.gather(*[store.award_xp(GID, UID, 10) for _ in range(20)])
    new_total = store.get_user(GID, UID)["xp"]
    check("award_xp keeps every one of them", new_total == 200, new_total)

    print("\naward_xp: no cooldown")
    reset()
    await store.award_xp(GID, UID, 10)
    await store.award_xp(GID, UID, 10)
    check("two awards in a row both land",
          store.get_user(GID, UID)["xp"] == 20, store.get_user(GID, UID)["xp"])

    print("\naward_xp: level reporting")
    reset()
    _xp, level, leveled = await store.award_xp(GID, UID, xp_for_level(1))
    check("crossing a threshold reports the level-up", leveled and level == 1,
          (level, leveled))
    _xp, _lvl, leveled = await store.award_xp(GID, UID, 1)
    check("staying inside a level does not", not leveled)
    reset()
    _xp, _lvl, leveled = await store.award_xp(GID, UID, 0)
    check("a zero award is a no-op", not leveled and store.get_user(GID, UID)["xp"] == 0)
    _xp, _lvl, leveled = await store.award_xp(GID, UID, -50)
    check("a negative award cannot drain XP", store.get_user(GID, UID)["xp"] == 0)

    print("\ngrant_xp: level-up settlement")
    reset()
    awarded, leveled = await rewards.grant_xp(GID, UID, xp_for_level(1),
                                              reason="Mission approved")
    check("reports what it awarded", awarded == xp_for_level(1), awarded)
    check("reports the level-up", leveled)
    check("pays the level-up reward once",
          store.get_user(GID, UID)["balance"] == settings.LEVEL_UP_REWARD,
          store.get_user(GID, UID)["balance"])
    check("writes the player's feed",
          [f for f in FEED if f[0] == UID and f[1] == "level_up"], FEED)
    check("the feed entry names the level",
          FEED and FEED[0][2].get("level") == 1, FEED)

    print("\ngrant_xp: no level-up")
    reset()
    awarded, leveled = await rewards.grant_xp(GID, UID, 1)
    check("awards without levelling", awarded == 1 and not leveled)
    check("pays no level-up reward", store.get_user(GID, UID)["balance"] == 0)
    check("writes nothing to the feed", FEED == [], FEED)

    print("\ngrant_xp: nothing to grant")
    reset()
    awarded, leveled = await rewards.grant_xp(GID, UID, 0)
    check("a zero award is a no-op", awarded == 0 and not leveled)
    awarded, _ = await rewards.grant_xp(GID, UID, -10)
    check("a negative award is a no-op",
          awarded == 0 and store.get_user(GID, UID)["xp"] == 0)
    check("neither touched the feed", FEED == [], FEED)

    print("\ngrant_xp: a feed failure must not lose banked XP")
    reset()
    boom = api_server._create_notification

    def _raise(*a, **k):
        raise RuntimeError("firestore down")

    api_server._create_notification = _raise
    try:
        awarded, leveled = await rewards.grant_xp(GID, UID, xp_for_level(1))
    finally:
        api_server._create_notification = boom
    check("the XP is still banked", store.get_user(GID, UID)["xp"] == xp_for_level(1))
    check("the level-up reward is still paid",
          store.get_user(GID, UID)["balance"] == settings.LEVEL_UP_REWARD)
    check("and it still reports the level-up", leveled)

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all checks passed")
    return 0


sys.exit(asyncio.run(main()))

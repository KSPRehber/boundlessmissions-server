"""A contract between two friendly accounts must not be an XP pump or a mint.

`rewards.contract_xp(payment)` turns every coin of a player-issued contract into
XP (100 XP per 60 coins), an issuer can approve their own contractor's submission
with no third party, and each level-up pays `LEVEL_UP_REWARD` coins. So two
accounts (one person with a website sign-up and a Discord account is enough) can
issue → submit → approve the same coins back and forth: the money is conserved,
the XP is not, and every level crossed prints new coins. No cheat detection can
see it: nothing was cheated in-game.

The closing design (2026-08-29) is deterministic — `rewards.human_contract_xp`
applies a per-contract cap, a per-contractor cooldown and a per-pair daily count,
and flags the pair to the moderators once. This script drives that gate the way
`contract_actions.review(approve=True)` does, on a simulated clock, and asks:
does a day of cycling still mint, and is the XP bounded by the settings?
"""
import asyncio

from _harness import check, section, finish
import settings
import rewards
from data.store import store, xp_for_level

A, B = "9301", "9302"
T0 = 1_800_000_000.0          # any fixed epoch; the gate takes `now` explicitly


def reset(uid, balance=0):
    u = store.get_user(0, uid)
    u.update({"balance": balance, "xp": 0, "level": 0, "debts": [], "tx": [], "tx_totals": {},
              "contract_xp_log": [], "last_contract_xp_at": 0.0})


async def cycle(issuer, contractor, payment, now):
    """issue (escrow) → submit → approve, as review(approve=True) does it."""
    assert await store.try_debit(0, issuer, payment, category=store.TX_CONTRACT_ESCROW)
    await store.add_balance(0, contractor, payment, garnishable=True,
                            category=store.TX_CONTRACT_PAYMENT)
    xp, gate, flag = await rewards.human_contract_xp(0, contractor, issuer, payment, now=now)
    if xp:
        _, level, leveled = await store.award_xp(0, contractor, xp)
        if leveled and settings.LEVEL_UP_REWARD:
            LEVEL_UPS.append(contractor)
            await store.add_balance(0, contractor, settings.LEVEL_UP_REWARD,
                                    garnishable=True, category=store.TX_REWARD)
    return xp, gate, flag


LEVEL_UPS: list[str] = []


async def main():
    P = settings.WEEKLY_COINS_PER_DIFFICULTY
    cap = settings.CONTRACT_XP_HUMAN_MAX
    free = settings.CONTRACT_PAIR_XP_FREE_PER_DAY
    cd = settings.CONTRACT_XP_COOLDOWN_MINUTES * 60
    print(f"    settings: cap {cap} XP/contract, cooldown {cd // 60} min, "
          f"{free} free per pair per {settings.CONTRACT_PAIR_WINDOW_HOURS} h")

    section("two accounts cycling a 6000-coin contract every 31 minutes for a day")
    reset(A, 6000)
    reset(B, 0)
    cycles = 48
    LEVEL_UPS.clear()
    xp_total = 0
    flags = 0
    gates = []
    for i in range(cycles):
        issuer, contractor = (A, B) if i % 2 == 0 else (B, A)
        xp, gate, flag = await cycle(issuer, contractor, 6000, T0 + i * (cd + 60))
        xp_total += xp
        flags += flag
        gates.append(gate)
    total = store.get_user(0, A)["balance"] + store.get_user(0, B)["balance"]
    minted = total - 6000
    check("the only coins minted are the level-up rewards on bounded XP",
          minted == len(LEVEL_UPS) * settings.LEVEL_UP_REWARD,
          f"{minted} coins minted for {len(LEVEL_UPS)} level-ups (A: "
          f"{store.get_user(0, A)['xp']} XP, B: {store.get_user(0, B)['xp']} XP)")
    bound = free * cap
    check(f"XP from the pair is bounded by the free count × cap ({bound})",
          0 < xp_total <= bound, f"{xp_total} XP over {cycles} cycles")
    check("the per-contract cap holds against a large payment",
          max(xp for xp in [cap]) == cap and rewards.contract_xp(6000) > cap
          and xp_total % cap == 0, f"total {xp_total}")
    check("the pair is flagged to the moderators exactly once", flags == 1, f"{flags} flags")
    check("later cycles are refused by the pair limit, not the cooldown",
          gates[-1] == rewards.XP_GATE_PAIR, gates[-1])

    section("rapid cycles: the cooldown")
    reset(A, 1800)
    reset(B, 0)
    xp1, g1, _ = await cycle(A, B, 600, T0)
    xp2, g2, _ = await cycle(A, B, 600, T0 + 60)
    xp3, g3, _ = await cycle(A, B, 600, T0 + cd + 1)
    check("first grant pays", xp1 == min(rewards.contract_xp(600), cap), xp1)
    check("a second contract a minute later pays no XP (cooldown)",
          xp2 == 0 and g2 == rewards.XP_GATE_COOLDOWN, (xp2, g2))
    check("…and pays again once the cooldown has passed", xp3 > 0 and g3 == "", (xp3, g3))
    check("the coins settled every time regardless",
          store.get_user(0, B)["balance"] >= 1800, store.get_user(0, B)["balance"])

    section("the reverse direction counts toward the same pair")
    reset(A, 600)
    reset(B, 600)
    n = 0
    now = T0
    seen_gate = ""
    while n < free + 1:
        issuer, contractor = (A, B) if n % 2 == 0 else (B, A)
        await store.add_balance(0, issuer, 600, category=store.TX_ADMIN)
        _, seen_gate, _ = await cycle(issuer, contractor, 600, now)
        now += cd + 1
        n += 1
    check("A→B and B→A share one pair budget", seen_gate == rewards.XP_GATE_PAIR, seen_gate)

    section("a ring of alts: the per-contractor daily ceiling")
    daily = settings.CONTRACT_XP_HUMAN_DAILY_MAX
    reset(A, 0)
    earned = 0
    now = T0
    last_gate = ""
    for k in range(12):
        alt = f"alt{k}"
        reset(alt, 600)
        xp, last_gate, _ = await cycle(alt, A, 600, now)
        earned += xp
        now += cd + 1
    check(f"twelve fresh issuers cannot pay A more than {daily} XP in a day",
          earned == daily and last_gate == rewards.XP_GATE_DAILY, (earned, last_gate))
    check("…and the coins from every one of them still settled",
          store.get_user(0, A)["balance"] >= 12 * 600, store.get_user(0, A)["balance"])

    section("what a level-up now costs to reach by cycling")
    per_day = min(free * cap, daily) if daily else free * cap
    for lvl in range(1, 6):
        need = xp_for_level(lvl) - xp_for_level(lvl - 1)
        print(f"    level {lvl}: {need:>5} XP = {need / per_day:>5.1f} days of "
              f"maxed-out pair cycling for +{settings.LEVEL_UP_REWARD} coins")
    finish()


asyncio.run(main())

"""`/contractreset` runs outside `contract_lock` and pays twice.

The command snapshots the player's contracts in a thread, then for each one
writes CANCELLED and refunds the issuer's escrow. Nothing serialises that with
the `@serialized` transitions, so a `review(approve)` (or the AI auto-accept,
or a player's own `cancel`) that lands between the snapshot and the write pays
the contractor — and the reset then refunds the issuer the same escrow. One
escrow, two payouts. A player who asks a mod for a reset while their partner
approves the submission gets both.
"""
import asyncio, types
from _econ import *  # noqa: F401,F403
from _econ import check, section, finish, mk, wallet, bal, run, store, ca, cdb, GID, DB, settings
import cogs.contracts as cc
import cogs.targets as targets


class _Resp:
    async def defer(self, **k): pass
SENT = []
class _FU:
    async def send(self, *a, **k): SENT.append(a)


class _Sel:
    def stream(self): return []
class _Doc:
    def collection(self, *a): return _Sel()
class _Col:
    def document(self, *a): return _Doc()
class _DB:
    def collection(self, *a): return _Col()
cc._db = _DB()


def interaction():
    return types.SimpleNamespace(
        guild_id=GID, user=types.SimpleNamespace(id="1", display_name="mod"),
        response=_Resp(), followup=_FU(), guild=None,
        client=types.SimpleNamespace(user=types.SimpleNamespace(id=999)))


async def main():
    settings.LEVEL_UP_REWARD = 0   # keep the level-up bonus out of the arithmetic
    section("A. reset snapshot → review(approve) → reset write: escrow paid out twice")
    wallet("100", balance=0); wallet("200", balance=0)
    mk("c1", status=cdb.SUBMITTED, issuer="100", contractor="200", payment=100, fine=0)

    async def _resolve(inter, member, username, **k):
        return types.SimpleNamespace(account_id="200", label="P", mention="@P")
    targets.resolve = _resolve

    # The real command reads the list in `asyncio.to_thread`; the review lands
    # while that thread is out. Same shape, made deterministic with a short sleep.
    orig = cdb.iter_user_contracts
    def slow_iter(gid, uid):
        rows = orig(gid, uid)          # snapshot on arrival …
        import time; time.sleep(0.05)  # … the latency is the trip back
        return rows
    cdb.iter_user_contracts = slow_iter

    cog = cc.Contracts.__new__(cc.Contracts); cog.bot = types.SimpleNamespace()  # skip __init__: it starts tasks.loops
    reset = asyncio.create_task(cc.Contracts.contractreset.callback(cog, interaction(), None, None))
    await asyncio.sleep(0.01)
    r = await ca.review(GID, "c1", actor_id="100", actor_name="Issuer", approve=True)
    try:
        await reset
    except Exception as exc:
        print('  reset raised:', repr(exc))
    print('  reset said:', SENT)
    check("review paid the contractor", r.ok and bal("200") == 100)
    check("one 100-coin escrow did not become 200 coins",
          bal("100") + bal("200") <= 100,
          f"issuer refunded {bal('100')} AND contractor paid {bal('200')} from one 100 escrow; "
          f"cogs/contracts.py contractreset never takes ca.contract_lock and re-checks nothing "
          f"after its snapshot (final status: {DB['c1']['status']})")
    finish()

run(main())

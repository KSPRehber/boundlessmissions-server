"""Ledger/balance divergence and wallet resurrection.

  A. `/setbalance` assigns `user["balance"]` directly under the lock and never
     calls `_record_locked`, so the ledger stops adding up to the wallet — the one
     invariant the Finance tab claims. (`admin_user_adjust` in api_server does it
     right, via add_balance with the delta.)
  B. `_garnish_locked` pays creditors through `get_user`, which MINTS a record
     for any id. A creditor who ran the "delete my data" flow is re-created
     (and flushed to Firestore on the next auto-save) the moment a debtor earns.
     Same for every refund/credit path aimed at a deleted issuer id.
"""
import types
from _econ import *  # noqa: F401,F403
from _econ import check, section, finish, mk, wallet, bal, ledger_sum, run, store, GID
import cogs.economy as eco
import cogs.targets as targets


class _Resp:
    async def defer(self, **k): pass
    async def send_message(self, *a, **k): pass
class _FU:
    async def send(self, *a, **k): pass


def interaction(uid="1"):
    return types.SimpleNamespace(guild_id=GID, user=types.SimpleNamespace(id=uid, display_name="mod"),
                                 response=_Resp(), followup=_FU(), guild=None)


async def main():
    section("A. /setbalance writes the wallet past the ledger")
    wallet("500", balance=0)
    await store.add_balance(GID, "500", 120, category=store.TX_REWARD)
    async def _resolve(inter, member, username, **k):
        return types.SimpleNamespace(account_id="500", label="P", avatar_url=None)
    targets.resolve = _resolve
    cog = eco.Economy(types.SimpleNamespace())
    await eco.Economy.setbalance.callback(cog, interaction(), 900, None, None)
    check("after /setbalance the ledger still sums to the balance",
          ledger_sum("500") == bal("500"),
          f"balance {bal('500')}, ledger sum {ledger_sum('500')}; cogs/economy.py setbalance assigns "
          f"user['balance'] without _record_locked")

    section("B. a deleted account is re-minted by a garnish payment")
    # A creditor who later deleted their data: no record at all.
    store._users.pop("777", None); store._dirty_users.discard("777")
    wallet("600", balance=0, debts=[{"creditor_id": "777", "amount": 50}])
    await store.add_balance(GID, "600", 100, garnishable=True, category=store.TX_CONTRACT_PAYMENT)
    check("garnishment does not resurrect a deleted creditor's record",
          not store.has_user("777"),
          f"users/777 re-created with balance {store.get_user(GID,'777')['balance']} and marked dirty "
          f"({'777' in store._dirty_users}) — the next auto-save writes it back to Firestore")
    finish()

run(main())

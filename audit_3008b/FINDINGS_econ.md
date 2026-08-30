# Economy audit — 2026-08-30 (audit_3008b)

Surface: `data/store.py`, `rewards.py`, `contract_actions.py`, `cogs/economy.py`,
`cogs/auctions.py` + `data/auctions.py`, `/api/v1/finance*`, marketplace buy, `cogs/xp.py`,
`cogs/contracts.py`. Every finding below is reproduced by a script in this directory
(`./run_econ.sh`); nothing here touched Firestore or Discord.

## Findings

### 1. MEDIUM — `/contractreset` runs outside `contract_lock` and pays one escrow twice
- `cogs/contracts.py:96-107` (`contractreset`), PoC `test_econ_contractreset_race.py`.
- Steps: contractor has a SUBMITTED contract. A mod runs `/contractreset` on them (the player can
  ask for it — "my contracts are stuck"). While the command's `to_thread(iter_user_contracts)`
  snapshot is out, the issuer clicks Approve (`ca.review`, or the AI auto-accept, or a `cancel`).
  Review pays the contractor 100; the reset resumes with its stale snapshot, writes CANCELLED over
  COMPLETED and refunds the issuer the same 100.
- Impact: 2× payout from one escrow; a paid contract is also relabelled `cancelled`. Same window
  exists against `give_up`/`pay_fine`/`expire_dispute` (fine collected + escrow refunded twice).
- Fix: cancel each contract through a `@serialized` transition (`ca.cancel`-like `mod_reset` that
  re-`_load`s under `contract_lock` and re-checks `status in active_statuses` before writing), or
  at minimum wrap the per-contract body in `async with ca.contract_lock(cid)` and re-read the
  document inside it.

### 2. MEDIUM — issuer-withdrawal fine is collected before the escrow refund lands
- `contract_actions.py:492-503` (`cancel`, ACTIVE + issuer), PoC `test_econ_cancel_fine_order.py`.
- Steps: issuer keeps their spare balance at 0 (everything is in the 100 escrow), withdraws an
  ACTIVE contract with fine 40. `debit_up_to` collects 0, the 40 becomes a debt to the contractor,
  and *then* `_pay_issuer(refund=100)` credits a wallet that is free to spend it (refunds are
  non-garnishable by design, and spends are never garnished).
- Impact: the withdrawal fine that exists to stop "preview the renders, walk away" is unenforced
  against any issuer who empties their wallet first; the contractor holds a debt collectable only
  from earnings the issuer need never have.
- Fix: refund the escrow first (it is the issuer's own money), then `debit_up_to` the fine from
  it — or collect the fine directly out of the escrow amount and refund `payment - collected`.

### 3. MEDIUM — auction close binds the winner to an ACTIVE contract with none of `accept`'s gates
- `cogs/auctions.py:283-290` (`_close_auction_locked`), `api_server.py:3416` (fine cap checked
  against `start_value`), PoC `test_econ_auction_bypass.py`.
- (a) `DEBT_MAX_OUTSTANDING` is enforced only in `ca.accept` (`contract_actions.py:417`); an auction
  winner never accepts. Weekly-mission select (`api_server.py:2533`) writes ACTIVE the same way.
  A debtor over the cap keeps taking obligations via auctions/weekly missions.
  (b) `MAX_FINE_MULTIPLE_OF_PAYMENT` is checked against the *start value*, but the contract's payment
  is the *winning bid*: start 10 000 / fine 50 000 / bid 1 → a 1-coin job carrying a 50 000 fine
  (50 000×, cap 5×), which `give_up` / dispute timeout turns into a 50 000 debt that follows the
  player. An issuer can bait this deliberately (a low bidder always "wins").
  (c) `MAX_ACTIVE_CONTRACTS_PER_USER` is not checked for the winner (14 active vs cap 10).
- Fix: in `try_place_bid` refuse a bid where `fine > amount * MAX_FINE_MULTIPLE_OF_PAYMENT` (the
  ceiling is already computed there, and the refusal can name the number), and refuse bidders over
  `DEBT_MAX_OUTSTANDING` / at the active cap (both are cheap in-memory reads). Apply the debt cap in
  `select_mission` too, or document that bot contracts are exempt.

### 4. LOW — a bot-issued contract's fine can be stalled forever
- `contract_actions.py:331` (`open_dispute_fields` resets `more_time_requests`), `:796` (bot
  `more_time` self-extends to ACTIVE), `:1067` (`expire_overdue`), PoC
  `test_econ_overdue_more_time_loop.py`.
- Steps: weekly mission refused → `more_time` (auto-granted, ACTIVE, due end of week) → never submit
  → overdue sweep → DISPUTED with `more_time_requests=0` → `more_time` again … 10 cycles in the PoC.
  `expire_dispute`, the only path that charges, is never reached.
- Fix: for bot-issued contracts count extensions per *contract* (do not reset the counter in
  `expire_overdue`, or store `more_time_total` alongside), or have `expire_overdue` refuse to
  reopen a dispute on a contract whose extension was already auto-granted once.

### 5. LOW — `/setbalance` writes the wallet past the ledger
- `cogs/economy.py:317`, PoC `test_econ_ledger_and_ghosts.py` A.
- Assigns `user["balance"]` directly; no `_record_locked`, no `tx_totals`. After one `/setbalance`
  the Finance tab's ledger no longer sums to the balance (900 vs 120 in the PoC) — the one
  invariant the tab promises. `admin_user_adjust` (`api_server.py:7943`) already does it right.
- Fix: `await store.add_balance(gid, uid, amount - current, category=TX_ADMIN, detail=...)` under
  the same semantics as the console; delete the direct write.

### 6. LOW — credits to a deleted account re-mint its `users/{id}` document
- `data/store.py:956` (`_garnish_locked` → `get_user`), and every `add_balance` aimed at an id from
  a contract/auction/listing document; PoC `test_econ_ledger_and_ghosts.py` B.
- A creditor/issuer who ran the delete-my-data flow is re-created with a balance the moment a
  debtor earns or a refund is routed to them, marked dirty, and flushed by the next auto-save.
- Fix: in `_garnish_locked`, pay only `if self.has_user(cid)` (else drop the debt entry as
  unpayable — the creditor chose to leave); have `delete_user` also strip that id from every other
  user's `debts`. For refunds, `_pay_issuer` can skip ids with no record.

## Verified sound (controls in `test_econ_store_invariants.py`)
- Store concurrency: 400 interleaved `try_debit`/`add_balance(garnishable)` tasks conserve the
  total, no wallet or debt goes negative, every ledger sums to its wallet. All mutation is on the
  event loop under one `asyncio.Lock`; no `to_thread` call touches `store`.
- Largest-remainder split: 3 000 random debt sets — `sum(paid) == take`, no creditor paid more than
  owed, `take ≤ min(total, gross·rate//100)`, no strand.
- `try_claim_timed_reward`: 50 concurrent claims pay once. `claim_contract_xp` is one critical
  section.
- Boundary coercion: pydantic lax `True → 1` and `10**40` reach the handlers but every spend goes
  through `try_debit`, which refuses; floats/`"1e3"`/≤0 are rejected on `FinanceSendRequest`,
  `ContractCreateRequest`, `WebAuctionBidRequest`. Discord `/pay`, `/givemoney`, `/fine`,
  `/setbalance` all gate sign/minimum.
- Self-dealing: contract create refuses `contractor_id == uid` (string compare), auction bid refuses
  the issuer inside the transaction, marketplace buy refuses `uid == seller_id`, finance send refuses
  self. Marketplace double-buy is a transactional claim with a non-garnishable refund of the buyer's
  own coins only.
- Garnish coverage: every *earning* credit passes `garnishable=True` (contract payment, fine
  received, marketplace sale, transfers in, screenshot/level-up/upload rewards); every refund/admin
  path does not. Spending an existing balance is not an escape because garnishment never claims
  principal — only #2 above manufactures an un-garnished credit.
- `_charge_fine`: debit + `add_debt` are two lock acquisitions but nothing between them can lose
  or duplicate coins; bot-issued fines file under creditor `""` and pay nobody.
- `expire_overdue` charges nothing and is idempotent on status; `expire_dispute`,
  `review`, `give_up`, `cancel`, `mod_resolve` are all `@serialized`; `submit_contract` holds the
  same `contract_lock` and `_auto_accept_contract` refuses a non-ACTIVE/SUBMITTED contract.
- Auction settlement: close is per-auction locked and idempotent; refund = `start_value - final`,
  never more than escrowed; a bid landing during close cannot create money.

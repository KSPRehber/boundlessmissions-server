# Economy / money-surface audit — audit_3008

Scope: everything that moves balance, XP, or ownership of value — `/finance*`,
`/missions/select`, all `/contracts/*`, `/auctions/*`, the web mirror
(`/web/contracts/*`, `/web/auctions/*`, `/web/marketplace/*`), and the data layer
(`contract_actions.py`, `data/store.py`, `data/marketplace.py`, `data/auctions.py`,
`data/contracts.py`, `rewards.py`).

Method: traced the amount and the ownership/state check from request to storage for
each handler. Prior audits (`audit_2908/`, `audit_2908_deep/`) already cover the
weekly-mission select race, the submit double-pay race, the vote race (now fixed with
per-user stripe locks), the sequential XP-farm gate, the dispute clock, upload quota,
and client-attested rewards — those are NOT re-reported here.

---

## 1. Concurrent contract approvals bypass the entire human-XP anti-farm gate
Severity: HIGH — CONFIRMED
rewards.py:104-131 (gate reads) · data/store.py:546-574 (lock-free reads vs. locked
note) · contract_actions.py:631-661 (`review` call site)

### The bug
`rewards.human_contract_xp` is the sole brake on player-issued-contract XP: a
per-contract cap (`CONTRACT_XP_HUMAN_MAX=500`), a 30-minute per-contractor cooldown,
a 1500 XP/day per-contractor ceiling, and a 3-per-pair-per-window flag. Every one of
those decisions is made from lock-free reads of the contractor's in-memory record:

- `store.last_contract_xp_at` (store.py:554) — cooldown check (rewards.py:109)
- `store.contract_xp_log` (store.py:546) — daily total (rewards.py:114) and
  `pair_completions` (rewards.py:77-79)

The write that would make a second call see the first — `store.note_contract_completion`
(store.py:557) — is the ONLY operation under `store._lock`, and it runs AFTER the gate
decision (rewards.py:131). So the check and the state it checks against are separated by
the whole body of `human_contract_xp`, with no lock spanning them. This is a classic
read-decide-then-write TOCTOU, and unlike `try_claim_timed_reward` (store.py:1024, which
does its cooldown check and credit under one lock) or the `@serialized` contract
transitions, nothing serialises it across DIFFERENT contracts.

`review` is `@serialized` per contract_id, so two different contracts between the same
pair take different locks and run concurrently. Both reach `await store.add_balance(...)`
for the payment (contract_actions.py:631) — a real suspension point that guarantees both
tasks are in flight — then both enter `human_contract_xp`. Because `asyncio.Lock` is
FIFO, the payment/note lock contention interleaves them so that both do their gate reads
before either notes completion:

    A: add_balance(payment) -> releases lock (B was queued, takes it)
    A: gate reads -> gate="" -> await note_completion -> queues behind B
    B: add_balance(payment) -> releases lock (A queued for its note)
    B: gate reads -> gate=""  (A has NOT noted yet — still queued) -> await note -> queues
    A: gets lock, notes A     -> grant_xp(A)
    B: gets lock, notes B     -> grant_xp(B)

Both grant full XP. The cooldown, the daily cap and the pair flag are all evaluated
against pre-completion state.

### Exploit sequence
Requires `CONTRACT_XP_HUMAN_ISSUED` (default True) and two accounts one person controls
(a Discord account + a website sign-up is enough — self-contracts are off, but two
accounts are trivial):

1. Account I (issuer) creates N contracts to account C (contractor), each `payment=300`
   (-> `contract_xp(300)=500`, at the cap). Escrow is conserved.
2. C accepts and submits all N (in-game submit -> SUBMITTED, held for human review).
3. I fires N CONCURRENT `POST /api/v1/web/contracts/{id}/review {"approve":true}` (the
   per-user budget is 30/min — `_web_actor`, api_server.py:6907-6913 — so N up to ~20-30
   in a burst is unthrottled). The same works from the KSP `review` endpoint.
4. All N pass the gate concurrently. C receives N × 500 XP instead of the intended 500
   (cooldown) / 1500 (daily cap). The pair flag (`flag_pair`, rewards.py:126) also races
   and typically fires ZERO times, so moderators are never told.
5. Repeat each 30-min window (or just re-batch — the cooldown never bit).

### Economic damage
- Coins minted: every XP level crossed pays `LEVEL_UP_REWARD=200` garnishable coins
  (rewards.py:158-166). A burst that would legitimately grant 500 XP instead grants
  N×500, crossing many more levels -> 200 coins each, from nothing. The contract coins
  stay conserved (escrow), but the level-up rewards are a genuine mint.
- Unbounded XP in a burst, defeating the exact control `test_xp_contract_farm.py`
  validated — that test drove the gate SEQUENTIALLY, so it never exercised this.
- The one moderator flag the design relies on to catch a colluding pair does not fire.

### Fix
Make the check-and-record atomic. Move the whole gate — cooldown read, daily read, pair
read, the decision, AND `note_contract_completion` — inside a single `store._lock`
critical section (a new `store.claim_contract_xp(...)` that returns the grantable XP and
records the completion under one lock), the mirror of `try_claim_timed_reward`. Then two
concurrent reviews serialise on the store lock and the second sees the first's
completion. (An in-process lock suffices under the one-process assumption every other
transition rests on; a second worker would need a Firestore transaction.)

---

## 2. `/contracts/create_rescue` accepts non-positive payment and negative fine
Severity: MEDIUM — CONFIRMED
api_server.py:3463-3464 (Form params) · 3555 (balance check) · 3615-3624 (escrow)

### The bug
Every other money entry point bounds its amount on the Pydantic model —
`ContractCreateRequest.payment` is `Field(..., gt=0)` / `fine` `ge=0`
(api_models.py:550-551), `AuctionCreateRequest.start_value` `gt=0` (api_models.py:567),
`FinanceSendRequest.amount` `gt=0` (api_models.py:334), `WebAuctionBidRequest.amount`
`gt=0` (api_models.py:826), and the marketplace price is range-checked in-handler
(api_server.py:5324). create_rescue ALONE declares its money as bare `Form`:

    payment: int = Form(...),     # no gt=0
    fine: int = Form(0),          # no ge=0

and the handler adds no lower bound:
- `if u.get("balance", 0) < payment` (:3555) — for `payment <= 0` this is trivially
  false, so the balance gate passes.
- `_fine_too_large(payment, fine)` (:3615, def at 2815) returns "" for any `fine <= 0`.
- `store.try_debit(gid, uid, payment, ...)` (:3616) — `try_debit` no-ops and returns
  True for `amount <= 0` (store.py:1063), so NO escrow is taken.
- The contract is created with the negative/zero `payment` and `fine`
  (`cdb.create_contract`, :3626).

### Consequences (integrity / griefing, not a direct mint)
- `payment=0, fine=0`: a valid "free rescue" that escrowed nothing — harmless-ish.
- `payment<0`: on approval, `review` runs
  `store.add_balance(contractor, c["payment"], garnishable=True)`
  (contract_actions.py:631) with a NEGATIVE amount, which `add_balance` clamps at zero
  (store.py:1010) — i.e. it DEBITS the contractor on completion instead of paying them,
  and `_pay_issuer(refund=payment)` skips the negative refund (contract_actions.py:219,
  `if refund > 0`). Net: coins are destroyed and the "recorded delta = balance change"
  ledger invariant the store is built around is exercised on an amount that was never
  supposed to be negative. A contractor who accepts such a contract is charged for doing
  the work; the payment is displayed, so a rational contractor declines, which is what
  keeps this from being a clean theft — but it is a real griefing/coin-destruction
  primitive and an unvalidated write.
- No mint path exists (rescue completion adds only the rescue counter,
  `_deliver_rescue_craft` -> `store.add_rescue`, api_server.py:3860; no bonus coins).

### Fix
Add the same bounds the other endpoints have: `payment: int = Form(..., gt=0)` and
`fine: int = Form(0, ge=0)`, or an explicit `if payment <= 0 or fine < 0: return ...`
guard before the escrow, matching `create_contract_from_ksp`.

---

## Surfaces traced and found sound (no finding)

- `/finance/send` (api_server.py:1819): `amount` gt=0 on the model and re-`int()`ed;
  self-send blocked (:1851), `MIN_TRANSFER` floor, target-existence check (:1863),
  atomic `try_debit` then garnishable credit, per-sender rate limit. Conserved.
- `/finance` (:1753): reads only the authenticated `uid`; no IDOR, no id parameter.
- `web_marketplace_buy` (:6463): self-buy blocked with string-normalised ids (:6480),
  atomic `try_debit` -> transactional `try_claim_purchase` -> refund-the-loser,
  already-owned re-download is free. Double-submit cannot charge twice. Conserved.
- Vote race (data/marketplace.py:296-347): now serialised per voter with a fixed lock
  stripe (`_vote_lock`), the endpoint adds per-IP + per-user + per-listing limits and a
  min-votes floor and account-age eligibility (api_server.py:6704-6741). The prior
  `test_vote_race.py` race is closed.
- delist / relist / delete (:7196, :7216, :7259): each checks `seller_id != str(uid)` ->
  403; relist is refused while at/below the live floor and while banned; delete with
  buyers degrades to delist. No money moves.
- Auctions: `try_place_bid` (data/auctions.py:82) validates the ceiling and writes inside
  one Firestore transaction, rejects self-bid and website-only bidders; `close_auction`
  (cogs/auctions.py:248) is serialised per auction with an asyncio lock and re-checks
  `status == OPEN` under it, so escrow is refunded exactly once. `web_auction_end` checks
  the issuer (api_server.py:7082). Escrow conserved across close and later cancel/give_up.
- Contract transitions (contract_actions.py): all `@serialized` per contract;
  accept/review/dispute/give_up/cancel/settle_response/more_time_response each verify the
  actor is the correct party (issuer or contractor) and the status, and all KSP + web
  endpoints pass `actor_id = authenticated user` (never a client-supplied id) — no IDOR.
  `_charge_fine` no-ops on `fine <= 0`, escrow/refund/fine arithmetic is conserved, and
  garnishment (`store._garnish_locked`) never pays out more than
  `min(debt, balance, share)` — no mint, no negative debt.
- Submit (api_server.py:3910): the whole body holds `ca.contract_lock`, the
  ACTIVE->SUBMITTED flip is a Firestore transaction (`cdb.claim_submission`), and
  `_auto_accept_contract` re-checks status — the prior submit double-pay race is closed.
  Auto-accept (which uses ungated `contract_xp`) is reachable ONLY for bot-issued
  contracts (`is_bot_issued`, :4361); human contracts always route to human review, so
  finding #1's gate is the correct and only path for player-pair XP.

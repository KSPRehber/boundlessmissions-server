"""One account, one vote — unless the votes arrive together.

`data/marketplace.set_vote` reads the caller's previous vote, computes a delta,
and moves the listing's counter with a Firestore Increment. The increment is
atomic; the read that decides the delta is not. The endpoint runs set_vote in a
thread pool and its rate limit is 120 votes an hour, so 25 identical requests
fired at once each see "no previous vote" and each add one. Because the rating
floor (`MARKETPLACE_AUTO_DELIST_SCORE`, -20) is enforced from this tally, one
account can take any listing off the grid.
"""
import threading

from _harness import check, section, finish, FakeCol
import settings
from data import marketplace as mkt

listings = FakeCol(latency=0.02)      # ~ a Firestore round trip
votes = FakeCol(latency=0.02)
mkt._col = lambda: listings
mkt._votes_doc = lambda uid: votes.document(str(uid))

LISTING = "L1"


def reset():
    listings.docs.clear()
    votes.docs.clear()
    listings.document(LISTING).set({"status": mkt.ACTIVE, "likes": 0, "dislikes": 0,
                                    "seller_id": "1"})


def tally():
    d = listings.document(LISTING).get().to_dict()
    return int(d["likes"]), int(d["dislikes"])


section("control: sequential votes from one account")
reset()
for _ in range(5):
    mkt.set_vote(LISTING, "voter", -1)
check("five sequential dislikes count once", tally() == (0, 1), f"tally={tally()}")
mkt.set_vote(LISTING, "voter", 0)
check("clearing restores the tally", tally() == (0, 0), f"tally={tally()}")

section("parallel identical votes from one account")
reset()
N = 25
threads = [threading.Thread(target=mkt.set_vote, args=(LISTING, "attacker", -1)) for _ in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
likes, dislikes = tally()
check(f"{N} parallel dislikes from one account count once", dislikes == 1,
      f"dislikes={dislikes}; the user's own vote record holds a single -1")
score = mkt.net_score({"likes": likes, "dislikes": dislikes})
check("one account cannot push a listing to the auto-delist floor",
      score > settings.MARKETPLACE_AUTO_DELIST_SCORE,
      f"score={score} <= floor {settings.MARKETPLACE_AUTO_DELIST_SCORE}: "
      f"_enforce_rating_floor would delist the craft on this vote")
mkt.set_vote(LISTING, "attacker", 0)
likes, dislikes = tally()
check("clearing the vote afterwards takes the inflated count back out", dislikes == 0,
      f"dislikes={dislikes} remain with no vote recorded against them — permanent")

section("the same race inflates likes for the 'recommended' sort")
reset()
threads = [threading.Thread(target=mkt.set_vote, args=(LISTING, "shill", 1)) for _ in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check(f"{N} parallel likes from one account count once", tally()[0] == 1, f"likes={tally()[0]}")

finish()

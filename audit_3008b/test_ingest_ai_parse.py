"""How each Gemini call site treats the model's answer as data.

  A. cogs/screenshots: the /analyze reward is `difficulty_rating` straight from
     the JSON — no type check, no clamp to 1..10. The only input to that call is
     the image, so text inside a screenshot is the steering channel (visual prompt
     injection; not reproduced here, the parse side is).
  B. /api/v1/achievement-photo: int() but no clamp either.
  C. controls: _ai_review_submission (approved is True, dict check, reason cap),
     _ai_resolve_part (answer validated against candidates), _classify_* fallback.
  D. non-dict answers: screenshots path assumes a dict outside its try.
"""
import asyncio, json, types
from _h import check, section, finish, quiet, src, between
import settings
import cogs.screenshots as shots
from data.store import store
import rewards
import api_server
quiet(api_server)
async def _no_announce(*a, **k): return None
rewards._announce_level_up = _no_announce
rewards._notify_level_up = lambda *a, **k: None

class FakeModels:
    def __init__(self, answer): self.answer = answer
    def generate_content(self, model, contents, config):
        return types.SimpleNamespace(text=json.dumps(self.answer), usage_metadata=None)
class FakeClient:
    def __init__(self, answer): self.models = FakeModels(answer)

def wallet(uid):
    u = store.get_user(0, uid); return u["balance"], u["xp"]

async def main():
    uid = "9401"
    u = store.get_user(0, uid); u.update({"balance": 0, "xp": 0, "level": 0, "tx": [], "tx_totals": {}})

    section("A. /analyze: difficulty_rating is unbounded and untyped")
    ssrc = src("cogs/screenshots.py")
    direct = between(ssrc, "if direct:", "# ── Mode 2")
    check("rating is coerced and clamped before _grant_rewards",
          "int(" in direct and ("min(" in direct or "max(" in direct),
          "cogs/screenshots.py: `rating = data.get(\"difficulty_rating\", 0)` → _grant_rewards(gid, uid, rating)")
    shots.active_client = lambda: FakeClient({"approved": True, "difficulty_rating": 1_000_000,
                                              "description": "ok"})
    data = await shots._run_gemini([b"\x89PNG"], 0)
    before = wallet(uid)
    xp, coins = await shots._grant_rewards(0, uid, data.get("difficulty_rating", 0))
    after = wallet(uid)
    check("a rating of 1,000,000 does not pay 1,000,000x the per-point reward",
          after[0] - before[0] <= 10 * settings.SCREENSHOT_COINS_PER_DIFFICULTY,
          f"+{after[0]-before[0]:,} coins, +{after[1]-before[1]:,} XP from one /analyze whose JSON said 1e6 "
          f"(max legitimate: {10*settings.SCREENSHOT_COINS_PER_DIFFICULTY} coins)")
    u.update({"balance": 0, "xp": 0, "level": 0})
    try:
        xp, coins = await shots._grant_rewards(0, uid, "10")
        crash = None
    except Exception as exc:
        crash = exc
    bal = store.get_user(0, uid)["balance"]
    check("a string rating (\"10\") is rejected rather than multiplied as a string",
          crash is not None or isinstance(bal, int),
          f"balance became {bal!r} ({type(bal).__name__})")

    section("B. achievement-photo: int() but no clamp")
    ach = between(src("api_server.py"), "rating = int(result.get(\"difficulty_rating\", 0) or 0)", "reward_suffix")
    check("achievement-photo rating is clamped to the 1..10 scale",
          "min(" in ach or "> 10" in ach or "<= 10" in ach,
          "api_server.py ~8745: `if rating > 0: _grant_rewards(gid, uid, rating)` — same unbounded multiply")

    section("C. controls")
    api = src("api_server.py")
    rev = between(api, "async def _ai_review_submission(", "async def _auto_accept_contract(")
    check("_ai_review_submission: approval requires `approved is True` (no truthy strings)",
          'result.get("approved") is True' in rev)
    check("_ai_review_submission: non-object answers are held, reason capped at 500",
          "isinstance(result, dict)" in rev and "[:500]" in rev)
    check("_ai_review_submission: ksp_level from the model is never read", 'result.get("ksp_level"' not in rev)
    from data import part_resolver as pr
    chosen = pr._ai("mainsail", [{"name": "A"}, {"name": "B"}], lambda l, c: "liquidEngineMainsail")
    check("_ai_resolve_part answer outside the candidate list is discarded", chosen is None)
    cls = between(api, "async def _classify_single_contract(", "# Crew-aboard limits")
    check("_classify_single_contract: any parse failure falls back to the heuristic",
          "except Exception" in cls and "_classify_text_heuristic(mission_text)" in cls)
    cache = between(api, "# Cache result back to the contract document", "return result")
    check("_classify_single_contract: mission_type from the model is validated before being stored (low)",
          'result.get("mission_type", "active_vessel")' not in cache or "in (" in cls,
          "api_server.py ~2420: mission_type/required_situation/required_body from the model are written to the "
          "contract document unvalidated; the text that steers them is the issuer's own mission (low)")

    section("D. non-dict model output")
    shots.active_client = lambda: FakeClient(["not", "an", "object"])
    data = await shots._run_gemini([b"\x89PNG"], 0)
    check("_run_gemini rejects a non-object answer", isinstance(data, dict),
          f"returned {type(data).__name__}; /analyze then does data.get() outside its try → unhandled "
          f"AttributeError → generic 'An error occurred' (robustness, not security)")
    finish()

asyncio.run(main())

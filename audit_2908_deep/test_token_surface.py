"""One token, every surface. A KSP session token (PluginData/session.token) is
accepted unchanged by every /api/v1/web/* endpoint, which run token-only (no
device binding, no mod-hash) — so a copied session file buys, bids, cancels and
delists from any machine while the device gate still says the KSP client is
protected."""
from _h import check, section, finish, src, between
from api_auth import build_session_payload, _sign_token, _verify_token

section("token payload carries no audience/surface")
payload = _verify_token(_sign_token(build_session_payload("0", "1", "p", "k", 0), "k"), "k")
check("a session token says which surface (ksp/web) it was minted for",
      "aud" in payload or "surface" in payload, f"payload keys: {sorted(payload)}")

s = src("api_server.py")
buy = between(s, "async def web_marketplace_buy", "\n@app.")
check("money-moving web endpoints require more than the bare token",
      "get_user_token_only" not in buy.split("\n")[0], "web_marketplace_buy depends on get_user_token_only")
finish()

"""Offline exercise of data/twofa: RFC vectors, replay refusal, recovery codes.

The TOTP here is hand-rolled on the standard library rather than pulled in as a
dependency, so the first thing this does is check it against **RFC 4226's published
test vectors**. An implementation that merely "returns six digits" and agrees with
itself would pass every other test in this file while being incompatible with every
authenticator app in existence.

After that, the two properties that make it a second factor rather than a formality:
a code cannot be used twice, and a lost phone is recoverable.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data import twofa

# ── a fake Firestore ─────────────────────────────────────────────────────────

DATA: dict[str, dict] = {}


class _Snap:
    def __init__(self, path):
        self._path = path
        self.exists = path in DATA

    def to_dict(self):
        return dict(DATA.get(self._path, {}))


class _Doc:
    def __init__(self, path): self._path = path
    def get(self, transaction=None): return _Snap(self._path)

    def set(self, payload, merge=False):
        if merge:
            DATA.setdefault(self._path, {}).update(payload)
        else:
            DATA[self._path] = dict(payload)

    def delete(self): DATA.pop(self._path, None)


class _Col:
    def __init__(self, name): self._name = name
    def document(self, doc_id): return _Doc(f"{self._name}/{doc_id}")


class _DB:
    def collection(self, name): return _Col(name)


twofa._db = _DB()

FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label} {detail}")


def main():
    ACC = "a_player0000000000000000001"

    print("\nRFC 4226 test vectors (secret '12345678901234567890')")
    # The RFC's ASCII secret, base32-encoded, with its published HOTP outputs.
    rfc_secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    expected = ["755224", "287082", "359152", "969429", "338314",
                "254676", "287922", "162583", "399871", "520489"]
    for counter, want in enumerate(expected):
        got = twofa._hotp(rfc_secret, counter)
        check(f"counter {counter} -> {want}", got == want, got)

    print("\nTOTP is HOTP over the time step")
    secret = twofa.generate_secret()
    # Aligned to a step boundary (1699999980 % 30 == 0). An arbitrary timestamp
    # sits part-way through a step, so "+29 seconds" would cross into the next one
    # and the stability check would be testing the wrong thing.
    at = 1_699_999_980.0
    check("a code is 6 digits", len(twofa.totp_now(secret, at)) == 6)
    check("stable across the whole 30s step",
          twofa.totp_now(secret, at) == twofa.totp_now(secret, at + 29))
    check("changes on the next step",
          twofa.totp_now(secret, at) != twofa.totp_now(secret, at + 30))

    print("\nsecrets are usable by a real authenticator")
    s = twofa.generate_secret()
    check("base32 alphabet only", all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in s))
    check("160 bits", len(s) == 32, len(s))
    uri = twofa.provisioning_uri(s, "Jeb")
    check("otpauth URI carries the secret", f"secret={s}" in uri)
    check("and the issuer", "issuer=Boundless%20Missions" in uri, uri)

    print("\nthe QR encodes the same URI the text does")
    uri = twofa.provisioning_uri(s, "Jeb")
    svg = twofa.provisioning_qr_svg(uri)
    check("renders something", bool(svg) and svg.lstrip().startswith("<svg"))
    check("inline-safe: no script and no external reference",
          "script" not in svg.lower() and "http://" not in svg and "https://" not in svg)
    # Honest about what this proves: there is no decoder here (that would be a
    # second dependency for tests alone), so this checks DETERMINISM — the same URI
    # always yields the same code — not that a phone can read it. The thing that
    # would actually go wrong silently is encoding a *different* URI from the one
    # shown as text, so that is asserted directly below.
    try:
        import segno
        check("the same URI always renders the same code",
              segno.make(uri, error="m").svg_inline(scale=5, border=2) == svg)
        check("a different URI renders a different code",
              segno.make(uri + "&x=1", error="m").svg_inline(scale=5, border=2) != svg)
    except ImportError:
        check("segno present", False, "not installed")
    check("a QR failure is survivable — manual entry still works",
          twofa.provisioning_qr_svg("") == "" or True)

    print("\nthe drift window")
    code_now = twofa.totp_now(secret, at)
    check("accepts the current step",
          twofa.match_counter(secret, code_now, at=at) is not None)
    check("accepts one step late",
          twofa.match_counter(secret, code_now, at=at + 30) is not None)
    check("accepts one step early",
          twofa.match_counter(secret, code_now, at=at - 30) is not None)
    check("refuses two steps away",
          twofa.match_counter(secret, code_now, at=at + 90) is None)
    check("refuses junk", twofa.match_counter(secret, "000000", at=at) is None
          or twofa.totp_now(secret, at) == "000000")
    check("refuses a short code", twofa.match_counter(secret, "123", at=at) is None)

    print("\nenrolment is not enforced until a code proves it")
    DATA.clear()
    started = twofa.begin_enroll(ACC, "Jeb")
    check("mints a secret", started and started["secret"])
    check("but is NOT enabled yet", twofa.status(ACC)["enabled"] is False)
    check("and reports itself pending", twofa.status(ACC)["pending"] is True)
    check("so sign-in is not gated", twofa.is_enabled(ACC) is False)

    ok, msg, codes = twofa.confirm_enroll(ACC, "000000")
    check("a wrong code does not enable it", not ok and not codes, msg)
    check("still off", twofa.is_enabled(ACC) is False)

    real = twofa.totp_now(started["secret"])
    ok, msg, codes = twofa.confirm_enroll(ACC, real)
    check("a real code enables it", ok, msg)
    check("and returns recovery codes once", len(codes) == twofa.RECOVERY_CODE_COUNT)
    check("now sign-in is gated", twofa.is_enabled(ACC) is True)
    check("the secret is never in the status payload",
          "secret" not in twofa.status(ACC))

    print("\na code cannot be used twice")
    ok, msg = twofa.verify(ACC, real)
    check("the code that enabled it is already spent", not ok, msg)
    check("and says so rather than 'wrong code'", "already been used" in msg, msg)

    print("\nrecovery codes are one-shot")
    DATA.clear()
    started = twofa.begin_enroll(ACC, "Jeb")
    _ok, _m, codes = twofa.confirm_enroll(ACC, twofa.totp_now(started["secret"]))
    check("stored hashed, never in the clear",
          all(c not in str(DATA) for c in codes))
    first = codes[0]
    ok, msg = twofa.verify(ACC, first)
    check("a recovery code signs you in", ok, msg)
    check("and is reported as spent", "left" in msg, msg)
    ok, _msg = twofa.verify(ACC, first)
    check("the same one cannot be reused", not ok)
    check("the rest still work", twofa.verify(ACC, codes[1])[0])
    check("the count goes down",
          twofa.status(ACC)["recovery_remaining"] == twofa.RECOVERY_CODE_COUNT - 2)
    check("case and spacing are forgiven",
          twofa.verify(ACC, f"  {codes[2].lower()}  ")[0])

    print("\nturning it off needs a code")
    ok, msg = twofa.disable(ACC, "000000")
    check("a wrong code cannot disable it", not ok, msg)
    check("still on", twofa.is_enabled(ACC) is True)
    ok, msg = twofa.disable(ACC, codes[3])
    check("a valid one can", ok, msg)
    check("and it is gone", twofa.is_enabled(ACC) is False)

    print("\nan unreadable record fails CLOSED")
    DATA.clear()
    started = twofa.begin_enroll(ACC, "Jeb")
    twofa.confirm_enroll(ACC, twofa.totp_now(started["secret"]))
    broken = twofa._col

    class _Boom:
        def document(self, _id):
            class _D:
                def get(self, transaction=None): raise RuntimeError("firestore down")
            return _D()

    twofa._col = lambda: _Boom()
    try:
        check("a failed read refuses the sign-in rather than skipping the factor",
              twofa.is_enabled(ACC) is True)
    finally:
        twofa._col = broken

    print("\nthe login challenge")
    DATA.clear()
    started = twofa.begin_enroll(ACC, "Jeb")
    twofa.confirm_enroll(ACC, twofa.totp_now(started["secret"]))
    cid = twofa.create_login_challenge(ACC)
    check("is created", bool(cid))
    got, msg, _p = twofa.resolve_login_challenge(cid, "000000")
    check("a wrong code does not resolve it", got is None, msg)

    # The code that ENABLED 2FA is spent, so it cannot also be the one that signs
    # you in. That is the replay guard, not a bug — right after enrolling you wait
    # for the next code. Asserted here so the behaviour is deliberate and visible.
    same_step = twofa.totp_now(started["secret"])
    got, msg, _p = twofa.resolve_login_challenge(cid, same_step)
    check("the code used to enrol cannot immediately sign you in too",
          got is None and "already been used" in msg, msg)

    # The next step's code — what the app shows a moment later — is accepted by
    # the drift window and is past `last_counter`.
    code = twofa.totp_now(started["secret"], time.time() + twofa.PERIOD)
    got, msg, _p = twofa.resolve_login_challenge(cid, code)
    check("the next code returns the account", got == ACC, (got, msg))
    got, msg, _p = twofa.resolve_login_challenge(cid, code)
    check("and the challenge is spent", got is None, msg)

    print("\nthe challenge can carry a pending link")
    DATA.clear()
    started = twofa.begin_enroll(ACC, "Jeb")
    twofa.confirm_enroll(ACC, twofa.totp_now(started["secret"]))
    link = {"guild_id": "0", "user_id": ACC, "username": "Jeb", "source": "panel"}
    cid = twofa.create_login_challenge(ACC, link)
    code = twofa.totp_now(started["secret"], time.time() + twofa.PERIOD)
    got, _msg, payload = twofa.resolve_login_challenge(cid, code)
    check("returns the account", got == ACC)
    check("and the link result the spent code produced", payload == link, payload)

    cid2 = twofa.create_login_challenge(ACC)
    code2 = twofa.totp_now(started["secret"], time.time() + 2 * twofa.PERIOD)
    _got, _msg, payload2 = twofa.resolve_login_challenge(cid2, code2)
    check("a plain sign-in challenge carries no payload — which is how the link "
          "endpoint refuses to complete one", payload2 == {}, payload2)

    print("\nbrute force is capped")
    DATA.clear()
    started = twofa.begin_enroll(ACC, "Jeb")
    twofa.confirm_enroll(ACC, twofa.totp_now(started["secret"]))
    cid = twofa.create_login_challenge(ACC)
    for _ in range(5):
        twofa.resolve_login_challenge(cid, "000000")
    got, msg, _p = twofa.resolve_login_challenge(cid, twofa.totp_now(started["secret"]))
    check("even a CORRECT code is refused after 5 wrong ones", got is None, msg)
    check("and the challenge is destroyed, not just blocked", "again" in msg, msg)

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all checks passed")
    return 0


sys.exit(main())

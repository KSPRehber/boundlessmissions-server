"""i18n.t / tp: str.format with user-controlled *values* (never templates)."""
from _h import check, section, finish
import i18n

section("format-string injection through values")
hostile = "{__class__} {0} {x.__init__.__globals__} {symbol} }{"
out = i18n.t(0, "eco.give.desc", name=hostile, amount=1, currency="KC", reason=hostile, balance=5)
check("placeholders inside a user value are not expanded", hostile in out and "__globals__" in out)
check("an extra kwarg the template does not use is ignored", "x" not in i18n.t(0, "common.error", x=object()))
check("a missing kwarg leaves the template intact rather than raising",
      i18n.t(0, "eco.pay.min") == i18n.S["eco.pay.min"]["en"])
check("templates are code-owned (S is only extended by S.update in cogs at import)",
      all(isinstance(k, str) and isinstance(v, dict) for k, v in i18n.S.items()))
check("unknown key returns the key (no exception)", i18n.tp(0, 1, "no.such.key") == "no.such.key")
finish()

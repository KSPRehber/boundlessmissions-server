"""Craft-hash bans: which edits the fingerprints survive, and which cheap ones they don't.

`data/craft_bans.fingerprint` derives `design` (part names + centimetre positions)
and `parts` (names only) from a brace-depth scan of the craft text. CLAUDE.md
concedes that a .craft is plain text and an *edit* gets past any hash. The finer
question is whether a purely cosmetic re-formatting — one KSP's ConfigNode
loader accepts and that changes no part, no position and no module — also
defeats the two fuzzy hashes. It should not: that is exactly the reach they
exist to add over `exact`.
"""
from _harness import check, section, finish
from data import craft_bans as cb


def craft(parts, *, name="Rocket", desc="", brace_style="newline", extra_keys=()):
    """A minimal .craft. `parts` = [(part_with_id, pos)]."""
    out = [f"ship = {name}", "version = 1.12.5", f"description = {desc}", "type = VAB"]
    for pname, pos in parts:
        if brace_style == "newline":
            out += ["PART", "{"]
        else:
            out += ["PART {"]
        out += list(extra_keys)
        out += [f"\tpart = {pname}", "\tpartName = Part", f"\tpos = {pos}",
                "\tMODULE", "\t{", "\t\tname = ModuleCommand", "\t}", "}"]
    return "\n".join(out).encode()


BASE = [("mk1pod_4294", "0.0,15.0,0.0"), ("fuelTank_4293", "0.0,12.5,0.0"),
        ("liquidEngine_4292", "0.0,10.0,0.0")]

section("what the fuzzy hashes are meant to survive")
fp0 = cb.fingerprint(craft(BASE))
check("a plain craft yields design and parts hashes", fp0["design"] and fp0["parts"]
      and fp0["part_count"] == 3)
fp1 = cb.fingerprint(craft(BASE, name="Totally Different", desc="new text"))
check("rename + re-description keep the design hash", fp1["design"] == fp0["design"])
fp2 = cb.fingerprint(craft([(n.rsplit("_", 1)[0] + "_1", p) for n, p in BASE]))
check("fresh instance ids keep the design hash", fp2["design"] == fp0["design"])
fp3 = cb.fingerprint(craft([(n, p.replace("15.0", "15.004")) for n, p in BASE]))
check("sub-centimetre float noise keeps the design hash", fp3["design"] == fp0["design"])
fp4 = cb.fingerprint(craft([(n, p.replace("15.0", "15.5")) for n, p in BASE]))
check("a real nudge changes design but keeps parts",
      fp4["design"] != fp0["design"] and fp4["parts"] == fp0["parts"])
check("a MODULE's own name= line is invisible to the hash",
      cb.fingerprint(craft(BASE, extra_keys=())) == fp0)
fpcrlf = cb.fingerprint(craft(BASE).replace(b"\n", b"\r\n"))
check("CRLF line endings keep both hashes",
      fpcrlf["design"] == fp0["design"] and fpcrlf["parts"] == fp0["parts"])
fpvessel = cb.fingerprint(b"VESSEL\n{\n" + b"".join(
    f"PART\n{{\nname = {n.rsplit('_', 1)[0]}\nposition = {p}\n}}\n".encode() for n, p in BASE)
    + b"}\n")
check("the same design read from a saved VESSEL node hashes identically",
      fpvessel["design"] == fp0["design"] and fpvessel["parts"] == fp0["parts"])

section("cosmetic re-formatting that KSP's loader accepts")
fp5 = cb.fingerprint(craft(BASE, brace_style="sameline"))
check("`PART {` with the brace on the node's own line still yields the parts "
      "(ConfigNode.PreFormatConfig splits braces off a line, so KSP loads it)",
      fp5["part_count"] == 3 and fp5["design"] == fp0["design"],
      f"part_count={fp5['part_count']} design={fp5['design']}: every design/parts ban "
      f"matches nothing, and the unparseable payload also gets no fingerprint of its own")
fp6 = cb.fingerprint(craft(BASE, extra_keys=("\tname = anything",)))
check("an unknown `name =` key in a .craft PART node (ignored by KSP) does not "
      "displace the part name", fp6["parts"] == fp0["parts"],
      "the first of part=/name= wins, so a stray key ahead of `part =` renames the part "
      "for the hash without touching the craft")
fp7 = cb.fingerprint(craft(BASE).replace(b"\n\tpart = ", b"\n\tpart= "))
check("`part= x` (no space before =) still parses", fp7["parts"] == fp0["parts"])

section("the honest edge cases")
fp8 = cb.fingerprint(b"")
check("an empty payload gets no design/parts hash (never a hash of nothing)",
      fp8["design"] is None and fp8["parts"] is None)
fp9 = cb.fingerprint(craft(BASE)[:-40])
check("a truncated file hashes only the parts that closed", fp9["part_count"] == 2)

finish()

"""Untrusted text that ends up in Discord embeds: length overflow (a 400 from
Discord = the message is never posted) and mention / markdown injection.

  A. rescue-contract mission text is uncapped (every other path caps at 500)
     and is rendered into a 1024-char embed field by contract_views._embed —
     every Discord delivery of that contract (offer, dispute, sue ticket) 400s.
  B. submitted file names are stored raw and rendered as masked links in the
     sue-ticket / issuer-review embed: length overflow + link-text injection.
  C. controls: report tickets, web tickets, bug reports all fit inside limits
     at their maximum input sizes; no user text reaches `content=`.
"""
import asyncio, re
import discord
from _h import check, section, finish, src, between
import settings
import cogs.contract_views as cv
from data import contracts as cdb

def embed_ok(e: discord.Embed) -> tuple[bool, str]:
    """Discord's per-part limits (the ones a 400 is raised on)."""
    probs = []
    if e.title and len(e.title) > 256: probs.append(f"title {len(e.title)}")
    if e.description and len(e.description) > 4096: probs.append(f"description {len(e.description)}")
    for f in e.fields:
        if len(f.name) > 256: probs.append(f"field name {len(f.name)}")
        if len(f.value) > 1024: probs.append(f"field value {len(f.value)}")
    if len(e.fields) > 25: probs.append(f"{len(e.fields)} fields")
    if len(e) > 6000: probs.append(f"total {len(e)}")
    return (not probs), ", ".join(probs)

def contract(mission, files=None, ctype="active_vessel"):
    return {"contract_id": "c1", "guild_id": "0", "issuer_id": "1", "issuer_name": "Iss",
            "contractor_id": "2", "contractor_name": "Con", "mission": mission,
            "payment": 100, "fine": 10, "due_date": "2099-01-01", "status": cdb.ACTIVE,
            "mission_type": ctype, "submitted_files": files or [], "constraints": {}}

section("A. rescue mission text: uncapped at ingestion, 1024-char field at render")
api = src("api_server.py")
rescue = between(api, 'async def create_rescue_contract(', '\n@app.')
check("ContractCreateRequest.mission is capped (control)",
      "max_length=500" in between(src("api_models.py"), "class ContractCreateRequest", "class AuctionCreateRequest"))
check("create_rescue_contract caps `mission` before storing it",
      bool(re.search(r"mission\s*=\s*\(?mission[^\n]*\[:\d+\]|len\(mission\)\s*[<>]", rescue)),
      "mission: str = Form(...) is stored via cdb.create_contract(mission=mission) with no length check")
long_mission = "Rescue my kerbal " * 200          # 3400 chars, well under the 80 MB request cap
e = cv._embed(contract(long_mission), 0)
ok, why = embed_ok(e)
check("contract_views._embed of a long rescue mission is postable", ok,
      f"embed violates Discord limits ({why}); deliver_to_player/sue/report paths all build this embed "
      f"and swallow the resulting 400 — the offer never reaches the contractor's corp channel")

section("B. submitted file names: raw into a masked-link field")
sub = between(api, "stored_files.append({\"filename\": craft_file.filename", "\n")
check("craft/screenshot filenames are truncated or sanitised before storage",
      "[:" in sub or "_safe" in sub or "basename" in sub,
      "api_server.py:4242/4253 store UploadFile.filename verbatim")
files = [{"filename": "a" * 1100 + ".craft", "url": "https://storage.invalid/x"},
         {"filename": "s.png", "url": "https://storage.invalid/y"}]
# Re-create exactly the field contract_actions.dispute(sue) adds (contract_actions.py ~1367)
e = cv._embed(contract("Land on the Mun", files), 0)
e.add_field(name="📁 Submitted Files",
            value="\n".join(f"📎 [{f['filename']}]({f['url']})" for f in files), inline=False)
ok, why = embed_ok(e)
check("sue-ticket embed survives a long submitted filename", ok,
      f"{why}: the ticket channel is created and recorded, but the post carrying ModReviewView "
      f"(the mods' enforce/cancel buttons) fails, so the dispute has no controls")
evil = "click here](https://evil.example/phish) [x"
val = f"📎 [{evil}](https://storage.invalid/x)"
check("a filename cannot rewrite the masked link's target",
      "https://evil.example" not in val or "](" not in evil,
      f"rendered as {val!r}: Discord shows 'click here' pointing at evil.example in a moderator ticket")

section("C. controls: report / ticket / bug-report embeds at maximum input")
from cogs import tickets as tk
title = "x" * 150; body = "y" * 4000            # TicketCreateRequest maxima
e = discord.Embed(title=f"🎫 {title}", description=body); e.set_footer(text="Ticket #0001")
e.add_field(name="Opened by", value="<@1>\n`1`\nUsername: `" + "u"*32 + "`", inline=False)
ok, why = embed_ok(e); check("web ticket at max title/body fits", ok, why)
reason = "r" * 1500
e = discord.Embed(title="🚨 Contract report", description=f"**What went wrong**\n{reason}")
ok, why = embed_ok(e); check("contract-report first embed at max reason fits", ok, why)
desc = f"**Summary**\n{'s'*200}\n\n**Details**\n{'d'*1500}"
e = discord.Embed(title="🐛 Bug report (in-game)", description=desc)
ok, why = embed_ok(e); check("bug-report embed at max summary/details fits", ok, why)
tsrc = src("cogs/tickets.py")
send = between(tsrc, "await channel.send(content=content, embed=e", "))")
check("ticket opening post: content is only bot-built mentions, allowed_mentions explicit",
      "allowed_mentions=discord.AllowedMentions(roles=True, users=True" in send
      and re.search(r"content_bits\.append\(opener\.mention\)", tsrc) is not None)
check("no user-authored text is sent as message `content` in the report/ticket paths",
      not re.search(r"content=f\"[^\"]*\{(reason|mission|summary|details|title|description)", api + tsrc))
finish()

"""TRACE THE THREE FLOWS. `python verify_approvals.py`

  (a) a reply "met Sahaj today"  -> proposal -> yes -> cell written + echo
  (b) Sid "no" after Vaishnavi "yes"  -> NOT written
  (c) focus set  -> R5 preview filtered

No sheet, no network, no Discord. The sheet write is replaced by a recorder that
says what WOULD have been written, so flow (b) can prove nothing reached it.
"""
import logging
import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import approvals
import config
import db as dbmod
import deadlines as dl
import focus
import gtm_sheet
import nextaction
import rules as rules_mod
import sheetwrite

SID, VAISHNAVI, TRISHI = 111, 222, 333
config.TEAM_ROSTER_IDS = [SID, VAISHNAVI, TRISHI]
config.SALES_APPROVER_IDS = [SID, VAISHNAVI]
config.SALES_FINAL_SAY_ID = SID
config.ROSTER_DISPLAY_NAMES = {str(SID): "Sid", str(VAISHNAVI): "Vaishnavi",
                               str(TRISHI): "Trishi"}

TODAY = date(2026, 9, 21)
failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"    {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def fresh_db():
    f = os.path.join(tempfile.gettempdir(), "verify_approvals.db")
    if os.path.exists(f):
        os.remove(f)
    return dbmod.DB(f)


# The sheet, as a parsed tab. Sahaj at Wispr Flow, meeting date blank.
HEADERS = ["Sr No", "Company/Uni", "Industry", "Name", "Designation", "Email id",
           "Based", "Research Paper Link", "LI Url", "First Contact",
           "First Contact Type", "First Contact Date", "Sid - LI Addition",
           "LI Connected Date", "LI DM Sent", "LI DM Date", "Meeting Date",
           "Meeting Status", "Next Steps/Notes", "Package", "Prospect Status",
           "Closure Prob%", "Estd. Deal Size (USD)", "Deal Status"]
ROWS = [HEADERS,
        ["1", "Wispr Flow", "AI Voice Agents", "Sahaj", "Chief Scientist", "", "SF",
         "", "", "TRUE", "LinkedIn", "01-09-2026", "TRUE", "05-09-2026", "TRUE",
         "09-09-2026", "", "", "", "", "", "", "", ""]]


def tab_and_row():
    import time
    tab = gtm_sheet.SHEETS._parse_values("Outreach PoCs", ROWS, read_at=time.time())
    return tab, tab.rows[0]


# ---------------------------------------------------------------- (a)
print("=" * 78)
print('(a)  reply "met Sahaj today"  ->  proposal  ->  yes  ->  written + echo')
print("=" * 78)

d = fresh_db()
tab, row = tab_and_row()

# What the extractor returns for "met Sahaj today".
extracted = [{"role": "meeting_date", "value": "21-09-2026", "supersedes": False,
              "quote": "met Sahaj today"}]
plan = sheetwrite.plan_writes(tab=tab, row=row, fields=extracted,
                              trigger=sheetwrite.TRIGGER_REPLY,
                              reply_text="met Sahaj today")
print(f"  plan           writes={plan['writes']}")
check("the plan has the cell", plan["writes"], {"meeting_date": "21-09-2026"})

proposed = approvals.proposal_text(company="Wispr Flow", poc="Sahaj",
                                   applied=plan["applied"])
print(f"  bot posts      {proposed}")
check("names the column", "Meeting Date" in proposed, True)
check("names the row", "Sahaj (Wispr Flow)" in proposed, True)
check("asks for a yes", "Reply yes." in proposed, True)

d.open_proposal(proposal_key="pA", kind="cell_update", tab=tab.title, sheet_row=2,
                row_key="wispr flow|sahaj", company="Wispr Flow", poc="Sahaj",
                payload={"writes": plan["writes"], "applied": plan["applied"],
                         "asks": plan["asks"], "skipped": plan["skipped"]},
                reply_text="met Sahaj today", trigger="reply",
                proposed_text=proposed, requested_by="Trishi", channel_id=1,
                message_id="m1", created_at="2026-09-21T14:00:00")

# NOTHING IS WRITTEN YET, and that is the whole point.
check("nothing written before the yes", d.proposal("pA")["status"], "open")

print('  Vaishnavi:     "yes"')
d.record_vote(proposal_key="pA", voter_id=VAISHNAVI, voter_label="Vaishnavi",
              vote="yes", voted_at="2026-09-21T14:02:00")
decision, why, by = approvals.decide(d.proposal("pA")["votes"])
check("decision", decision, approvals.APPLY)
print(f"  decision       {decision} — {why}")

written = dict(d.proposal("pA")["payload"]["writes"])
applied = d.proposal("pA")["payload"]["applied"]
echo = sheetwrite.echo_line(company="Wispr Flow", poc="Sahaj", applied=applied,
                            asks=[], skipped=[], undo_hours=24)
print(f"  WOULD WRITE    row 2 {written}")
print(f"  bot echoes     {echo}")
check("the cell is the one proposed", written, {"meeting_date": "21-09-2026"})
check("the echo names the column", "meeting date" in echo.lower(), True)
check("the echo offers the undo", "undo" in echo.lower(), True)

# The original text survives the round trip — the terminal-word gate needs it.
check("the original reply is kept", d.proposal("pA")["reply_text"], "met Sahaj today")
check("...and 'yes' contains no terminal word",
      sheetwrite.said_terminal_words("yes"), "")

# ---------------------------------------------------------------- (b)
print()
print("=" * 78)
print('(b)  Vaishnavi "yes", then Sid "no"  ->  NOT written')
print("=" * 78)

d = fresh_db()
d.open_proposal(proposal_key="pB", kind="cell_update", tab="Outreach PoCs",
                sheet_row=2, row_key="wispr flow|sahaj", company="Wispr Flow",
                poc="Sahaj", payload={"writes": {"meeting_date": "21-09-2026"},
                                      "applied": [], "asks": [], "skipped": []},
                reply_text="met Sahaj today", trigger="reply",
                proposed_text=proposed, requested_by="Trishi", channel_id=1,
                message_id="m2", created_at="2026-09-21T14:00:00")

print('  Vaishnavi:     "yes"')
d.record_vote(proposal_key="pB", voter_id=VAISHNAVI, voter_label="Vaishnavi",
              vote="yes", voted_at="2026-09-21T14:02:00")
mid, mid_why, _ = approvals.decide(d.proposal("pB")["votes"])
print(f"  after one vote {mid} — {mid_why}")

print('  Sid:           "no"')
d.record_vote(proposal_key="pB", voter_id=SID, voter_label="Sid",
              vote="no", voted_at="2026-09-21T14:09:00")
decision, why, by = approvals.decide(d.proposal("pB")["votes"])
print(f"  decision       {decision} — {why}")

check("Sid's no wins", decision, approvals.DECLINE)
check("the decider is Sid", by, "Sid")
check("the reason names the override", "outranks" in why, True)
d.close_proposal(proposal_key="pB", status="declined", decision=why,
                 decided_by=by, decided_at="2026-09-21T14:09:00")
check("the proposal is declined, not applied", d.proposal("pB")["status"], "declined")
check("NOTHING was written", decision != approvals.APPLY, True)
print("  bot says       Not doing that one: " + why
      + ". (Vaishnavi had said yes, so to be clear — the sheet is unchanged.)")

# Order must not matter: Sid first, Vaishnavi second, same answer.
d.record_vote(proposal_key="pB", voter_id=VAISHNAVI, voter_label="Vaishnavi",
              vote="yes", voted_at="2026-09-21T14:20:00")
check("a later yes from Vaishnavi does NOT flip it",
      approvals.decide(d.proposal("pB")["votes"])[0], approvals.DECLINE)

# ---------------------------------------------------------------- (c)
print()
print("=" * 78)
print("(c)  focus set  ->  R5 preview filtered")
print("=" * 78)

d = fresh_db()
cmd = "prioritise only AI Voice Agents for the next two weeks"
parsed = focus.parse(cmd)
print(f'  Sid:           "{cmd}"')
check("parsed as a set", parsed["action"], focus.SET)
check("the subject", parsed["value"], "AI Voice Agents")
check("the duration", parsed["days"], 14)

ends = focus.expiry(on=TODAY, days=parsed["days"])
d.set_focus(field="", value=parsed["value"], raw=cmd, set_by="Sid", set_by_id=SID,
            set_on=dl.iso(TODAY), expires_on=dl.iso(ends))
print(f"  bot confirms   {focus.confirmation(parsed['value'], days=parsed['days'], ends=ends)}")
live = d.active_focus(today=dl.iso(TODAY))
check("the focus is live", live["value"], "AI Voice Agents")

def prospect_row(n, company, industry, name, designation):
    return {"_row": n, "_extra": {}, "company": company, "industry": industry,
            "name": name, "designation": designation, "based": "",
            "first_contact": "", "first_contact_date": ""}

prospects = [
    prospect_row(2, "Acme", "Fintech", "Ann", "Founder"),
    prospect_row(3, "PolyAI", "AI Voice Agents", "Eleonore", "Founder"),
    prospect_row(4, "Borealis", "Robotics", "Bo", "CEO"),
    prospect_row(5, "Wispr Flow", "AI voice agents (conversational)", "Sahaj", "Chief Scientist"),
]

R5 = rules_mod.by_id("R5")
out_no = nextaction.run(today=date(2026, 9, 22), rows=prospects, day_rules=[R5])
out_yes = nextaction.run(today=date(2026, 9, 22), rows=prospects, day_rules=[R5],
                         focus=dict(live))

print(f"  without focus  {[a['company'] for a in out_no['actions']]}")
print(f"  WITH focus     {[a['company'] for a in out_yes['actions']]}")
check("unfocused takes the first two companies in sheet order",
      sorted({a["company"] for a in out_no["actions"]}), ["Acme", "PolyAI"])
check("focused takes the two that match",
      sorted({a["company"] for a in out_yes["actions"]}), ["PolyAI", "Wispr Flow"])
note = out_yes["actions"][0].get("focus_note", "")
print(f"  R5 says        {note}")
check("the note names the focus", "AI Voice Agents" in note, True)
check("...and the matching count", "2 matching" in note, True)

# A focus nobody matches must SAY SO, not go quiet.
miss = nextaction.run(today=date(2026, 9, 22), rows=prospects, day_rules=[R5],
                      focus={"value": "Quantum Robotics"})
mnote = miss["actions"][0].get("focus_note", "") if miss["actions"] else ""
print(f"  no matches     {mnote}")
check("a focus that matches nothing still produces items",
      len(miss["actions"]) > 0, True)
check("...and says nothing matched", "Nothing on the sheet matches" in mnote, True)
check("...and says it fell back to sheet order", "sheet order" in mnote, True)

print()
print("=" * 78)
print("  " + ("ALL PASSED" if not failures else f"{failures} FAILED"))
print("=" * 78)
raise SystemExit(1 if failures else 0)

"""A HEAVY MONDAY, end to end. `python verify_heavy_monday.py`

Four rule posts + two R8 meeting-prep + one R9 follow-up + the pending-approvals
sweep. Prints every send time and proves nothing lands after SALES_DRIP_END.

Nothing is sent and nothing is written: `drip.plan` computes, `approvals`
renders, and the append is a dry run.
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
import drip
import gtm_sheet
import nextaction

MONDAY = date(2026, 9, 21)
SID, VAISHNAVI, KUSHAL = 1001, 1002, 1003
config.TEAM_ROSTER_IDS = [SID, VAISHNAVI, KUSHAL]
config.SALES_ALWAYS_TAG_IDS = [VAISHNAVI, SID]
config.SALES_APPROVER_IDS = [SID, VAISHNAVI]
config.SALES_FINAL_SAY_ID = SID
config.ROSTER_DISPLAY_NAMES = {str(SID): "Sid", str(VAISHNAVI): "Vaishnavi",
                               str(KUSHAL): "Kushal"}
config.SALES_DMS_ENABLED = False

failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"    {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def item(rule_id, trigger, owner, company, *, counts=True, dayof="", extra=None):
    band = nextaction.RULE_BANDS.get(trigger, nextaction.P_CHASE)
    out = {
        "rule": trigger, "rule_id": rule_id, "type": trigger,
        "rule_name": nextaction.TYPE_LABELS.get(trigger, trigger),
        "label": nextaction.TYPE_LABELS.get(trigger, trigger),
        "owner": owner, "priority": band,
        "priority_label": nextaction.BAND_LABELS[band],
        "due_date": MONDAY, "due_iso": dl.iso(MONDAY), "overdue_days": 0,
        "company": company, "poc": "", "poc_designation": "", "sheet_row": 2,
        "row_key": f"{company.lower()}|", "contact_key": f"{company.lower()}|",
        "max_items_per_post": config.DRIP_MAX_ITEMS_PER_POST,
        "counts_toward_cap": counts, "destination": "channel",
        "web_pending": False, "why": "seeded", "text": f"{company} — seeded",
        "key": f"{rule_id}:{company.lower()}",
    }
    if dayof:
        out["dayof_time"] = dayof
    out.update(extra or {})
    return out


def main() -> int:
    global failures
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")

    actions = [
        item("R1", nextaction.R_AI_NEWS, "Vaishnavi", "the AI news"),
        item("R4", nextaction.R_DELIVERABLES, "Sid", "MSA"),
        item("R7", nextaction.R_DM_NO_MEETING, "Kushal", "London Met"),
        item("R10", nextaction.R_CLOSURE_SUPPORT, "Vaishnavi", "ElevenLabs"),
        item("R8", nextaction.R_MEETING_PREP, "Vaishnavi", "Wispr Flow",
             counts=False, dayof=config.MEETING_DAYOF_TIME),
        item("R8", nextaction.R_MEETING_PREP, "Kushal", "PolyAI", counts=False),
        item("R9", nextaction.R_MEETING_FOLLOWUP, "Sid", "Acme", counts=False,
             extra={"rung": 3, "rung_destination": "escalation",
                    "delivered_to": "channel", "addressed_to": "Sid"}),
    ]

    planned = drip.plan(actions, day=MONDAY)
    messages = planned["messages"]
    cap = config.message_cap_for(MONDAY)
    counted = [m for m in messages if m.get("counts_toward_cap", True)]

    print("=" * 78)
    print("HEAVY MONDAY — 4 rule posts + 2 R8 + 1 R9 + the approvals sweep")
    print("=" * 78)
    print(f"  window          {config.SALES_DRIP_START} - {config.SALES_DRIP_END} IST")
    print(f"  gap             {config.MESSAGE_GAP_MINUTES} min, floor "
          f"{config.MESSAGE_GAP_MIN_MINUTES}")
    gap, fits = drip.fitted_gap(len(messages))
    print(f"  fitted gap      {gap} min ({fits} post(s) fit)")
    print(f"  cap for Monday  {cap}   ({len(counted)} counted, "
          f"{len(messages) - len(counted)} outside)")
    print()

    for m in sorted(messages, key=lambda x: x["send_at"]):
        flag = "" if m.get("counts_toward_cap", True) else "  [outside the cap]"
        dayof = "  [day-of, fixed time]" if m.get("dayof_time") else ""
        print(f"  {m['send_at_hhmm']}  {m['rule_id']:<4} {m['rule_name']:<28} "
              f"-> {m['owner'] or '-':<10}{flag}{dayof}")

    # --- the sweep, which rides the first slot and takes no cap slot --------
    f = os.path.join(tempfile.gettempdir(), "verify_heavy.db")
    if os.path.exists(f):
        os.remove(f)
    d = dbmod.DB(f)
    for name, co in (("p1", "Wispr Flow"), ("p2", "Acme")):
        d.open_proposal(
            proposal_key=name, kind="cell_update", tab="Outreach PoCs", sheet_row=2,
            row_key=f"{co.lower()}|x", company=co, poc="",
            payload={"writes": {"meeting_date": "24-09-2026"}},
            reply_text="met them", trigger="reply",
            proposed_text=f"Shall I set Meeting Date for {co}? Reply yes.",
            requested_by="Trishi", channel_id=1, message_id=f"m-{name}",
            created_at="2026-09-18T14:00:00",
        )
    nudge_before = dl.iso(dl.subtract_working_days(MONDAY,
                                                  config.PROPOSAL_NUDGE_AFTER_DAYS))
    pending = [p for p in d.stale_proposals(before_iso=nudge_before, nudged=False) if p]
    sweep = drip.with_tags(approvals.pending_text(pending))
    # The sweep rides the first WINDOWED slot, not the day-of touch —
    # that one fires at 10:00 and is a different thing entirely.
    first = min(m["send_at_hhmm"] for m in messages if not m.get("dayof_time"))
    print(f"  {first}  —    Pending approvals (rides slot 1, no cap slot)")
    print()
    for line in sweep.splitlines():
        print("  | " + line)
    print()

    # --- checks ------------------------------------------------------------
    print("=" * 78)
    print("  CHECKS")
    print("=" * 78)
    eh, em = config.drip_end_ist()
    end = eh * 60 + em
    late = [m for m in messages
            if (m["send_at"].hour * 60 + m["send_at"].minute) > end
            and not m.get("dayof_time")]
    check("seven posts planned", len(messages), 7)
    check("four count against the cap", len(counted), 4)
    check("three sit outside it", len(messages) - len(counted), 3)
    check(f"nothing windowed lands after {config.SALES_DRIP_END}", len(late), 0)
    check("nothing rolled", len(planned["rolled"]), 0)
    dayof = [m for m in messages if m.get("dayof_time")]
    check("the R8 day-of touch keeps its fixed time",
          dayof[0]["send_at_hhmm"] if dayof else None, config.MEETING_DAYOF_TIME)
    windowed = sorted(m["send_at_hhmm"] for m in messages if not m.get("dayof_time"))
    check("the first windowed post is on the start",
          windowed[0], config.SALES_DRIP_START)
    r9 = [m for m in messages if m["rule_id"] == "R9"][0]
    check("R9's escalation posts in the channel", r9["destination"], "channel")
    check("...addressed to Sid", r9["owner"], "Sid")
    body = " ".join(str(v) for v in r9.values())
    check("no message says 'would have been a DM'",
          "would have been a DM" in body or "cannot send those" in body, False)
    check("the sweep lists both proposals", sweep.count("\n  - "), 2)
    check("the sweep tags Vaishnavi and Sid",
          f"<@{VAISHNAVI}>" in sweep and f"<@{SID}>" in sweep, True)

    print()
    print("  " + ("ALL PASSED" if not failures else f"{failures} FAILED"))
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

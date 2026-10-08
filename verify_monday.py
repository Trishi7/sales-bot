"""SIMULATE ONE MONDAY and print the plan. `python verify_monday.py`

THE CHECK THE SCHEDULE CHANGE IS WRITTEN AGAINST: Monday's four rules plus two
meeting-prep items should produce SIX posts, of which only FOUR count against
the cap of 5. The four go in the order the team gave on 8 Oct (NFT2-1069) —
deliverables 14:00, DM sent no meeting 16:00, AI news 18:00, closure support
20:00 — exactly two hours apart; the two meeting posts go at 10:00, outside
that order; and every post opens by tagging Vaishnavi and Sid.

It touches no sheet, no database and no network. Nothing is sent — `drip.plan`
computes and returns, and this file only prints what it returned.
"""
import logging
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import os as _os  # noqa: E402  NFT2-1064 offline guard: canned source statuses, real Sheets/Drive calls blocked
import sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "tests"))
import offline_guard  # noqa: E402
GUARD = offline_guard.install_script()
import config
import deadlines as dl
import drip
import nextaction

# Two people on the roster, both always tagged. Real ids are irrelevant here —
# what matters is that the roster gate authorises them, because `mention_for`
# refuses to ping anybody it does not find in TEAM_ROSTER_IDS.
VAISHNAVI, SID, KUSHAL = 1001, 1002, 1003
config.TEAM_ROSTER_IDS = [VAISHNAVI, SID, KUSHAL]
config.SALES_ALWAYS_TAG_IDS = [VAISHNAVI, SID]
config.ROSTER_DISPLAY_NAMES = {
    str(VAISHNAVI): "Vaishnavi", str(SID): "Sid", str(KUSHAL): "Kushal",
}


# THE SCHEDULE IS PINNED TO THE SHIPPED DEFAULTS (NFT2-1069), not read from
# this machine's .env: the day these checks describe is the one the team was
# given, and a laptop with its own gap, cap or window must not change it.
config.SALES_DRIP_START, config.SALES_DRIP_END = "14:00", "20:00"
config.MESSAGE_GAP_MINUTES = config.MESSAGE_GAP_MIN_MINUTES = 120
config.MESSAGE_JITTER_MINUTES = 0
config.DAILY_MESSAGE_CAP = 5
config.MEETING_DAYOF_TIME, config.NEXT_STEP_TIME = "10:00", "15:00"

MONDAY = date(2026, 9, 21)


def item(rule_id, trigger, owner, company, *, counts=True, due=MONDAY, overdue=0,
         dayof=""):
    band = nextaction.RULE_BANDS.get(trigger, nextaction.P_CHASE)
    return {
        # A FIXED-TIME ITEM CARRIES ITS TIME, as `nextaction` sets it on every
        # meeting-prep touch.
        **({"dayof_time": dayof} if dayof else {}),
        "rule": trigger, "rule_id": rule_id, "type": trigger,
        "rule_name": nextaction.TYPE_LABELS.get(trigger, trigger),
        "label": nextaction.TYPE_LABELS.get(trigger, trigger),
        "owner": owner, "priority": band,
        "priority_label": nextaction.BAND_LABELS[band],
        "due_date": due, "due_iso": dl.iso(due), "overdue_days": overdue,
        "company": company, "poc": "", "poc_designation": "", "sheet_row": 2,
        "row_key": f"{company.lower()}|", "contact_key": f"{company.lower()}|",
        "max_items_per_post": config.DRIP_MAX_ITEMS_PER_POST,
        "counts_toward_cap": counts, "destination": "channel",
        "web_pending": False, "why": "seeded for the simulation",
        "text": f"{company} — seeded", "key": f"{rule_id}:{company.lower()}",
    }


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    # FOUR SCHEDULED RULES that count against the cap, and TWO meeting-prep
    # items that do not. R8's two items are for different owners, so the
    # grouping rule (one group per rule x owner) makes them two posts.
    actions = [
        item("R1", nextaction.R_AI_NEWS, "Vaishnavi", "the AI news"),
        item("R4", nextaction.R_DELIVERABLES, "Sid", "MSA", overdue=2),
        item("R7", nextaction.R_DM_NO_MEETING, "Kushal", "ElevenLabs", overdue=7),
        item("R10", nextaction.R_CLOSURE_SUPPORT, "Vaishnavi", "PolyAI"),
        item("R8", nextaction.R_MEETING_PREP, "Vaishnavi", "Acme", counts=False,
             dayof=config.MEETING_DAYOF_TIME),
        item("R8", nextaction.R_MEETING_PREP, "Kushal", "Borealis", counts=False,
             dayof=config.MEETING_DAYOF_TIME),
    ]

    planned = drip.plan(actions, day=MONDAY)
    messages = planned["messages"]
    cap = config.message_cap_for(MONDAY)
    counted = [m for m in messages if m.get("counts_toward_cap", True)]
    uncounted = [m for m in messages if not m.get("counts_toward_cap", True)]

    print("=" * 78)
    print("MONDAY SIMULATION — 4 scheduled rules + 2 meeting-prep items")
    print("=" * 78)
    print(f"  date              {dl.iso(MONDAY)} ({MONDAY.strftime('%A')})")
    print(f"  SALES_DRIP_START  {config.SALES_DRIP_START} IST")
    print(f"  cap for Monday    {cap}   (DAILY_MESSAGE_CAP, every weekday)")
    print(f"  SALES_DRIP_END    {config.SALES_DRIP_END} IST")
    print(f"  gap               {drip.gap_minutes()} min, never less "
          f"(jitter adds up to {config.MESSAGE_JITTER_MINUTES})")
    print(f"  items per post    {config.DRIP_MAX_ITEMS_PER_POST}")
    print()
    print(f"  POSTS             {len(messages)}")
    print(f"    against the cap {len(counted)}")
    print(f"    outside it      {len(uncounted)}   (R8/R9 — a meeting does not wait)")
    print(f"  rolled            {len(planned['rolled'])}")
    print(f"  held              {len(planned['held'])}")
    print()

    for m in messages:
        owner = m.get("owner") or ""
        tags = drip.tag_prefix(
            owner_id=config.roster_id_for_name(owner), owner_name=owner,
        )
        names = " ".join(
            config.ROSTER_DISPLAY_NAMES.get(t.strip("<@>"), t)
            for t in tags.split()
        )
        print(f"  slot {m['slot']}  {m['send_at_hhmm']}  {m['rule_id']:<4} "
              f"{m['rule_name']:<28} -> {owner:<10} "
              f"{'' if m.get('counts_toward_cap', True) else '[outside the cap]'}")
        print(f"          tags: {tags}   ({names})")

    print()
    print("  Sample message body, as it would go out:")
    print("  " + "-" * 74)
    sample = messages[0]
    body = drip.compose_fallback(sample, address="")
    full = drip.with_tags(
        body,
        owner_id=config.roster_id_for_name(sample.get("owner") or ""),
        owner_name=sample.get("owner") or "",
    )
    for line in full.splitlines():
        print("  | " + line)
    print("  " + "-" * 74)
    print()

    # ---- the assertions this file exists for -----------------------------
    checks = [
        ("six posts", len(messages), 6),
        ("four count against the cap", len(counted), 4),
        ("two sit outside it", len(uncounted), 2),
        ("the first spaced post lands on SALES_DRIP_START",
         [m["send_at_hhmm"] for m in messages if not m["pinned"]][0],
         config.SALES_DRIP_START),
        ("the four rules go in Monday's order, two hours apart",
         [(m["rule_id"], m["send_at_hhmm"]) for m in messages if not m["pinned"]],
         [("R4", "14:00"), ("R7", "16:00"), ("R1", "18:00"), ("R10", "20:00")]),
        ("the two meeting-prep posts go at 10:00, outside that order",
         [(m["rule_id"], m["send_at_hhmm"], m["pinned"]) for m in messages
          if m["pinned"]], [("R8", "10:00", True), ("R8", "10:00", True)]),
        ("...and the day is the same with them or without them",
         [(m["rule_id"], m["send_at_hhmm"])
          for m in drip.plan(actions[:4], day=MONDAY)["messages"]],
         [(m["rule_id"], m["send_at_hhmm"]) for m in messages if not m["pinned"]]),
        ("nothing lands after SALES_DRIP_END",
         [m["send_at_hhmm"] for m in messages
          if m["send_at"] > drip.window_end(MONDAY)], []),
        ("nothing rolled", len(planned["rolled"]), 0),
        ("every post tags Vaishnavi",
         all(f"<@{VAISHNAVI}>" in drip.tag_prefix(
             owner_id=config.roster_id_for_name(m.get("owner") or ""),
             owner_name=m.get("owner") or "") for m in messages), True),
        ("every post tags Sid",
         all(f"<@{SID}>" in drip.tag_prefix(
             owner_id=config.roster_id_for_name(m.get("owner") or ""),
             owner_name=m.get("owner") or "") for m in messages), True),
        ("the tags come FIRST", full.splitlines()[0].startswith("<@"), True),
        ("Kushal's posts also tag him",
         all(f"<@{KUSHAL}>" in drip.tag_prefix(
             owner_id=config.roster_id_for_name("Kushal"), owner_name="Kushal")
             for m in messages if m.get("owner") == "Kushal"), True),
    ]

    # Spacing: between two SPACED posts the gap is the full gap, never less.
    # The fixed-time posts are not part of it.
    times = [m["send_at"] for m in messages if not m["pinned"]]
    gaps = [int((b - a).total_seconds() // 60) for a, b in zip(times, times[1:])]
    floor = drip.min_gap_minutes()
    checks.append(("every gap between spaced posts is exactly 120 minutes",
                   gaps, [120, 120, 120]))
    checks.append(("...which is the floor the sender's catch-up guard uses",
                   floor, 120))

    # WITH NO DELIVERABLES, AND THEN WITH NO R7 EITHER: the rest move up.
    def order_of(chosen):
        return [(m["rule_id"], m["send_at_hhmm"])
                for m in drip.plan(chosen, day=MONDAY)["messages"]]

    by_rule = {a["rule_id"]: a for a in actions[:4]}
    checks += [
        ("no deliverables this week: R7 14:00, R1 16:00, R10 18:00",
         order_of([by_rule["R1"], by_rule["R7"], by_rule["R10"]]),
         [("R7", "14:00"), ("R1", "16:00"), ("R10", "18:00")]),
        ("nobody for R7 (rule 13 covers them all): R4, R1, R10 at 14, 16, 18",
         order_of([by_rule["R10"], by_rule["R1"], by_rule["R4"]]),
         [("R4", "14:00"), ("R1", "16:00"), ("R10", "18:00")]),
        ("only AI news is due: it goes at 14:00",
         order_of([by_rule["R1"]]), [("R1", "14:00")]),
    ]

    print(f"  send times   {', '.join(m['send_at_hhmm'] for m in messages)}")
    print(f"  gaps (min)   {gaps}   floor {floor}")
    print()

    # ---- the weekend, checked from the same plan function ----------------
    SAT, SUN = date(2026, 9, 26), date(2026, 9, 27)
    deliverable_due_mon = item("R4", nextaction.R_DELIVERABLES, "Sid", "MSA",
                               due=SUN + __import__("datetime").timedelta(days=1))
    deliverable_due_thu = item("R4", nextaction.R_DELIVERABLES, "Sid", "Website",
                               due=SUN + __import__("datetime").timedelta(days=4))
    news = item("R1", nextaction.R_AI_NEWS, "Vaishnavi", "the AI news")

    sat = drip.plan([deliverable_due_mon, news], day=SAT)
    sun_due = drip.plan([deliverable_due_mon, news], day=SUN)
    sun_not_due = drip.plan([deliverable_due_thu, news], day=SUN)

    print("  weekend")
    print(f"    Saturday                      {len(sat['messages'])} post(s)")
    print(f"    Sunday, deliverable due Mon   {len(sun_due['messages'])} post(s)"
          + (f" at {sun_due['messages'][0]['send_at'].strftime('%H:%M')}"
             if sun_due['messages'] else ""))
    print(f"    Sunday, nothing due Mon       {len(sun_not_due['messages'])} post(s)")
    print()

    checks += [
        ("Saturday is silent", len(sat["messages"]), 0),
        ("Sunday sends one post when a deliverable is due Monday",
         len(sun_due["messages"]), 1),
        ("...and it is the R4 one, not the news",
         sun_due["messages"][0]["rule_id"] if sun_due["messages"] else None, "R4"),
        ("...at SALES_DRIP_START",
         sun_due["messages"][0]["send_at"].strftime("%H:%M")
         if sun_due["messages"] else None, config.SALES_DRIP_START),
        ("Sunday is silent when nothing is due Monday",
         len(sun_not_due["messages"]), 0),
    ]

    failures = 0
    for name, got, want in checks:
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    print()
    print("  " + ("ALL PASSED" if not failures else f"{failures} FAILED"))
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

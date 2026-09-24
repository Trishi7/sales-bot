"""EVENTS & SUMMITS, and the weekly funnel line — two extra things the drip carries.

Both produce ACTIONS in the same shape `nextaction.py` emits, so the drip groups,
ranks, spaces and caps them exactly like everything else. That is the whole point
of putting them here rather than giving either its own send path: this bot has
one proactive outlet and a hard cap on how often it speaks, and a feature that
routed around either would be re-introducing the problem the drip was built to
solve.

EVENTS — ONE REMINDER EACH, AT T-EVENT_LEAD_DAYS, FOREVER.

    A conference the team has already decided about does not need reminding
    twice, and "we mentioned it in March" is not a reason to mention it again in
    April. The dedup is a permanent SQLite row (`event_reminders`), keyed on the
    event's name AND its date — so an event that MOVES earns a fresh reminder,
    because the new date is new information, while re-reading the same row
    tomorrow does not.

    T-20 is the plan's number: far enough out that a booth, a talk slot or a
    flight is still bookable, close enough that it is not immediately forgotten
    again.

THE WEEKLY FUNNEL LINE — OFF BY DEFAULT, AND OPT-IN.

    One short Friday message: leading counts, then lagging counts. Nothing else.
    A weekly number nobody asked for is the definition of a message that gets
    skimmed, and it spends one of the day's three slots — so `WEEKLY_FUNNEL_ENABLED`
    defaults to false and somebody has to want it.

    It is deliberately COUNTS ONLY. No commentary, no trend, no "up 12% on last
    week". The bot does not have enough weeks of clean data to say anything about
    a trend, and a confident sentence about noise is worse than a number.

THIS MODULE IS PURE. Rows and counts in, action dicts out. It reads no sheet
(the caller passes the rows), writes no database row, and sends nothing.
"""
import logging
from datetime import date, timedelta
from typing import Optional

import config
import deadlines as dl
import gtm_sheet
import nextaction

log = logging.getLogger(__name__)

# The two action types this module adds to the queue. They live here rather than
# in nextaction.py because they are not per-ROW actions — an event belongs to
# nobody's outreach row, and the funnel line belongs to the whole sheet.
EVENT_REMINDER = "event_reminder"
WEEKLY_FUNNEL = "weekly_funnel"

TYPE_LABELS = {
    EVENT_REMINDER: "Events coming up",
    WEEKLY_FUNNEL: "This week's numbers",
}


def event_key(row: dict) -> str:
    """The permanent dedup key for one event row: name + date."""
    name = gtm_sheet.clean_cell(row.get("event"))
    when = gtm_sheet.sheet_date(row.get("event_date"))
    return f"{gtm_sheet.normalise_header(name)}|{dl.iso(when) if when else ''}"


def due_events(
    rows: list, *, today: Optional[date] = None, already_sent=None,
) -> list:
    """Events whose single reminder is due today, as action dicts.

    "DUE" MEANS THE LEAD WINDOW HAS OPENED AND NOT CLOSED: the event is between
    today and T+EVENT_LEAD_DAYS. A window rather than an exact day because the
    drip can only speak when a slot is free, and an exact-day test would drop a
    reminder entirely on a busy Tuesday — for a once-forever message that is the
    difference between late and never.

    A PAST EVENT IS NEVER REMINDED ABOUT. Obvious, and worth stating: the sheet
    keeps last year's conferences and a bot cheerfully flagging one would be the
    clearest possible signal it cannot read a date.

    `already_sent(key)` is `db.event_reminder_sent` — passed in so this stays
    pure and testable.
    """
    today = today or dl.today_ist()
    lead = max(1, int(config.EVENT_LEAD_DAYS))
    horizon = today + timedelta(days=lead)
    out: list = []

    for row in rows or []:
        name = gtm_sheet.clean_cell(row.get("event"))
        when = gtm_sheet.sheet_date(row.get("event_date"))
        if not name or when is None:
            continue
        if when < today or when > horizon:
            continue
        key = event_key(row)
        if already_sent is not None and already_sent(key):
            continue

        days_away = (when - today).days
        where = gtm_sheet.clean_cell(row.get("location"))
        status = gtm_sheet.clean_cell(row.get("status"))
        owner = gtm_sheet.clean_cell(row.get("owner"))
        out.append({
            "type": EVENT_REMINDER,
            "label": TYPE_LABELS[EVENT_REMINDER],
            "owner": owner,
            "due_date": when - timedelta(days=lead),
            "due_iso": dl.iso(when - timedelta(days=lead)),
            "priority": nextaction.P_MEETING,
            "priority_label": nextaction.BAND_LABELS[nextaction.P_MEETING],
            "overdue_days": 0,
            # The event's NAME goes in the company slot so the drip's grouping
            # and its "companies comma-separated" sentence work unchanged — a
            # message about three summits reads exactly like one about three
            # accounts, which is the shape people already know how to answer.
            "company": name,
            "poc": "",
            "poc_designation": "",
            "sheet_row": row.get("_row"),
            "row_key": key,
            "event_key": key,
            "event_date": dl.iso(when),
            "location": where,
            "anchor": dl.iso(when),
            "anchor_label": "the event date",
            "why": (
                f"{name} is on {dl.format_date(when)}, {days_away}d away"
                + (f" in {where}" if where else "")
                + (f"; the sheet says {status!r}" if status else "")
                + f". One reminder at T-{lead}, and never again."
            ),
            "text": (
                f"{name} is {days_away} days out"
                + (f" ({where})" if where else "")
                + ". Worth deciding now if we are going — no rush if it is already "
                  "settled, just tell me and I will leave it."
            ),
            "key": f"event:{key}",
        })

    out.sort(key=lambda a: (a["due_date"], a["company"]))
    return out


def funnel_action(counts: dict, *, today: Optional[date] = None) -> Optional[dict]:
    """The one Friday numbers line, or None when it is off or not that day.

    `counts` is `tracker.funnel_metrics(...)`. Rendered as COUNTS ONLY — leading
    first, because those are the numbers still changeable this week and the
    lagging ones are last week's outcome whatever anybody does now.
    """
    today = today or dl.today_ist()
    if not config.WEEKLY_FUNNEL_ENABLED:
        return None
    if today.weekday() != max(0, min(6, config.WEEKLY_FUNNEL_WEEKDAY)):
        return None
    if not counts:
        return None

    leading = _phrase([
        ("outreach", counts.get("outreach_sent")),
        ("replies", counts.get("replies")),
        ("meetings booked", counts.get("meetings_booked")),
        ("follow-ups", counts.get("followups_done")),
    ])
    lagging = _phrase([
        ("pilots", counts.get("pilots")),
        ("paid", counts.get("paid")),
        ("repeats", counts.get("repeats")),
    ])
    if not leading and not lagging:
        return None

    body = "This week: " + (leading or "nothing recorded going out")
    if lagging:
        body += f". Landed: {lagging}"
    body += ". Nothing needed from you — just so it is written down somewhere."

    return {
        "type": WEEKLY_FUNNEL,
        "label": TYPE_LABELS[WEEKLY_FUNNEL],
        "owner": "",
        "due_date": today,
        "due_iso": dl.iso(today),
        # LAST IN THE QUEUE, ALWAYS. It is a number, not a task, and it must
        # never take a slot from a reply somebody is waiting on.
        "priority": nextaction.P_CONTEXT,
        "priority_label": nextaction.BAND_LABELS[nextaction.P_CONTEXT],
        "overdue_days": 0,
        "company": "the week",
        "poc": "",
        "poc_designation": "",
        "sheet_row": None,
        "row_key": f"funnel|{dl.iso(today)}",
        "anchor": dl.iso(today),
        "anchor_label": "this week",
        "why": f"the weekly funnel line for the week ending {dl.format_date(today)}",
        "text": body,
        "key": f"funnel:{dl.iso(today)}",
    }


def _phrase(pairs: list) -> str:
    """"12 outreach, 3 replies and 2 meetings booked" — zeros included.

    A ZERO IS A NUMBER AND IT STAYS IN. Dropping the zeros would turn a bad week
    into a short sentence and a good week into a long one, which is exactly the
    kind of quiet editorialising a counts-only line exists to avoid.
    """
    parts = [
        f"{int(value)} {label}"
        for label, value in pairs
        if value is not None
    ]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _self_test() -> int:
    """`python -m events` — the lead window, the dedup and the funnel line."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    today = date(2026, 9, 9)                  # a Wednesday
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    def ev(name, when, **kw):
        row = {"_row": 2, "event": name, "event_date": when, "_extra": {}}
        row.update(kw)
        return row

    rows = [
        ev("NeurIPS", "25-09-2026"),          # 16d away — inside T-20
        ev("SaaStr", "09-09-2026"),           # today — inside
        ev("Web Summit", "20-11-2026"),       # 72d away — outside
        ev("Old Summit", "01-08-2026"),       # past
        ev("", "25-09-2026"),                 # no name — a spacer
    ]

    print("the lead window")
    due = due_events(rows, today=today)
    check("only events inside T-20 and not past",
          sorted(a["company"] for a in due), ["NeurIPS", "SaaStr"])
    check("the reminder is dated T-minus the lead days",
          due[0]["due_iso"] if due[0]["company"] == "SaaStr" else due[1]["due_iso"],
          dl.iso(date(2026, 9, 9) - timedelta(days=20)))

    print("\nonce, forever")
    seen = {event_key(rows[0])}
    due2 = due_events(rows, today=today, already_sent=lambda k: k in seen)
    check("an already-reminded event is gone",
          [a["company"] for a in due2], ["SaaStr"])
    moved = ev("NeurIPS", "28-09-2026")       # same name, new date (still inside T-20)
    check("...but a MOVED event earns a fresh one",
          bool(due_events([moved], today=today, already_sent=lambda k: k in seen)), True)

    print("\nthe message")
    text = due[0]["text"]
    check("no bullets or headers", any(c in text for c in ("**", "- ", "\\u2022")), False)
    check("gives an out", "no rush" in text, True)

    print("\nthe weekly funnel line")
    counts = {"outreach_sent": 12, "replies": 3, "meetings_booked": 2,
              "followups_done": 8, "pilots": 1, "paid": 0, "repeats": 0}
    check("off by default", funnel_action(counts, today=date(2026, 9, 11)), None)
    config.WEEKLY_FUNNEL_ENABLED = True
    check("not on a Wednesday", funnel_action(counts, today=today), None)
    line = funnel_action(counts, today=date(2026, 9, 11))     # a Friday
    check("on its Friday", bool(line), True)
    check("counts only, zeros included", "0 paid" in line["text"], True)
    check("last in the queue", line["priority"], nextaction.P_CONTEXT)
    check("no bullets", any(c in line["text"] for c in ("**", "- ")), False)
    config.WEEKLY_FUNNEL_ENABLED = False

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

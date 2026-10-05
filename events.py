"""THE WEEKLY FUNNEL LINE — one extra thing the drip carries.

It produces an ACTION in the same shape `nextaction.py` emits, so the drip
groups, ranks, spaces and caps it exactly like everything else. That is the
whole point of putting it here rather than giving it its own send path: this
bot has one proactive outlet and a hard cap on how often it speaks.

EVENTS ARE NO LONGER HERE. This module used to hold a second events lane as
well — `due_events`, ONE reminder per event at T-EVENT_LEAD_DAYS (20), for
ever — running beside R3, which read the same tab on its own schedule. Two
paths over one tab meant an event could be announced by one, ignored by the
other, and reminded about by neither when it mattered. R3 is the one events
path now (`nextaction._r_events`, every Wednesday, a 14-day window), and
EVENT_LEAD_DAYS and EVENTS_ENABLED are retired with the lane.

THE WEEKLY FUNNEL LINE — OFF BY DEFAULT, AND OPT-IN.

    One short Friday message: leading counts, then lagging counts. Nothing else.
    A weekly number nobody asked for is the definition of a message that gets
    skimmed, and it spends one of the day's counted posts — so
    `WEEKLY_FUNNEL_ENABLED` defaults to false and somebody has to want it.

    It is deliberately COUNTS ONLY. No commentary, no trend, no "up 12% on last
    week". The bot does not have enough weeks of clean data to say anything about
    a trend, and a confident sentence about noise is worse than a number.

THIS MODULE IS PURE. Counts in, an action dict out. It reads no sheet, writes
no database row, and sends nothing.
"""
import logging
from datetime import date, timedelta
from typing import Optional

import config
import deadlines as dl
import gtm_sheet
import nextaction

log = logging.getLogger(__name__)

# The action type this module adds to the queue. It lives here rather than in
# nextaction.py because it is not a per-ROW action: the funnel line belongs to
# the whole sheet.
WEEKLY_FUNNEL = "weekly_funnel"

TYPE_LABELS = {
    WEEKLY_FUNNEL: "This week's numbers",
}


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
    """`python -m events` — the funnel line."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    today = date(2026, 9, 9)                  # a Wednesday
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

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

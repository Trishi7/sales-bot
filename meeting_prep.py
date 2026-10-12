"""R8, MEETING PREPARATION: who is named today, on which touch, and what a
reply asking for a reminder means. PURE: no sheet, no database, no Discord.

WHAT IT READS. ONE COLUMN: Meeting Date on Outreach PoCs, on every row that has
one. No activation gate, no Meeting Status, no stage, no package. So a meeting
marked Completed or cancelled with a date still to come is still chased, and a
row nobody has contacted yet qualifies: both were accepted on 12 Oct 2026, and
a status check must not creep back in here.

THE TOUCHES DECIDE WHEN ANYTHING IS SAID. MEETING_PREP_DAYS_BEFORE (5 and 3)
and the day itself. The 7-day window is only the read filter: a meeting 7, 6,
4, 2 or 1 days out says nothing that day.

IT HAS A MEMORY NOW (`db.meeting_touches`, `db.meeting_dates_seen`), handed in
as `state`:
  - a touch that went out for (contact, date) never goes out again, so a late
    restart cannot repeat it and a date typed in after the day's post still
    gets its touch at the next run;
  - a NEW date has no touches on record, so a rescheduled meeting gets fresh
    ones and the old date's are not repeated;
  - a date that MOVED so close that both earlier touches were skipped gets ONE
    catch-up touch. A meeting newly BOOKED that close gets none: it is told
    apart by the contact having had another upcoming date before this one;
  - a date nobody can read is asked for once a week, not every day.

`python -m meeting_prep` is the self-test.
"""
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

import activation
import deadlines as dl
import gtm_sheet
import packages
import sheetwrite

log = logging.getLogger("sales-bot.meeting_prep")

# The read filter: a meeting further out than this is not looked at.
WINDOW_DAYS = 7
# How often a date nobody can read is asked for.
ASK_EVERY_DAYS = 7
# How far back the news line looks in what R1 has stored.
NEWS_DAYS = 14

TOUCH_DAY = "0"
TOUCH_CATCHUP = "catchup"
TOUCH_UNREADABLE = "unreadable"

# The two posts. Today's meetings are ALWAYS their own post.
POST_TODAY = "today"
POST_PREP = "prep"

# A reminder somebody asked for under a meeting post: its row key in
# `scheduled_reminders` is "meeting|<contact key>|<meeting date>".
REMINDER_PREFIX = "meeting|"


def cell(row: dict) -> str:
    """The Meeting Date cell as text, "" when there is nothing in it. A cell
    holding only punctuation ("-", "—") is an empty cell, not a date nobody
    could read."""
    text = gtm_sheet.clean_cell((row or {}).get("meeting_date"))
    return text if re.search(r"[A-Za-z0-9]", text) else ""


def company_key(company) -> str:
    """Two people at one company share this, however the cell is cased."""
    return gtm_sheet.normalise_header(str(company or ""))


def _iso(value) -> Optional[date]:
    return dl.parse_date(str(value or "")) if str(value or "").strip() else None


def moved_in(key: str, when: date, *, today: date, seen: dict, current: set,
             nearest: int) -> bool:
    """Did this contact's meeting MOVE to `when`, so close that every earlier
    touch was skipped? (As against being booked that close in the first place,
    or the bot simply having been down on the touch's day.)

    THREE THINGS, all read from `meeting_dates_seen`:
      - when this date was first seen it was already inside the nearest touch
        (`nearest` days). Seen earlier than that, its touch day came and went:
        that touch belonged to one date and is gone, not caught up;
      - the contact had ANOTHER date on record that the sheet no longer shows;
      - and that other date was still to come when this one appeared. A
        meeting from last March is history, not a date that moved.
    """
    mine = (seen or {}).get(key) or {}
    first = _iso(mine.get(dl.iso(when))) or today
    if (when - first).days >= nearest:
        return False
    for other_iso in mine:
        other = _iso(other_iso)
        if other is None or other == when or other_iso in current:
            continue
        if other >= first:
            return True
    return False


def pick(rows, *, today: date, befores, state: Optional[dict] = None) -> dict:
    """Everything R8 has to say today, and why the rest said nothing.

        {"items":       [...]  one per line of a post, in post order
         "blank_names": [...]  rows with a Meeting Date and no Name (skipped)
         "duplicates":  [...]  the same person on the same date twice (one kept)
         "past": n, "outside": n   dates gone by, and beyond the 7 days
         "quiet": n                inside the 7 days, not on a touch today
         "done": n                 on a touch that has already gone out
         "asked": n                unreadable, asked for inside the last week
         "dates":       [...]}     every (row_key, iso) on the sheet, to note

    AN ITEM: {"row", "row_key", "name", "company", "meeting" (a date, or None
    when unreadable), "raw", "days_out", "touch", "post", "block", "pick_order"}.
    `block` is the number of days the post says ("in 3 days"); for a catch-up
    it is the REAL number of days left.

    THE ORDER. Today's post in sheet order. The afternoon post CLOSEST FIRST
    (so 3 days before 5), then the unreadable dates. Inside a block two people
    at one company stand together, where the first of them stood, so one news
    line can follow both.

    `state` is `db.meeting_prep_state()`. None means "no memory": every touch
    due by the date is returned and nothing is caught up (a preview, a test of
    the dates alone). The sender never passes None; it posts nothing instead.
    """
    befores = sorted({int(b) for b in (befores or ()) if int(b) > 0}, reverse=True)
    nearest = min(befores) if befores else 0
    touches = (state or {}).get("touches") or {}
    seen = (state or {}).get("seen") or {}
    out = {"items": [], "blank_names": [], "duplicates": [], "past": 0, "outside": 0,
           "quiet": 0, "done": 0, "asked": 0, "dates": []}

    entries = []
    current: dict = {}
    for position, row in enumerate(rows or ()):
        raw = cell(row)
        if not raw:
            continue
        name = gtm_sheet.clean_cell(row.get("name"))
        company = gtm_sheet.clean_cell(row.get("company"))
        if not name:
            out["blank_names"].append({"sheet_row": row.get("_row"), "company": company,
                                       "meeting_date": raw})
            continue
        key = activation.row_key(row)
        when = gtm_sheet.sheet_date(raw)
        entries.append({"row": row, "row_key": key, "name": name, "company": company,
                        "meeting": when, "raw": raw, "position": position})
        if when is not None:
            current.setdefault(key, set()).add(dl.iso(when))
            out["dates"].append((key, dl.iso(when)))

    taken: set = set()
    for e in entries:
        key, when = e["row_key"], e["meeting"]
        if when is None:
            # NEVER DROPPED: named in the afternoon post, once a week.
            if (key, "") in taken:
                continue
            taken.add((key, ""))
            last = _iso((touches.get((key, "")) or {}).get(TOUCH_UNREADABLE))
            if last is not None and 0 <= (today - last).days < ASK_EVERY_DAYS:
                out["asked"] += 1
                continue
            out["items"].append({**e, "days_out": None, "touch": TOUCH_UNREADABLE,
                                 "post": POST_PREP, "block": None})
            continue
        days_out = (when - today).days
        if days_out < 0:
            out["past"] += 1
            continue
        if days_out > WINDOW_DAYS:
            out["outside"] += 1
            continue
        iso = dl.iso(when)
        if (key, iso) in taken:
            # THE SAME PERSON ON THE SAME DATE TWICE is one meeting. (The same
            # person on two DATES is two meetings, and both are listed.)
            out["duplicates"].append({"name": e["name"], "company": e["company"],
                                      "meeting_date": iso, "sheet_row": e["row"].get("_row")})
            continue
        taken.add((key, iso))
        sent = touches.get((key, iso)) or {}
        if days_out == 0:
            touch, post, block = TOUCH_DAY, POST_TODAY, 0
        elif days_out in befores:
            touch, post, block = str(days_out), POST_PREP, days_out
        elif (0 < days_out < nearest and not sent
              and moved_in(key, when, today=today, seen=seen, current=current[key],
                           nearest=nearest)):
            # ONE CATCH-UP, and only one: it is recorded like any other touch,
            # and "not sent" above is false from then on.
            touch, post, block = TOUCH_CATCHUP, POST_PREP, days_out
        else:
            out["quiet"] += 1
            continue
        if touch in sent:
            out["done"] += 1
            continue
        out["items"].append({**e, "days_out": days_out, "touch": touch, "post": post,
                             "block": block})

    out["items"] = ordered(out["items"])
    for i, item in enumerate(out["items"]):
        item["pick_order"] = i
    return out


def ordered(items) -> list:
    """Post order (see `pick`): today's post, then the afternoon blocks closest
    first, then the unreadable dates; inside a block, a company's people
    together where its first person stood."""
    def block_rank(item):
        if item["post"] == POST_TODAY:
            return (0, 0)
        if item["touch"] == TOUCH_UNREADABLE:
            return (2, 0)
        return (1, int(item["block"]))

    first_at: dict = {}
    for item in sorted(items, key=lambda i: i["position"]):
        first_at.setdefault((block_rank(item), company_key(item["company"]) or item["row_key"]),
                            item["position"])
    return sorted(items, key=lambda i: (
        block_rank(i),
        first_at[(block_rank(i), company_key(i["company"]) or i["row_key"])],
        i["position"]))


def sent_today(state: Optional[dict], today: date, post: str) -> int:
    """How many of `post`'s touches have gone out today. A second post of the
    same kind on one day (a date typed in after the first had gone) is told
    apart from the first by this number: see `drip.group_key`."""
    marker = dl.iso(today)
    n = 0
    for (_key, _iso_date), sent in ((state or {}).get("touches") or {}).items():
        for touch, on in (sent or {}).items():
            if on == marker and ((touch == TOUCH_DAY) == (post == POST_TODAY)):
                n += 1
    return n


# -- a reply asking for a reminder ----------------------------------------------

# The close of both posts offers one ("another nudge before it starts", "remind
# you again nearer the day"), so these are the words that take the offer up. A
# bare "yes" is NOT one: the same close asks "are we all set?", and "yes" is
# the answer to that.
_WANTS_RE = re.compile(r"\b(?:remind(?:er|ers|ed|ing)?|nudge|ping|heads[\s-]?up|buzz|alert)\b",
                       re.I)
_EVERYONE_RE = re.compile(r"\b(?:all(?:\s+of\s+(?:them|these|those))?|both|each|every(?:one|body)?)\b",
                          re.I)
_BEFORE_RE = re.compile(
    r"\b(?:(?:the|a|one|1)\s+day\s+before|(\d{1,2})\s+days?\s+before|"
    r"(?:the\s+)?(?:day|morning|evening|night)\s+before)\b", re.I)
_ON_THE_DAY_RE = re.compile(r"\b(?:on\s+the\s+day|(?:the\s+)?(?:day|morning)\s+of|same\s+day)\b",
                            re.I)
_TIME_RE = re.compile(
    r"\b(\d{1,2}\s*[:.]\s*\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?|\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?)|"
    r"noon|midday|morning|afternoon|evening|tonight|eod|end\s+of\s+day)\b", re.I)
# What is a date or a time, so a digit inside one is not read as a person's
# number: "2" in "2 days before", "24" in "24 Oct", "3" in "3pm".
_NOT_A_NUMBER = (
    _BEFORE_RE, _TIME_RE,
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?\b"),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{packages._MONTH}\b", re.I),
    re.compile(rf"\b{packages._MONTH}\b\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b", re.I),
    re.compile(r"\bin\s+\d{1,3}\s*(?:days?|weeks?|hours?|hrs?|minutes?|mins?)\b", re.I),
    re.compile(r"\b\d{1,3}\s*(?:days?|weeks?|hours?|hrs?|minutes?|mins?)\b", re.I),
    re.compile(r"\b(?:the\s+)?\d{1,2}(?:st|nd|rd|th)\b", re.I),
)
_HONORIFICS = {"dr", "mr", "mrs", "ms", "prof", "sir", "shri", "smt"}


def wants_reminder(text) -> bool:
    return bool(_WANTS_RE.search(str(text or "")))


def _numbers(text: str, count: int) -> list:
    """The people a reply picks BY NUMBER ("2", "#2 and 3"), 1..count."""
    body = str(text or "")
    for pattern in _NOT_A_NUMBER:
        body = pattern.sub(lambda m: " " * len(m.group(0)), body)
    found = []
    for m in re.finditer(r"(?<![\w.])#?(\d{1,2})(?![\w:])", body):
        n = int(m.group(1))
        if 1 <= n <= count and n not in found:
            found.append(n)
    return found


def people_named(text, people) -> list:
    """Which of a post's `people` a reply means, as they are ordered on it.

    BY NUMBER (the number in front of the name on that post), or BY NAME: the
    person's name or a run of it, the company, or the first name alone when
    nobody else on the post shares it. "all", "both" or "each" is everybody. A
    post that named ONE person needs neither. ONLY THE PEOPLE ON THAT POST can
    be matched: [] when the reply names none of them.
    """
    people = list(people or ())
    if not people:
        return []
    body = str(text or "")
    picked: list = []

    def add(person):
        if person not in picked:
            picked.append(person)

    by_n = {int(p.get("n") or 0): p for p in people}
    for n in _numbers(body, max(by_n) if by_n else 0):
        if n in by_n:
            add(by_n[n])
    names = {str(p.get("poc") or ""): p for p in people if str(p.get("poc") or "").strip()}
    for _pos, name in packages.match_names(body, list(names)):
        add(names[name])
    companies: dict = {}
    for p in people:
        if str(p.get("company") or "").strip():
            companies.setdefault(str(p["company"]), []).append(p)
    for _pos, company in packages.match_names(body, list(companies)):
        for p in companies[company]:
            add(p)
    # A FIRST NAME ALONE ("remind me about Ada"), when it is nobody else's.
    said = set(re.findall(r"[a-z0-9]+", body.lower()))
    firsts: dict = {}
    for p in people:
        words = [w for w in re.findall(r"[a-z0-9]+", str(p.get("poc") or "").lower())
                 if w not in _HONORIFICS]
        if words and len(words[0]) >= 2:
            firsts.setdefault(words[0], []).append(p)
    for first, owners in firsts.items():
        if first in said and len(owners) == 1:
            add(owners[0])
    if not picked and _EVERYONE_RE.search(body):
        picked = list(people)
    if not picked and len(people) == 1:
        picked = list(people)
    return sorted(picked, key=lambda p: int(p.get("n") or 0))


def reminder_when(text, *, meeting: Optional[date], now: datetime,
                  default_time: str) -> dict:
    """WHEN a reply wants its reminder, for a meeting on `meeting`:

        {"date": date|None, "time": "HH:MM"|"", "why": ""|"none"|"past"|"after"}

    THE DAY: "the day before", "2 days before" and "on the day" count from the
    meeting; anything else is read as people type it (`packages.dates_in`:
    "Thursday", "tomorrow", "24 Oct", "the 24th", day-first). THE TIME: "3pm",
    "15:30", "morning" (`sheetwrite.parse_reminder_time`). A time with no day
    is today. A day with no time is `default_time`.

    `why` says what is wrong, and then nothing is booked:
      "none"   no day and no time could be read;
      "past"   that moment has already gone;
      "after"  it falls after the meeting's day, or the meeting has been.
    """
    body = str(text or "")
    today = now.date()
    out = {"date": None, "time": "", "why": ""}
    if meeting is not None and meeting < today:
        return {**out, "why": "after"}
    clock = ""
    scrubbed = body
    for m in _TIME_RE.finditer(body):
        got = sheetwrite.parse_reminder_time(m.group(1))
        if got:
            clock = got
            scrubbed = body[:m.start()] + " " * len(m.group(0)) + body[m.end():]
            break
    when: Optional[date] = None
    before = _BEFORE_RE.search(body)
    if before and meeting is not None:
        when = meeting - timedelta(days=int(before.group(1) or 1))
    elif _ON_THE_DAY_RE.search(body) and meeting is not None:
        when = meeting
    else:
        found = packages.dates_in(scrubbed, today=today)
        when = found[0][1] if found else None
    if when is None and not clock:
        return {**out, "why": "none"}
    if when is None:
        when = today
    hhmm = clock or default_time
    out.update(date=when, time=hhmm)
    if meeting is not None and when > meeting:
        return {**out, "why": "after"}
    if when < today or (when == today and hhmm <= now.strftime("%H:%M")):
        return {**out, "why": "past"}
    return out


def reminder_key(row_key: str, meeting_iso: str) -> str:
    return f"{REMINDER_PREFIX}{row_key}|{meeting_iso}"


def parse_reminder_key(key: str) -> tuple:
    """("<contact key>", "<meeting date>") from a reminder's row key, or
    ("", "") when it is not a meeting's. The contact key itself holds a "|"
    (company|name), so the date is what follows the LAST one."""
    text = str(key or "")
    if not text.startswith(REMINDER_PREFIX):
        return "", ""
    rest = text[len(REMINDER_PREFIX):]
    contact, _sep, iso = rest.rpartition("|")
    return contact, iso


def reanchor(*, meeting_iso: str, due_iso: str, current, today: date) -> dict:
    """What becomes of a reminder set for a meeting on `meeting_iso`, now that
    the sheet shows `current` (that contact's readable Meeting Dates):

        {"action": "keep"}                       the date is unchanged
        {"action": "move", "meeting", "due"}     the meeting moved: same number
                                                 of days before the new date
        {"action": "close"}                      no meeting still to come

    THE SAME DISTANCE BEFORE THE NEW DATE: a reminder for the day before stays
    a reminder for the day before. Where that day has already gone (the
    meeting moved to tomorrow) it is today, and never after the meeting.
    """
    dates = sorted({d for d in (current or ()) if d is not None})
    old = _iso(meeting_iso)
    if old is not None and old in dates:
        return {"action": "keep"}
    upcoming = [d for d in dates if d >= today]
    if not upcoming:
        return {"action": "close"}
    new = upcoming[0]
    due = _iso(due_iso)
    gap = max(0, (old - due).days) if (old is not None and due is not None) else 0
    return {"action": "move", "meeting": new, "due": min(new, max(today, new - timedelta(days=gap)))}


# -- self-test ------------------------------------------------------------------

def _self_test() -> int:
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))

    today = date(2026, 10, 12)                       # a Monday

    def row(n, name, company, when, **more):
        return {"_row": n, "name": name, "company": company, "meeting_date": when, **more}

    def day(n):
        return (today + timedelta(days=n)).strftime("%d-%m-%Y")

    def touches(rows, state=None, on=today):
        return [(i["name"], i["touch"]) for i in pick(rows, today=on, befores=[5, 3],
                                                      state=state)["items"]]

    print("who is picked")
    check("5, 3 and 0 days out are touches",
          touches([row(2, "A", "X", day(5)), row(3, "B", "Y", day(3)), row(4, "C", "Z", day(0))]),
          [("C", "0"), ("B", "3"), ("A", "5")])
    check("7, 6, 4, 2 and 1 days out say nothing",
          touches([row(i, f"P{i}", "X", day(i)) for i in (7, 6, 4, 2, 1)]), [])
    check("a past date and one beyond the window say nothing",
          touches([row(2, "A", "X", day(-1)), row(3, "B", "Y", day(8))]), [])
    check("no status and no activation is read",
          touches([row(2, "A", "X", day(3), meeting_status="Completed", deal_status="Lost")]),
          [("A", "3")])
    got = pick([row(2, "", "X", day(3))], today=today, befores=[5, 3])
    check("a blank name is skipped and reported", (got["items"], len(got["blank_names"])), ([], 1))
    got = pick([row(2, "A", "X", day(3)), row(3, "A", "X", day(3)), row(4, "A", "X", day(5))],
               today=today, befores=[5, 3])
    check("the same person twice on one date is one line; on two dates, two",
          ([(i["name"], i["touch"]) for i in got["items"]], len(got["duplicates"])),
          ([("A", "3"), ("A", "5")], 1))
    check("a punctuation-only cell is an empty cell", touches([row(2, "A", "X", "-")]), [])

    print("\nthe memory")
    key = activation.row_key(row(2, "A", "X", ""))
    iso3 = dl.iso(today + timedelta(days=3))
    state = {"touches": {(key, iso3): {"3": dl.iso(today)}}, "seen": {}}
    check("a touch that went out is not repeated", touches([row(2, "A", "X", day(3))], state), [])
    check("a new date gets a fresh touch", touches([row(2, "A", "X", day(5))], state), [("A", "5")])
    check("an unreadable date is named", touches([row(2, "A", "X", "next weekish")], state),
          [("A", "unreadable")])
    asked = {"touches": {(key, ""): {"unreadable": dl.iso(today - timedelta(days=3))}}, "seen": {}}
    check("...once a week, not every day", touches([row(2, "A", "X", "next weekish")], asked), [])
    asked = {"touches": {(key, ""): {"unreadable": dl.iso(today - timedelta(days=7))}}, "seen": {}}
    check("...and again a week later", touches([row(2, "A", "X", "next weekish")], asked),
          [("A", "unreadable")])

    print("\nthe catch-up")
    iso10, iso2 = dl.iso(today + timedelta(days=10)), dl.iso(today + timedelta(days=2))
    moved = {"touches": {}, "seen": {key: {iso10: dl.iso(today - timedelta(days=4)),
                                           iso2: dl.iso(today)}}}
    check("moved from 10 days out to 2: one catch-up",
          touches([row(2, "A", "X", day(2))], moved), [("A", "catchup")])
    done = {"touches": {(key, iso2): {"catchup": dl.iso(today)}}, "seen": moved["seen"]}
    check("...and never a second", touches([row(2, "A", "X", day(2))], done), [])
    check("...nor the day after",
          touches([row(2, "A", "X", day(2))], done, on=today + timedelta(days=1)), [])
    booked = {"touches": {}, "seen": {key: {iso2: dl.iso(today)}}}
    check("booked 2 days out, never moved: nothing",
          touches([row(2, "A", "X", day(2))], booked), [])
    down = {"touches": {}, "seen": {key: {iso10: dl.iso(today - timedelta(days=9)),
                                          iso2: dl.iso(today - timedelta(days=2))}}}
    check("first seen 4 days out and the bot was down on day 3: the touch is gone",
          touches([row(2, "A", "X", day(2))], down), [])
    check("with no memory nothing is caught up", touches([row(2, "A", "X", day(2))]), [])

    print("\nthe order")
    got = pick([row(2, "A", "Acme", day(5)), row(3, "B", "Beta", day(3)),
                row(4, "C", "Acme", day(3)), row(5, "D", "Beta", day(3)),
                row(6, "E", "Zed", "soon")], today=today, befores=[5, 3])
    check("closest first, a company's people together, the unreadable last",
          [i["name"] for i in got["items"]], ["B", "D", "C", "A", "E"])

    print("\nwho a reply means")
    people = [{"n": 1, "poc": "Ada Lovelace", "company": "Acme AI"},
              {"n": 2, "poc": "Sujith P", "company": "ARTPARK"},
              {"n": 3, "poc": "Dr. David Gamez", "company": "Middlesex University London"}]

    def who(text, among=people):
        return [p["n"] for p in people_named(text, among)]

    check("by number", who("remind me about 2 tomorrow"), [2])
    check("two numbers", who("nudge me on 1 and 3 the day before"), [1, 3])
    check("a number inside a date or a time is not a person",
          who("remind me on 2 Nov at 3pm"), [])
    check("'2 days before' is not person 2", who("remind me 2 days before"), [])
    check("by name", who("remind me about Sujith on Thursday"), [2])
    check("by a short first name", who("ping me about Ada tomorrow"), [1])
    check("an honorific is not a first name", who("remind me about David tomorrow"), [3])
    check("by company", who("remind me about ARTPARK tomorrow"), [2])
    check("everybody", who("remind me about all of them tomorrow"), [1, 2, 3])
    check("nobody named, several on the post", who("remind me tomorrow"), [])
    check("nobody named, one on the post", who("remind me tomorrow", people[:1]), [1])
    check("somebody who is not on the post", who("remind me about Priya tomorrow"), [])

    print("\nwhen")
    now = datetime(2026, 10, 12, 11, 0, tzinfo=dl.IST)
    meet = date(2026, 10, 15)

    def when(text, meeting=meet):
        got = reminder_when(text, meeting=meeting, now=now, default_time="14:00")
        return (dl.iso(got["date"]) if got["date"] else "", got["time"], got["why"])

    check("the day before", when("remind me the day before"), ("2026-10-14", "14:00", ""))
    check("2 days before, at a time", when("remind me 2 days before at 9am"),
          ("2026-10-13", "09:00", ""))
    check("on the day", when("nudge me on the day, 9:30 am"), ("2026-10-15", "09:30", ""))
    check("a weekday", when("remind me on Wednesday"), ("2026-10-14", "14:00", ""))
    check("a time alone is today", when("another nudge at 3pm", today), ("2026-10-12", "15:00", ""))
    check("a time already gone", when("nudge me at 9am", today), ("2026-10-12", "09:00", "past"))
    check("after the meeting", when("remind me on 20 Oct"), ("2026-10-20", "14:00", "after"))
    check("the meeting has been", when("remind me tomorrow", date(2026, 10, 9)), ("", "", "after"))
    check("no day and no time", when("yes please remind me"), ("", "", "none"))
    check("wanting one", (wants_reminder("yes, remind me"), wants_reminder("yes all set"),
                          wants_reminder("another nudge at 3")), (True, False, True))

    print("\nre-anchoring")
    check("the date is unchanged",
          reanchor(meeting_iso="2026-10-15", due_iso="2026-10-14",
                   current=[date(2026, 10, 15)], today=today), {"action": "keep"})
    check("moved later: the same distance before",
          reanchor(meeting_iso="2026-10-15", due_iso="2026-10-14",
                   current=[date(2026, 10, 17)], today=today),
          {"action": "move", "meeting": date(2026, 10, 17), "due": date(2026, 10, 16)})
    check("moved to tomorrow: never before today",
          reanchor(meeting_iso="2026-10-20", due_iso="2026-10-17",
                   current=[date(2026, 10, 13)], today=today),
          {"action": "move", "meeting": date(2026, 10, 13), "due": date(2026, 10, 12)})
    check("no meeting left",
          reanchor(meeting_iso="2026-10-15", due_iso="2026-10-14", current=[], today=today),
          {"action": "close"})
    check("the key round-trips", parse_reminder_key(reminder_key("acme ai|ada lovelace", "2026-10-15")),
          ("acme ai|ada lovelace", "2026-10-15"))

    print("\nALL PASSED" if not failures else f"\n{failures} FAILED")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if _self_test() else 0)

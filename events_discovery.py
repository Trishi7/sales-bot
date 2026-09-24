"""R3 — find events the sheet does not have, and fill in the deadlines it lacks.

TWO JOBS, BOTH ABOUT THE SAME GAP. `_r_events` in nextaction.py reads the AI
Events & Summits tab and reminds us about what is on it. That is useful and it is
only half a rule: an event nobody has typed in does not exist, and a row whose
"Last day for registration" cell is empty is a reminder that cannot tell you the
thing that actually bites — the conference is in November and registration shut
in September.

    DISCOVERY      search for events in the current and next month, skip the
                   ones already on the tab, PROPOSE the rest. On a yes, append.

    BACKFILL       for rows whose registration deadline is empty or "not
                   available", look it up — the row's own link first, then a
                   search — and propose writing just that one cell.

PERMISSION IS NOT OPTIONAL AND IT IS NOT IMPLIED. Neither job writes anything by
itself. Discovery proposes a row and waits; backfill proposes a cell and waits.
`gtm_sheet.append_row` is reachable only through `approvals`, and that is the
design rather than an accident — a bot that adds rows to a shared sheet on the
strength of a search result is a bot somebody turns off.

THE LIMITS ARE THERE BECAUSE PROPOSALS COST ATTENTION. Three per run and eight a
month, counted in SQLite. A discovery feature with no ceiling produces a weekly
listings page, and a listings page is not read — at which point the one event
that mattered is missed by exactly the same margin as if the feature did not
exist.

AND A NO IS AN ANSWER. An event somebody declined is recorded as declined and is
never proposed again. Re-asking a fortnight later is not a reminder, it is
nagging, and it teaches people to skim.

EVERY FACT CARRIES ITS LINK, the deadline included. A date the search did not
actually state is never written — "registration deadline not stated" is a real
answer and a guessed date is a wrong one that looks right.

NO I/O HERE. This module builds prompts and reads what comes back; the search is
`llm.web_research` and the write is `gtm_sheet.append_row`, both called by
bot.py. That keeps the matching, the limits and the parsing testable without a
network — see `_self_test`.
"""
import logging
import re
from datetime import date, timedelta
from typing import Optional

import config
import deadlines as dl
import gtm_sheet

log = logging.getLogger(__name__)

# What a row needs in its registration-deadline cell before the backfill leaves
# it alone. Anything in this set reads as "we do not know", which is the same
# state as empty — the sheet has all three spellings today.
# STORED IN THEIR NORMALISED FORM, because that is what they are compared
# against — `_norm` strips punctuation, so "n/a" arrives here as "n a" and "--"
# arrives as "". Writing them with their punctuation intact is the obvious
# mistake and it fails silently: the cell reads as a real deadline and the row
# is never chased.
UNKNOWN_DEADLINE = {
    "", "n a", "na", "tbd", "tba", "unknown", "not available",
    "not announced", "not stated", "not yet announced", "none", "nil",
    "to be confirmed", "to be announced",
}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(text or "")).split()).strip().lower()


def event_key(name: str, when: Optional[date]) -> str:
    """The identity of an event: normalised name plus its date.

    NAME ALONE IS NOT ENOUGH — "AI Summit" runs in four cities a year and they
    are four events. DATE ALONE is obviously not enough either. Together they
    are stable across the spelling drift the tab actually contains ("NeurIPS"
    vs "Neurips 2026" vs "NeurIPS  2026").
    """
    return f"{_norm(name)}|{dl.iso(when) if when else ''}"


def deadline_is_unknown(value) -> bool:
    return _norm(value).strip() in UNKNOWN_DEADLINE


# -- matching against what the tab already has --------------------------------


def _name_tokens(name: str) -> set:
    """The meaningful words of an event name, for tolerant matching.

    YEARS AND FILLER ARE DROPPED. "NeurIPS 2026" and "NeurIPS" are the same
    conference, and an exact-string check would propose a duplicate of a row
    that is already there — which is the single most annoying thing a discovery
    feature can do.
    """
    words = set(_norm(name).split())
    return {w for w in words
            if len(w) > 2 and not w.isdigit()
            and w not in {"the", "and", "for", "ai", "conference", "summit",
                          "expo", "forum", "week", "annual", "international"}}


def already_on_tab(name: str, when: Optional[date], rows: list) -> Optional[dict]:
    """The existing row for this event, or None. Tolerant of spelling.

    THREE TESTS, LOOSEST LAST. An exact normalised match; then a date match with
    overlapping name words; then a strong name-token overlap with no usable date
    on either side. The point is to err towards "we already have it" — a missed
    discovery costs one event, a duplicated row costs somebody's afternoon and
    makes the sheet untrustworthy.
    """
    want_norm = _norm(name)
    want_tokens = _name_tokens(name)
    if not want_norm:
        return None

    for row in rows or ():
        have = gtm_sheet.clean_cell(row.get("event"))
        if not have:
            continue
        have_norm = _norm(have)
        if have_norm == want_norm:
            return row

        have_when = gtm_sheet.parse_event_date(row.get("event_date"))
        have_start = have_when.get("start") if have_when.get("known") else None
        have_tokens = _name_tokens(have)
        shared = want_tokens & have_tokens
        if not shared:
            continue

        # Same month and a shared distinctive word: the same event.
        if when is not None and have_start is not None:
            if (have_start.year, have_start.month) == (when.year, when.month):
                return row
            # A date we both know and that differs by more than a month is a
            # different instance of a recurring event, so keep looking.
            continue

        # No usable date on one side: fall back to a strong name overlap.
        if len(shared) >= max(1, min(len(want_tokens), len(have_tokens))):
            return row
    return None


def in_window(when: Optional[date], *, today: date) -> bool:
    """Is the event in the CURRENT or NEXT calendar month, and still ahead?

    THE WINDOW IS MONTHS, NOT DAYS, because that is how conference calendars are
    published and how somebody thinks about them. An event that has already
    happened is never in the window, whatever month it fell in.
    """
    if when is None:
        return False
    if when < today:
        return False
    nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    end = (nxt + timedelta(days=32)).replace(day=1)
    return when < end


# -- discovery ----------------------------------------------------------------


def discovery_prompt(existing: list, *, today: date,
                     keywords: Optional[list] = None) -> str:
    """The search for events we do not have."""
    seeds = keywords if keywords is not None else [
        k for k in (config.EVENT_KEYWORDS or []) if str(k).strip()
    ]
    nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    have = []
    for row in (existing or ())[:60]:
        name = gtm_sheet.clean_cell(row.get("event"))
        if name:
            have.append(name)

    lines = [
        "Search for AI conferences, summits and industry events taking place in "
        f"{today.strftime('%B %Y')} or {nxt.strftime('%B %Y')}.",
        "",
        "RELEVANT TO: AI research, model evaluations, AI agents, speech and voice AI, "
        "AI safety and governance, human data and annotation. A sales team at an "
        "AI-data company would go to these to meet buyers.",
        "",
        f"ONLY events whose date falls in those two months, and only ones that have "
        f"NOT already happened (today is {dl.iso(today)}).",
    ]
    if seeds:
        lines += [
            "",
            "Terms from the team's own rules sheet — SEEDS AND NOT LIMITS ('not "
            "limited to these'), so adjacent terms are fair game:",
            "  " + ", ".join(seeds),
        ]
    if have:
        lines += [
            "",
            "SKIP these, we already have them:",
            "  " + "; ".join(have),
        ]
    lines += [
        "",
        "FOR EACH EVENT, one line in exactly this shape and nothing else:",
        "  EVENT | <name> | <city, country> | <YYYY-MM-DD> | <registration deadline "
        "as YYYY-MM-DD, or NOT STATED> | <url>",
        "",
        "THE DATE AND THE URL ARE BOTH REQUIRED — no date or no url, no line.",
        "For a multi-day event give the FIRST day.",
        "NEVER invent a registration deadline. If the page does not state one, write "
        "NOT STATED in that field. A guessed deadline is worse than no deadline: "
        "somebody will plan around it.",
        "If you found nothing new, reply with exactly: NOTHING FOUND",
    ]
    return "\n".join(lines)


_EVENT_RE = re.compile(r"^\s*EVENT\s*\|", re.IGNORECASE)
_DEADLINE_RE = re.compile(r"^\s*DEADLINE\s*\|", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>)\]]+")
_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _read_date(raw: str) -> Optional[date]:
    m = _ISO_RE.search(str(raw or ""))
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            return None
    return dl.parse_date(raw)


def parse_events(text: str) -> list:
    """The EVENT lines, as [{name, location, date, deadline, link, ...}].

    A LINE WITHOUT A DATE OR A URL IS DROPPED. Both are required to propose a
    row: without a date the event cannot be checked against the tab or the
    window, and without a link nobody can verify it exists.
    """
    out: list = []
    for line in str(text or "").splitlines():
        if not _EVENT_RE.match(line):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        name = parts[1]
        location = parts[2] if len(parts) > 2 else ""
        when = _read_date(parts[3] if len(parts) > 3 else "")
        deadline_raw = parts[4] if len(parts) > 4 else ""
        tail = " ".join(parts[5:]) if len(parts) > 5 else ""
        found = _URL_RE.search(tail) or _URL_RE.search(line)
        link = found.group(0).rstrip(".,;)") if found else ""
        if not name or when is None or not link:
            log.info("[events] dropped a discovery with no date or no link: %r",
                     line[:120])
            continue
        deadline = None if _norm(deadline_raw).startswith("not stated") \
            else _read_date(deadline_raw)
        out.append({
            "name": name, "location": location, "date": when,
            "deadline": deadline, "link": link,
            "event_key": event_key(name, when),
        })
    return out


def choose(candidates: list, *, existing: list, today: date, db,
           per_run: int = 0, per_month: int = 0) -> tuple:
    """(to_propose, skipped) — the candidates that survive every gate.

    THE GATES, IN ORDER AND FOR DIFFERENT REASONS:
      1. it has a date we can read and a link                 (parse_events)
      2. the date is in this month or next, and ahead of us
      3. it is not already on the tab, spelling tolerated
      4. it has not been proposed before — a no is an answer
      5. the monthly ceiling has room
      6. the per-run ceiling has room

    `skipped` carries a reason per rejection, because "discovery found nothing"
    and "discovery found four and all four were already on the tab" look
    identical from outside and only one of them is worth investigating.
    """
    per_run = per_run or config.EVENTS_DISCOVERY_MAX_PER_RUN
    per_month = per_month or config.EVENTS_DISCOVERY_MAX_PER_MONTH
    month = today.strftime("%Y-%m")

    try:
        used = db.event_discoveries_this_month(month)
    except Exception:
        log.exception("[events] could not count this month's proposals; proposing none")
        return [], ["could not read the monthly count, so nothing was proposed"]

    room = max(0, int(per_month) - int(used))
    keep: list = []
    skipped: list = []

    for cand in candidates:
        if len(keep) >= int(per_run):
            skipped.append(f"{cand['name']} — over the {per_run}-per-run limit, "
                           "it will come up again")
            continue
        if not room:
            skipped.append(f"{cand['name']} — {used} event(s) already proposed this "
                           f"month, which is the limit of {per_month}")
            continue
        if not in_window(cand["date"], today=today):
            skipped.append(f"{cand['name']} — {dl.format_date(cand['date'])} is "
                           "outside this month and next")
            continue
        row = already_on_tab(cand["name"], cand["date"], existing)
        if row is not None:
            skipped.append(f"{cand['name']} — already on the tab at row "
                           f"{row.get('_row')}")
            continue
        seen = db.event_discovery_seen(cand["event_key"])
        if seen:
            status = str(seen.get("status") or "proposed")
            skipped.append(f"{cand['name']} — already {status}; not asking again")
            continue
        keep.append(cand)
        room -= 1

    return keep, skipped


def render_proposal(events: list) -> str:
    """The message that asks. Name, place, date, deadline, link — and the ask.

    EVERY FIELD OR AN HONEST BLANK. "Registration deadline: not stated" is a
    fact about the page; leaving the line out entirely would read as "there
    isn't one", which is a different and unfounded claim.
    """
    if not events:
        return ""
    lines = [
        f"{len(events)} AI event(s) coming up that aren't on the Events tab:" if
        len(events) != 1 else "An AI event coming up that isn't on the Events tab:"
    ]
    for e in events:
        bits = [f"• {e['name']}"]
        if e.get("location"):
            bits.append(e["location"])
        bits.append(dl.format_date(e["date"]))
        lines.append(" — ".join(bits))
        lines.append(
            "  registration closes "
            + (dl.format_date(e["deadline"]) if e.get("deadline")
               else "— the page doesn't say")
        )
        lines.append(f"  <{e['link']}>")
    lines.append("")
    lines.append("Want these on the sheet? Say yes and I'll add them — I won't "
                 "add anything without one.")
    return "\n".join(lines)


def append_values(event: dict) -> dict:
    """One discovered event as cells for `gtm_sheet.append_row`.

    UNKNOWN CELLS ARE LEFT EMPTY, NEVER GUESSED. The tab has columns for
    timings, key people, registered and attended, and a search result knows none
    of them. An empty cell is a question somebody can answer; an invented one is
    a wrong answer nobody knows to check.
    """
    values = {
        "event": str(event.get("name") or "").strip(),
        "location": str(event.get("location") or "").strip(),
        "event_date": dl.iso(event["date"]) if event.get("date") else "",
        "link": str(event.get("link") or "").strip(),
    }
    if event.get("deadline"):
        values["registration_deadline"] = dl.iso(event["deadline"])
    return {k: v for k, v in values.items() if v}


# -- the registration-deadline backfill ---------------------------------------


def rows_needing_deadline(rows: list, *, today: date, db) -> tuple:
    """(to_look_up, quiet) — rows whose deadline is missing and worth chasing.

    A ROW IS WORTH CHASING WHEN the event has not happened, its deadline cell
    reads as unknown, and we have not already looked and failed inside
    EVENTS_DEADLINE_RECHECK_DAYS. `quiet` names the ones held back, so the log
    can say why the list is short.
    """
    since = dl.iso(today - timedelta(
        days=max(1, int(config.EVENTS_DEADLINE_RECHECK_DAYS))))
    want: list = []
    quiet: list = []
    for row in rows or ():
        name = gtm_sheet.clean_cell(row.get("event"))
        if not name:
            continue
        if not deadline_is_unknown(row.get("registration_deadline")):
            continue
        when = gtm_sheet.parse_event_date(row.get("event_date"))
        start = when.get("start") if when.get("known") else None
        if start is not None and start < today:
            continue
        key = event_key(name, start)
        if db.deadline_recently_checked(key, since_iso=since):
            quiet.append(f"{name} — looked recently and didn't find one")
            continue
        want.append({
            "event_key": key, "name": name, "date": start,
            "link": gtm_sheet.clean_cell(row.get("link")),
            "sheet_row": row.get("_row"),
        })
    return want, quiet


def deadline_prompt(rows: list, *, today: date) -> str:
    """Look up the registration deadline for these events. Links first."""
    lines = [
        "Find the LAST DAY TO REGISTER for each of these events. "
        f"Today is {dl.iso(today)}.",
        "",
        "For each one, check the event's own page FIRST — its link is given where "
        "we have it — and only then search more widely.",
        "",
    ]
    for r in rows:
        bits = [f"  - {r['name']}"]
        if r.get("date"):
            bits.append(f"on {dl.format_date(r['date'])}")
        if r.get("link"):
            bits.append(f"<{r['link']}>")
        lines.append(" ".join(bits))
    lines += [
        "",
        "FOR EACH, one line in exactly this shape and nothing else:",
        "  DEADLINE | <event name exactly as given above> | <YYYY-MM-DD or NOT FOUND> "
        "| <url of the page that states it>",
        "",
        "NEVER GIVE A DATE THE PAGE DOES NOT STATE. If you cannot find a registration "
        "deadline that is written down somewhere, write NOT FOUND. Somebody will plan "
        "around whatever you say, so a guess is worse than nothing.",
        "A date needs the url of the page that states it, or it is NOT FOUND.",
    ]
    return "\n".join(lines)


def parse_deadlines(text: str, asked: list) -> tuple:
    """(found, missing) from the DEADLINE lines.

    A DATE WITHOUT A URL IS TREATED AS NOT FOUND. The prompt says a deadline
    needs the page that states it, and this is where that is enforced rather
    than hoped for — an unsourced date is exactly the guess the prompt forbids.
    """
    by_name = {_norm(r["name"]): r for r in (asked or [])}
    found: list = []
    seen: set = set()

    for line in str(text or "").splitlines():
        if not _DEADLINE_RE.match(line):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        name = parts[1]
        row = by_name.get(_norm(name))
        if row is None:
            # Match loosely: the model sometimes trims a trailing year.
            for key, cand in by_name.items():
                if key.startswith(_norm(name)) or _norm(name).startswith(key):
                    row = cand
                    break
        if row is None:
            continue
        when = _read_date(parts[2])
        tail = " ".join(parts[3:]) if len(parts) > 3 else ""
        m = _URL_RE.search(tail) or _URL_RE.search(line)
        url = m.group(0).rstrip(".,;)") if m else ""
        if when is None or not url:
            continue
        if row["event_key"] in seen:
            continue
        seen.add(row["event_key"])
        found.append({**row, "deadline": when, "source": url})

    missing = [r for r in (asked or []) if r["event_key"] not in seen]
    return found, missing


def render_deadline_proposal(found: list) -> str:
    """Ask to write the deadlines we found. One cell each, each with its source."""
    if not found:
        return ""
    lines = ["I found registration deadlines for these — shall I put them in the sheet?"]
    for f in found:
        lines.append(
            f"• {f['name']} — closes {dl.format_date(f['deadline'])}  <{f['source']}>"
            + (f"  (row {f['sheet_row']})" if f.get("sheet_row") else "")
        )
    lines.append("")
    lines.append("Say yes and I'll write just those cells. Nothing else on the rows "
                 "changes.")
    return "\n".join(lines)


def _self_test() -> int:
    """`python -m events_discovery` — matching, windows, limits, parsing."""
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    today = date(2026, 9, 28)

    class FakeDB:
        def __init__(self, used=0, seen=None):
            self._used, self._seen = used, seen or {}

        def event_discoveries_this_month(self, month):
            return self._used

        def event_discovery_seen(self, key):
            return self._seen.get(key)

        def deadline_recently_checked(self, key, *, since_iso):
            return key in self._seen

    print("the window is this month and next")
    check("today is in", in_window(today, today=today), True)
    check("later this month", in_window(date(2026, 9, 30), today=today), True)
    check("next month", in_window(date(2026, 10, 31), today=today), True)
    check("the month after is out", in_window(date(2026, 11, 1), today=today), False)
    check("the past is out", in_window(date(2026, 9, 1), today=today), False)
    check("an unknown date is out", in_window(None, today=today), False)

    print("\nmatching against the tab, tolerating spelling")
    rows = [
        {"event": "NeurIPS 2026", "event_date": "2026-12-01", "_row": 3},
        {"event": "The AI Summit London", "event_date": "2026-10-15", "_row": 4},
        {"event": "Weird Name", "event_date": "not available", "_row": 5},
    ]
    check("an exact match is found",
          already_on_tab("NeurIPS 2026", date(2026, 12, 1), rows)["_row"], 3)
    check("a year difference still matches",
          already_on_tab("NeurIPS", date(2026, 12, 3), rows)["_row"], 3)
    check("'The' and 'Summit' are not distinctive",
          already_on_tab("AI Summit London", date(2026, 10, 20), rows)["_row"], 4)
    check("a different month is a different instance",
          already_on_tab("NeurIPS", date(2027, 5, 1), rows), None)
    check("something genuinely new is not found",
          already_on_tab("Voice AI Forum", date(2026, 10, 2), rows), None)
    check("a row with no date still matches on name",
          already_on_tab("Weird Name", None, rows)["_row"], 5)

    print("\nparsing discoveries")
    text = (
        "EVENT | Voice AI Forum | Bengaluru, India | 2026-10-14 | 2026-10-01 | https://v.ai/f\n"
        "EVENT | Evals Summit | London, UK | 2026-10-20 | NOT STATED | https://e.com/s\n"
        "EVENT | No Link Event | Paris | 2026-10-22 | NOT STATED |\n"
        "EVENT | No Date Event | Paris | sometime | NOT STATED | https://x.com\n"
    )
    got = parse_events(text)
    check("two survive", len(got), 2)
    check("a deadline is read", got[0]["deadline"], date(2026, 10, 1))
    check("NOT STATED becomes None, not a guess", got[1]["deadline"], None)
    check("no link, no event",
          any(e["name"] == "No Link Event" for e in got), False)
    check("no date, no event",
          any(e["name"] == "No Date Event" for e in got), False)
    check("the key is name plus date",
          got[0]["event_key"], "voice ai forum|2026-10-14")

    print("\nchoosing what to propose")
    keep, skipped = choose(got, existing=rows, today=today, db=FakeDB(),
                           per_run=3, per_month=8)
    check("both are proposable", len(keep), 2)
    check("nothing was skipped", skipped, [])

    keep, skipped = choose(got, existing=rows, today=today, db=FakeDB(), per_run=1)
    check("the per-run limit bites", len(keep), 1)
    check("...and says so", "per-run limit" in skipped[0], True)

    keep, skipped = choose(got, existing=rows, today=today, db=FakeDB(used=8),
                           per_month=8)
    check("the monthly limit bites", len(keep), 0)
    check("...and says so", "limit of 8" in skipped[0], True)

    declined = FakeDB(seen={"voice ai forum|2026-10-14": {"status": "declined"}})
    keep, skipped = choose(got, existing=rows, today=today, db=declined)
    check("a declined event is not re-proposed", len(keep), 1)
    check("...and the reason says declined",
          any("declined" in s for s in skipped), True)

    on_tab = choose(
        parse_events("EVENT | NeurIPS | Vancouver | 2026-12-02 | NOT STATED | https://n.cc\n"),
        existing=rows, today=today, db=FakeDB())
    check("December is outside the window anyway", len(on_tab[0]), 0)

    print("\nthe proposal message")
    msg = render_proposal(got)
    check("it names the event", "Voice AI Forum" in msg, True)
    check("every event carries its link", msg.count("<http"), 2)
    check("a known deadline is stated",
          f"closes {dl.format_date(date(2026, 10, 1))}" in msg, True)
    check("an unknown one is honest", "the page doesn't say" in msg, True)
    check("it asks before adding", "without one" in msg, True)

    print("\nthe cells that would be appended")
    values = append_values(got[0])
    check("the name goes in", values["event"], "Voice AI Forum")
    check("the date is ISO", values["event_date"], "2026-10-14")
    check("the link goes in", values["link"], "https://v.ai/f")
    check("an unknown deadline is ABSENT, not guessed",
          "registration_deadline" in append_values(got[1]), False)
    check("a known one is present",
          append_values(got[0])["registration_deadline"], "2026-10-01")
    check("nothing is invented for timings or attendees",
          set(values) <= {"event", "location", "event_date", "link",
                          "registration_deadline"}, True)

    print("\nwhich rows need a deadline")
    tab = [
        {"event": "Has One", "event_date": "2026-10-10",
         "registration_deadline": "2026-10-01", "_row": 2},
        {"event": "Needs One", "event_date": "2026-10-11",
         "registration_deadline": "", "_row": 3, "link": "https://n.com"},
        {"event": "Says NA", "event_date": "2026-10-12",
         "registration_deadline": "not available", "_row": 4},
        {"event": "Already Gone", "event_date": "2026-09-01",
         "registration_deadline": "", "_row": 5},
    ]
    want, quiet = rows_needing_deadline(tab, today=today, db=FakeDB())
    names = [w["name"] for w in want]
    check("a row with a deadline is left alone", "Has One" in names, False)
    check("an empty cell is chased", "Needs One" in names, True)
    check("'not available' is chased too", "Says NA" in names, True)
    check("a past event is not", "Already Gone" in names, False)

    asked_key = event_key("Needs One", date(2026, 10, 11))
    want2, quiet2 = rows_needing_deadline(
        tab, today=today, db=FakeDB(seen={asked_key: True}))
    check("a recent miss is not re-asked",
          "Needs One" in [w["name"] for w in want2], False)
    check("...and it says why", any("Needs One" in q for q in quiet2), True)

    print("\nparsing deadlines")
    found, missing = parse_deadlines(
        "DEADLINE | Needs One | 2026-10-05 | https://n.com/register\n"
        "DEADLINE | Says NA | NOT FOUND |\n",
        want,
    )
    check("one was found", len(found), 1)
    check("the date is read", found[0]["deadline"], date(2026, 10, 5))
    check("it carries its source", found[0]["source"], "https://n.com/register")
    check("the other is missing", [m["name"] for m in missing], ["Says NA"])
    unsourced, still_missing = parse_deadlines(
        "DEADLINE | Needs One | 2026-10-05 |\n", want)
    check("a date with no source is NOT accepted", len(unsourced), 0)
    check("...it counts as missing", len(still_missing), 2)

    print("\nthe deadline proposal")
    msg = render_deadline_proposal(found)
    check("it names the event", "Needs One" in msg, True)
    check("it carries the source", "n.com/register" in msg, True)
    check("it asks", "Say yes" in msg, True)
    check("it promises to touch nothing else", "Nothing else" in msg, True)

    print("\nunknown-deadline spellings")
    for raw in ("", "  ", "n/a", "N/A", "TBD", "not available", "Not Announced", "-"):
        check(f"{raw!r} reads as unknown", deadline_is_unknown(raw), True)
    check("a real date does not", deadline_is_unknown("2026-10-01"), False)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

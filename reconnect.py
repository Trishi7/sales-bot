"""R14 — the reconnect follow-up: which day each person is due on, who is due
today and in what order, and whether a reply under the post asks for the rest.
PURE: rows and text in, dicts out. No sheet read, no database, no network, no
model.

    python -m reconnect          # the self-test

FOUR JOBS, ALL SMALL.

  WHICH DAY (`trigger_for`). Three rules, in order, the first that matches:
    1  Reconnect Date holds a readable date        that date
    2  Reconnect Date empty or FALSE, and
       Meeting Date holds a date                   Meeting Date + 45 days
    3  Meeting Date is empty too                   newest contact date + 45 days
  A ROW WITH NO DATE IN ANY OF THOSE COLUMNS HAS NO DAY and never produces a
  message.

  READING THE DATE (`read_date`). The column is typed by hand. Full dates are
  `deadlines.parse_date`'s (DAY-FIRST on anything ambiguous: 03/04/2026 is 3
  April); a date with no year is the Deliverables tab's reading
  (`nextaction._deliverable_due`): the next time that day comes round, unless
  it passed within the last 90 days. Ordinals, a trailing full stop and a
  double space are stripped first. Nothing is guessed: what cannot be read is
  None, and the person is then LISTED WITH A FLAG so somebody fixes the cell.

  WHO IS DUE TODAY (`survey`, `due_today`). Every row with a company name; no
  activation gate. A trigger on a Saturday or Sunday posts on the Monday. A
  trigger whose day has already gone is SKIPPED, with no catch-up. One person
  fires once (`already_fired`), and only a NEW Reconnect Date re-arms them.

  A YES (`wants_the_rest`). True only for a yes.

NOTHING HERE WRITES. The sender records who fired, after the post has gone.
"""
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

import deadlines as dl
import gtm_sheet
import nextaction
import replies

log = logging.getLogger(__name__)

# How long after a meeting, or after the last contact, a person comes round.
GAP_DAYS = 45
# The columns "the newest contact date" is the latest of.
CONTACT_ROLES = ("first_contact_date", "li_connected_date", "li_dm_date",
                 "email_1_date", "email_2_date", "email_3_date")
# Deal Status values that end a row for this rule, matched as whole words.
STOP_STATUSES = ("lost", "on hold")

RULE_RECONNECT, RULE_MEETING, RULE_CONTACT = 1, 2, 3

_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
          r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_MONTH_NUMBERS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                  "nov", "dec")
_ORDINAL_RE = re.compile(r"(\d{1,2})\s*(?:st|nd|rd|th)\b", re.IGNORECASE)
# The three shapes a date with NO YEAR is typed in: "13 dec", "dec 13", "13/12".
_DAY_MONTH_RE = re.compile(rf"^(\d{{1,2}})\s*[-/ ]?\s*(?:of\s+)?({_MONTH})$", re.IGNORECASE)
_MONTH_DAY_RE = re.compile(rf"^({_MONTH})\s*[-/ ]?\s*(\d{{1,2}})$", re.IGNORECASE)
_NUMERIC_RE = re.compile(r"^(\d{1,2})\s*[/.-]\s*(\d{1,2})$")
_STOP_RE = re.compile(r"\b(?:" + "|".join(re.escape(s) for s in STOP_STATUSES) + r")\b")
# What may sit beside a yes when somebody asks for the rest in so many words.
_REST_WORDS = frozenset((
    "show", "me", "us", "the", "rest", "them", "all", "list", "everyone", "everybody",
    "others", "remaining", "full", "of", "it", "see", "share", "send", "look", "at",
    "lets", "id", "like", "to", "would", "i", "we", "please", "thanks", "too", "and"))
_REST_NOUNS = frozenset(("rest", "all", "everyone", "everybody", "others", "remaining",
                         "list", "them"))


def is_empty(value) -> bool:
    """Is this cell empty for this rule? Blank, or FALSE.

    FALSE IS THE CHECKBOX ARTEFACT: a column that was once a checkbox column
    leaves the word in every untouched cell (327 First Contact cells carry it
    today). It says nothing about a date, so it reads as nothing.
    """
    if isinstance(value, (date, datetime)):
        return False
    text = gtm_sheet.clean_cell(value)
    return not text or gtm_sheet.parse_flag(text) is False


def _tidy(text: str) -> str:
    """The cell with what people leave around a date taken off: ordinals
    ("13th" is 13), a trailing full stop or comma, a double space, a full stop
    after a month name ("Dec. 13")."""
    out = _ORDINAL_RE.sub(r"\1", str(text or ""))
    out = re.sub(rf"\b({_MONTH})\.", r"\1", out, flags=re.IGNORECASE)
    out = " ".join(out.split())
    return out.strip(" .,;:")


def _month_number(word: str) -> int:
    return _MONTH_NUMBERS.index(word.lower()[:3]) + 1


def _no_year(day: int, month: int, *, today: date) -> Optional[date]:
    """A day and a month with no year, as the Deliverables tab reads "25-Sep":
    the next time it comes round, unless it passed within the last 90 days
    (`nextaction._deliverable_due`, which owns the rule and the number)."""
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    return nextaction._deliverable_due(
        f"{day}-{_MONTH_NUMBERS[month - 1]}", today=today)


def read_date(value, *, today: date) -> Optional[date]:
    """One hand-typed cell as a date, or None when it cannot be read.

        13/12/2026  13-12-2026  13.12.2026     13 Dec 2026, DAY FIRST, ALWAYS
        Dec 13 2026  December 13, 2026         13 Dec 2026
        2026-12-13                             13 Dec 2026
        13th dec  13 Dec  Dec 13  13/12        13 Dec, the year filled in
        a real date cell                       as it stands

    NEVER MONTH-FIRST ON NUMBERS. "03/04/2026" is 3 April. A numeric date that
    only reads month-first ("12/13/2026") is not read at all: the same column
    would then hold "03/04" meaning two different things.

    A cell holding TWO dates (a meeting that moved) is the later one, as
    everywhere else on this tab (`gtm_sheet.sheet_date`).
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = gtm_sheet.clean_cell(value)
    if not raw:
        return None
    text = _tidy(raw)
    if not text:
        return None
    full = dl.parse_date(text)
    if full is not None:
        return full
    m = _DAY_MONTH_RE.match(text)
    if m:
        return _no_year(int(m.group(1)), _month_number(m.group(2)), today=today)
    m = _MONTH_DAY_RE.match(text)
    if m:
        return _no_year(int(m.group(2)), _month_number(m.group(1)), today=today)
    m = _NUMERIC_RE.match(text)
    if m:
        return _no_year(int(m.group(1)), int(m.group(2)), today=today)
    return gtm_sheet.sheet_date(raw)


def newest_contact(row: dict, *, today: date) -> Optional[date]:
    """The latest readable date across the six contact columns, or None. A
    date typed as text ("02.09.2026") counts exactly like a real date cell."""
    found = [d for d in (read_date(row.get(role), today=today) for role in CONTACT_ROLES)
             if d is not None]
    return max(found) if found else None


def trigger_for(row: dict, *, today: date) -> tuple:
    """(date | None, rule number, unclear) for ONE row.

    The three rules in order; the first that matches wins. `unclear` is True
    when the Reconnect Date cell holds something that is not a date: the day
    then comes from rule 2 or 3, so the person is not lost while the cell is
    wrong, and the post says the date is unclear.

    (None, 0, unclear) when no column on the row holds a date: the row can
    never fire. MEETING STATUS IS NOT READ: a meeting date with a blank status
    still counts.
    """
    cell = row.get("reconnect_date")
    unclear = False
    if not is_empty(cell):
        own = read_date(cell, today=today)
        if own is not None:
            return own, RULE_RECONNECT, False
        unclear = True
    meeting = None if is_empty(row.get("meeting_date")) else read_date(
        row.get("meeting_date"), today=today)
    if meeting is not None:
        return meeting + timedelta(days=GAP_DAYS), RULE_MEETING, unclear
    contact = newest_contact(row, today=today)
    if contact is not None:
        return contact + timedelta(days=GAP_DAYS), RULE_CONTACT, unclear
    return None, 0, unclear


def is_stopped(row: dict) -> bool:
    """Deal Status says Lost or On Hold: the row never fires, even when its
    date arrives. Whole words, after trimming and lower-casing ("On-Hold" and
    "on  hold" are On Hold; "Holdings" is not)."""
    text = " ".join(re.sub(r"[-_/]+", " ", gtm_sheet.clean_cell(
        row.get("deal_status")).lower()).split())
    return bool(text and _STOP_RE.search(text))


def row_key(row: dict) -> str:
    """Who a row is, the way every other rule keys a person."""
    return nextaction._contact_key(row)


def has_fired(key: str, when: Optional[date], rule: int, already_fired) -> bool:
    """Is this person done? `already_fired` is {row_key: [trigger dates that
    have fired, ISO]}.

    SOMEBODY WHO FIRED NEVER FIRES AGAIN, with one way back: a NEW Reconnect
    Date. So a person with a record is done unless today's trigger comes from
    the Reconnect Date cell (rule 1) and is a date that has not fired. A later
    email or meeting moves rules 2 and 3 to a new day and re-arms nothing.
    """
    fired = (already_fired or {}).get(key)
    if not fired:
        return False
    if rule != RULE_RECONNECT or when is None:
        return True
    return dl.iso(when) in {str(d) for d in fired}


def survey(rows, *, today: date, already_fired=None, named_by_r13=None,
           shift=None) -> dict:
    """Every row with a company name, sorted into what happens to it.

        {"due": [...]          due TODAY, in the post's order
         "pending": [...]      a day still to come
         "past": [...]         the day has gone: skipped, no catch-up
         "never": n            no date in any column
         "fired": [...]        already posted about
         "stopped": [...]      Deal Status Lost or On Hold
         "blank": [...]        Name or Company blank: skipped
         "by_r13": [...]       due today and named by R13 today: not repeated
         "duplicates": [...]   a second row for one person
         "early_meetings": [...]  rule 2 from a meeting still in the future
         "with_company": n, "dated": n, "by_rule": {1: n, 2: n, 3: n},
         "on_weekend": n}

    Each person is {"key", "name", "company", "rule", "unclear", "trigger",
    "post_day", "sheet_row"}. `dated` counts every row that has a day at all,
    whatever happens to it afterwards; `by_rule` and `on_weekend` describe the
    same rows.

    ORDER: by trigger date, then the sheet's own row order. Stable.

    `shift` moves a weekend day to the Monday (`nextaction.shift_off_weekend`,
    which reads NEXT_ACTION_WEEKEND_SHIFT). `named_by_r13` is the contact keys
    R13 named today.
    """
    shift = shift or nextaction.shift_off_weekend
    named = set(named_by_r13 or ())
    out = {"due": [], "pending": [], "past": [], "never": 0, "fired": [], "stopped": [],
           "blank": [], "by_r13": [], "duplicates": [], "early_meetings": [],
           "with_company": 0, "dated": 0, "by_rule": {1: 0, 2: 0, 3: 0}, "on_weekend": 0}
    seen: set = set()
    for position, row in enumerate(rows or ()):
        company = gtm_sheet.clean_cell(row.get("company"))
        name = gtm_sheet.clean_cell(row.get("name") or row.get("poc"))
        if not company and not name:
            continue                                  # an empty row is not a person
        entry = {"key": row_key(row), "name": name, "company": company,
                 "sheet_row": row.get("_row"), "position": position}
        if not company or not name:
            out["blank"].append({**entry, "missing": "Company" if not company else "Name"})
            if company:
                out["with_company"] += 1
            continue
        out["with_company"] += 1
        when, rule, unclear = trigger_for(row, today=today)
        entry.update(rule=rule, unclear=unclear, trigger=when,
                     post_day=shift(when) if when is not None else None)
        if when is None:
            out["never"] += 1
            continue
        out["dated"] += 1
        out["by_rule"][rule] += 1
        if when.weekday() >= 5:
            out["on_weekend"] += 1
        if entry["key"] in seen:
            out["duplicates"].append(entry)
            continue
        seen.add(entry["key"])
        if rule == RULE_MEETING and when - timedelta(days=GAP_DAYS) > today:
            out["early_meetings"].append(entry)
        if is_stopped(row):
            out["stopped"].append(entry)
        elif has_fired(entry["key"], when, rule, already_fired):
            out["fired"].append(entry)
        elif entry["post_day"] < today:
            out["past"].append(entry)
        elif entry["post_day"] > today:
            out["pending"].append(entry)
        elif entry["key"] in named:
            out["by_r13"].append(entry)
        else:
            out["due"].append(entry)
    for bucket in ("due", "pending", "past"):
        out[bucket].sort(key=lambda e: (e["trigger"], e["position"]))
    return out


def due_today(rows, *, today: date, already_fired=None, named_by_r13=None,
              shift=None) -> list:
    """The people due today, in the post's order, each with its rule number
    and its unclear flag. [] when nobody is, and then nothing is posted."""
    return survey(rows, today=today, already_fired=already_fired,
                  named_by_r13=named_by_r13, shift=shift)["due"]


def wants_the_rest(text) -> bool:
    """True only for a yes.

    A bare yes ("yes", "sure", "yes please", `replies.is_bare_vote`, whose
    words are approvals.py's own), or a yes said in so many words: "show me the
    rest", "yes, all of them". ANYTHING ELSE IS NOT A YES: a no, a question, a
    comment, a name. The caller says nothing to those.
    """
    body = str(text or "")
    if not body.strip() or "?" in body:
        return False
    vote = replies.is_bare_vote(body)
    if vote:
        return vote == "yes"
    words = replies._words(body)
    if not words or len(words) > 8:
        return False
    rest = [w for w in words if w not in replies._YES_WORDS]
    if any(w in replies._NO_WORDS for w in words):
        return False
    return (all(w in _REST_WORDS for w in rest)
            and any(w in _REST_NOUNS for w in rest))


def _self_test() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    def row(n, name="Ann", company="Acme", **cells):
        return {"_row": n, "name": name, "company": company, **cells}

    def shift(d):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d

    today = date(2026, 10, 13)                        # a Tuesday
    want = date(2026, 12, 13)

    print("reading a date")
    for text in ("13/12/2026", "13-12-2026", "13.12.2026", "Dec 13 2026",
                 "December 13, 2026", "2026-12-13", "13th dec", "13 Dec", "Dec 13",
                 "13th Dec.", "13  dec", "13/12", "13.12.26", " 13th December 2026. "):
        check(repr(text), read_date(text, today=today), want)
    check("a real date cell", read_date(date(2026, 12, 13), today=today), want)
    check("03/04/2026 is 3 April, never 4 March", read_date("03/04/2026", today=today),
          date(2026, 4, 3))
    check("month-first numbers are not read", read_date("12/13/2026", today=today), None)
    check("a word is not a date", read_date("banana", today=today), None)
    check("no year, passed 30 days ago: this year's",
          read_date("13 Sep", today=today), date(2026, 9, 13))
    check("no year, passed 120 days ago: next year's",
          read_date("15 Jun", today=today), date(2027, 6, 15))
    check("no year, today", read_date("13 Oct", today=today), today)
    check("FALSE is empty, a word is not", (is_empty("FALSE"), is_empty(""), is_empty("banana")),
          (True, True, False))

    print("\nwhich rule gives the day")
    check("rule 1 beats rule 2",
          trigger_for(row(2, reconnect_date="20 Oct 2026", meeting_date="01-09-2026"), today=today),
          (date(2026, 10, 20), 1, False))
    check("blank Reconnect Date: Meeting Date + 45",
          trigger_for(row(2, reconnect_date="", meeting_date="01-09-2026"), today=today),
          (date(2026, 10, 16), 2, False))
    check("FALSE reads as blank",
          trigger_for(row(2, reconnect_date="FALSE", meeting_date="01-09-2026"), today=today),
          (date(2026, 10, 16), 2, False))
    check("no meeting: the newest contact date + 45",
          trigger_for(row(2, reconnect_date="FALSE", meeting_date="", li_dm_date="02.09.2026",
                          first_contact_date="01-08-2026"), today=today),
          (date(2026, 10, 17), 3, False))
    check("no date anywhere: never", trigger_for(row(2, first_contact_date="FALSE"), today=today),
          (None, 0, False))
    check("an unreadable Reconnect Date: flagged, and the day from rule 3",
          trigger_for(row(2, reconnect_date="banana", email_1_date="29-08-2026"), today=today),
          (date(2026, 10, 13), 3, True))

    print("\nwho is due today")
    rows = [
        row(2, "Ann", "Acme", reconnect_date="13/10/2026"),
        row(3, "Bob", "Borealis", reconnect_date="banana", email_1_date="29-08-2026"),
        row(4, "Cy", "Cinder", reconnect_date="1 Oct 2026"),                   # past
        row(5, "Di", "Delta", reconnect_date="13/10/2026", deal_status="Lost"),
        row(6, "Ed", "Ember", reconnect_date="13/10/2026", deal_status="On Hold"),
        row(7, "", "Flint", reconnect_date="13/10/2026"),                      # blank name
        row(8, "Gus", "Garnet"),                                               # no date
        row(9, "Hal", "Harbor", meeting_date="29-08-2026"),                    # rule 2, today
        row(10, "Ivy", "Iris", reconnect_date="20 Dec 2026"),                  # later
        row(11, "Jo", "Juno", reconnect_date="13/10/2026"),                    # R13 has her
        row(12, "Kai", "Kestrel", reconnect_date="13/10/2026"),                # already fired
    ]
    got = survey(rows, today=today, shift=shift,
                 already_fired={row_key(rows[10]): ["2026-10-13"]},
                 named_by_r13={row_key(rows[9])})
    check("the people due, by trigger date then sheet order",
          [(e["name"], e["rule"], e["unclear"]) for e in got["due"]],
          [("Ann", 1, False), ("Bob", 3, True), ("Hal", 2, False)])
    check("everybody else is accounted for",
          ([e["name"] for e in got["past"]], [e["name"] for e in got["stopped"]],
           [e["company"] for e in got["blank"]], got["never"],
           [e["name"] for e in got["pending"]], [e["name"] for e in got["by_r13"]],
           [e["name"] for e in got["fired"]]),
          (["Cy"], ["Di", "Ed"], ["Flint"], 1, ["Ivy"], ["Jo"], ["Kai"]))
    sat = [row(2, reconnect_date="17/10/2026")]       # a Saturday
    check("a Saturday trigger posts on the Monday, and only then",
          [len(due_today(sat, today=d, shift=shift)) for d in
           (date(2026, 10, 16), date(2026, 10, 17), date(2026, 10, 18), date(2026, 10, 19),
            date(2026, 10, 20))], [0, 0, 0, 1, 0])
    check("a NEW Reconnect Date fires once more",
          len(due_today([row(2, reconnect_date="13/10/2026")], today=today, shift=shift,
                        already_fired={"acme|ann": ["2026-09-01"]})), 1)
    check("a later email does not re-arm somebody who fired",
          len(due_today([row(2, email_3_date="29-08-2026")], today=today, shift=shift,
                        already_fired={"acme|ann": ["2026-09-20"]})), 0)
    check("a meeting recorded early is reported",
          [e["name"] for e in survey([row(2, meeting_date="20-10-2026")], today=today,
                                     shift=shift)["early_meetings"]], ["Ann"])

    print("\na yes")
    for text, said in (("yes", True), ("Yes please", True), ("sure", True), ("yep, thanks Saley", True),
                       ("show me the rest", True), ("yes show the rest please", True),
                       ("all of them", True), ("no", False), ("not now", False),
                       ("no, don't show the rest", False), ("who is David?", False),
                       ("yes?", False), ("Daisy has moved to Agoda", False), ("", False),
                       ("thanks", False)):
        check(repr(text), wants_the_rest(text), said)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

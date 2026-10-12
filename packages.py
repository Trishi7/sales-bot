"""R12 — Sales packages: who is listed and in what order, which package a reply
names, and which day it promises. PURE: rows and text in, dicts out. No sheet
read, no database, no network, no model.

    python -m packages          # the self-test

THREE JOBS, ALL SMALL.

  WHICH PACKAGES (`not_ready`). Every row of the Sales Packages tab whose Ready?
  is not Yes (BLANK IS NOT READY, `gtm_sheet.ready_flag`), one per name, in
  PACKAGES_ORDER. The rule and the startup log both read it from here.

  WHICH PACKAGE A REPLY NAMES (`match_names`). Against the packages ON THE POST
  BEING ANSWERED and nothing else, so an ordinary word cannot attach a date to a
  package nobody mentioned. Tolerant of a shortened form: "Vibe-check" is
  "Evals_Vibe-check".

  WHICH DAY IT PROMISES (`dates_in`, `promises`). `deadlines.parse_date` and the
  reminder reader in sheetwrite.py, plus the plain forms people type under a
  post: "next Friday", "end of the month", "24 Oct", "the 24th". Ambiguous
  numerics are DAY-FIRST, for the reason `deadlines.parse_date` gives.

NOTHING HERE GUESSES. A reply with no date in it yields no date, and a date
with nothing to attach it to is reported as unattached; the caller asks again
rather than writing something down.
"""
import calendar
import logging
import re
from datetime import date, timedelta
from typing import Optional

import config
import deadlines as dl
import gtm_sheet
import sheetwrite

log = logging.getLogger(__name__)

ORDER_DESC, ORDER_ASC = "completion_desc", "completion_asc"
ORDERS = (ORDER_DESC, ORDER_ASC)
# A date-reminder for a package lives in `scheduled_reminders` under this
# row_key prefix; the one reminder sender tells them apart by it.
REMINDER_PREFIX = "package|"


def name_of(row: dict) -> str:
    """The package as the sheet spells it: its Name. A ROW WHOSE NAME IS BLANK
    HAS NO NAME and is skipped; the package id stands in only on a tab that has
    no Name column at all."""
    if "name" in row:
        return gtm_sheet.clean_cell(row.get("name"))
    return gtm_sheet.clean_cell(row.get("package"))


def name_key(name) -> str:
    """One package, however it is cased, spaced or punctuated."""
    return " ".join(_tokens(name))


def reminder_key(name) -> str:
    return REMINDER_PREFIX + name_key(name)


def _tokens(text) -> list:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def completion_of(row: dict) -> Optional[int]:
    """% Completion as a whole number ("33%", "33", "33 %" and "0.33" are all
    33), or None when the cell is blank or is not a number."""
    return gtm_sheet.closure_percent(gtm_sheet.clean_cell(row.get("completion")))


def is_ready(row: dict) -> bool:
    return gtm_sheet.ready_flag(row.get("ready")) == "Yes"


def columns_missing(rows) -> list:
    """Which of the two columns the rule cannot run without are absent from
    the tab: "Name" and "Ready?". A row parsed from the sheet carries a key for
    every role its tab mapped, blank or not, so a role NO row carries is a
    column the bot cannot see (renamed to something no alias matches). A
    column the team MOVED is found by its header and is not missing."""
    rows = list(rows or ())
    missing = []
    if not any("name" in r or "package" in r for r in rows):
        missing.append("Name")
    if not any("ready" in r for r in rows):
        missing.append("Ready?")
    return missing


def order() -> str:
    value = str(getattr(config, "PACKAGES_ORDER", ORDER_DESC) or ORDER_DESC)
    return value if value in ORDERS else ORDER_DESC


def not_ready(rows) -> dict:
    """{"packages": [...], "duplicates": [...], "blank_names": n}.

    `packages` is every package whose Ready? is not Yes, ONE PER NAME, in
    PACKAGES_ORDER, each {"name", "completion", "status", "ready", "doc",
    "sheet_row"}.

    THE ORDER: highest % Completion first (completion_desc), or lowest first
    (completion_asc). A TIE KEEPS THE SHEET'S OWN ORDER. A BLANK OR UNREADABLE
    % SORTS LAST EITHER WAY: an explicit 0 and a missing number are different
    things, and "nobody has said" must not outrank "somebody said zero".

    TWO ROWS WITH ONE NAME ARE ONE PACKAGE: the furthest-along row is kept
    (highest completion; the first on a tie) and the other is reported in
    `duplicates`, so there can never be two reminders for one name. If ANY row
    of that name says Ready? Yes, the package is ready.
    """
    seen: dict = {}
    blank = 0
    duplicates: list = []
    for position, row in enumerate(rows or ()):
        name = name_of(row)
        if not name:
            blank += 1
            continue
        entry = {"name": name, "completion": completion_of(row),
                 "status": gtm_sheet.clean_cell(row.get("status")),
                 "ready": gtm_sheet.ready_flag(row.get("ready")),
                 "ready_raw": gtm_sheet.clean_cell(row.get("ready")),
                 "doc": gtm_sheet.clean_cell(row.get("product_doc")),
                 "sheet_row": row.get("_row"), "position": position}
        key = name_key(name)
        kept = seen.get(key)
        if kept is None:
            seen[key] = entry
            continue
        better = (entry["ready"] == "Yes" and kept["ready"] != "Yes") or (
            entry["ready"] == kept["ready"]
            and (entry["completion"] if entry["completion"] is not None else -1)
            > (kept["completion"] if kept["completion"] is not None else -1))
        loser = kept if better else entry
        if better:
            entry["position"] = kept["position"]
            seen[key] = entry
        duplicates.append({"name": name, "kept_row": seen[key]["sheet_row"],
                           "dropped_row": loser["sheet_row"]})
    waiting = [e for e in seen.values() if e["ready"] != "Yes"]
    descending = order() == ORDER_DESC

    def rank(entry):
        pct = entry["completion"]
        if pct is None:
            return (1, 0, entry["position"])
        return (0, -pct if descending else pct, entry["position"])
    waiting.sort(key=rank)
    return {"packages": waiting, "duplicates": duplicates, "blank_names": blank}


def ready_but_incomplete(rows) -> list:
    """Rows whose Ready? is Yes while % Completion is under 100. READY WINS:
    such a package is not listed. This is for ONE line in the startup log, so
    the mismatch is visible to us without a post going to the team."""
    out = []
    for row in rows or ():
        name = name_of(row)
        pct = completion_of(row)
        if name and is_ready(row) and pct is not None and pct < 100:
            out.append({"name": name, "completion": pct, "sheet_row": row.get("_row")})
    return out


# -- which package a reply names -------------------------------------------------

def match_names(text, names) -> list:
    """[(position, name)] for every package of `names` the reply mentions, in
    the order they are mentioned.

    A PACKAGE IS NAMED when the reply carries its whole name, or a SHORTENED
    FORM: a run of the name's own words, in order ("Vibe-check" for
    "Evals_Vibe-check"; case, spaces and punctuation ignored). A one-word run
    counts only when it is at least five letters long ("pack" names nothing). A run that fits TWO of
    the packages names neither: an ambiguous word attaches nothing. Where runs
    overlap, the longest wins.
    """
    words = [(m.group(0), m.start()) for m in re.finditer(r"[a-z0-9]+", str(text or "").lower())]
    said = [w for w, _p in words]
    candidates = []
    for name in names or ():
        parts = _tokens(name)
        if not parts:
            continue
        for size in range(len(parts), 0, -1):
            for at in range(len(parts) - size + 1):
                run = parts[at:at + size]
                if size == 1 and size < len(parts) and len(run[0]) < 5:
                    continue
                for i in range(len(said) - size + 1):
                    if said[i:i + size] == run:
                        candidates.append((size, i, name))
    if not candidates:
        return []
    owners: dict = {}
    for size, i, name in candidates:
        owners.setdefault((i, size), set()).add(name)
    taken: set = set()
    found: dict = {}
    for size, i, name in sorted(candidates, key=lambda c: (-c[0], c[1])):
        span = set(range(i, i + size))
        if span & taken:
            continue
        if len(owners[(i, size)]) > 1:
            taken |= span                        # fits two packages: names neither
            continue
        taken |= span
        found.setdefault(name, words[i][1])
    return sorted(((pos, name) for name, pos in found.items()))


# -- which day a reply promises --------------------------------------------------

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS["sept"] = 9
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|" \
         r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_WEEKDAY = r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|" \
           r"sat(?:urday)?|sun(?:day)?)"
_PATTERNS = (
    ("month_end", re.compile(rf"\b(?:the\s+)?end\s+of\s+(?:(the|this|next)\s+month|({_MONTH}))\b|"
                             r"\b(this\s+|next\s+)?month[\s-]?end\b", re.I)),
    ("iso", re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b")),
    ("numeric", re.compile(r"\b\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?\b")),
    ("day_month", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b\.?,?(?:\s+(\d{{4}}))?", re.I)),
    ("month_day", re.compile(rf"\b({_MONTH})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I)),
    ("relative", re.compile(r"\b(?:day\s+after\s+tomorrow|tomorrow|tmrw|today|tonight)\b", re.I)),
    ("in_n", re.compile(r"\bin\s+(\d{1,3})\s*(days?|weeks?)\b", re.I)),
    ("weekday", re.compile(rf"\b(?:(next|this|coming)\s+)?({_WEEKDAY})\b", re.I)),
    ("nth", re.compile(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", re.I)),
)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _read(kind: str, m, today: date) -> Optional[date]:
    text = m.group(0)
    try:
        if kind == "month_end":
            words = text.lower()
            named = m.group(2)
            if named:
                month = _MONTHS[named.lower().rstrip(".")]
                year = today.year if month >= today.month else today.year + 1
                return _month_end(year, month)
            if "next" in words:
                first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
                return _month_end(first.year, first.month)
            return _month_end(today.year, today.month)
        if kind == "iso":
            return dl.parse_date(text)
        if kind == "numeric":
            parts = re.split(r"[/.-]", text)
            if len(parts) == 2:                   # no year: this year, DAY-FIRST
                return date(today.year, int(parts[1]), int(parts[0]))
            return dl.parse_date(text.replace(".", "-"))
        if kind in ("day_month", "month_day"):
            day, month = (m.group(1), m.group(2)) if kind == "day_month" else (m.group(2), m.group(1))
            year = int(m.group(3)) if m.group(3) else today.year
            return date(year, _MONTHS[month.lower().rstrip(".")], int(day))
        if kind in ("relative", "in_n", "weekday", "nth"):
            return sheetwrite.parse_reminder_date(text, today=today)
    except (ValueError, KeyError):
        return None
    return None


def dates_in(text, *, today: date) -> list:
    """[(position, date)] for every day the reply names, in the order named.

    "next Friday", "Friday", "tomorrow", "in 2 weeks", "end of the month",
    "end of November", "24 Oct", "Oct 24", "the 24th", "24/10", "24-10-2026",
    "2026-10-24". A DATE WITH NO YEAR IS THIS YEAR'S; "the 24th" and a weekday
    are the next one. Nothing that is not written as a day is read as one: "33"
    and "two" are not dates. Overlapping readings keep the first pattern that
    matched (the more specific ones are tried first).
    """
    body = str(text or "")
    taken: list = []
    out: list = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(body):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            when = _read(kind, m, today)
            if when is None:
                continue
            taken.append((m.start(), m.end()))
            out.append((m.start(), when))
    return sorted(out)


def promises(text, names, *, today: date) -> dict:
    """What a reply under the packages post promises:

        {"dated":      [(name, date), ...]   a package and its day
         "undated":    [name, ...]           named, and no day could be read
         "past":       [(name, date), ...]   the day given has already gone
         "unattached": [date, ...]}          a day, and no package it belongs to

    EACH PACKAGE TAKES THE DAY WRITTEN NEAREST AFTER ITS NAME, up to the next
    package named; failing that the nearest one before it. One day and several
    packages named ("Vibe-check and DPO pack, both end of the month") is that
    day for each of them. A day with NO package named belongs to the post's
    package only when the post named exactly one; otherwise it is unattached
    and nothing is recorded for it.
    """
    names = [n for n in (names or ()) if str(n).strip()]
    mentions = match_names(text, names)
    found = dates_in(text, today=today)
    out = {"dated": [], "undated": [], "past": [], "unattached": []}
    if not mentions:
        if found and len(names) == 1:
            mentions = [(-1, names[0])]
        else:
            out["unattached"] = [d for _p, d in found]
            return out
    for i, (pos, name) in enumerate(mentions):
        nxt = mentions[i + 1][0] if i + 1 < len(mentions) else 10 ** 9
        after = [d for p, d in found if pos <= p < nxt]
        before = [d for p, d in found if p < pos]
        when = after[0] if after else (before[-1] if before else
                                       (found[0][1] if len(found) == 1 else None))
        if when is None:
            out["undated"].append(name)
        elif when < today:
            out["past"].append((name, when))
        else:
            out["dated"].append((name, when))
    return out


def _self_test() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    def row(n, name, ready="", completion="", **kw):
        return {"_row": n, "name": name, "ready": ready, "completion": completion, "status": "", **kw}

    saved = getattr(config, "PACKAGES_ORDER", ORDER_DESC)
    config.PACKAGES_ORDER = ORDER_DESC
    print("who is listed, and the order")
    rows = [row(2, "A", "", "0%"), row(3, "B", "No", "33%"), row(4, "C", "Yes", "100%"),
            row(5, "D", "", ""), row(6, "E", "", "0"), row(7, "", "", "50%"), row(8, "F", "", "n/a")]
    got = not_ready(rows)
    check("blank Ready? is listed, Yes is not, a blank name is skipped",
          ([p["name"] for p in got["packages"]], got["blank_names"]), (["B", "A", "E", "D", "F"], 1))
    check("an explicit 0 comes before a blank or unreadable per cent",
          [p["completion"] for p in got["packages"]], [33, 0, 0, None, None])
    config.PACKAGES_ORDER = ORDER_ASC
    check("completion_asc flips the numbers and still puts the blanks last",
          [p["name"] for p in not_ready(rows)["packages"]], ["A", "E", "B", "D", "F"])
    config.PACKAGES_ORDER = "sideways"
    check("an unrecognised order is the default", order(), ORDER_DESC)
    config.PACKAGES_ORDER = ORDER_DESC
    twice = not_ready([row(2, "Evals Pack", "", "20%"), row(3, "evals_pack", "", "60%"), row(4, "Z", "", "40%")])
    check("two rows with one name are one package: the furthest along is kept",
          ([(p["name"], p["completion"]) for p in twice["packages"]],
           [(d["kept_row"], d["dropped_row"]) for d in twice["duplicates"]]),
          ([("evals_pack", 60), ("Z", 40)], [(3, 2)]))
    check("Ready? Yes under 100% is reported, and is not listed",
          (ready_but_incomplete([row(2, "G", "Yes", "80%"), row(3, "H", "Yes", "100%")]),
           not_ready([row(2, "G", "Yes", "80%")])["packages"]),
          ([{"name": "G", "completion": 80, "sheet_row": 2}], []))
    check("a missing Ready? column is seen", columns_missing([{"_row": 2, "name": "A"}]), ["Ready?"])

    print("\nwhich package a reply names")
    post = ["Evals_Vibe-check", "DPO Pack", "Hinglish STT", "Voice Agents Bench"]
    check("a shortened form", [n for _p, n in match_names("Vibe-check will be done friday", post)],
          ["Evals_Vibe-check"])
    check("the whole name, any case", [n for _p, n in match_names("hinglish stt by the 24th", post)],
          ["Hinglish STT"])
    check("two packages, in the order named",
          [n for _p, n in match_names("DPO pack next week, vibe check after", post)],
          ["DPO Pack", "Evals_Vibe-check"])
    check("a short common word names nothing", match_names("the pack is close", post), [])
    check("a word two packages share names neither",
          match_names("bench soon", ["Voice Bench", "Agents Bench"]), [])
    check("a package that is not on the post is not matched",
          match_names("Hinglish STT on Friday", ["DPO Pack"]), [])

    print("\nwhich day a reply promises")
    today = date(2026, 10, 15)                    # a Thursday
    for text, want in (("next Friday", date(2026, 10, 16)), ("by Friday", date(2026, 10, 16)),
                       ("end of the month", date(2026, 10, 31)), ("end of November", date(2026, 11, 30)),
                       ("24 Oct", date(2026, 10, 24)), ("Oct 24", date(2026, 10, 24)),
                       ("the 24th", date(2026, 10, 24)), ("24/10", date(2026, 10, 24)),
                       ("03/11/2026", date(2026, 11, 3)), ("2026-11-02", date(2026, 11, 2)),
                       ("tomorrow", date(2026, 10, 16)), ("in 2 weeks", date(2026, 10, 29)),
                       ("24th October 2026", date(2026, 10, 24))):
        check(f"{text!r}", [d for _p, d in dates_in(text, today=today)], [want])
    check("no date in it", dates_in("it is 33 per cent done, two people on it", today=today), [])
    got = promises("Vibe-check by next Friday, DPO pack end of the month", post, today=today)
    check("several packages and dates in one reply", got["dated"],
          [("Evals_Vibe-check", date(2026, 10, 16)), ("DPO Pack", date(2026, 10, 31))])
    check("one date for two packages named",
          promises("vibe-check and DPO pack, both 24 Oct", post, today=today)["dated"],
          [("Evals_Vibe-check", date(2026, 10, 24)), ("DPO Pack", date(2026, 10, 24))])
    check("a package named and no date", promises("Vibe-check is nearly there", post, today=today)["undated"],
          ["Evals_Vibe-check"])
    check("a date already gone", promises("Vibe-check 2 Oct", post, today=today)["past"],
          [("Evals_Vibe-check", date(2026, 10, 2))])
    check("a date with no package named, on a post of several, is unattached",
          promises("end of the month", post, today=today),
          {"dated": [], "undated": [], "past": [], "unattached": [date(2026, 10, 31)]})
    check("...and on a post of ONE package it is that package's",
          promises("end of the month", ["DPO Pack"], today=today)["dated"], [("DPO Pack", date(2026, 10, 31))])
    config.PACKAGES_ORDER = saved

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

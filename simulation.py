"""SIMULATION — run any rule, any day, against the real sheet, changing nothing.

"@bot simulate monday" in the test channel, and the bot posts the exact messages
it would send on the next Monday, in order — those messages and nothing else,
built by the same code a real day uses, each tagged "[TEST]". "Why was it
quiet" afterwards explains what was skipped (`why_quiet`).

WHY THIS EXISTS. Twelve rules, a posting window, a daily cap, a roll-over, an
approval queue and a leave check interact in ways nobody can hold in their head.
The only honest way to know what Monday looks like is to watch Monday happen —
and waiting until Monday is a poor development loop.

ISOLATION IS THE WHOLE CONTRACT. A simulation that left a trace would be worse
than no simulation, because the traces are all silent:

    THE DATABASE IS A COPY. Copied to a temp file, used, and deleted. Without
    it a simulated week would claim every drip slot for those dates (the real
    day would then think it had already spoken), mark conferences as reminded
    FOREVER — `event_reminders` rows are never removed — record proposals,
    spend the web-search budget, and burn R9 ladder rungs.

    SHEET WRITES ARE ALWAYS DRY-RUN, regardless of SHEET_WRITES_ENABLED. There
    is no flag to turn this off and there must not be one.

    DMs ARE POSTED, NOT SENT. "[DM to Vaishnavi] ..." in the test channel.

    MENTIONS ARE PLAIN NAMES unless SIMULATION_REAL_MENTIONS is on. A simulated
    week would otherwise put forty notifications on two people's phones for
    messages that are not real.

    THE KILL SWITCH IS BYPASSED. A simulation is a question, not an
    announcement, and "the bot is off" is exactly when somebody wants to ask it.

THE CLOCK IS INJECTED, NOT FAKED GLOBALLY. `today` and `now` are passed down as
arguments — they already were, because the engine was built pure — so a
simulation is the same code with different numbers, not a parallel path. That is
what makes it worth trusting: if the simulation and the real Monday disagree,
one of them is a bug in the shared code rather than in a mock.

TEST MODE IS A DIFFERENT THING and lives here too. SALES_TEST_MODE redirects
REAL output to the test channel with NORMAL STATE and REAL TIMING — for testing
replies, approvals, undo and appends end to end against a test sheet. It is not
isolated and does not pretend to be.
"""
import difflib
import logging
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta
from typing import Optional

import config
import deadlines as dl

log = logging.getLogger(__name__)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")
_WEEKDAY_INDEX = {name: i for i, name in enumerate(WEEKDAYS)}
# Three-letter forms too, because that is what people type.
_WEEKDAY_INDEX.update({name[:3]: i for i, name in enumerate(WEEKDAYS)})

MODE_DAY = "day"
MODE_WEEK = "week"
MODE_RULE = "rule"

_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_RULE_RE = re.compile(r"\b(?:rule\s+)?(R\d{1,2})\b", re.IGNORECASE)
# "next monday" / "last week": the one explicit way out of the current week.
_SHIFT_WORDS = {"next": 1, "last": -1, "previous": -1}
_SIM_RE = re.compile(r"\bsimulat(?:e|ion)\b", re.IGNORECASE)

# The test helpers, all of them test-channel-and-approver only.
_LEAVE_RE = re.compile(
    r"\bpretend\s+(?P<who>[A-Za-z][\w'\-. ]{0,40}?)\s+is\s+on\s+leave"
    r"(?:\s+today)?\b",
    re.IGNORECASE,
)
_CLEAR_LEAVE_RE = re.compile(r"\bclear\s+leave\b", re.IGNORECASE)
_ADVANCE_RE = re.compile(
    r"\badvance\s+clock\s+(?P<n>-?\d{1,3})\s*(?:days?)?\b", re.IGNORECASE
)
_RESET_CLOCK_RE = re.compile(r"\breset\s+clock\b", re.IGNORECASE)
_RESET_STATE_RE = re.compile(r"\breset\s+test\s+state\b", re.IGNORECASE)


# -- THE PLAIN-LANGUAGE TEST COMMANDS -----------------------------------------
#
# WHO THESE ARE FOR. The commands above all contain the word "simulate" and all
# run against a throwaway database. Both were right for the person who wrote
# them and wrong for the person who has to use them: a non-technical tester
# cannot guess the word "simulate", and when they do find it, the results are
# discarded — so nothing they click on, approve or undo has any effect, and the
# bot appears not to work at all.
#
# So there is a second set, in the words somebody would actually use, under a
# clock that stays where they put it, against the REAL database and the REAL
# sheet. The simulations are unchanged and still there for a quick preview.
#
# MATCHED AS PLAIN TEXT, BEFORE ANY MODEL CALL, deliberately. The first thing a
# tester does when the bot is misbehaving is ask it for help — and an
# unreachable or misconfigured API is one of the things they are testing. A
# help command that needs the model to work is a help command that is missing
# exactly when it is wanted.
CMD_HELP = "help"
CMD_MAKE_IT = "make_it"
CMD_NEXT_DAY = "next_day"
CMD_BACK_TO_TODAY = "back_to_today"
CMD_START_OVER = "start_over"

_TEST_HELP_RE = re.compile(
    r"^(?:test\s+help|help\s+test|test\s+commands|what\s+can\s+i\s+test)[\s?.!]*$",
    re.IGNORECASE,
)
_MAKE_IT_RE = re.compile(r"^make\s+it\s+(?P<when>.{1,40}?)[\s?.!]*$", re.IGNORECASE)
_NEXT_DAY_RE = re.compile(
    r"^(?:next\s+day|the\s+next\s+day|tomorrow)[\s?.!]*$", re.IGNORECASE
)
_BACK_TO_TODAY_RE = re.compile(
    r"^(?:back\s+to\s+today|back\s+to\s+the\s+real\s+day|real\s+clock|"
    r"stop\s+testing)[\s?.!]*$",
    re.IGNORECASE,
)
_START_OVER_RE = re.compile(r"^start\s+over[\s?.!]*$", re.IGNORECASE)

# The confirmation for "start over", and nothing else. Deliberately a short
# closed list: "start over" deletes the test database, and a tester who typed
# something ambiguous should be asked again rather than have it guessed at.
_YES_RE = re.compile(
    r"^(?:yes|yep|yeah|y|confirm|confirmed|do\s+it|go\s+ahead)[\s?.!]*$", re.IGNORECASE
)
_NO_RE = re.compile(
    r"^(?:no|nope|n|cancel|stop|never\s+mind|nevermind)[\s?.!]*$", re.IGNORECASE
)


def parse_test_command(text: str) -> Optional[dict]:
    """One of the plain-language test commands, or None.

    {"cmd", "date", "raw"}. `date` is set only for "make it X", and only when X
    could be read as a day — an unreadable one comes back as None so the caller
    can say what it tried rather than silently picking today.
    """
    body = " ".join(str(text or "").split()).strip()
    if not body:
        return None

    if _TEST_HELP_RE.match(body):
        return {"cmd": CMD_HELP, "date": None, "raw": body}
    if _NEXT_DAY_RE.match(body):
        return {"cmd": CMD_NEXT_DAY, "date": None, "raw": body}
    if _BACK_TO_TODAY_RE.match(body):
        return {"cmd": CMD_BACK_TO_TODAY, "date": None, "raw": body}
    if _START_OVER_RE.match(body):
        return {"cmd": CMD_START_OVER, "date": None, "raw": body}

    m = _MAKE_IT_RE.match(body)
    if m:
        asked = m.group("when").strip()
        when = read_day(asked)
        # "MAKE IT HAPPEN" IS NOT A DATE COMMAND. An unreadable day is only
        # claimed as one when it was plainly an ATTEMPT at a day — a mistyped
        # weekday, or anything with a number in it. Otherwise the phrase is
        # ordinary English and belongs to whoever handles it next; claiming it
        # would have the bot answer "I couldn't read 'happen' as a day" to
        # somebody who was simply talking.
        if when is None and not _looks_like_an_attempt_at_a_day(asked):
            return None
        return {"cmd": CMD_MAKE_IT, "date": when, "raw": body, "asked_for": asked}
    return None


def _looks_like_an_attempt_at_a_day(phrase: str) -> bool:
    """Was this MEANT to be a day, even though it does not parse?

    A near-miss on a weekday ("mondya") or anything carrying a number ("32
    Sep", "2026-13-01") was an attempt, and deserves "I couldn't read that as a
    day" rather than silence. Plain words are not.
    """
    want = " ".join(str(phrase or "").split()).strip().lower().strip(".,!?")
    if not want:
        return False
    if any(ch.isdigit() for ch in want):
        return True
    names = list(WEEKDAYS) + [n[:3] for n in WEEKDAYS] + [
        "today", "tomorrow", "yesterday",
    ] + [m.lower() for m in (
        "jan", "feb", "mar", "apr", "may", "jun",
        "jul", "aug", "sep", "oct", "nov", "dec",
    )]
    return bool(difflib.get_close_matches(want, names, n=1, cutoff=0.7))


def is_yes(text: str) -> bool:
    return bool(_YES_RE.match(" ".join(str(text or "").split())))


def is_no(text: str) -> bool:
    return bool(_NO_RE.match(" ".join(str(text or "").split())))


def read_day(phrase: str) -> Optional[date]:
    """"monday", "28 Sep", "2026-09-28", "tomorrow" -> a date. None otherwise.

    A BARE WEEKDAY MEANS THAT DAY OF THIS WEEK (Monday to Sunday containing
    today), passed or not — the same rule `this_week_day` gives the simulations.
    "make it Monday" on a Wednesday is the Monday two days ago; on a Monday it
    is today. "next monday" / "last monday" step a week either way.

    EVERY WORD HERE IS READ AGAINST THE REAL DATE (`real_today`), never the
    pretend one: with the clock parked on Monday 5 Oct and the real date Thu
    1 Oct, "monday" is 28 Sep, "today" is 1 Oct and "tomorrow" is 2 Oct.
    """
    want = " ".join(str(phrase or "").split()).strip().lower().strip(".,!?")
    if not want:
        return None
    if want in ("today", "now"):
        return real_today()
    if want == "tomorrow":
        return real_today() + timedelta(days=1)
    if want == "yesterday":
        return real_today() - timedelta(days=1)

    parts = want.split()
    if len(parts) == 1 and parts[0] in _WEEKDAY_INDEX:
        return this_week_day(_WEEKDAY_INDEX[parts[0]])
    if (len(parts) == 2 and parts[0] in _SHIFT_WORDS
            and parts[1] in _WEEKDAY_INDEX):
        return this_week_day(_WEEKDAY_INDEX[parts[1]]) + timedelta(
            weeks=_SHIFT_WORDS[parts[0]])

    # Anything else is a date, read by the one parser the sheet already uses,
    # so a tester may type a date in any form the spreadsheet accepts.
    parsed = dl.parse_date(want)
    if parsed is not None:
        return parsed
    # A bare day-and-month ("28 Sep") is the commonest form and that parser
    # wants a year. Try this year, then next, and prefer one not in the past —
    # somebody testing a date almost always means the one coming up.
    base = real_today()
    for year in (base.year, base.year + 1):
        got = dl.parse_date(f"{want} {year}")
        if got is not None and got >= base:
            return got
    return dl.parse_date(f"{want} {base.year}")


def help_text() -> str:
    """Every test command, in words, with no jargon and no rule codes.

    IT NAMES WHAT IS REAL AND WHAT IS NOT, because that is the one question a
    tester cannot answer from the outside, and the one that decides whether
    they trust what they just watched happen.
    """
    import clock

    lines = [
        "Here is everything you can say to me in this channel.",
        "",
        "  make it Monday      This week's Monday: I will act as if today is",
        "                      that day and post that day's messages. A day",
        "                      name means that day of THIS week, even if it",
        "                      has passed; say 'next monday' or 'last monday'",
        "                      for another week. 'tomorrow' or a date like",
        "                      28 Sep works too.",
        "                      Day names, 'today' and 'tomorrow' always count",
        "                      from the REAL date, wherever the pretend day is.",
        "  next day            Move on from the pretend day to the one after.",
        "  back to today       Stop pretending. The real date comes back.",
        "  start over          Wipe everything I have recorded while testing",
        "                      and begin again. I will ask you to confirm.",
        "  test help           This list.",
        "",
        "While a pretend day is set, everything is REAL: what I write goes to",
        "the spreadsheet, approving and undoing work, and chasing somebody over",
        "several days works because the days really do move. That is the point.",
        "",
        "  simulate monday     A quick preview instead. I show you what this",
        "  simulate week       week's Monday (or this whole week) would look",
        "  simulate rule R8    like and record none of it: nothing is written,",
        "                      and I forget it the moment it finishes. 'next'",
        "                      or 'last' before monday/week picks another week.",
        "",
        "  pretend Sid is on leave   See what happens when somebody is away.",
        "  clear leave               Undo that.",
    ]
    st = clock.status()
    lines.append("")
    lines.append(
        f"Right now I think it is {clock.describe()}." if st["pretending"]
        else f"Right now I am on the real clock: {clock.describe()}."
    )
    return "\n".join(lines)


# -- the injected clock -------------------------------------------------------
#
# ONLY MEANINGFUL IN SALES_TEST_MODE. `advance clock 3 days` exists so the
# nudge-and-drop sweep and R8's re-anchoring can be tested without waiting three
# days; applying it to a live bot would silently shift every deadline the team
# depends on, so `advance_clock` refuses outside test mode.
_clock_offset_days: int = 0


def clock_offset_days() -> int:
    return _clock_offset_days if config.SALES_TEST_MODE else 0


def set_clock_offset(days: int) -> tuple:
    """(ok, message). Refuses outside SALES_TEST_MODE."""
    global _clock_offset_days
    if not config.SALES_TEST_MODE:
        return False, (
            "Advancing the clock only works with SALES_TEST_MODE=true. On a live "
            "bot it would shift every deadline the team depends on."
        )
    _clock_offset_days = int(days)
    return True, (
        f"Clock advanced {int(days)} day(s) — today is now "
        f"{dl.format_date(today())}. Say reset clock to put it back."
    )


def reset_clock() -> str:
    global _clock_offset_days
    _clock_offset_days = 0
    return f"Clock reset — today is {dl.format_date(today())} again."


def today() -> date:
    """Today, as the bot sees it: the PRETEND date when one is set (plus the
    test offset). What "next day" moves on from — and never the base for
    resolving a weekday or a week; that is `real_today`."""
    return dl.today_ist() + timedelta(days=clock_offset_days())


def real_today() -> date:
    """The REAL date (plus the test offset), whatever the pretend clock says.

    THE BASE FOR EVERY WEEKDAY AND EVERY WEEK. A tester who parked the clock on
    Monday 5 Oct and then says "make it monday" means the Monday of the week
    they are actually sitting in. Resolved against the pretend date, each
    "make it monday" drifted further from the calendar on the wall.
    """
    return dl.real_today_ist() + timedelta(days=clock_offset_days())


def dates_line() -> str:
    """"real date 2026-10-01 | pretend date 2026-09-28" (or "none") — the one
    line boot and every test command log."""
    import clock

    st = clock.status()
    pretend = dl.iso(st["pretend_date"]) if st["pretend_date"] else "none"
    return f"real date {dl.iso(real_today())} | pretend date {pretend}"


def now() -> datetime:
    return dl.now_ist() + timedelta(days=clock_offset_days())


# -- the leave override -------------------------------------------------------
_leave_override: dict = {}


def pretend_on_leave(name: str) -> str:
    key = " ".join(str(name or "").split()).strip()
    if not key:
        return "Who?"
    import leave as _leave
    _leave_override[_leave._norm(key)] = {
        "name": key, "from": "", "until": "", "quote": "(test override)",
    }
    return (
        f"Treating {key} as on leave from now on. Work addressed to them will go "
        "to whoever covers. Say clear leave to undo."
    )


def clear_leave_override() -> str:
    n = len(_leave_override)
    _leave_override.clear()
    return f"Cleared {n} leave override(s)." if n else "No leave overrides were set."


def leave_override() -> dict:
    """The override map, or {} — merged over the real leave read by the caller."""
    return dict(_leave_override)


# -- the in-simulation flag ---------------------------------------------------
#
# A PROCESS-WIDE FLAG RATHER THAN A THREADED ARGUMENT, and it is the one place
# this module is not purely functional. The alternative was passing `dry_run`
# down through six call sites that have no other reason to know a simulation is
# running — and a single one forgetting would write to the real sheet.
#
# The flag is set and cleared by `simulating()`, which is a context manager so
# it cannot be left on by an exception.
_in_simulation: bool = False


def in_simulation() -> bool:
    return _in_simulation


class simulating:
    """Marks the block as a simulation. Sheet writes become dry runs."""

    def __enter__(self):
        global _in_simulation
        self._was = _in_simulation
        _in_simulation = True
        return self

    def __exit__(self, *exc):
        global _in_simulation
        _in_simulation = self._was
        return False


# -- parsing ------------------------------------------------------------------


def parse(text: str) -> Optional[dict]:
    """A simulation command, or None. {mode, date, rule, fast, raw}.

    RETURNS None RATHER THAN GUESSING. "simulate" alone is not a command — the
    bot asks which day rather than picking one, because a simulation of the
    wrong day looks exactly like a simulation of the right one.
    """
    body = " ".join(str(text or "").split()).strip()
    if not body or not _SIM_RE.search(body):
        return None

    low = body.lower()
    # "real" opts out of the compressed spacing; "fast" is the default.
    fast = " real " not in f" {low} "

    m = _RULE_RE.search(body)
    if m:
        return {"mode": MODE_RULE, "date": None, "rule": m.group(1).upper(),
                "fast": fast, "raw": body}

    m = _DATE_RE.search(body)
    if m:
        try:
            when = date.fromisoformat(m.group(1))
        except ValueError:
            return None
        return {"mode": MODE_DAY, "date": when, "rule": "", "fast": fast,
                "raw": body}

    # THIS WEEK UNLESS SAID OTHERWISE. A bare "week" or weekday is the current
    # Monday-to-Sunday week; "next"/"last" right before it steps one week.
    tokens = re.findall(r"[a-z]+", low)
    for i, token in enumerate(tokens):
        if token != "week" and token not in _WEEKDAY_INDEX:
            continue
        shift = _SHIFT_WORDS.get(tokens[i - 1], 0) if i else 0
        if token == "week":
            return {"mode": MODE_WEEK,
                    "date": this_week_day(0) + timedelta(weeks=shift),
                    "rule": "", "fast": fast, "raw": body}
        return {"mode": MODE_DAY,
                "date": this_week_day(_WEEKDAY_INDEX[token]) + timedelta(weeks=shift),
                "rule": "", "fast": fast, "raw": body}

    return None


def this_week_day(index: int, *, from_day: Optional[date] = None) -> date:
    """That weekday in the week (Monday to Sunday) containing the REAL today.

    PASSED OR NOT. "simulate monday" on a Wednesday is the Monday two days ago —
    a tester means this week. A past pretend date is fine: the research cache
    and drip_sends are keyed per date, so nothing collides with today.

    THE REAL WEEK, not the pretend clock's (`real_today`).
    """
    base = from_day or real_today()
    monday = base - timedelta(days=base.weekday())
    return monday + timedelta(days=int(index))


def next_weekday(index: int, *, from_day: Optional[date] = None) -> date:
    """The first date on or after `from_day` (the real today) that falls on
    `index`.

    Forward-only, today included. Bare weekday names do NOT use this — they
    mean this week (`this_week_day`).
    """
    base = from_day or real_today()
    delta = (int(index) - base.weekday()) % 7
    return base + timedelta(days=delta)


def week_of(start: Optional[date] = None) -> list:
    """Monday to Sunday of the week containing `start` (default: the REAL today)."""
    monday = this_week_day(0, from_day=start or real_today())
    return [monday + timedelta(days=i) for i in range(7)]


def next_date_for_rule(rule_id: str, *, from_day: Optional[date] = None) -> Optional[date]:
    """The next date the named rule would fire on. None when it never would.

    THIS WEEK FIRST, THEN NEXT. Unlike a bare weekday this looks forward only:
    the scan starts at the REAL today, so a rule whose day is today or later
    this week gets that day, and one whose day has already passed gets next
    week's.

    ANCHORED RULES (R8, R9) HAVE NO WEEKDAY, so "the next date it would fire" is
    simply the next working day — their own evaluators decide whether anything
    is actually due, which is the honest answer for a rule whose trigger is a
    meeting date on the sheet rather than the calendar.
    """
    import rules as rules_mod

    rule = rules_mod.by_id(str(rule_id).upper())
    if rule is None or not rule.enabled:
        return None
    base = from_day or real_today()
    for offset in range(0, 21):
        day = base + timedelta(days=offset)
        if rule.runs_on(day):
            return day
    return None


# -- the isolated database ----------------------------------------------------


class SandboxDB:
    """A throwaway copy of the live database, deleted on exit.

    THE COPY IS THE ISOLATION. Every ledger the bot writes is permanent and
    several are irreversible — `event_reminders` rows are never removed, so a
    simulation that marked a conference as reminded would silence the real
    reminder for good. Copying the file is cruder than mocking each write and
    very much safer: nothing has to remember to be mocked.

    ON A COPY FAILURE THE SIMULATION DOES NOT RUN. Falling back to the live
    database would be the exact failure this exists to prevent.
    """

    def __init__(self, source_path: str):
        self.source = source_path
        self.path = ""
        self._dir = ""
        self.db = None

    def __enter__(self):
        import db as dbmod

        self._dir = tempfile.mkdtemp(prefix="saley-sim-")
        self.path = os.path.join(self._dir, "sim.db")
        try:
            if os.path.exists(self.source):
                shutil.copy2(self.source, self.path)
        except OSError as e:
            shutil.rmtree(self._dir, ignore_errors=True)
            raise RuntimeError(
                f"could not copy the database for the simulation ({e}); refusing to "
                "run against the live one"
            ) from e
        self.db = dbmod.DB(self.path)
        log.info("[sim] running on a copy of %s at %s", self.source, self.path)
        return self.db

    def __exit__(self, *exc):
        shutil.rmtree(self._dir, ignore_errors=True)
        log.info("[sim] discarded the simulation database")
        return False


# -- rendering ----------------------------------------------------------------


def strip_mentions(text: str) -> str:
    """Turn <@id> tokens into plain names. What keeps a simulation quiet.

    A simulated week carries forty tags; sending them would put forty
    notifications on two people's phones for messages that are not real.
    SIMULATION_REAL_MENTIONS turns this off for the rare case where somebody
    wants to see the pings land.
    """
    if config.SIMULATION_REAL_MENTIONS:
        return text

    def _name(m):
        uid = m.group(1)
        known = str(config.ROSTER_DISPLAY_NAMES.get(str(uid)) or "").strip()
        return f"@{known}" if known else f"@user{uid}"

    return re.sub(r"<@!?(\d+)>", _name, text or "")


def prefix(text: str) -> str:
    """Every simulated message carries the marker, on its first line."""
    body = (text or "").strip()
    return f"{config.SIMULATION_PREFIX} {body}" if body else config.SIMULATION_PREFIX


def dm_line(recipient: str, text: str) -> str:
    """A DM, shown rather than sent."""
    return f"[DM to {recipient or 'someone'}] {text}"


# THE DAY IN POINTS — one grouping, used by the "why was it quiet" answer for a
# test day and a simulation alike, so the two can never disagree.
BULLET_MAX_WORDS = 15


def _plain_rule(rule_id: str, fallback: str) -> str:
    import rules as rules_mod
    return rules_mod.plain_description(rule_id or "") or str(fallback or "something")


def day_points(*, planned: Optional[dict], rules_run: Optional[list]) -> dict:
    """{"rolled": [(name, items)], "looked": [names], "not_today": [names],
    "failed": [names]} — plain rule names, no rule codes, no owners.

    ROLLED IS COUNTED IN ITEMS PER RULE: two owner groups of the same rule are
    one line to a tester ("P1 deliverables due this week — title and due date only (5)"), which is what the
    old per-group list with its owner-and-company parentheticals got wrong.

    A HELD GROUP ("asked recently") counts as LOOKED, NOTHING DUE: the rule
    ran and found the item, but the nudge ladder says it is not due today.
    A DISABLED rule is off every day, so it is not news about this one.
    """
    rolled: dict = {}
    for g in ((planned or {}).get("rolled") or []):
        name = _plain_rule(g.get("rule_id"), g.get("rule_name") or g.get("type"))
        rolled[name] = rolled.get(name, 0) + max(1, len(g.get("actions") or []))

    looked: list = []
    not_today: list = []
    failed: list = []

    def _add(bucket, name):
        if name and name not in bucket:
            bucket.append(name)

    for g in ((planned or {}).get("held") or []):
        _add(looked, _plain_rule(g.get("rule_id"), g.get("rule_name") or g.get("type")))
    for entry in (rules_run or []):
        rid = str(entry.get("id") or "")
        if rid in ("", "—"):
            continue
        name = _plain_rule(rid, entry.get("name"))
        why = str(entry.get("why") or "")
        if entry.get("ran"):
            if "raised" in why:
                _add(failed, name)
            elif not entry.get("items"):
                _add(looked, name)
        elif why.startswith("does not run on"):
            _add(not_today, name)
    return {"rolled": [(n, c) for n, c in rolled.items()], "looked": looked,
            "not_today": not_today, "failed": failed}


def fit_bullet(label: str, items: list, *, limit: int = BULLET_MAX_WORDS) -> str:
    """"• <label> a, b and 3 more", never more than `limit` words.

    The first item is always shown, even alone over the limit — a bullet that
    names nothing is worse than a long one.
    """
    words = len(label.split())
    shown: list = []
    for i, item in enumerate(items):
        more = len(items) - i - 1
        cost = len(str(item).split()) + (3 if more else 0)
        if shown and words + cost > limit:
            break
        shown.append(str(item))
        words += len(str(item).split())
    hidden = len(items) - len(shown)
    text = ", ".join(shown) + (f" and {hidden} more" if hidden else "")
    return f"• {label} {text}"


def point_lines(*, sent: int, points: dict, weekday: str) -> list:
    """The bullets. A bullet with nothing in it is left out entirely."""
    lines = [f"• Sent: {int(sent)}"]
    if points.get("rolled"):
        lines.append(fit_bullet("Rolled to tomorrow:",
                                [f"{n} ({c})" for n, c in points["rolled"]]))
    if points.get("looked"):
        lines.append(fit_bullet("Looked, nothing due:", points["looked"]))
    if points.get("not_today"):
        lines.append(fit_bullet(f"Not a {weekday} rule:", points["not_today"]))
    return lines


# "WHY WAS IT QUIET" — ASKED, NOT POSTED. A test run posts only the messages a
# real day would; what it skipped is the answer to a question, matched as plain
# text before any model call.
_WHY_QUIET_RE = re.compile(
    r"\b(?:why\s+(?:was|is)\s+(?:it|today|the\s+day|that)\s+(?:so\s+)?"
    r"(?:quiet|silent|empty)"
    r"|what\s+did\s+(?:you|it)\s+skip"
    r"|why\s+(?:did\s+)?nothing\s+(?:go\s+out|went\s+out|get\s+sent|today))",
    re.IGNORECASE,
)


def is_why_quiet(text: str) -> bool:
    return bool(_WHY_QUIET_RE.search(" ".join(str(text or "").split())))


def why_quiet(result: Optional[dict]) -> str:
    """The last test run's plan, in points: sent, rolled, looked and found
    nothing, not today's rule. Deterministic, no model, no rule codes.

    `result` is {"date", "planned", "rules_run", "sent", "simulated", "note"},
    or None when no test run has happened since the bot started.
    """
    if not result:
        return ("I haven't run a test day or a simulation since I last started, so "
                "there is no plan to explain yet. Say \"make it Monday\" to run one.")
    day = result.get("date")
    label = day.strftime("%A %d %b") if day else "That day"
    if result.get("simulated"):
        label += " (simulated)"
    if result.get("note"):
        return f"{label}: {result['note']}"
    sent = int(result.get("sent") or 0)
    points = day_points(planned=result.get("planned"),
                        rules_run=result.get("rules_run"))
    head = label
    if points.get("failed"):
        head += " · couldn't check " + ", ".join(points["failed"][:2])
    lines = [head] + point_lines(
        sent=sent, points=points, weekday=day.strftime("%A") if day else "today's")
    if len(lines) == 2 and not sent:
        lines.append("Nothing was due, rolled or skipped — the rules found no work.")
    import rules as rules_mod
    return rules_mod.render_for_user("\n".join(lines))


def pace_seconds(*, fast: bool, count: int) -> float:
    """How long to wait between simulated posts.

    "real" DOES NOT MEAN REAL. A week at real spacing would run for days, so it
    is compressed to SIMULATION_MAX_SECONDS spread across the posts — the
    PLANNED TIMES ARE STILL REPORTED EXACTLY, only the waiting is shortened.
    Pretending otherwise would mean a "real" simulation nobody ever saw finish.
    """
    if fast:
        return max(0.0, float(config.SIMULATION_FAST_GAP_SECONDS))
    ceiling = max(1, int(config.SIMULATION_MAX_SECONDS))
    return max(1.0, ceiling / max(1, int(count)))


def may_run(channel_id, user_id) -> tuple:
    """(allowed, why). The two gates, asked in one place.

    THE CHANNEL FIRST, THEN THE PERSON. A simulation typed in the real sales
    channel must do NOTHING — not refuse loudly, not explain itself — because a
    refusal there is itself a message in a channel the team reads.
    """
    if not config.simulation_available():
        return False, (
            "SALES_TEST_CHANNEL_ID is not set, so there is nowhere to run a "
            "simulation. Set it to the test channel's id."
        )
    if not config.is_test_channel(channel_id):
        return False, ""          # silent: wrong channel
    if not config.is_approver(user_id):
        return False, (
            "Simulations are Sid's and Vaishnavi's to run — they compose real "
            "messages and cost real model calls."
        )
    return True, ""


def _self_test() -> int:
    """`python -m simulation` — parsing, dates and rendering. No I/O."""
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    base = date(2026, 9, 22)          # a Tuesday

    print("parsing")
    check("simulate monday", parse("simulate monday")["mode"], MODE_DAY)
    check("...is a Monday",
          next_weekday(0, from_day=base).weekday(), 0)
    check("simulate week", parse("simulate week")["mode"], MODE_WEEK)
    check("simulate a date",
          parse("simulate 2026-09-28")["date"], date(2026, 9, 28))
    check("simulate rule R8", parse("simulate rule R8")["rule"], "R8")
    check("rule ids are upper-cased", parse("simulate rule r8")["rule"], "R8")
    check("fast is the default", parse("simulate monday")["fast"], True)
    check("real opts out", parse("simulate monday real")["fast"], False)
    check("fast can be explicit", parse("simulate monday fast")["fast"], True)
    check("three-letter days", parse("simulate wed")["mode"], MODE_DAY)
    check("not a command", parse("what is the queue"), None)
    check("bare simulate is not a command", parse("simulate"), None)
    check("empty", parse(""), None)
    check("a bad date is refused", parse("simulate 2026-13-45"), None)

    print("\nnext weekday (forward-only)")
    check("today counts", next_weekday(1, from_day=base), base)
    check("tomorrow", next_weekday(2, from_day=base), date(2026, 9, 23))
    check("wraps to next week", next_weekday(0, from_day=base), date(2026, 9, 28))

    print("\nthis week's day")
    check("a passed day stays this week",
          this_week_day(0, from_day=base), date(2026, 9, 21))
    check("today is today", this_week_day(1, from_day=base), base)
    check("later this week", this_week_day(6, from_day=base), date(2026, 9, 27))
    check("Monday on a Monday", this_week_day(0, from_day=date(2026, 9, 28)),
          date(2026, 9, 28))

    print("\nweek_of")
    days = week_of(base)
    check("seven days", len(days), 7)
    check("starts this week's Monday", days[0], date(2026, 9, 21))
    check("ends this week's Sunday", days[-1], date(2026, 9, 27))
    check("in order", days == sorted(days), True)

    print("\nthis week in commands (clock pinned to Tue 22 Sep)")
    global today, real_today
    was_today, was_real = today, real_today
    today = real_today = lambda: base  # noqa: E731
    try:
        check("simulate monday -> this Monday",
              parse("simulate monday")["date"], date(2026, 9, 21))
        check("simulate week -> this Monday",
              parse("simulate week")["date"], date(2026, 9, 21))
        check("simulate next monday",
              parse("simulate next monday")["date"], date(2026, 9, 28))
        check("simulate last week",
              parse("simulate last week")["date"], date(2026, 9, 14))
        check("simulate R4 needs no 'rule'", parse("simulate R4")["rule"], "R4")
        check("make it monday -> yesterday", read_day("monday"), date(2026, 9, 21))
        check("make it next monday", read_day("next monday"), date(2026, 9, 28))
        check("tomorrow unchanged", read_day("tomorrow"), date(2026, 9, 23))
    finally:
        today, real_today = was_today, was_real

    # THE PRETEND CLOCK IS NEVER THE BASE. Parked on Mon 5 Oct, real date Thu
    # 1 Oct: `clock`'s two answers are swapped for fixed ones, and the real
    # `today()` / `real_today()` above are what get exercised.
    print("\nweekdays resolve from the REAL date (pretend Mon 5 Oct, real Thu 1 Oct)")
    import clock
    was_now, was_pretend = clock.real_now_ist, clock.today_ist
    clock.real_now_ist = lambda: datetime(2026, 10, 1, 13, 17, tzinfo=dl.IST)
    clock.today_ist = lambda: date(2026, 10, 5)
    try:
        check("the pretend date", today(), date(2026, 10, 5))
        check("the real date", real_today(), date(2026, 10, 1))
        check("make it monday", parse_test_command("make it monday")["date"],
              date(2026, 9, 28))
        check("make it tuesday", parse_test_command("make it tuesday")["date"],
              date(2026, 9, 29))
        check("make it next monday",
              parse_test_command("make it next monday")["date"], date(2026, 10, 5))
        check("make it last monday",
              parse_test_command("make it last monday")["date"], date(2026, 9, 21))
        check("make it today", read_day("today"), date(2026, 10, 1))
        check("make it tomorrow", read_day("tomorrow"), date(2026, 10, 2))
        check("make it yesterday", read_day("yesterday"), date(2026, 9, 30))
        # A BARE DAY-AND-MONTH IS THE NEXT ONE ON OR AFTER THE REAL DATE. With
        # the pretend date as the base, "3 Oct" was read as 3 Oct NEXT year.
        check("a bare 3 Oct is this year's, counted from the real date",
              read_day("3 Oct"), date(2026, 10, 3))
        check("a bare 5 Oct is not pushed a year by the pretend date",
              read_day("5 Oct"), date(2026, 10, 5))
        check("a dated day is itself", read_day("2026-09-28"), date(2026, 9, 28))
        check("simulate monday", parse("simulate monday")["date"], date(2026, 9, 28))
        week = week_of(parse("simulate week")["date"])
        check("simulate week starts", week[0], date(2026, 9, 28))
        check("simulate week ends", week[-1], date(2026, 10, 4))
        check("week_of() with no date is the real week", week_of()[0],
              date(2026, 9, 28))
        check("simulate next monday", parse("simulate next monday")["date"],
              date(2026, 10, 5))
        check("simulate last week", parse("simulate last week")["date"],
              date(2026, 9, 21))
        check("next weekday scans from the real date", next_weekday(0),
              date(2026, 10, 5))
        check("\"next day\" is a command, not a date (it moves the pretend day)",
              parse_test_command("next day")["cmd"], CMD_NEXT_DAY)
        # "back to today": the pretend clock is dropped, so both agree again.
        clock.today_ist = lambda: clock.real_now_ist().date()
        check("back to today", today(), date(2026, 10, 1))
    finally:
        clock.real_now_ist, clock.today_ist = was_now, was_pretend

    print("\nrendering")
    config.ROSTER_DISPLAY_NAMES = {"111": "Vaishnavi", "222": "Sid"}
    config.SIMULATION_REAL_MENTIONS = False
    check("mentions become names",
          strip_mentions("<@111> <@222> hello"), "@Vaishnavi @Sid hello")
    check("an unknown id still reads",
          strip_mentions("<@999> hi"), "@user999 hi")
    config.SIMULATION_REAL_MENTIONS = True
    check("...unless real mentions are on",
          strip_mentions("<@111> hi"), "<@111> hi")
    config.SIMULATION_REAL_MENTIONS = False
    check("the prefix is applied", prefix("hello").startswith("[TEST]"), True)
    check("a DM is shown, not sent",
          dm_line("Vaishnavi", "chase this"), "[DM to Vaishnavi] chase this")

    print("\nwhy was it quiet")
    check("asked plainly", is_why_quiet("why was it quiet?"), True)
    check("asked about skips", is_why_quiet("what did you skip today"), True)
    check("not every question", is_why_quiet("what is the queue"), False)
    check("no run yet says so", "no plan to explain" in why_quiet(None), True)
    pts = day_points(
        planned={"rolled": [
            {"rule_id": "R4", "rule_name": "Deliverables checklist", "actions": [1, 2]},
            {"rule_id": "R4", "rule_name": "Deliverables checklist", "actions": [3]},
        ], "held": []},
        rules_run=[{"id": "R12", "name": "Sales packages", "ran": False,
                    "why": "does not run on Monday (runs Thursday)"},
                   {"id": "R6", "name": "LinkedIn", "ran": True, "items": 0},
                   {"id": "R9", "name": "Meeting done", "ran": False,
                    "why": "disabled in bot_rules.yaml"}])
    text = why_quiet({
        "date": date(2026, 9, 28), "sent": 3,
        "planned": {"rolled": [
            {"rule_id": "R4", "rule_name": "Deliverables checklist", "actions": [1, 2]},
            {"rule_id": "R4", "rule_name": "Deliverables checklist", "actions": [3]},
        ], "held": []},
        "rules_run": [{"id": "R12", "name": "Sales packages", "ran": False,
                       "why": "does not run on Monday (runs Thursday)"},
                      {"id": "R6", "name": "LinkedIn", "ran": True, "items": 0},
                      {"id": "R9", "name": "Meeting done", "ran": False,
                       "why": "disabled in bot_rules.yaml"}],
    })
    check("same points as day_points", len(pts["rolled"]), 1)
    print("   " + text.replace("\n", "\n   "))
    check("reports the count", "• Sent: 3" in text, True)
    check("rolled is counted per rule", "P1 deliverables due this week — title and due date only (3)" in text, True)
    check("reports what looked and found nothing",
          "Looked, nothing due: people connected on LinkedIn" in text, True)
    check("reports what is not today's", "Not a Monday rule: sales packages" in text, True)
    check("a disabled rule is not mentioned", "no next steps" in text, False)
    check("no rule codes", re.findall(r"\bR\d{1,2}\b", text), [])
    check("at most six lines", len(text.splitlines()) <= 6, True)
    check("every bullet within 15 words",
          all(len(l.split()) - 1 <= BULLET_MAX_WORDS
              for l in text.splitlines() if l.startswith("•")), True)
    check("a long list is cut with 'and N more'",
          fit_bullet("Not a Monday rule:", ["a b c d e", "f g h i j", "k l m n o"]),
          "• Not a Monday rule: a b c d e and 2 more")

    print("\npacing")
    check("fast uses the configured gap",
          pace_seconds(fast=True, count=6), float(config.SIMULATION_FAST_GAP_SECONDS))
    check("real is compressed, not real",
          pace_seconds(fast=False, count=6) <= config.SIMULATION_MAX_SECONDS, True)

    print("\nthe gates")
    config.SALES_TEST_CHANNEL_ID = 4242
    config.TEAM_ROSTER_IDS = [111, 222]
    config.SALES_APPROVER_IDS = [111]
    check("wrong channel is SILENT", may_run(999, 111), (False, ""))
    check("right channel, non-approver is refused",
          may_run(4242, 222)[0], False)
    check("...with a reason", bool(may_run(4242, 222)[1]), True)
    check("right channel and approver", may_run(4242, 111), (True, ""))

    print("\nthe clock")
    config.SALES_TEST_MODE = False
    ok, why = set_clock_offset(3)
    check("advancing is refused outside test mode", ok, False)
    check("...and says why", "SALES_TEST_MODE" in why, True)
    config.SALES_TEST_MODE = True
    ok, _why = set_clock_offset(3)
    check("allowed in test mode", ok, True)
    check("today moves", today(), dl.today_ist() + timedelta(days=3))
    reset_clock()
    check("reset puts it back", today(), dl.today_ist())
    config.SALES_TEST_MODE = False
    check("the offset is inert outside test mode", clock_offset_days(), 0)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

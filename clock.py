"""THE CLOCK — what time the bot thinks it is, and the only place that decides.

Everything the bot does is reckoned against a date: which rules run, when the
posting window opens, when a deadline falls, how long an undo stays open, what
"three days ago" means. Until now each of those asked `datetime.now()` through
`deadlines.today_ist()`, which is correct and also untestable — the only way to
see what the bot does on Thursday was to wait for Thursday.

THE PRETEND CLOCK. A tester says "make it Monday" and the bot lives on Monday:
`now_ist()` returns a Monday, every rule sees a Monday, every deadline is
counted from a Monday, and it STAYS Monday until they say "next day" or "back
to today". It is stored in SQLite rather than in memory because a tester who
restarts the bot has not changed their mind about what day it is — a pretend
clock that quietly evaporated on a restart would be worse than none, because
the bot would carry on behaving as if it were still Monday's tester.

    pretend now = pretend start + (real now - real start)

so time still MOVES. An hour of testing is an hour on the pretend clock, which
is what makes a gap between two posts mean something. What it will not do is
roll into the next day on its own: the date is held at the pretend day (the
last second of it, if a tester really does sit there past midnight) because
"make it Monday" is an instruction about the day, and a day that changed itself
while somebody was mid-test would invalidate the test without telling them.

THIS IS NOT THE SIMULATION SANDBOX and the two must not be confused.
`simulation.py` previews a day against a THROWAWAY COPY of the database and
writes nothing anywhere; that is still there and still the right tool for a
quick look. Under this clock the state is REAL — writes go to the configured
sheet, approvals and undo work, the nudge-and-drop ladder climbs — because the
things worth testing over several days are exactly the ones that leave a trace.
That is also why it refuses to run outside SALES_TEST_MODE: on a live bot it
would move every deadline the team depends on.
"""
import logging
import os
import sqlite3
import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

import config

log = logging.getLogger(__name__)

# The team works in IST and every date this bot reckons is a calendar date in
# this zone. Defined HERE rather than in `deadlines.py` so that the module
# answering "what time is it" has no dependencies of its own — `deadlines`
# imports this name and re-exports it, so `dl.IST` still means what it did.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS test_clock (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    pretend_start TEXT NOT NULL,
    real_start    TEXT NOT NULL,
    set_by        TEXT NOT NULL DEFAULT '',
    set_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_lock = threading.RLock()
# {"pretend_start": datetime, "real_start": datetime, "set_by": str} or {}.
_state: dict = {}
_loaded = False

# WHERE A PRETEND DAY STARTS when nobody said a time. Early enough that the
# whole posting window is still ahead of the tester.
DEFAULT_START = time(9, 0)


# -- the real clock -----------------------------------------------------------


def real_now_ist() -> datetime:
    """The actual wall clock, IST. Nothing pretends here."""
    return datetime.now(IST)


def real_today_ist() -> date:
    return real_now_ist().date()


# -- storage ------------------------------------------------------------------


def _db_path() -> str:
    return (str(config.DB_PATH or "").strip() or "./sales_bot.db")


def _connect():
    """Our own connection to the bot's database.

    OUR OWN, not the `DB` object's. This module is imported by `deadlines`,
    which is imported by everything including `db` — taking the DB class here
    would be a cycle. Connections are per-operation throughout this codebase
    anyway, and the clock is read from memory after the first load, so the cost
    is one connect at boot.
    """
    c = sqlite3.connect(_db_path())
    c.row_factory = sqlite3.Row
    return c


def _parse(value: str) -> Optional[datetime]:
    try:
        out = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return out if out.tzinfo else out.replace(tzinfo=IST)


def _load() -> dict:
    """Read the stored clock once, then serve it from memory.

    A FAILURE HERE IS NOT A PRETEND CLOCK. Every caller of `now_ist()` is on a
    hot path and none of them can do anything useful with a database error, so
    an unreadable table means "no pretend clock" — the bot runs on the real
    time, which is the safe answer and the one it would have given anyway.
    """
    global _loaded
    with _lock:
        if _loaded:
            return dict(_state)
        _loaded = True
        try:
            with _connect() as c:
                c.executescript(_SCHEMA)
                row = c.execute(
                    "SELECT pretend_start, real_start, set_by FROM test_clock "
                    "WHERE id = 1"
                ).fetchone()
        except Exception:
            log.debug("[clock] the stored clock could not be read", exc_info=True)
            return {}
        if not row:
            return {}
        pretend, real = _parse(row["pretend_start"]), _parse(row["real_start"])
        if pretend is None or real is None:
            log.warning("[clock] the stored clock is unreadable; ignoring it")
            return {}
        _state.update({
            "pretend_start": pretend, "real_start": real,
            "set_by": str(row["set_by"] or ""),
        })
        log.warning(
            "[clock] A PRETEND CLOCK IS SET: the bot thinks it is %s. Set by %s. "
            "Say 'back to today' in the test channel to clear it.",
            describe(), _state["set_by"] or "somebody",
        )
        return dict(_state)


def _save(pretend_start: datetime, *, by: str) -> None:
    with _lock:
        _state.update({
            "pretend_start": pretend_start, "real_start": real_now_ist(),
            "set_by": str(by or ""),
        })
    try:
        with _connect() as c:
            c.executescript(_SCHEMA)
            c.execute(
                "INSERT INTO test_clock (id, pretend_start, real_start, set_by) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET pretend_start = excluded.pretend_start, "
                "real_start = excluded.real_start, set_by = excluded.set_by, "
                "set_at = CURRENT_TIMESTAMP",
                (_state["pretend_start"].isoformat(),
                 _state["real_start"].isoformat(), _state["set_by"]),
            )
            c.commit()
    except Exception:
        log.exception("[clock] the pretend clock could not be stored; it will "
                      "be lost on the next restart")


def _clear() -> None:
    with _lock:
        _state.clear()
    try:
        with _connect() as c:
            c.executescript(_SCHEMA)
            c.execute("DELETE FROM test_clock WHERE id = 1")
            c.commit()
    except Exception:
        log.exception("[clock] the pretend clock could not be cleared from storage")


def forget() -> None:
    """Drop the cached clock so the next read goes back to storage.

    For the test-state wipe, which deletes the database file underneath us.
    """
    global _loaded
    with _lock:
        _state.clear()
        _loaded = False


# -- the clock the bot actually reads -----------------------------------------


def pretending() -> bool:
    return bool(_load())


def now_ist() -> datetime:
    """What time the bot thinks it is. The pretend clock, or the real one.

    THE DATE IS HELD. Elapsed real time is added so the clock moves, but never
    past the end of the pretend day — see the module docstring.
    """
    state = _load()
    if not state:
        return real_now_ist()
    elapsed = real_now_ist() - state["real_start"]
    if elapsed < timedelta(0):          # the machine's clock went backwards
        elapsed = timedelta(0)
    out = state["pretend_start"] + elapsed
    if out.date() != state["pretend_start"].date():
        return datetime.combine(
            state["pretend_start"].date(), time(23, 59, 59), tzinfo=IST
        )
    return out


def today_ist() -> date:
    return now_ist().date()


# -- describing it ------------------------------------------------------------


def format_moment(when: Optional[datetime] = None) -> str:
    """"Monday 28 Sep, 2:00 PM" — the way the opening line says it."""
    moment = when or now_ist()
    hour = moment.strftime("%I").lstrip("0") or "12"
    return (
        f"{moment.strftime('%A')} {moment.strftime('%d').lstrip('0')} "
        f"{moment.strftime('%b')}, {hour}:{moment.strftime('%M %p')}"
    )


def describe(when: Optional[datetime] = None) -> str:
    """The moment, marked as test time when the clock is pretending."""
    line = format_moment(when)
    return f"{line} (test time)" if pretending() else line


def status() -> dict:
    """{pretending, now, real_now, set_by} — for the boot log and `test help`."""
    state = _load()
    return {
        "pretending": bool(state),
        "now": now_ist(),
        "real_now": real_now_ist(),
        "set_by": str(state.get("set_by") or ""),
        "db_path": _db_path(),
    }


# -- moving it ----------------------------------------------------------------


def _refuse() -> tuple:
    return False, (
        "I can only change the day with SALES_TEST_MODE turned on. On the live "
        "bot it would move every deadline the team is working to."
    )


def set_day(day: date, *, at: Optional[time] = None, by: str = "") -> tuple:
    """(ok, message). Make it `day`, starting at `at` (default 9:00 IST)."""
    if not config.SALES_TEST_MODE:
        return _refuse()
    start = datetime.combine(day, at or DEFAULT_START, tzinfo=IST)
    _save(start, by=by)
    log.warning("[clock] the pretend clock is now %s (set by %s)",
                describe(), by or "somebody")
    return True, f"Right — it's now {describe()}."


def set_time_of_day(at: time, *, by: str = "") -> tuple:
    """Jump to a time on the SAME pretend day, keeping the clock running.

    This is what the test posting run uses to stand at 10:00 and then at 14:00:
    the day must not change underneath it, and the posts have to be reckoned
    against the hour they would really go out at.
    """
    state = _load()
    if not state:
        return False, "There is no pretend day set, so there is no day to move within."
    start = datetime.combine(state["pretend_start"].date(), at, tzinfo=IST)
    _save(start, by=by or state.get("set_by", ""))
    return True, f"It's now {describe()}."


def next_day(*, by: str = "") -> tuple:
    """(ok, message). Tomorrow, from wherever the clock is standing."""
    if not config.SALES_TEST_MODE:
        return _refuse()
    state = _load()
    base = state["pretend_start"].date() if state else real_today_ist()
    return set_day(base + timedelta(days=1), by=by)


def back_to_today(*, by: str = "") -> tuple:
    """(ok, message). Drop the pretend clock; the real date comes back."""
    if not _load():
        return True, (
            f"The clock was already the real one — it's {describe()}."
        )
    _clear()
    log.warning("[clock] the pretend clock was cleared by %s; real time is back",
                by or "somebody")
    return True, f"Back to the real clock — it's {describe()}."


def _self_test() -> int:
    """`python -m clock` — the arithmetic, against a throwaway database."""
    import shutil
    import tempfile

    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    tmp = tempfile.mkdtemp(prefix="saley-clock-")
    config.DB_PATH = os.path.join(tmp, "clock_test.db")
    try:
        print("the real clock")
        config.SALES_TEST_MODE = False
        forget()
        check("no pretend clock by default", pretending(), False)
        check("today is today", today_ist(), real_today_ist())
        ok, why = set_day(date(2026, 9, 28))
        check("refused outside test mode", ok, False)
        check("...and says why", "SALES_TEST_MODE" in why, True)

        print("\nmake it Monday")
        config.SALES_TEST_MODE = True
        ok, line = set_day(date(2026, 9, 28), at=time(14, 0), by="tester")
        check("allowed in test mode", ok, True)
        check("the day moved", today_ist(), date(2026, 9, 28))
        check("the opening line reads plainly",
              "Monday 28 Sep, 2:00 PM (test time)" in line, True)
        check("it is marked as test time", "(test time)" in describe(), True)
        check("time still moves forward", now_ist() >= datetime(
            2026, 9, 28, 14, 0, tzinfo=IST), True)

        print("\nit survives a restart")
        forget()
        check("re-read from SQLite", today_ist(), date(2026, 9, 28))
        check("...still pretending", pretending(), True)

        print("\nmoving within the day")
        set_time_of_day(time(10, 0), by="tester")
        check("the hour moved", now_ist().hour, 10)
        check("the day did not", today_ist(), date(2026, 9, 28))

        print("\nnext day")
        next_day(by="tester")
        check("tomorrow", today_ist(), date(2026, 9, 29))
        check("...starts at the default hour", now_ist().hour, DEFAULT_START.hour)

        print("\nback to today")
        ok, line = back_to_today(by="tester")
        check("cleared", ok, True)
        check("the real date is back", today_ist(), real_today_ist())
        check("no longer pretending", pretending(), False)
        check("saying it twice is harmless", back_to_today()[0], True)

        print("\nthe date is held, not rolled")
        set_day(date(2026, 9, 28), at=time(23, 0), by="tester")
        with _lock:
            _state["real_start"] = real_now_ist() - timedelta(hours=5)
        check("five hours later it is still Monday", today_ist(), date(2026, 9, 28))
        check("...held at the last second", now_ist().hour, 23)
        back_to_today()
    finally:
        config.SALES_TEST_MODE = False
        forget()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

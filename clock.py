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

    pretend now = the pretend DATE + the REAL IST time of day

THE PRETEND CLOCK OWNS THE DATE ONLY. Testers move the day; the wall clock is
always IST. "make it Monday" at 13:17 is Monday 28 Sep, 1:17 PM (test time),
and a reminder asked for at 1:19 PM fires at 1:19 PM real time. (It used to be
pretend start + elapsed, which left the clock standing at a test day's 14:00
stop for the rest of the afternoon — a tester at 13:17 was told 1:17 PM "has
already passed".)

A PRETEND DAY ENDS WHEN THE REAL DAY CHANGES (8 Oct 2026). On 8 Oct the bot
still thought it was Wed 7 Oct: somebody had said "make it Wednesday" the day
before and nobody said "back to today", and because the clock is stored it
survived the night and the restart. Every answer that morning was a day out.
So the stored clock remembers the real date it was set on (`real_start`), and
the first read on a later real date clears it, logs that once, and carries on
with the real date. Within one real day nothing changes: "make it …", "next
day" and a test day all work as before, and a restart still keeps the day.

THE ONE EXCEPTION IS TEMPORARY. A test day has to live through its 10:00 and
14:00 stops, so `set_time_of_day` sets an IN-MEMORY override (moving with real
time from the stop) that `_run_test_day` clears in a `finally`. It is never
stored: a restart, or the end of the run, puts the clock back to pretend date +
real IST time.

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
# ONLY THE DATE of pretend_start is used; the time of day is always real.
_state: dict = {}
_loaded = False
# THE TEST-DAY STOP: {"at": time, "real_start": datetime} or {}. In memory only.
_override: dict = {}

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
            return {} if _expired() else dict(_state)
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
        if _expired():
            return {}
        log.warning(
            "[clock] A PRETEND CLOCK IS SET: the bot thinks it is %s. Set by %s. "
            "Say 'back to today' in the test channel to clear it.",
            describe(), _state["set_by"] or "somebody",
        )
        return dict(_state)


def _expired() -> bool:
    """Was the pretend day set on an EARLIER real date? If so it is over: clear
    it (memory and storage), say so once, and answer True.

    Called with `_lock` held, on every read, so a bot left running overnight
    drops yesterday's pretend day at the first thing it does after midnight,
    and a bot restarted the next morning drops it as it loads.
    """
    if not _state:
        return False
    set_on = _state["real_start"].astimezone(IST).date()
    today = real_today_ist()
    if set_on >= today:
        return False
    was = _state["pretend_start"].date()
    by = _state.get("set_by") or "somebody"
    _override.clear()
    _clear()
    log.warning(
        "[clock] the pretend day (%s, set on %s by %s) has ended because the real "
        "day changed; the clock is the real one again: %s.",
        was.isoformat(), set_on.isoformat(), by, today.isoformat(),
    )
    return True


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
        _override.clear()
        _loaded = False


# -- the clock the bot actually reads -----------------------------------------


def pretending() -> bool:
    return bool(_load())


def now_ist() -> datetime:
    """What time the bot thinks it is: the pretend DATE + the real IST time.

    No pretend date -> the real IST clock. During a test day's stop (the
    temporary override) the stop's time, moving with real time, never past the
    end of the pretend day.
    """
    state = _load()
    real = real_now_ist()
    if not state:
        return real
    day = state["pretend_start"].date()
    with _lock:
        ov = dict(_override)
    if ov:
        moved = max(timedelta(0), real - ov["real_start"])
        out = datetime.combine(day, ov["at"], tzinfo=IST) + moved
        if out.date() != day:
            return datetime.combine(day, time(23, 59, 59), tzinfo=IST)
        return out
    return datetime.combine(day, real.time(), tzinfo=IST)


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
    """The moment, marked "(test time)" while a pretend date is set, "(IST)"
    otherwise. EVERY reply that quotes the time uses this, never a
    hand-formatted datetime."""
    line = format_moment(when)
    return f"{line} (test time)" if pretending() else f"{line} (IST)"


def real_date_label() -> str:
    """"Thu 1 Oct" — the REAL date, the short way a person writes it."""
    real = real_today_ist()
    return f"{real.strftime('%a')} {real.day} {real.strftime('%b')}"


def describe_with_real() -> str:
    """"Monday 28 Sep, 1:17 PM (test time) — the real date is Thu 1 Oct."

    WHAT A TESTER IS TOLD WHEN THEY NAME A DAY. Day names are read against the
    real date, so the reply says both: where the clock now stands, and the
    date it was counted from.
    """
    return f"{describe()} — the real date is {real_date_label()}."


def status() -> dict:
    """{pretending, now, real_now, set_by, pretend_date, override} — for the
    boot log and `test help`."""
    state = _load()
    with _lock:
        ov = dict(_override)
    return {
        "pretending": bool(state),
        "now": now_ist(),
        "real_now": real_now_ist(),
        "pretend_date": state["pretend_start"].date() if state else None,
        "override": ov.get("at"),
        "set_by": str(state.get("set_by") or ""),
        "db_path": _db_path(),
    }


def process_timezone() -> str:
    """The zone the PROCESS runs in (TZ env, time.tzname) — for the boot log,
    so a server on UTC is visible at a glance. The bot never depends on it."""
    import time as _time
    return (f"TZ={os.environ.get('TZ') or '(unset)'}, "
            f"tzname={'/'.join(_time.tzname)}, "
            f"local offset={datetime.now().astimezone().strftime('%z')}")


# -- moving it ----------------------------------------------------------------


def _refuse() -> tuple:
    return False, (
        "I can only change the day with SALES_TEST_MODE turned on. On the live "
        "bot it would move every deadline the team is working to."
    )


def set_day(day: date, *, at: Optional[time] = None, by: str = "") -> tuple:
    """(ok, message). Make it `day`. The time of day stays the real IST time.

    `at` is accepted for old callers and ignored: the pretend clock owns the
    date only.
    """
    if not config.SALES_TEST_MODE:
        return _refuse()
    start = datetime.combine(day, DEFAULT_START, tzinfo=IST)
    clear_time_override(why="the day was changed")
    _save(start, by=by)
    log.warning("[clock] the pretend clock is now %s (set by %s)",
                describe(), by or "somebody")
    return True, f"Right — it's now {describe()}."


def set_time_of_day(at: time, *, by: str = "") -> tuple:
    """A TEMPORARY stop on the same pretend day: 10:00, then 14:00.

    What the test posting run uses so its plan is lived through at the hour the
    posts would really go out. IN MEMORY ONLY, moving with real time from the
    stop, and `_run_test_day` clears it in a `finally` (`clear_time_override`).
    """
    state = _load()
    if not state:
        return False, "There is no pretend day set, so there is no day to move within."
    with _lock:
        _override.clear()
        _override.update({"at": at, "real_start": real_now_ist()})
    log.info("[clock] test-day stop: %s (temporary; set by %s)", describe(),
             by or state.get("set_by") or "somebody")
    return True, f"It's now {describe()}."


def clear_time_override(*, why: str = "") -> bool:
    """Drop the test-day stop. True when there was one. Logged."""
    with _lock:
        had = bool(_override)
        _override.clear()
    if had:
        log.info("[clock] the test-day time stop was cleared%s; the clock is back to "
                 "%s", f" ({why})" if why else "", describe())
    return had


def next_day(*, by: str = "") -> tuple:
    """(ok, message). Tomorrow, from wherever the clock is standing."""
    if not config.SALES_TEST_MODE:
        return _refuse()
    state = _load()
    base = state["pretend_start"].date() if state else real_today_ist()
    return set_day(base + timedelta(days=1), by=by)


def back_to_today(*, by: str = "") -> tuple:
    """(ok, message). Drop the pretend clock; the real date comes back."""
    clear_time_override(why="back to today")
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
        ok, line = set_day(date(2026, 9, 28), by="tester")
        real = real_now_ist()
        check("allowed in test mode", ok, True)
        check("the day moved", today_ist(), date(2026, 9, 28))
        check("the time of day is the REAL IST time",
              (now_ist().hour, now_ist().minute), (real.hour, real.minute))
        check("the opening line reads plainly",
              line, f"Right — it's now {format_moment(now_ist())} (test time).")
        check("it is marked as test time", "(test time)" in describe(), True)

        print("\nit survives a restart")
        forget()
        check("re-read from SQLite", today_ist(), date(2026, 9, 28))
        check("...still pretending", pretending(), True)

        print("\nthe test-day stop is temporary")
        set_time_of_day(time(10, 0), by="tester")
        check("the hour moved", now_ist().hour, 10)
        check("the day did not", today_ist(), date(2026, 9, 28))
        forget()
        check("a restart drops the stop", now_ist().hour, real_now_ist().hour)
        set_time_of_day(time(14, 0), by="tester")
        check("the afternoon stop", now_ist().hour, 14)
        check("clearing it says so", clear_time_override(why="run ended"), True)
        check("back to the real time of day", now_ist().hour, real_now_ist().hour)
        check("...on the pretend date", today_ist(), date(2026, 9, 28))

        print("\nnext day")
        next_day(by="tester")
        check("tomorrow", today_ist(), date(2026, 9, 29))
        check("...at the real time of day", now_ist().hour, real_now_ist().hour)

        print("\nback to today")
        ok, line = back_to_today(by="tester")
        check("cleared", ok, True)
        check("the real date is back", today_ist(), real_today_ist())
        check("no longer pretending", pretending(), False)
        check("saying it twice is harmless", back_to_today()[0], True)
        check("the real clock says (IST)", describe().endswith("(IST)"), True)

        print("\na pretend day ends when the real day changes")
        set_day(date(2026, 9, 28), by="tester")
        check("set today: it stays", (pretending(), today_ist()), (True, date(2026, 9, 28)))
        forget()
        check("...across a restart on the same real day",
              (pretending(), today_ist()), (True, date(2026, 9, 28)))
        yesterday = (real_now_ist() - timedelta(days=1)).isoformat()
        with _connect() as c:
            c.execute("UPDATE test_clock SET real_start = ? WHERE id = 1", (yesterday,))
            c.commit()
        forget()
        check("set yesterday: gone on today's first read",
              (pretending(), today_ist()), (False, real_today_ist()))
        with _connect() as c:
            left = c.execute("SELECT COUNT(*) FROM test_clock").fetchone()[0]
        check("...and cleared from storage, so it does not come back", left, 0)
        set_day(date(2026, 9, 28), by="tester")
        with _lock:
            _state["real_start"] = real_now_ist() - timedelta(days=1)
        check("a bot left running overnight drops it too, without a restart",
              (today_ist(), pretending()), (real_today_ist(), False))
        ok, _line = set_day(date(2026, 9, 28), by="tester")
        check("a new pretend day can be set straight away", (ok, today_ist()),
              (True, date(2026, 9, 28)))
        next_day(by="tester")
        check("...and 'next day' still moves it", today_ist(), date(2026, 9, 29))
        back_to_today()

        print("\na stop never rolls into the next day")
        set_day(date(2026, 9, 28), by="tester")
        set_time_of_day(time(23, 0), by="tester")
        with _lock:
            _override["real_start"] = real_now_ist() - timedelta(hours=5)
        check("five hours later it is still Monday", today_ist(), date(2026, 9, 28))
        check("...held at the last second", now_ist().hour, 23)
        back_to_today()
        check("back to today clears the stop too", status()["override"], None)
    finally:
        config.SALES_TEST_MODE = False
        forget()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

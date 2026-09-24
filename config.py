"""Configuration loaded from environment variables.

This is the SALES & MARKETING bot — a separate Discord application from the PM
bot, with its own token, its own channels, its own database and its own venv.
Nothing here knows about issue trackers or ticket creation; this bot's only output
is a Discord message in a sales channel.

The values that matter most are the SCOPE ones:

  SALES_CHANNEL_IDS   — the ONLY channels the bot may read from or post in.
                        Enforced in code at BOTH ends (see guardrails.py), and
                        that enforcement is the second layer: the bot's Discord
                        role should also be denied View Channel everywhere else
                        (DEPLOY.md).
  SALES_ASK_CHANNEL_ID— the sales channel the bot POSTS in unprompted: the daily
                        digest and the deadline announcements. It is NOT an
                        answering exemption — everywhere, the bot replies only
                        when @-mentioned or replied to.
  TEAM_ROSTER_IDS     — the only people the bot may ever @-mention.

`validate()` returns the missing REQUIRED vars and logs loud warnings for the
configurations that are legal but almost certainly a mistake.
"""
import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


def _int_list(name: str) -> list[int]:
    """Comma-separated env value → list of ints, skipping anything unparseable
    (with a warning) rather than crashing startup on one bad id."""
    out: list[int] = []
    for raw in (os.getenv(name, "") or "").replace(";", ",").split(","):
        tok = raw.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            log.warning("%s contains %r, which is not a Discord id; skipping it", name, tok)
    return out


def _int_set(name: str) -> set[int]:
    return set(_int_list(name))


def _str_list(name: str, default: str = "") -> list[str]:
    """Comma-separated env value → list of stripped strings (casing preserved)."""
    raw = os.getenv(name, default)
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def _lower_str_set(name: str, default: str = "") -> set[str]:
    return {x.lower() for x in _str_list(name, default)}


# Weekday names as a human writes them. Used by every "which day does X happen"
# setting, so "fri", "Friday" and "FRI" are the same day everywhere and there is
# exactly one parser to be wrong.
_WEEKDAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def _weekday(name: str, default: str) -> int:
    """One weekday name (or a bare 0-6) → Python's weekday index, 0=Monday.

    An unreadable value falls back to the default WITH a warning rather than to
    "never fires", which is the failure nobody notices: a reminder that silently
    stopped looks exactly like a week with nothing to remind about.
    """
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        raw = default.strip().lower()
    if raw.isdigit() and 0 <= int(raw) <= 6:
        return int(raw)
    if raw in _WEEKDAY_NAMES:
        return _WEEKDAY_NAMES[raw]
    log.warning(
        "%s=%r is not a weekday (mon..sun or 0-6); using %s instead", name, raw, default
    )
    return _WEEKDAY_NAMES.get(default.strip().lower(), 4)


def _weekdays(name: str, default: str) -> set[int]:
    """A comma-separated list of weekday names → a set of weekday indices.

    Unreadable tokens are SKIPPED with a warning rather than taking the whole
    list down: "mon,fri" with a typo in one of them should still fire on the
    other.
    """
    def parse(text: str, *, complain: bool) -> set[int]:
        found: set[int] = set()
        for tok in (text or "").replace(";", ",").split(","):
            t = tok.strip().lower()
            if not t:
                continue
            if t.isdigit() and 0 <= int(t) <= 6:
                found.add(int(t))
            elif t in _WEEKDAY_NAMES:
                found.add(_WEEKDAY_NAMES[t])
            elif complain:
                log.warning("%s contains %r, which is not a weekday; skipping it", name, tok)
        return found

    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return parse(default, complain=False)
    out = parse(raw, complain=True)
    if not out:
        log.warning("%s=%r named no usable weekday; using %s instead", name, raw, default)
        return parse(default, complain=False)
    return out


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    log.warning("%s=%r is not a recognised boolean; using default %s", name, raw, default)
    return default


# -- reading .env again, after boot -------------------------------------------
# `load_dotenv()` above runs once, at import. That is right for almost every
# setting here: they are read into module constants and a change needs a restart
# anyway. The digest kill switch is the exception — it has to be flippable
# without one — so it re-reads the file through this cache.
#
# Cached on (path, mtime, size): an unchanged file is parsed once and every
# later call is a stat. mtime alone can miss an edit inside the same second on a
# coarse filesystem, so size rides along with it.
_DOTENV_CACHE: dict = {"key": None, "values": {}}


def _dotenv_value(name: str):
    """The current value of `name` in the .env file, or None if it isn't there.

    Returns None — not a default — for every failure: no file, unreadable file,
    key absent. The caller decides what "absent" means; this only reports what
    the file says right now.
    """
    from dotenv import dotenv_values, find_dotenv

    try:
        path = find_dotenv(usecwd=True)
        if not path:
            return None
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        if _DOTENV_CACHE["key"] != key:
            _DOTENV_CACHE["values"] = dict(dotenv_values(path) or {})
            _DOTENV_CACHE["key"] = key
    except OSError:
        # A .env that vanished or went unreadable mid-run. Say nothing at every
        # tick about it; the caller falls back to the boot value.
        return None
    except Exception:
        log.debug("could not re-read .env for %s", name, exc_info=True)
        return None
    return _DOTENV_CACHE["values"].get(name)


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; using default %s", name, raw, default)
        return default


def _float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using default %s", name, raw, default)
        return default


def _json_object(name: str, default: dict) -> dict:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return dict(default)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("Could not parse %s as JSON (%s); using default", name, e)
        return dict(default)
    if not isinstance(parsed, dict):
        log.warning("%s must be a JSON object; got %s. Using default.", name, type(parsed).__name__)
        return dict(default)
    return parsed


# -- Discord identity ---------------------------------------------------------

# The token of the NEW sales Discord application. NOT the PM bot's token — this
# is a different bot user, in the same server, with a different role.
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# THE scope. The only channels this bot reads from or posts in, enforced at both
# ends by guardrails.py. An empty list means the bot can do nothing at all, which
# validate() refuses to start on — silence is better than a bot loose in a server.
SALES_CHANNEL_IDS: list[int] = _int_list("SALES_CHANNEL_IDS")

# The sales channel that is the bot's POSTING home: the daily digest and the
# deadline announcements go here by default. It buys NO answering exemption —
# in this channel, exactly as in every other one, the bot answers only when it
# is @-mentioned or when someone replies to one of its own messages. Must itself
# be a sales channel — validate() folds it in if someone forgets, so the ask
# channel can never become a hole in the scope rule.
SALES_ASK_CHANNEL_ID = _int("SALES_ASK_CHANNEL_ID", 0)

if SALES_ASK_CHANNEL_ID and SALES_ASK_CHANNEL_ID not in SALES_CHANNEL_IDS:
    log.warning(
        "SALES_ASK_CHANNEL_ID=%s is not in SALES_CHANNEL_IDS — adding it, since the "
        "ask channel is by definition a channel the bot reads and answers in.",
        SALES_ASK_CHANNEL_ID,
    )
    SALES_CHANNEL_IDS.append(SALES_ASK_CHANNEL_ID)

# THE TEST CHANNEL JOINS THE SCOPE THE SAME WAY, and for the same reason: it is
# by definition a channel the bot reads commands in and posts simulations to.
# An operator sets one variable; forgetting to also list it here would make
# every simulation silently unreachable, which is the worst possible failure for
# a feature whose entire job is to show you what the bot would do.
#
# Read late — SALES_TEST_CHANNEL_ID is defined further down with the rest of the
# simulation settings, so this block is re-run at the end of the module. See
# `_attach_test_channel`.
SALES_CHANNEL_ID_SET: set[int] = set(SALES_CHANNEL_IDS)


def _attach_test_channel() -> None:
    """Fold SALES_TEST_CHANNEL_ID into the scope. Idempotent."""
    cid = max(0, int(globals().get("SALES_TEST_CHANNEL_ID", 0) or 0))
    if not cid or cid in SALES_CHANNEL_ID_SET:
        return
    log.info(
        "[config] SALES_TEST_CHANNEL_ID=%s added to the bot's channel scope — it is "
        "where simulation commands are typed and where simulated messages go.", cid,
    )
    SALES_CHANNEL_IDS.append(cid)
    SALES_CHANNEL_ID_SET.add(cid)


def is_sales_channel(channel_id) -> bool:
    """THE scope predicate. Every read and every send passes through this (via
    guardrails.py). A non-numeric / None channel id is NOT a sales channel."""
    try:
        return int(channel_id) in SALES_CHANNEL_ID_SET
    except (TypeError, ValueError):
        return False


# -- Who the bot may address --------------------------------------------------

# The team roster: the ONLY Discord users this bot may @-mention. Anyone not on
# it is referred to by plain-text name, never pinged (guardrails.mention_for).
# Ids are authoritative; names are the spoofable fallback for people whose id we
# don't have yet, used only for display resolution.
TEAM_ROSTER_IDS: set[int] = _int_set("TEAM_ROSTER_IDS")
TEAM_ROSTER_NAMES: set[str] = _lower_str_set("TEAM_ROSTER_NAMES")

# Optional Discord id → display name, so a nudge can name someone the bot has
# never seen post. Purely cosmetic; it grants nobody any permission.
ROSTER_DISPLAY_NAMES: dict = _json_object("ROSTER_DISPLAY_NAMES", default={})


def is_on_roster(user_id, display_name: str = "") -> bool:
    """May the bot address this person at all? Id match wins; the name fallback
    exists only for roster entries we have no id for yet."""
    try:
        if int(user_id) in TEAM_ROSTER_IDS:
            return True
    except (TypeError, ValueError):
        pass
    name = (display_name or "").strip().lower()
    return bool(name) and name in TEAM_ROSTER_NAMES


def roster_id_for_name(display_name: str) -> int:
    """A NAME from a spreadsheet → the roster Discord id, or 0.

    The master tab's Owner column holds a human's name, not an id, and a name is
    SPOOFABLE — which is why this only ever resolves against ROSTER_DISPLAY_NAMES
    and TEAM_ROSTER_IDS. An unknown name returns 0, and the caller then names the
    person in plain text instead of pinging them. That is the failing-closed
    behaviour the roster gate is built on.

    Matching is case-insensitive and ignores surrounding punctuation, and a
    first-name-only match is accepted when it is UNAMBIGUOUS — sheets say
    "Vaishnavi", Discord says "Vaishnavi K".
    """
    want = " ".join(str(display_name or "").split()).strip().lower()
    if not want:
        return 0
    pairs = []
    for raw_id, name in (ROSTER_DISPLAY_NAMES or {}).items():
        try:
            uid = int(str(raw_id).strip())
        except (TypeError, ValueError):
            continue
        if uid in TEAM_ROSTER_IDS:
            pairs.append((uid, " ".join(str(name or "").split()).strip().lower()))

    for uid, name in pairs:
        if name and name == want:
            return uid
    # First token, but only when exactly one roster member answers to it.
    first = want.split()[0] if want.split() else ""
    if first:
        hits = [uid for uid, name in pairs if name.split()[:1] == [first]]
        if len(hits) == 1:
            return hits[0]
    return 0


# -- Model --------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Used for every LLM call the bot makes: query answering, commitment detection,
# nudge wording, social replies.
MODEL = os.getenv("MODEL", "claude-sonnet-4-6")

# -- Storage ------------------------------------------------------------------

# Fresh SQLite file for THIS bot. Never point it at the PM bot's bot_state.db.
DB_PATH = (os.getenv("DB_PATH", "") or "").strip() or "./sales_bot.db"

# -- SIMULATION AND TEST MODE -------------------------------------------------
# THE TEST CHANNEL. Every simulation command is typed here and every simulated
# message is posted here, and nowhere else.
#
# IT IS ADDED TO THE BOT'S SCOPE AUTOMATICALLY, exactly as the ask channel is —
# see `sales_channel_ids()`. An operator sets one variable; they do not also
# have to remember to add it to SALES_CHANNEL_IDS, and forgetting would make
# every simulation silently unreachable.
#
# 0/unset turns simulation off entirely: the commands are not recognised and the
# bot says so rather than pretending to run one.
SALES_TEST_CHANNEL_ID = _int("SALES_TEST_CHANNEL_ID", 0)


def test_channel_id() -> int:
    return max(0, int(SALES_TEST_CHANNEL_ID or 0))


def simulation_available() -> bool:
    return bool(test_channel_id())


# LIVE TEST MODE. Redirects ALL real proactive output — the drip, the proposal
# sweep, escalations, DMs — into the test channel, with NORMAL STATE AND REAL
# TIMING. It is not a simulation: the database is the real one, slots are
# claimed, events are marked sent. It exists to test replies, approvals, undo
# and appends end to end against a test sheet, where the only thing changed is
# who can see the output.
#
# POINT DB_PATH AT A *_test.db AND GTM_SHEET_ORIGINAL_ID AT A COPY before
# turning this on. The bot warns loudly at boot if you have not.
SALES_TEST_MODE = _bool("SALES_TEST_MODE", default=False)

# Whether a simulated message pings anybody. OFF by default and deliberately so:
# a simulated week would otherwise put forty notifications on two people's
# phones for messages that are not real. Names render as plain text instead.
SIMULATION_REAL_MENTIONS = _bool("SIMULATION_REAL_MENTIONS", default=False)

# The gap between simulated posts in "fast" mode, in seconds. Long enough to
# read in order, short enough that a week does not take an afternoon.
SIMULATION_FAST_GAP_SECONDS = _int("SIMULATION_FAST_GAP_SECONDS", 5)

# The longest a single simulation may run, in seconds. A week at real spacing
# would run for days, so "real" spacing in a simulation is COMPRESSED to this
# ceiling — the planned times are still reported exactly, the waiting is not.
SIMULATION_MAX_SECONDS = _int("SIMULATION_MAX_SECONDS", 900)

# The prefix on every simulated message. One string so it can be grepped for,
# and so nothing can post a simulated message without carrying it.
SIMULATION_PREFIX = (
    os.getenv("SIMULATION_PREFIX", "") or ""
).strip() or "[TEST]"

# -- THE PLAIN-LANGUAGE TEST RUN ----------------------------------------------
#
# These govern the "make it Monday" path, which is a different thing from the
# simulations above: the clock is PERSISTENT, the database is the REAL one and
# the writes are REAL. See clock.py and simulation.parse_test_command.

# The gap between two posts in a test run, in seconds. A test day is watched by
# somebody sitting there, so the real 90-minute spacing is compressed — but not
# to nothing: the posts have to arrive one at a time and in an order a person
# can follow, and a burst of six is the thing this number exists to prevent.
TEST_POST_GAP_SECONDS = _int("TEST_POST_GAP_SECONDS", 20)

# How long a "start over" confirmation stays open, in seconds. Short, because a
# stray "yes" in a conversation that has moved on must never be the thing that
# deletes a database.
TEST_CONFIRM_SECONDS = _int("TEST_CONFIRM_SECONDS", 120)

# WHERE THE CLOCK STANDS FOR EACH HALF OF A TEST DAY, "HH:MM" IST.
#
# TWO STOPS, NOT ONE, because the real day has two. The meeting-prep day-of
# touch is fixed at MEETING_DAYOF_TIME and lands outside the posting window;
# everything else waits for SALES_DRIP_START. Standing at one time would mean
# either the morning items never came due or the afternoon ones all did at
# once, and in both cases the tester would be watching a day that does not
# happen.
TEST_MORNING_TIME = (os.getenv("TEST_MORNING_TIME", "") or "").strip() or "10:00"
TEST_AFTERNOON_TIME = (os.getenv("TEST_AFTERNOON_TIME", "") or "").strip() or "14:00"


def test_morning_ist() -> tuple[int, int]:
    """TEST_MORNING_TIME as (hour, minute) IST. Unreadable falls back to 10:00."""
    import digest as _digest

    return _digest.parse_time(TEST_MORNING_TIME, default="10:00")


def test_afternoon_ist() -> tuple[int, int]:
    """TEST_AFTERNOON_TIME as (hour, minute) IST. Unreadable falls back to 14:00."""
    import digest as _digest

    return _digest.parse_time(TEST_AFTERNOON_TIME, default="14:00")

# Now that the id exists, put it in scope.
_attach_test_channel()


def is_test_channel(channel_id) -> bool:
    """True only for the test channel. Separate from `is_sales_channel` because
    the simulation commands are refused ANYWHERE ELSE — a "simulate week" typed
    in the real sales channel must do nothing at all."""
    cid = test_channel_id()
    if not cid:
        return False
    try:
        return int(channel_id) == cid
    except (TypeError, ValueError):
        return False

# The state contract for the future COSA supervisor: a rewritten-at-startup-and-
# daily snapshot, plus an append-only action log. Schemas are documented in
# README.md so a third process can consume them.
STATE_DIR = (os.getenv("STATE_DIR", "./state") or "./state").strip()

# -- Persona ------------------------------------------------------------------

# The bot's name — the one identity it uses everywhere it speaks. Naming is open;
# "Saley" is the default, and it is the name the bot introduces itself by, signs
# its state file with, and sends as its research User-Agent. One definition, read
# through config by persona.py, main.py, state.py and research.py — so renaming
# the bot is one env var and a restart, never a search-and-replace.
COS_NAME = (os.getenv("COS_NAME", "") or "").strip() or "Saley"

# The policy file at the repo root. persona.py RE-READS it on every query, so
# editing it takes effect without a restart.
SALES_POLICY_FILE = (os.getenv("SALES_POLICY_FILE", "./sales_policy.md") or "").strip()

# Voice on/off. When false the prompts fall back to a neutral voice; the policy
# is still loaded, because policy is not decoration.
COS_PERSONA_ENABLED = _bool("COS_PERSONA_ENABLED", default=True)

# -- Source layer -------------------------------------------------------------
# Three sources the bot reasons over (sources.py). Each self-reports connected /
# awaiting-access, and "what can you do" answers honestly from those statuses.

# Local folder of Drive-synced meeting notes (notes.py reads it). Empty → the
# meeting-notes source reports awaiting-access. The folder is CREATED at startup
# when it doesn't exist, so a fresh box doesn't need a manual mkdir before the
# first sync.
NOTES_DIR = (os.getenv("NOTES_DIR", "") or "").strip()

# The full sync command — an entire rclone invocation, run through the shell.
# Empty → no sync ever runs and the bot reads whatever is already on disk.
#
# WINDOWS: this runs as a SUBPROCESS, which does not see PowerShell aliases or
# functions. `rclone` must be on PATH or written as a full path to rclone.exe.
NOTES_SYNC_CMD = (os.getenv("NOTES_SYNC_CMD", "") or "").strip()
# How often the background sync runs, in minutes. It also runs once at startup
# and on demand before a notes question is answered.
NOTES_SYNC_MINUTES = _int("NOTES_SYNC_MINUTES", 30)
# Hard timeout for one sync. A first sync pulls every doc and is much slower than
# the incremental ones, so this is generous by default.
NOTES_SYNC_TIMEOUT_SECONDS = _int("NOTES_SYNC_TIMEOUT_SECONDS", 120)

# -- Which synced notes stay OUT of context -----------------------------------
# The sync pulls every "Notes by Gemini" doc shared with the sync account, and
# the bot loads ALL of them EXCEPT the recurring product standups. A doc is
# excluded when its TITLE contains any of these, matched case-insensitively as a
# substring. Everything else — PM calls, customer calls, ad-hoc meets — is in.
#
# This is an EXCLUDE list, not an include list, and the direction matters. The
# previous include-based filter (a sales attendee marker or a sales title hint)
# admitted zero of the 72 docs on the live folder, because the invite list didn't
# survive the Gemini export and nobody titles a real call "sales sync". An
# include-list that matches nothing looks exactly like "no meetings happened",
# which is the failure the notes pipeline exists to prevent. The worst an
# exclude-list does is put a standup in context: visible, and harmless.
#
# Setting this EMPTY is legal and means "load everything, exclude nothing".
NOTES_EXCLUDE_TITLE_PATTERNS = _str_list("NOTES_EXCLUDE_TITLE_PATTERNS", "AM sync,PM sync")

# The sales spreadsheet is no longer a stub: it is the GTM Playbook, read live
# through the Sheets API. Its settings live in the GTM SPREADSHEET section below
# (GOOGLE_SERVICE_ACCOUNT_JSON, GTM_SHEET_ORIGINAL_ID, SHEET_WRITES_ENABLED).
#
# -- The strategy doc (WIRED UP — strategy.py, read-only over Drive) ----------
# STRATEGY_DOC_ID points at the HUMAN-OWNED strategy document — Vaishnavi's
# edited version of the v2 draft, once it is in Drive. The knowledge layer reads
# it, and BOTH enforcements run against it:
#   - stale-doc: Drive's modifiedTime against STRATEGY_STALE_DAYS;
#   - outreach-vs-plan: the targets it names against where outreach went.
# It is also what `deadlines.cadence_from_strategy` reads, so a cadence stated
# in the doc now outranks the working-day defaults below.
#
# The bot has READ-ONLY access to it and no code path that could write to it —
# see drive.py's scope list.
#
# STRATEGY_DOC_FILE IS NOW THE CORE BRAIN, AND IT DEFAULTS TO A REAL PATH.
# `./sales_strategy.md` at the repo root is loaded into the system prompt of
# EVERY model call — answers, proactive composition, research, extraction — and
# RE-READ on each one, exactly like sales_policy.md. Editing that file changes
# what the bot thinks, with no restart and no deploy.
#
# WHERE THE TWO DOCUMENTS DISAGREE, THE STRATEGY DOC WINS. That precedence is
# stated inside the prompt block itself (persona.strategy_preamble), so it holds
# for every rule, including ones nobody has got round to de-duplicating by hand.
#
# STRATEGY_DOC_ID stays the Drive pointer and still outranks the file for the
# STALENESS and OUTREACH-vs-PLAN checks, which need Drive's modifiedTime.
STRATEGY_DOC_ID = (os.getenv("STRATEGY_DOC_ID", "") or "").strip()
STRATEGY_DOC_FILE = (
    os.getenv("STRATEGY_DOC_FILE", "./sales_strategy.md") or ""
).strip()

# HOW MUCH OF THE STRATEGY DOC GOES INTO EACH PROMPT. The doc rides in the
# system prompt of every model call, so an unbounded one is paid for on every
# question. Truncation is reported in the prompt itself rather than done
# silently — a model told it is reading a truncated plan can say so; one that
# isn't told will answer as though it read the whole thing. 0 means no limit.
STRATEGY_PROMPT_MAX_CHARS = _int("STRATEGY_PROMPT_MAX_CHARS", 24000)

# Past this many CALENDAR days without a revision, the plan is reported stale.
# Calendar, not working, days: a plan going stale over a long weekend is still
# going stale. 0 turns the currency rule off (the date is still reported).
STRATEGY_STALE_DAYS = _int("STRATEGY_STALE_DAYS", 30)

# How long a read of the doc is cached. A failed read is cached for the same
# interval, so a doc nobody has shared yet doesn't cost every question a round
# trip to a 403.
STRATEGY_CACHE_MINUTES = _int("STRATEGY_CACHE_MINUTES", 30)

# The outreach-vs-plan check: master switch, and the two bounds that keep a
# verbose plan from producing a verbose digest section.
STRATEGY_CHECK_ENABLED = _bool("STRATEGY_CHECK_ENABLED", default=True)
STRATEGY_MAX_TARGETS = _int("STRATEGY_MAX_TARGETS", 40)
STRATEGY_MAX_FINDINGS = _int("STRATEGY_MAX_FINDINGS", 6)

# Hard timeout for one Drive/Docs/Sheets REST call (drive.py).
DRIVE_TIMEOUT_SECONDS = _int("DRIVE_TIMEOUT_SECONDS", 30)

# -- MEETING CITATIONS (meetings.py) ------------------------------------------
# THE RULE: whenever meeting knowledge shapes a line — a hold, a decision, a
# commitment — the line names its source: "…on hold (Sales Bot Discussion,
# 2 Sep)". It applies to digest items, cadence chases, the tracker reminder,
# prep briefs and answers alike, and it is NOT a knob: there is no setting that
# turns citations off, because a meeting-derived claim with no citation is a bug.
#
# These two only bound how far back the knowledge layer looks and how many notes
# it opens per pass — a folder of 300 documents must not be re-parsed in full on
# every digest section.
MEETING_FACTS_DAYS = _int("MEETING_FACTS_DAYS", 45)
MEETING_FACTS_MAX_NOTES = _int("MEETING_FACTS_MAX_NOTES", 40)

# -- THE TO-DO SHEET (todos.py) -----------------------------------------------
# ONE Google Sheet, created by the bot on first run, SHARED with the team, and
# appended to weekly from the meeting notes. Humans own Status and Notes; the
# bot never edits or deletes a row.

TODO_SHEET_ENABLED = _bool("TODO_SHEET_ENABLED", default=True)

# The sheet's title, and its id. The id is normally EMPTY: the bot creates the
# sheet on first run and remembers the id in its own config store (the `meta`
# table in the SQLite file), so a restart re-opens the same sheet. Set this only
# to point the bot at a sheet somebody made by hand — it overrides the store.
TODO_SHEET_TITLE = (
    os.getenv("TODO_SHEET_TITLE", "") or ""
).strip() or "Membrane Sales To-Dos"
TODO_SHEET_ID = (os.getenv("TODO_SHEET_ID", "") or "").strip()

# WHO THE SHEET IS SHARED WITH, as Editor. CRITICAL: a spreadsheet created by a
# service account is owned by that service account and lives in a Drive no human
# can browse — until it is shared it is invisible, not merely hard to find.
TEAM_SHARE_EMAILS = _str_list(
    "TEAM_SHARE_EMAILS",
    "trishi@nfthing.com,vaishnavi@membrane.social,claudedrive@nfthing.com",
)
# Send Google's own "X shared a file with you" email on top of the channel post.
# Off by default: the link is posted in the sales channel, and an email to every
# address on every redeploy is noise that post already covers.
TODO_SHARE_NOTIFY = _bool("TODO_SHARE_NOTIFY", default=False)

# Which day's digest carries the weekly refresh. mon..sun or 0-6.
TODO_REFRESH_DAY = (os.getenv("TODO_REFRESH_DAY", "") or "").strip() or "fri"
# How far back the refresh reads meeting notes for action items.
TODO_NOTES_DAYS = _int("TODO_NOTES_DAYS", 7)
# Bounds: how many new rows one refresh may append, and how many open items the
# "show the to-dos" answer prints before it stops and gives the link.
TODO_MAX_NEW_PER_REFRESH = _int("TODO_MAX_NEW_PER_REFRESH", 25)
TODO_SHOW_MAX = _int("TODO_SHOW_MAX", 20)


def todo_refresh_weekday() -> int:
    """TODO_REFRESH_DAY as Python's weekday index (0=Monday)."""
    return _weekday("TODO_REFRESH_DAY", "fri")


# -- THE TRACKER REMINDER (a SECTION of the digest, never its own message) -----
# Vaishnavi's twice-weekly "update the tracker" prompt. It is a SECTION INSIDE
# that day's daily digest — there is no separate send path for it and there must
# never be one; see the one-message rule in digest.py.
TRACKER_REMINDER_ENABLED = _bool("TRACKER_REMINDER_ENABLED", default=True)
TRACKER_REMINDER_DAYS = (
    os.getenv("TRACKER_REMINDER_DAYS", "") or ""
).strip() or "mon,fri"


def tracker_reminder_weekdays() -> set:
    """The weekdays whose digest carries the tracker reminder section."""
    return _weekdays("TRACKER_REMINDER_DAYS", "mon,fri")

# -- Query behaviour ----------------------------------------------------------

QUERY_DISCORD_LOOKBACK_DAYS = _int("QUERY_DISCORD_LOOKBACK_DAYS", 14)
QUERY_MAX_MESSAGES_PER_CHANNEL = _int("QUERY_MAX_MESSAGES_PER_CHANNEL", 400)
QUERY_CHANNEL_SCAN_DEFAULT_DAYS = _int("QUERY_CHANNEL_SCAN_DEFAULT_DAYS", 2)
QUERY_CHANNEL_SCAN_MAX_DAYS = _int("QUERY_CHANNEL_SCAN_MAX_DAYS", 14)
QUERY_CHANNEL_DIGEST_MAX_MESSAGES = _int("QUERY_CHANNEL_DIGEST_MAX_MESSAGES", 60)
QUERY_HISTORY_MAX_DAYS = _int("QUERY_HISTORY_MAX_DAYS", 60)
QUERY_HISTORY_MAX_MATCHES = _int("QUERY_HISTORY_MAX_MATCHES", 8)
QUERY_HISTORY_MAX_CONTEXT = _int("QUERY_HISTORY_MAX_CONTEXT", 6)

# Tool-use loop bounds for query_engine.py.
QUERY_ENGINE_MAX_TOOL_ITERATIONS = _int("QUERY_ENGINE_MAX_TOOL_ITERATIONS", 8)
QUERY_ENGINE_MAX_TOKENS = _int("QUERY_ENGINE_MAX_TOKENS", 1536)

# Discord hard-caps a message at 2000 chars; leave room for the reply decoration.
QUERY_REPLY_CHUNK = _int("QUERY_REPLY_CHUNK", 1900)
QUERY_REPLY_MAX_MESSAGES = _int("QUERY_REPLY_MAX_MESSAGES", 4)

# Short-term per-channel conversational memory (memory.py).
QUERY_MEMORY_TURNS = _int("QUERY_MEMORY_TURNS", 5)
QUERY_MEMORY_TTL_MINUTES = _int("QUERY_MEMORY_TTL_MINUTES", 10)

# -- Deadline chasing (followups.py) ------------------------------------------
# "I'll send the deck tomorrow" → an open chase, nudged once it's overdue. The
# only thing a chase can ever produce is a Discord message in the channel the
# promise was made in.

COS_FOLLOWUP_ENABLED = _bool("COS_FOLLOWUP_ENABLED", default=True)
# How often the sweeper wakes up.
COS_FOLLOWUP_CHECK_INTERVAL_MINUTES = _int("COS_FOLLOWUP_CHECK_INTERVAL_MINUTES", 15)
# Fallback deadline when someone promises with no time attached ("will send it
# after the call") — 8 working-ish hours.
COS_FOLLOWUP_DEFAULT_DUE_MINUTES = _int("COS_FOLLOWUP_DEFAULT_DUE_MINUTES", 480)
# Don't act on a shaky read: a false chase is worse than a missed one.
COS_FOLLOWUP_MIN_CONFIDENCE = _float("COS_FOLLOWUP_MIN_CONFIDENCE", 0.6)

# COS_MAX_NUDGES_PER_SWEEP is RETIRED. It capped how many chases could be posted
# in one sweep, which only mattered when chases posted individually. They don't:
# every chase is a line in the ONE daily digest, so the cap that matters is
# SALES_DIGEST_MAX_PER_SECTION.

# HOW MANY TIMES ONE THING IS CHASED before it escalates. Still enforced, still
# in the `nudges` table — what changed is the vehicle: an "attempt" is now an
# appearance in the OVERDUE section of a digest rather than its own message.
# Once the cap is spent the item moves to the ESCALATIONS section addressed to
# ESCALATE_TO_ID and stops being chased.
#
# COS_NUDGE_WINDOW_HOURS is retained for the `nudges` log and for any code that
# still asks "did we chase this recently"; the digest's own once-a-day guarantee
# is what actually paces chasing now, so values below 24 have no effect.
COS_NUDGE_WINDOW_HOURS = _int("COS_NUDGE_WINDOW_HOURS", 24)
COS_NUDGE_MAX_ATTEMPTS = _int("COS_NUDGE_MAX_ATTEMPTS", 2)

# -- Startup / daily state ----------------------------------------------------

# Local hour (0–23, server time) at which the daily state/summary.json rewrite
# runs. The startup rewrite happens regardless.
STATE_DAILY_HOUR = _int("STATE_DAILY_HOUR", 8)

# -- GTM spreadsheet (Google Sheets API, service account) ---------------------
# The GTM Playbook is the sales team's system of record. The bot reads it LIVE
# through the Sheets API — there is no sync interval and no local copy of the
# sheet; rclone is only ever used for the meeting-notes docs.

# Path to the service-account key file. This grants READ AND WRITE access to the
# playbook, so it is a SECRET: .gitignore covers the usual key filenames, and it
# must never be committed. Empty → the spreadsheet source reports
# awaiting-access and the bot says so when asked.
#
# THE ACCOUNT NOW NEEDS EDITOR, NOT VIEWER. Writes go to the real sheet — see
# the block below — so a Viewer share reads fine and fails on the first write
# with a 403 that gtm_sheet translates into "shared read-only, needs Editor".
GOOGLE_SERVICE_ACCOUNT_JSON = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "") or "").strip()

# The "NFThing <> GTM Playbook" — the team's system of record, and now the
# bot's WRITE target as well as its read source.
GTM_SHEET_ORIGINAL_ID = (
    os.getenv("GTM_SHEET_ORIGINAL_ID", "") or ""
).strip() or "15fhmQAOVoABg2TpxSRL57LhC_I-62hZZAspl1Dqd_fs"

# -- WRITES: THE SANDBOX-COPY ERA IS OVER -------------------------------------
# GTM_SHEET_COPY_ID, SHEET_WRITE_TARGET, sheet_write_id() and
# BOT_DEADLINE_COLUMN are REMOVED. Setting any of them now does nothing.
#
# WHAT THEY WERE. The bot owned exactly one column — "Next Deadline (bot)",
# appended at the far right of the tab — and wrote single cells into it, on a
# SANDBOX COPY of the playbook by default. That design was right when nobody had
# agreed what the bot may touch: an owned column on a throwaway sheet cannot
# damage anything, and it let the write path be built and proven before anyone
# had to trust it.
#
# IT WAS ALSO USELESS, and increasingly so. A column on a copy nobody opens is a
# write into a drawer. The team works in the real sheet; a date the bot recorded
# somewhere else is a date nobody sees. And the two sheets were only ever
# row-aligned by luck — one sorted row in the copy and every write landed on the
# wrong company, which is why `write_deadline_cell` had to re-check the company
# name before every single write.
#
# WHAT REPLACES IT, per Vaishnavi's walkthrough: the bot writes into the REAL
# "Outreach PoCs" tab, into the columns the team actually keeps, and ONLY inside
# the WRITABLE WINDOW between the restricted bands (RESTRICTED_COLUMN_RANGES,
# default A:I and S:X — the identity block and the formula block are still
# untouchable, enforced in code since V1). There is no copy and no "which sheet"
# question left to get wrong.
#
# WHAT DID NOT CHANGE: SQLite is still the brain. Deadlines, snoozes, scheduled
# reminders, activations and the drip's own slot log all live there and are
# authoritative. The sheet is what the TEAM reads; SQLite is what the bot knows.

# Master switch for cell writes. false → the bot reads, answers, extracts and
# ECHOES what it would have written, and writes nothing. Useful for a week of
# watching it get the extraction right before letting it touch the sheet.
SHEET_WRITES_ENABLED = _bool("SHEET_WRITES_ENABLED", default=True)

# -- PERMISSION BEFORE EVERY WRITE --------------------------------------------
# THE GLOBAL RULE FROM THE WORKBOOK: the bot never writes a cell straight from a
# reply. It says what it proposes to change, in the sheet's own words, and waits
# for a yes.
#
# WHAT THIS COSTS AND WHY IT IS WORTH IT. The old loop was one message: somebody
# said "met Sahaj today" and the cell was already written by the time they read
# the echo. That is faster and it is fine right up until the extractor is wrong
# about which row, which column or which date — and then a person's data has
# been overwritten by a machine that nobody told to do it. An undo window
# catches that only if somebody reads the echo. A proposal catches it before it
# happens.
#
# WHO MAY SAY YES. Only SALES_APPROVER_IDS — Sid and Vaishnavi. Anybody may tell
# the bot something and it will propose the change; only an approver's yes
# applies it.
SALES_APPROVER_IDS: list[int] = _int_list("SALES_APPROVER_IDS")


def approver_ids() -> list:
    """The ids that may approve a write. Roster-filtered, current at call time.

    An approver who is not on TEAM_ROSTER_IDS cannot be @-mentioned, and an
    approval flow whose approver cannot be addressed is a flow that stalls
    silently — so they are filtered out here and named at boot.
    """
    return [uid for uid in SALES_APPROVER_IDS if uid in TEAM_ROSTER_IDS]


def is_approver(user_id) -> bool:
    """May this person approve a write? The single gate every approval asks."""
    try:
        return int(user_id) in set(approver_ids())
    except (TypeError, ValueError):
        return False


# WHOSE ANSWER WINS WHEN TWO APPROVERS DISAGREE. Strategy section 8: "Sid's word
# is final." A no from this id overrides a yes from anybody else, and a yes from
# this id overrides an earlier no — the LAST word from this person is the
# answer, and it is announced as such rather than applied quietly.
#
# 0/unset means no tie-break: the FIRST approver to answer decides, which is the
# honest degraded behaviour and is logged at startup.
SALES_FINAL_SAY_ID = _int("SALES_FINAL_SAY_ID", 0)

# HOW LONG A PROPOSAL STAYS OPEN before the one nudge. WORKING days, not
# calendar days: a proposal made on Friday afternoon is not stale on Saturday.
PROPOSAL_NUDGE_AFTER_DAYS = _int(
    "PROPOSAL_NUDGE_AFTER_DAYS",
    _int("PROPOSAL_NUDGE_AFTER_WORKING_DAYS", 1),
)

# The old name, kept as an alias so an existing .env keeps working. Assigned
# from the new one so the two can never hold different numbers.
PROPOSAL_NUDGE_AFTER_WORKING_DAYS = PROPOSAL_NUDGE_AFTER_DAYS

# ...AND HOW LONG AFTER THE NUDGE BEFORE IT IS DROPPED. ONE nudge, then the
# proposal is closed and logged and never mentioned again. Two nudges is
# nagging; a proposal that never expires is a queue of half-decisions nobody can
# see the end of.
PROPOSAL_DROP_AFTER_DAYS = _int(
    "PROPOSAL_DROP_AFTER_DAYS",
    _int("PROPOSAL_DROP_AFTER_WORKING_DAYS", 1),
)
PROPOSAL_DROP_AFTER_WORKING_DAYS = PROPOSAL_DROP_AFTER_DAYS

# -- ROW ADDITIONS ------------------------------------------------------------
# A NEW ROW MAY BE APPENDED — never edited into existence, never silently. R2
# (news-company screen), R3 (events) and R11 (new pipeline company) can each
# propose one, and an approver's yes appends it.
#
# THE IDENTITY COLUMNS OF A *NEW* OUTREACH PoCs ROW ARE WRITABLE, and only
# there. A:I on an EXISTING row stays as locked as it has always been: those
# cells hold work somebody did, and the bands exist to protect exactly that. A
# row the bot is creating has no such work in it — every cell is blank because
# the row did not exist a second ago — so filling A-I of a brand-new row
# destroys nothing and is the only way an appended row is any use at all.
#
# S-X OF A NEW ROW IS STILL REFUSED. Those are commercial judgements and a
# formula block; a bot that has just discovered a company has no business
# stating its closure probability.
SHEET_ROW_ADDITIONS_ENABLED = _bool("SHEET_ROW_ADDITIONS_ENABLED", default=True)

# Which tabs may receive an appended row. A tab not named here is refused even
# with an approval, because "which tabs may grow" is a decision about the shape
# of the workbook rather than about one row.
SHEET_APPENDABLE_TABS: list[str] = _str_list(
    "SHEET_APPENDABLE_TABS", "outreach_pocs,researcher_lines,events_summits",
)

# The columns the bot may fill on a NEW Outreach PoCs row. A:I is the identity
# block; J:R is the writable window, which it could write anyway. S:X is absent
# on purpose — see above.
NEW_ROW_WRITABLE_RANGES = (
    os.getenv("NEW_ROW_WRITABLE_RANGES", "") or ""
).strip() or "A:R"

# PARSED ON FIRST USE, not at import: `parse_column_ranges` is defined further
# down this file with the other band helpers, and moving either one to sit
# beside the other would separate a setting from the paragraph that explains it.
_NEW_ROW_INDEXES: Optional[frozenset] = None


def new_row_writable_indexes() -> frozenset:
    """The column indexes the bot may fill on a row it is CREATING."""
    global _NEW_ROW_INDEXES
    if _NEW_ROW_INDEXES is None:
        _NEW_ROW_INDEXES = frozenset(
            i for lo, hi in parse_column_ranges(NEW_ROW_WRITABLE_RANGES)
            for i in range(lo, hi + 1)
        )
    return _NEW_ROW_INDEXES


def may_write_new_row_column(index0) -> bool:
    """May the bot fill this column on a row it is CREATING?

    FAILS CLOSED, like `is_restricted_column`: an index that cannot be read as a
    number is refused. This is a widening of the write surface and the one place
    A:I is reachable at all, so it gets the same paranoia as the lock it relaxes.
    """
    try:
        idx = int(index0)
    except (TypeError, ValueError):
        return False
    return idx in new_row_writable_indexes()


# -- FOCUS COMMANDS -----------------------------------------------------------
# "Prioritise only AI Voice Agents for the next two weeks." Sid or Vaishnavi can
# redirect prospecting at any time; R5 then offers matching contacts FIRST.
#
# A FOCUS NARROWS, IT DOES NOT SILENCE. When nothing on the sheet matches, R5
# SAYS SO and falls back to sheet order rather than going quiet — a focus that
# accidentally matched nothing would otherwise read exactly like a quiet week,
# and nobody would know the filter was the reason.
#
# ONLY SALES_APPROVER_IDS MAY SET ONE, for the same reason only they may approve
# a write: a focus changes who the whole team is contacting for a fortnight.
FOCUS_DEFAULT_DAYS = _int("FOCUS_DEFAULT_DAYS", 14)

# The longest a focus may run. A focus is a deliberate, temporary narrowing; one
# set for six months is a strategy change wearing a command's clothes, and it
# should be an edit to sales_strategy.md instead.
FOCUS_MAX_DAYS = _int("FOCUS_MAX_DAYS", 90)

# Which Outreach PoCs roles a focus filter is matched against. Industry,
# company, designation and location — the four things somebody means when they
# say "only AI voice agents" or "only the London universities".
FOCUS_MATCH_ROLES: list[str] = _str_list(
    "FOCUS_MATCH_ROLES", "industry,company,designation,based",
)


def sheet_write_id() -> str:
    """The spreadsheet id writes go to, or "" when writing is off.

    ONE sheet now. Kept as a function rather than inlined because every write
    path resolves the target through here, and a single chokepoint is what makes
    "the bot cannot write anywhere else" checkable rather than asserted.
    """
    return GTM_SHEET_ORIGINAL_ID if SHEET_WRITES_ENABLED else ""


# HOW LONG AN "UNDO" STAYS AVAILABLE, in hours. Any team member may undo any
# write in this window and the exact prior cell values are restored from SQLite.
#
# 24 HOURS IS THE POINT, not a detail. The bot writes into the sheet the team
# actually works in, on the strength of a sentence somebody typed in a channel.
# That is only acceptable if it is trivially reversible by whoever notices, and
# noticing usually happens the next morning.
SHEET_WRITE_UNDO_HOURS = _int("SHEET_WRITE_UNDO_HOURS", 24)

# MOST CELLS ONE REPLY MAY CHANGE. A safety ceiling, not a target: a single
# sentence should touch one or two cells, and an extraction that suddenly wants
# to rewrite nine of them has misread something. Past this the write is refused
# whole — never half-applied — and the bot says so and asks.
SHEET_WRITE_MAX_CELLS = _int("SHEET_WRITE_MAX_CELLS", 4)

# WORDS THAT MUST APPEAR VERBATIM before the bot will set a prospect status to a
# terminal value. "Dead" and "Unresponsive" end a row for good — the next-action
# engine stops it permanently — so the bot never infers one. Somebody has to
# have actually said it.
TERMINAL_STATUS_WORDS: list[str] = _str_list(
    "TERMINAL_STATUS_WORDS",
    "dead,unresponsive,not interested,no longer interested,drop them,drop it,"
    "write it off,write them off,close it,closed lost,lost",
)


# -- SUPPRESS-OR-CONVERT: check before you nudge ------------------------------
# BEFORE ANY PROACTIVE MESSAGE GOES OUT, the bot looks for evidence that the
# thing it is about to ask for has already happened — in the synced meeting
# notes and in what the team said in the sales channels.
#
# EVIDENCE DOES NOT SILENCE THE NUDGE, IT CHANGES IT. A suppressed message is
# indistinguishable from a bot that has stopped working, and the sheet is still
# wrong either way. So the nudge becomes a RECORD-OFFER:
#
#   task    "Vaishnavi — has the DM to Sahaj gone out?"
#   offer   "Saw the meeting's set for Friday — want me to mark it on the row?"
#
# That is strictly better than both alternatives. Chasing something already done
# is the fastest way to get a bot muted; going silent leaves the row wrong AND
# tells nobody. The offer closes the loop with one word back.
#
# THE EVIDENCE IS QUOTED, ALWAYS. Every conversion names what it found and where
# — the meeting note and its date, or the channel message and its author. A bot
# that says "I think this is done" without saying why is asking to be trusted on
# a guess.

# Master switch. Off = every nudge goes out as a task, whatever the notes say.
SUPPRESS_OR_CONVERT_ENABLED = _bool("SUPPRESS_OR_CONVERT_ENABLED", default=True)

# How far back to look for evidence, in days. Short on purpose: a meeting note
# from three weeks ago saying "we'll book something" is not evidence that a
# meeting exists now, and treating it as such would convert a live nudge into an
# offer to record something that never happened.
NOTES_LOOKBACK_DAYS = _int("NOTES_LOOKBACK_DAYS", 7)

# -- RESEARCH BRIEFS (mention-triggered only) ---------------------------------
# "brief me on Sahaj (Acme)" -> who they are, how much their role weighs, which
# of their work maps to our lanes, an angle, and a DRAFT message to personalise.
#
# IT IS COPY MATERIAL. Never auto-sent, never written to the sheet, never
# actioned on its own. The bot has no outbound channel to a prospect and this
# does not give it one — it hands a human a draft to edit.
#
# IT ONLY READS URLS THAT ARE ALREADY ON THE ROW. The bot does not search the
# web, does not follow links it found in a page, and does not guess a URL from a
# name. If the row has no research link, the brief says so.

# Which domains a research link may point at. Anything else is REFUSED with a
# one-line note naming the domain — not silently skipped, because "I ignored
# three of your links" is something the person needs to know.
#
# Matched on the registered domain and its subdomains, so "arxiv.org" also
# allows "www.arxiv.org". Widen it deliberately; every entry is a place the bot
# will fetch from unattended.
RESEARCH_ALLOWED_DOMAINS: list[str] = _str_list(
    "RESEARCH_ALLOWED_DOMAINS", "arxiv.org",
)

# -- WEB SEARCH ----------------------------------------------------------------
# Anthropic's SERVER-SIDE web search tool, declared on the Messages API call.
# Anthropic runs the search; nothing in this bot opens a socket to a search
# engine, and `research.py`'s fetch path is untouched — that still fetches only
# URLs already on a row, only from RESEARCH_ALLOWED_DOMAINS.
#
# WEB CONTENT IS DATA, NEVER INSTRUCTIONS. websearch.SAFETY_PREAMBLE says so to
# the model on every call, and the code gives a page nowhere to go: nothing acts
# on a search result without a human's yes (approvals.py).
WEB_SEARCH_ENABLED = _bool("WEB_SEARCH_ENABLED", default=True)

# WHICH VERSION OF THE TOOL. Three exist and they are NOT interchangeable:
#
#   web_search_20250305   basic search
#   web_search_20260209   adds dynamic filtering (Claude 4.6 and later)
#   web_search_20260318   adds response-inclusion control   <- the default
#
# A version the model does not support is a 400, so this is a setting rather
# than a constant: an operator on an older model drops to the basic variant
# without a code change.
WEB_SEARCH_TOOL_TYPE = (
    os.getenv("WEB_SEARCH_TOOL_TYPE", "") or ""
).strip() or "web_search_20260318"

# HOW MANY SEARCHES ONE CALL MAY MAKE. Past it the API returns a
# `max_uses_exceeded` error INSIDE a 200 response and the model stops searching
# — which the bot reports rather than hiding. Simple lookups use 1-3; a
# multi-company screen legitimately wants more.
WEB_SEARCH_MAX_USES = _int("WEB_SEARCH_MAX_USES", 5)

# HOW MANY SEARCHES A DAY, ACROSS EVERYTHING. Web search is billed per search on
# top of tokens, and seven rules can each want several — a runaway day is a real
# bill, not a rounding error.
#
# WHEN IT IS SPENT THE RULES DEGRADE, THEY DO NOT FAIL. Each one still produces
# its items and each says "web research unavailable today — the daily search
# budget is spent". Going quiet instead would look exactly like a quiet week.
#
# COUNTED FROM `usage.server_tool_use.web_search_requests`, which is what the
# API actually billed. An errored search is not billed and does not count.
WEB_SEARCH_DAILY_BUDGET = _int("WEB_SEARCH_DAILY_BUDGET", 60)

# Optional domain filters. allowed_domains and blocked_domains are MUTUALLY
# EXCLUSIVE — a request carrying both is a 400 — so when both are set the
# allow-list wins and the block-list is dropped with a warning. Bare domains,
# no scheme: "example.com", or "example.com/blog".
#
# EMPTY IS THE RIGHT DEFAULT. An allow-list here would make R1's news sweep read
# only the sites somebody listed last quarter, which is not a news sweep.
WEB_SEARCH_ALLOWED_DOMAINS: list[str] = _str_list("WEB_SEARCH_ALLOWED_DOMAINS", "")
WEB_SEARCH_BLOCKED_DOMAINS: list[str] = _str_list("WEB_SEARCH_BLOCKED_DOMAINS", "")

# Localisation for search results. Any subset is legal; an empty set is omitted.
# The country is an ISO 3166-1 alpha-2 code and the API rejects an unsupported
# one with a 400.
WEB_SEARCH_CITY = (os.getenv("WEB_SEARCH_CITY", "") or "").strip() or "Bengaluru"
WEB_SEARCH_REGION = (os.getenv("WEB_SEARCH_REGION", "") or "").strip() or "Karnataka"
WEB_SEARCH_COUNTRY = (os.getenv("WEB_SEARCH_COUNTRY", "") or "").strip() or "IN"
WEB_SEARCH_TIMEZONE = (
    os.getenv("WEB_SEARCH_TIMEZONE", "") or ""
).strip() or "Asia/Kolkata"

# "full" or "excluded". LEAVE IT AT "full".
#
# "excluded" looks like free money — it drops the raw search-result blocks once
# a completed code execution has consumed them, saving output tokens. MEASURED
# AGAINST THE LIVE API, IT BREAKS THE LINKS. With dynamic filtering on (the
# default), the search runs inside code execution, the `citations` array comes
# back EMPTY, and "excluded" then removes the only other place the URLs live —
# so a response that visibly contained three links parsed to ZERO sources.
#
# Every fact this bot states from the web has to carry its link. That is worth
# more than the tokens. Set "excluded" only if you have verified that your model
# and tool version still return citations.
WEB_SEARCH_RESPONSE_INCLUSION = (
    os.getenv("WEB_SEARCH_RESPONSE_INCLUSION", "") or ""
).strip() or "full"

# THE ESCAPE HATCH FOR A MODEL WITHOUT PROGRAMMATIC TOOL CALLING. On
# web_search_20260209 and later, `allowed_callers` defaults to
# ["code_execution_20260120"] and the API provisions that code execution itself
# — do NOT also declare the code execution tool. A model that cannot do it
# returns a 400 telling you to set ["direct"]; that is what this is for. Empty
# means "send nothing and take the tool's own default".
WEB_SEARCH_ALLOWED_CALLERS: list[str] = _str_list("WEB_SEARCH_ALLOWED_CALLERS", "")

# How long a single fetch may take, and how much of a page is read. Bounded so a
# slow or enormous page degrades the brief instead of hanging the bot.
RESEARCH_FETCH_TIMEOUT_SECONDS = _int("RESEARCH_FETCH_TIMEOUT_SECONDS", 12)
RESEARCH_FETCH_MAX_BYTES = _int("RESEARCH_FETCH_MAX_BYTES", 400000)
# Most links one brief will fetch. A row with twelve papers on it is a reading
# list, not a brief.
RESEARCH_MAX_LINKS = _int("RESEARCH_MAX_LINKS", 4)

# LINKEDIN. Set BOTH to enable the LinkedIn half of a brief. Unset (the current
# state) means the bot SAYS SO in the brief — "LinkedIn access is pending" — in
# those words, rather than quietly producing a brief with a hole in it where the
# person's career history should be.
#
# There is no scraping fallback and there must not be one: LinkedIn's terms
# forbid it, and a bot that scrapes on a team's behalf puts the team at risk.
LINKEDIN_API_CLIENT_ID = (os.getenv("LINKEDIN_API_CLIENT_ID", "") or "").strip()
LINKEDIN_API_CLIENT_SECRET = (os.getenv("LINKEDIN_API_CLIENT_SECRET", "") or "").strip()
LINKEDIN_API_ACCESS_TOKEN = (os.getenv("LINKEDIN_API_ACCESS_TOKEN", "") or "").strip()


def linkedin_ready() -> bool:
    """Are LinkedIn API credentials configured?

    An access token alone is enough to call the API; the client id/secret pair is
    what a refresh flow would need. Either shape counts as configured, and
    neither is present today.
    """
    return bool(
        LINKEDIN_API_ACCESS_TOKEN
        or (LINKEDIN_API_CLIENT_ID and LINKEDIN_API_CLIENT_SECRET)
    )


# -- EVENTS & SUMMITS ---------------------------------------------------------
# The playbook has an "Events & Summits" tab. Each event earns ONE reminder, at
# T-EVENT_LEAD_DAYS, and never another.
#
# ONCE, FOREVER. The dedup is a permanent SQLite row, not a per-day marker: a
# conference the team has already decided about does not need reminding twice,
# and "we told you in March" is not a reason to tell you again in April. The
# reminder rides the drip like everything else — it counts against
# DAILY_MESSAGE_CAP and the kill switch applies to it.

EVENTS_ENABLED = _bool("EVENTS_ENABLED", default=True)

# Days before the event that the single reminder fires. 20 is the plan's number:
# far enough out that a booth, a talk slot or a flight is still bookable, close
# enough that it is not forgotten again immediately.
EVENT_LEAD_DAYS = _int("EVENT_LEAD_DAYS", 20)

# An optional NAME hint for the events tab, comma-separated. Like the master-tab
# hint it is only a hint; the tab is also found by its header signature.
GTM_EVENTS_TAB_TITLES: list[str] = _str_list(
    "GTM_EVENTS_TAB_TITLES",
    "AI Events & Summits,AI Events and Summits,Events & Summits,"
    "Events and Summits,Events,Summits",
)

# -- THE WEEKLY FUNNEL LINE ---------------------------------------------------
# One short Friday message: leading counts, then lagging counts. Nothing else.
#
# OFF BY DEFAULT, AND OPT-IN PER THE PLAN. A weekly number nobody asked for is
# the definition of a message that gets skimmed, and it spends one of the day's
# three slots. Turn it on when somebody actually wants it.
WEEKLY_FUNNEL_ENABLED = _bool("WEEKLY_FUNNEL_ENABLED", default=False)
# 0=Monday ... 4=Friday. Which weekday carries it.
WEEKLY_FUNNEL_WEEKDAY = _int("WEEKLY_FUNNEL_WEEKDAY", 4)

# Reads are live, cached this long to stay inside the API quota. A stale-but-
# recent answer is fine; a quota ban is not.
SHEET_CACHE_SECONDS = _int("SHEET_CACHE_SECONDS", 60)

# -- The researcher/buyer mapping sheet (THE THIRD SOURCE, READ-ONLY) ---------
# "membrane.social - Researcher Buyer Mapping": which PEOPLE to pitch inside the
# orgs the playbook tracks, which lane they sit in, what to open with, and who
# must not be pitched at all.
#
# THE BOT NEVER WRITES HERE. That is enforced in three places, not asserted in a
# comment: mapping_sheet.py has no write method and builds its client with the
# READ-ONLY Sheets scope, and `is_read_only_sheet_id()` below is checked by every
# write path in gtm_sheet.py.
GTM_MAPPING_SHEET_ID = (
    os.getenv("GTM_MAPPING_SHEET_ID", "") or ""
).strip() or "1YwNj2R3OoGhn3bvFIZmGBS9s9Fl316Mp7JsACxrHM2Q"


def service_account_email() -> str:
    """The service account's address, for a share instruction that names it.

    "Share it with the service account" is useless advice; "share it with
    sales-bot@sales-bot-write.iam.gserviceaccount.com" is a click. Read from the
    key file rather than configured separately, so the two cannot disagree.
    """
    import json as _json
    import os as _os

    path = (GOOGLE_SERVICE_ACCOUNT_JSON or "").strip()
    if not path or not _os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return str(_json.load(f).get("client_email") or "").strip()
    except Exception:
        return ""


def read_only_sheet_ids() -> set:
    """Spreadsheet ids the bot may READ but must never write to.

    A function rather than a constant so it reflects the CURRENT value of the
    setting even if something reassigns it at runtime — a stale snapshot taken at
    import time would be a hole in exactly the guarantee this exists to make.
    """
    return {GTM_MAPPING_SHEET_ID} - {""}


def is_read_only_sheet_id(sheet_id: str) -> bool:
    """True when `sheet_id` names a sheet that is read-only by policy.

    Every write path checks this before touching a cell, so the guarantee holds
    even if someone points GTM_SHEET_ORIGINAL_ID at the mapping sheet by mistake.
    """
    return bool(sheet_id) and str(sheet_id).strip() in read_only_sheet_ids()


# Optional override for header→role mapping in the MAPPING sheet, same shape and
# same purpose as GTM_COLUMN_MAP but keyed by the mapping tab kinds
# (mapping_legend / researcher_mapping / org_coverage / edge_map).
GTM_MAPPING_COLUMN_MAP: dict = _json_object("GTM_MAPPING_COLUMN_MAP", default={})

# Optional override for header→role mapping when a tab's wording is ambiguous.
# Headers are discovered dynamically at parse time (tabs will evolve), so this is
# only needed when auto-detection picks wrong. Shape:
#   {"outreach_pocs": {"company": "Client Name", "poc": "Point of Contact"}}
# Values are the literal header text in the sheet; keys are the roles in
# gtm_sheet.ROLES.
GTM_COLUMN_MAP: dict = _json_object("GTM_COLUMN_MAP", default={})

# -- THE CANONICAL TAB: "Outreach PoCs", FOUND BY NAME ------------------------
# PHASE 2 MOVED THE BOT'S SHEET WORLD. The canonical tab is the "Outreach PoCs"
# tab of the GTM Playbook (GTM_SHEET_ORIGINAL_ID, unchanged). The old outreach
# TRACKER tab (live: the hidden "Outreach Updates") and the phase-1 cadence
# rules that ran on it are RETIRED: no rule evaluates against them and no
# proactive output path is wired to them any more.
#
# THIS ONE TAB IS FOUND BY NAME, NOT BY HEADER SIGNATURE, and that is a
# deliberate reversal of how every other tab is found. A signature is the right
# authority when the question is "which of these twenty tabs is the tracker";
# here the tab was named to us directly, and the retired tracker's columns are
# near-identical to the new tab's, so a signature match would cheerfully
# re-adopt the retired tab as the canonical one. Naming it is what makes the
# retirement real.
#
# THE COLUMNS ARE STILL DISCOVERED DYNAMICALLY at parse time — tabs evolve, and
# nothing about this tab's header text is compiled in. The FULL discovered
# schema of every tab is logged at startup (GTM_LOG_FULL_SCHEMA).
GTM_POCS_TAB_TITLES: list[str] = _str_list(
    "GTM_POCS_TAB_TITLES", "Outreach PoCs,Outreach POCs,Outreach PoC,Outreach Pocs"
)

# -- THE READ-ONLY CONTEXT TABS, ALL FOUND BY NAME ----------------------------
# Five more tabs the bot READS and never writes. Each is claimed by NAME for the
# same reason the canonical tab is: a header signature describes a shape, and a
# playbook has several tabs of each shape. "Deliverables Checklist" and
# "Sales Packages" both look like "a list of things with a status"; the events
# tab and the goals tab both look like "a name and a date". Naming them is what
# makes discovery deterministic.
#
# EVERY ONE OF THEM IS READ-ONLY IN THE SAME WAY THE MAPPING SHEET IS: there is
# no write path that addresses them. Writes are only ever planned against the
# canonical tab's writable window (see RESTRICTED_COLUMN_RANGES below), and the
# write planner resolves its target tab through `pocs_tab()` alone.
#
# Each is a comma-separated list of candidate titles, matched case-, space- and
# punctuation-insensitively (gtm_sheet.normalise_header). Their full discovered
# schemas are logged at startup under [gtm.schema] like every other tab.

# "Deliverables Checklist": Sr No, Action Item, Functional Dependency, Priority,
# Tentative Deadline, Timelines, Link/Destination, Status, Reminder Freq.
# Its deadlines carry NO YEAR ("25-Sep") — see gtm_sheet.parse_bare_deadline.
GTM_DELIVERABLES_TAB_TITLES: list[str] = _str_list(
    "GTM_DELIVERABLES_TAB_TITLES",
    "Deliverables Checklist,Deliverable Checklist,Deliverables,Checklist",
)

# "Master Pipeline": Sr no., Company, Industry, Geography, Approx. Funding,
# Outreach Line - Researchers, Dates. One researcher-led outreach line per
# company — the tab the bot quotes when asked what the opening line for an org
# is meant to be.
GTM_PIPELINE_TAB_TITLES: list[str] = _str_list(
    "GTM_PIPELINE_TAB_TITLES",
    "Master Pipeline,Master pipeline,Pipeline Master,Master Pipeline Sheet",
)

# "Sales Packages": Package, Name, Purpose, Use Case, Size, Audio Files, Image
# Files, JSONL Output, Pulse_Product Doc, % Completion, Ready?, Status. What the
# team can actually SELL today, and how finished each package is — so the bot
# can stop itself offering a package that is not ready.
GTM_PACKAGES_TAB_TITLES: list[str] = _str_list(
    "GTM_PACKAGES_TAB_TITLES",
    "Sales Packages,Sales Package,Packages,Package List",
)

# "Q4-OND2026-Goal Setting": the strategy-motions table and the goals table.
# READ-ONLY CONTEXT FOR ANSWERS — the bot quotes it when asked what the quarter
# is committed to. Nothing proactive runs off it and no rule evaluates against
# it; it is background the model is allowed to cite, nothing more.
GTM_GOALS_TAB_TITLES: list[str] = _str_list(
    "GTM_GOALS_TAB_TITLES",
    "Q4-OND2026-Goal Setting,Q4 OND2026 Goal Setting,Goal Setting,Goals,"
    "Q4 Goal Setting",
)

# The OTHER tabs are still identified by the COLUMNS they carry, not by their
# names — names drift, signatures don't. gtm_sheet.py holds the signatures; the
# tab NAME that matched each role is logged at startup under [gtm.roles].
#
#   MASTER    "Response Status" + "Intro Sent" + "Meeting Done"
#             (live: "Master Data"). STATUS ONLY: aggregate answers and the
#             weekly funnel definition.
#   PIPELINE  "Lead Stage" + "Estimated Value (INR)"  (live: "Lead Master Sheet")
#   FUNNEL    "Vertical / Stage"   (live: "Sales Funnel - March-June 2026")
#   RESEARCHER LINES  "Outreach Line - Researchers" + "Dates"
#             (live: "Master Pipeline")
#
# RE-POINTING THE BOT AT A NEW SHEET IS AN ENV CHANGE PLUS A RESTART. Nothing
# about the sheet's identity or its column names is compiled in:
#   GTM_SHEET_ORIGINAL_ID     which spreadsheet
#   GTM_POCS_TAB_TITLES       which tab is canonical
#   GTM_COLUMN_MAP            which header means which role
#   GTM_MASTER_TAB_TITLES     an optional NAME hint for the master tab
#   GTM_DELIVERABLES_TAB_TITLES / GTM_PIPELINE_TAB_TITLES /
#   GTM_PACKAGES_TAB_TITLES / GTM_EVENTS_TAB_TITLES /
#   GTM_GOALS_TAB_TITLES      the five read-only context tabs, each by name
#   RESTRICTED_COLUMN_RANGES  which columns the bot may never write
#   SALES_DEFAULT_OWNER_ID    who items are addressed to
# Change those, restart, and the bot runs against the new sheet.

# An optional NAME hint for the master tab, comma-separated, matched case- and
# punctuation-insensitively. IT IS A HINT, NOT AN AUTHORITY: a tab named here is
# still only read as the master if it carries the master signature.
GTM_MASTER_TAB_TITLES: list[str] = _str_list(
    "GTM_MASTER_TAB_TITLES", "Master data,Master Data,Master-data,Masterdata"
)

# Read tabs the spreadsheet marks HIDDEN. gspread returns hidden worksheets like
# any other — this flag exists so discovery can be narrowed if a hidden tab ever
# causes trouble, not because hidden means private. Hidden tabs are logged as
# hidden, and the canonical tab is taken by NAME whether it is hidden or not.
GTM_READ_HIDDEN_TABS = _bool("GTM_READ_HIDDEN_TABS", default=True)

# Log the FULL discovered schema — every tab, every header, hidden or not — once
# per spreadsheet at startup. This is how a column rename is diagnosed in one
# log read instead of a debugging session, so it defaults ON.
GTM_LOG_FULL_SCHEMA = _bool("GTM_LOG_FULL_SCHEMA", default=True)

# -- RESTRICTED COLUMN BANDS: WHERE THE BOT MAY NEVER WRITE -------------------
# Comma-separated A1 column ranges ("A:I,S:X") or bare single columns ("A,C").
# Every column inside a band is DENIED to every write path in code, resolved to
# column INDEXES once at import so a band cannot mean one thing in one code path
# and something else in another.
#
# THIS IS A WRITE LOCK, NOT A READ LOCK. Reading stays completely unrestricted —
# the bot still parses, quotes and answers questions about every column in the
# tab. The bands exist because the sheet's left-hand identity block and its
# right-hand formula block are maintained by people and by formulas, and a bot
# writing into either would destroy work it cannot see.
#
# The columns BETWEEN the bands are the WRITABLE WINDOW, and the NAMED columns
# inside it are logged at startup — so a shifted column is visible before any
# write, rather than after one has landed in the wrong place.
RESTRICTED_COLUMN_RANGES = (
    os.getenv("RESTRICTED_COLUMN_RANGES", "") or ""
).strip() or "A:I,S:X"


def _column_index(label: str):
    """"A" -> 0, "S" -> 18, "AA" -> 26. None when it isn't a column label."""
    text = str(label or "").strip().upper()
    if not text or not text.isalpha():
        return None
    n = 0
    for ch in text:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def column_label(index0) -> str:
    """0-based column index -> A1 letter (0 -> A, 26 -> AA)."""
    n = int(index0) + 1
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def parse_column_ranges(spec: str) -> list:
    """"A:I,S:X" -> [(0, 8), (18, 23)] — inclusive, 0-based pairs.

    An unparseable fragment is dropped WITH A WARNING NAMING IT rather than
    raising: a typo here must not take the bot down. It must not silently WIDEN
    what may be written either, which is why the fragment is named — the bands
    that did parse still lock exactly what they lock.
    """
    out: list = []
    for part in str(spec or "").split(","):
        frag = part.strip()
        if not frag:
            continue
        lo_label, _sep, hi_label = frag.partition(":")
        lo = _column_index(lo_label)
        hi = _column_index(hi_label) if hi_label.strip() else lo
        if lo is None or hi is None:
            log.warning(
                "RESTRICTED_COLUMN_RANGES: %r is not a column range like 'A:I' or 'S'; "
                "that fragment is ignored. The other bands still apply.", frag,
            )
            continue
        out.append((min(lo, hi), max(lo, hi)))
    return sorted(out)


RESTRICTED_COLUMN_BANDS: list = parse_column_ranges(RESTRICTED_COLUMN_RANGES)
RESTRICTED_COLUMN_INDEXES: frozenset = frozenset(
    i for lo, hi in RESTRICTED_COLUMN_BANDS for i in range(lo, hi + 1)
)


def is_restricted_column(index0) -> bool:
    """True when this 0-based column index falls inside a restricted band.

    FAILS CLOSED: an index that cannot be read as a number is treated as
    restricted, because "I could not work out which column this is" has never
    been a reason to write to it.
    """
    try:
        return int(index0) in RESTRICTED_COLUMN_INDEXES
    except (TypeError, ValueError):
        return True


def restricted_band_label(index0) -> str:
    """Which band a column falls in, as "A:I". "" when it falls in none."""
    try:
        idx = int(index0)
    except (TypeError, ValueError):
        return "(unreadable column index)"
    for lo, hi in RESTRICTED_COLUMN_BANDS:
        if lo <= idx <= hi:
            return f"{column_label(lo)}:{column_label(hi)}" if lo != hi else column_label(lo)
    return ""


def writable_windows() -> list:
    """The contiguous unrestricted runs BETWEEN the restricted bands.

    With bands A:I and S:X that is [(9, 17)] — columns J..R.

    Columns to the RIGHT of the last band are unrestricted too, but they are not
    "between" the bands and are deliberately not reported as the window: the
    window is the gap the sheet's owners left for the bot, and naming an
    open-ended tail alongside it would make a misalignment harder to spot rather
    than easier.
    """
    if len(RESTRICTED_COLUMN_BANDS) < 2:
        return []
    out: list = []
    for (_lo_a, hi_a), (lo_b, _hi_b) in zip(
        RESTRICTED_COLUMN_BANDS, RESTRICTED_COLUMN_BANDS[1:]
    ):
        if lo_b > hi_a + 1:
            out.append((hi_a + 1, lo_b - 1))
    return out


def writable_window_label() -> str:
    """The writable window as "J:R". "" when the bands leave no gap between them."""
    return ", ".join(
        f"{column_label(lo)}:{column_label(hi)}" if lo != hi else column_label(lo)
        for lo, hi in writable_windows()
    )


# A mapped master column that is empty on nearly every row is the signature of a
# status conveyed by CELL COLOUR rather than by text. The bot reads VALUES ONLY
# — it cannot see fills — so it warns at startup naming the column instead of
# silently treating every row as blank.
# The warning fires when a tab has at least MIN_ROWS rows and the column is
# filled on no more than RATIO of them.
CADENCE_EMPTY_COLUMN_MIN_ROWS = _int("CADENCE_EMPTY_COLUMN_MIN_ROWS", 10)
CADENCE_EMPTY_COLUMN_RATIO = _float("CADENCE_EMPTY_COLUMN_RATIO", 0.05)

# -- PHASE 2: THE CADENCE RULE SET IS RETIRED ---------------------------------
# THE PHASE-1 RULES (a-j) ARE GONE, and this block is what is left of them.
#
# What was removed, in full: (a) stale_followup, (b) intro_pending,
# (c) start_interacting and its cold ceiling, (d) alt_channel, (e) unresponsive,
# (f) lock_meeting, (g) try_another_poc, (h) meeting_soon, (i) post_meeting,
# (j) nextstep_stall, plus the UPDATE-TRACKER fill-in asks and the
# master/tracker cross-check that were derived from them. Their thresholds
# (FOLLOWUP_STALE_DAYS, CONNECT_REMINDER_DAYS, CONNECT_REMINDER_MAX_DAYS,
# ALT_CHANNEL_AT, UNRESPONSIVE_AT, NEXTSTEP_STALL_DAYS, UPDATE_TRACKER_MAX,
# CADENCE_CROSSCHECK_ENABLED, CADENCE_CROSSCHECK_MAX) are removed with them —
# a threshold left behind for a rule that no longer exists is a lie in the
# config, and somebody would eventually tune it and wonder why nothing changed.
#
# The five cadence SECTIONS still exist in the digest and are simply empty until
# a phase-2 rule set is defined against the "Outreach PoCs" tab. The sheet-health
# flags below survive, because they are a property of the SHEET rather than a
# cadence rule, and they are the one thing that still has something to say.

# Master switch for whatever the cadence block computes. Off = the cadence
# sections are simply absent from the digest; nothing else changes.
CADENCE_ENABLED = _bool("CADENCE_ENABLED", default=True)

# Cell values that mark a row REJECTED. A rejected row is excluded from every
# proactive feature — never chased, revisited offline by humans. Matched as
# whole phrases against the response, reason, status, next-steps and notes
# cells, case-insensitively.
CADENCE_REJECTED_MARKERS: list[str] = _str_list(
    "CADENCE_REJECTED_MARKERS",
    "rejected,not interested,disqualified,do not contact,dnc,closed lost,"
    "lost,dropped,drop,blacklist,blacklisted",
)

# WHO A PROACTIVE ITEM IS ADDRESSED TO.
#
# A line resolves its owner in this order:
#   1. the row's own owner cell, if the canonical tab carries one (or
#      GTM_COLUMN_MAP names one: {"outreach_pocs": {"owner": "Owned By"}}),
#      resolved against the roster by display name;
#   2. SALES_DEFAULT_OWNER_ID — Vaishnavi's Discord id.
# The id must ALSO be in TEAM_ROSTER_IDS to actually be @-mentioned; the roster
# is the only thing that authorises a mention, and an id that isn't on it is
# named in plain text instead. Unset means lines are addressed to the
# DEADLINE_NOTIFY_IDS group line, which is worse but never wrong.
SALES_DEFAULT_OWNER_ID = _int("SALES_DEFAULT_OWNER_ID", 0)

# THE DATA-QUALITY FLAGS: broken formulas (#REF! and friends), master rows whose
# cells come from the wrong vocabulary, and response values nobody standardised.
# One line each, and DEDUPED UNTIL FIXED — a flag whose signature hasn't changed
# since the last time it was reported is not repeated, because a daily reminder
# of a known-broken formula is how a digest gets muted.
CADENCE_DATA_QUALITY_ENABLED = _bool("CADENCE_DATA_QUALITY_ENABLED", default=True)

# THE URGENT ITEMS ARE NEVER TRUNCATED, and they have their own ceiling. A HARD
# CEILING, NOT A TARGET: it exists so a broken sheet cannot produce a
# thousand-line message. If it is ever actually hit, the closing "N more held"
# line says so.
URGENT_MAX = _int("URGENT_MAX", 25)

# How many NON-URGENT items the digest carries. Everything past the cap is
# counted in one closing line rather than silently dropped.
DIGEST_MAX_ITEMS = _int("DIGEST_MAX_ITEMS", 15)

# The uncapped on-demand list is bounded too — a Discord reply has a size limit.
CADENCE_FULL_LIST_MAX = _int("CADENCE_FULL_LIST_MAX", 200)


# -- THE NEXT-ACTION STATE MACHINE --------------------------------------------
# ONE next action per ACTIVE row, and never more than one.
#
# This REPLACES the old per-row "what next" logic — the three row-hygiene flags
# (HOT / STALLED / DEAD-DEAL) and the ad-hoc deadline kinds that stood in for a
# cadence. Those answered "is something wrong with this row"; a row could match
# two of them at once, and neither said what to actually do. The state machine
# answers "what is the single next thing somebody does about this row, when is
# it due, and who owns it" — which is the question a sales team asks.
#
# EXACTLY ONE ACTION PER ROW is the whole design. Triggers are evaluated in a
# fixed order and the FIRST match wins, so a row can never produce two competing
# instructions. See nextaction.py, which holds the order and the reason for it.
#
# IT SENDS NOTHING. The engine is pure computation: rows in, one action out. It
# has no send path, it is not wired into the daily digest, and the only way to
# see its output is to ask for it ("cadence preview") or to read the startup
# log. That is deliberate — it is safe to deploy with the digest kill switch
# off, and wiring the queue into the digest is a separate, visible decision.

# Master switch for the engine. Off = no queue is computed and "cadence preview"
# says so rather than returning an empty list, which would read as "nothing to
# do" when the truth is "I did not look".
NEXT_ACTION_ENABLED = _bool("NEXT_ACTION_ENABLED", default=True)

# -- THE TWELVE RULES ---------------------------------------------------------
# What the bot does on its own initiative is the twelve rules in
# `bot_rules.yaml`. WHICH DAY each runs, HOW MANY items a post may carry, WHERE
# it goes and WHETHER it counts against the cap all live in that file.
#
# WHAT LIVES HERE IS THE ARITHMETIC EACH RULE NEEDS, and nothing else. A number
# below answers "how long is too long"; the FILE answers "does this rule run
# today". Keeping those apart is what makes a schedule change an edit and a
# restart rather than a developer's afternoon.

# WHERE THE RULES LIVE. The machine copy of the "Bot Rules" tab of
# *Sales Bot_membrane*. Read ONCE at startup: a file that reloaded itself would
# let two messages in one day run under two different schedules, and the log
# line explaining the first would not explain the second.
BOT_RULES_FILE = (os.getenv("BOT_RULES_FILE", "./bot_rules.yaml") or "").strip()

# R3 — EVENTS, EVERY OTHER WEDNESDAY. The anchor is the first Wednesday the rule
# fires on; every second Wednesday after it is a run day. An anchor date rather
# than "odd ISO weeks" because the team picked a date, and an ISO-week parity
# rule silently flips its meaning in any year with 53 weeks.
EVENTS_ANCHOR_DATE = (os.getenv("EVENTS_ANCHOR_DATE", "2026-09-23") or "").strip()

# -- R1 NEWS: WHO TO SEARCH FOR, AND WHAT COUNTS ------------------------------
#
# R1 searches for OUR PEOPLE FIRST and the wider field only as a fallback. News
# about somebody already on our sheets is something the team can act on this
# week; a bigger story about a stranger is reading material.
#
# ROTATION, because the budget cannot carry everyone. Searching every PoC,
# researcher and pipeline company daily is impossible inside
# WEB_SEARCH_DAILY_BUDGET, and searching the same eight every day would mean the
# ninth person is never searched at all. So a last-searched date is kept per
# person and company in SQLite and the LEAST RECENTLY SEARCHED come up first —
# everybody comes round, and the order is a fact in the database rather than an
# accident of sheet order.

# How many people/companies one R1 run searches for. Eight fits a single search
# call's query comfortably; raising it makes each query longer and vaguer rather
# than making the run find more.
NEWS_PEOPLE_PER_RUN = _int("NEWS_PEOPLE_PER_RUN", 8)

# The most stories one post may carry. The rule's own max_items_per_post in
# bot_rules.yaml caps the ITEMS; this caps the STORIES inside the news item.
NEWS_MAX_ITEMS = _int("NEWS_MAX_ITEMS", 5)

# PREFERRED SITES, comma-separated bare domains, and it is a PREFERENCE rather
# than a restriction. The first search is limited to these; if it comes back
# with fewer than NEWS_MIN_ITEMS stories, a SECOND open search runs across the
# whole web. Empty (the default) means one open search and no first pass.
#
# Why not just use WEB_SEARCH_ALLOWED_DOMAINS: that one is a hard allow-list for
# every search the bot makes. This is R1's opinion about where the good AI
# coverage is, and being wrong about it must not cost the day's news.
NEWS_PREFERRED_DOMAINS = _str_list("NEWS_PREFERRED_DOMAINS")

# How thin the preferred-domain pass has to be before the open pass runs.
NEWS_MIN_ITEMS = _int("NEWS_MIN_ITEMS", 3)

# How long a posted story stays remembered, so it is not posted twice. A month:
# a funding round re-reported three weeks later is the same funding round.
NEWS_REPEAT_DAYS = _int("NEWS_REPEAT_DAYS", 30)

# SEED KEYWORDS FOR R1, from the "Sample Keywords" column of the Bot Rules tab.
#
# SEEDS, NOT LIMITS. The sheet says "not limited to these", so the search is
# told they are a starting point and adjacent terms are fair game. They are here
# rather than hardcoded so Vaishnavi can tune them in the sheet; which keyword
# produced which story is logged, so there is something to tune against.
NEWS_KEYWORDS = _str_list("NEWS_KEYWORDS")


# -- R3 EVENTS: DISCOVERY AND DEADLINE BACKFILL -------------------------------
#
# R3 used to read the AI Events & Summits tab and nothing else, so an event
# nobody had typed in did not exist. Discovery searches for events in the
# current and next month and PROPOSES them; an approver says yes and only then
# is a row appended.

# The most events proposed in one run, and in one calendar month. Two limits
# because they stop different things: the per-run limit stops one message
# becoming a listings page, and the per-month limit stops a slow drip of
# proposals nobody has time to read.
EVENTS_DISCOVERY_MAX_PER_RUN = _int("EVENTS_DISCOVERY_MAX_PER_RUN", 3)
EVENTS_DISCOVERY_MAX_PER_MONTH = _int("EVENTS_DISCOVERY_MAX_PER_MONTH", 8)

# How long before the bot asks again about a registration deadline it could not
# find. Saying "I couldn't find it" once is useful; saying it every other
# Wednesday is noise, and the answer rarely changes inside a fortnight.
EVENTS_DEADLINE_RECHECK_DAYS = _int("EVENTS_DEADLINE_RECHECK_DAYS", 14)

# SEED KEYWORDS FOR R3's discovery, from the same "Sample Keywords" column.
# Seeds, not limits — see NEWS_KEYWORDS.
EVENT_KEYWORDS = _str_list("EVENT_KEYWORDS")

# R4 — DELIVERABLES. An item is chased when its tentative deadline is within
# this many days OR has already passed. 3 is the plan's number: close enough to
# be actionable, far enough that the answer is not "I know, it is today".
DELIVERABLE_NEAR_DAYS = _int("DELIVERABLE_NEAR_DAYS", 3)

# R4 — who a deliverable is addressed to when its Functional Dependency cell is
# blank. Blank is common and it is not the same as unowned.
DELIVERABLE_DEFAULT_OWNER = (
    os.getenv("DELIVERABLE_DEFAULT_OWNER", "") or ""
).strip() or "Vaishnavi"

# R4 — which Priority values count as P1.
DELIVERABLE_P1_MARKERS: list[str] = _str_list(
    "DELIVERABLE_P1_MARKERS", "p1,p 1,1,high,highest,critical,urgent",
)

# R4 — Status values that mean the item IS done, and so is never chased.
# Everything else, blank included, is not done.
DELIVERABLE_DONE_MARKERS: list[str] = _str_list(
    "DELIVERABLE_DONE_MARKERS",
    "done,completed,complete,closed,shipped,live,finished,delivered",
)

# R5 — PROSPECTS. Two companies or universities a week, in sheet order, and the
# bot stays with one until every contact on it has a first contact recorded.
# Moving on early is how a company ends up half-contacted forever.
PROSPECT_COMPANIES_PER_WEEK = _int("PROSPECT_COMPANIES_PER_WEEK", 2)

# R5 — after this many posts carrying the SAME contact with nothing changed, the
# bot asks whether to skip them rather than listing them a fourth time. Three is
# where repeating turns into nagging.
PROSPECT_REPEAT_ASK_AT = _int("PROSPECT_REPEAT_ASK_AT", 3)

# R5 — the role order within a company, best first. A contact whose designation
# matches an earlier phrase is offered before one matching a later phrase;
# anything matching none goes last, in sheet order.
PROSPECT_ROLE_ORDER: list[str] = _str_list(
    "PROSPECT_ROLE_ORDER",
    "founder,co-founder,cofounder,ceo,cto,coo,cpo,chief executive,chief technology,"
    "chief scientist,chief,vp,head of research,research lead,principal researcher,"
    "research scientist,researcher,professor,phd,product",
)

# R6 — LinkedIn connected, no DM after this many days.
LI_NO_DM_DAYS = _int("LI_NO_DM_DAYS", 3)

# R7 — DM sent, no meeting after this many days.
DM_NO_MEETING_DAYS = _int("DM_NO_MEETING_DAYS", 7)

# R8 — MEETING PREP. How many days before the meeting each touch fires, and the
# IST time the day-of touch goes at. Touches already in the past are SKIPPED, so
# a meeting booked with three days notice gets the 3-day and day-of touches and
# never a late 5-day one. A reschedule re-anchors every touch on the new date.
MEETING_PREP_DAYS_BEFORE: list[str] = _str_list("MEETING_PREP_DAYS_BEFORE", "5,3")
MEETING_DAYOF_TIME = (os.getenv("MEETING_DAYOF_TIME", "10:00") or "").strip() or "10:00"

# R9 — MEETING DONE, NO NEXT STEPS. First ask this many days after the meeting,
# then every this-many days again.
MEETING_FOLLOWUP_AFTER_DAYS = _int("MEETING_FOLLOWUP_AFTER_DAYS", 3)
MEETING_FOLLOWUP_EVERY_DAYS = _int("MEETING_FOLLOWUP_EVERY_DAYS", 3)

# R9 — the ladder, as destinations in order: one channel post, then two DMs,
# then one escalation to Sid, then STOP. Running off the end stops the chase
# permanently, and that end is the point — a follow-up loop with no last rung is
# the thing that gets a bot muted.
MEETING_FOLLOWUP_LADDER: list[str] = _str_list(
    "MEETING_FOLLOWUP_LADDER", "channel,dm,dm,escalation",
)

# R10 — CLOSURE SUPPORT. Deals STRICTLY ABOVE this closure probability, at deal
# / demo / quote stage. EXACTLY 50 IS EXCLUDED: "above 50" is the rule as
# written, and a boundary a bot decides for itself is a boundary nobody agreed
# to.
CLOSURE_SUPPORT_MIN = _int("CLOSURE_SUPPORT_MIN", 50)

# R10 — which prospect-status values count as deal / demo / quote.
CLOSURE_SUPPORT_STAGES: list[str] = _str_list(
    "CLOSURE_SUPPORT_STAGES",
    "deal,demo,quote,demo done,quote sent,proposal,negotiation",
)

# R11 — NEW COMPANY IN THE MASTER PIPELINE. This many WORKING days after a
# company first appears. There is NO created-date column on that tab, so
# "appears" means "in today's names and not in the last snapshot". The snapshot
# is taken by the CALLER, not by the engine, so the engine stays pure.
NEW_COMPANY_AFTER_WORKING_DAYS = _int("NEW_COMPANY_AFTER_WORKING_DAYS", 1)

# R11 — how long a company stays eligible after it appears. Without a window, a
# snapshot gap (the bot was down for a week) would dump every company added in
# that gap into one post.
NEW_COMPANY_WINDOW_DAYS = _int("NEW_COMPANY_WINDOW_DAYS", 7)

# RETIRED: FOLLOWUP_GRACE_DAYS — the ordinary-cadence trigger it belonged to is
# gone. Each of the twelve rules now carries its own interval, which is the
# point: "how long is too long" is a different number for a connection with no
# DM than for a meeting with no next steps, and one setting could never be both.
#
# RETIRED: CONNECT_DM_CHECK_HOURS — the 48-hour "DM sent?" prompt is gone. R6
# asks at LI_NO_DM_DAYS instead, and asks a better question: it says whether an
# email is on file rather than only whether a DM went out.

# RETIRED: CONNECTION_DM_CHECK_DAYS, DM_PROGRESS_CHECK_DAYS, DEMO_QUOTE_DAYS
# and MEETING_PROPOSAL_WORKING_DAYS — the last four phase-1 thresholds. They
# were still being READ into settings nothing consumed: no rule, no evaluator
# and no answer path referenced any of them after the twelve rules landed.
#
# A threshold that is read but never used is worse than one that is deleted,
# because it survives a grep, appears in the config, and invites somebody to
# tune it and wonder why nothing changed.
#
# What does the work now:
#   CONNECTION_DM_CHECK_DAYS       -> R6's LI_NO_DM_DAYS
#   DM_PROGRESS_CHECK_DAYS         -> R7's DM_NO_MEETING_DAYS
#   DEMO_QUOTE_DAYS                -> R10's CLOSURE_SUPPORT_MIN / _STAGES
#   MEETING_PROPOSAL_WORKING_DAYS  -> each rule's own interval in
#                                     bot_rules.yaml. "How long is too long" is
#                                     a different number per rule, which is why
#                                     one setting could never be all of them.

# RETIRED: CHANNEL_SWITCH_AT and UNRESPONSIVE_SUGGEST_AT — the channel-switch
# counsel and the unresponsive suggestion are both gone. Neither counted touches
# the sheet actually records any more (the follow-up columns they read were
# retired with the tracker tab), so both had become counters of a number nobody
# was writing down. A human still marks a contact unresponsive, and the STOP
# semantics below honour it the moment they do.
#
# RETIRED: SLOW_LANE_DAYS — the two-lane split is gone. R10 gates on closure
# probability directly (CLOSURE_SUPPORT_MIN) rather than pacing every row by
# which side of a threshold it fell.

# RETIRED: SLOW_LANE_DAYS, HOT_DEAL_DAYS and CLOSURE_HOT_THRESHOLD — the
# two-lane split is gone entirely. The last two were still read, and used by
# nothing: R10 gates on closure probability directly (CLOSURE_SUPPORT_MIN)
# rather than sorting every row into a fast lane and a slow one and pacing each
# lane on its own clock.

# RETIRED: ON_HOLD_PULSE_DAYS — the monthly on-hold pulse is gone. A parked deal
# is now simply a deal no rule selects; DEAL_ON_HOLD_MARKERS below still
# identifies one so answers can say a row is parked rather than silent.

# (10) STOP, FOREVER. Closure values that end the row: no next action is ever
# produced for it again, by any trigger, including the priority override.
# Matched case-insensitively as whole phrases against the closure cell; a
# closure of exactly 0% stops the row too, and that is handled numerically
# rather than by this list.
CLOSURE_STOP_MARKERS: list[str] = _str_list(
    "CLOSURE_STOP_MARKERS",
    "dead,unresponsive,won,lost,closed won,closed lost,closed-won,closed-lost,"
    "not interested,do not contact,dnc",
)

# What the deal-status cell says when a deal is parked, and when it is live.
# Both are phrase lists so a sheet that writes "Paused" instead of "On Hold"
# needs an env change rather than a code change.
DEAL_ON_HOLD_MARKERS: list[str] = _str_list(
    "DEAL_ON_HOLD_MARKERS", "on hold,onhold,on-hold,paused,parked,deferred",
)
DEAL_IN_PROGRESS_MARKERS: list[str] = _str_list(
    "DEAL_IN_PROGRESS_MARKERS",
    "in progress,inprogress,in-progress,active,live,ongoing,negotiating,proposal sent",
)

# What the prospect/stage cell says when a demo has happened. The quote chase
# fires off this.
PROSPECT_DEMO_MARKERS: list[str] = _str_list(
    "PROSPECT_DEMO_MARKERS", "demo,demo done,demo given,demo completed,demo scheduled,demoed",
)

# What the first-contact-type cell says when the first contact WAS by email.
# The progress check reads this and drops its "ask for their email" line when it
# matches — see DM_PROGRESS_CHECK_DAYS.
EMAIL_CONTACT_TYPES: list[str] = _str_list(
    "EMAIL_CONTACT_TYPES", "email,e-mail,mail,cold email,emailer",
)

# NO DUE DATE EVER LANDS ON A SATURDAY OR A SUNDAY. Every computed date is
# shifted forward to the Monday.
#
# THE ONE EXCEPTION IS AN EXPLICITLY SCHEDULED REMINDER — a one-off somebody
# asked for at a specific time ("remind me about Acme on Saturday morning").
# That is a person's own instruction about their own weekend and the bot has no
# business moving it. Rows in the scheduled_reminders table keep their exact
# date; everything else is shifted.
#
# Turning this off is supported but almost certainly wrong: a Saturday due date
# is read on Monday anyway, two days late, and looks like the bot cannot read a
# calendar.
NEXT_ACTION_WEEKEND_SHIFT = _bool("NEXT_ACTION_WEEKEND_SHIFT", default=True)

# How many lines "cadence preview" prints. It is a read-only answer in a Discord
# message, and a message has a size limit; the count of what was cut is always
# reported alongside.
NEXT_ACTION_PREVIEW_MAX = _int("NEXT_ACTION_PREVIEW_MAX", 60)


# -- Meeting-prep briefs ------------------------------------------------------
# A meeting inside MEETING_PREP_DAYS gets ONE prep brief attached to that day's
# digest, deduped in SQLite so it is written once per meeting rather than once
# per day until the meeting happens.
CADENCE_PREP_BRIEFS_ENABLED = _bool("CADENCE_PREP_BRIEFS_ENABLED", default=True)
# How many briefs one digest may carry. Three meetings on one day is a good day;
# ten is a formatting problem.
CADENCE_PREP_MAX_PER_DIGEST = _int("CADENCE_PREP_MAX_PER_DIGEST", 3)
# How far back the brief looks for a meeting note about the same company.
CADENCE_PREP_NOTES_DAYS = _int("CADENCE_PREP_NOTES_DAYS", 120)

# THE MEETING-PREP BRIEF CARRIES NO WEB RESEARCH, and says so in as many words
# rather than being quietly absent. Web search EXISTS now (WEB_SEARCH_ENABLED)
# and the drip's rules use it — the brief is built from the sheets and the
# meeting notes, and nothing in it may be invented. Turning this off only hides
# the notice; it does not add research to the brief.
CADENCE_PREP_NOTE_NO_WEB = _bool("CADENCE_PREP_NOTE_NO_WEB", default=True)

# -- Deadline authority -------------------------------------------------------
# When asked about a deadline that doesn't exist, the bot SETS one rather than
# shrugging. Defaults are in WORKING DAYS, IST. The strategy doc's cadence wins
# over these whenever that doc is readable.

# Days after first contact / last touch before the next outreach follow-up is due.
OUTREACH_FOLLOWUP_DAYS = _int("OUTREACH_FOLLOWUP_DAYS", 3)
# Days to wait on a reply before chasing it.
REPLY_CHASE_DAYS = _int("REPLY_CHASE_DAYS", 2)
# Days before a booked meeting that prep is due. This ALSO defines the
# meeting-prep window for the cadence: a meeting within this many days is URGENT
# in the digest and is what triggers its one prep brief. Raised from 1 to 4 for
# phase 1 — a brief that lands the morning of the meeting is too late to act on.
MEETING_PREP_DAYS = _int("MEETING_PREP_DAYS", 4)

# Discord ids told about every deadline the bot sets ("shout to change"). These
# must ALSO be in TEAM_ROSTER_IDS to actually be pinged — the roster is the only
# thing that authorises a mention.
DEADLINE_NOTIFY_IDS: list[int] = _int_list("DEADLINE_NOTIFY_IDS")

# Who an unanswered chase escalates to after COS_NUDGE_MAX_ATTEMPTS. Also must be
# on the roster to be pinged.
ESCALATE_TO_ID = _int("ESCALATE_TO_ID", 0)

# -- WHO EVERY CHANNEL MESSAGE TAGS -------------------------------------------
# Vaishnavi and Sid, on every proactive CHANNEL message, at the START of it.
# Strategy section 9: "Every channel message tags both Vaishnavi and Sid."
#
# AT THE START, NOT THE END, and that is the whole reason this is a setting
# rather than a line in the composer. A tag at the end of a paragraph is read
# after the paragraph, which is exactly the wrong order for something that says
# "this is for you". A reader who sees their name first decides whether to read
# on; one who sees it last has already decided not to.
#
# EVERY ID HERE MUST ALSO BE IN TEAM_ROSTER_IDS. `mention_for()` is the only
# thing in this codebase that produces a mention token and it checks the roster
# on the id, so an id missing from the roster is named in plain text instead of
# pinged — silently, as far as the message is concerned. The boot check below
# names any that are missing, because "we tagged them" and "we wrote their name"
# look identical in a log and are not the same thing at all.
#
# DMs ARE NOT TAGGED. A DM is already addressed to exactly one person; opening
# it by tagging two others would be strange, and one of them is usually not in
# it. See `always_tag_ids()`.
SALES_ALWAYS_TAG_IDS: list[int] = _int_list("SALES_ALWAYS_TAG_IDS")


def always_tag_ids() -> list:
    """The ids every proactive CHANNEL message opens by tagging.

    A function rather than the constant so the CURRENT value is read on every
    message. Off-roster ids are filtered out HERE as well as in `mention_for()`
    — belt and braces, because this list is the one place a person is tagged
    without anybody having decided it per-message.
    """
    return [uid for uid in SALES_ALWAYS_TAG_IDS if uid in TEAM_ROSTER_IDS]


# -- THE NARROW DM EXCEPTION ---------------------------------------------------
# THE BAN IS STILL THE DEFAULT AND STILL ENFORCED IN CODE. What follows opens
# exactly two doors, and `guardrails.send()` refuses everything else.
#
# OFF BY DEFAULT, FOR THE ROLLOUT. A DM is the most intrusive thing this bot can
# do and the first one will arrive unannounced. Turning it on is somebody's
# decision, taken once, with the reasons in front of them.
SALES_DMS_ENABLED = _bool("SALES_DMS_ENABLED", default=False)

# DOOR (a): AN ITEM THIS MANY DAYS OVERDUE, to the person who owns it. Overdue
# means past its deadline OR past its first reminder — whichever the item
# carries. Three days is the number the strategy doc names.
DM_OVERDUE_DAYS = _int("DM_OVERDUE_DAYS", 3)

# DOOR (b): R9's SECOND AND THIRD FOLLOW-UPS. The ladder is
# MEETING_FOLLOWUP_LADDER (channel, dm, dm, escalation) and these are rungs 2
# and 3 — the escalation rung goes to ESCALATE_TO_ID and is door (a)'s shape,
# not this one. Rungs are 1-based here because that is how the ladder reads to a
# person counting them.
DM_MEETING_FOLLOWUP_RUNGS: list[int] = _int_list("DM_MEETING_FOLLOWUP_RUNGS") or [2, 3]

# NEVER THE SAME ITEM IN THE CHANNEL AND A DM ON THE SAME DAY. Enforced against
# the drip ledger by item key; this setting exists so the rule can be relaxed
# for a test run without editing code, and it should not be.
DM_SAME_DAY_AS_CHANNEL = _bool("DM_SAME_DAY_AS_CHANNEL", default=False)

# WHO AN ESCALATION IS ADDRESSED TO when it posts in the channel because DMs are
# off. Strategy section 8: Sid's word is final, so an escalation that cannot be
# a DM is a channel post with his name on it — not an unaddressed one that
# everybody assumes somebody else is handling.
ESCALATION_ADDRESSEE = (
    os.getenv("ESCALATION_ADDRESSEE", "") or ""
).strip() or "Sid"


def may_dm(user_id) -> bool:
    """Is this person DM-able at all? Roster membership plus the master switch.

    THE ROSTER GATE IS NOT NEGOTIABLE and is checked here as well as in
    `guardrails.send()`: this is a bot that must never contact anybody outside
    the team, and a DM is the one path where "outside the team" would be
    invisible to everyone but the recipient.
    """
    if not SALES_DMS_ENABLED:
        return False
    try:
        return int(user_id) in TEAM_ROSTER_IDS
    except (TypeError, ValueError):
        return False


# -- THE LEAVE CHANNEL: THE ONE NON-SALES CHANNEL THE BOT MAY READ -------------
# Before the bot addresses somebody, it checks whether they said they are off
# today. Addressing a person on leave is how a nudge becomes noise they come
# back to a week later.
#
# READ-ONLY, AND ENFORCED AS SUCH. `guardrails.may_read` admits this ONE extra
# id; `guardrails.send` does NOT — its channel check is still SALES_CHANNEL_IDS
# alone, so there is no code path that posts here. Read and write are two
# separate gates in this module precisely so one can be widened without the
# other.
#
# 0/unset turns the check off entirely, and the bot then addresses everybody as
# though they were in. That is the honest degraded behaviour, and it is logged
# at startup rather than left to be discovered.
HOLIDAY_CHANNEL_ID = _int("HOLIDAY_CHANNEL_ID", 0)

# How far back to read that channel. Leave is usually announced the day before
# or the morning of; a fortnight covers "I am off all next week" posted early.
HOLIDAY_LOOKBACK_DAYS = _int("HOLIDAY_LOOKBACK_DAYS", 14)

# How long a leave read is cached. The channel changes a few times a week, and
# re-reading it for every addressed owner in a post would be several history
# scans for one message.
HOLIDAY_CACHE_MINUTES = _int("HOLIDAY_CACHE_MINUTES", 30)

# WHO THE WORK FALLS TO when its owner is away, in order. Owner on leave ->
# the first name here; that person on leave -> the next; and so on.
#
# THE LAST NAME IS NEVER TREATED AS ON LEAVE. Sid is the backstop: somebody has
# to be addressable or the item silently goes to nobody, and an item nobody is
# addressed about is an item nobody chases. Sid may well be away — the point is
# that the bot does not get to decide there is no one left to tell.
LEAVE_FALLBACK_ORDER: list[str] = _str_list("LEAVE_FALLBACK_ORDER", "Vaishnavi,Sid")

# -- Row hygiene flags — RETIRED ----------------------------------------------
# The three flags (HOT / STALLED / DEAD-DEAL) and their settings
# (STALLED_AFTER_DAYS, SHEET_FLAGS_ENABLED, and MAX_FLAGS_PER_SWEEP before them)
# are GONE. They were the old per-row "what next" logic and the NEXT-ACTION
# STATE MACHINE replaces them outright.
#
# WHY REPLACED RATHER THAN KEPT ALONGSIDE. The flags answered "is something
# wrong with this row", the state machine answers "what is the single next thing
# somebody does about it". A row could be HOT and DEAD-DEAL at once and the code
# had a dedup ordering to pick between them; the state machine cannot produce
# two answers, because its triggers are evaluated in order and the first match
# wins. Two systems both deciding what a row needs is how a bot ends up
# contradicting itself in one message.
#
# Their proactive outlet — the digest's HOT and HYGIENE sections — is unwired
# with them. The state machine deliberately has no proactive outlet yet: it is
# read through "cadence preview" and the startup log. See NEXT_ACTION_* above.

# -- THE DRIP SCHEDULER -------------------------------------------------------
# THE ONE DAILY DIGEST IS RETIRED. Its format — one long message at
# SALES_DIGEST_TIME, grouped into HOT / DEADLINES / OVERDUE / ESCALATIONS /
# HYGIENE / five cadence sections, with carry-forward "(3rd day)" markers — is
# gone. What replaces it is a DRIP: at most DAILY_MESSAGE_CAP short messages a
# weekday, time-spaced, one per (action type x owner).
#
# WHY. The digest was one message a day because six kinds of scattered message
# got the bot muted. It solved that and created the opposite problem: a wall of
# sections that reads like a report, gets skimmed, and asks a person to find
# their own name in it. The drip keeps the volume contract — three messages, not
# six paths — and gives each one a single subject and a single owner, which is
# how a person actually receives work.
#
# THE GROUPING RULE IS ABSOLUTE: one message per (type x owner), companies
# comma-separated in one sentence. NEVER two types in a message, never two
# owners. That is what makes a message answerable: "yes, done" means something
# when the message asked one thing of one person.
#
# The two things that are NOT the drip, and are still IMMEDIATE — spacing
# applies to proactive sends only, never to answers:
#   - a REPLY to a question someone asked;
#   - the ask-time deadline announcement, which is the answer to "when is the
#     follow-up for X?" and is consent-based by design ("shout to change").

# THE KILL SWITCH FOR EVERY UNPROMPTED MESSAGE. UNCHANGED, AND DELIBERATELY SO.
#
# The name, the semantics and the suppressed-log line are all kept as they were.
# The switch is currently false on the server, and an operator who set it that
# way to stop the bot talking must not discover that a rewrite quietly re-armed
# it under a new name. The drip INHERITS this switch; it does not get one of its
# own.
#
#   true / unset  the drip sends its messages (the historical behaviour, which
#                 is why unset means on).
#   false         the bot sends NOTHING unprompted. Not a drip message, not a
#                 re-ask, nothing. There is no second switch to also check.
#
# WHAT DOES NOT STOP: everything still computes. Deadlines are still tracked,
# the next-action queue is still evaluated, SQLite state and audit.jsonl are
# still written. The bot still answers when @-mentioned ("cadence preview",
# "sheet status", sheet questions) and still announces a deadline when someone
# asks for one. Only the unprompted sending stops.
#
# This module-level value is the BOOT reading, used for the startup log below.
# The runtime gate is `digest_enabled()`, which re-reads the setting at send
# time so flipping it does not need a restart.
SALES_DIGEST_ENABLED = _bool("SALES_DIGEST_ENABLED", default=True)


def digest_enabled() -> bool:
    """Is unprompted sending on? Read LIVE, not at import.

    KEPT UNDER ITS OLD NAME on purpose. This is the same gate the retired digest
    used, read at the same point in the tick, logging the same line. The drip
    inherits it rather than introducing a switch of its own — an operator who
    turned the bot off should not have to learn a new variable to keep it off.

    Read at send time rather than at boot so an operator can flip the switch and
    have it take effect on the next sweep tick. A restart applies it too, of
    course — this just doesn't require one.

    WHERE IT READS FROM, and why in this order: this project's contract is that
    config lives in .env and pm2 passes no settings of its own (see
    ecosystem.config.js), so the .env file is the thing an operator actually
    edits and it is consulted first — that is what makes the switch live. The
    process environment is the fallback, which is also the answer when there is
    no .env file at all. The file is re-read only when its mtime changes, so the
    sweep tick is not doing disk I/O every few minutes for one boolean.

    Anything unreadable — a missing file, a permission error, a value that isn't
    a boolean — falls back to the boot reading. A kill switch that fails to a
    guess would be worse than one that fails to what the operator last booted
    with.
    """
    raw = _dotenv_value("SALES_DIGEST_ENABLED")
    if raw is None:
        raw = os.getenv("SALES_DIGEST_ENABLED")
    if raw is None or not raw.strip():
        return SALES_DIGEST_ENABLED
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    log.warning(
        "SALES_DIGEST_ENABLED=%r is not a recognised boolean; using the value the bot "
        "booted with (%s).", raw, SALES_DIGEST_ENABLED,
    )
    return SALES_DIGEST_ENABLED

# WHEN THE FIRST MESSAGE OF THE DAY GOES OUT. Wall-clock IST, "HH:MM", computed
# against Asia/Kolkata explicitly (see deadlines.IST) and never against the
# server clock — a cloud box runs UTC, and "10:00" server time would land at
# 15:30 for the team.
SALES_DRIP_START = (
    os.getenv("SALES_DRIP_START", "") or os.getenv("SALES_DIGEST_TIME", "") or ""
).strip() or "14:00"

# SALES_DIGEST_TIME is kept as an ALIAS so an existing .env keeps working —
# SALES_DRIP_START wins if both are set. It named the digest's single send time
# and now names the first drip slot, which is the same thing an operator meant
# by it.
SALES_DIGEST_TIME = SALES_DRIP_START

# THE END OF THE POSTING WINDOW. Nothing proactive lands after this, ever.
#
# WITHOUT IT THE DAY HAD NO CEILING. Six posts at 90-minute gaps from 14:00 ran
# to 21:13 — the cap kept the COUNT down and nothing kept the last one out of
# somebody's evening. A message at nine at night is not read that night and is
# resented in the morning.
#
# HOW THE FIT WORKS, in order:
#   1. try MESSAGE_GAP_MINUTES between every post;
#   2. if that overruns SALES_DRIP_END, shrink the gap EVENLY until the day
#      fits — every post moves, none is singled out;
#   3. never below MESSAGE_GAP_MIN_MINUTES;
#   4. whatever still does not fit ROLLS to the next applicable day, where it
#      goes at the FRONT — ahead of that day's own items, because it has
#      already waited.
#
# THE ONE EXCEPTION IS R8's DAY-OF TOUCH. It keeps MEETING_DAYOF_TIME (10:00),
# outside the window, because a note about a meeting that starts at 11 is
# worthless at 14:00.
SALES_DRIP_END = (
    os.getenv("SALES_DRIP_END", "") or ""
).strip() or "18:30"


def drip_end_ist() -> tuple[int, int]:
    """SALES_DRIP_END as (hour, minute) IST. Unreadable falls back to 18:30."""
    import digest as _digest

    return _digest.parse_time(SALES_DRIP_END, default="18:30")

# HOW MANY PROACTIVE MESSAGES A WEEKDAY, EVER. The volume contract, and a HARD
# ceiling rather than a target: three short messages a day is what a busy
# channel absorbs without learning to skim. Anything past it ROLLS TO TOMORROW
# rather than being dropped — except that a positive reply overrides the roll,
# because a reply that waits a day is a reply that goes cold.
DAILY_MESSAGE_CAP = _int("DAILY_MESSAGE_CAP", 3)

# ...AND THE PER-DAY SHAPE, which is what the schedule actually needs. Monday
# and Tuesday carry more rules than Wednesday, so a single scalar either
# throttled those two days or let the quiet ones run loose.
#
# "mon:4,tue:4,wed:3,thu:3,fri:3,sun:1" — any day not named here falls back to
# DAILY_MESSAGE_CAP, and Saturday is absent on purpose: the day is silent and
# `is_sending_day` refuses it before a cap is ever consulted.
#
# SUNDAY IS 1 AND THAT IS NOT A TYPO. Sunday sends at most one post, and only
# for Deliverables Checklist items due on the Monday — see SUNDAY_RULE_IDS.
DAILY_MESSAGE_CAP_BY_DAY = (
    os.getenv("DAILY_MESSAGE_CAP_BY_DAY", "") or ""
).strip() or "mon:4,tue:4,wed:3,thu:3,fri:3,sun:1"


def _parse_day_caps(spec: str) -> dict:
    """"mon:4,tue:4" -> {0: 4, 1: 4}, keyed by Python's weekday().

    An unparseable fragment is DROPPED WITH A WARNING NAMING IT rather than
    raising: a typo here must not take the bot down, and the days that did parse
    still mean exactly what they say. The named-but-broken day then falls back
    to DAILY_MESSAGE_CAP, which is the safe direction — a smaller cap, not a
    bigger one.
    """
    names = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    out: dict = {}
    for part in str(spec or "").split(","):
        frag = part.strip()
        if not frag:
            continue
        day, _sep, value = frag.partition(":")
        key = names.get(day.strip().lower()[:3])
        try:
            cap = int(str(value).strip())
        except (TypeError, ValueError):
            cap = -1
        if key is None or cap < 0:
            log.warning(
                "DAILY_MESSAGE_CAP_BY_DAY: %r is not a day:cap pair like 'mon:4'; that "
                "fragment is ignored and %s falls back to DAILY_MESSAGE_CAP=%d. The "
                "other days still apply.", frag, day.strip() or "?", DAILY_MESSAGE_CAP,
            )
            continue
        out[key] = cap
    return out


DAILY_MESSAGE_CAPS: dict = _parse_day_caps(DAILY_MESSAGE_CAP_BY_DAY)


def message_cap_for(day) -> int:
    """The cap for ONE day. DAILY_MESSAGE_CAP_BY_DAY first, then the scalar.

    A function rather than a lookup at import so it reflects the CURRENT value
    even if something reassigns it at runtime — a snapshot taken at import time
    would be a hole in exactly the guarantee the volume contract exists to make.
    """
    try:
        weekday = day.weekday()
    except AttributeError:
        return max(0, int(DAILY_MESSAGE_CAP))
    return max(0, int(DAILY_MESSAGE_CAPS.get(weekday, DAILY_MESSAGE_CAP)))

# MINUTES BETWEEN PROACTIVE MESSAGES. Three messages in one minute is one long
# message with extra steps; the spacing is what makes each one land as its own
# thing and gives the person time to act on the first before the second arrives.
MESSAGE_GAP_MINUTES = _int("MESSAGE_GAP_MINUTES", 90)

# ...PLUS OR MINUS THIS MANY MINUTES. A bot that posts at exactly 10:00, 11:30
# and 13:00 every single day reads as a machine running a script, and people
# start filing it as such. The jitter is DETERMINISTIC — seeded on the date and
# the slot number — so a restart mid-morning recomputes the same schedule and
# cannot double-send. Set to 0 for exact spacing.
MESSAGE_JITTER_MINUTES = _int("MESSAGE_JITTER_MINUTES", 15)

# THE FLOOR THE GAP MAY SHRINK TO when a day will not otherwise fit inside the
# posting window. Below this the messages stop reading as separate things and
# start reading as one long one delivered in instalments — which is the failure
# the spacing exists to prevent, so the day rolls its overflow instead of
# squeezing any harder.
MESSAGE_GAP_MIN_MINUTES = _int("MESSAGE_GAP_MIN_MINUTES", 30)

# ONE GENTLE RE-ASK, this many days after a nudge nobody actioned. Then the
# subject goes back to the normal cadence and is never re-asked again for that
# nudge. Two asks is a reminder; three is nagging, and nagging is what gets a
# bot muted.
DRIP_REASK_DAYS = _int("DRIP_REASK_DAYS", 2)

# WEEKDAYS ONLY. The drip does not send on Saturdays or Sundays — the same
# instinct as the next-action engine's weekend shift, applied to the sending
# rather than to the due date. Set false to send every day.
DRIP_WEEKDAYS_ONLY = _bool("DRIP_WEEKDAYS_ONLY", default=True)

# Which sales channel the drip sends in. 0/unset → the ask channel, else the
# first channel in SALES_CHANNEL_IDS. Kept under its old name because it is the
# same channel the digest used and an existing .env should keep working.
SALES_DIGEST_CHANNEL_ID = _int("SALES_DIGEST_CHANNEL_ID", 0)

# HOW MANY ITEMS ONE POST CARRIES before the rest roll to that rule's next
# scheduled day. A sentence listing twenty companies is a list wearing a
# sentence's clothes.
#
# THE OVERFLOW ROLLS, IT IS NOT TRIMMED. A rule that produced nine items and may
# say five has four waiting for its next run — not four dropped. The preview and
# the plan both report the number waiting, because silently losing four
# prospects is the failure this cap could otherwise cause.
#
# DRIP_MAX_COMPANIES_PER_MESSAGE IS KEPT AS AN ALIAS so an existing .env keeps
# working. The new name wins when both are set: a rule's items are no longer
# always companies (R4 carries deliverables, R12 carries packages), and the old
# name had stopped describing what it capped.
DRIP_MAX_ITEMS_PER_POST = _int(
    "DRIP_MAX_ITEMS_PER_POST",
    _int("DRIP_MAX_COMPANIES_PER_MESSAGE", 5),
)

# The old name, still readable by anything that has not been migrated. Assigned
# from the new one so the two can never hold different numbers.
DRIP_MAX_COMPANIES_PER_MESSAGE = DRIP_MAX_ITEMS_PER_POST

# WHICH RULES MAY SEND ON A SUNDAY, and nothing else may. Saturday is silent
# outright; Sunday carries ONE post, at SALES_DRIP_START, and only for
# Deliverables Checklist items whose deadline falls on the Monday.
#
# A rule id list rather than a boolean because "the weekend is silent except for
# this one thing" is a rule about WHICH thing, and encoding it as a flag would
# put the answer in code where nobody can change it.
SUNDAY_RULE_IDS: list[str] = _str_list("SUNDAY_RULE_IDS", "R4")

# NO PUBLIC-HOLIDAY HANDLING. Deliberately, and stated rather than left as an
# absence: the bot posts on a public holiday exactly as it would on any weekday.
# The leave check (HOLIDAY_CHANNEL_ID) covers a person being away; a whole team
# being away is a thing somebody can switch off with SALES_DIGEST_ENABLED.

# Compose the message text with the model, in the voice exemplars in
# sales_policy.md. Off (or any model failure) falls back to a deterministic
# template that is still one sentence, still gives an out, and still names the
# companies — a model outage must cost polish, never the message.
DRIP_LLM_COMPOSE = _bool("DRIP_LLM_COMPOSE", default=True)


def drip_start_ist() -> tuple[int, int]:
    """SALES_DRIP_START as (hour, minute) IST. Unreadable values fall back to
    10:00 with a warning rather than to "never"."""
    import digest as _digest

    return _digest.parse_time(SALES_DRIP_START)


def digest_time_ist() -> tuple[int, int]:
    """Kept as an alias for `drip_start_ist()` — same value, older name."""
    return drip_start_ist()


# -- Weekly funnel numbers ----------------------------------------------------
# The funnel used to be its own scheduled post. It isn't any more — a second
# unprompted message a week is still a second unprompted message. The numbers
# now ride along as a section of the daily digest on WEEKLY_DIGEST_WEEKDAY.

WEEKLY_DIGEST_ENABLED = _bool("WEEKLY_DIGEST_ENABLED", default=True)
# 0=Monday … 4=Friday … 6=Sunday. Friday by default. Which day's digest carries
# the funnel block.
WEEKLY_DIGEST_WEEKDAY = _int("WEEKLY_DIGEST_WEEKDAY", 4)
# WEEKLY_DIGEST_HOUR_IST is RETIRED: the funnel goes out with the daily digest,
# so its time is SALES_DIGEST_TIME.
# Which sales channel it posts in. Kept as an alias for SALES_DIGEST_CHANNEL_ID
# so an existing .env keeps working; SALES_DIGEST_CHANNEL_ID wins if both are set.
WEEKLY_DIGEST_CHANNEL_ID = _int("WEEKLY_DIGEST_CHANNEL_ID", 0)


def digest_channel_id() -> int:
    """Where the daily digest goes. ALWAYS a sales channel: an explicit id is
    honoured only if it is in scope, so this setting can't be used to post
    outside SALES_CHANNEL_IDS."""
    for name, value in (
        ("SALES_DIGEST_CHANNEL_ID", SALES_DIGEST_CHANNEL_ID),
        ("WEEKLY_DIGEST_CHANNEL_ID", WEEKLY_DIGEST_CHANNEL_ID),
    ):
        if not value:
            continue
        if is_sales_channel(value):
            return value
        log.warning(
            "%s=%s is not in SALES_CHANNEL_IDS — ignoring it and using a sales "
            "channel instead.",
            name, value,
        )
    if SALES_ASK_CHANNEL_ID:
        return SALES_ASK_CHANNEL_ID
    return SALES_CHANNEL_IDS[0] if SALES_CHANNEL_IDS else 0


# -- Logging ------------------------------------------------------------------

LOG_LEVEL = (os.getenv("LOG_LEVEL", "INFO") or "INFO").strip().upper()


def validate() -> list[str]:
    """Return the list of missing REQUIRED env vars (empty when startup is safe).
    Everything else here is a warning: legal, but probably not what you meant.

    One check here does more than warn: a playbook id that resolves to the
    read-only mapping sheet forces SHEET_WRITES_ENABLED off, which is why this
    function rebinds it."""
    global SHEET_WRITES_ENABLED

    required = {
        "DISCORD_TOKEN": DISCORD_TOKEN,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    }
    missing = [k for k, v in required.items() if not v]

    # The scope list is required. With no channels the bot has nowhere legal to
    # read or speak, and a bot that can't be scoped shouldn't be running at all.
    if not SALES_CHANNEL_IDS:
        missing.append("SALES_CHANNEL_IDS")

    if not SALES_ASK_CHANNEL_ID:
        log.warning(
            "SALES_ASK_CHANNEL_ID is unset — the daily digest and the deadline "
            "announcements will fall back to the first channel in SALES_CHANNEL_IDS. "
            "Answering is unaffected: in every sales channel the bot replies only when "
            "@-mentioned or replied to."
        )

    # The canonical tab, the restricted bands, and what is left of the cadence.
    if not GTM_POCS_TAB_TITLES:
        log.error(
            "GTM_POCS_TAB_TITLES is empty, so there is no canonical tab. The bot's "
            'sheet world is the "Outreach PoCs" tab and it is found BY NAME — with no '
            "name to look for, every proactive feature has nothing to run against. Set "
            "GTM_POCS_TAB_TITLES to the tab's exact title."
        )
    else:
        log.info(
            "[config] canonical tab (by name): %s. The old outreach tracker tab and the "
            "phase-1 cadence rules (a-j) are RETIRED — nothing evaluates against them.",
            ", ".join(repr(t) for t in GTM_POCS_TAB_TITLES),
        )

    # THE FIVE READ-ONLY CONTEXT TABS, ALSO FOUND BY NAME. Logged together so
    # that one glance at the boot log answers "what is this bot looking for",
    # and an empty setting is an ERROR rather than a shrug: a name-discovered
    # tab with no name to look for is not found, and nothing that depends on it
    # says a word about it afterwards.
    for label, titles, setting in (
        ("deliverables checklist", GTM_DELIVERABLES_TAB_TITLES,
         "GTM_DELIVERABLES_TAB_TITLES"),
        ("master pipeline", GTM_PIPELINE_TAB_TITLES, "GTM_PIPELINE_TAB_TITLES"),
        ("sales packages", GTM_PACKAGES_TAB_TITLES, "GTM_PACKAGES_TAB_TITLES"),
        ("events & summits", GTM_EVENTS_TAB_TITLES, "GTM_EVENTS_TAB_TITLES"),
        ("goal setting", GTM_GOALS_TAB_TITLES, "GTM_GOALS_TAB_TITLES"),
    ):
        if not titles:
            log.error(
                "%s is empty, so the %s tab is found by NO name and will not be read. "
                "Everything that depends on it goes silent without saying so. Set it to "
                "the tab's exact title.", setting, label,
            )
        else:
            log.info(
                "[config] %-22s (read-only, by name): %s",
                label, ", ".join(repr(t) for t in titles),
            )

    # THE WRITE LOCK. Logged whether or not writing is on, because "which columns
    # can the bot touch" is a question people ask about a bot that is currently
    # read-only, and the answer must not depend on a switch somewhere else.
    if not RESTRICTED_COLUMN_BANDS:
        log.warning(
            "RESTRICTED_COLUMN_RANGES=%r resolved to NO bands — no column is denied to "
            "the write paths. That is almost certainly a typo: the default is 'A:I,S:X'. "
            "Reading is unrestricted either way.",
            RESTRICTED_COLUMN_RANGES,
        )
    else:
        window = writable_window_label()
        log.info(
            "[config] restricted (never written): %s. Writable window between the bands: "
            "%s. Reading is UNRESTRICTED — this is a write lock only.",
            ", ".join(
                f"{column_label(lo)}:{column_label(hi)}" if lo != hi else column_label(lo)
                for lo, hi in RESTRICTED_COLUMN_BANDS
            ),
            window or "(none — the bands leave no gap between them)",
        )
        if not window:
            log.warning(
                "RESTRICTED_COLUMN_RANGES=%r leaves NO writable window between the bands, "
                "so every write inside the banded region will be refused. Deadline "
                "mirroring will report that refusal rather than writing.",
                RESTRICTED_COLUMN_RANGES,
            )

    if CADENCE_ENABLED:
        log.info(
            "[config] cadence block ON. The phase-1 rules are retired, so the cadence "
            "sections carry sheet-health flags only until a phase-2 rule set exists. "
            "URGENT_MAX=%d DIGEST_MAX_ITEMS=%d MEETING_PREP_DAYS=%d",
            URGENT_MAX, DIGEST_MAX_ITEMS, MEETING_PREP_DAYS,
        )
        if DIGEST_MAX_ITEMS < 1:
            log.warning(
                "DIGEST_MAX_ITEMS=%d caps the digest's non-urgent items at nothing.",
                DIGEST_MAX_ITEMS,
            )
        if URGENT_MAX < 1:
            log.warning(
                "URGENT_MAX=%d — the urgent items would be capped at nothing. That is a "
                "hard ceiling against a broken sheet, not a target.",
                URGENT_MAX,
            )
        if not GTM_MASTER_TAB_TITLES:
            log.info(
                "GTM_MASTER_TAB_TITLES is empty. That is fine — the master tab is found "
                "by its header signature ('Response Status' + 'Intro Sent' + 'Meeting "
                "Done'), and the name is only a tie-break hint."
            )
        if not CADENCE_REJECTED_MARKERS:
            log.warning(
                "CADENCE_REJECTED_MARKERS is empty — the only rows treated as rejected "
                "will be the ones whose Response says so ('N', 'N - Rejected'). A row "
                "written off in a Reason or Status cell will stay visible to the "
                "proactive features."
            )
        if not SALES_DEFAULT_OWNER_ID:
            log.warning(
                "SALES_DEFAULT_OWNER_ID is unset and the canonical tab may have no owner "
                "column, so no proactive line can be addressed to anybody: they will all "
                "go out under the DEADLINE_NOTIFY_IDS group line. Set it to Vaishnavi's "
                "Discord id."
            )
        elif SALES_DEFAULT_OWNER_ID not in TEAM_ROSTER_IDS:
            log.warning(
                "SALES_DEFAULT_OWNER_ID=%d is not in TEAM_ROSTER_IDS — the roster gate "
                "fails closed, so items will name that person in plain text instead of "
                "@-mentioning them. Add the id to TEAM_ROSTER_IDS.",
                SALES_DEFAULT_OWNER_ID,
            )

    if not TEAM_ROSTER_IDS and not TEAM_ROSTER_NAMES:
        log.warning(
            "TEAM_ROSTER_IDS and TEAM_ROSTER_NAMES are both empty — the roster gate "
            "fails CLOSED, so the bot will never @-mention anyone. Chases will still be "
            "posted, addressed by plain-text name. Set TEAM_ROSTER_IDS to the sales "
            "team's Discord ids to enable pings."
        )
    elif not TEAM_ROSTER_IDS:
        log.warning(
            "TEAM_ROSTER_IDS is empty — the roster is falling back to DISPLAY NAMES "
            "(%s), which are spoofable, and a name-only entry still cannot be pinged "
            "(a ping needs an id). Set the Discord ids.",
            ", ".join(sorted(TEAM_ROSTER_NAMES)),
        )

    if not NOTES_DIR:
        log.info(
            "NOTES_DIR is unset — the meeting-notes source will report "
            "awaiting-access, and the bot will say so when asked what it can do."
        )
    elif not NOTES_SYNC_CMD:
        log.info(
            "NOTES_SYNC_CMD is unset — nothing will refresh %r, so the bot reads "
            "whatever is already on disk and its notes go stale silently.", NOTES_DIR,
        )
    if NOTES_SYNC_MINUTES < 1:
        log.warning(
            "NOTES_SYNC_MINUTES=%s is below 1 — it will be clamped to 1 minute, which "
            "will hammer the sync. The default is 30.", NOTES_SYNC_MINUTES,
        )
    if not NOTES_EXCLUDE_TITLE_PATTERNS:
        # Not a mistake, but worth stating: the product standups will now be read
        # into context alongside the real meetings.
        log.info(
            "NOTES_EXCLUDE_TITLE_PATTERNS is empty — EVERY synced meeting note is "
            "loaded, including the AM/PM product standups. Set it to "
            "'AM sync,PM sync' to keep those out."
        )

    if COS_NUDGE_MAX_ATTEMPTS < 1:
        log.warning(
            "COS_NUDGE_MAX_ATTEMPTS=%s is below 1 — no chase would ever be sent. "
            "Set COS_FOLLOWUP_ENABLED=false if that is what you meant.",
            COS_NUDGE_MAX_ATTEMPTS,
        )
    if COS_NUDGE_WINDOW_HOURS < 1:
        log.warning(
            "COS_NUDGE_WINDOW_HOURS=%s is below 1 — the per-promise cooldown "
            "effectively disappears and the bot could chase the same promise every "
            "sweep. The default is 24.",
            COS_NUDGE_WINDOW_HOURS,
        )
    if not 0.0 <= COS_FOLLOWUP_MIN_CONFIDENCE <= 1.0:
        log.warning(
            "COS_FOLLOWUP_MIN_CONFIDENCE=%s is outside 0.0–1.0 — above 1 means nothing "
            "is ever chased, below 0 means everything is.",
            COS_FOLLOWUP_MIN_CONFIDENCE,
        )

    if not 0 <= STATE_DAILY_HOUR <= 23:
        log.warning("STATE_DAILY_HOUR=%s is outside 0–23; it will be clamped.", STATE_DAILY_HOUR)

    # -- GTM sheet --------------------------------------------------------
    # Not required: an unreachable sheet is a supported state (the source reports
    # awaiting-access and the bot says so). But it is worth being loud, because
    # the sheet is the bot's main source and silence looks like "no deals".
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        log.warning(
            "GOOGLE_SERVICE_ACCOUNT_JSON is unset — the GTM spreadsheet source will "
            "report awaiting-access and no sheet question can be answered. Point it at "
            "the service-account key file."
        )
    elif not os.path.isfile(GOOGLE_SERVICE_ACCOUNT_JSON):
        log.warning(
            "GOOGLE_SERVICE_ACCOUNT_JSON=%r does not exist — the spreadsheet source will "
            "report awaiting-access.",
            GOOGLE_SERVICE_ACCOUNT_JSON,
        )

    if SHEET_WRITES_ENABLED:
        log.warning(
            "SHEET_WRITES_ENABLED=true — the bot WRITES INTO THE REAL GTM Playbook, in "
            "the writable window %s of the Outreach PoCs tab. The restricted bands (%s) "
            "are refused in code and cannot be reached. Every write is echoed in "
            "channel, recorded in audit.jsonl, and undoable by anyone for %dh.",
            writable_window_label() or "(none — no window between the bands!)",
            RESTRICTED_COLUMN_RANGES, SHEET_WRITE_UNDO_HOURS,
        )
        if not writable_window_label():
            log.error(
                "...but RESTRICTED_COLUMN_RANGES=%r leaves NO writable window, so every "
                "write will be refused. Fix the bands or set SHEET_WRITES_ENABLED=false "
                "so the refusal is a decision rather than a surprise.",
                RESTRICTED_COLUMN_RANGES,
            )
    else:
        log.info(
            "SHEET_WRITES_ENABLED=false — the bot still reads, extracts and ECHOES what "
            "it would have written, and writes nothing to any sheet. SQLite state "
            "(deadlines, snoozes, reminders) is unaffected."
        )

    if SHEET_WRITE_UNDO_HOURS < 1:
        log.warning(
            "SHEET_WRITE_UNDO_HOURS=%s is below 1 — a write would be irreversible almost "
            "immediately. The whole basis for letting the bot touch the real sheet is "
            "that anyone who notices can put it back.",
            SHEET_WRITE_UNDO_HOURS,
        )
    if SHEET_WRITE_MAX_CELLS < 1:
        log.warning(
            "SHEET_WRITE_MAX_CELLS=%s is below 1 — no write will ever be applied.",
            SHEET_WRITE_MAX_CELLS,
        )
    if not TERMINAL_STATUS_WORDS:
        log.warning(
            "TERMINAL_STATUS_WORDS is empty — nothing gates a prospect status of Dead or "
            "Unresponsive, which STOPS a row for good. The bot would be able to end a "
            "row on an inference. Restore the list."
        )

    # The mapping sheet is read-only BY POLICY, so the one configuration that
    # could break that promise — pointing the playbook id at it — is caught here
    # and neutralised rather than warned about. A warning would be a note in a
    # log nobody reads while the bot wrote to a sheet it was told never to touch.
    if is_read_only_sheet_id(sheet_write_id()):
        log.error(
            "GTM_SHEET_ORIGINAL_ID resolves to GTM_MAPPING_SHEET_ID (%s), which is "
            "READ-ONLY by policy. Forcing SHEET_WRITES_ENABLED=false; the mapping sheet "
            "is never a write target.",
            GTM_MAPPING_SHEET_ID,
        )
        SHEET_WRITES_ENABLED = False

    if GTM_MAPPING_SHEET_ID and GTM_MAPPING_SHEET_ID == GTM_SHEET_ORIGINAL_ID:
        log.error(
            "GTM_MAPPING_SHEET_ID is the same id as the GTM Playbook. They are different "
            "spreadsheets with different rules — the mapping sheet is read-only and the "
            "playbook is not. Check both ids."
        )

    if not GTM_MAPPING_SHEET_ID:
        log.warning(
            "GTM_MAPPING_SHEET_ID is unset — the researcher/buyer mapping source will "
            "report awaiting-access, so no 'who do we pitch at X' question can be "
            "answered."
        )

    if SHEET_CACHE_SECONDS < 1:
        log.warning(
            "SHEET_CACHE_SECONDS=%s is below 1 — every question would re-read the sheet "
            "and you will hit the API quota. The default is 60.",
            SHEET_CACHE_SECONDS,
        )

    # -- deadline authority ------------------------------------------------
    for name, value in (
        ("OUTREACH_FOLLOWUP_DAYS", OUTREACH_FOLLOWUP_DAYS),
        ("REPLY_CHASE_DAYS", REPLY_CHASE_DAYS),
        ("MEETING_PREP_DAYS", MEETING_PREP_DAYS),
    ):
        if value < 1:
            log.warning(
                "%s=%s is below 1 working day — deadlines would land in the past the "
                "moment they are set.",
                name, value,
            )

    # -- the twelve rules ---------------------------------------------------
    # Nothing here is fatal: a rule that is quieter than intended is
    # recoverable. What IS worth a line at boot is a setting that makes a rule
    # UNREACHABLE — that failure is silent by nature, and the whole point of the
    # engine is that its output can be checked against the rules that made it.
    #
    # THE SCHEDULE ITSELF IS NOT VALIDATED HERE. Which day each rule runs, how
    # many items it may carry and where it goes live in bot_rules.yaml, and
    # rules.py validates that file and logs every rule at startup. Duplicating
    # the check would give two places to disagree about one answer.
    if NEXT_ACTION_ENABLED:
        log.info(
            "[config] the twelve rules are ON (computation only — the engine has no "
            "send path). rules_file=%s deliverables<=%dd prospects=%d co/week "
            "ask_at=%d li_no_dm=%dd dm_no_meeting=%dd prep=%s+day-of@%s "
            "followup=%dd/every %dd ladder=%s closure>%d%% new_company=+%dwd "
            "window=%dd weekend_shift=%s",
            BOT_RULES_FILE, DELIVERABLE_NEAR_DAYS, PROSPECT_COMPANIES_PER_WEEK,
            PROSPECT_REPEAT_ASK_AT, LI_NO_DM_DAYS, DM_NO_MEETING_DAYS,
            ",".join(MEETING_PREP_DAYS_BEFORE) or "(none)", MEETING_DAYOF_TIME,
            MEETING_FOLLOWUP_AFTER_DAYS, MEETING_FOLLOWUP_EVERY_DAYS,
            ">".join(MEETING_FOLLOWUP_LADDER) or "(empty)",
            CLOSURE_SUPPORT_MIN, NEW_COMPANY_AFTER_WORKING_DAYS,
            NEW_COMPANY_WINDOW_DAYS, NEXT_ACTION_WEEKEND_SHIFT,
        )
        if not MEETING_FOLLOWUP_LADDER:
            log.warning(
                "MEETING_FOLLOWUP_LADDER is empty, so R9 has no rungs and a finished "
                "meeting with no next steps is never chased at all. The plan's ladder "
                "is channel,dm,dm,escalation."
            )
        elif MEETING_FOLLOWUP_LADDER[0] != "channel":
            log.warning(
                "MEETING_FOLLOWUP_LADDER starts with %r, not 'channel'. R9's first ask "
                "is meant to be public — starting on a DM means the first anyone else "
                "hears of a stalled meeting is the escalation.",
                MEETING_FOLLOWUP_LADDER[0],
            )
        if not 0 <= CLOSURE_SUPPORT_MIN <= 99:
            log.warning(
                "CLOSURE_SUPPORT_MIN=%d is outside 0-99, so R10 selects either every "
                "deal or none of them. The plan's number is 50, and exactly 50 is "
                "excluded because the rule says ABOVE 50.",
                CLOSURE_SUPPORT_MIN,
            )
        if not CLOSURE_SUPPORT_STAGES:
            log.warning(
                "CLOSURE_SUPPORT_STAGES is empty — R10 matches no prospect status and "
                "closure support never fires, however high a closure probability gets."
            )
        if PROSPECT_REPEAT_ASK_AT < 2:
            log.warning(
                "PROSPECT_REPEAT_ASK_AT=%d — R5 would ask whether to skip a contact "
                "the first or second time it names them, which reads as a bot giving "
                "up immediately. The plan's number is 3.",
                PROSPECT_REPEAT_ASK_AT,
            )
        if not DELIVERABLE_P1_MARKERS:
            log.warning(
                "DELIVERABLE_P1_MARKERS is empty, so R4 recognises no item as P1 and "
                "the deliverables chase never fires."
            )
        bad_prep = [d for d in MEETING_PREP_DAYS_BEFORE if not str(d).strip().isdigit()]
        if bad_prep:
            log.warning(
                "MEETING_PREP_DAYS_BEFORE contains %s, which is not a whole number of "
                "days; those entries are ignored and R8 fires on the rest.",
                ", ".join(repr(d) for d in bad_prep),
            )
        if not NEXT_ACTION_WEEKEND_SHIFT:
            log.warning(
                "NEXT_ACTION_WEEKEND_SHIFT=false — due dates may land on a Saturday or a "
                "Sunday. They will be read on the Monday anyway, two days late."
            )
        if not CLOSURE_STOP_MARKERS:
            log.warning(
                "CLOSURE_STOP_MARKERS is empty — only a closure of exactly 0%% will stop "
                "a row, so Won, Lost, Dead and Unresponsive rows keep producing work "
                "forever. That is how a queue stops being trusted."
            )
        if not NEXT_ACTION_WEEKEND_SHIFT:
            log.warning(
                "NEXT_ACTION_WEEKEND_SHIFT=false — due dates may land on a Saturday or a "
                "Sunday. They will be read on the Monday anyway, two days late."
            )
    else:
        log.warning(
            "NEXT_ACTION_ENABLED=false — no next-action queue is computed at all. "
            "'cadence preview' will say so rather than returning an empty list, which "
            "would read as 'nothing to do' when the truth is 'I did not look'."
        )

    # A deadline nobody is told about isn't authority, it's bookkeeping.
    unpingable = [uid for uid in DEADLINE_NOTIFY_IDS if uid not in TEAM_ROSTER_IDS]
    if DEADLINE_NOTIFY_IDS and unpingable:
        log.warning(
            "DEADLINE_NOTIFY_IDS contains %s, who are not in TEAM_ROSTER_IDS — the roster "
            "is the only thing that authorises a mention, so they will be named in plain "
            "text rather than pinged. Add them to TEAM_ROSTER_IDS.",
            ", ".join(str(u) for u in unpingable),
        )
    elif not DEADLINE_NOTIFY_IDS:
        log.warning(
            "DEADLINE_NOTIFY_IDS is empty — the bot will still announce every deadline it "
            "sets in-channel, but nobody is tagged to object to it. Set it to Kushal's and "
            "Vaishnavi's Discord ids."
        )

    # -- who every channel message tags ------------------------------------
    if not SALES_ALWAYS_TAG_IDS:
        log.warning(
            "SALES_ALWAYS_TAG_IDS is empty — proactive channel messages will tag nobody "
            "by default. The strategy doc says every channel message tags both Vaishnavi "
            "and Sid; set it to their two Discord ids."
        )
    else:
        off_roster = [u for u in SALES_ALWAYS_TAG_IDS if u not in TEAM_ROSTER_IDS]
        if off_roster:
            log.error(
                "SALES_ALWAYS_TAG_IDS contains %s, who are NOT in TEAM_ROSTER_IDS. The "
                "roster is the only thing that authorises a mention, so they will be "
                "named in plain text and NOT pinged — which looks identical in the "
                "message and is not the same thing. Add them to TEAM_ROSTER_IDS.",
                ", ".join(str(u) for u in off_roster),
            )
        log.info(
            "[config] every proactive channel message opens by tagging %d person(s): %s",
            len(always_tag_ids()),
            ", ".join(str(u) for u in always_tag_ids()) or "(none — all off-roster)",
        )

    # -- the narrow DM exception -------------------------------------------
    if not SALES_DMS_ENABLED:
        log.info(
            "[config] SALES_DMS_ENABLED=false — the DM ban is fully in force. R9's later "
            "rungs post in the channel as ordinary follow-ups and the %d-day overdue "
            "escalation posts in the channel addressed to %s. Nothing is dropped, and "
            "no message mentions the fallback — it is logged as [dm] "
            "fallback-to-channel.", DM_OVERDUE_DAYS, ESCALATION_ADDRESSEE,
        )
    else:
        log.warning(
            "[config] SALES_DMS_ENABLED=true — DMs are permitted, NARROWLY: only to "
            "TEAM_ROSTER_IDS members, only for items %d+ days overdue and for R9 rungs "
            "%s, and never for an item already posted in the channel the same day. Every "
            "DM is written to state/audit.jsonl with its reason.",
            DM_OVERDUE_DAYS,
            ", ".join(str(r) for r in DM_MEETING_FOLLOWUP_RUNGS) or "(none)",
        )
        if DM_SAME_DAY_AS_CHANNEL:
            log.error(
                "DM_SAME_DAY_AS_CHANNEL=true — the same item can go to the channel AND "
                "arrive as a DM on the same day. That reads as the bot asking twice and "
                "is the thing the rule exists to stop. Set it false."
            )

    # -- the leave channel ---------------------------------------------------
    if not HOLIDAY_CHANNEL_ID:
        log.warning(
            "HOLIDAY_CHANNEL_ID is unset — the bot cannot check whether an owner is on "
            "leave and will address everybody as though they are in. A nudge to somebody "
            "on holiday is noise they come back to a week later."
        )
    elif HOLIDAY_CHANNEL_ID in SALES_CHANNEL_IDS:
        log.warning(
            "HOLIDAY_CHANNEL_ID=%s is ALSO in SALES_CHANNEL_IDS, so the bot can post "
            "there. It has no reason to, and the leave channel was meant to be read-only "
            "— remove it from SALES_CHANNEL_IDS.", HOLIDAY_CHANNEL_ID,
        )
    else:
        log.info(
            "[config] leave channel %s is READ-ONLY: it is the one non-sales channel "
            "may_read() admits, and send() still refuses it. Fallback order: %s (the "
            "last never counts as on leave).",
            HOLIDAY_CHANNEL_ID, " -> ".join(LEAVE_FALLBACK_ORDER) or "(none)",
        )

    # -- web search ----------------------------------------------------------
    if not WEB_SEARCH_ENABLED:
        log.info(
            "[config] WEB_SEARCH_ENABLED=false — R1, R2, R3, R6, R8, R10 and R11 will "
            "still produce their items and will say 'web research unavailable today'. "
            "Nothing is dropped."
        )
    else:
        log.info(
            "[config] web search ON: tool=%s, max %d search(es) per call, %d a day. "
            "Web content is DATA, never instructions — websearch.SAFETY_PREAMBLE says "
            "so on every call and nothing acts on a page without a human's yes.",
            WEB_SEARCH_TOOL_TYPE, WEB_SEARCH_MAX_USES, WEB_SEARCH_DAILY_BUDGET,
        )
        if WEB_SEARCH_ALLOWED_DOMAINS and WEB_SEARCH_BLOCKED_DOMAINS:
            log.warning(
                "Both WEB_SEARCH_ALLOWED_DOMAINS and WEB_SEARCH_BLOCKED_DOMAINS are "
                "set. The API rejects a request carrying both, so only the allow-list "
                "is sent — the block-list is redundant when an allow-list exists."
            )
        if WEB_SEARCH_ALLOWED_DOMAINS:
            log.warning(
                "WEB_SEARCH_ALLOWED_DOMAINS=%s — R1's news sweep will only ever read "
                "those sites, which is not a news sweep. Leave it empty unless you "
                "mean it.", ", ".join(WEB_SEARCH_ALLOWED_DOMAINS),
            )
        if WEB_SEARCH_DAILY_BUDGET < WEB_SEARCH_MAX_USES:
            log.warning(
                "WEB_SEARCH_DAILY_BUDGET=%d is below WEB_SEARCH_MAX_USES=%d, so a "
                "single call can exhaust the whole day. Raise the budget or lower the "
                "per-call cap.", WEB_SEARCH_DAILY_BUDGET, WEB_SEARCH_MAX_USES,
            )
        if WEB_SEARCH_RESPONSE_INCLUSION and not WEB_SEARCH_TOOL_TYPE >= "web_search_20260318":
            log.info(
                "[config] WEB_SEARCH_RESPONSE_INCLUSION is ignored on %s — it needs "
                "web_search_20260318 or later.", WEB_SEARCH_TOOL_TYPE,
            )

    # -- permission before every write --------------------------------------
    if not SALES_APPROVER_IDS:
        log.error(
            "SALES_APPROVER_IDS is empty — NOBODY can approve a sheet write, so every "
            "proposal the bot makes will expire unanswered and no cell will ever "
            "change. Set it to Sid's and Vaishnavi's Discord ids."
        )
    else:
        off_roster = [u for u in SALES_APPROVER_IDS if u not in TEAM_ROSTER_IDS]
        if off_roster:
            log.error(
                "SALES_APPROVER_IDS contains %s, who are not in TEAM_ROSTER_IDS. An "
                "approver the bot cannot @-mention is an approval flow that stalls "
                "silently — they are ignored. Add them to TEAM_ROSTER_IDS.",
                ", ".join(str(u) for u in off_roster),
            )
        log.info(
            "[config] %d approver(s) may say yes to a write: %s. Nothing is written "
            "from a reply without one.",
            len(approver_ids()),
            ", ".join(str(u) for u in approver_ids()) or "(none — all off-roster)",
        )
    if SALES_FINAL_SAY_ID and SALES_FINAL_SAY_ID not in set(approver_ids()):
        log.warning(
            "SALES_FINAL_SAY_ID=%s is not among the approvers, so the tie-break can "
            "never fire: that person's answer is not counted at all. Add them to "
            "SALES_APPROVER_IDS.", SALES_FINAL_SAY_ID,
        )
    elif not SALES_FINAL_SAY_ID:
        log.warning(
            "SALES_FINAL_SAY_ID is unset — when two approvers disagree the FIRST answer "
            "decides. The strategy doc says Sid's word is final; set it to his id."
        )
    if SHEET_ROW_ADDITIONS_ENABLED:
        log.info(
            "[config] row additions ON for %s. A new Outreach PoCs row may have its "
            "identity columns (%s) filled by the bot; an EXISTING row's A:I and S:X "
            "stay locked.",
            ", ".join(SHEET_APPENDABLE_TABS) or "(no tabs — nothing can be appended)",
            NEW_ROW_WRITABLE_RANGES,
        )
        overlap = new_row_writable_indexes() & frozenset(
            i for lo, hi in parse_column_ranges("S:X") for i in range(lo, hi + 1)
        )
        if overlap:
            log.warning(
                "NEW_ROW_WRITABLE_RANGES=%r reaches into the commercial block (S:X). A "
                "bot that has just discovered a company has no business stating its "
                "closure probability. The default is A:R.", NEW_ROW_WRITABLE_RANGES,
            )
    else:
        log.info(
            "[config] SHEET_ROW_ADDITIONS_ENABLED=false — R2, R3 and R11 will still "
            "propose new rows and will say they cannot append them."
        )

    # -- focus commands ------------------------------------------------------
    if FOCUS_DEFAULT_DAYS > FOCUS_MAX_DAYS:
        log.warning(
            "FOCUS_DEFAULT_DAYS=%d is above FOCUS_MAX_DAYS=%d, so every focus set "
            "without an explicit duration would be refused. Lower the default.",
            FOCUS_DEFAULT_DAYS, FOCUS_MAX_DAYS,
        )
    if not FOCUS_MATCH_ROLES:
        log.warning(
            "FOCUS_MATCH_ROLES is empty — a focus would match no column and R5 would "
            "always report 'nothing matches' and fall back to sheet order."
        )

    if ESCALATE_TO_ID and ESCALATE_TO_ID not in TEAM_ROSTER_IDS:
        log.warning(
            "ESCALATE_TO_ID=%s is not in TEAM_ROSTER_IDS — an escalated chase will name "
            "them without pinging them. Add them to the roster.",
            ESCALATE_TO_ID,
        )
    elif not ESCALATE_TO_ID:
        log.info(
            "ESCALATE_TO_ID is unset — an unanswered chase will be flagged in-channel "
            "rather than escalated to a named person."
        )

    # -- the drip ----------------------------------------------------------
    if SALES_DIGEST_ENABLED:
        hour, minute = drip_start_ist()
        channel = digest_channel_id()
        if not channel:
            log.warning(
                "SALES_DIGEST_ENABLED is on but there is no sales channel to send the "
                "drip in — it will be skipped, and since the drip is the ONLY proactive "
                "path this bot has, nothing unprompted will ever go out. Set "
                "SALES_CHANNEL_IDS (and optionally SALES_DIGEST_CHANNEL_ID)."
            )
        else:
            log.info(
                "Drip: up to %d message(s) per weekday in channel %s, first at %02d:%02d "
                "IST, then gaps of %d+/-%d min. One message per (action type x owner). "
                "Replies and ask-time deadline announcements are IMMEDIATE and are not "
                "spaced or capped.",
                DAILY_MESSAGE_CAP, channel, hour, minute,
                MESSAGE_GAP_MINUTES, MESSAGE_JITTER_MINUTES,
            )
        log.info(
            "[config] per-day caps: %s (anything unnamed falls back to "
            "DAILY_MESSAGE_CAP=%d). Saturday is silent; Sunday sends at most one post "
            "and only for rule(s) %s.",
            ", ".join(
                "%s=%d" % (n, DAILY_MESSAGE_CAPS[i])
                for i, n in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
                if i in DAILY_MESSAGE_CAPS
            ) or "(none parsed)",
            DAILY_MESSAGE_CAP,
            ", ".join(SUNDAY_RULE_IDS) or "(none — Sunday is silent too)",
        )
        if 5 in DAILY_MESSAGE_CAPS and DAILY_MESSAGE_CAPS[5]:
            log.warning(
                "DAILY_MESSAGE_CAP_BY_DAY gives Saturday a cap of %d, but Saturday is "
                "silent and is refused before any cap is consulted. That entry does "
                "nothing.", DAILY_MESSAGE_CAPS[5],
            )
        if DRIP_MAX_ITEMS_PER_POST < 1:
            log.warning(
                "DRIP_MAX_ITEMS_PER_POST=%d — no post could carry an item, so every "
                "rule would roll its whole output forever. The plan's number is 5.",
                DRIP_MAX_ITEMS_PER_POST,
            )
        if DAILY_MESSAGE_CAP < 1:
            log.warning(
                "DAILY_MESSAGE_CAP=%s is below 1 — the bot will never send anything "
                "unprompted. If that is what you want, SALES_DIGEST_ENABLED=false says "
                "it plainly and logs one line a day to prove it.",
                DAILY_MESSAGE_CAP,
            )
        elif DAILY_MESSAGE_CAP > 6:
            log.warning(
                "DAILY_MESSAGE_CAP=%s is above 6. The whole point of the volume contract "
                "is that a busy channel absorbs a few short messages and starts skimming "
                "past more. The agreed number is 3.",
                DAILY_MESSAGE_CAP,
            )
        if MESSAGE_GAP_MINUTES - MESSAGE_JITTER_MINUTES < 15:
            log.warning(
                "MESSAGE_GAP_MINUTES=%d minus MESSAGE_JITTER_MINUTES=%d leaves a floor of "
                "%d minutes between messages. Below about 15 the drip stops being a drip "
                "and becomes one long message delivered in pieces.",
                MESSAGE_GAP_MINUTES, MESSAGE_JITTER_MINUTES,
                MESSAGE_GAP_MINUTES - MESSAGE_JITTER_MINUTES,
            )
        if MESSAGE_JITTER_MINUTES < 0:
            log.warning(
                "MESSAGE_JITTER_MINUTES=%s is negative; it is used as a +/- spread, so "
                "the sign is ignored. Set 0 for exact spacing.",
                MESSAGE_JITTER_MINUTES,
            )
        if DRIP_REASK_DAYS < 1:
            log.warning(
                "DRIP_REASK_DAYS=%s is below 1, so a group would be re-asked the same "
                "day it was nudged. Two asks is a reminder; two asks in one day is "
                "nagging.",
                DRIP_REASK_DAYS,
            )
        if not DRIP_WEEKDAYS_ONLY:
            log.info(
                "DRIP_WEEKDAYS_ONLY=false — the drip will send at weekends too. A nudge "
                "that lands on a Saturday is read on Monday anyway, having spent the "
                "weekend as an unread badge."
            )
        if not DRIP_LLM_COMPOSE:
            log.info(
                "DRIP_LLM_COMPOSE=false — messages use the deterministic templates "
                "rather than the voice exemplars in sales_policy.md. Still one sentence, "
                "still with an out, just less varied."
            )
        if COS_FOLLOWUP_CHECK_INTERVAL_MINUTES > 60:
            log.warning(
                "COS_FOLLOWUP_CHECK_INTERVAL_MINUTES=%s is over an hour — a drip message "
                "can only go out on a sweep tick, so it may land up to that late and the "
                "spacing will drift.",
                COS_FOLLOWUP_CHECK_INTERVAL_MINUTES,
            )
    else:
        log.warning(
            "SALES_DIGEST_ENABLED=false — the bot will send NO unprompted messages at "
            "all: no drip message, no re-ask, nothing. The name is unchanged on purpose; "
            "the drip inherits this switch rather than adding one of its own, so an "
            "operator who turned the bot off does not have to learn a new variable to "
            "keep it off. It STILL tracks deadlines, computes the next-action queue and "
            "writes SQLite and audit.jsonl, still answers when @-mentioned, and still "
            "announces a deadline when someone asks for one. Each suppressed day logs "
            "one line: '[digest] suppressed — SALES_DIGEST_ENABLED=false'. Setting it "
            "back to true resumes at the next scheduled slot; the skipped days are not "
            "replayed."
        )

    if WEEKLY_DIGEST_ENABLED and not 0 <= WEEKLY_DIGEST_WEEKDAY <= 6:
        log.warning(
            "WEEKLY_DIGEST_WEEKDAY=%s is outside 0–6 (0=Monday); it will be clamped to "
            "Friday. It now selects which day's DAILY digest carries the funnel block.",
            WEEKLY_DIGEST_WEEKDAY,
        )

    # -- the tracker reminder ----------------------------------------------
    # It is a SECTION of the digest, so it cannot fire on a day the digest
    # doesn't. Saying so here is the difference between "the reminder is broken"
    # and "the digest is off".
    if TRACKER_REMINDER_ENABLED:
        days = sorted(tracker_reminder_weekdays())
        names = ", ".join(
            ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[d] for d in days
        ) or "(no day)"
        if not days:
            log.warning(
                "TRACKER_REMINDER_DAYS=%r resolved to no weekday — the tracker reminder "
                "will never appear.", TRACKER_REMINDER_DAYS,
            )
        elif not SALES_DIGEST_ENABLED:
            log.warning(
                "TRACKER_REMINDER_ENABLED is on (%s) but SALES_DIGEST_ENABLED is off. The "
                "reminder is a SECTION of the daily digest and has no send path of its "
                "own, so it will not go out while the switch is off. It is still built "
                "on those days, so it appears the moment the switch goes back on and in "
                "`--dry-run-digest` meanwhile.", names,
            )
        else:
            log.info("Tracker reminder: a section of the %s digest.", names)

    # -- the to-do sheet ---------------------------------------------------
    if TODO_SHEET_ENABLED:
        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            log.warning(
                "TODO_SHEET_ENABLED is on but GOOGLE_SERVICE_ACCOUNT_JSON is unset — the "
                "bot cannot create or read the to-do sheet without the service account."
            )
        if not TEAM_SHARE_EMAILS:
            log.warning(
                "TEAM_SHARE_EMAILS is empty. A spreadsheet the service account creates is "
                "INVISIBLE to every human until it is shared, so the to-do sheet would "
                "exist and nobody could open it. Set the team's addresses."
            )
        else:
            log.info(
                "To-do sheet %r: shared as Editor with %s; refreshed from the meeting "
                "notes on %s.",
                TODO_SHEET_TITLE, ", ".join(TEAM_SHARE_EMAILS), TODO_REFRESH_DAY,
            )
        if TODO_SHEET_ID:
            log.info(
                "TODO_SHEET_ID is set, so the bot will use that sheet and never create "
                "one of its own."
            )

    # -- the strategy doc --------------------------------------------------
    if not (STRATEGY_DOC_ID or STRATEGY_DOC_FILE):
        log.warning(
            "Neither STRATEGY_DOC_ID nor STRATEGY_DOC_FILE is set — the bot cannot check "
            "outreach against the plan, cannot tell you how current the plan is, and "
            "every deadline falls back to the working-day defaults instead of the "
            "cadence the strategy doc states."
        )
    elif STRATEGY_DOC_FILE and not os.path.exists(STRATEGY_DOC_FILE):
        # THE CORE BRAIN IS MISSING. Loud, because the failure is otherwise
        # invisible: every prompt still gets built, the bot still answers, and
        # it answers without the document that is supposed to be deciding what
        # it says. It will TELL people the strategy is missing rather than
        # inventing one (see persona.strategy_preamble), but nobody reads a
        # prompt — they read this log line.
        log.error(
            "STRATEGY_DOC_FILE=%r does not exist. That file is the bot's CORE BRAIN: it "
            "is loaded into the system prompt of every model call. Without it the bot "
            "runs on the persona and sales_policy.md alone, and it will say so when "
            "asked. Create it at the repo root, or point STRATEGY_DOC_FILE at the copy.",
            STRATEGY_DOC_FILE,
        )
    elif STRATEGY_DOC_ID and not GOOGLE_SERVICE_ACCOUNT_JSON:
        log.warning(
            "STRATEGY_DOC_ID is set but GOOGLE_SERVICE_ACCOUNT_JSON is not — the doc is "
            "read over the Drive API with the service account, so it will stay "
            "unreadable."
        )

    return missing

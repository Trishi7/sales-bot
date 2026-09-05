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

SALES_CHANNEL_ID_SET: set[int] = set(SALES_CHANNEL_IDS)


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

# The state contract for the future COSA supervisor: a rewritten-at-startup-and-
# daily snapshot, plus an append-only action log. Schemas are documented in
# README.md so a third process can consume them.
STATE_DIR = (os.getenv("STATE_DIR", "./state") or "./state").strip()

# -- Persona ------------------------------------------------------------------

# The bot's name — the one identity it uses everywhere it speaks. Naming is open;
# "SalesCoS" is only the default.
COS_NAME = (os.getenv("COS_NAME", "") or "").strip() or "SalesCoS"

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
# (GOOGLE_SERVICE_ACCOUNT_JSON, GTM_SHEET_*_ID, SHEET_WRITE_TARGET).
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
# see drive.py's scope list. STRATEGY_DOC_FILE is the local-copy fallback.
STRATEGY_DOC_ID = (os.getenv("STRATEGY_DOC_ID", "") or "").strip()
STRATEGY_DOC_FILE = (os.getenv("STRATEGY_DOC_FILE", "") or "").strip()

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

# Path to the service-account key file. This grants read (and, on the sandbox,
# write) access to the sheets, so it is a SECRET: .gitignore covers the usual key
# filenames, and it must never be committed. Empty → the spreadsheet source
# reports awaiting-access and the bot says so when asked.
GOOGLE_SERVICE_ACCOUNT_JSON = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "") or "").strip()

# The ORIGINAL "NFThing <> GTM Playbook" — the READ-ONLY source of truth. The bot
# never writes here unless SHEET_WRITE_TARGET is explicitly set to "original".
GTM_SHEET_ORIGINAL_ID = (
    os.getenv("GTM_SHEET_ORIGINAL_ID", "") or ""
).strip() or "15fhmQAOVoABg2TpxSRL57LhC_I-62hZZAspl1Dqd_fs"

# The SANDBOX copy — the bot's writable mirror, and the default write target.
GTM_SHEET_COPY_ID = (
    os.getenv("GTM_SHEET_COPY_ID", "") or ""
).strip() or "1yYA38hq6iKguFg2giiyn66AYpXoXfaNiTrKB1Yn2W1A"

# Where writes go: "copy" (default, the sandbox), "original" (the real sheet —
# deliberate and rarely right), or "off" (read-only; deadlines still live in
# SQLite and are still announced, they just aren't mirrored to a sheet).
SHEET_WRITE_TARGET = (os.getenv("SHEET_WRITE_TARGET", "") or "copy").strip().lower()
if SHEET_WRITE_TARGET not in ("copy", "original", "off"):
    log.warning(
        "SHEET_WRITE_TARGET=%r is not copy|original|off — falling back to 'copy' "
        "(the sandbox), which is the safe default.",
        SHEET_WRITE_TARGET,
    )
    SHEET_WRITE_TARGET = "copy"


def sheet_write_id() -> str:
    """The spreadsheet id writes go to, or "" when writing is off. The ONLY
    place the write target is resolved, so a caller can't accidentally aim a
    write at the original."""
    if SHEET_WRITE_TARGET == "off":
        return ""
    return GTM_SHEET_ORIGINAL_ID if SHEET_WRITE_TARGET == "original" else GTM_SHEET_COPY_ID


# The ONE column the bot may write, appended at the far right of the tracker tab.
# Cell-level writes into this column only — never a row, never another column.
BOT_DEADLINE_COLUMN = (
    os.getenv("BOT_DEADLINE_COLUMN", "") or ""
).strip() or "Next Deadline (bot)"

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
    even if someone points GTM_SHEET_COPY_ID at the mapping sheet by mistake.
    """
    return bool(sheet_id) and str(sheet_id).strip() in read_only_sheet_ids()


# Optional override for header→role mapping in the MAPPING sheet, same shape and
# same purpose as GTM_COLUMN_MAP but keyed by the mapping tab kinds
# (mapping_legend / researcher_mapping / org_coverage / edge_map).
GTM_MAPPING_COLUMN_MAP: dict = _json_object("GTM_MAPPING_COLUMN_MAP", default={})

# Optional override for header→role mapping when a tab's wording is ambiguous.
# Headers are discovered dynamically at parse time (tabs will evolve), so this is
# only needed when auto-detection picks wrong. Shape:
#   {"outreach_tracker": {"company": "Client Name", "poc": "Point of Contact"}}
# Values are the literal header text in the sheet; keys are the roles in
# gtm_sheet.ROLES.
GTM_COLUMN_MAP: dict = _json_object("GTM_COLUMN_MAP", default={})

# -- TAB IDENTIFICATION: BY HEADER SIGNATURE ----------------------------------
# The GTM Playbook's tabs are found by the COLUMNS they carry, not by their
# names — names drift, signatures don't. gtm_sheet.py holds the signatures; the
# tab NAME that matched each role is logged at startup under [gtm.roles].
#
#   TRACKER   "Last followed up date" + "Total follow-ups till date"
#             (live: the HIDDEN "Outreach Updates" tab). THE CADENCE SOURCE —
#             the only tab with dates in it, so every phase-1 rule runs there.
#   MASTER    "Response Status" + "Intro Sent" + "Meeting Done"
#             (live: "Master Data"). STATUS ONLY, no dates: aggregate answers,
#             the weekly funnel definition, and the nightly cross-check.
#   PIPELINE  "Lead Stage" + "Estimated Value (INR)"  (live: "Lead Master Sheet")
#   FUNNEL    "Vertical / Stage"   (live: "Sales Funnel - March-June 2026")
#   RESEARCHER LINES  "Outreach Line - Researchers" + "Dates"
#             (live: "Master Pipeline")
#
# RE-POINTING THE BOT AT A NEW SHEET IS AN ENV CHANGE PLUS A RESTART, and this
# is the promise made in the 27 Aug alignment meeting. Nothing about the sheet's
# identity or its column names is compiled in:
#   GTM_SHEET_ORIGINAL_ID     which spreadsheet
#   GTM_COLUMN_MAP            which header means which rule field
#   GTM_MASTER_TAB_TITLES     an optional NAME hint for the master tab
#   SALES_DEFAULT_OWNER_ID    who cadence items are addressed to
#   the CADENCE_* thresholds below
# Change those, restart, and the bot runs the same rules against the new sheet.
# There is no migration and no code edit.

# An optional NAME hint for the master tab, comma-separated, matched case- and
# punctuation-insensitively.
#
# IT IS A HINT, NOT AN AUTHORITY, and that is a deliberate downgrade. It used to
# decide which tab was the master outright, which put the entire cadence on a
# status-only tab with no dates in it — every date rule read a blank and quietly
# never fired. A tab named here is still only read as the master if it carries
# the master signature, and naming a tab here can never take the cadence off the
# tracker.
GTM_MASTER_TAB_TITLES: list[str] = _str_list(
    "GTM_MASTER_TAB_TITLES", "Master data,Master Data,Master-data,Masterdata"
)

# Read tabs the spreadsheet marks HIDDEN. The master tab is fed by a hidden
# "outreach updates" sheet, and gspread returns hidden worksheets like any other
# — this flag exists so discovery can be narrowed if a hidden tab ever causes
# trouble, not because hidden means private. Hidden tabs are logged as hidden.
GTM_READ_HIDDEN_TABS = _bool("GTM_READ_HIDDEN_TABS", default=True)

# Log the FULL discovered schema — every tab, every header, hidden or not — once
# per spreadsheet at startup. This is how a column rename is diagnosed in one
# log read instead of a debugging session, so it defaults ON.
GTM_LOG_FULL_SCHEMA = _bool("GTM_LOG_FULL_SCHEMA", default=True)

# A mapped master column that is empty on nearly every row is the signature of a
# status conveyed by CELL COLOUR rather than by text. The bot reads VALUES ONLY
# — it cannot see fills — so it warns at startup naming the column instead of
# silently treating every row as blank.
# The warning fires when a tab has at least MIN_ROWS rows and the column is
# filled on no more than RATIO of them.
CADENCE_EMPTY_COLUMN_MIN_ROWS = _int("CADENCE_EMPTY_COLUMN_MIN_ROWS", 10)
CADENCE_EMPTY_COLUMN_RATIO = _float("CADENCE_EMPTY_COLUMN_RATIO", 0.05)

# -- PHASE 1 CADENCE RULES ----------------------------------------------------
# The nine daily rules from Vaishnavi's "Steps for Sales Bot" doc, computed
# against the master tab and fed into the EXISTING once-daily digest. No rule
# has a send path of its own; nothing here can make the bot speak twice.
#
# EVERY "n" BELOW IS A PLACEHOLDER. Vaishnavi's numbers were left unset in the
# doc and will be tuned once the digest has been read for a week. They are env
# vars precisely so that tuning is a restart, not a deploy.

# Master switch for the whole cadence block. Off = the five cadence sections are
# simply absent from the digest; nothing else changes.
CADENCE_ENABLED = _bool("CADENCE_ENABLED", default=True)

# (a) Last Followed-up Date older than this, with no response → chase the owner.
FOLLOWUP_STALE_DAYS = _int("FOLLOWUP_STALE_DAYS", 5)
# (c) First Contacted set but never Connected after this long → start interacting.
CONNECT_REMINDER_DAYS = _int("CONNECT_REMINDER_DAYS", 7)
# ...AND NOT AFTER THIS LONG. Past this, the row is COLD, not "yet to start".
#
# THIS IS THE CEILING RULE (c) NEEDED AND DID NOT HAVE. On the live sheet rule
# (c) fired on 756 of 886 rows, because most of the tracker is March-June
# outreach that never connected. Raising CONNECT_REMINDER_DAYS does not help at
# all — those rows are older than ANY threshold, so a higher floor still lets
# every one of them through. A ceiling is the only lever that works.
#
# Rows past it are excluded from the individual "start interacting" chase and
# reported as ONE summary line instead ("N cold contacts (never connected, first
# contacted Mar-Jun) — ask 'cold list' to see them"). The list itself is
# available uncapped on demand. Nothing is hidden; it is counted rather than
# enumerated, because 756 identical nudges is not a work list.
#
# Set to 0 to turn the ceiling off and go back to chasing every one of them.
CONNECT_REMINDER_MAX_DAYS = _int("CONNECT_REMINDER_MAX_DAYS", 30)
# (d) Total follow-ups at or above this with no response → try another channel.
ALT_CHANNEL_AT = _int("ALT_CHANNEL_AT", 4)
# (e) Total follow-ups at or above this with no response → ask the OWNER to mark
# the PoC unresponsive in the sheet. The bot never writes that itself: its only
# writable cell anywhere remains its own BOT_DEADLINE_COLUMN.
UNRESPONSIVE_AT = _int("UNRESPONSIVE_AT", 7)
# (i) Next Steps present but unchanged for this many days → chase.
NEXTSTEP_STALL_DAYS = _int("NEXTSTEP_STALL_DAYS", 10)

# Cell values that mark a row REJECTED. A rejected row is excluded from every
# rule and every digest section — never chased, revisited offline by humans.
# Matched as whole phrases against the response, reason, status, next-steps and
# notes cells, case-insensitively.
#
# "Response = N" is NOT a rejection: rule (g) exists precisely to suggest another
# PoC at a company whose first contact said no. Rejection has to be written down
# deliberately.
CADENCE_REJECTED_MARKERS: list[str] = _str_list(
    "CADENCE_REJECTED_MARKERS",
    "rejected,not interested,disqualified,do not contact,dnc,closed lost,"
    "lost,dropped,drop,blacklist,blacklisted",
)

# WHO A CADENCE ITEM IS ADDRESSED TO.
#
# THE TRACKER HAS NO OWNER COLUMN. Every row is worked by the same person today,
# so a cadence line resolves its owner in this order:
#   1. the row's own owner cell, if a column ever appears (or GTM_COLUMN_MAP
#      names one: {"outreach_tracker": {"owner": "Owned By"}}), resolved against
#      the roster by display name;
#   2. SALES_DEFAULT_OWNER_ID — Vaishnavi's Discord id.
# The id must ALSO be in TEAM_ROSTER_IDS to actually be @-mentioned; the roster
# is the only thing that authorises a mention, and an id that isn't on it is
# named in plain text instead. Unset means cadence lines are addressed to the
# DEADLINE_NOTIFY_IDS group line, which is worse but never wrong.
SALES_DEFAULT_OWNER_ID = _int("SALES_DEFAULT_OWNER_ID", 0)

# How many UPDATE-TRACKER fill-in asks the digest carries. SEPARATE from
# DIGEST_MAX_ITEMS on purpose: the tracker is sparse (follow-up counts are blank
# on most rows), so a row whose rule inputs are missing becomes a "please fill
# this in" ask rather than a chase — and without its own budget those asks would
# consume the whole 15-item cadence and push the real work off the digest.
UPDATE_TRACKER_MAX = _int("UPDATE_TRACKER_MAX", 5)

# THE NIGHTLY CONSISTENCY CROSS-CHECK. The master tab and the tracker describe
# the same rows in two vocabularies; where they disagree, one of them is wrong
# and a human has to say which. The bot reports the disagreement as an
# UPDATE-TRACKER line and NEVER infers a winner — picking one silently is how a
# status gets quietly rewritten by a bot nobody asked.
CADENCE_CROSSCHECK_ENABLED = _bool("CADENCE_CROSSCHECK_ENABLED", default=True)
# How many disagreements one digest reports. They are a fill-in ask like any
# other and share the UPDATE_TRACKER_MAX budget; this caps how many are computed
# into the list at all.
CADENCE_CROSSCHECK_MAX = _int("CADENCE_CROSSCHECK_MAX", 5)

# THE DATA-QUALITY FLAGS: broken formulas (#REF! and friends), master rows whose
# cells come from the wrong vocabulary, and response values nobody standardised.
# One UPDATE-TRACKER line each, and DEDUPED UNTIL FIXED — a flag whose signature
# hasn't changed since the last time it was reported is not repeated, because a
# daily reminder of a known-broken formula is how a digest gets muted.
CADENCE_DATA_QUALITY_ENABLED = _bool("CADENCE_DATA_QUALITY_ENABLED", default=True)

# THE URGENT ITEMS ARE NEVER TRUNCATED, and they have their own ceiling.
#
# A positive reply with no next step (f), a meeting inside MEETING_PREP_DAYS (h)
# and a post-meeting gap (i) are exactly what Vaishnavi prioritised. Sharing one
# fifteen-item budget with the rest of the cadence, sixteen urgent rows filled
# the whole digest on day one and then — one row later — would have started
# truncating the positives themselves. Truncating the positives defeats the
# digest, so they get their own budget and DIGEST_MAX_ITEMS applies to
# everything else.
#
# THIS IS A HARD CEILING, NOT A TARGET. It exists only so a broken sheet — a
# column that suddenly reads as positive on every row — cannot produce a
# thousand-line message. If it is ever actually hit, that is a bug to look at,
# and the closing "N more held" line will say so.
URGENT_MAX = _int("URGENT_MAX", 25)

# How many NON-URGENT cadence items the digest carries. 15 is the 10–15
# phase-1 agreement from the 27 Aug meeting. The urgent items are counted
# separately against URGENT_MAX above and are never truncated by this cap.
#
# Everything past the cap is counted in one closing line and available on demand
# via the "full cadence list" query.
DIGEST_MAX_ITEMS = _int("DIGEST_MAX_ITEMS", 15)

# The uncapped on-demand list is bounded too — a Discord reply has a size limit,
# and 400 lines of cadence is not an answer.
CADENCE_FULL_LIST_MAX = _int("CADENCE_FULL_LIST_MAX", 200)

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

# ONLINE RESEARCH IS NOT IN PHASE 1. This bot has no web access, so a brief's
# external-research section says so in as many words rather than being quietly
# absent — and nothing in it may be invented. Leave this ON until a web-access
# decision is made and implemented; turning it off only hides the notice, it
# does not add research.
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

# -- Row hygiene flags --------------------------------------------------------
# Surfaced in-channel at most ONCE A DAY per row, so a flag is a signal rather
# than a recurring complaint.

# An open row with no response whose last follow-up is older than this (working
# days) is STALLED.
STALLED_AFTER_DAYS = _int("STALLED_AFTER_DAYS", 5)
# Master switch for the three row-hygiene flags (HOT / STALLED / DEAD-DEAL).
# These no longer post on their own: HOT is the first section of the daily
# digest and STALLED/DEAD-DEAL are its HYGIENE section. Off means both sections
# are simply absent.
SHEET_FLAGS_ENABLED = _bool("SHEET_FLAGS_ENABLED", default=True)

# MAX_FLAGS_PER_SWEEP is RETIRED — there is no per-sweep posting left to cap.
# SALES_DIGEST_MAX_PER_SECTION bounds each digest section instead, and unlike
# the old cap it SAYS how many rows it left out rather than silently dropping
# them onto the next tick.

# -- THE ONE DAILY DIGEST -----------------------------------------------------
# Every proactive thing this bot has to say goes out ONCE A DAY, in one message,
# at SALES_DIGEST_TIME. There is no other unprompted send path in the code: no
# reminders, no chases, no escalations, no hygiene flags scattered through the
# day. See digest.py for what is in it and why.
#
# The two things that are NOT the digest, and are still immediate:
#   - a REPLY to a question someone asked;
#   - the ask-time deadline announcement, which is the answer to "when is the
#     follow-up for X?" and is consent-based by design ("shout to change").

# THE KILL SWITCH FOR EVERY UNPROMPTED MESSAGE.
#
#   true / unset  the digest posts at SALES_DIGEST_TIME (the historical
#                 behaviour, which is why unset means on).
#   false         the bot posts NOTHING unprompted. Not the digest, and not any
#                 section that rides in it — cadence lines, the tracker-update
#                 reminder, meeting-prep briefs, escalations, the funnel block.
#                 None of those has a send path of its own (see bot.py), so one
#                 gate covers all of them.
#
# WHAT DOES NOT STOP: everything still computes. Deadlines are still tracked,
# cadence is still evaluated, SQLite state and audit.jsonl are still written.
# The bot still answers when @-mentioned ("full cadence list", a hygiene check,
# sheet questions) and still announces a deadline when someone asks for one.
# Only the unprompted posting stops.
#
# This module-level value is the BOOT reading, used for the startup log below.
# The runtime gate is `digest_enabled()`, which re-reads the setting at digest
# time so flipping it does not need a restart.
SALES_DIGEST_ENABLED = _bool("SALES_DIGEST_ENABLED", default=True)


def digest_enabled() -> bool:
    """Is unprompted posting on? Read LIVE, not at import.

    Read at digest time rather than at boot so an operator can flip the switch
    and have it take effect on the next sweep tick. A restart applies it too, of
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

# WALL-CLOCK IST, "HH:MM". Computed against Asia/Kolkata explicitly (see
# deadlines.IST), never against the server clock — a cloud box runs UTC, and a
# digest scheduled at "10:00" server time would land at 15:30 for the team.
SALES_DIGEST_TIME = (os.getenv("SALES_DIGEST_TIME", "") or "").strip() or "10:00"

# Most items shown per section. A first run over a messy 886-row sheet can turn
# up seventy stalled rows, and a digest nobody can scroll to the end of is the
# problem we started with. Anything past the cap is COUNTED in a trailing line,
# never silently dropped.
SALES_DIGEST_MAX_PER_SECTION = _int("SALES_DIGEST_MAX_PER_SECTION", 15)

# Which sales channel the digest posts in. 0/unset → the ask channel, else the
# first channel in SALES_CHANNEL_IDS.
SALES_DIGEST_CHANNEL_ID = _int("SALES_DIGEST_CHANNEL_ID", 0)


def digest_time_ist() -> tuple[int, int]:
    """SALES_DIGEST_TIME as (hour, minute) IST. Unreadable values fall back to
    10:00 with a warning rather than to "never"."""
    import digest as _digest

    return _digest.parse_time(SALES_DIGEST_TIME)


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

    One check here does more than warn: a write target that resolves to the
    read-only mapping sheet is forced to "off", which is why this function
    rebinds SHEET_WRITE_TARGET."""
    global SHEET_WRITE_TARGET

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

    # The cadence thresholds. Nothing here is fatal — the rules are all
    # independently useful — but a threshold ordering that makes a rule
    # unreachable is worth one line at boot rather than a week of silence.
    if CADENCE_ENABLED:
        log.info(
            "[config] cadence ON. Thresholds (ALL PLACEHOLDERS until tuned): "
            "FOLLOWUP_STALE_DAYS=%d CONNECT_REMINDER_DAYS=%d ALT_CHANNEL_AT=%d "
            "CONNECT_REMINDER_MAX_DAYS=%d UNRESPONSIVE_AT=%d NEXTSTEP_STALL_DAYS=%d "
            "MEETING_PREP_DAYS=%d URGENT_MAX=%d DIGEST_MAX_ITEMS=%d UPDATE_TRACKER_MAX=%d",
            FOLLOWUP_STALE_DAYS, CONNECT_REMINDER_DAYS, ALT_CHANNEL_AT,
            CONNECT_REMINDER_MAX_DAYS, UNRESPONSIVE_AT, NEXTSTEP_STALL_DAYS,
            MEETING_PREP_DAYS, URGENT_MAX, DIGEST_MAX_ITEMS, UPDATE_TRACKER_MAX,
        )
        if ALT_CHANNEL_AT >= UNRESPONSIVE_AT:
            log.warning(
                "ALT_CHANNEL_AT=%d is not below UNRESPONSIVE_AT=%d — rule (d) 'try another "
                "channel' can never fire, because rule (e) 'mark them unresponsive' takes "
                "over at or above its own threshold. Set ALT_CHANNEL_AT lower.",
                ALT_CHANNEL_AT, UNRESPONSIVE_AT,
            )
        if DIGEST_MAX_ITEMS < 1:
            log.warning(
                "DIGEST_MAX_ITEMS=%d caps the cadence at nothing — the digest would carry "
                "no cadence items at all. The phase-1 agreement was 10-15.",
                DIGEST_MAX_ITEMS,
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
                "written off in a Reason or Status cell will keep being chased."
            )
        if not SALES_DEFAULT_OWNER_ID:
            log.warning(
                "SALES_DEFAULT_OWNER_ID is unset and the tracker has no owner column, so "
                "no cadence line can be addressed to anybody: they will all go out under "
                "the DEADLINE_NOTIFY_IDS group line. Set it to Vaishnavi's Discord id."
            )
        elif SALES_DEFAULT_OWNER_ID not in TEAM_ROSTER_IDS:
            log.warning(
                "SALES_DEFAULT_OWNER_ID=%d is not in TEAM_ROSTER_IDS — the roster gate "
                "fails closed, so cadence items will name that person in plain text "
                "instead of @-mentioning them. Add the id to TEAM_ROSTER_IDS.",
                SALES_DEFAULT_OWNER_ID,
            )
        if CONNECT_REMINDER_MAX_DAYS and CONNECT_REMINDER_MAX_DAYS <= CONNECT_REMINDER_DAYS:
            log.warning(
                "CONNECT_REMINDER_MAX_DAYS=%d is not above CONNECT_REMINDER_DAYS=%d — the "
                "window rule (c) fires in is empty, so NOBODY will be reminded to start "
                "interacting and every never-connected row goes straight to the cold "
                "summary. Set the ceiling above the floor.",
                CONNECT_REMINDER_MAX_DAYS, CONNECT_REMINDER_DAYS,
            )
        elif not CONNECT_REMINDER_MAX_DAYS:
            log.warning(
                "CONNECT_REMINDER_MAX_DAYS=0 — the cold ceiling is OFF, so rule (c) fires "
                "on every never-connected row however old. On the live sheet that was 756 "
                "of 886 rows. The default of 30 exists for exactly that reason."
            )
        if URGENT_MAX < 1:
            log.warning(
                "URGENT_MAX=%d — the urgent items (positive replies awaiting a next step, "
                "meetings in the prep window, post-meeting gaps) would be capped at "
                "nothing. That is the part of the digest Vaishnavi prioritised.",
                URGENT_MAX,
            )
        if UPDATE_TRACKER_MAX < 1:
            log.warning(
                "UPDATE_TRACKER_MAX=%d — no fill-in asks will be posted, so rows whose "
                "follow-up count or last-followed date is missing will simply be silent "
                "rather than asked about.",
                UPDATE_TRACKER_MAX,
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

    if SHEET_WRITE_TARGET == "original":
        log.warning(
            "SHEET_WRITE_TARGET=original — the bot will write its %r column into the REAL "
            "GTM Playbook, not the sandbox. It still only ever writes cells in that one "
            "column, and never overwrites a human-entered date. Set SHEET_WRITE_TARGET=copy "
            "to aim writes at the sandbox instead.",
            BOT_DEADLINE_COLUMN,
        )
    elif SHEET_WRITE_TARGET == "off":
        log.info(
            "SHEET_WRITE_TARGET=off — deadlines are still set, stored and announced, but "
            "nothing is mirrored to any sheet."
        )

    if GTM_SHEET_ORIGINAL_ID == GTM_SHEET_COPY_ID:
        log.warning(
            "GTM_SHEET_ORIGINAL_ID and GTM_SHEET_COPY_ID are the SAME id — the 'sandbox' "
            "is the real sheet, so the read-only guarantee on the original is gone. Point "
            "the copy at the sandbox spreadsheet."
        )

    # The mapping sheet is read-only BY POLICY, so the one configuration that
    # could break that promise — aiming writes at it — is caught here and
    # neutralised rather than warned about. A warning would be a note in a log
    # nobody reads while the bot wrote to a sheet it was told never to touch.
    if is_read_only_sheet_id(sheet_write_id()):
        log.error(
            "SHEET_WRITE_TARGET=%s resolves to GTM_MAPPING_SHEET_ID (%s), which is "
            "READ-ONLY by policy. Forcing SHEET_WRITE_TARGET=off. Point "
            "GTM_SHEET_COPY_ID at the sandbox playbook instead; the mapping sheet is "
            "never a write target.",
            SHEET_WRITE_TARGET, GTM_MAPPING_SHEET_ID,
        )
        SHEET_WRITE_TARGET = "off"

    if GTM_MAPPING_SHEET_ID in (GTM_SHEET_ORIGINAL_ID, GTM_SHEET_COPY_ID):
        log.error(
            "GTM_MAPPING_SHEET_ID is the same id as one of the GTM Playbook sheets. "
            "They are different spreadsheets with different rules — the mapping sheet "
            "is read-only and the playbook is not. Check both ids."
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
        ("STALLED_AFTER_DAYS", STALLED_AFTER_DAYS),
    ):
        if value < 1:
            log.warning(
                "%s=%s is below 1 working day — deadlines would land in the past the "
                "moment they are set.",
                name, value,
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

    # -- the one daily digest ----------------------------------------------
    if SALES_DIGEST_ENABLED:
        hour, minute = digest_time_ist()
        channel = digest_channel_id()
        if not channel:
            log.warning(
                "SALES_DIGEST_ENABLED is on but there is no sales channel to post the "
                "daily digest in — it will be skipped, and since the digest is the ONLY "
                "proactive message this bot sends, nothing unprompted will ever go out. "
                "Set SALES_CHANNEL_IDS (and optionally SALES_DIGEST_CHANNEL_ID)."
            )
        else:
            log.info(
                "Daily digest: %02d:%02d IST in channel %s. This is the only unprompted "
                "message the bot sends; replies and ask-time deadline announcements are "
                "immediate.",
                hour, minute, channel,
            )
        if SALES_DIGEST_MAX_PER_SECTION < 1:
            log.warning(
                "SALES_DIGEST_MAX_PER_SECTION=%s is below 1 — it will be clamped to 1. "
                "Every section would otherwise show nothing but its overflow line.",
                SALES_DIGEST_MAX_PER_SECTION,
            )
        if COS_FOLLOWUP_CHECK_INTERVAL_MINUTES > 60:
            log.warning(
                "COS_FOLLOWUP_CHECK_INTERVAL_MINUTES=%s is over an hour — the digest can "
                "only go out on a sweep tick, so it may post up to that late.",
                COS_FOLLOWUP_CHECK_INTERVAL_MINUTES,
            )
    else:
        log.warning(
            "SALES_DIGEST_ENABLED=false — the bot will send NO unprompted messages at "
            "all: no digest, and none of the sections that ride in it (cadence, the "
            "tracker reminder, meeting-prep briefs, escalations, the funnel block). It "
            "STILL tracks deadlines, evaluates cadence and writes SQLite and "
            "audit.jsonl, still answers when @-mentioned, and still announces a deadline "
            "when someone asks for one. Each skipped digest logs one line: "
            "'[digest] suppressed — SALES_DIGEST_ENABLED=false'. Setting it back to true "
            "resumes at the NEXT scheduled digest; the skipped days are not replayed."
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
    elif STRATEGY_DOC_ID and not GOOGLE_SERVICE_ACCOUNT_JSON:
        log.warning(
            "STRATEGY_DOC_ID is set but GOOGLE_SERVICE_ACCOUNT_JSON is not — the doc is "
            "read over the Drive API with the service account, so it will stay "
            "unreadable."
        )

    return missing

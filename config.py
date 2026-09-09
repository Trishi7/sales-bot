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
    "GTM_EVENTS_TAB_TITLES", "Events & Summits,Events and Summits,Events,Summits",
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

# (1) FIRST CONTACT, NO MOVEMENT. Calendar days after the first-contact date
# before the row is due a follow-up (or a suggestion to try a different PoC at
# the same company). The meeting's number.
FOLLOWUP_GRACE_DAYS = _int("FOLLOWUP_GRACE_DAYS", 7)

# (2) CONNECTED, NO DM YET. Hours after the connection date before the bot asks
# "DM sent?". HOURS, not days, because this is the one check that is meant to
# land while the connection is still warm.
#
# THE SHEET HOLDS DATES, NOT TIMES, so this is rounded UP to whole days when it
# is applied (48h -> 2 days). It is expressed in hours anyway because that is
# how the rule was agreed and how anyone tuning it will think about it.
CONNECT_DM_CHECK_HOURS = _int("CONNECT_DM_CHECK_HOURS", 48)

# (3) STILL NO DM A WEEK ON. Calendar days after the connection date for the
# harder DM check. Distinct from the 48-hour nudge above: that one is a prompt,
# this one is a week of silence on a live connection.
CONNECTION_DM_CHECK_DAYS = _int("CONNECTION_DM_CHECK_DAYS", 7)

# (4) DM SENT, NOTHING SINCE. Calendar days after the DM-sent date before the
# progress check. The progress check ALSO asks for an email address or phone
# number — but ONLY when the first contact was not already by email. Asking a
# prospect you emailed for their email address is the kind of line that gets a
# bot switched off, so the ask is type-aware. See EMAIL_CONTACT_TYPES.
DM_PROGRESS_CHECK_DAYS = _int("DM_PROGRESS_CHECK_DAYS", 7)

# (5) DEMO GIVEN, NOTHING MOVED. Calendar days after the demo before the quote
# chase. Short on purpose: a demo with no quote behind it goes cold fastest.
DEMO_QUOTE_DAYS = _int("DEMO_QUOTE_DAYS", 3)

# (6) THE PRIORITY OVERRIDE (strategy section 5.1). ANY positive or replied row
# gets a meeting-proposal action due within this many WORKING days, and it goes
# to the front of the queue ahead of everything else. Working days, not calendar
# days: "propose a meeting within two days" said on a Friday means Tuesday.
#
# A reply that has been sitting for a week produces a date IN THE PAST, and that
# is left in the past rather than pulled to today — the queue shows it as
# overdue, which is the true statement about it.
MEETING_PROPOSAL_WORKING_DAYS = _int("MEETING_PROPOSAL_WORKING_DAYS", 2)

# (7) SILENT TOUCHES. How many follow-ups with no response before the bot
# counsels a change of channel, and before it suggests marking the PoC
# Unresponsive. THE BOT NEVER WRITES EITHER: marking somebody unresponsive is a
# judgement with consequences and it belongs to whoever owns the row.
CHANNEL_SWITCH_AT = _int("CHANNEL_SWITCH_AT", 4)
UNRESPONSIVE_SUGGEST_AT = _int("UNRESPONSIVE_SUGGEST_AT", 7)

# (8) THE SLOW LANE. Closure below CLOSURE_HOT_THRESHOLD percent gets this
# cadence in calendar days instead of the 7-day one, and sits at the back of the
# queue. A 20% deal chased weekly is a bot spending the team's credibility on a
# row the team has already priced.
SLOW_LANE_DAYS = _int("SLOW_LANE_DAYS", 20)

# ...and the fast lane. Closure ABOVE the threshold, or a deal marked In
# Progress, gets this cadence and rides near the front.
HOT_DEAL_DAYS = _int("HOT_DEAL_DAYS", 7)

# The percentage that divides the two lanes. A row exactly ON the threshold is
# treated as SLOW: "50%" is not "more likely than not".
CLOSURE_HOT_THRESHOLD = _int("CLOSURE_HOT_THRESHOLD", 50)

# (9) ON HOLD. A deal parked by agreement is not chased — it gets a pulse check
# this many calendar days apart, and nothing else.
ON_HOLD_PULSE_DAYS = _int("ON_HOLD_PULSE_DAYS", 30)

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
).strip() or "10:00"

# SALES_DIGEST_TIME is kept as an ALIAS so an existing .env keeps working —
# SALES_DRIP_START wins if both are set. It named the digest's single send time
# and now names the first drip slot, which is the same thing an operator meant
# by it.
SALES_DIGEST_TIME = SALES_DRIP_START

# HOW MANY PROACTIVE MESSAGES A WEEKDAY, EVER. The volume contract, and a HARD
# ceiling rather than a target: three short messages a day is what a busy
# channel absorbs without learning to skim. Anything past it ROLLS TO TOMORROW
# rather than being dropped — except that a positive reply overrides the roll,
# because a reply that waits a day is a reply that goes cold.
DAILY_MESSAGE_CAP = _int("DAILY_MESSAGE_CAP", 3)

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

# How many companies one message names before it says "and N more". A sentence
# listing twenty companies is a list wearing a sentence's clothes.
DRIP_MAX_COMPANIES_PER_MESSAGE = _int("DRIP_MAX_COMPANIES_PER_MESSAGE", 6)

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

    # -- the next-action state machine -------------------------------------
    # Nothing here is fatal: every trigger is independently useful, and a queue
    # that is quieter than intended is recoverable. What IS worth a line at boot
    # is a threshold ordering that makes a trigger UNREACHABLE — that failure is
    # silent by nature, and the whole point of the engine is that its output can
    # be checked against the rules that produced it.
    if NEXT_ACTION_ENABLED:
        log.info(
            "[config] next-action engine ON (computation only — it has no send path). "
            "grace=%dd dm_check=%dh/%dd progress=%dd demo_quote=%dd "
            "meeting_proposal=%dwd channel_switch@%d unresponsive@%d "
            "fast=%dd slow=%dd (threshold %d%%) on_hold_pulse=%dd weekend_shift=%s",
            FOLLOWUP_GRACE_DAYS, CONNECT_DM_CHECK_HOURS, CONNECTION_DM_CHECK_DAYS,
            DM_PROGRESS_CHECK_DAYS, DEMO_QUOTE_DAYS, MEETING_PROPOSAL_WORKING_DAYS,
            CHANNEL_SWITCH_AT, UNRESPONSIVE_SUGGEST_AT, HOT_DEAL_DAYS, SLOW_LANE_DAYS,
            CLOSURE_HOT_THRESHOLD, ON_HOLD_PULSE_DAYS, NEXT_ACTION_WEEKEND_SHIFT,
        )
        if CHANNEL_SWITCH_AT >= UNRESPONSIVE_SUGGEST_AT:
            log.warning(
                "CHANNEL_SWITCH_AT=%d is not below UNRESPONSIVE_SUGGEST_AT=%d, so the "
                "change-of-channel counsel can NEVER fire: the unresponsive suggestion "
                "is evaluated first and takes every row that reaches either. Set "
                "CHANNEL_SWITCH_AT lower.",
                CHANNEL_SWITCH_AT, UNRESPONSIVE_SUGGEST_AT,
            )
        dm_hours_as_days = max(1, -(-max(0, CONNECT_DM_CHECK_HOURS) // 24))
        if dm_hours_as_days >= CONNECTION_DM_CHECK_DAYS:
            log.warning(
                "CONNECT_DM_CHECK_HOURS=%dh rounds to %dd, which is not below "
                "CONNECTION_DM_CHECK_DAYS=%dd — so the 'DM sent?' prompt can never fire: "
                "the week-later DM check is evaluated first. Lower the hours or raise "
                "the days.",
                CONNECT_DM_CHECK_HOURS, dm_hours_as_days, CONNECTION_DM_CHECK_DAYS,
            )
        if SLOW_LANE_DAYS <= HOT_DEAL_DAYS:
            log.warning(
                "SLOW_LANE_DAYS=%d is not above HOT_DEAL_DAYS=%d — the slow lane is not "
                "slower than the fast one, so a 20%% deal is chased as hard as a 90%% "
                "one. That is the thing the two lanes exist to stop.",
                SLOW_LANE_DAYS, HOT_DEAL_DAYS,
            )
        if not 1 <= CLOSURE_HOT_THRESHOLD <= 99:
            log.warning(
                "CLOSURE_HOT_THRESHOLD=%d is outside 1-99, so every row with a closure "
                "percentage lands in the same lane. The default is 50.",
                CLOSURE_HOT_THRESHOLD,
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
        if MEETING_PROPOSAL_WORKING_DAYS < 1:
            log.warning(
                "MEETING_PROPOSAL_WORKING_DAYS=%d — every reply would be due the day it "
                "arrived, so the whole override band reads as overdue on arrival.",
                MEETING_PROPOSAL_WORKING_DAYS,
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
    elif STRATEGY_DOC_ID and not GOOGLE_SERVICE_ACCOUNT_JSON:
        log.warning(
            "STRATEGY_DOC_ID is set but GOOGLE_SERVICE_ACCOUNT_JSON is not — the doc is "
            "read over the Drive API with the service account, so it will stay "
            "unreadable."
        )

    return missing

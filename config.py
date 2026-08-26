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
# The strategy doc IS still a stub — setting these records the intended location
# so the status line can say what we're waiting on, but nothing reads it yet.
# That gap is load-bearing: the deadline cadence prefers this document and only
# falls back to the working-day defaults because it can't be read.
STRATEGY_DOC_ID = (os.getenv("STRATEGY_DOC_ID", "") or "").strip()
STRATEGY_DOC_FILE = (os.getenv("STRATEGY_DOC_FILE", "") or "").strip()

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

# -- Deadline authority -------------------------------------------------------
# When asked about a deadline that doesn't exist, the bot SETS one rather than
# shrugging. Defaults are in WORKING DAYS, IST. The strategy doc's cadence wins
# over these whenever that doc is readable.

# Days after first contact / last touch before the next outreach follow-up is due.
OUTREACH_FOLLOWUP_DAYS = _int("OUTREACH_FOLLOWUP_DAYS", 3)
# Days to wait on a reply before chasing it.
REPLY_CHASE_DAYS = _int("REPLY_CHASE_DAYS", 2)
# Days before a booked meeting that prep is due.
MEETING_PREP_DAYS = _int("MEETING_PREP_DAYS", 1)

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

SALES_DIGEST_ENABLED = _bool("SALES_DIGEST_ENABLED", default=True)

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
            "all. It will still answer questions and still announce a deadline when "
            "someone asks for one, but nothing will be chased, flagged or escalated."
        )

    if WEEKLY_DIGEST_ENABLED and not 0 <= WEEKLY_DIGEST_WEEKDAY <= 6:
        log.warning(
            "WEEKLY_DIGEST_WEEKDAY=%s is outside 0–6 (0=Monday); it will be clamped to "
            "Friday. It now selects which day's DAILY digest carries the funnel block.",
            WEEKLY_DIGEST_WEEKDAY,
        )

    return missing

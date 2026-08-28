"""The GTM Playbook spreadsheet — live reads, and one narrow write path.

The sales team's system of record is a Google Sheet, read LIVE through the Sheets
API with a service account. There is no sync interval and no local copy: a
question asked now is answered from the sheet as it is now, within a short cache.
(rclone is only ever used for the meeting-notes docs; it has nothing to do with
this file.)

TWO SPREADSHEETS
    ORIGINAL  "NFThing <> GTM Playbook" — the READ-ONLY source of truth.
    COPY      "… — BOT COPY (sandbox)"  — the bot's writable mirror, and the
              default write target (`SHEET_WRITE_TARGET`).

A THIRD spreadsheet — the researcher/buyer mapping — is deliberately NOT handled
here. It is read-only by policy and lives in mapping_sheet.py, which has no write
method and a read-only OAuth scope. This module only has to know that it must
never write to it: `_refuse_if_read_only()` checks every write target against
`config.is_read_only_sheet_id()` before a cell is touched.

FOUR KINDS OF TAB. Three are recognised by what's in them rather than by name;
the fourth is recognised by its NAME, deliberately:
    master_data          THE canonical cadence source — one row per company/PoC
                         being worked, auto-updated from a HIDDEN "outreach
                         updates" sheet. Identified by TITLE
                         (GTM_MASTER_TAB_TITLES), because "this tab is the
                         master" is a human decision, not something to infer
                         from headers that the old tracker also has.
    positioning_matrix   use cases A–I: label, use case, problem, offering,
                         company type, ICP, business impact.
    outreach_tracker     one row per company being worked. The pre-master
                         tracker; still read, still the fallback when no master
                         tab exists.
    prospect_priority    scored companies, P1–P3, with a rationale.

HIDDEN TABS ARE READ. gspread returns hidden worksheets like any other, and the
master tab's own feed is hidden — so discovery includes them (GTM_READ_HIDDEN_TABS)
and the schema log marks them "hidden" rather than pretending they aren't there.

THE BOT READS VALUES, NEVER FORMATTING. A status conveyed by CELL COLOUR is
invisible to it. That failure is silent by nature, so `_warn_colour_coded()`
looks for its signature at startup — a mapped column that is empty on nearly
every row — and names the column in a warning instead of quietly treating the
whole column as blank.

HEADERS ARE DISCOVERED AT PARSE TIME. The tabs will evolve — columns get added,
renamed, reordered — so nothing here is positional. Each tab's header row is
read, each header is matched to a known ROLE by a set of aliases, and everything
unmatched is still carried as an "extra" so a question about a column this code
has never heard of can still be answered. `GTM_COLUMN_MAP` overrides the match
when wording is genuinely ambiguous.

WRITES ARE DELIBERATELY TINY. The bot appends ONE column — `BOT_DEADLINE_COLUMN`,
"Next Deadline (bot)" — at the far right of the tracker tab, and writes only
individual cells inside it, one `values.update` per cell. Never a row, never a
full-sheet write, never any other column. And a human-entered date in the
ORIGINAL always wins: the bot adopts it rather than overwriting it.

FAILURE IS A FIRST-CLASS STATE. No access, a quota ban, an auth failure — none of
these may take the bot down. A failed read falls back to the last cached copy
with an explicit staleness note; a sheet that was never readable reports
awaiting-access along with the exact "share it with <service account>"
instruction, so the fix is one line rather than a debugging session.
"""
import logging
import re
import threading
import time
from typing import Optional

import config

log = logging.getLogger(__name__)

# gspread / google-auth are optional at IMPORT time: the bot must still start and
# still answer non-sheet questions on a box where they aren't installed. The
# import error is reported through the normal awaiting-access path instead.
try:
    import gspread
    from google.oauth2.service_account import Credentials

    _IMPORT_ERROR: Optional[str] = None
except Exception as e:  # pragma: no cover - depends on the environment
    gspread = None
    Credentials = None
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"

# Read+write on spreadsheets only. No Drive scope: the bot has no business
# listing, creating, sharing or deleting files, and a narrower token is a
# narrower blast radius if the key ever leaks.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

ORIGINAL = "original"
COPY = "copy"

# Tab kinds.
MASTER = "master_data"
POSITIONING = "positioning_matrix"
TRACKER = "outreach_tracker"
PRIORITY = "prospect_priority"

# The roles the phase-1 cadence rules read. Every one of them is nameable in
# GTM_COLUMN_MAP, so a column rename in the sheet is an env change.
CADENCE_ROLES = (
    "company", "industry", "poc", "poc_designation", "poc_vertical",
    "first_contacted", "connected", "intro_date", "last_followed_up",
    "followups_count", "response", "meeting_date", "assets_shared", "next_steps",
)


# -- header roles -------------------------------------------------------------
# role -> the alias fragments that identify it. Matching is case-insensitive and
# punctuation-insensitive, tried as exact-normalised first and then as a
# substring, longest alias first — so "last followed up date" beats a loose
# "date" match. Order within a list is only a tie-break aid; specificity wins.
ROLES: dict[str, dict[str, tuple[str, ...]]] = {
    # The master tab. Its rule fields are exactly the ones named in the phase-1
    # spec; the aliases cover the wordings the playbook has actually used
    # ("initial contact month" for first contact, "membrane Intro Date" for the
    # intro). Anything unmatched still arrives in the row's "_extra".
    MASTER: {
        "sr_no": ("sr. no.", "sr no", "s.no", "sno", "serial", "#"),
        "company": ("company", "company name", "account", "organisation",
                    "organization", "client", "org"),
        "industry": ("industry", "sector", "domain"),
        "poc": ("poc", "person", "point of contact", "contact name",
                "contact person", "poc name", "champion", "contact"),
        "poc_designation": ("designation", "poc designation", "title", "job title", "role"),
        "poc_vertical": ("vertical", "poc vertical", "department", "function", "team"),
        "first_contacted": ("first contacted", "initial contact month", "initial contact",
                            "first contact", "date of first contact", "month of first contact",
                            "outreach date", "contacted on"),
        "connected": ("connected?", "connected", "connection status", "is connected"),
        "intro_date": ("membrane intro date", "intro date", "introduction date",
                       "membrane intro", "intro"),
        "last_followed_up": ("last followed-up date", "last followed up date",
                             "last follow-up date", "last followed up", "last follow up",
                             "last followup", "last touch"),
        "followups_count": ("total follow-ups", "total follow ups", "total followups",
                            "total follow-ups till date", "number of follow-ups",
                            "no of follow ups", "follow-ups", "followups"),
        "response": ("response", "response?", "response (y/p/n)", "responded", "replied"),
        "meeting_date": ("meeting date", "meeting", "call date", "demo date", "meeting on"),
        "assets_shared": ("assets shared", "assets", "collateral shared",
                          "material shared", "deck shared"),
        "next_steps": ("next steps", "next step", "next action", "action items", "action"),
        # Not in the spec's field list, but read when present: they make the
        # digest addressable and the exclusion rule reliable.
        "owner": ("owner", "row owner", "assigned to", "assignee", "sdr", "bd",
                  "account owner", "handled by", "responsible"),
        "status": ("status", "stage", "deal status", "current status"),
        "reason": ("reason", "lost reason", "reason for no response", "why"),
        "use_case": ("use case", "usecase", "pitch"),
        "other_updates": ("other updates", "updates", "notes", "comments", "remarks"),
        "bot_deadline": (config.BOT_DEADLINE_COLUMN.lower(), "next deadline (bot)",
                         "next deadline"),
    },
    POSITIONING: {
        "label": ("label", "use case label", "ref", "id"),
        "use_case": ("use case", "usecase", "case"),
        "problem": ("problem statement", "problem", "pain"),
        "offering": ("offering", "what we offer", "solution", "product"),
        "company_type": ("company type", "type of company", "segment", "category"),
        "icp": ("icp", "ideal customer profile", "ideal customer"),
        "business_impact": ("business impact", "impact", "value", "outcome"),
    },
    TRACKER: {
        "sr_no": ("sr. no.", "sr no", "s.no", "sno", "serial", "#"),
        "company": ("company", "company name", "account", "client", "organisation", "organization"),
        "industry": ("industry", "sector", "vertical (company)"),
        "poc": ("poc", "point of contact", "contact name", "contact person", "champion"),
        "poc_designation": ("poc designation", "designation", "title", "role"),
        "poc_vertical": ("poc vertical", "vertical", "department", "function"),
        "first_contacted": ("first contacted", "first contact", "date of first contact", "outreach date"),
        "use_case": ("use case", "usecase", "pitch"),
        "intro_date": ("membrane intro date", "intro date", "introduction date", "intro"),
        "last_followed_up": ("last followed up date", "last followed up", "last follow up", "last followup", "last touch"),
        "followups_count": ("total follow-ups till date", "total follow ups", "total followups", "number of follow-ups", "follow-ups", "followups"),
        "response": ("response?", "response", "responded", "replied"),
        "reason": ("reason", "lost reason", "reason for no response", "why"),
        "meeting_date": ("meeting date", "meeting", "call date", "demo date"),
        "assets_shared": ("assets shared", "assets", "collateral shared", "material shared"),
        "next_steps": ("next steps", "next step", "action", "next action"),
        "other_updates": ("other updates", "updates", "notes", "comments", "remarks"),
        "connected": ("connected?", "connected", "connection status"),
        # The bot's own column. Never auto-created by a read; see ensure_bot_column().
        "bot_deadline": (config.BOT_DEADLINE_COLUMN.lower(), "next deadline (bot)", "next deadline"),
    },
    PRIORITY: {
        "company": ("company", "company name", "account", "prospect", "organisation", "organization"),
        "priority": ("priority", "p1/p2/p3", "tier", "band", "priority bucket"),
        "score": ("score", "rating", "points", "weighted score"),
        "rationale": ("rationale", "reason", "why", "justification", "notes"),
        "industry": ("industry", "sector"),
        "use_case": ("use case", "usecase"),
    },
}

# Tab-kind detection: (kind, signature roles, how many must match, roles that are
# MANDATORY however good the rest of the score is).
#
# The mandatory column is what stops false positives on a sheet with twenty tabs.
# A prospect list is identified by having an actual priority/score column — the
# real playbook has an investor tab whose "Activity / Notes" header matched
# "rationale" and whose fund names matched loosely, and without this it was
# detected as a prospect-priority tab and its rows would have been answered from.
# `company` is mandatory for PRIORITY too: every prospect answer cites the company
# it came from, so a scored tab with no company column has nothing citable in it
# (the live "Activation Score - Warmed Up Co" tab is exactly this — a scoring
# scratchpad with priority and score headers and no companies).
#
# MASTER IS DELIBERATELY ABSENT FROM THE SCORE-OFF BELOW. Its columns are a
# superset of the tracker's, so on headers alone every tracker tab would score as
# a master and the "which tab is canonical" question would be decided by row
# counts. It is matched by TITLE instead (`_is_master_title`), and its signature
# is only ever used to confirm a title match — see `_detect_kind`.
_MASTER_SIGNATURE = (
    MASTER,
    ("company", "poc", "first_contacted", "connected", "response",
     "last_followed_up", "next_steps"),
    3,
    ("company",),
)

_KIND_SIGNATURES: list[tuple[str, tuple[str, ...], int, tuple[str, ...]]] = [
    (TRACKER, ("company", "first_contacted", "last_followed_up", "response", "next_steps"),
     3, ("company",)),
    (POSITIONING, ("use_case", "problem", "offering", "icp", "business_impact"),
     3, ("use_case",)),
    (PRIORITY, ("company", "priority", "score", "rationale"),
     2, ("company", "priority")),
]

_KIND_NAME_HINTS: list[tuple[str, tuple[str, ...]]] = [
    (TRACKER, ("outreach", "tracker", "pipeline")),
    (POSITIONING, ("positioning", "matrix", "use case", "usecase", "playbook")),
    (PRIORITY, ("priority", "prospect", "scoring", "score", "p1")),
]

# How hard the write round-trip tries to read its cell back before giving up.
# Sheets' read quota is per-minute, so a short exponential backoff clears the
# common case; three attempts is ~7s worst case, which a self-test can afford.
_ROUNDTRIP_READ_ATTEMPTS = 3
_ROUNDTRIP_BACKOFF_SECONDS = 2

# "P1" / "p-2" / "Priority 3" → 1 / 2 / 3.
_PRIORITY_RE = re.compile(r"\bp[\s\-]?([123])\b", re.IGNORECASE)
# Values that mean "yes" in a flag column like Connected?.
_YES = {"yes", "y", "true", "1", "responded", "replied", "connected", "positive", "done"}
_NO = {"no", "n", "false", "0", "none", "-", "na", "n/a", "not yet", "nil"}

# THE RESPONSE VOCABULARY, taken from what the real tracker actually contains
# rather than from what a "Response?" header suggests. The live column holds
# "P" / "N" / "Did Respond" / "Awaited" / blank — not yes and no — and reading it
# as a yes/no flag made every replied prospect invisible to the HOT check.
#
# The distinction that matters is POLARITY, because the two flags need different
# things: STALLED must not chase anyone who replied at all (positive or not),
# while HOT is about a reply that deserves an answer and hasn't had one.
RESPONSE_POSITIVE = "positive"
RESPONSE_NEGATIVE = "negative"
RESPONSE_UNKNOWN = "responded"   # they replied; the sheet doesn't say how it went
RESPONSE_NONE = "none"           # nothing back yet

_RESP_POSITIVE = {"p", "positive", "yes", "y", "interested", "warm", "keen", "good"}
_RESP_NEGATIVE = {"n", "negative", "no", "not interested", "declined", "rejected", "pass"}
_RESP_NONE = {"", "awaited", "awaiting", "await", "no response", "none", "-", "na",
              "n/a", "nil", "not yet", "pending", "tbd"}


def response_status(value) -> str:
    """Classify a Response? cell into positive / negative / responded / none.

    Anything unrecognised counts as RESPONSE_UNKNOWN — a human wrote something
    in the cell, so they DID hear back, and treating an unfamiliar note as
    silence would make the bot chase a prospect who has already answered.
    """
    v = normalise_header(str(value or "")).replace("?", "").strip()
    if v in _RESP_NONE:
        return RESPONSE_NONE
    if v in _RESP_POSITIVE:
        return RESPONSE_POSITIVE
    if v in _RESP_NEGATIVE:
        return RESPONSE_NEGATIVE
    if "did respond" in v or "responded" in v or "replied" in v:
        return RESPONSE_UNKNOWN
    if v.startswith("not ") or v.startswith("no "):
        return RESPONSE_NEGATIVE
    return RESPONSE_UNKNOWN


def normalise_header(text: str) -> str:
    """A header as a comparable key: lower-cased, punctuation collapsed to
    spaces, whitespace squeezed. "Sr. No." and "sr no" become the same thing."""
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9?]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_yes(value) -> bool:
    """True when a Response?/Connected? cell says yes. Anything unrecognised is
    NOT a yes — a "maybe" or a stray note must never be read as a reply."""
    v = normalise_header(str(value or "")).replace("?", "").strip()
    return v in _YES


def is_no_or_blank(value) -> bool:
    """True when a cell is empty or explicitly negative. Distinct from `not
    is_yes(...)`, which would also swallow "waiting on legal"."""
    v = normalise_header(str(value or "")).replace("?", "").strip()
    return v == "" or v in _NO


def parse_priority(value) -> Optional[int]:
    """"P1" → 1. None when the cell doesn't name a priority band."""
    m = _PRIORITY_RE.search(str(value or ""))
    if m:
        return int(m.group(1))
    v = normalise_header(str(value or ""))
    return int(v) if v in ("1", "2", "3") else None


class SheetAccessError(Exception):
    """A sheet could not be read. Carries a human-actionable `remedy` — usually
    the exact share instruction — because "permission denied" alone has never
    helped anyone."""

    def __init__(self, message: str, *, remedy: str = "", transient: bool = False):
        super().__init__(message)
        self.remedy = remedy
        # Transient (quota, 5xx, network) vs structural (not shared, no key).
        # The former is worth answering from cache; the latter needs a human.
        self.transient = transient


class Tab:
    """One parsed worksheet: its discovered schema and its rows.

    `rows` are dicts keyed by ROLE, plus "_row" (the 1-based sheet row number,
    needed to address a cell for a write) and "_extra" (every column that didn't
    map to a known role, keyed by its original header — so a question about a
    column this code has never heard of is still answerable).
    """

    def __init__(self, *, title: str, kind: str, headers: list[str],
                 role_to_col: dict[str, int], rows: list[dict], read_at: float,
                 header_row: int = 1):
        self.title = title
        self.kind = kind
        self.headers = headers
        self.role_to_col = role_to_col      # role -> 0-based column index
        self.rows = rows
        self.read_at = read_at
        # 1-based sheet row the headers live on. Kept because appending the bot's
        # column has to put its header on the SAME row, and re-reading the whole
        # tab just to rediscover that costs a request we don't need to spend.
        self.header_row = header_row

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.read_at)

    def column_letter(self, role: str) -> Optional[str]:
        idx = self.role_to_col.get(role)
        return _col_letter(idx) if idx is not None else None

    def schema_line(self) -> str:
        """The one-line schema summary logged once per tab at startup."""
        mapped = ", ".join(sorted(self.role_to_col)) or "(none)"
        return (
            f"tab {self.title!r} kind={self.kind} rows={len(self.rows)} "
            f"cols={len(self.headers)} mapped=[{mapped}]"
        )

    def find_company(self, name: str) -> list[dict]:
        """Rows whose company cell matches `name`, exact-ish first then
        substring. Returns a LIST: two rows for one company is a real thing in a
        tracker, and silently picking one would hide it."""
        want = normalise_header(name)
        if not want:
            return []
        exact = [r for r in self.rows if normalise_header(r.get("company", "")) == want]
        if exact:
            return exact
        return [r for r in self.rows if want in normalise_header(r.get("company", ""))]


def _col_letter(index0: int) -> str:
    """0-based column index → A1 letter (0→A, 26→AA)."""
    n = int(index0) + 1
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


class GTMSheets:
    """Live access to the two GTM spreadsheets.

    Thread-safety: reads happen from `asyncio.to_thread`, so the cache and the
    lazily-built client are guarded by a lock.
    """

    def __init__(self) -> None:
        self._client = None
        self._auth_error: Optional[str] = None
        self._lock = threading.RLock()
        # (sheet_key, tab_kind) -> Tab. Holds the LAST GOOD read, kept
        # indefinitely so a later outage can still be answered from it (with a
        # staleness note) rather than with silence.
        self._cache: dict[tuple[str, str], Tab] = {}
        # sheet_key -> {"ok": bool, "error": str, "remedy": str, "title": str}
        self._access: dict[str, dict] = {}
        self._schema_logged: set[tuple[str, str]] = set()

    # -- identity ----------------------------------------------------------

    @property
    def service_account_email(self) -> str:
        """The address the sheets have to be shared with. Read straight from the
        key file so the instruction can never name the wrong account."""
        path = config.GOOGLE_SERVICE_ACCOUNT_JSON
        if not path:
            return ""
        try:
            import json

            with open(path, "r", encoding="utf-8") as f:
                return str(json.load(f).get("client_email") or "")
        except Exception:
            log.debug("[gtm] could not read client_email from the key file", exc_info=True)
            return ""

    def share_instruction(self, which: str) -> str:
        """The exact fix for a permission error, naming the account and the
        access level each sheet needs."""
        email = self.service_account_email or "the bot's service account"
        role = "Viewer" if which == ORIGINAL else "Editor"
        label = (
            "NFThing <> GTM Playbook"
            if which == ORIGINAL
            else "NFThing <> GTM Playbook — BOT COPY (sandbox)"
        )
        # Deliberately ASCII: this string is logged, and a Windows console on a
        # cp1252 code page raises UnicodeEncodeError on arrows and dashes — which
        # would turn "here is how to fix your config" into a crash.
        return (
            f'Share "{label}" with {email} as {role} '
            f"(open the sheet, click Share, paste the address, set {role}, Send)."
        )

    def sheet_id(self, which: str) -> str:
        return config.GTM_SHEET_ORIGINAL_ID if which == ORIGINAL else config.GTM_SHEET_COPY_ID

    # -- auth --------------------------------------------------------------

    def _get_client(self):
        """The authorised gspread client, built once. Raises SheetAccessError
        with an actionable remedy rather than a bare exception."""
        with self._lock:
            if self._client is not None:
                return self._client
            if gspread is None:
                raise SheetAccessError(
                    f"the Google Sheets libraries aren't installed ({_IMPORT_ERROR})",
                    remedy="Run: pip install gspread google-auth",
                )
            path = config.GOOGLE_SERVICE_ACCOUNT_JSON
            if not path:
                raise SheetAccessError(
                    "no service-account key is configured",
                    remedy="Set GOOGLE_SERVICE_ACCOUNT_JSON to the key file path.",
                )
            try:
                creds = Credentials.from_service_account_file(path, scopes=SCOPES)
                self._client = gspread.authorize(creds)
            except FileNotFoundError:
                raise SheetAccessError(
                    f"the service-account key file {path!r} does not exist",
                    remedy=f"Put the key at {path}, or fix GOOGLE_SERVICE_ACCOUNT_JSON.",
                )
            except Exception as e:
                raise SheetAccessError(
                    f"the service-account key could not be loaded ({type(e).__name__})",
                    remedy=f"Check that {path!r} is a valid service-account JSON key.",
                )
            return self._client

    def _open(self, which: str):
        """Open one spreadsheet, translating the API's errors into something a
        human can act on."""
        client = self._get_client()
        sid = self.sheet_id(which)
        if not sid:
            raise SheetAccessError(
                f"no spreadsheet id configured for the {which}",
                remedy=f"Set GTM_SHEET_{'ORIGINAL' if which == ORIGINAL else 'COPY'}_ID.",
            )
        try:
            return client.open_by_key(sid)
        except Exception as e:
            raise self._translate(e, which, sid)

    def _translate(
        self, e: Exception, which: str, sid: str, *, writing: bool = False
    ) -> SheetAccessError:
        """Map an API exception to a SheetAccessError with the right remedy and
        the right transient flag.

        `writing` matters: a 403 on a READ means the sheet isn't shared at all,
        while a 403 on a WRITE means it is shared read-only. Same status code,
        completely different fix."""
        name = type(e).__name__
        text = str(e) or ""
        status = getattr(getattr(e, "response", None), "status_code", None)

        if name == "PermissionError" or status == 403 or "PERMISSION_DENIED" in text:
            # Three very different 403s share this shape, so distinguish them —
            # each has a different fix, and "permission denied" alone sends
            # someone to the wrong console.
            if "has not been used" in text or "SERVICE_DISABLED" in text or "accessNotConfigured" in text:
                return SheetAccessError(
                    "the Google Sheets API is not enabled for this service account's project",
                    remedy=(
                        "Enable the Google Sheets API in the Google Cloud console for the "
                        "key's project, then retry."
                    ),
                )
            if writing:
                # Read worked, write didn't: the sheet is shared READ-ONLY. This
                # is the single most likely sharing mistake, and it is invisible
                # until the first write, so name it precisely.
                return SheetAccessError(
                    f"the service account can READ the {which} sheet but not WRITE to it "
                    "(it is shared read-only)",
                    remedy=(
                        f"Re-share it with {self.service_account_email or 'the service account'} "
                        "as EDITOR, not Viewer (open the sheet, click Share, find the address, "
                        "change Viewer to Editor)."
                    ),
                )
            return SheetAccessError(
                f"the service account cannot open the {which} sheet (permission denied)",
                remedy=self.share_instruction(which),
            )
        if name == "SpreadsheetNotFound" or status == 404:
            return SheetAccessError(
                f"the {which} spreadsheet id {sid!r} was not found",
                remedy=(
                    f"Check GTM_SHEET_{'ORIGINAL' if which == ORIGINAL else 'COPY'}_ID. "
                    "It is the long id in the sheet's URL between /d/ and /edit. "
                    + self.share_instruction(which)
                ),
            )
        if status == 429 or "RESOURCE_EXHAUSTED" in text or "Quota exceeded" in text:
            return SheetAccessError(
                "the Sheets API quota is exhausted",
                remedy="Wait for the quota window to reset; reads are cached meanwhile.",
                transient=True,
            )
        if status and 500 <= int(status) < 600:
            return SheetAccessError(
                f"the Sheets API returned a server error ({status})",
                remedy="Transient on Google's side; the last cached read is used meanwhile.",
                transient=True,
            )
        if name in ("APIError", "ConnectionError", "Timeout", "ReadTimeout", "SSLError"):
            return SheetAccessError(
                f"the Sheets API call failed ({name})",
                remedy="Usually transient; the last cached read is used meanwhile.",
                transient=True,
            )
        return SheetAccessError(
            f"the {which} sheet could not be opened ({name})",
            remedy=self.share_instruction(which),
        )

    # -- access check (startup) -------------------------------------------

    def check_access(self) -> dict[str, dict]:
        """Probe BOTH spreadsheets. Never raises.

        Returns {which: {ok, title, tabs, error, remedy}}. This is what the
        startup check reports and what `sources.py` turns into connected /
        awaiting-access, so a missing share is surfaced as one actionable line
        instead of as a crash.
        """
        out: dict[str, dict] = {}
        for which in (ORIGINAL, COPY):
            try:
                sh = self._open(which)
                titles = [ws.title for ws in sh.worksheets()]
                out[which] = {
                    "ok": True,
                    "title": sh.title,
                    "tabs": titles,
                    "error": "",
                    "remedy": "",
                }
                log.info(
                    "[gtm] %s sheet OK: %r with %d tab(s): %s",
                    which, sh.title, len(titles), ", ".join(titles),
                )
            except SheetAccessError as e:
                out[which] = {
                    "ok": False,
                    "title": "",
                    "tabs": [],
                    "error": str(e),
                    "remedy": e.remedy,
                }
                log.error("[gtm] %s sheet UNAVAILABLE: %s", which, e)
                if e.remedy:
                    log.error("[gtm] %s sheet FIX: %s", which, e.remedy)
            except Exception as e:
                out[which] = {
                    "ok": False, "title": "", "tabs": [],
                    "error": f"unexpected {type(e).__name__}", "remedy": "",
                }
                log.exception("[gtm] %s sheet check raised unexpectedly", which)
        with self._lock:
            self._access = out
        return out

    def last_access(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._access)

    # -- schema discovery --------------------------------------------------

    def _map_headers(self, kind: str, headers: list[str]) -> dict[str, int]:
        """Discovered headers → {role: column index}.

        Exact normalised matches are taken first across ALL roles, then
        substring matches fill what's left — otherwise a loose alias like "date"
        could claim the column that an exact "Meeting Date" wanted. A header is
        used by at most one role, and a role by at most one header.
        """
        aliases = ROLES.get(kind, {})
        norm = [normalise_header(h) for h in headers]
        taken_cols: set[int] = set()
        out: dict[str, int] = {}

        # An operator override is absolute — it exists precisely for the cases
        # auto-detection gets wrong, so it is applied before any matching.
        override = (config.GTM_COLUMN_MAP or {}).get(kind, {})
        for role, header_text in (override or {}).items():
            key = normalise_header(str(header_text))
            for i, h in enumerate(norm):
                if h == key and i not in taken_cols:
                    out[role] = i
                    taken_cols.add(i)
                    log.info(
                        "[gtm] GTM_COLUMN_MAP: %s.%s -> column %r", kind, role, headers[i]
                    )
                    break
            else:
                log.warning(
                    "[gtm] GTM_COLUMN_MAP names %r for %s.%s, but no such header exists "
                    "in this tab; falling back to auto-detection for that role.",
                    header_text, kind, role,
                )

        for exact_pass in (True, False):
            for role, role_aliases in aliases.items():
                if role in out:
                    continue
                # Longest alias first: "last followed up date" must beat "date".
                for alias in sorted(role_aliases, key=len, reverse=True):
                    key = normalise_header(alias)
                    if not key:
                        continue
                    for i, h in enumerate(norm):
                        if i in taken_cols or not h:
                            continue
                        hit = (h == key) if exact_pass else (key in h or h in key)
                        if hit:
                            out[role] = i
                            taken_cols.add(i)
                            break
                    if role in out:
                        break
        return out

    def _matches_kind(self, kind: str, headers: list[str]) -> int:
        """How well `headers` fit `kind`: the signature score, or 0 for no match.

        A kind's mandatory roles must ALL be present — that is what keeps a tab
        of investor notes from being read as a prospect list because one of its
        headers happened to contain the word "notes".
        """
        spec = next((s for s in _KIND_SIGNATURES if s[0] == kind), None)
        if spec is None and kind == MASTER:
            spec = _MASTER_SIGNATURE
        if spec is None:
            return 0
        _k, needed, threshold, mandatory = spec
        mapped = self._map_headers(kind, headers)
        if any(role not in mapped for role in mandatory):
            return 0
        score = sum(1 for r in needed if r in mapped)
        return score if score >= threshold else 0

    @staticmethod
    def _is_master_title(title: str) -> bool:
        """Is this the tab an operator has NAMED as the master?

        Exact normalised title match against GTM_MASTER_TAB_TITLES. Deliberately
        NOT a substring test: the live playbook already has a "Master Lead List"
        that is a prospect-priority tab, and a loose match would hand the whole
        cadence to it.
        """
        low = normalise_header(title)
        if not low:
            return False
        return any(
            low == normalise_header(t)
            for t in (config.GTM_MASTER_TAB_TITLES or [])
            if str(t).strip()
        )

    def _detect_kind(self, title: str, headers: list[str]) -> Optional[str]:
        """What KIND of tab this is.

        THE MASTER TAB IS DECIDED BY ITS TITLE, first and unconditionally-ish:
        an operator named it in GTM_MASTER_TAB_TITLES, and that is a decision
        about which tab is canonical, not a guess to be overridden by a header
        score. Its signature still has to match, so a tab renamed "Master data"
        with none of the rule columns in it is reported rather than silently
        driving the cadence.

        Everything else is by name hint first, then by header signature so
        detection survives a rename. None when it is none of the kinds, which is
        the common case in a real playbook full of research and strategy tabs.
        """
        low = normalise_header(title)
        if self._is_master_title(title):
            if self._matches_kind(MASTER, headers):
                return MASTER
            log.warning(
                "[gtm] tab %r is named as the master tab (GTM_MASTER_TAB_TITLES) but "
                "does not carry the cadence columns — found headers %s. The cadence "
                "will fall back to the outreach tracker. Fix the tab, or point "
                "GTM_MASTER_TAB_TITLES / GTM_COLUMN_MAP at the right thing.",
                title, ", ".join(repr(h) for h in headers[:20]) or "(none)",
            )
        for kind, hints in _KIND_NAME_HINTS:
            if any(h in low for h in hints) and self._matches_kind(kind, headers):
                # The name hint only wins if the HEADERS agree: a tab called
                # "Playbook" holding tracker columns is a tracker.
                return kind
        best, best_score = None, 0
        for kind, _needed, _threshold, _mandatory in _KIND_SIGNATURES:
            score = self._matches_kind(kind, headers)
            if score > best_score:
                best, best_score = kind, score
        return best

    @staticmethod
    def _header_row_index(values: list[list]) -> int:
        """Which row is the header. Sheets in the wild carry a title row, a blank
        row, or a note above the real header, so this picks the first row with at
        least two non-empty cells that looks like labels rather than data."""
        for i, row in enumerate(values[:10]):
            filled = [c for c in row if str(c).strip()]
            if len(filled) >= 2:
                return i
        return 0

    @staticmethod
    def _a1_sheet(title: str) -> str:
        """A worksheet title as an A1 range meaning "the whole sheet".

        Single quotes inside the name are doubled, which is how A1 notation
        escapes them — the live playbook has a tab called
        "6thJune'26_Strategy_Fortune 500" and an unescaped name there makes the
        whole batch request fail.
        """
        return "'" + str(title).replace("'", "''") + "'"

    def _batch_values(self, sh, titles: list[str]) -> dict[str, list[list]]:
        """Every worksheet's values in ONE API call, as {title: rows}.

        This is a quota decision, not a tidiness one. The obvious version calls
        `get_all_values()` per worksheet, which is ~20 read requests per
        spreadsheet on the real playbook and ~40 for both — and Sheets allows 60
        reads per minute per user. Two cache misses in a minute exhausted the
        quota and the bot started answering from stale cache for no reason.
        One batch call is one request no matter how many tabs there are.

        Falls back to per-worksheet reads if the batch call fails, so an
        unexpected range error degrades to "slow" rather than "no data".
        """
        try:
            resp = sh.values_batch_get([self._a1_sheet(t) for t in titles])
        except Exception:
            log.warning(
                "[gtm] batched read failed; falling back to one request per tab "
                "(this burns quota — see _batch_values)", exc_info=True,
            )
            out: dict[str, list[list]] = {}
            for ws in sh.worksheets():
                try:
                    out[ws.title] = ws.get_all_values()
                except Exception:
                    log.exception("[gtm] could not read tab %r", ws.title)
            return out

        ranges = resp.get("valueRanges") or []
        # The response comes back in request order, so zip against the titles we
        # asked for rather than trying to parse the sheet name back out of the
        # returned range string (which re-quotes and re-escapes it).
        return {
            title: (vr.get("values") or [])
            for title, vr in zip(titles, ranges)
        }

    @staticmethod
    def _worksheets(sh) -> list[tuple[str, bool]]:
        """[(title, hidden)] for EVERY worksheet, hidden ones included.

        gspread's `worksheets()` grew an `exclude_hidden` argument; older
        releases have no such parameter and return everything. Both are handled,
        because the master tab is fed by a hidden "outreach updates" sheet and a
        discovery pass that skips hidden tabs would miss the thing this phase is
        built on.
        """
        try:
            sheets = sh.worksheets(exclude_hidden=False)
        except TypeError:
            sheets = sh.worksheets()   # older gspread: hidden already included
        out: list[tuple[str, bool]] = []
        for ws in sheets:
            hidden = getattr(ws, "isSheetHidden", None)
            if hidden is None:
                try:
                    hidden = bool((ws._properties or {}).get("hidden", False))
                except Exception:
                    hidden = False
            out.append((ws.title, bool(hidden)))
        if not config.GTM_READ_HIDDEN_TABS:
            skipped = [title for title, hidden in out if hidden]
            if skipped:
                log.warning(
                    "[gtm] GTM_READ_HIDDEN_TABS=false — skipping %d hidden tab(s): %s. "
                    "The master tab's feed is hidden, so this can hide the cadence "
                    "source.", len(skipped), ", ".join(repr(s) for s in skipped),
                )
            out = [(title, hidden) for title, hidden in out if not hidden]
        return out

    def _warn_colour_coded(self, tab: "Tab") -> None:
        """Name the mapped columns that are systematically empty.

        THE BOT READS VALUES, NOT FORMATTING. In the master tab a status can be
        carried by the CELL'S COLOUR, which the Sheets values API does not return
        and this bot therefore cannot see. The failure is silent — every rule
        that reads such a column just sees blanks and never fires — so the
        signature is checked here and reported by name.

        Only mapped CADENCE roles are checked: an empty "_extra" column is
        somebody's scratch space, not a rule input.
        """
        total = len(tab.rows)
        if total < max(1, config.CADENCE_EMPTY_COLUMN_MIN_ROWS):
            return
        ratio = max(0.0, float(config.CADENCE_EMPTY_COLUMN_RATIO))
        for role in CADENCE_ROLES:
            if role not in tab.role_to_col:
                continue
            filled = sum(1 for r in tab.rows if str(r.get(role) or "").strip())
            if filled > total * ratio:
                continue
            header = tab.headers[tab.role_to_col[role]] if tab.role_to_col[role] < len(tab.headers) else role
            log.warning(
                "[gtm] tab %r: the %s column (%r) is filled on %d of %d rows. It MAY BE "
                "COLOR-CODED — this bot reads cell VALUES only and cannot see fills, so "
                "every rule that reads %s will see a blank. It needs a text value in "
                "each cell (or point GTM_COLUMN_MAP[%r][%r] at the column that has one).",
                tab.title, role, header, filled, total, role, tab.kind, role,
            )

    def _log_full_schema(self, which: str, entries: list[dict]) -> None:
        """Log the FULL discovered schema of every tab, once per spreadsheet.

        Every tab, hidden or not, recognised or not, with its complete header
        list. This is a diagnosis tool with one job: when a rule stops firing
        because someone renamed a column, the answer is in the startup log
        instead of in a debugging session.
        """
        log.info(
            "[gtm.schema] ===== %s: %d tab(s) discovered (%d hidden) =====",
            which, len(entries), sum(1 for e in entries if e["hidden"]),
        )
        for e in entries:
            log.info(
                "[gtm.schema] %s%-22r kind=%-17s rows=%-5s headers(%d)=[%s]",
                "HIDDEN " if e["hidden"] else "       ",
                e["title"], e["kind"] or "(unrecognised)",
                e["rows"] if e["rows"] is not None else "-",
                len(e["headers"]),
                " | ".join(str(h) for h in e["headers"]) or "(no header row)",
            )
            if e["kind"] and e["mapped"]:
                log.info(
                    "[gtm.schema]        %r mapped roles: %s",
                    e["title"],
                    ", ".join(f"{role}->{col!r}" for role, col in sorted(e["mapped"].items())),
                )
            if e["kind"] and e["unmapped"]:
                log.info(
                    "[gtm.schema]        %r columns carried as _extra (no role): %s",
                    e["title"], ", ".join(repr(h) for h in e["unmapped"]),
                )
        log.info("[gtm.schema] ===== end of %s schema =====", which)

    def _parse_values(
        self, title: str, values: list[list], *, read_at: float
    ) -> Optional[Tab]:
        """One worksheet's raw values → a Tab, or None when it isn't one of the
        three kinds."""
        if not values:
            return None
        hidx = self._header_row_index(values)
        headers = [str(h).strip() for h in values[hidx]]
        kind = self._detect_kind(title, headers)
        if kind is None:
            log.debug("[gtm] tab %r doesn't match a known kind; skipping", title)
            return None

        role_to_col = self._map_headers(kind, headers)
        col_to_role = {v: k for k, v in role_to_col.items()}

        rows: list[dict] = []
        for r, raw in enumerate(values[hidx + 1:], start=hidx + 2):  # 1-based sheet row
            cells = [str(c).strip() for c in raw]
            if not any(cells):
                continue
            row: dict = {"_row": r, "_extra": {}}
            for i, header in enumerate(headers):
                value = cells[i] if i < len(cells) else ""
                role = col_to_role.get(i)
                if role:
                    row[role] = value
                elif header:
                    row["_extra"][header] = value
            # A tracker/priority row with no company is a spacer or a total line,
            # not a deal — carrying it would let it be counted and flagged.
            if kind in (MASTER, TRACKER, PRIORITY) and not (row.get("company") or "").strip():
                continue
            rows.append(row)

        return Tab(
            title=title, kind=kind, headers=headers,
            role_to_col=role_to_col, rows=rows, read_at=read_at,
            header_row=hidx + 1,
        )

    # -- reads -------------------------------------------------------------

    def read(self, which: str = ORIGINAL, *, force: bool = False) -> dict[str, Tab]:
        """Every recognised tab of one spreadsheet, as {kind: Tab}.

        Cached for `SHEET_CACHE_SECONDS` to stay inside the API quota. On a
        failed read, the last good copy is returned — check `Tab.age_seconds` and
        say so — because a sheet outage must never take the bot down. Raises
        SheetAccessError only when there is NO cached copy to fall back to.
        """
        now = time.time()
        with self._lock:
            cached = {
                kind: tabs[0] for (sk, kind), tabs in self._cache.items()
                if sk == which and tabs
            }
            if cached and not force:
                freshest = min(tab.read_at for tab in cached.values())
                if now - freshest < max(1, config.SHEET_CACHE_SECONDS):
                    return cached

        try:
            sh = self._open(which)
            discovered = self._worksheets(sh)          # [(title, hidden)], hidden included
            titles = [title for title, _hidden in discovered]
            hidden_by_title = {title: hidden for title, hidden in discovered}
            by_title = self._batch_values(sh, titles)
            read_at = time.time()
            groups: dict[str, list[Tab]] = {}
            unrecognised: list[str] = []
            schema_entries: list[dict] = []
            for title in titles:
                raw = by_title.get(title) or []
                try:
                    tab = self._parse_values(title, raw, read_at=read_at)
                except Exception:
                    log.exception("[gtm] tab %r failed to parse; skipping it", title)
                    tab = None
                # The schema entry is built for EVERY tab, recognised or not —
                # an unrecognised tab's headers are exactly what you need to see
                # when working out why it wasn't recognised.
                if tab is not None:
                    mapped = {
                        role: (tab.headers[i] if i < len(tab.headers) else f"col{i}")
                        for role, i in tab.role_to_col.items()
                    }
                    taken = set(tab.role_to_col.values())
                    schema_entries.append({
                        "title": title, "hidden": hidden_by_title.get(title, False),
                        "kind": tab.kind, "rows": len(tab.rows), "headers": tab.headers,
                        "mapped": mapped,
                        "unmapped": [h for i, h in enumerate(tab.headers)
                                     if i not in taken and str(h).strip()],
                    })
                else:
                    hidx = self._header_row_index(raw) if raw else 0
                    headers = [str(h).strip() for h in (raw[hidx] if raw else [])]
                    schema_entries.append({
                        "title": title, "hidden": hidden_by_title.get(title, False),
                        "kind": None, "rows": max(0, len(raw) - hidx - 1) if raw else 0,
                        "headers": headers, "mapped": {}, "unmapped": [],
                    })
                if tab is None:
                    unrecognised.append(title)
                    continue
                groups.setdefault(tab.kind, []).append(tab)
                key = (which, tab.title)
                if key not in self._schema_logged:
                    log.info(
                        "[gtm] %s %s%s", which, tab.schema_line(),
                        " [HIDDEN TAB]" if hidden_by_title.get(title) else "",
                    )
                    self._schema_logged.add(key)

            # The full schema, and the colour-coding check, are STARTUP work:
            # once per spreadsheet per process, not on every cache miss.
            full_key = (which, "_full_schema")
            if config.GTM_LOG_FULL_SCHEMA and full_key not in self._schema_logged:
                self._log_full_schema(which, schema_entries)
                for tab in groups.get(MASTER, []) or groups.get(TRACKER, []):
                    try:
                        self._warn_colour_coded(tab)
                    except Exception:
                        log.debug("[gtm] colour-coding check failed", exc_info=True)
                self._schema_logged.add(full_key)

            if MASTER not in groups and (which, "_no_master") not in self._schema_logged:
                log.warning(
                    "[gtm] %s has no master tab: none of %s exists in it. The daily "
                    "cadence will fall back to the outreach tracker, which is the "
                    "pre-master source. Set GTM_MASTER_TAB_TITLES to the real tab name.",
                    which, ", ".join(repr(x) for x in (config.GTM_MASTER_TAB_TITLES or [])),
                )
                self._schema_logged.add((which, "_no_master"))

            # SEVERAL tabs can legitimately share a kind — the real playbook has
            # a master lead list, a Fortune-500 list and a vertical list, all of
            # them genuine prospect-priority tabs. Keep them all; the biggest is
            # the primary, and the tools search across every one.
            for kind, found in groups.items():
                found.sort(key=lambda t: len(t.rows), reverse=True)
                if len(found) > 1:
                    log.info(
                        "[gtm] %d %s tabs: %s (primary: %r)",
                        len(found), kind, ", ".join(repr(t.title) for t in found),
                        found[0].title,
                    )

            with self._lock:
                for kind, found in groups.items():
                    self._cache[(which, kind)] = found

            if unrecognised and (which, "_unrecognised") not in self._schema_logged:
                log.info(
                    "[gtm] %s: %d tab(s) matched no known kind and are ignored: %s. "
                    "If one of these should be read, set GTM_COLUMN_MAP for it.",
                    which, len(unrecognised),
                    ", ".join(repr(t) for t in unrecognised[:25]),
                )
                self._schema_logged.add((which, "_unrecognised"))

            log.info(
                "[gtm] read %s: %s",
                which,
                ", ".join(
                    f"{k}={sum(len(t.rows) for t in v)} rows across {len(v)} tab(s)"
                    for k, v in groups.items()
                ) or "no known tabs",
            )
            return {k: v[0] for k, v in groups.items()}

        except SheetAccessError as e:
            with self._lock:
                cached = {
                    kind: tabs[0] for (sk, kind), tabs in self._cache.items()
                    if sk == which and tabs
                }
            if cached:
                log.warning(
                    "[gtm] read of %s failed (%s); answering from the cached copy "
                    "(%.0fs old)",
                    which, e, max(t.age_seconds for t in cached.values()),
                )
                return cached
            log.error("[gtm] read of %s failed with no cache to fall back to: %s", which, e)
            raise

    def tab(self, kind: str, which: str = ORIGINAL) -> Optional[Tab]:
        """The PRIMARY tab of a kind (the one with the most rows), or None when
        the sheet has no such tab. Propagates SheetAccessError when there is
        nothing cached."""
        return self.read(which).get(kind)

    def tabs_of(self, kind: str, which: str = ORIGINAL) -> list[Tab]:
        """EVERY tab of a kind, largest first.

        Several tabs can legitimately share a kind — the playbook keeps a master
        lead list, a Fortune-500 list and a vertical list, all of them real
        prospect-priority tabs. A question like "which P1s have we never
        contacted" has to look at all of them, so anything answering across
        prospects uses this rather than `tab()`.
        """
        self.read(which)  # ensure the cache is populated / fresh
        with self._lock:
            return list(self._cache.get((which, kind)) or [])

    def cadence_tab(self, which: str = ORIGINAL) -> tuple[Optional[Tab], str]:
        """THE tab the daily cadence runs against, and which one it turned out to be.

        Returns (tab, source) where source is "master_data" or "outreach_tracker".
        The master tab wins whenever it exists; the tracker is the fallback so
        that a sheet which hasn't grown a "Master data" tab yet still produces a
        cadence rather than an empty digest. `(None, "")` when neither exists.
        """
        tabs = self.read(which)
        master = tabs.get(MASTER)
        if master is not None:
            return master, MASTER
        tracker_tab = tabs.get(TRACKER)
        if tracker_tab is not None:
            return tracker_tab, TRACKER
        return None, ""

    def staleness_note(self, tab: Tab) -> str:
        """The note an answer must carry when it came from cache rather than a
        live read. "" when the data is fresh."""
        age = tab.age_seconds
        if age <= max(1, config.SHEET_CACHE_SECONDS) * 2:
            return ""
        mins = int(age // 60)
        if mins < 60:
            when = f"{max(1, mins)} minute(s) ago"
        else:
            when = f"{mins // 60} hour(s) ago"
        return (
            f"(read from the sheet {when} — I couldn't reach the Sheets API just now, "
            "so this may be out of date)"
        )

    # -- writes ------------------------------------------------------------
    # The whole write surface of this bot. One column, one cell at a time.

    def _refuse_if_read_only(self, target: str) -> str:
        """"" when `target` may be written to, else why it may not be.

        THE THIRD LOCK on the researcher/buyer mapping sheet. That sheet is
        read-only by policy, and mapping_sheet.py enforces it twice over — no
        write method, and a read-only OAuth scope. This is the third: if anyone
        ever points GTM_SHEET_COPY_ID (or _ORIGINAL_ID) at a read-only sheet id,
        every write path here refuses rather than discovering the mistake by
        making it. config.validate() also forces SHEET_WRITE_TARGET=off in that
        case; this covers the runtime path regardless of whether validate() ran.
        """
        sid = self.sheet_id(target)
        if config.is_read_only_sheet_id(sid):
            return (
                f"REFUSING to write: the {target} sheet id ({sid}) is the researcher/buyer "
                "mapping sheet, which is READ-ONLY by policy — the bot never writes to it. "
                "Point GTM_SHEET_COPY_ID at the sandbox playbook instead."
            )
        return ""

    def ensure_bot_column(self, which: Optional[str] = None) -> tuple[Optional[str], str]:
        """Make sure the tracker tab has the bot's deadline column, appending it
        at the FAR RIGHT if missing.

        Returns (column_letter, note). The column letter is None when writing is
        off, the sheet is unreachable, or there is no tracker tab — all of which
        are reported, never raised.

        Appending a header is the one structural change the bot ever makes, and
        it only ever happens at the right-hand edge, so no existing column moves
        and no existing cell changes.
        """
        if config.SHEET_WRITE_TARGET == "off":
            return None, "sheet writing is off (SHEET_WRITE_TARGET=off)"
        target = which or config.SHEET_WRITE_TARGET

        refusal = self._refuse_if_read_only(target)
        if refusal:
            log.error("[gtm] %s", refusal)
            return None, refusal

        # The CACHED schema first. Every deadline write calls this, and forcing a
        # fresh read each time meant one write cost a full re-read of the
        # spreadsheet — the fast path here is "the column is already there",
        # which needs no request at all.
        def _tracker(force: bool):
            tabs = self.read(target, force=force)
            return tabs.get(TRACKER)

        try:
            tracker = _tracker(False)
            if tracker is None:
                return None, f"the {target} sheet has no outreach tracker tab"
            existing = tracker.role_to_col.get("bot_deadline")
            if existing is None:
                # Not in the cached schema. Re-read before appending: a duplicate
                # column is a structural change to someone else's spreadsheet and
                # is not undoable from here, so it is worth one request to be sure.
                tracker = _tracker(True)
                if tracker is None:
                    return None, f"the {target} sheet has no outreach tracker tab"
                existing = tracker.role_to_col.get("bot_deadline")
            if existing is not None:
                return _col_letter(existing), ""
        except SheetAccessError as e:
            return None, f"cannot reach the {target} sheet: {e}"

        try:
            sh = self._open(target)
            ws = sh.worksheet(tracker.title)
            col_index0 = len(tracker.headers)
            # Widen the grid first if the header would land outside it.
            if col_index0 >= ws.col_count:
                ws.add_cols(col_index0 - ws.col_count + 1)
            letter = _col_letter(col_index0)
            header_row = tracker.header_row
            ws.update(
                values=[[config.BOT_DEADLINE_COLUMN]],
                range_name=f"{letter}{header_row}",
                value_input_option="USER_ENTERED",
            )
        except SheetAccessError as e:
            return None, f"cannot add the column: {e}. {e.remedy}".strip()
        except Exception as e:
            # Translate with writing=True so a 403 here is reported as "shared
            # read-only, needs Editor" rather than the read-side "not shared".
            err = self._translate(e, target, self.sheet_id(target), writing=True)
            log.error("[gtm] could not append the bot deadline column: %s", err)
            if err.remedy:
                log.error("[gtm] ACTION REQUIRED: %s", err.remedy)
            return None, f"{err}. {err.remedy}".strip()

        log.info(
            "[gtm] appended column %r at %s on the %s tracker",
            config.BOT_DEADLINE_COLUMN, letter, target,
        )
        # Invalidate: the schema just changed under the cache.
        with self._lock:
            self._cache.pop((target, TRACKER), None)
        return letter, ""

    def write_deadline_cell(
        self, *, row: int, value: str, which: Optional[str] = None,
        expect_company: str = "",
    ) -> dict:
        """Write ONE cell in the bot's deadline column. The only write path.

        `expect_company` is a SAFETY INTERLOCK, and callers should always pass
        it. Row numbers are discovered on the ORIGINAL (the source of truth) but
        written to whatever SHEET_WRITE_TARGET names, normally the sandbox copy.
        That is only sound while the two stay row-aligned, and nothing enforces
        that they do — one inserted or sorted row in the copy and every
        subsequent write lands on the wrong company, silently and in a column
        nobody is watching. So the target row is checked to still name the
        company we think it does, and the write is refused if it doesn't.

        Returns {ok, cell, target, error}. Never raises: a failed write is
        reported and audited, and the deadline still stands in SQLite — the sheet
        is a mirror, not the record.
        """
        if config.SHEET_WRITE_TARGET == "off":
            return {"ok": False, "cell": "", "target": "off",
                    "error": "sheet writing is off (SHEET_WRITE_TARGET=off)"}
        target = which or config.SHEET_WRITE_TARGET

        refusal = self._refuse_if_read_only(target)
        if refusal:
            log.error("[gtm] %s", refusal)
            return {"ok": False, "cell": "", "target": target, "error": refusal}

        letter, note = self.ensure_bot_column(target)
        if letter is None:
            return {"ok": False, "cell": "", "target": target, "error": note}

        cell = f"{letter}{int(row)}"

        if expect_company:
            mismatch = self._row_mismatch(target, row=int(row), expect_company=expect_company)
            if mismatch:
                log.error("[gtm] REFUSING to write %s!%s: %s", target, cell, mismatch)
                return {"ok": False, "cell": cell, "target": target, "error": mismatch}
        try:
            sh = self._open(target)
            tracker_title = None
            tabs = self.read(target)
            if TRACKER in tabs:
                tracker_title = tabs[TRACKER].title
            ws = sh.worksheet(tracker_title) if tracker_title else sh.sheet1
            # values.update on a SINGLE cell range. Not append_row, not a range
            # spanning other columns — the range is one cell by construction.
            ws.update(
                values=[[value]],
                range_name=cell,
                value_input_option="USER_ENTERED",
            )
        except SheetAccessError as e:
            log.error("[gtm] write to %s!%s failed: %s", target, cell, e)
            return {"ok": False, "cell": cell, "target": target,
                    "error": f"{e}. {e.remedy}".strip()}
        except Exception as e:
            err = self._translate(e, target, self.sheet_id(target), writing=True)
            log.error("[gtm] write to %s!%s failed: %s", target, cell, err)
            if err.remedy:
                log.error("[gtm] ACTION REQUIRED: %s", err.remedy)
            return {"ok": False, "cell": cell, "target": target,
                    "error": f"{err}. {err.remedy}".strip()}

        log.info("[gtm] WROTE %s!%s = %r", target, cell, value)
        # Patch the cached row in place instead of dropping the tracker cache.
        # Dropping it made every write cost a full re-read of the spreadsheet on
        # the next call, so writing deadlines for a dozen rows re-read the whole
        # playbook a dozen times. We know exactly which cell changed and to what.
        self._patch_cached_cell(target, row=int(row), value=value)
        return {"ok": True, "cell": cell, "target": target, "error": ""}

    def _row_mismatch(self, target: str, *, row: int, expect_company: str) -> str:
        """"" when the target sheet's row really is `expect_company`, else why not."""
        try:
            tabs = self.read(target)
        except SheetAccessError as e:
            return f"could not confirm row {row} on the {target} sheet before writing: {e}"
        tab = tabs.get(TRACKER)
        if tab is None:
            return f"the {target} sheet has no outreach tracker tab to write to"
        actual = next(
            (r.get("company") for r in tab.rows if r.get("_row") == row), None
        )
        if actual is None:
            return (
                f"row {row} does not exist on the {target} tracker (the sheets have "
                f"drifted out of alignment); refusing to write"
            )
        if normalise_header(actual) != normalise_header(expect_company):
            return (
                f"row {row} on the {target} tracker is {actual!r}, not {expect_company!r} "
                f"— the sheets have drifted out of alignment; refusing to write"
            )
        return ""

    def _patch_cached_cell(self, target: str, *, row: int, value: str) -> None:
        """Reflect a written deadline into the cached tracker rows."""
        with self._lock:
            for tab in self._cache.get((target, TRACKER)) or []:
                for cached_row in tab.rows:
                    if cached_row.get("_row") == row:
                        cached_row["bot_deadline"] = value
                        return

    def read_cell(self, cell: str, which: Optional[str] = None) -> Optional[str]:
        """Read back one cell, by A1 address, from the tracker tab. Used by the
        write round-trip check — a write nobody verified is a write nobody can
        trust.

        RAISES SheetAccessError when the read itself failed. It deliberately does
        not swallow the error into a None: "the API would not answer" and "the
        cell holds something else" are opposite conclusions about a write, and
        collapsing them into one falsy value made a read-quota 429 look like a
        corrupted write.
        """
        target = which or config.SHEET_WRITE_TARGET
        if target == "off":
            raise SheetAccessError(
                "sheet reading is off (SHEET_WRITE_TARGET=off)",
                remedy="Set SHEET_WRITE_TARGET=copy to use the sandbox.",
            )
        try:
            sh = self._open(target)
            tabs = self.read(target)
            ws = sh.worksheet(tabs[TRACKER].title) if TRACKER in tabs else sh.sheet1
            return str(ws.acell(cell).value or "")
        except SheetAccessError:
            raise
        except Exception as e:
            raise self._translate(e, target, self.sheet_id(target)) from e

    def verify_write_roundtrip(
        self, *, row: int, value: str, expect_company: str = ""
    ) -> dict:
        """Write one cell, read it back, confirm it matches.

        Proves the whole write path end to end — auth, the column, the cell
        address, the value — against whatever SHEET_WRITE_TARGET points at.
        Returns {ok, cell, wrote, read_back, error}.
        """
        result = self.write_deadline_cell(row=row, value=value, expect_company=expect_company)
        if not result["ok"]:
            return {"ok": False, "cell": result["cell"], "wrote": value,
                    "read_back": None, "error": result["error"]}

        # The read-back is retried, because the failure this hits in practice is
        # a 429 on the per-minute read quota rather than a bad write — and
        # reporting that as "the write did not stick" would be a lie about the
        # one thing this check exists to establish.
        cell = result["cell"]
        last: Optional[SheetAccessError] = None
        for attempt in range(_ROUNDTRIP_READ_ATTEMPTS):
            try:
                got = self.read_cell(cell)
            except SheetAccessError as e:
                last = e
                if not e.transient or attempt == _ROUNDTRIP_READ_ATTEMPTS - 1:
                    break
                delay = _ROUNDTRIP_BACKOFF_SECONDS * (2 ** attempt)
                log.warning(
                    "[gtm] read-back of %s failed (%s); retrying in %ds (%d/%d)",
                    cell, e, delay, attempt + 1, _ROUNDTRIP_READ_ATTEMPTS,
                )
                time.sleep(delay)
                continue

            ok = (got or "").strip() == value.strip()
            if not ok and attempt < _ROUNDTRIP_READ_ATTEMPTS - 1:
                # Sheets is read-after-write consistent in principle and not
                # always in practice: the read issued immediately after the
                # update came back with an EMPTY cell that a second later held
                # the written value. Retry before calling a good write bad.
                delay = _ROUNDTRIP_BACKOFF_SECONDS * (2 ** attempt)
                log.warning(
                    "[gtm] %s read back as %r, not %r yet; retrying in %ds (%d/%d)",
                    cell, got, value, delay, attempt + 1, _ROUNDTRIP_READ_ATTEMPTS,
                )
                time.sleep(delay)
                continue
            log.info(
                "[gtm] write round-trip %s: %s wrote=%r read_back=%r",
                "PASSED" if ok else "FAILED", cell, value, got,
            )
            return {
                "ok": ok,
                "cell": cell,
                "wrote": value,
                "read_back": got,
                "error": "" if ok else "the value read back did not match what was written",
            }

        log.error(
            "[gtm] write round-trip INCONCLUSIVE: %s was written but could not be "
            "read back: %s", cell, last,
        )
        return {
            "ok": False,
            "cell": cell,
            "wrote": value,
            "read_back": None,
            "error": (
                f"the cell was written but could not be read back to confirm it: {last}. "
                f"{getattr(last, 'remedy', '')}"
            ).strip(),
        }


# One instance, shared. Holds a lazily-built client and a cache, no connection.
SHEETS = GTMSheets()


def _self_test() -> int:
    """`python -m gtm_sheet` — access check, schema dump, and the SANDBOX write
    round-trip, without booting Discord.

    This exists so the write path can be re-verified after any change to it, on
    demand, by whoever is deploying. It refuses to run against the original: the
    round-trip writes a real cell, and the only sheet this bot may experiment on
    is the sandbox copy.
    """
    import argparse

    parser = argparse.ArgumentParser(description="GTM sheet self-test")
    parser.add_argument(
        "--write", action="store_true",
        help="also run the write round-trip against the sandbox copy",
    )
    parser.add_argument(
        "--row", type=int, default=0,
        help="tracker sheet row to round-trip (default: the first data row)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    print(f"service account: {SHEETS.service_account_email or '(no key readable)'}")
    access = SHEETS.check_access()
    failed = False
    for which in (ORIGINAL, COPY):
        info = access.get(which, {})
        if info.get("ok"):
            print(f"  {which:<9} OK   {info.get('title')!r} ({len(info.get('tabs') or [])} tabs)")
        else:
            failed = True
            print(f"  {which:<9} FAIL {info.get('error')}")
            if info.get("remedy"):
                print(f"             -> {info['remedy']}")
    if failed:
        return 1

    for which in (ORIGINAL, COPY):
        print(f"\n{which} schema:")
        try:
            for kind in (TRACKER, POSITIONING, PRIORITY):
                for tab in SHEETS.tabs_of(kind, which):
                    print(f"  {tab.schema_line()}")
        except SheetAccessError as e:
            print(f"  could not read: {e}")
            return 1

    if not args.write:
        print("\n(skipping the write round-trip — pass --write to run it)")
        return 0

    if config.SHEET_WRITE_TARGET != COPY:
        print(
            f"\nrefusing to round-trip with SHEET_WRITE_TARGET={config.SHEET_WRITE_TARGET!r}: "
            f"this test writes a real cell and may only touch the sandbox copy."
        )
        return 2

    tab = SHEETS.tab(TRACKER, COPY)
    if tab is None or not tab.rows:
        print("\nno tracker tab with rows on the copy — nothing to round-trip")
        return 1
    row = args.row or tab.rows[0]["_row"]
    company = next((r.get("company") for r in tab.rows if r["_row"] == row), "?")

    import deadlines as _dl
    value = _dl.sheet_cell_value(
        _dl.add_working_days(_dl.today_ist(), config.OUTREACH_FOLLOWUP_DAYS),
        rule="write round-trip self-test",
    )
    print(f"\nround-trip: {tab.title!r} row {row} ({company!r}) <- {value!r}")
    result = SHEETS.verify_write_roundtrip(row=row, value=value, expect_company=company)
    print(f"  cell      {result['cell']}")
    print(f"  wrote     {result['wrote']!r}")
    print(f"  read back {result['read_back']!r}")
    print(f"  {'PASSED' if result['ok'] else 'FAILED: ' + result['error']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_self_test())

"""GOOGLE DRIVE + DOCS + SHEETS-REST — the bot's SECOND Google credential.

WHY THIS EXISTS SEPARATELY FROM gtm_sheet.py. That module deliberately holds a
spreadsheets-only token: "no Drive scope — the bot has no business listing,
creating, sharing or deleting files, and a narrower token is a narrower blast
radius if the key ever leaks." That reasoning is still right for the GTM
playbook, so it is untouched. Two later requirements genuinely need Drive:

  THE TO-DO SHEET   The bot CREATES one spreadsheet of its own and SHARES it
                    with the team. A service-account-owned file is invisible to
                    every human until it is shared, so "create" without "share"
                    would produce a sheet nobody can open — the failure mode
                    this module's `share` exists to prevent.
  THE STRATEGY DOC  A human-owned Google Doc, shared TO the service account.
                    Reading it is a Drive export, not a Sheets call.

So there are two clients, with two scope sets, and the wide one lives here:

    gtm_sheet.SHEETS   spreadsheets                      the human GTM sheets
    drive (this file)  spreadsheets + drive.file
                       + drive.readonly                  the bot's own sheet,
                                                         and read-only Drive

    drive.file      create and manage ONLY files this app created. It is what
                    lets the bot make the to-do sheet and share it, and it is
                    deliberately not `drive` (full): the bot can never modify a
                    file it did not create.
    drive.readonly  read a file a human shared with the service account. This
                    is the only way to read the strategy doc, and it is
                    read-only by construction — there is no code path in this
                    file that writes to a file the bot did not create.

NO googleapiclient. The Drive and Sheets REST APIs are called directly over a
`google.auth` AuthorizedSession, which is already a dependency (google-auth
ships it). Adding google-api-python-client for six endpoints would be a new
transitive dependency tree for no new capability.

EVERY FAILURE IS A DriveError WITH A REMEDY. The two errors that actually happen
— "the key file isn't there" and "nobody shared the doc with the service
account" — are indistinguishable from "the feature is broken" unless the message
says which, so each one names the fix and the address to share with.
"""
import json
import logging
import re
import threading
from typing import Optional

import config

log = logging.getLogger(__name__)

# google-auth is optional at IMPORT time, exactly as gspread is in gtm_sheet: a
# box without it must still start the bot and still answer every non-Drive
# question. The import error is reported through the normal awaiting-access path.
try:
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession

    _IMPORT_ERROR: Optional[str] = None
except Exception as e:  # pragma: no cover - depends on the environment
    Credentials = None
    AuthorizedSession = None
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"

# See the module docstring for why each one is here.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",    # write the bot's own sheet
    "https://www.googleapis.com/auth/drive.file",      # create + share THAT sheet
    "https://www.googleapis.com/auth/drive.readonly",  # read docs shared TO us
]

# Google's wording when a project has never enabled an API. Matched on the
# message rather than the status code, because the status code is a plain 403.
_API_DISABLED_RE = re.compile(
    r"has not been used in project|API (?:is )?(?:has been )?disabled", re.IGNORECASE
)

DRIVE_V3 = "https://www.googleapis.com/drive/v3"
SHEETS_V4 = "https://sheets.googleapis.com/v4/spreadsheets"

# Google Docs / Sheets / Slides are not files with bytes; they are exported.
# Anything else is fetched with alt=media.
_EXPORT_AS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


class DriveError(Exception):
    """A Drive/Sheets failure WITH the fix attached.

    `remedy` is shown to humans (a startup log line, a source status, a digest
    note), so it must name an action — "share it with <address> as Viewer" —
    rather than restate the error.
    """

    def __init__(self, message: str, *, remedy: str = "", transient: bool = False):
        super().__init__(message)
        # Kept separately from the remedy so a caller can report "what went
        # wrong" and "how to fix it" in different places without string
        # surgery on the joined form.
        self.message = message
        self.remedy = remedy
        self.transient = transient

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} {self.remedy}".strip() if self.remedy else base


_lock = threading.RLock()
_session = None


def service_account_email() -> str:
    """The address a human has to share a doc with. Read from the key file each
    time so the instruction can never name the wrong account."""
    path = config.GOOGLE_SERVICE_ACCOUNT_JSON
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return str(json.load(f).get("client_email") or "")
    except Exception:
        log.debug("[drive] could not read client_email from the key file", exc_info=True)
        return ""


def share_instruction(what: str, *, role: str = "Viewer") -> str:
    """The exact fix for a 404/403 on a human-owned file.

    Deliberately ASCII — this string is logged, and a Windows console on a
    cp1252 code page raises UnicodeEncodeError on arrows and dashes, which would
    turn "here is how to fix your config" into a crash.
    """
    email = service_account_email() or "the bot's service account"
    return (
        f"Share {what} with {email} as {role} "
        f"(open it, click Share, paste the address, set {role}, Send)."
    )


def available() -> tuple[bool, str]:
    """(usable, why_not). Checked before anything reaches the network so a
    missing library and a missing share never look like the same problem."""
    if AuthorizedSession is None:
        return False, f"the Google auth libraries aren't installed ({_IMPORT_ERROR})"
    if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
        return False, "no service-account key is configured (GOOGLE_SERVICE_ACCOUNT_JSON)"
    return True, ""


def session():
    """The authorised HTTP session, built once. Raises DriveError with a remedy
    rather than a bare exception, because every caller reports to a human."""
    global _session
    with _lock:
        if _session is not None:
            return _session
        ok, why = available()
        if not ok:
            raise DriveError(
                why,
                remedy=(
                    "Run: pip install google-auth"
                    if AuthorizedSession is None
                    else "Set GOOGLE_SERVICE_ACCOUNT_JSON to the key file path."
                ),
            )
        path = config.GOOGLE_SERVICE_ACCOUNT_JSON
        try:
            creds = Credentials.from_service_account_file(path, scopes=SCOPES)
        except FileNotFoundError:
            raise DriveError(
                f"the service-account key file {path!r} does not exist",
                remedy=f"Put the key at {path}, or fix GOOGLE_SERVICE_ACCOUNT_JSON.",
            )
        except Exception as e:
            raise DriveError(
                f"the service-account key could not be loaded ({type(e).__name__})",
                remedy=f"Check that {path!r} is a valid service-account JSON key.",
            )
        _session = AuthorizedSession(creds)
        log.info(
            "[drive] authorised as %s with scopes %s",
            service_account_email() or "(unknown account)", ", ".join(SCOPES),
        )
        return _session


def _timeout() -> int:
    return max(5, int(config.DRIVE_TIMEOUT_SECONDS))


def _request(method: str, url: str, *, what: str, share_as: str = "", **kwargs):
    """One HTTP call, with the API's errors translated into something a human
    can act on. `share_as` names the access level to ask for when the answer is
    "I cannot see that file", which is by far the most common failure."""
    sess = session()
    kwargs.setdefault("timeout", _timeout())
    try:
        resp = sess.request(method, url, **kwargs)
    except Exception as e:
        raise DriveError(
            f"could not reach Google to {what} ({type(e).__name__})",
            remedy="Check network access from this box, then retry.",
            transient=True,
        )
    if resp.status_code in (200, 201, 204):
        return resp
    try:
        body = str(resp.json().get("error", {}).get("message", ""))[:300]
    except Exception:
        body = (resp.text or "")[:300]
    # THE FIRST-DEPLOY FAILURE, and it looks like nothing else. A project that
    # has never called the Drive API answers every request with a 403 whose text
    # is "Google Drive API has not been used in project N before or it is
    # disabled" — which reads as a permissions problem and is not one. Nothing
    # about sharing, keys or scopes fixes it; somebody has to click Enable once.
    # So it gets its own remedy, carrying the console URL Google itself names.
    if resp.status_code == 403 and _API_DISABLED_RE.search(body):
        url = ""
        m = re.search(r"https://console\.developers\.google\.com/\S+?(?=[\s\\\"']|$)", body)
        if m:
            url = m.group(0).rstrip(".")
        raise DriveError(
            f"could not {what}: the Google Drive API is not enabled for this project",
            remedy=(
                "Enable the Google Drive API once, in the Cloud console"
                + (f": {url}" if url else " for this service account's project")
                + ", wait a minute for it to propagate, then restart the bot. No key, "
                "scope or sharing change will fix this — it is a project setting."
            ),
        )
    if resp.status_code in (403, 404) and share_as:
        raise DriveError(
            f"could not {what}: Google returned {resp.status_code} ({body})",
            remedy=share_instruction("that file", role=share_as),
        )
    if resp.status_code in (429, 500, 502, 503, 504):
        raise DriveError(
            f"could not {what}: Google returned {resp.status_code} ({body})",
            remedy="Transient on Google's side — the next run will retry.",
            transient=True,
        )
    raise DriveError(
        f"could not {what}: Google returned {resp.status_code} ({body})",
        remedy="Check the file id and the service account's access.",
    )


# -- reading -----------------------------------------------------------------

_META_FIELDS = "id,name,mimeType,modifiedTime,createdTime,webViewLink,owners(emailAddress)"


def file_metadata(file_id: str) -> dict:
    """Name, type, when it was last MODIFIED, and its link.

    `modifiedTime` is the whole reason the strategy doc has a currency rule at
    all: "the plan hasn't been touched since 12 Aug" is a fact read off Drive,
    not a judgement the bot makes.
    """
    file_id = (file_id or "").strip()
    if not file_id:
        raise DriveError("no file id given", remedy="Set the id in the environment.")
    resp = _request(
        "GET", f"{DRIVE_V3}/files/{file_id}",
        what=f"read the metadata for {file_id}",
        share_as="Viewer",
        params={"fields": _META_FIELDS, "supportsAllDrives": "true"},
    )
    return resp.json()


def export_text(file_id: str, *, mime_type: str = "") -> str:
    """A document's text. Google-native files are EXPORTED (a Doc has no bytes
    to download); everything else is fetched with alt=media.

    Returns "" for a Google file type with no text form, rather than raising: a
    strategy deck in Slides is a legitimate thing to be told about, not a crash.
    """
    file_id = (file_id or "").strip()
    if not mime_type:
        mime_type = str(file_metadata(file_id).get("mimeType") or "")
    export_as = _EXPORT_AS.get(mime_type)
    if export_as:
        resp = _request(
            "GET", f"{DRIVE_V3}/files/{file_id}/export",
            what=f"export {file_id} as text", share_as="Viewer",
            params={"mimeType": export_as},
        )
    elif mime_type.startswith("application/vnd.google-apps."):
        log.info("[drive] %s is a %s — no text form to export", file_id, mime_type)
        return ""
    else:
        resp = _request(
            "GET", f"{DRIVE_V3}/files/{file_id}",
            what=f"download {file_id}", share_as="Viewer",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
    resp.encoding = resp.encoding or "utf-8"
    return resp.text or ""


def permissions(file_id: str) -> list[dict]:
    """Who can currently open a file. Used to report the to-do sheet's sharing
    honestly instead of assuming a share that may have failed."""
    resp = _request(
        "GET", f"{DRIVE_V3}/files/{file_id}/permissions",
        what=f"list who can see {file_id}", share_as="Viewer",
        params={"fields": "permissions(id,type,role,emailAddress)",
                "supportsAllDrives": "true"},
    )
    return list(resp.json().get("permissions") or [])


# -- creating and sharing (drive.file: only files this bot made) --------------


def create_spreadsheet(title: str) -> dict:
    """Create ONE spreadsheet owned by the service account.

    Returns {"id", "url", "title"}. The caller MUST share it — see `share`. A
    service-account-owned file lives in a Drive no human can browse, so an
    unshared sheet is a sheet that does not exist as far as the team is
    concerned.
    """
    # CREATED THROUGH THE DRIVE API, not through Sheets' own `spreadsheets.create`.
    # Both produce the same file, but they fail differently, and the difference
    # matters more than it sounds: Sheets' create needs Drive underneath, and on
    # a project where the Drive API has never been enabled it answers with a
    # bare "The caller does not have permission" — which sends whoever is
    # debugging it to re-check the key, the scopes and the sharing, none of
    # which are the problem. The Drive endpoint returns Google's real message,
    # naming the API and the console URL that turns it on.
    resp = _request(
        "POST", f"{DRIVE_V3}/files", what=f"create the spreadsheet {title!r}",
        params={"fields": "id,name,webViewLink", "supportsAllDrives": "true"},
        json={"name": title, "mimeType": "application/vnd.google-apps.spreadsheet"},
    )
    data = resp.json()
    sid = str(data.get("id") or "")
    if not sid:
        raise DriveError(
            f"Google accepted the create for {title!r} but returned no spreadsheet id",
            remedy="Retry; if it repeats, create the sheet by hand and set TODO_SHEET_ID.",
        )
    url = str(data.get("webViewLink") or sheet_url(sid))
    log.info("[drive] created spreadsheet %r id=%s", title, sid)
    return {"id": sid, "url": url, "title": title}


def share(file_id: str, email: str, *, role: str = "writer", notify: bool = False) -> dict:
    """Give one person access to a file THIS BOT created.

    `notify=False` by default: the link is posted in the sales channel, and a
    Google notification email to every team member on every redeploy is noise
    the channel post already covers.

    Raises DriveError on failure — the caller reports which addresses did and
    did not get access, because "shared" is the entire difference between a
    working to-do sheet and an invisible one.
    """
    email = (email or "").strip()
    if not email:
        raise DriveError("no email address to share with", remedy="Set TEAM_SHARE_EMAILS.")
    resp = _request(
        "POST", f"{DRIVE_V3}/files/{file_id}/permissions",
        what=f"share {file_id} with {email}",
        params={"sendNotificationEmail": "true" if notify else "false",
                "supportsAllDrives": "true"},
        json={"type": "user", "role": role, "emailAddress": email},
    )
    log.info("[drive] shared %s with %s as %s", file_id, email, role)
    try:
        return resp.json()
    except Exception:
        return {"emailAddress": email, "role": role}


def sheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def doc_url(file_id: str) -> str:
    return f"https://docs.google.com/document/d/{file_id}/edit"


# -- the Sheets values API (the bot's own sheet only) -------------------------


def sheet_values(spreadsheet_id: str, a1: str) -> list[list]:
    """Read a range. Missing trailing cells come back as SHORT ROWS — every
    caller pads, because a row whose Status cell was never typed is a normal
    row, not a broken one."""
    resp = _request(
        "GET", f"{SHEETS_V4}/{spreadsheet_id}/values/{a1}",
        what=f"read {a1} from {spreadsheet_id}", share_as="Editor",
        params={"majorDimension": "ROWS"},
    )
    return list(resp.json().get("values") or [])


def sheet_update(spreadsheet_id: str, a1: str, rows: list[list]) -> dict:
    """Overwrite a range. Used ONLY for the header row of the bot's own sheet."""
    resp = _request(
        "PUT", f"{SHEETS_V4}/{spreadsheet_id}/values/{a1}",
        what=f"write {a1} in {spreadsheet_id}", share_as="Editor",
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": rows},
    )
    return resp.json()


def sheet_append(spreadsheet_id: str, a1: str, rows: list[list]) -> dict:
    """APPEND rows, never overwrite.

    This is the only bulk write the to-do refresh performs, and it is
    append-only by construction rather than by convention: humans edit Status
    and Notes in that sheet, and a write that could land on an occupied row
    would silently discard somebody's edit.
    """
    resp = _request(
        "POST", f"{SHEETS_V4}/{spreadsheet_id}/values/{a1}:append",
        what=f"append {len(rows)} row(s) to {spreadsheet_id}", share_as="Editor",
        params={"valueInputOption": "USER_ENTERED",
                "insertDataOption": "INSERT_ROWS"},
        json={"values": rows},
    )
    return resp.json()


def format_header(spreadsheet_id: str, *, columns: int) -> None:
    """Freeze and bold row 1. Cosmetic, and deliberately best-effort: a sheet
    with an unfrozen header is perfectly usable, so this never fails a
    creation."""
    try:
        _request(
            "POST", f"{SHEETS_V4}/{spreadsheet_id}:batchUpdate",
            what=f"format the header of {spreadsheet_id}", share_as="Editor",
            json={"requests": [
                {"updateSheetProperties": {
                    "properties": {"sheetId": 0,
                                   "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }},
                {"repeatCell": {
                    "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1,
                              "startColumnIndex": 0, "endColumnIndex": max(1, columns)},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }},
            ]},
        )
    except DriveError:
        log.info("[drive] could not format the header of %s; the sheet is still usable",
                 spreadsheet_id, exc_info=True)

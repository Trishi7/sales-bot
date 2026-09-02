"""THE TO-DO SHEET — "Membrane Sales To-Dos", created, shared, and refreshed.

ONE SHEET, OWNED BY THE BOT, EDITED BY HUMANS. The bot creates it once, shares
it with the team, and thereafter only ever APPENDS rows to it. Status and Notes
belong to whoever is doing the work; nothing here writes to them and nothing
here deletes a row. That asymmetry is the whole contract:

    the bot   appends action items it read out of the meeting notes
    humans    edit Status and Notes freely, reorder, annotate, close

SHARING IS NOT A NICETY, IT IS THE FEATURE. A spreadsheet created by a service
account is owned by that service account, and it lives in a Drive no human can
browse or search. Until it is shared it is invisible — not "hard to find",
invisible. So creation and sharing are one operation here, the share failures
are reported per address rather than as a single boolean, and the link is posted
in the sales channel because a link nobody has is the same as no sheet.

THE ID IS REMEMBERED, NOT RE-DERIVED. After creation the spreadsheet id is
stored in the bot's own config store (the `meta` table, the same place the
once-a-day digest marker lives) and read back from there on every boot, so a
restart re-opens the same sheet instead of creating a second one. TODO_SHEET_ID
in the environment overrides it, which is how you point the bot at a sheet
somebody made by hand. THE STORED ID IS AUTHORITATIVE — the "create" path runs
only when neither the env nor the store has one, so there is exactly one sheet
no matter how many times the bot restarts.

THE WEEKLY REFRESH (TODO_REFRESH_DAY, default Friday). Action items are read out
of that week's meeting notes by `meetings.action_items`, deduped against the
rows already in the sheet, and appended. The digest for that day carries ONE
line — "To-do sheet updated: +N new · <link>" — because a second unprompted
message a week is still a second unprompted message.

EVERY APPEND IS AUDITED. One `todo_appended` line per row in audit.jsonl, plus a
`todo_refresh` summary, so a row that appears in the sheet can always be traced
back to the meeting it came from and the run that wrote it.

CITATIONS ARE STRUCTURAL HERE. "Source meeting" is a real column, filled from
`meetings.citation`, so the rule that a meeting-derived claim names its meeting
is enforced by the schema rather than by remembering to add it.

NO DISCORD IN THIS FILE. It returns text and dicts; bot.py decides what becomes
a digest section. There is no send path here and there must never be one.
"""
import logging
import re
from datetime import date
from typing import Iterable, Optional

import config
import deadlines as dl
import drive
import gtm_sheet
import meetings
import state

log = logging.getLogger(__name__)

# The columns, in order, exactly as specified. Changing this list changes the
# sheet's schema, so it is the one place it is written down.
HEADERS = (
    "#", "To-do", "Owner", "Source meeting", "Date raised", "Due", "Status", "Notes",
)

COL_INDEX = {name: i for i, name in enumerate(HEADERS)}

# Where the spreadsheet id lives between restarts. Same store as the digest's
# once-a-day marker — see the module docstring.
META_KEY_ID = "todo_sheet_id"
META_KEY_ANNOUNCED = "todo_sheet_announced"
META_KEY_LAST_REFRESH = "todo_sheet_last_refresh"

STATUS_OPEN = "Open"

# A row is CLOSED when a human typed one of these into Status. Everything else —
# including a blank cell, a "wip", a question mark — is open, because the sheet
# is a to-do list and the default state of a to-do is "not done".
_CLOSED = {
    "done", "closed", "complete", "completed", "cancelled", "canceled",
    "dropped", "wontfix", "won't fix", "not doing", "n/a", "na", "obsolete",
}

_RANGE_ALL = "A:H"
_RANGE_HEADER = "A1:H1"


# -- identity ----------------------------------------------------------------


def enabled() -> bool:
    return bool(config.TODO_SHEET_ENABLED)


def sheet_id(db=None) -> str:
    """The spreadsheet id: the env override first, then what was stored after
    creation. "" means "no sheet yet" and is what triggers the create path."""
    if config.TODO_SHEET_ID:
        return config.TODO_SHEET_ID
    if db is None:
        return ""
    try:
        return (db.get_meta(META_KEY_ID) or "").strip()
    except Exception:
        log.exception("[todos] could not read the stored sheet id")
        return ""


def link(db=None) -> str:
    sid = sheet_id(db)
    return drive.sheet_url(sid) if sid else ""


# -- creation and sharing ----------------------------------------------------


def _share_all(sid: str) -> tuple[list[str], list[dict]]:
    """Share the sheet as EDITOR with every address in TEAM_SHARE_EMAILS.

    Returns (shared_ok, failures). Per address, because "sharing failed" is not
    actionable and "vaishnavi@… bounced: no such Google account" is. Editor,
    not Viewer: the humans own Status and Notes, and a read-only to-do list is
    a report, not a list.
    """
    ok: list[str] = []
    failed: list[dict] = []
    for email in config.TEAM_SHARE_EMAILS:
        try:
            drive.share(sid, email, role="writer", notify=config.TODO_SHARE_NOTIFY)
            ok.append(email)
        except drive.DriveError as e:
            log.error("[todos] could NOT share the to-do sheet with %s: %s", email, e)
            failed.append({"email": email, "error": str(e), "remedy": e.remedy})
        except Exception as e:
            log.exception("[todos] unexpected failure sharing with %s", email)
            failed.append({"email": email, "error": f"{type(e).__name__}: {e}", "remedy": ""})
    return ok, failed


def ensure(db) -> dict:
    """Make sure the sheet exists, is shared, and has its header row.

    Returns
        {ok, created, id, url, shared[], failed[], error, remedy, already}

    IDEMPOTENT. With an id already known this re-shares (Drive treats a repeat
    share as a no-op update) and returns `created=False`, so a redeploy never
    produces a second sheet and never loses access after somebody removes a
    permission by accident.

    NEVER RAISES. A Drive outage on boot must not stop the bot connecting; the
    caller reports the failure and tries again next time.
    """
    out = {"ok": False, "created": False, "id": "", "url": "", "shared": [],
           "failed": [], "error": "", "remedy": "", "already": False}
    if not enabled():
        out["error"] = "TODO_SHEET_ENABLED=false"
        return out

    ok, why = drive.available()
    if not ok:
        out["error"] = why
        out["remedy"] = "Install google-auth and set GOOGLE_SERVICE_ACCOUNT_JSON."
        return out

    sid = sheet_id(db)
    if not sid:
        try:
            made = drive.create_spreadsheet(config.TODO_SHEET_TITLE)
        except drive.DriveError as e:
            out["error"] = e.message
            out["remedy"] = e.remedy
            return out
        except Exception as e:
            log.exception("[todos] the to-do sheet could not be created")
            out["error"] = f"the to-do sheet could not be created ({type(e).__name__}: {e})"
            return out
        sid = made["id"]
        out["created"] = True
        # STORED BEFORE ANYTHING ELSE. If the share or the header write fails,
        # the next run must find THIS sheet rather than create another one.
        try:
            db.set_meta(META_KEY_ID, sid)
        except Exception:
            log.exception(
                "[todos] CREATED sheet %s but could not store its id — a restart may "
                "create a second sheet. Set TODO_SHEET_ID=%s in the environment.", sid, sid,
            )
        state.audit(
            "todo_sheet_created",
            reason="first run: the team's to-do sheet did not exist yet",
            sheet_id=sid, title=config.TODO_SHEET_TITLE, url=drive.sheet_url(sid),
        )
    else:
        out["already"] = True

    out["id"] = sid
    out["url"] = drive.sheet_url(sid)

    # The header row, written every time. It is one cheap call and it repairs a
    # sheet somebody cleared, which otherwise silently breaks the dedup.
    try:
        existing = drive.sheet_values(sid, _RANGE_HEADER)
        if not existing or [str(c).strip() for c in existing[0]] != list(HEADERS):
            drive.sheet_update(sid, _RANGE_HEADER, [list(HEADERS)])
            drive.format_header(sid, columns=len(HEADERS))
            log.info("[todos] wrote the header row on %s", sid)
    except drive.DriveError as e:
        out["error"] = e.message
        out["remedy"] = e.remedy
        return out
    except Exception as e:
        log.exception("[todos] could not write the header row")
        out["error"] = f"the header row could not be written ({type(e).__name__}: {e})"
        return out

    shared, failed = _share_all(sid)
    out["shared"], out["failed"] = shared, failed
    out["ok"] = True
    if failed and not shared:
        out["error"] = (
            "the sheet exists but could not be shared with anyone, so nobody can open it"
        )
        out["remedy"] = failed[0].get("remedy", "")
    state.audit(
        "todo_sheet_shared",
        reason="a service-account-owned sheet is invisible until it is shared",
        sheet_id=sid, url=out["url"], shared=shared,
        failed=[f["email"] for f in failed],
    )
    log.info(
        "[todos] sheet %s ready (%s) — shared with %d of %d address(es)%s",
        sid, "created" if out["created"] else "already existed",
        len(shared), len(config.TEAM_SHARE_EMAILS),
        f"; FAILED: {[f['email'] for f in failed]}" if failed else "",
    )
    return out


# -- reading -----------------------------------------------------------------


def _pad(row: list, width: int) -> list:
    """Sheets omits trailing empty cells, so a row whose Notes were never typed
    comes back short. Padding here is what stops every reader indexing off the
    end of a perfectly normal row."""
    row = list(row or [])
    return row + [""] * max(0, width - len(row))


def _dedup_key(task: str, owner: str = "") -> str:
    """What makes two to-dos "the same one".

    Task text plus owner, normalised — punctuation, case and spacing removed.
    Task ALONE would merge "[Vaishnavi] send the deck" and "[Kushal] send the
    deck", which are two jobs; owner alone merges everything a person owns. The
    text is clipped because a human who reworded the tail of a line in the sheet
    has not created a new to-do.
    """
    return gtm_sheet.normalise_header(str(task or ""))[:120] + "|" + \
        gtm_sheet.normalise_header(str(owner or ""))


def read_rows(db) -> dict:
    """Every row in the sheet as
        {ok, rows[{n, task, owner, source_meeting, date_raised, due, status,
                   notes, open, row}], error, remedy, url}

    Never raises. An unreadable sheet is reported, not guessed at.
    """
    out = {"ok": False, "rows": [], "error": "", "remedy": "", "url": link(db)}
    sid = sheet_id(db)
    if not sid:
        out["error"] = "the to-do sheet has not been created yet"
        out["remedy"] = "It is created on the next boot, or set TODO_SHEET_ID."
        return out
    try:
        values = drive.sheet_values(sid, _RANGE_ALL)
    except drive.DriveError as e:
        out["error"] = e.message
        out["remedy"] = e.remedy
        return out
    except Exception as e:
        log.exception("[todos] could not read the to-do sheet")
        out["error"] = f"the to-do sheet could not be read ({type(e).__name__}: {e})"
        return out

    rows = []
    for i, raw in enumerate(values[1:], start=2):  # row 1 is the header
        cells = _pad(raw, len(HEADERS))
        task = str(cells[COL_INDEX["To-do"]]).strip()
        if not task:
            continue
        status = str(cells[COL_INDEX["Status"]]).strip()
        rows.append({
            "n": str(cells[COL_INDEX["#"]]).strip(),
            "task": task,
            "owner": str(cells[COL_INDEX["Owner"]]).strip(),
            "source_meeting": str(cells[COL_INDEX["Source meeting"]]).strip(),
            "date_raised": str(cells[COL_INDEX["Date raised"]]).strip(),
            "due": str(cells[COL_INDEX["Due"]]).strip(),
            "status": status or STATUS_OPEN,
            "notes": str(cells[COL_INDEX["Notes"]]).strip(),
            "open": status.strip().lower() not in _CLOSED,
            "row": i,
        })
    out["ok"] = True
    out["rows"] = rows
    log.info("[todos] read %d row(s) from %s (%d open)",
             len(rows), sid, sum(1 for r in rows if r["open"]))
    return out


def open_items(db, *, limit: Optional[int] = None) -> dict:
    """The open to-dos, oldest first — the answer to "@bot show the to-dos".

    Oldest first because the point of the list is what has been sitting there,
    not what was added this morning.
    """
    data = read_rows(db)
    if not data["ok"]:
        return data
    rows = [r for r in data["rows"] if r["open"]]
    rows.sort(key=lambda r: (r.get("date_raised") or "9999", r.get("row", 0)))
    cap = max(1, int(limit if limit is not None else config.TODO_SHOW_MAX))
    data["rows"] = rows[:cap]
    data["open_total"] = len(rows)
    data["shown"] = len(data["rows"])
    return data


def format_items(data: dict, *, db=None) -> str:
    """The to-do list as Discord-ready text: the LINK first, then the open rows.

    The link leads because it is the thing somebody asking for the to-dos
    actually wants; the rows are the answer to "and what is on it".
    """
    url = data.get("url") or link(db)
    if not data.get("ok"):
        why = data.get("error") or "the to-do sheet is not available"
        remedy = data.get("remedy") or ""
        return f"I can't read the to-do sheet right now: {why}. {remedy}".strip()

    rows = data.get("rows") or []
    total = data.get("open_total", len(rows))
    head = f"**{config.TODO_SHEET_TITLE}** — {url}"
    if not rows:
        return head + "\nNothing open on it right now."
    lines = [head, f"{total} open item(s)" + (f", showing {len(rows)}" if len(rows) < total else "") + ":"]
    for r in rows:
        bits = [r["task"]]
        if r["owner"]:
            bits.append(f"owner {r['owner']}")
        if r["due"]:
            bits.append(f"due {r['due']}")
        if r["source_meeting"]:
            # THE CITATION. It is a column in the sheet and it stays attached
            # when a row is quoted into a channel.
            bits.append(f"({r['source_meeting']})")
        lines.append("• " + " · ".join(bits))
    return "\n".join(lines)


# -- the weekly refresh ------------------------------------------------------


def is_refresh_day(today: date) -> bool:
    """Is today the day the sheet is refreshed? TODO_REFRESH_DAY, default fri."""
    return today.weekday() == config.todo_refresh_weekday()


def _next_number(rows: list[dict]) -> int:
    """The next value for the "#" column. Reads the largest number already
    there rather than counting rows, so a row a human deleted doesn't cause a
    duplicate id."""
    best = 0
    for r in rows:
        digits = re.sub(r"\D", "", str(r.get("n") or ""))
        if digits:
            best = max(best, int(digits))
    return best + 1


def refresh(db, *, today: Optional[date] = None,
            companies: Optional[Iterable[str]] = None,
            days: Optional[int] = None) -> dict:
    """Extract this week's action items and APPEND the new ones.

    Returns {ok, added, skipped, items[], url, error, remedy, considered}.

    DEDUP AGAINST THE SHEET, NOT AGAINST A LOCAL MEMORY. The sheet is the
    record; a bot-side "already appended" table would drift the first time
    somebody deleted a row, and then the item would never come back.

    NEVER DELETES, NEVER EDITS. Every write is an append (see
    `drive.sheet_append`), so a human's Status edit cannot be clobbered by a
    refresh landing on their row.
    """
    today = today or dl.today_ist()
    out = {"ok": False, "added": 0, "skipped": 0, "items": [], "url": link(db),
           "error": "", "remedy": "", "considered": 0}
    if not enabled():
        out["error"] = "TODO_SHEET_ENABLED=false"
        return out

    existing = read_rows(db)
    if not existing["ok"]:
        out["error"], out["remedy"] = existing["error"], existing["remedy"]
        return out

    seen = {_dedup_key(r["task"], r["owner"]) for r in existing["rows"]}
    # Also dedup on the task alone against UNOWNED rows already in the sheet, so
    # an item that arrives owned later doesn't duplicate the unowned one.
    seen_taskonly = {_dedup_key(r["task"]) for r in existing["rows"] if not r["owner"]}

    try:
        found = meetings.action_items(days=days, companies=companies)
    except Exception:
        log.exception("[todos] could not extract action items from the meeting notes")
        out["error"] = "the meeting notes could not be read for action items"
        return out
    out["considered"] = len(found)

    number = _next_number(existing["rows"])
    fresh: list[dict] = []
    for item in found:
        key = _dedup_key(item["task"], item["owner"])
        if key in seen or _dedup_key(item["task"]) in seen_taskonly:
            out["skipped"] += 1
            continue
        seen.add(key)
        fresh.append(item)
        if len(fresh) >= max(1, int(config.TODO_MAX_NEW_PER_REFRESH)):
            log.info(
                "[todos] %d new item(s) found; appending the first %d this run",
                len(found) - out["skipped"], len(fresh),
            )
            break

    if not fresh:
        out["ok"] = True
        log.info("[todos] refresh for %s: nothing new (%d considered, %d already on the sheet)",
                 dl.iso(today), out["considered"], out["skipped"])
        return out

    rows = []
    for item in fresh:
        rows.append([
            number,
            item["task"],
            item["owner"],
            item["source_meeting"],   # the rule-2 citation, as a column
            item["date_raised"],
            item["due"],
            STATUS_OPEN,
            "",                        # Notes: the humans' column, never written
        ])
        number += 1

    sid = sheet_id(db)
    try:
        drive.sheet_append(sid, _RANGE_ALL, rows)
    except drive.DriveError as e:
        out["error"] = e.message
        out["remedy"] = e.remedy
        return out
    except Exception as e:
        log.exception("[todos] the append failed")
        out["error"] = f"the append failed ({type(e).__name__}: {e})"
        return out

    for item, row in zip(fresh, rows):
        state.audit(
            "todo_appended",
            reason="a commitment made in a meeting became a row on the to-do sheet",
            sheet_id=sid, number=row[0], task=item["task"], owner=item["owner"] or None,
            source_meeting=item["source_meeting"], date_raised=item["date_raised"],
            due=item["due"] or None, note_path=(item.get("note") or {}).get("path"),
        )
    state.audit(
        "todo_refresh",
        reason="the weekly to-do refresh",
        sheet_id=sid, url=out["url"], date=dl.iso(today),
        considered=out["considered"], added=len(rows), skipped=out["skipped"],
    )
    try:
        db.set_meta(META_KEY_LAST_REFRESH, dl.iso(today))
    except Exception:
        log.exception("[todos] could not record the refresh date")

    out["ok"] = True
    out["added"] = len(rows)
    out["items"] = fresh
    log.info("[todos] refresh for %s: +%d new, %d already on the sheet, %d considered",
             dl.iso(today), out["added"], out["skipped"], out["considered"])
    return out


# -- the digest's lines ------------------------------------------------------


def refresh_line(result: dict, *, db=None) -> str:
    """THE one line the refresh day's digest carries. Exactly as specified:
    "To-do sheet updated: +N new · <link>"."""
    if not result.get("ok"):
        why = result.get("error") or "it could not be updated"
        return f"To-do sheet NOT updated: {why}. {result.get('remedy', '')}".strip()
    url = result.get("url") or link(db)
    if not result.get("added"):
        return f"To-do sheet checked: nothing new this week · {url}"
    return f"To-do sheet updated: +{result['added']} new · {url}"


def needs_announcement(db) -> bool:
    """Has the link been posted in the channel yet?

    PERSISTED, NOT INFERRED FROM "did I just create it". The sheet is created at
    boot and the link goes out with the next digest, which may be hours later
    and on the far side of a restart. Keying the announcement on the create call
    would lose the link exactly when the bot was restarted between the two —
    and an unannounced sheet is an invisible one.
    """
    if not enabled():
        return False
    if not sheet_id(db):
        return False
    try:
        return not (db.get_meta(META_KEY_ANNOUNCED) or "").strip()
    except Exception:
        log.exception("[todos] could not read the announcement marker; not announcing")
        return False


def mark_announced(db, *, on_date: str = "") -> None:
    """Record that the link has gone out. Called as a DIGEST EFFECT — only after
    the message actually posted, so a refused send doesn't burn the one
    announcement the sheet gets."""
    try:
        db.set_meta(META_KEY_ANNOUNCED, on_date or dl.iso(dl.today_ist()))
    except Exception:
        log.exception(
            "[todos] posted the to-do link but could not record it — it may be "
            "announced again tomorrow"
        )


def announcement_lines(ensured: dict) -> list[str]:
    """The FIRST-RUN announcement: the sheet exists, who can open it, the link.

    This is the "post the link in the sales channel" step. It goes out as a
    SECTION OF THE DAILY DIGEST rather than as a message of its own — see the
    one-message rule in digest.py — and it is one of the two things that can
    make an otherwise empty day post, because a link nobody receives is the
    same as no sheet at all.
    """
    if not ensured.get("ok") or not ensured.get("url"):
        return []
    lines = [
        f"The team to-do sheet: **{config.TODO_SHEET_TITLE}** — {ensured['url']}",
    ]
    if ensured.get("shared"):
        lines.append(
            "Shared as Editor with " + ", ".join(ensured["shared"])
            + ". Action items from the meeting notes are appended every "
            + config.TODO_REFRESH_DAY.capitalize()
            + "; Status and Notes are yours to edit and I never touch them."
        )
    if ensured.get("failed"):
        lines.append(
            "I could NOT share it with "
            + ", ".join(f["email"] for f in ensured["failed"])
            + " — "
            + (ensured["failed"][0].get("remedy") or "check the address is a Google account")
            + "."
        )
    return lines

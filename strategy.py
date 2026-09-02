"""THE STRATEGY DOC — the plan the bot checks outreach against, and its currency.

WHAT CHANGED. This module used to be a stub: `sources.StrategyDoc` reported
awaiting-access, `deadlines.cadence_from_strategy` was handed None, and the two
things the doc exists for — "is the plan current" and "is what we're doing still
what we said we'd do" — were unenforceable. STRATEGY_DOC_ID now points at the
HUMAN-OWNED strategy doc (Vaishnavi's edited version of the v2 draft), so this
is the reader, and both enforcements run against it.

HUMAN-OWNED IS THE POINT. The bot has read-only access to a document somebody
else edits. It never writes to it, never suggests writing to it, and has no code
path that could: `drive.py` reads it through the `drive.readonly` scope. When
the doc changes, the bot's answers change on the next cache expiry — nobody has
to tell it.

TWO ENFORCEMENTS, and they are different questions:

  CURRENCY (stale-doc)   Drive's `modifiedTime` against STRATEGY_STALE_DAYS.
                         "The plan hasn't been revised since 12 Aug (21 days)"
                         is a fact read off Drive, not a judgement. Strategy
                         currency is one of the things this bot was asked to
                         enforce, and it is the easiest one to enforce honestly.

  OUTREACH-vs-PLAN       What the doc names as a target, against where outreach
                         actually went. Two findings, in both directions:
                           OFF-PLAN   we are working a segment the plan does
                                      not name.
                           UNWORKED   the plan names a segment and nothing went
                                      to it in the window.
                         Neither is an accusation — a plan can be out of date
                         and the pipeline right. Both are stated as the
                         comparison they are, with the counts behind them.

HOW THE TARGETS ARE READ, and the limits of it. The doc is prose written by a
human, not a schema. So the extraction is deliberately conservative: it takes
the bullet and short lines under a heading that says TARGET / ICP / SEGMENT /
VERTICAL / PRIORIT*, and nothing else. If the doc has no such heading, the bot
says so — "I can read the strategy doc but it has no target/ICP section I could
match outreach against" — instead of guessing at a plan from the whole text and
then reporting drift against its own guess. A wrong plan check is worse than
none: it would send the team to defend outreach against a target nobody set.

MATCHING IS TWO-SIDED AND USES REAL VOCABULARY. A target matches a row when the
target's words appear in the row's industry, vertical or use case — the row's
own cells, never an inference about what a company "is". A row's vertical is
off-plan only when it appears NOWHERE in the doc, not merely outside the target
section, because plans mention their segments in the prose around the list.
"""
import logging
import os
import re
import threading
from datetime import date, datetime, timezone
from typing import Optional

import config
import deadlines as dl
import drive
import gtm_sheet

log = logging.getLogger(__name__)

# The headings whose bullets are read as TARGETS. Matched on a normalised line,
# so "Target Segments:" and "TARGET SEGMENTS" are the same heading.
_TARGET_HEADING_RE = re.compile(
    r"^(?:\d+[.)]\s*)?(?:target|targets|icp|ideal customer|segments?|verticals?|"
    r"priorit(?:y|ies)|focus|who we (?:sell|go) (?:to|after))\b",
    re.IGNORECASE,
)
# A heading of any kind — used to know where the target section ENDS. A section
# that ran to the bottom of the doc would swallow the whole plan as "targets".
_ANY_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+\S|\d+[.)]\s+\S|[A-Z][A-Za-z /&-]{2,48}:\s*$)"
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•·•]|\d+[.)])\s+(?P<text>.+?)\s*$")

# A target phrase has to be a NAME, not a sentence. Anything longer than this is
# a policy statement that happens to sit under the heading, and matching rows
# against it would produce noise in both directions.
_TARGET_MAX_WORDS = 6
_TARGET_MIN_CHARS = 3

# Words that carry no segment meaning and would match almost any row.
_TARGET_STOPWORDS = {
    "and", "the", "for", "with", "our", "all", "any", "new", "other", "others",
    "etc", "more", "high", "low", "priority", "target", "targets", "segment",
    "segments", "vertical", "verticals", "icp", "focus", "customers", "clients",
    "companies", "accounts", "leads", "prospects",
}

_lock = threading.RLock()
_cache: dict = {"at": None, "data": None}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def configured() -> bool:
    return bool(config.STRATEGY_DOC_ID or config.STRATEGY_DOC_FILE)


def _read_local(path: str) -> dict:
    """A strategy doc on disk. Kept because STRATEGY_DOC_FILE predates the Drive
    reader and a local copy is the obvious fallback when Drive is down."""
    try:
        st = os.stat(path)
    except OSError as e:
        return {
            "ok": False, "source": "file", "text": "", "modified": None,
            "error": f"the strategy file {path!r} could not be read ({type(e).__name__})",
            "remedy": "Fix STRATEGY_DOC_FILE, or set STRATEGY_DOC_ID to the Drive doc.",
        }
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return {
            "ok": False, "source": "file", "text": "", "modified": None,
            "error": f"the strategy file {path!r} could not be opened ({type(e).__name__})",
            "remedy": "Check the file's permissions.",
        }
    return {
        "ok": True, "source": "file", "text": text, "name": os.path.basename(path),
        "url": "", "id": "",
        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).date().isoformat(),
        "error": "", "remedy": "",
    }


def _read_drive(doc_id: str) -> dict:
    """The human-owned doc, over the read-only Drive scope."""
    try:
        meta = drive.file_metadata(doc_id)
        text = drive.export_text(doc_id, mime_type=str(meta.get("mimeType") or ""))
    except drive.DriveError as e:
        return {
            "ok": False, "source": "drive", "text": "", "modified": None,
            "error": e.message,
            "remedy": e.remedy or drive.share_instruction("the strategy doc", role="Viewer"),
        }
    except Exception as e:
        return {
            "ok": False, "source": "drive", "text": "", "modified": None,
            "error": f"the strategy doc could not be read ({type(e).__name__})",
            "remedy": "Check STRATEGY_DOC_ID and the service account's access.",
        }

    modified = str(meta.get("modifiedTime") or "")[:10] or None
    if not text.strip():
        return {
            "ok": False, "source": "drive", "text": "", "modified": modified,
            "name": str(meta.get("name") or ""),
            "url": str(meta.get("webViewLink") or drive.doc_url(doc_id)), "id": doc_id,
            "error": (
                f"the strategy doc ({meta.get('mimeType')}) has no text form I can export"
            ),
            "remedy": "Point STRATEGY_DOC_ID at a Google Doc, or set STRATEGY_DOC_FILE.",
        }
    return {
        "ok": True, "source": "drive", "text": text, "modified": modified,
        "name": str(meta.get("name") or ""),
        "url": str(meta.get("webViewLink") or drive.doc_url(doc_id)), "id": doc_id,
        "error": "", "remedy": "",
    }


def read(*, force: bool = False) -> dict:
    """The strategy doc, cached for STRATEGY_CACHE_MINUTES.

    Returns {ok, text, modified (ISO date or None), name, url, id, source,
    error, remedy}. NEVER raises: an unreadable plan costs the plan check and
    the currency line, and nothing else in the digest.

    A FAILED READ IS CACHED TOO, for the same interval. Without that, every
    question about the plan retries a 403 against Drive and the answer arrives
    seconds late for a failure whose fix is a human sharing a document.
    """
    if not configured():
        return {
            "ok": False, "source": "", "text": "", "modified": None,
            "error": "no strategy doc is configured (STRATEGY_DOC_ID / STRATEGY_DOC_FILE)",
            "remedy": "Set STRATEGY_DOC_ID to the doc's Drive id.",
        }

    ttl = max(1, int(config.STRATEGY_CACHE_MINUTES)) * 60
    with _lock:
        at, data = _cache["at"], _cache["data"]
        if not force and data is not None and at and (_now() - at).total_seconds() < ttl:
            return dict(data)

    if config.STRATEGY_DOC_ID:
        data = _read_drive(config.STRATEGY_DOC_ID)
    else:
        data = _read_local(config.STRATEGY_DOC_FILE)

    with _lock:
        _cache["at"] = _now()
        _cache["data"] = dict(data)

    if data["ok"]:
        log.info(
            "[strategy] read %s (%s, %d chars), last revised %s",
            data.get("name") or "the strategy doc", data["source"],
            len(data["text"]), data.get("modified") or "unknown",
        )
    else:
        log.warning("[strategy] NOT readable: %s %s", data["error"], data["remedy"])
    return dict(data)


def text() -> Optional[str]:
    """The doc body, or None. This is what `deadlines.cadence_from_strategy`
    reads, so a readable doc's stated cadence now outranks the env defaults."""
    data = read()
    return data["text"] if data["ok"] else None


def last_updated() -> Optional[str]:
    """ISO date the plan was last revised, or None."""
    data = read()
    return data.get("modified") if data["ok"] else None


# -- currency ----------------------------------------------------------------


def age_days(*, today: Optional[date] = None) -> Optional[int]:
    """Calendar days since the plan was last revised. Calendar, not working,
    days: a plan going stale over a long weekend is still going stale."""
    raw = last_updated()
    if not raw:
        return None
    try:
        when = date.fromisoformat(raw)
    except ValueError:
        return None
    return max(0, ((today or dl.today_ist()) - when).days)


def is_stale(*, today: Optional[date] = None) -> bool:
    limit = int(config.STRATEGY_STALE_DAYS)
    if limit <= 0:
        return False
    age = age_days(today=today)
    return age is not None and age > limit


def currency_line(*, today: Optional[date] = None) -> str:
    """One line about how current the plan is. "" when the doc is unreadable —
    the caller reports that separately, and saying "the plan may be stale" about
    a document you cannot open would be a guess dressed as a finding."""
    data = read()
    if not data["ok"]:
        return ""
    age = age_days(today=today)
    if age is None:
        return "The strategy doc is readable but Drive reports no revision date for it."
    limit = int(config.STRATEGY_STALE_DAYS)
    when = data.get("modified")
    if limit > 0 and age > limit:
        return (
            f"STRATEGY IS STALE — last revised {when} ({age} days ago), past the "
            f"{limit}-day rule. Outreach is being checked against a plan nobody has "
            "touched since then."
        )
    return f"Strategy last revised {when} ({age} days ago)."


# -- the plan's targets ------------------------------------------------------


def _target_phrases(body: str) -> list[dict]:
    """[{phrase, heading}] — the short names listed under a target heading.

    Conservative by design; see the module docstring. Lines are taken only while
    inside a target section, only when they are a bullet or a short line, and
    only when what is left after stripping stopwords still says something.

    WHERE THE SECTION ENDS is the part that has to be got right, because a doc
    written in plain prose has no closing tag and the next heading is often just
    a bare word on its own line ("Cadence", "Motion") that no heading regex
    catches. Running past it turns the following section's TITLE into a target
    and then reports "the plan names Cadence and nothing went to it" — a finding
    about the parser dressed up as a finding about the team. So the shape of the
    list itself is used as the boundary:

        a BULLETED list ends at the first non-bullet line;
        an UNBULLETED list ends at the first blank line.

    Both are how a human reads it, and both fail closed: an early stop loses a
    target (the check then simply says less), a late stop invents one.
    """
    out: list[dict] = []
    heading = ""
    inside = False
    bulleted: Optional[bool] = None   # what kind of list this section turned out to be
    collected = 0

    for raw in (body or "").splitlines():
        line = raw.strip()
        bullet = _BULLET_RE.match(raw)

        if not line:
            # A blank line closes an unbulleted list, and is ignored inside a
            # bulleted one (bullets are routinely double-spaced).
            if inside and bulleted is False:
                inside = False
            continue

        bare = re.sub(r"^#{1,6}\s+", "", line).strip().rstrip(":").strip()
        if not bullet and _TARGET_HEADING_RE.match(bare) and len(bare) <= 60:
            inside, heading, bulleted, collected = True, bare, None, 0
            continue
        if not inside:
            continue

        if bulleted is None:
            bulleted = bool(bullet)
        elif bulleted and not bullet:
            inside = False       # the bulleted list is over
            continue
        if not bullet and (_ANY_HEADING_RE.match(line) or collected == 0 and len(line) > 80):
            inside = False
            continue

        phrase = (bullet.group("text") if bullet else line).strip()
        # A bullet often reads "Academic labs — they buy tooling": the name is
        # the part before the dash, and the rest is rationale.
        phrase = re.split(r"\s+[—–-]\s+|:\s+", phrase)[0].strip().strip(".,;")
        words = [w for w in re.split(r"\W+", phrase) if w]
        if not words or len(words) > _TARGET_MAX_WORDS:
            continue
        if len(phrase) < _TARGET_MIN_CHARS:
            continue
        meaningful = [w for w in words if w.lower() not in _TARGET_STOPWORDS]
        if not meaningful:
            continue
        out.append({"phrase": phrase, "heading": heading})
        collected += 1
        if len(out) >= max(1, int(config.STRATEGY_MAX_TARGETS)):
            break
    return out


def targets() -> list[dict]:
    """The plan's named targets, or [] when the doc has no target section."""
    data = read()
    if not data["ok"]:
        return []
    found = _target_phrases(data["text"])
    log.info("[strategy] %d target phrase(s) read from the plan%s",
             len(found), "" if found else " — no TARGET/ICP/SEGMENT section found")
    return found


# -- outreach vs the plan ----------------------------------------------------


def _row_segments(row: dict) -> str:
    """The row's OWN words about what it is. Never an inference about the
    company — only cells a human filled in."""
    return gtm_sheet.normalise_header(
        " ".join(
            str(row.get(role) or "")
            for role in ("industry", "poc_vertical", "use_case")
        )
    )


def _touched_in_window(row: dict, *, since: date, today: date) -> bool:
    """Did anything actually go out to this row in the window? Read off the
    tracker's date columns, so "no outreach" means no dated touch, not an
    absence of enthusiasm."""
    for role in ("first_contacted", "intro_date", "last_followed_up"):
        parsed = dl.parse_date(row.get(role))
        if parsed and since <= parsed <= today:
            return True
    return False


def plan_check(rows: list[dict], *, since: date, today: Optional[date] = None) -> dict:
    """Compare the window's outreach against the plan's targets.

    Returns {"ok", "reason", "lines", "off_plan", "unworked", "targets",
    "touched"}. `lines` is what the digest prints; everything else is there so
    an answer can be more specific when somebody asks.

    Every finding is a COUNT OF ROWS, because a claim about drift with no
    number behind it is an opinion. Nothing is asserted about a company the
    tracker does not carry.
    """
    today = today or dl.today_ist()
    data = read()
    if not data["ok"]:
        return {"ok": False, "reason": data["error"], "remedy": data.get("remedy", ""),
                "lines": [], "off_plan": [], "unworked": [], "targets": [],
                "touched": 0}

    plan_targets = targets()
    if not plan_targets:
        return {
            "ok": False,
            "reason": (
                "I can read the strategy doc, but it has no TARGET / ICP / SEGMENT "
                "section I could match outreach against, so I am not reporting drift "
                "against a plan I would have had to guess at."
            ),
            "remedy": (
                "Add a TARGET / ICP / SEGMENT heading to the strategy doc with the "
                "segments listed under it, one per line."
            ),
            "lines": [], "off_plan": [], "unworked": [], "targets": [], "touched": 0,
        }

    doc_text = gtm_sheet.normalise_header(data["text"])
    touched = [r for r in (rows or []) if _touched_in_window(r, since=since, today=today)]

    # NOTHING WENT OUT AT ALL is a different finding from "nothing went to these
    # segments", and reporting the first as the second would list every target
    # in the plan as neglected on a week where the team simply didn't do
    # outreach. That reads as a targeting failure and it is a volume one.
    if not touched:
        return {
            "ok": True, "reason": "", "off_plan": [], "unworked": [],
            "targets": plan_targets, "touched": 0,
            "lines": [
                f"No dated outreach at all between {dl.iso(since)} and {dl.iso(today)}, "
                f"so there is nothing to compare against the plan's "
                f"{len(plan_targets)} target(s). That is a volume question, not a "
                "targeting one."
            ],
        }

    # UNWORKED: a target the plan names that nothing in the window matched.
    unworked: list[dict] = []
    for t in plan_targets:
        key = gtm_sheet.normalise_header(t["phrase"])
        if not key:
            continue
        hits = sum(1 for r in touched if key in _row_segments(r))
        if hits == 0:
            unworked.append({"phrase": t["phrase"], "heading": t["heading"]})

    # OFF-PLAN: a vertical we worked that the doc never mentions ANYWHERE. The
    # whole doc, not just the target list — plans name their segments in the
    # prose around the bullets, and flagging those would be a false positive
    # about the bot's own parsing rather than about the team's outreach.
    off_counts: dict[str, int] = {}
    for r in touched:
        for role in ("poc_vertical", "industry"):
            value = str(r.get(role) or "").strip()
            if len(value) < 3:
                continue
            key = gtm_sheet.normalise_header(value)
            if key and key not in doc_text:
                off_counts[value] = off_counts.get(value, 0) + 1
            break
    off_plan = sorted(
        ({"segment": k, "rows": v} for k, v in off_counts.items()),
        key=lambda d: -d["rows"],
    )[: max(1, int(config.STRATEGY_MAX_FINDINGS))]

    lines: list[str] = []
    window = f"{dl.iso(since)} to {dl.iso(today)}"
    lines.append(
        f"Checked {len(touched)} row(s) touched {window} against "
        f"{len(plan_targets)} target(s) in the plan."
    )
    if off_plan:
        parts = "; ".join(f"{d['segment']} ({d['rows']})" for d in off_plan)
        lines.append(
            f"OFF-PLAN — outreach went to segment(s) the plan does not name: {parts}."
        )
    if unworked:
        parts = "; ".join(t["phrase"] for t in unworked[: int(config.STRATEGY_MAX_FINDINGS)])
        lines.append(
            f"IN THE PLAN, NOTHING SENT — no outreach in this window matched: {parts}."
        )
    if not off_plan and not unworked:
        lines.append("Outreach and the plan agree: every target was worked, nothing off-plan.")

    return {
        "ok": True, "reason": "", "lines": lines, "off_plan": off_plan,
        "unworked": unworked, "targets": plan_targets, "touched": len(touched),
    }


# -- status ------------------------------------------------------------------


def status_detail() -> tuple[bool, str]:
    """(readable, one-paragraph detail) for `sources.StrategyDoc`."""
    if not configured():
        return False, (
            "No strategy doc is configured (STRATEGY_DOC_ID / STRATEGY_DOC_FILE), so I "
            "can't check outreach against the plan or tell you how current it is."
        )
    data = read()
    if not data["ok"]:
        return False, f"{data['error']}. {data['remedy']}".strip()

    bits = [
        f"Reading {data.get('name') or 'the strategy doc'} "
        f"({data['source']}, {len(data['text'])} chars)."
    ]
    line = currency_line()
    if line:
        bits.append(line)
    found = targets()
    if found:
        bits.append(
            f"{len(found)} target(s) read from its "
            f"{found[0]['heading'] or 'target'} section — outreach is checked against them."
        )
    else:
        bits.append(
            "It has no TARGET / ICP / SEGMENT section I could match outreach against, so "
            "the outreach-vs-plan check reports that rather than guessing at a plan."
        )
    return True, " ".join(bits)


if __name__ == "__main__":  # pragma: no cover - operator convenience
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    ok, detail = status_detail()
    print(("READABLE" if ok else "NOT READABLE") + " — " + detail)
    for t in targets():
        print(f"  target: {t['phrase']}   (under {t['heading']!r})")

"""THE MEETING KNOWLEDGE LAYER — and the citation that must travel with it.

THE RULE THIS MODULE EXISTS TO ENFORCE:

    WHENEVER MEETING KNOWLEDGE SHAPES A LINE — a hold, a decision, a commitment
    — THE LINE NAMES ITS SOURCE.

        "Acme paused until the pilot lands (Sales Bot Discussion, 2 Sep)"

    Everywhere: digest items, cadence chases, the tracker reminder, prep briefs,
    the to-do sheet, and answers to questions. A meeting-derived claim with no
    meeting citation is a BUG, not a style lapse.

WHY IT IS A BUG. A sheet-derived line can be argued with by opening the sheet —
the bot already names the cell it read. A meeting-derived line has no such
handle: "we're holding off on Acme" is either something somebody actually
decided in a room, or something the bot inferred, and from the outside those two
look identical. The citation is what makes the difference visible. Without it
the team has to take the bot's word for it, and the first time it is wrong they
stop taking its word for anything.

WHAT IS EXTRACTED, and from where. `notes.py` owns the folder, the sync and the
exclusion filter; this module reads ONLY through `notes.list_notes` /
`notes.read_note`, so a doc the filter holds back can never reach a digest line.
From each loaded note:

    HOLDS         a line saying work on a named company is paused, parked,
                  deprioritised, or not to be chased. These are the ones that
                  change what the bot DOES: a held company still shows in the
                  cadence (suppressing it would hide real work) but the line
                  says it is on hold and cites the meeting that put it there.
    DECISIONS     the note's own "Decisions" section, verbatim.
    COMMITMENTS   the note's "Next steps" block — "[Name] do the thing" — which
                  is also what the to-do sheet is built from.

NOTHING IS SUMMARISED BY A MODEL HERE. Every fact is a line the team wrote,
carried through unchanged apart from clipping. That is deliberate: a citation
attached to a paraphrase is worse than no citation, because it lends a
model's wording the authority of a minute.

COMPANY MATCHING IS AGAINST A LIST THAT ALREADY EXISTS. A fact is attached to a
company only when the company's name — taken from the TRACKER, not invented here
— appears in the line. So the bot can never announce a hold on a company that is
not in the pipeline, and cannot mistake a person's surname for an account.

THIS MODULE IS PURE AND SEND-FREE. It reads files through notes.py and returns
dicts. Nothing here can make the bot speak.
"""
import logging
import re
import threading
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

import config
import deadlines as dl
import gtm_sheet
import notes

log = logging.getLogger(__name__)

KIND_HOLD = "hold"
KIND_DECISION = "decision"
KIND_COMMITMENT = "commitment"

# How much of a note line survives into a digest bullet. Long enough to carry the
# decision, short enough that three of them still fit next to the day's work.
_CLIP = 180

# HOLD VOCABULARY. Deliberately narrow: this is the one fact class that changes
# how a cadence line reads, so a loose match would silently mute chases. Each
# phrase has to be an explicit instruction to stop, not merely negative
# sentiment about a deal.
_HOLD_RE = re.compile(
    r"\b("
    r"on\s+hold|put\s+on\s+hold|holding\s+off|hold\s+off|"
    r"paused?|pausing|park(?:ed|ing)?|"
    r"deprioriti[sz]e[d]?|de-prioriti[sz]e[d]?|"
    r"stop\s+(?:the\s+)?outreach|stop\s+chasing|don'?t\s+chase|do\s+not\s+chase|"
    r"no\s+further\s+outreach|freeze|frozen|shelved?|back\s?burner"
    r")\b",
    re.IGNORECASE,
)

# NEGATION KILLS A HOLD. "Anthropic stays active and is not paused" contains the
# word "paused" and means the exact opposite of a hold; without this the bot
# would annotate every Anthropic line with a hold the meeting explicitly ruled
# out, which is worse than missing a real one — it puts a fabricated decision in
# brackets next to a real citation and lends it that citation's authority.
# Checked in the ~24 characters before the matched phrase, which is where a
# negator sits in English, and against the whole line for the "un-hold" verbs.
_NEGATOR_BEFORE_RE = re.compile(
    r"\b(?:not|isn'?t|aren'?t|won'?t|never|nothing|neither|no\s+longer|nor|"
    r"rather\s+than|instead\s+of|without|avoid|stop(?:ping)?\s+the\s+hold)"
    r"\b[^.]{0,24}$",
    re.IGNORECASE,
)
_UNHOLD_RE = re.compile(
    r"\b(?:un-?paus\w*|un-?hold|off\s+hold|resume[ds]?|resuming|restart\w*|"
    r"back\s+on|re-?activat\w*|lift(?:ed|ing)?\s+the\s+(?:hold|pause))\b",
    re.IGNORECASE,
)


def is_hold(line: str) -> bool:
    """Does this line PUT something on hold?

    A hold is an instruction to stop, so a sentence that merely contains the
    word "paused" is not one. Both directions are checked: a negator in front of
    the phrase, and an un-hold verb anywhere in the line.
    """
    text = str(line or "")
    m = _HOLD_RE.search(text)
    if not m:
        return False
    if _UNHOLD_RE.search(text):
        return False
    return not _NEGATOR_BEFORE_RE.search(text[: m.start()])


# A due date stated inside a next-step line: "by 12 Sep", "due Friday", "by EOD".
_DUE_PHRASE_RE = re.compile(
    r"\b(?:by|before|due|due\s+by|deadline)\s+(?P<when>[^.;,)]{2,28})", re.IGNORECASE
)
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
    "wed": 2, "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_TODAYISH = {"today": 0, "tonight": 0, "eod": 0, "cob": 0, "tomorrow": 1}


# -- the citation ------------------------------------------------------------


def _short_date(d: Optional[date]) -> str:
    """"2 Sep". No zero padding and no %-d, which is not portable to Windows."""
    if d is None:
        return ""
    return f"{d.day} {d.strftime('%b')}"


# The export tool's furniture, stripped so a citation reads as a meeting's NAME
# ("Sales Bot Discussion") rather than as a filename ("Sales Bot Discussion –
# 2026／09／02 11：20 IST – Notes by Gemini"). The citation is meant to be said out
# loud in a channel; a timestamp in it is noise that makes people stop reading
# the brackets, which defeats the point of putting them there.
_NAME_NOISE_RE = (
    re.compile(r"\s*[–—-]\s*Notes by\b.*$", re.IGNORECASE),
    re.compile(r"\s*[–—-]\s*\d{4}[^\s]*\s*\d{1,2}[:：.]\d{2}.*$"),
    re.compile(r"\s*[–—-]\s*\d{1,2}[:：.]\d{2}\s*(?:IST|UTC|GMT|AM|PM)?\s*$", re.IGNORECASE),
    re.compile(r"\s*\(\s*\)\s*$"),
)


def _clean_name(raw: str) -> str:
    name = " ".join(str(raw or "").split()).strip()
    for pattern in _NAME_NOISE_RE:
        name = pattern.sub("", name).strip()
    return name.strip(" -–—·:").strip()


def meeting_name(note: Optional[dict]) -> str:
    """What the meeting is CALLED, for a citation.

    The label ("Sales Bot Discussion") is what a human would say; the title is
    the whole filename and is only a fallback. Neither is ever fabricated — a
    note with no readable name cites as "a meeting note", which is honest and
    still tells the reader where to look.
    """
    if not note:
        return ""
    label = _clean_name(note.get("label"))
    if label:
        return label[:60]
    title = _clean_name(note.get("title"))
    return title[:60] or "a meeting note"


def citation(note: Optional[dict]) -> str:
    """"Sales Bot Discussion, 2 Sep" — the exact string that goes in brackets.

    Returns "" when there is no note, and callers MUST treat that as "this line
    is not meeting-derived" rather than dropping the brackets and keeping the
    claim.
    """
    if not note:
        return ""
    name = meeting_name(note)
    when = ""
    raw = str(note.get("date") or "").strip()
    if raw:
        try:
            when = _short_date(date.fromisoformat(raw))
        except ValueError:
            when = raw
    if name and when:
        return f"{name}, {when}"
    return name or when


def cite(text: str, note: Optional[dict]) -> str:
    """Append the citation to a line. THE ONE FUNCTION every caller should use.

    Idempotent-ish by construction: if the citation is already the tail of the
    line it is not added twice, so a line that passes through two renderers does
    not end up double-cited.
    """
    tag = citation(note)
    if not tag:
        return text
    if text.rstrip().endswith(f"({tag})"):
        return text
    return f"{text} ({tag})"


# -- reading the notes -------------------------------------------------------


def _clip(text, limit: int = _CLIP) -> str:
    s = " ".join(str(text or "").split()).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _company_index(companies: Iterable[str]) -> list[tuple[str, str]]:
    """[(normalised_name, original_name)], longest first.

    Longest first so "Acme Research Labs" wins over "Acme" on a line that names
    both — the more specific match is the one a human would make.
    """
    seen: dict[str, str] = {}
    for raw in companies or []:
        name = str(raw or "").strip()
        if len(name) < 3:
            # Two-letter "companies" match everything. A tracker row with a
            # one-word stub name is not something to hang a hold on.
            continue
        key = gtm_sheet.normalise_header(name)
        if key and key not in seen:
            seen[key] = name
    return sorted(seen.items(), key=lambda kv: -len(kv[0]))


def _companies_in(line: str, index: list[tuple[str, str]]) -> list[str]:
    """Which known companies a line names. Never invents one."""
    hay = gtm_sheet.normalise_header(line)
    if not hay:
        return []
    out: list[str] = []
    for key, name in index:
        if key in hay:
            out.append(name)
    return out


def _note_lines(note: dict) -> list[tuple[str, str]]:
    """[(kind_hint, line)] for every line of a note worth reading as a fact.

    The summary is included because holds are stated there as often as in the
    Decisions block ("we agreed to pause Acme until the pilot lands"), and a
    hold that only lives in the summary is still a hold.
    """
    out: list[tuple[str, str]] = []
    for text in note.get("decisions") or []:
        if str(text).strip():
            out.append((KIND_DECISION, str(text).strip()))
    for step in note.get("next_steps") or []:
        task = step.get("task") if isinstance(step, dict) else str(step)
        if str(task or "").strip():
            out.append((KIND_COMMITMENT, str(task).strip()))
    summary = str(note.get("summary") or "").strip()
    for sentence in re.split(r"(?<=[.!?])\s+", summary):
        s = sentence.strip()
        if len(s) >= 12:
            out.append((KIND_DECISION, s))
    return out


# Cache: parsing every note's body on every digest section would re-read the
# whole folder several times per digest. The folder only changes when a sync
# runs, so the key carries the notes scan stamp.
_lock = threading.RLock()
_cache: dict = {"key": None, "facts": []}


def _cache_key(days: int, index: list[tuple[str, str]]) -> tuple:
    st = notes.sync_status()
    return (
        days,
        len(index),
        st.get("last_success"),
        st.get("docs_seen"),
        st.get("docs_loaded"),
    )


def facts(*, companies: Optional[Iterable[str]] = None, days: Optional[int] = None) -> list[dict]:
    """Every citable fact from the loaded meeting notes in the window.

    Each fact is
        {kind, text, companies[], note{date,label,title,path}, citation, date}

    `citation` is precomputed and is the ONLY thing a renderer should print
    alongside `text` — computing it at render time is how a line ends up
    uncited when one caller forgets.

    Never raises: a notes folder that cannot be read costs the citations and
    nothing else, and the digest still posts.
    """
    days = int(days if days is not None else config.MEETING_FACTS_DAYS)
    index = _company_index(companies or [])
    key = _cache_key(days, index)
    with _lock:
        if _cache["key"] == key:
            return list(_cache["facts"])

    out: list[dict] = []
    try:
        metas = notes.list_notes(days=max(1, days))
    except Exception:
        log.info("[meetings] the notes folder could not be listed; no citations today",
                 exc_info=True)
        metas = []

    for meta in metas[: max(1, config.MEETING_FACTS_MAX_NOTES)]:
        try:
            note = notes.read_note(date=meta.get("date"), label=meta.get("label"))
        except Exception:
            log.debug("[meetings] could not read %r", meta.get("path"), exc_info=True)
            continue
        if not note:
            continue
        tag = citation(note)
        stub = {
            "date": note.get("date"),
            "label": note.get("label"),
            "title": note.get("title"),
            "path": note.get("path"),
        }
        for hint, line in _note_lines(note):
            kind = KIND_HOLD if is_hold(line) else hint
            out.append({
                "kind": kind,
                "text": _clip(line),
                "companies": _companies_in(line, index),
                "note": stub,
                "citation": tag,
                "date": note.get("date"),
            })

    out.sort(key=lambda f: str(f.get("date") or ""), reverse=True)
    with _lock:
        _cache["key"] = key
        _cache["facts"] = list(out)
    log.info(
        "[meetings] %d citable fact(s) from %d note(s) in the last %d days "
        "(holds=%d decisions=%d commitments=%d)",
        len(out), len(metas), days,
        sum(1 for f in out if f["kind"] == KIND_HOLD),
        sum(1 for f in out if f["kind"] == KIND_DECISION),
        sum(1 for f in out if f["kind"] == KIND_COMMITMENT),
    )
    return list(out)


def holds(*, companies: Optional[Iterable[str]] = None,
          days: Optional[int] = None) -> dict[str, dict]:
    """{normalised company key: the most recent hold fact naming it}.

    MOST RECENT WINS. A company put on hold in August and un-paused in September
    should not read as held — and while the bot cannot detect an un-pause
    reliably, taking the newest statement is the closest honest approximation
    and is what a human reading the notes top-down would do.
    """
    out: dict[str, dict] = {}
    for fact in facts(companies=companies, days=days):
        if fact["kind"] != KIND_HOLD:
            continue
        for name in fact["companies"]:
            key = gtm_sheet.normalise_header(name)
            if key and key not in out:  # facts are newest-first
                out[key] = fact
    if out:
        log.info("[meetings] %d compan(ies) are on hold per the meeting notes: %s",
                 len(out), ", ".join(sorted(out)[:8]))
    return out


def hold_for(company: str, held: dict[str, dict]) -> Optional[dict]:
    """The hold fact for one company, or None. Kept as a function so callers do
    not each re-implement the normalisation."""
    key = gtm_sheet.normalise_header(str(company or ""))
    return held.get(key) if key else None


def annotate_hold(text: str, company: str, held: dict[str, dict]) -> str:
    """A cadence line, plus its hold and the meeting that decided it.

    A HELD ROW IS STILL SHOWN. Dropping it would be the bot quietly deciding
    that a meeting outranks the pipeline, and the row's owner would never learn
    why it vanished. Instead the line says what the meeting said, and cites it,
    so the owner can act on the row or on the hold.
    """
    fact = hold_for(company, held)
    if not fact:
        return text
    return cite(f"{text} — on hold", fact.get("note"))


def for_company(company: str, *, companies: Optional[Iterable[str]] = None,
                days: Optional[int] = None, limit: int = 5) -> list[dict]:
    """Every citable fact naming one company, newest first. The answer path's
    door into the meeting knowledge."""
    key = gtm_sheet.normalise_header(str(company or ""))
    if not key:
        return []
    pool = companies if companies is not None else [company]
    out = [
        f for f in facts(companies=pool, days=days)
        if any(gtm_sheet.normalise_header(c) == key for c in f["companies"])
    ]
    return out[: max(1, limit)]


# -- commitments, for the to-do sheet ----------------------------------------


def _due_from(text: str, *, raised: Optional[date]) -> str:
    """A due date STATED in the line, as ISO, or "".

    Only what the sentence actually says. "by Friday" resolves against the day
    the commitment was raised, not against today, because a to-do extracted a
    week late must not silently acquire a new deadline.
    """
    m = _DUE_PHRASE_RE.search(text or "")
    if not m:
        return ""
    when = m.group("when").strip().lower().strip(".,;:")
    anchor = raised or dl.today_ist()

    if when in _TODAYISH:
        return dl.iso(anchor + timedelta(days=_TODAYISH[when]))
    head = when.split()[0] if when.split() else ""
    if head in _TODAYISH:
        return dl.iso(anchor + timedelta(days=_TODAYISH[head]))
    for word, idx in _WEEKDAYS.items():
        if head == word or when.startswith(word + " ") or when == word:
            ahead = (idx - anchor.weekday()) % 7
            return dl.iso(anchor + timedelta(days=ahead or 7))
    parsed = dl.parse_date(when)
    if parsed is None and raised is not None:
        # "by 12 Sep" with no year: parse against the year it was raised in.
        parsed = dl.parse_date(f"{when} {raised.year}")
    return dl.iso(parsed) if parsed else ""


def action_items(*, days: Optional[int] = None,
                 companies: Optional[Iterable[str]] = None) -> list[dict]:
    """Commitments from the window's meeting notes, ready for the to-do sheet.

    Each item is
        {task, owner, source_meeting, date_raised, due, note}

    `source_meeting` IS the citation string — the to-do sheet's "Source meeting"
    column is the rule-2 citation in spreadsheet form, so the same function
    produces both and they cannot drift apart.

    Untagged next-step lines keep `owner` empty rather than being assigned to
    whoever spoke last; an unowned to-do is a real state and the sheet shows it
    as one.
    """
    days = int(days if days is not None else config.TODO_NOTES_DAYS)
    out: list[dict] = []
    try:
        metas = notes.list_notes(days=max(1, days))
    except Exception:
        log.info("[meetings] no notes to extract action items from", exc_info=True)
        return []

    index = _company_index(companies or [])
    for meta in metas[: max(1, config.MEETING_FACTS_MAX_NOTES)]:
        try:
            note = notes.read_note(date=meta.get("date"), label=meta.get("label"))
        except Exception:
            log.debug("[meetings] could not read %r", meta.get("path"), exc_info=True)
            continue
        if not note:
            continue
        raised = None
        try:
            raised = date.fromisoformat(str(note.get("date") or ""))
        except ValueError:
            raised = None
        tag = citation(note)
        for step in note.get("next_steps") or []:
            task = _clip(step.get("task") if isinstance(step, dict) else step, 300)
            if len(task) < 8:
                # A three-word fragment is a parsing artefact, not a to-do.
                continue
            owner = ""
            if isinstance(step, dict):
                owner = str(step.get("owner_name") or "").strip()
            out.append({
                "task": task,
                "owner": owner,
                "source_meeting": tag,
                "date_raised": str(note.get("date") or ""),
                "due": _due_from(task, raised=raised),
                "companies": _companies_in(task, index),
                "note": {
                    "date": note.get("date"), "label": note.get("label"),
                    "title": note.get("title"), "path": note.get("path"),
                },
            })
    log.info("[meetings] %d action item(s) in the last %d days of notes", len(out), days)
    return out


# -- status ------------------------------------------------------------------


def status() -> dict:
    """What the citation layer can currently see — reported at startup and in
    state/summary.json, so "no citations" is never mistaken for "no meetings"."""
    try:
        st = notes.sync_status()
    except Exception:
        st = {}
    return {
        "notes_loaded": st.get("docs_loaded", 0),
        "notes_on_disk": st.get("docs_seen", 0),
        "window_days": config.MEETING_FACTS_DAYS,
        "last_sync": st.get("last_success"),
        "degraded": bool(st.get("degraded")),
    }


if __name__ == "__main__":  # pragma: no cover - operator convenience
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    found = facts()
    print(f"{len(found)} citable fact(s) in the last {config.MEETING_FACTS_DAYS} days")
    for f in found[:40]:
        print(f"  [{f['kind']:<10}] {f['text']}  ({f['citation']})")
    print(f"\n{datetime.now().isoformat(timespec='seconds')}")

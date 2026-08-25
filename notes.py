"""Meeting notes — READ-ONLY context for answering questions.

This module owns BOTH halves of the meeting-notes pipeline:

  SYNC   it runs `config.NOTES_SYNC_CMD` (a full rclone command) to pull the
         Drive docs down into `config.NOTES_DIR` — at startup, every
         NOTES_SYNC_MINUTES, and on demand before a notes question is answered.
         The command is the only thing that touches Drive; the bot itself holds
         no Google credential and never writes anything back.
  FILTER it decides what came down is readable, and loads everything except
         the product standups.

THE FILTER IS EXCLUDE-BASED. The sync pulls every "Notes by Gemini" doc shared
with the sync account, and by default the bot loads ALL of them — a PM call, an
ad-hoc meet and a customer call are all context worth having. The only documents
kept out are the recurring product standups, matched by title against
`config.NOTES_EXCLUDE_TITLE_PATTERNS` (default "AM sync,PM sync",
case-insensitive substring).

This inverts the previous rule, and the inversion was the point. The old filter
was include-based: a note counted only if its attendee list named a specific
person or its title carried a sales keyword. On the live folder that admitted
ZERO of 72 synced docs — every real meeting was filtered out because the invite
list didn't survive the Gemini export and nobody titles a call "sales sync". An
include-list fails silently and looks exactly like "no meetings happened", which
is the one failure this module exists to prevent. An exclude-list fails loudly
instead: the worst case is a standup in context, which is visible and harmless,
rather than a customer call missing from it.

A sync failure NEVER raises and never crashes the bot: it is recorded, reported
through `sync_status()`, and logged ONCE per distinct failure (a failure that
repeats every 30 minutes must not fill the log). The `sales_meeting_notes`
source then reports DEGRADED — readable, but going stale — rather than pretending
the notes are current.

Generalised from the PM bot's standup reader, so it is no longer tied to
twice-daily engineering syncs:

  - ANY meeting note counts, not just "Notes by Gemini" exports. A note is
    recognised by having a parseable DATE in its filename or first lines. Gemini
    notes still work — they're a special case of the general rule, and their
    `Summary` / `Decisions` / `Next steps` structure is still parsed when present.
  - `session` is now a free-form LABEL ("AM", "PM", "pipeline review", "Acme
    call") rather than an AM/PM enum, because sales meetings aren't a standup.

Format-tolerant by design: the on-disk format depends on how the sync is
configured (Google Docs commonly export to .docx, but .txt / .md / .html are all
common). `_extract_text` handles those with the stdlib only — no new deps.

Everything degrades gracefully: an unset/empty/missing NOTES_DIR, an unreadable
file, or an unknown format yields [] / None rather than raising. `is_configured()`
distinguishes "no access" from "no notes for that day", which callers must keep
distinct — reporting a folder it can't see as "nothing was discussed" is the one
failure this module exists to prevent.
"""
import html
import logging
import os
import re
import subprocess
import threading
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

# A DATE is what makes a file a meeting note here — not a vendor marker. The PM
# bot required "Notes by Gemini" in the title, which would silently drop a sales
# note someone wrote by hand or exported from another tool. A dated document in
# the notes folder IS a meeting note; that is the whole test.
#
# rclone sanitises the "/" in a Google Docs title to a full-width slash "／" (or
# sometimes "_"), so accept any separator.
_DATE_RE = re.compile(r"(?<!\d)(\d{4})[\s/／._-]{1,3}(\d{1,2})[\s/／._-]{1,3}(\d{1,2})(?!\d)")

# A parenthesised label — "(Pipeline review)", "(AM Sync)", "(Acme call)" — is how
# a note says WHICH meeting it was. Free text, not an enum: sales meetings aren't
# a twice-daily standup.
_LABEL_PAREN_RE = re.compile(r"\(([^)]{2,60})\)")
# Bare AM/PM as a fallback, so notes carried over from the old standup naming
# still resolve to a sensible label.
_AMPM_RE = re.compile(r"\b(AM|PM)\b", re.IGNORECASE)
# Tokens that name the FORMAT rather than the meeting; stripped from a label.
_LABEL_NOISE = {"notes", "note", "by", "gemini", "meeting", "transcript", "doc", "copy"}

# Text extensions we can read directly as UTF-8.
_TEXT_EXTS = {".txt", ".md", ".markdown", ".text", ".csv", ".log", ""}

# Section headers we recognise inside a note (canonical key → aliases). Broader
# than the standup set: sales notes label the same three things several ways.
_SECTION_ALIASES = {
    "summary": ("summary", "overview", "tl;dr", "recap"),
    "details": ("details", "discussion", "notes", "context"),
    "decisions": (
        "decisions", "aligned", "decisions and aligned", "decisions & aligned",
        "decisions / aligned", "key decisions", "agreements", "aligned on",
        "agreed", "outcomes",
    ),
    # All of these mean the same thing: who owes what after this meeting.
    "next_steps": (
        "next steps", "next step", "action items", "action item", "actions",
        "follow ups", "follow-ups", "followups", "todos", "to do", "to-dos",
        "owners", "commitments",
    ),
}
# Reverse lookup: normalised header text → canonical key.
_HEADER_TO_KEY = {alias: key for key, aliases in _SECTION_ALIASES.items() for alias in aliases}

# A "[Name] task" next-step line (optionally bulleted).
_NEXT_STEP_RE = re.compile(r"^\s*[\*\-•·]?\s*\[(?P<owner>[^\]]+)\]\s*(?P<task>.+?)\s*$")
# A bullet line (for decisions / generic lists).
_BULLET_RE = re.compile(r"^\s*[\*\-•·]\s+(?P<text>.+?)\s*$")
# Boilerplate footers auto-note tools append — never part of the real content.
# Gemini's are listed explicitly because those exports are the common case here;
# the generic clauses catch the equivalents from other tools.
_TRAILER_RE = re.compile(
    r"(?i)^\s*("
    r"you should review gemini|gemini can make mistakes|get tips and learn|"
    r"this (summary|report|transcript) was (created|generated|produced)|"
    r"review gemini'?s notes|.*gemini'?s notes to make sure|"
    r"(ai|automatically)[- ]generated (summary|notes|transcript))",
)

# Phrases that mean "I care about the freshest / today's meeting" → sync first.
_WANTS_RECENT_RE = re.compile(
    r"\b(today|todays|this\s+morning|this\s+afternoon|this\s+evening|tonight|"
    r"just\s+now|latest|most\s+recent|last\s+(meeting|call|sync)|"
    r"stand[\s-]?up|sync|call|meeting)\b",
    re.IGNORECASE,
)

# How much raw text to hand back to the model as a fallback.
_RAW_MAX_CHARS = 4000

# How much of a document we read to find its date and label when the filename
# carries neither. Both sit in the first few lines of any export; 1200 chars is
# comfortably enough and keeps a folder of 300 docs cheap to scan.
_HEAD_CHARS = 1200


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -- text extraction (format-tolerant, stdlib only) --------------------------


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _docx_text(path: str) -> str:
    """Extract text from a .docx (a zip of XML) without python-docx: read
    word/document.xml and turn paragraph/break tags into newlines."""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception:
        log.debug("[notes] could not read docx %s", path, exc_info=True)
        return ""
    xml = re.sub(r"(?i)</w:p>", "\n", xml)
    xml = re.sub(r"(?i)<w:br\s*/?>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml)


def _extract_text(path: str) -> str:
    """Best-effort plain text from a note file. Returns "" on anything we can't
    read (e.g. .pdf — no stdlib extractor)."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".docx":
            return _docx_text(path)
        if ext in (".html", ".htm"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return _strip_html(f.read())
        if ext in _TEXT_EXTS:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception:
        log.debug("[notes] extract failed for %s", path, exc_info=True)
        return ""
    log.debug("[notes] unsupported extension %r for %s; skipping", ext, path)
    return ""


# -- the exclusion filter ----------------------------------------------------
# The sync pulls every Gemini note shared with the sync account. Everything it
# brings down is loaded EXCEPT the recurring product standups, which are matched
# by title. There is no include-list: see the module docstring for why.

#: Fallback when NOTES_EXCLUDE_TITLE_PATTERNS is unreadable. Kept in sync with
#: the default in config.py, which is the value that actually applies.
_DEFAULT_EXCLUDE = ("AM sync", "PM sync")


def _cfg(name: str, default):
    """Read a config value without importing config at module import time (this
    module is deliberately importable on its own, e.g. from a test or a script)."""
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


def _exclude_patterns() -> list[str]:
    """The configured title patterns, lowercased, blanks dropped.

    An EMPTY list is a legitimate configuration and means "exclude nothing" —
    every synced doc loads. That is a real choice someone might make, so it is
    not silently replaced by the default; `config.validate()` says so at startup
    instead.
    """
    raw = _cfg("NOTES_EXCLUDE_TITLE_PATTERNS", list(_DEFAULT_EXCLUDE))
    if raw is None:
        raw = list(_DEFAULT_EXCLUDE)
    return [p.strip().lower() for p in raw if str(p).strip()]


def _classify(title: str) -> tuple[bool, str]:
    """Should this note be loaded? Returns (load_it, one-line reason).

    Matched on the TITLE only — the filename stem, or the document's first line
    when the filename carries no title. Deliberately not the body: a standup that
    merely mentions "the PM sync" in its summary is still a real meeting, and
    matching on body text would drop it.

    The reason is kept on every note so the status line and the logs can say WHY
    a doc was loaded or held back, rather than leaving the filter a black box.
    """
    low = (title or "").lower()
    for pattern in _exclude_patterns():
        if pattern in low:
            return False, f"title matches the excluded pattern {pattern!r} (product standup)"
    return True, "loaded (not an excluded standup)"


# -- filename / title parsing ------------------------------------------------


def _parse_date(text: str) -> Optional[date]:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _clean_label(raw: str) -> str:
    """Strip format words ("Notes by Gemini"), dates and punctuation out of a
    candidate label, leaving what actually names the meeting. "" when nothing
    survives."""
    without_date = _DATE_RE.sub(" ", raw or "")
    words = [w for w in re.split(r"[\s_]+", without_date) if w]
    kept = [w for w in words if w.strip(".,-–—:·()").lower() not in _LABEL_NOISE]
    candidate = " ".join(kept).strip(" -–—:·,.")
    if not candidate or candidate.isdigit():
        return ""
    return candidate[:60]


def _parse_label(text: str, *, freeform: bool = False) -> Optional[str]:
    """WHICH meeting this note is from, as free text — "Pipeline review", "Acme
    call", "AM Sync". Returns None when nothing meaningful is there; callers must
    treat that as "unlabelled", never as a default meeting type.

    Tried in order:
      1. A parenthesised label — where both Google Docs titles and the old
         standup naming put it: "… (Pipeline review)", "… (AM Sync)".
      2. `freeform`: whatever is left of the text once the date and the format
         words are removed — this is what catches the ordinary
         "2026-08-14 Acme call.docx". Only enabled for FILENAMES, because
         applying it to a document's body would return the first sentence of the
         meeting rather than its name.
      3. A bare AM/PM or morning/evening word, so notes carried over from the
         standup naming still resolve.
    """
    for raw in _LABEL_PAREN_RE.findall(text or ""):
        candidate = _clean_label(raw)
        if candidate:
            return candidate

    if freeform:
        # Everything outside the parens — "2026-08-14 Pipeline review (Notes by
        # Gemini)" leaves "Pipeline review" once the date and noise words go.
        candidate = _clean_label(_LABEL_PAREN_RE.sub(" ", text or ""))
        if candidate:
            return candidate

    m = _AMPM_RE.search(text or "")
    if m:
        return m.group(1).upper()
    low = (text or "").lower()
    if "morning" in low:
        return "AM"
    if "afternoon" in low or "evening" in low:
        return "PM"
    return None


def _candidate_files(notes_dir: str):
    """Yield (path, filename) for every regular file under NOTES_DIR."""
    try:
        entries = sorted(os.listdir(notes_dir))
    except OSError:
        log.info("[notes] cannot list dir %r; treating as empty", notes_dir)
        return
    for name in entries:
        path = os.path.join(notes_dir, name)
        if os.path.isfile(path):
            yield path, name


def _meta_for_file(path: str, name: str) -> Optional[dict]:
    """Return {date, label, path, title, loaded, filter_reason} for a meeting-note
    file, or None when it has no parseable date (the only thing that disqualifies
    a file outright — a dated document in the notes folder IS a meeting note).

    The head of the document is read as a FALLBACK, for the date and the label
    when the filename carries neither. `_scan` caches the result per (path,
    mtime, size), so a folder of 300 docs is parsed once per change, not once per
    question.
    """
    stem = os.path.splitext(name)[0]
    head = _extract_text(path)[:_HEAD_CHARS]

    d = _parse_date(stem)
    # freeform=True: a filename's leftover words ARE its label.
    label = _parse_label(stem, freeform=True)
    if d is None:
        d = _parse_date(head)
    if label is None:
        # freeform stays OFF here — the body's leftover words are the meeting's
        # content, not its name.
        label = _parse_label(head)

    # No date anywhere -> not a meeting note (it's a stray file in the folder).
    if d is None:
        return None

    # Prefer the filename as the title; fall back to the doc's own first line.
    title = stem.strip() or (head.strip().splitlines() or [name])[0][:160]
    title = title.strip()[:160]
    loaded, why = _classify(title)
    return {
        "date": d.isoformat(),
        "label": label,
        "path": path,
        "title": title,
        "loaded": loaded,
        "filter_reason": why,
    }


# path -> ((mtime, size), meta). Parsing every doc's head on every status probe
# would re-read the whole folder several times per question; the folder only
# changes when a sync runs, so cache on the file's own stat.
_SCAN_CACHE: dict[str, tuple[tuple, Optional[dict]]] = {}

# What the last scan saw. `sync_status()` reports these, so the source can state
# "N docs on disk, M loaded, K excluded" instead of a bare "connected".
_STATS: dict = {
    "docs_seen": 0,       # every file in the folder
    "notes_seen": 0,      # …that parsed as a dated meeting note
    "loaded_docs": 0,     # …and cleared the exclusion filter
    "excluded_docs": 0,   # …and were held back as product standups
    "scanned_at": None,
}


def _scan(notes_dir: str) -> tuple[list[dict], list[dict]]:
    """Parse and classify every file in NOTES_DIR. Returns (all_notes,
    loaded_notes) and refreshes `_STATS`. Excluded notes are returned in
    `all_notes` — and ONLY there, for counting — so callers can report how much
    was held back without ever reading it."""
    all_notes: list[dict] = []
    loaded_notes: list[dict] = []
    docs = 0
    live: set[str] = set()

    for path, name in _candidate_files(notes_dir):
        docs += 1
        live.add(path)
        try:
            st = os.stat(path)
            key = (st.st_mtime, st.st_size)
        except OSError:
            continue
        cached = _SCAN_CACHE.get(path)
        if cached is not None and cached[0] == key:
            meta = cached[1]
        else:
            meta = _meta_for_file(path, name)
            _SCAN_CACHE[path] = (key, meta)
            if meta is None:
                log.debug("[notes] %s has no parseable date; not a meeting note", name)
            elif not meta["loaded"]:
                log.debug("[notes] EXCLUDED %s — %s", name, meta["filter_reason"])
            else:
                log.debug("[notes] loaded %s — %s", name, meta["filter_reason"])
        if meta is None:
            continue
        all_notes.append(meta)
        if meta["loaded"]:
            loaded_notes.append(meta)

    for gone in [p for p in _SCAN_CACHE if p not in live]:
        _SCAN_CACHE.pop(gone, None)

    _STATS.update(
        docs_seen=docs,
        notes_seen=len(all_notes),
        loaded_docs=len(loaded_notes),
        excluded_docs=len(all_notes) - len(loaded_notes),
        scanned_at=_utcnow().isoformat(),
    )
    return all_notes, loaded_notes


# -- public API --------------------------------------------------------------


def list_notes(
    days: int = 14,
    *,
    notes_dir: Optional[str] = None,
    include_excluded: bool = False,
) -> list[dict]:
    """The meeting notes from the last `days`, newest first, as
    [{date, label, path, title, loaded, filter_reason}]. [] when disabled/empty.

    This is the only door into the notes, and it is the door the exclusion filter
    guards: a doc whose title matches NOTES_EXCLUDE_TITLE_PATTERNS is never
    returned here, so it is never read, never summarised and never put in front
    of the model. `include_excluded=True` exists for diagnostics ("what else is
    in there?") and must not be used to build an answer.
    """
    notes_dir = _resolve_dir(notes_dir)
    if not notes_dir:
        return []
    all_notes, loaded_notes = _scan(notes_dir)
    considered = all_notes if include_excluded else loaded_notes

    cutoff = (_utcnow() - timedelta(days=max(0, int(days)))).date()
    out: list[dict] = []
    for meta in considered:
        try:
            d = date.fromisoformat(meta["date"])
        except ValueError:
            continue
        if d < cutoff:
            continue
        out.append(meta)
    # Newest first. Within one day, labels sort in reverse alphabetical order —
    # arbitrary but STABLE, which is what matters: "the most recent note" must
    # mean the same file on every call.
    out.sort(key=lambda m: (m["date"], m.get("label") or ""), reverse=True)
    log.info(
        "[notes] list_notes(days=%d) -> %d note(s) in window; %d doc(s) on disk, "
        "%d loaded, %d excluded (standups)",
        days, len(out), _STATS["docs_seen"], _STATS["loaded_docs"],
        _STATS["excluded_docs"],
    )
    return out


def _segment_sections(text: str) -> dict[str, list[str]]:
    """Split note text into {canonical_section: [lines]} by recognised headers.
    Lines before the first header go under "_preamble"."""
    sections: dict[str, list[str]] = {"_preamble": []}
    current = "_preamble"
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if _TRAILER_RE.match(line):
            continue  # drop Gemini's boilerplate footer wherever it appears
        norm = line.strip().rstrip(":").strip().lower()
        key = _HEADER_TO_KEY.get(norm)
        # Treat a short standalone header line as a section boundary.
        if key and len(line.strip()) <= 40:
            current = key
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _clean_bullets(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        m = _BULLET_RE.match(ln)
        out.append(m.group("text").strip() if m else s)
    return out


def _parse_next_steps(lines: list[str]) -> list[dict]:
    """Parse the Next steps block into [{owner_name, task}] by splitting on the
    leading [Name] tag. Note tools write one action item per line, so each line
    is its own item — a "[Name] task" line yields that owner; an untagged
    (non-boilerplate) line yields owner_name=None, which callers must report as
    unowned rather than assigning it to whoever spoke last. Boilerplate is
    dropped upstream."""
    steps: list[dict] = []
    for ln in lines:
        if not ln.strip():
            continue
        m = _NEXT_STEP_RE.match(ln)
        if m:
            steps.append({"owner_name": m.group("owner").strip(), "task": m.group("task").strip()})
            continue
        cont = _BULLET_RE.match(ln)
        text = (cont.group("text") if cont else ln).strip()
        if text:
            steps.append({"owner_name": None, "task": text})
    return steps


def read_note(
    date: Optional[str] = None,
    label: Optional[str] = None,
    *,
    notes_dir: Optional[str] = None,
) -> Optional[dict]:
    """Parse the matching meeting note. Returns
    {date, label, title, path, summary, decisions[], next_steps[{owner_name,
    task}], raw}. With `date` omitted, uses the most recent on file. `label`
    matches case-insensitively as a SUBSTRING, so "pipeline" finds "Pipeline
    review". None when disabled / nothing matched. Read-only."""
    notes_dir = _resolve_dir(notes_dir)
    if not notes_dir:
        return None
    found = list_notes(days=3650, notes_dir=notes_dir)
    if not found:
        return None

    if date:
        found = [n for n in found if n["date"] == date]
    if label:
        want = label.strip().lower()
        found = [n for n in found if want in (n.get("label") or "").lower()]
    if not found:
        return None
    chosen = found[0]  # already newest-first

    text = _extract_text(chosen["path"])
    sections = _segment_sections(text)
    summary_lines = [ln for ln in sections.get("summary", []) if ln.strip()]
    summary = " ".join(_clean_bullets(summary_lines)).strip()
    decisions = _clean_bullets(sections.get("decisions", []))
    next_steps = _parse_next_steps(sections.get("next_steps", []))

    raw = (text or "").strip()
    if len(raw) > _RAW_MAX_CHARS:
        raw = raw[:_RAW_MAX_CHARS].rstrip() + "\n…(truncated)"

    log.info(
        "[notes] read_note(date=%s label=%s) → %s %s: summary=%d decisions=%d next_steps=%d",
        date, label, chosen["date"], chosen.get("label"),
        len(summary), len(decisions), len(next_steps),
    )
    return {
        "date": chosen["date"],
        "label": chosen.get("label"),
        "title": chosen.get("title"),
        "path": chosen["path"],
        "summary": summary,
        "decisions": decisions,
        "next_steps": next_steps,
        "raw": raw,
    }


def freshness(*, notes_dir: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Return (latest_date_on_disk_iso, newest_file_mtime_iso) so a reply can
    state how current the data is. (None, None) when disabled/empty."""
    notes_dir = _resolve_dir(notes_dir)
    if not notes_dir:
        return (None, None)
    found = list_notes(days=3650, notes_dir=notes_dir)
    latest_date = found[0]["date"] if found else None

    newest_mtime = None
    try:
        mtimes = [os.path.getmtime(p) for p, _ in _candidate_files(notes_dir)]
        if mtimes:
            newest_mtime = datetime.fromtimestamp(max(mtimes), tz=timezone.utc).isoformat()
    except OSError:
        log.debug("[notes] mtime scan failed", exc_info=True)
    return (latest_date, newest_mtime)


def is_configured(*, notes_dir: Optional[str] = None) -> bool:
    """True when notes access is usable: NOTES_DIR is set AND the folder exists
    (the folder is created at startup, so this is False only when NOTES_DIR is
    unset or unwritable).
    Distinguishes 'no access' (say so) from 'configured but no note for that day'
    (found=false) so callers never conflate the two — reporting a folder the bot
    can't see as 'nothing was discussed' is the failure this guards against."""
    return bool(_resolve_dir(notes_dir))


def labels_on(date_iso: str, *, notes_dir: Optional[str] = None) -> list[str]:
    """The meeting labels that have a note on file for the given YYYY-MM-DD —
    e.g. ["Pipeline review", "Acme call"]. Empty when disabled / none on file.
    Lets a reply say "there's also a note from the Acme call that day" after
    reading one of them, instead of implying it was the only meeting.

    Notes with no parseable label are counted but not named, so the count still
    reflects reality."""
    out: list[str] = []
    for n in list_notes(days=3650, notes_dir=notes_dir):
        if n.get("date") != date_iso:
            continue
        label = (n.get("label") or "").strip()
        if label and label not in out:
            out.append(label)
    return sorted(out)


def wants_recent(question: str) -> bool:
    """True when the question is clearly about a recent/today meeting, so a
    sync-on-demand is worth running before reading."""
    return bool(_WANTS_RECENT_RE.search(question or ""))


# -- the sync ----------------------------------------------------------------
# One rclone command, run at startup, on a timer, and on demand. It is the only
# thing in this bot that talks to Drive, and it is allowed to fail: a box without
# rclone, or with an expired token, must still run — reading stale notes and
# SAYING they are stale, not crashing.

_SYNC_LOCK = threading.Lock()

_SYNC: dict = {
    "last_attempt": None,   # iso — when the command last ran
    "last_success": None,   # iso — when it last ran cleanly
    "ok": None,             # None = never run, else the last outcome
    "error": None,          # one short line, the failure itself
    "remedy": None,         # one short line, what to DO about it
    "runs": 0,
}
# Signature of the failure we last logged at ERROR. A sync that fails every 30
# minutes must produce ONE error in the log, not one per tick — so a repeat of
# the same failure drops to DEBUG, and a NEW failure is loud again.
_LAST_LOGGED_FAILURE: Optional[str] = None


def ensure_dir(notes_dir: Optional[str] = None) -> str:
    """Make sure NOTES_DIR exists, creating it if it doesn't. Returns the path,
    or "" when no path is configured / it couldn't be created.

    rclone creates the destination itself, but the folder is also what
    `is_configured()` tests, and a first boot that hasn't synced yet should
    report "no notes yet" rather than "no access"."""
    if notes_dir is None:
        notes_dir = _cfg("NOTES_DIR", "")
    notes_dir = (notes_dir or "").strip()
    if not notes_dir:
        return ""
    if os.path.isdir(notes_dir):
        return notes_dir
    try:
        os.makedirs(notes_dir, exist_ok=True)
        log.info("[notes] created NOTES_DIR %s", os.path.abspath(notes_dir))
        return notes_dir
    except OSError as e:
        log.error(
            "[notes] could not create NOTES_DIR %r (%s) — meeting notes are unavailable "
            "until that path is writable.", notes_dir, e,
        )
        return ""


def _remedy_for(returncode: Optional[int], stderr: str, cmd: str) -> str:
    """ONE sentence naming the fix for this failure. A sync error the operator
    can't act on is just noise, so every branch here ends in something to do."""
    err = (stderr or "").lower()
    exe = (cmd or "").strip().split()[0] if (cmd or "").strip() else "the sync command"

    if returncode is None:
        return (
            "The sync timed out. Raise NOTES_SYNC_TIMEOUT_SECONDS, or narrow "
            "NOTES_SYNC_CMD with --include so it copies fewer files."
        )
    if (
        "is not recognized as an internal or external command" in err
        or "command not found" in err
        or "no such file or directory" in err
        or "cannot find the path" in err
        or returncode in (9009, 127)
    ):
        return (
            f"{exe!r} was not found by the shell. On Windows a PowerShell alias or "
            "function is NOT visible to a subprocess — put rclone.exe on PATH, or write "
            "its full path in NOTES_SYNC_CMD (e.g. C:\\rclone\\rclone.exe copy ...)."
        )
    if "didn't find section in config file" in err or "failed to create file system" in err:
        return (
            "The rclone remote named in NOTES_SYNC_CMD isn't configured for this user. "
            "Run 'rclone listremotes' to see the real names, and 'rclone config' to add "
            "it — a service running as another user has its own rclone.conf."
        )
    if (
        "token" in err or "oauth" in err or "invalid_grant" in err
        or "unauthorized" in err or "401" in err
    ):
        return "rclone's Google authorisation has expired — run 'rclone config reconnect <remote>:'."
    if "403" in err or "permission" in err or "access denied" in err or "quota" in err:
        return (
            "The Google account behind the rclone remote can't read those docs — share "
            "the notes folder with it (and check the Drive API quota)."
        )
    return f"Run the command yourself in a plain shell to see what it says: {cmd}"


def _record_failure(error: str, remedy: str) -> None:
    """Mark the sync degraded and log it ONCE per distinct failure."""
    global _LAST_LOGGED_FAILURE
    _SYNC.update(ok=False, error=error, remedy=remedy)
    signature = f"{error}|{remedy}"
    if signature != _LAST_LOGGED_FAILURE:
        _LAST_LOGGED_FAILURE = signature
        log.error(
            "[notes] SYNC FAILED — %s FIX: %s (further identical failures are logged at "
            "DEBUG; the meeting-notes source now reports DEGRADED, so answers say the "
            "notes may be stale)", error, remedy,
        )
    else:
        log.debug("[notes] sync still failing: %s", error)


def sync_now(
    sync_cmd: Optional[str] = None,
    *,
    timeout: Optional[float] = None,
    reason: str = "on demand",
) -> bool:
    """Run the sync command once. Returns True only on a clean exit.

    NEVER raises: every failure path is recorded on `_SYNC` and reported through
    `sync_status()`. Blocking (subprocess + a folder rescan) — call it via
    asyncio.to_thread from async code.

    Concurrent calls are DROPPED rather than queued: the startup sync, the timer
    and an on-demand sync can all land at once, and running three rclones over
    one folder helps nobody.
    """
    if sync_cmd is None:
        sync_cmd = _cfg("NOTES_SYNC_CMD", "")
    sync_cmd = (sync_cmd or "").strip()
    if not sync_cmd:
        return False
    if timeout is None:
        timeout = float(_cfg("NOTES_SYNC_TIMEOUT_SECONDS", 120) or 120)

    notes_dir = ensure_dir()
    if not notes_dir:
        _record_failure(
            "NOTES_DIR is not set or could not be created, so there is nowhere to sync into.",
            "Set NOTES_DIR to a writable folder.",
        )
        return False

    if not _SYNC_LOCK.acquire(blocking=False):
        log.debug("[notes] a sync is already running; skipping the %s sync", reason)
        return bool(_SYNC.get("ok"))

    try:
        _SYNC["last_attempt"] = _utcnow().isoformat()
        _SYNC["runs"] = int(_SYNC.get("runs") or 0) + 1
        log.info("[notes] sync (%s): running %r (timeout=%ss)", reason, sync_cmd, timeout)
        try:
            proc = subprocess.run(
                sync_cmd, shell=True, timeout=timeout, capture_output=True, text=True,
            )
        except subprocess.TimeoutExpired:
            _record_failure(
                f"the sync command did not finish within {timeout}s.",
                _remedy_for(None, "", sync_cmd),
            )
            return False
        except Exception as e:
            _record_failure(
                f"the sync command could not be started ({type(e).__name__}: {e}).",
                _remedy_for(1, str(e), sync_cmd),
            )
            return False

        # Collapse rclone's multi-line stderr: this string ends up in a log line,
        # a status detail and a tool result, and a newline mid-sentence reads as
        # two separate messages in all three.
        stderr = " ".join((proc.stderr or "").split())
        if proc.returncode != 0:
            _record_failure(
                f"the sync exited {proc.returncode}: {stderr[:300] or 'no stderr output'}",
                _remedy_for(proc.returncode, stderr, sync_cmd),
            )
            # Still rescan: whatever is already on disk is what we can answer from.
            _scan(notes_dir)
            log.info(
                "[notes] synced %d docs, %d loaded, %d excluded (standups) — the sync "
                "FAILED, so those counts are what was already on disk",
                _STATS["docs_seen"], _STATS["loaded_docs"], _STATS["excluded_docs"],
            )
            return False

        global _LAST_LOGGED_FAILURE
        _LAST_LOGGED_FAILURE = None
        _SYNC.update(ok=True, error=None, remedy=None, last_success=_utcnow().isoformat())
        _scan(notes_dir)
        # THE line per sync. N is every file the sync left in the folder, M is how
        # many went into context, and K is how many were held back as standups.
        # M + K can be less than N: a file with no parseable date isn't a note at
        # all and is counted in neither.
        log.info(
            "[notes] synced %d docs, %d loaded, %d excluded (standups)",
            _STATS["docs_seen"], _STATS["loaded_docs"], _STATS["excluded_docs"],
        )
        if _STATS["docs_seen"] and not _STATS["loaded_docs"]:
            # Under an exclude-based filter this is a genuinely odd state — it
            # means everything that came down is a standup, or nothing that came
            # down parsed as a dated note at all. Both are worth a look, and
            # neither means "no meetings happened".
            log.warning(
                "[notes] all %d synced doc(s) ended up unloaded: %d excluded as standups "
                "(patterns=%s) and %d had no parseable date. Widen or clear "
                "NOTES_EXCLUDE_TITLE_PATTERNS if that is wrong.",
                _STATS["docs_seen"], _STATS["excluded_docs"],
                _cfg("NOTES_EXCLUDE_TITLE_PATTERNS", []),
                _STATS["docs_seen"] - _STATS["notes_seen"],
            )
        return True
    finally:
        _SYNC_LOCK.release()


def _stale(interval_minutes: Optional[int] = None) -> bool:
    """True when the last sync ATTEMPT is older than NOTES_SYNC_MINUTES (or has
    never happened). Attempt, not success: a broken sync must not be retried on
    every single question."""
    if interval_minutes is None:
        interval_minutes = int(_cfg("NOTES_SYNC_MINUTES", 30) or 30)
    last = _SYNC.get("last_attempt")
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (_utcnow() - when) >= timedelta(minutes=max(1, interval_minutes))


def maybe_sync(*, reason: str = "periodic") -> bool:
    """Sync only if the last attempt is older than NOTES_SYNC_MINUTES. Cheap
    enough to call before every notes question."""
    if not _stale():
        log.debug("[notes] skipping the %s sync — the last attempt was recent", reason)
        return bool(_SYNC.get("ok"))
    return sync_now(reason=reason)


def sync_for_question(question: str) -> dict:
    """The before-answering sync. A question about a recent meeting FORCES a
    sync; anything else only syncs when the folder is stale.

    Returns {ran, ok, forced, degraded, error, remedy} so the tool result can tell
    the model the notes may be stale instead of hiding it."""
    if not (_cfg("NOTES_SYNC_CMD", "") or "").strip():
        return {
            "ran": False, "ok": False, "forced": False, "configured": False,
            "degraded": False, "error": None, "remedy": None,
        }
    forced = wants_recent(question)
    before = _SYNC.get("last_attempt")
    if forced:
        ok = sync_now(reason="a question about a recent meeting")
    else:
        ok = maybe_sync(reason="before answering a notes question")
    return {
        "ran": forced or _SYNC.get("last_attempt") != before,
        "ok": bool(ok),
        "forced": forced,
        "configured": True,
        "degraded": _SYNC.get("ok") is False,
        "error": _SYNC.get("error"),
        "remedy": _SYNC.get("remedy"),
    }


def sync_status() -> dict:
    """Everything the `sales_meeting_notes` source needs to describe itself
    truthfully: when the sync last ran, how many docs it left on disk, how many
    were loaded into context, and how many were held back as standups.

    Rescans the folder when nothing has scanned it yet, so a status call before
    the first sync still reports real numbers."""
    notes_dir = _resolve_dir(None)
    if notes_dir and not _STATS["scanned_at"]:
        _scan(notes_dir)
    return {
        "dir": _cfg("NOTES_DIR", ""),
        "dir_exists": bool(notes_dir),
        "cmd_configured": bool((_cfg("NOTES_SYNC_CMD", "") or "").strip()),
        "interval_minutes": int(_cfg("NOTES_SYNC_MINUTES", 30) or 30),
        "last_attempt": _SYNC.get("last_attempt"),
        "last_success": _SYNC.get("last_success"),
        "ok": _SYNC.get("ok"),
        "degraded": _SYNC.get("ok") is False,
        "error": _SYNC.get("error"),
        "remedy": _SYNC.get("remedy"),
        "docs_seen": _STATS["docs_seen"],
        "notes_seen": _STATS["notes_seen"],
        "docs_loaded": _STATS["loaded_docs"],
        "docs_excluded": _STATS["excluded_docs"],
        "scanned_at": _STATS["scanned_at"],
        "exclude_patterns": list(_cfg("NOTES_EXCLUDE_TITLE_PATTERNS", [])),
    }


def _resolve_dir(notes_dir: Optional[str]) -> str:
    """Resolve the effective notes dir: explicit arg, else config.NOTES_DIR.
    Returns "" (disabled) when unset/empty or the path doesn't exist."""
    if notes_dir is None:
        try:
            import config
            notes_dir = config.NOTES_DIR
        except Exception:
            notes_dir = ""
    notes_dir = (notes_dir or "").strip()
    if not notes_dir:
        return ""
    if not os.path.isdir(notes_dir):
        log.info(
            "[notes] NOTES_DIR %r does not exist; meeting-notes reading is disabled "
            "(the source will report awaiting-access)", notes_dir,
        )
        return ""
    return notes_dir

"""Meeting notes — READ-ONLY context for answering questions.

A separate sync process (rclone or equivalent) copies meeting-notes docs out of
Google Drive into a local folder, `config.NOTES_DIR`. This module ONLY reads
those local files: the bot holds no Google credential and never writes anything
back to Drive. It is the reader behind the `sales_meeting_notes` source in
sources.py.

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
    """Return {date, label, path, title} for a meeting-note file, or None when it
    has no parseable date (the only thing that disqualifies a file here).

    Reads the document's first lines only when the FILENAME alone doesn't carry a
    date — a cheap fast path for the common "2026-08-14 Pipeline review.docx"
    case, with a fallback for terse filenames whose date lives in the header.
    """
    stem = os.path.splitext(name)[0]

    head = ""
    d = _parse_date(stem)
    # freeform=True: a filename's leftover words ARE its label.
    label = _parse_label(stem, freeform=True)

    if d is None or label is None:
        head = _extract_text(path)[:600]
        if d is None:
            d = _parse_date(head)
        if label is None:
            # freeform stays OFF here — the body's leftover words are the
            # meeting's content, not its name.
            label = _parse_label(head)

    # No date anywhere → not a meeting note (it's a stray file in the folder).
    if d is None:
        return None

    # Prefer the filename as the title; fall back to the doc's own first line.
    title = stem.strip() or (head.strip().splitlines() or [name])[0][:160]
    return {"date": d.isoformat(), "label": label, "path": path, "title": title.strip()[:160]}


# -- public API --------------------------------------------------------------


def list_notes(days: int = 14, *, notes_dir: Optional[str] = None) -> list[dict]:
    """Parse NOTES_DIR for meeting notes from the last `days`. Returns
    [{date, label, path, title}] newest first. [] when disabled/empty."""
    notes_dir = _resolve_dir(notes_dir)
    if not notes_dir:
        return []
    cutoff = (_utcnow() - timedelta(days=max(0, int(days)))).date()
    out: list[dict] = []
    for path, name in _candidate_files(notes_dir):
        meta = _meta_for_file(path, name)
        if not meta:
            continue
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
    log.info("[notes] list_notes(days=%d) → %d note(s)", days, len(out))
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
    """True when notes access is usable: NOTES_DIR is set AND the folder exists.
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


def sync_now(sync_cmd: str, *, timeout: float = 25.0) -> bool:
    """Run the rclone sync command (shell) to pull the freshest notes before a
    read. Best-effort: logs and returns False on any failure/timeout — never
    raises. Blocking; call via asyncio.to_thread from async code."""
    if not (sync_cmd or "").strip():
        return False
    log.info("[notes] sync-on-demand: running %r (timeout=%ss)", sync_cmd, timeout)
    try:
        proc = subprocess.run(
            sync_cmd, shell=True, timeout=timeout,
            capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        log.warning("[notes] sync command timed out after %ss", timeout)
        return False
    except Exception:
        log.exception("[notes] sync command raised")
        return False
    if proc.returncode != 0:
        log.warning(
            "[notes] sync exited %s; stderr=%s", proc.returncode, (proc.stderr or "")[:300]
        )
        return False
    log.info("[notes] sync-on-demand complete")
    return True


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

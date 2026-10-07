"""Meeting notes — READ-ONLY context for answering questions.

This module owns BOTH halves of the meeting-notes pipeline:

  SYNC   it runs `config.NOTES_SYNC_CMD` (a full rclone command) to pull the
         docs of ONE Drive folder down into `config.NOTES_DIR` — at startup,
         every NOTES_SYNC_MINUTES, and on demand before a notes question is
         answered. The command is the only thing that touches Drive; the bot
         itself holds no Google credential and never writes anything back.
  FILTER it decides what on disk is readable. Nothing is, unless it can be
         shown to have come from that one folder.

THE FILTER IS AN ALLOWLIST, AND THE FOLDER IS THE TAG. A file is read only when
ALL of these hold, in this order (`_classify`):

  1. it is in the MANIFEST for the current source — so it came down from the
     folder named in `config.NOTES_SOURCE_FOLDER`, by the configured command;
  2. it has a parseable date (what makes a file a meeting note at all);
  3. its TITLE does not match the standup guard — a built-in floor
     (`_STANDUP_FLOOR`) plus `config.NOTES_EXCLUDE_TITLE_PATTERNS`;
  4. `config.NOTES_REQUIRE_TITLE_TAGS` is empty (the default), or the title
     carries one of the tags.

WHY IT IS NO LONGER EXCLUDE-ONLY. Until 6 Oct the rule was "read everything in
NOTES_DIR except titles containing 'AM sync' / 'PM sync'". The folder held 76
files left over from an old, broader sync; 74 were standups and were held back,
and two internal product calls ("PM Call …") were not. Asked "what do we need
to do today?", the sales bot answered from one of them. An exclude list can
only ever name what somebody already thought of; everything else gets in. So
the direction is inverted: nothing is readable until it is shown to be from the
sales folder, and the standup guard is a second lock, not the only one.

THE MANIFEST (`STATE_DIR/notes_manifest.json`) is how "came from the folder" is
known on disk. It records the source folder's name, the absolute NOTES_DIR and
the files the sync left there. No manifest, an unreadable one, or one written
for a different folder or directory means NOTHING is from the folder. It lives
in STATE_DIR, not NOTES_DIR, so a mirroring sync cannot delete it.

THE SWEEP AND THE QUARANTINE. Immediately before every sync, any file at the
top level of NOTES_DIR that the manifest does not vouch for is MOVED — never
deleted — to `NOTES_DIR/_quarantine/<timestamp>/`. Changing the source folder
invalidates the whole manifest, so the first sync afterwards moves everything
that was there. `_quarantine` is never listed, scanned or read, at any depth:
only regular files at the TOP LEVEL of NOTES_DIR are ever candidates.

THE FLOOR. The seven standup titles in `_STANDUP_FLOOR` cannot be configured
away. The live `.env` pinned the old two-pattern value, so a wider default
would never have reached the server; and "set it empty to load everything" is
exactly the switch that must not exist on a sales-only bot.

THE HISTORY THAT MADE THE OLD RULE, AND WHY IT DOES NOT COME BACK. An earlier
include-list (a sales attendee, or a sales word in the title) admitted ZERO of
72 synced docs, silently, and looked exactly like "no meetings happened". This
allowlist asks nothing of a title — the folder is the tag — and it cannot fail
silently: every scan counts each file into exactly one bucket, the counts are
logged on every sync and every change, a WARNING names the bucket that took
everything when files came from the folder and none loaded, and the same
counts are in `sync_status()` for the source status and state/summary.json.

A sync failure NEVER raises and never crashes the bot: it is recorded, reported
through `sync_status()`, and logged ONCE per distinct failure (a failure that
repeats every 30 minutes must not fill the log).

WHAT THE CHANNEL HEARS WHEN THERE IS NOTHING TO READ is one of three fixed
sentences (`SAY_NOT_CONNECTED`, `SAY_EMPTY`, `SAY_UNREACHABLE`) and nothing
else — no reason, no fix, no count, and never a pointer at another source. The
reasons and the counts are for the operator: the log and the source status.

Generalised from the PM bot's standup reader, so it is no longer tied to
twice-daily engineering syncs:

  - ANY dated note in the sales folder counts, not just "Notes by Gemini"
    exports. Gemini notes still work — they're a special case of the general
    rule, and their `Summary` / `Decisions` / `Next steps` structure is still
    parsed when present.
  - `session` is now a free-form LABEL ("pipeline review", "Acme call") rather
    than an AM/PM enum, because sales meetings aren't a standup.

Format-tolerant by design: the on-disk format depends on how the sync is
configured (Google Docs commonly export to .docx, but .txt / .md / .html are all
common). `_extract_text` handles those with the stdlib only — no new deps.

Everything degrades gracefully: an unset/empty/missing NOTES_DIR, an unreadable
file, or an unknown format yields [] / None rather than raising.
`source_state()` distinguishes "not connected", "unreachable", "empty" and
"ok", which callers must keep distinct — reporting a folder it can't see as
"nothing was discussed" is the one failure this module exists to prevent.
"""
import html
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Optional
import deadlines as dl

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
    # THE REAL CLOCK, IN IST: sync timing and scan stamps are about the
    # machine. The "last N days" window below uses the bot's date instead.
    return dl.real_now_ist()


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


# -- the allowlist -----------------------------------------------------------
# Deny by default. A file is readable only when the manifest says it came from
# the one sales folder AND it is dated AND its title is not a standup's AND it
# carries a required tag when tags are required. See the module docstring for
# why this replaced the exclude-only rule.

#: THE FLOOR: standup titles that are refused whatever the configuration says.
#: NOTES_EXCLUDE_TITLE_PATTERNS can only ADD to these. They are the real titles
#: of the AM/PM product standups in Drive ("NFThing Kick-off ( AM Sync) – …",
#: "NFThing Wrap-up ( PM Sync) – …") and the usual spellings of "standup". A bare
#: "sync" is deliberately NOT here: it would refuse a real "Sales sync".
_STANDUP_FLOOR = (
    "am sync", "pm sync", "nfthing kick-off", "nfthing wrap-up",
    "standup", "stand-up", "daily sync",
)

#: The subfolder of NOTES_DIR that strays are moved into. Never listed, never
#: scanned, never read — at any depth.
QUARANTINE_DIRNAME = "_quarantine"

MANIFEST_FILENAME = "notes_manifest.json"
MANIFEST_VERSION = 1

# The states of the notes source. Exactly one holds at any time.
STATE_NOT_CONFIGURED = "not_configured"
STATE_MISCONFIGURED = "misconfigured"
STATE_UNREACHABLE = "unreachable"
STATE_EMPTY = "empty"
STATE_OK = "ok"

# WHAT THE CHANNEL HEARS WHEN THERE IS NOTHING TO READ. Exact text, agreed with
# the team: plain words, and never a pointer at any other source. `{folder}` is
# NOTES_SOURCE_FOLDER — the folder's name is configuration, never a constant.
SAY_NOT_CONNECTED = "Meeting notes aren't connected to me yet."
SAY_EMPTY = "There are no sales meeting notes in the {folder} folder yet."
SAY_UNREACHABLE = "I can't reach the sales notes folder right now, so I haven't checked the notes."

# A DECISION, KEPT AS ONE SWITCH. When the folder synced successfully before
# and the LATEST sync fails, the files on disk are the sales folder's own last
# good copy. The team decided (7 Oct) to keep answering from them: True — the
# tool result says the sync is degraded, so the answer says it may be stale.
# False would read nothing and say SAY_UNREACHABLE until a sync succeeds again.
# `source_state` is the only reader of this.
_SERVE_LAST_GOOD_COPY_WHEN_SYNC_FAILS = True


def _cfg(name: str, default):
    """Read a config value without importing config at module import time (this
    module is deliberately importable on its own, e.g. from a test or a script)."""
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


def _cfg_str(name: str) -> str:
    return str(_cfg(name, "") or "").strip()


def _norm_title(text: str) -> str:
    """A title (or a pattern) reduced to lowercase words separated by one space.

    Every run of non-alphanumeric characters becomes one space, so the things
    that vary between a Drive title, its rclone-sanitised filename and what a
    human types — "／" for "/", "：" for ":", "–", "(", "-", "_", doubled spaces —
    stop mattering: "AM-Sync", "am  sync" and "( AM Sync)" are all "am sync".
    """
    return re.sub(r"[\W_]+", " ", str(text or "").casefold()).strip()


def _title_matches(title: str, patterns) -> Optional[str]:
    """The first pattern that appears in the title ON WORD BOUNDARIES, else None.

    Word boundaries, not a raw substring: "pm sync" must not match "Program
    sync", and "standup" must not match half of another word. Both sides are
    normalised the same way and padded with a space, so a plain substring test
    is a whole-word test.
    """
    hay = f" {_norm_title(title)} "
    for pattern in patterns or ():
        needle = _norm_title(pattern)
        if needle and f" {needle} " in hay:
            return pattern
    return None


def standup_patterns() -> list[str]:
    """The effective standup guard: the floor plus NOTES_EXCLUDE_TITLE_PATTERNS,
    normalised and de-duplicated, floor first.

    The configured list only ever ADDS. An empty value therefore means "just
    the floor", never "load everything" — which is what makes a stale or blank
    line in a server's .env safe.
    """
    out: list[str] = []
    configured = _cfg("NOTES_EXCLUDE_TITLE_PATTERNS", []) or []
    for raw in list(_STANDUP_FLOOR) + [str(p) for p in configured]:
        norm = _norm_title(raw)
        if norm and norm not in out:
            out.append(norm)
    return out


def is_standup_title(text: str) -> bool:
    """Does this title — or a citation built from one — name a product standup?
    Public because the to-do sheet applies the same guard to its "Source
    meeting" cells (todos.py)."""
    return _title_matches(text, standup_patterns()) is not None


def _required_tags() -> list[str]:
    """NOTES_REQUIRE_TITLE_TAGS, normalised. Empty by default: the folder is the
    tag, and nobody should have to rename a meeting to have it read."""
    out: list[str] = []
    for raw in _cfg("NOTES_REQUIRE_TITLE_TAGS", []) or []:
        norm = _norm_title(raw)
        if norm and norm not in out:
            out.append(norm)
    return out


def _classify(title: str, in_manifest: bool) -> tuple[bool, str, str]:
    """Should this DATED note be loaded? Returns (load_it, bucket, reason).

    The checks run in a fixed order and the first that fails names the bucket,
    so every file is counted exactly once: `not_from_folder`,
    `excluded_standup`, `missing_tag`, or `loaded`. (`undated` is decided by
    the caller, between the first check and the second, because it needs the
    document's own text and a file that is not from the folder is never opened.)

    Matched on the TITLE only — the filename stem, or the document's first line
    when the filename carries no title. Deliberately not the body: a sales call
    whose notes QUOTE "the AM sync" is still a sales call.

    The reason is kept on every note so the logs can say WHY a doc was loaded
    or held back, rather than leaving the filter a black box.
    """
    if not in_manifest:
        return False, "not_from_folder", "not in the manifest of the sales notes folder"
    hit = _title_matches(title, standup_patterns())
    if hit:
        return False, "excluded_standup", f"title matches the standup pattern {hit!r}"
    tags = _required_tags()
    if tags and not _title_matches(title, tags):
        return False, "missing_tag", f"title carries none of the required tags {tags!r}"
    return True, "loaded", "loaded (from the sales notes folder)"


# -- the source: what is configured, and what the manifest vouches for --------


def _split_cmd(cmd: str) -> list[str]:
    """The command as shell-ish tokens, quoted strings kept whole (and kept in
    their quotes). Used only to tell `rclone sync` from `rclone copy` without
    being fooled by a folder NAMED "… sync …"."""
    try:
        return shlex.split(cmd or "", posix=False)
    except ValueError:
        return (cmd or "").split()


def sync_mode(cmd: Optional[str] = None) -> str:
    """"mirror" (rclone sync), "copy" (rclone copy) or "other".

    It matters because the two fail differently. A copy never removes anything,
    so a note taken OUT of the Drive folder stays readable. A mirror removes it
    — and would also remove the quarantine, which is why a mirror must carry an
    exclude for it (`_config_problem`).
    """
    if cmd is None:
        cmd = _cfg_str("NOTES_SYNC_CMD")
    tokens = [t.lower() for t in _split_cmd(cmd)]
    at = None
    for i, token in enumerate(tokens):
        exe = re.split(r"[\\/]", token.strip("\"'"))[-1]
        if exe in ("rclone", "rclone.exe"):
            at = i
            break
    if at is None:
        return "other"
    rest = tokens[at + 1:]
    if "sync" in rest:
        return "mirror"
    if "copy" in rest:
        return "copy"
    return "other"


def _config_problem(cmd: Optional[str] = None) -> tuple[Optional[str], str, str]:
    """(state, what is wrong, the fix) — state is None when the configuration
    is usable, else STATE_NOT_CONFIGURED or STATE_MISCONFIGURED.

    NOT CONFIGURED needs all three of NOTES_DIR, NOTES_SOURCE_FOLDER and
    NOTES_SYNC_CMD, not just the command: the live .env already had a command
    (pointing at a folder that does not exist), so "a command is set" cannot be
    what connects the source. Naming the folder is the deliberate act.

    MISCONFIGURED is a command the bot refuses to run: one that does not name
    the folder (so nothing ties what it pulls to the tag), or a mirror with no
    exclude for the quarantine (rclone would delete what the bot set aside).
    """
    if cmd is None:
        cmd = _cfg_str("NOTES_SYNC_CMD")
    cmd = (cmd or "").strip()
    folder = _cfg_str("NOTES_SOURCE_FOLDER")
    missing = [
        name for name, value in (
            ("NOTES_DIR", _cfg_str("NOTES_DIR")),
            ("NOTES_SOURCE_FOLDER", folder),
            ("NOTES_SYNC_CMD", cmd),
        ) if not value
    ]
    if missing:
        return (
            STATE_NOT_CONFIGURED,
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set.",
            "Set NOTES_DIR, NOTES_SOURCE_FOLDER and NOTES_SYNC_CMD (DEPLOY.md, "
            "meeting notes).",
        )
    if folder not in cmd:
        return (
            STATE_MISCONFIGURED,
            f"NOTES_SYNC_CMD does not name the folder in NOTES_SOURCE_FOLDER ({folder!r}).",
            "Make NOTES_SYNC_CMD pull from exactly that folder — the name must appear "
            "in the command character for character.",
        )
    if sync_mode(cmd) == "mirror" and QUARANTINE_DIRNAME not in cmd:
        return (
            STATE_MISCONFIGURED,
            "NOTES_SYNC_CMD mirrors (rclone sync) without excluding the quarantine, so "
            "it would delete the files I set aside.",
            f'Add --exclude "/{QUARANTINE_DIRNAME}/**" to NOTES_SYNC_CMD.',
        )
    return None, "", ""


def _abs_dir(notes_dir: str) -> str:
    """One spelling of a directory, so "./notes" and its absolute path compare
    equal in the manifest."""
    return os.path.normcase(os.path.abspath(notes_dir)) if notes_dir else ""


def _manifest_path() -> str:
    """STATE_DIR/notes_manifest.json. In STATE_DIR, not NOTES_DIR, so a
    mirroring sync can never delete the bot's record of what it pulled."""
    return os.path.join(_cfg_str("STATE_DIR") or "./state", MANIFEST_FILENAME)


def _load_manifest(notes_dir: Optional[str] = None) -> Optional[dict]:
    """The manifest, ONLY when it vouches for the current source and directory.

    None for no file, an unreadable file, a different NOTES_SOURCE_FOLDER or a
    different NOTES_DIR — and None means nothing on disk is from the folder.
    That is the whole fail-closed rule: changing the folder's name in .env
    makes every note unreadable at once, before any sync has run.
    """
    folder = _cfg_str("NOTES_SOURCE_FOLDER")
    if notes_dir is None:
        notes_dir = _cfg_str("NOTES_DIR")
    if not folder or not notes_dir:
        return None
    try:
        with open(_manifest_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        log.warning("[notes] the manifest %s is unreadable; treating every file as a stray",
                    _manifest_path(), exc_info=True)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    if data.get("source") != folder or data.get("notes_dir") != _abs_dir(notes_dir):
        return None
    return data


def _write_manifest(notes_dir: str, files: list[str], last_success: Optional[str]) -> bool:
    """Record what the sync left in NOTES_DIR. Written to a temp file and
    replaced, so a reader never sees half a manifest. Returns False (and logs)
    when it cannot be written — in which case nothing is readable, which is the
    safe side of that failure."""
    path = _manifest_path()
    data = {
        "version": MANIFEST_VERSION,
        "source": _cfg_str("NOTES_SOURCE_FOLDER"),
        "notes_dir": _abs_dir(notes_dir),
        "written_at": _utcnow().isoformat(),
        "last_success": last_success,
        "files": sorted(files),
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception:
        log.error(
            "[notes] could not write the manifest %s — no meeting note is readable "
            "until STATE_DIR is writable.", path, exc_info=True,
        )
        return False


def _sweep_strays(notes_dir: str, manifest: Optional[dict]) -> list[str]:
    """MOVE every top-level file the manifest does not vouch for into
    NOTES_DIR/_quarantine/<YYYYMMDD-HHMMSS>/. Returns the names it could NOT
    move (normally []).

    Runs immediately before a sync, so whatever is at the top level afterwards
    arrived from the configured command. NEVER deletes and never overwrites: a
    name already taken in the quarantine folder gets a numeric suffix. Never
    raises — a file that cannot be moved (locked, no permission, or the
    quarantine folder could not be created) stays where it is and is reported.

    THE STUCK ONES ARE THE RETURN VALUE, AND THEY MATTER. A stray that is still
    at the top level after the sweep would otherwise be written into the next
    manifest and become readable — the opposite of what was just logged. So the
    caller keeps every returned name OUT of the manifest. The price, accepted:
    if the sync itself brings down a file of the same name, that file is left
    out too until the stray is gone. The sweep retries on every sync.

    One WARNING and one audit event per sweep that moved anything; a sweep that
    moves nothing is silent.
    """
    known = set((manifest or {}).get("files") or [])
    strays = [(path, name) for path, name in _candidate_files(notes_dir) if name not in known]
    if not strays:
        return []
    dest = os.path.join(notes_dir, QUARANTINE_DIRNAME, _utcnow().strftime("%Y%m%d-%H%M%S"))
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError:
        log.error("[notes] could not create the quarantine folder %s; %d stray file(s) "
                  "stay in place and stay UNREAD", dest, len(strays), exc_info=True)
        return [name for _, name in strays]
    moved: list[str] = []
    stuck: list[str] = []
    for path, name in strays:
        target = os.path.join(dest, name)
        stem, ext = os.path.splitext(name)
        n = 1
        while os.path.exists(target):
            target = os.path.join(dest, f"{stem}.{n}{ext}")
            n += 1
        try:
            shutil.move(path, target)
            moved.append(name)
        except Exception:
            stuck.append(name)
            log.error("[notes] could not move the stray file %r to the quarantine; it "
                      "stays in place and stays UNREAD", name, exc_info=True)
    if moved:
        _SYNC["quarantined"] = int(_SYNC.get("quarantined") or 0) + len(moved)
        folder = _cfg_str("NOTES_SOURCE_FOLDER")
        log.warning(
            "[notes] moved %d file(s) that did not come from %s to %s — nothing was deleted",
            len(moved), folder, dest,
        )
        try:
            import state
            state.audit(
                "notes_quarantined",
                reason="files in NOTES_DIR that the manifest of the sales notes folder "
                       "does not vouch for are set aside: never read, never deleted",
                source_folder=folder, moved=len(moved), to=dest, names=moved[:20],
            )
        except Exception:
            log.debug("[notes] could not audit the quarantine sweep", exc_info=True)
    return stuck


def source_state() -> str:
    """Exactly one of not_configured / misconfigured / unreachable / empty / ok.

      not_configured  NOTES_DIR, NOTES_SOURCE_FOLDER or NOTES_SYNC_CMD is empty
      misconfigured   the command is one the bot refuses to run
      unreachable     configured, but the folder has never been pulled for this
                      source — or it is empty and that emptiness was never (or
                      is no longer) confirmed by a clean sync
      empty           a clean sync says the folder holds nothing
      ok              the manifest lists files

    In the first two the sync never runs and no file is moved or opened, so a
    deploy with an untouched .env changes nothing on disk.
    """
    problem, _, _ = _config_problem()
    if problem:
        return problem
    notes_dir = _cfg_str("NOTES_DIR")
    if not os.path.isdir(notes_dir):
        return STATE_UNREACHABLE
    manifest = _load_manifest(notes_dir)
    if manifest is None:
        return STATE_UNREACHABLE
    failing = _SYNC.get("ok") is False
    if not manifest["files"]:
        if not manifest.get("last_success") or failing:
            return STATE_UNREACHABLE
        return STATE_EMPTY
    if failing and not _SERVE_LAST_GOOD_COPY_WHEN_SYNC_FAILS:
        return STATE_UNREACHABLE
    return STATE_OK


def nothing_to_say() -> Optional[str]:
    """The one sentence to say about meeting notes when none can be read; None
    when at least one note is loaded.

    A folder that holds only standups (or only untagged or undated files) reads
    as EMPTY to the channel: from where the team sits there are no sales notes
    in it. Which bucket took them is the operator's business and is in the log.
    """
    state_now = source_state()
    if state_now in (STATE_NOT_CONFIGURED, STATE_MISCONFIGURED):
        return SAY_NOT_CONNECTED
    if state_now == STATE_UNREACHABLE:
        return SAY_UNREACHABLE
    if state_now == STATE_OK:
        notes_dir = _resolve_dir(None)
        if notes_dir and _scan(notes_dir)[1]:
            return None
    return SAY_EMPTY.format(folder=_cfg_str("NOTES_SOURCE_FOLDER"))


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
    """Yield (path, filename) for every regular file at the TOP LEVEL of
    NOTES_DIR — and nothing else.

    THE QUARANTINE GUARANTEE LIVES HERE. This is the only place the folder is
    listed, it never recurses, and it skips the name `_quarantine` outright. So
    nothing under NOTES_DIR/_quarantine — at any depth — can be listed, counted,
    read, cited or written into the manifest, whatever its title or date. The
    consequence for whoever fills the Drive folder: notes must sit at its top
    level; a subfolder is not read.
    """
    try:
        entries = sorted(os.listdir(notes_dir))
    except OSError:
        log.info("[notes] cannot list dir %r; treating as empty", notes_dir)
        return
    for name in entries:
        if name == QUARANTINE_DIRNAME:
            continue
        path = os.path.join(notes_dir, name)
        if os.path.isfile(path):
            yield path, name


def _meta_for_file(path: str, name: str, in_manifest: bool = False) -> dict:
    """Return {date, label, path, title, loaded, filter_reason, bucket} for a
    file in NOTES_DIR. `bucket` is the ONE place the file is counted:
    not_from_folder, undated, excluded_standup, missing_tag or loaded.

    A FILE THAT IS NOT FROM THE FOLDER IS NEVER OPENED. Its bucket is decided
    from the manifest alone, so a stray document's text is not even read to
    look for a date.

    For a file that IS from the folder, the head of the document is read as a
    FALLBACK, for the date and the label when the filename carries neither. No
    date anywhere means it is not a meeting note: `date` is None and the bucket
    is `undated`. `_scan` caches the result per (path, mtime, size), so a
    folder of 300 docs is parsed once per change, not once per question.
    """
    stem = os.path.splitext(name)[0]
    if not in_manifest:
        d = _parse_date(stem)
        title = (stem.strip() or name)[:160]
        _, bucket, why = _classify(title, False)
        return {
            "date": d.isoformat() if d else None,
            "label": None,
            "path": path,
            "title": title,
            "loaded": False,
            "filter_reason": why,
            "bucket": bucket,
        }

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

    # Prefer the filename as the title; fall back to the doc's own first line.
    title = stem.strip() or (head.strip().splitlines() or [name])[0][:160]
    title = title.strip()[:160]

    # No date anywhere -> not a meeting note (it's a stray file in the folder).
    if d is None:
        return {
            "date": None,
            "label": label,
            "path": path,
            "title": title,
            "loaded": False,
            "filter_reason": "no parseable date in the filename or the first lines",
            "bucket": "undated",
        }

    loaded, bucket, why = _classify(title, True)
    return {
        "date": d.isoformat(),
        "label": label,
        "path": path,
        "title": title,
        "loaded": loaded,
        "filter_reason": why,
        "bucket": bucket,
    }


# path -> (key, meta). Parsing every doc's head on every status probe would
# re-read the whole folder several times per question; the folder only changes
# when a sync runs, so cache on the file's own stat — plus everything else the
# classification depends on (the manifest and the pattern/tag lists), so a new
# manifest or a config change reclassifies every file.
_SCAN_CACHE: dict[str, tuple[tuple, dict]] = {}

# What the last scan saw. `sync_status()` reports these, so the source can state
# where every file went instead of a bare "connected". The five buckets
# (not_from_folder, undated, excluded_docs, missing_tag, loaded_docs) always sum
# to docs_seen.
_STATS: dict = {
    "docs_seen": 0,        # every file at the top level of the folder
    "from_folder": 0,      # …that the manifest vouches for
    "not_from_folder": 0,  # …that it does not (unread; moved at the next sync)
    "notes_seen": 0,       # from the folder AND parsed as a dated meeting note
    "undated": 0,          # from the folder, no parseable date
    "excluded_docs": 0,    # from the folder, dated, held back as a standup
    "missing_tag": 0,      # from the folder, dated, lacking a required tag
    "loaded_docs": 0,      # cleared every check
    "scanned_at": None,
}

# The counts line last logged at INFO, and the "nothing loaded" case last
# warned about. A scan runs on every question; the log gets one line per
# CHANGE, and the warning once per distinct situation.
_LAST_COUNTS_LOGGED: Optional[str] = None
_LAST_ZERO_WARNED: Optional[tuple] = None


def _report_counts(*, force: bool = False) -> None:
    """THE OPERATOR'S LINE — never the channel's. One INFO line saying where
    every file on disk went, on each sync (`force`) and whenever it changes;
    DEBUG otherwise.

    And the guard against the old "0 of 72 admitted, silently" failure: when
    files came from the folder and NONE loaded, a WARNING names the bucket that
    took them and the setting to look at. Once per distinct situation, so a
    question a minute does not repeat it.
    """
    global _LAST_COUNTS_LOGGED, _LAST_ZERO_WARNED
    folder = _cfg_str("NOTES_SOURCE_FOLDER")
    line = (
        f"source={folder or '(none)'} state={source_state()} "
        f"on_disk={_STATS['docs_seen']} from_folder={_STATS['from_folder']} "
        f"loaded={_STATS['loaded_docs']} standup={_STATS['excluded_docs']} "
        f"missing_tag={_STATS['missing_tag']} undated={_STATS['undated']} "
        f"quarantined={int(_SYNC.get('quarantined') or 0)}"
    )
    if force or line != _LAST_COUNTS_LOGGED:
        _LAST_COUNTS_LOGGED = line
        log.info("[notes] %s", line)
    else:
        log.debug("[notes] %s", line)

    if not (_STATS["from_folder"] and not _STATS["loaded_docs"]):
        _LAST_ZERO_WARNED = None
        return
    signature = (folder, _STATS["from_folder"], _STATS["excluded_docs"],
                 _STATS["missing_tag"], _STATS["undated"])
    if signature == _LAST_ZERO_WARNED:
        return
    _LAST_ZERO_WARNED = signature
    buckets = {
        "excluded_standup": (
            _STATS["excluded_docs"],
            "their titles match the standup guard (the built-in standup titles plus "
            "NOTES_EXCLUDE_TITLE_PATTERNS). Standup notes do not belong in the sales "
            "folder; if a real sales meeting is caught, look at "
            "NOTES_EXCLUDE_TITLE_PATTERNS",
        ),
        "missing_tag": (
            _STATS["missing_tag"],
            "their titles carry none of NOTES_REQUIRE_TITLE_TAGS "
            f"({list(_cfg('NOTES_REQUIRE_TITLE_TAGS', []) or [])}). Clear "
            "NOTES_REQUIRE_TITLE_TAGS to read every note in the folder",
        ),
        "undated": (
            _STATS["undated"],
            "they have no date in the filename or the first lines, and a date is what "
            "makes a file a meeting note",
        ),
    }
    name, (count, why) = max(buckets.items(), key=lambda kv: kv[1][0])
    log.warning(
        "[notes] %d file(s) came from %s and NONE loaded: standup=%d missing_tag=%d "
        "undated=%d. The bucket that took the most is %s (%d): %s. The channel is told "
        "there are no sales notes in the folder.",
        _STATS["from_folder"], folder, _STATS["excluded_docs"], _STATS["missing_tag"],
        _STATS["undated"], name, count, why,
    )


def _scan(notes_dir: str, *, report: bool = True) -> tuple[list[dict], list[dict]]:
    """Classify every top-level file in NOTES_DIR. Returns (all_notes,
    loaded_notes) and refreshes `_STATS`. `all_notes` is every DATED file,
    whatever its bucket — and held-back ones are ONLY there, for counting — so
    callers can report how much was held back without ever reading it.

    The manifest is loaded once per scan. With no usable configuration there is
    no manifest to trust, so every file is `not_from_folder` and none is opened.
    """
    manifest = _load_manifest(notes_dir) if _config_problem()[0] is None else None
    known = set(manifest["files"]) if manifest else set()
    stamp = manifest.get("written_at") if manifest else None
    rules = (stamp, tuple(standup_patterns()), tuple(_required_tags()))

    all_notes: list[dict] = []
    loaded_notes: list[dict] = []
    counts = {"not_from_folder": 0, "undated": 0, "excluded_standup": 0,
              "missing_tag": 0, "loaded": 0}
    docs = 0
    live: set[str] = set()

    for path, name in _candidate_files(notes_dir):
        docs += 1
        live.add(path)
        in_manifest = name in known
        try:
            st = os.stat(path)
            key = (st.st_mtime, st.st_size, in_manifest, rules)
        except OSError:
            # Vanished between the listing and the stat. Still one file seen, so
            # it is counted somewhere: as not readable.
            counts["not_from_folder"] += 1
            continue
        cached = _SCAN_CACHE.get(path)
        if cached is not None and cached[0] == key:
            meta = cached[1]
        else:
            meta = _meta_for_file(path, name, in_manifest)
            _SCAN_CACHE[path] = (key, meta)
            log.debug("[notes] %s %s — %s", meta["bucket"], name, meta["filter_reason"])
        counts[meta["bucket"]] += 1
        if meta["date"] is None:
            continue
        all_notes.append(meta)
        if meta["loaded"]:
            loaded_notes.append(meta)

    for gone in [p for p in _SCAN_CACHE if p not in live]:
        _SCAN_CACHE.pop(gone, None)

    from_folder = docs - counts["not_from_folder"]
    _STATS.update(
        docs_seen=docs,
        from_folder=from_folder,
        not_from_folder=counts["not_from_folder"],
        notes_seen=from_folder - counts["undated"],
        undated=counts["undated"],
        excluded_docs=counts["excluded_standup"],
        missing_tag=counts["missing_tag"],
        loaded_docs=counts["loaded"],
        scanned_at=_utcnow().isoformat(),
    )
    if report:
        _report_counts()
    return all_notes, loaded_notes


# -- public API --------------------------------------------------------------


def list_notes(
    days: int = 14,
    *,
    notes_dir: Optional[str] = None,
    include_excluded: bool = False,
) -> list[dict]:
    """The SALES meeting notes from the last `days`, newest first, as
    [{date, label, path, title, loaded, filter_reason, bucket}]. [] unless the
    source state is `ok`.

    This is the only door into the notes, and it is the door the allowlist
    guards: a file that is not from the sales folder, or whose title is a
    standup's, is never returned here, so it is never read, never summarised
    and never put in front of the model. Every other reader in the bot — the
    tools, the citations, the nudge evidence, the prep briefs — comes through
    here. `include_excluded=True` exists for diagnostics and counting ("what
    else is in there?") and must not be used to build an answer.
    """
    notes_dir = _resolve_dir(notes_dir)
    if not notes_dir:
        return []
    all_notes, loaded_notes = _scan(notes_dir)
    if source_state() != STATE_OK:
        loaded_notes = []
    considered = all_notes if include_excluded else loaded_notes

    # A DATE DECISION: "the last N days" as the bot reckons today (IST,
    # pretend date included).
    cutoff = dl.today_ist() - timedelta(days=max(0, int(days)))
    out: list[dict] = []
    for meta in considered:
        try:
            d = date.fromisoformat(meta["date"])
        except (TypeError, ValueError):
            continue
        if d < cutoff:
            continue
        out.append(meta)
    # Newest first. Within one day, labels sort in reverse alphabetical order —
    # arbitrary but STABLE, which is what matters: "the most recent note" must
    # mean the same file on every call.
    out.sort(key=lambda m: (m["date"], m.get("label") or ""), reverse=True)
    log.debug("[notes] list_notes(days=%d) -> %d note(s) in window", days, len(out))
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
    state how current the data is. (None, None) when disabled/empty.

    Both halves describe SALES notes only: the date is the newest loaded note's,
    and the mtime is taken over the loaded notes' own files — a stray file
    sitting in the folder must not make the notes look fresher than they are."""
    notes_dir = _resolve_dir(notes_dir)
    if not notes_dir:
        return (None, None)
    found = list_notes(days=3650, notes_dir=notes_dir)
    latest_date = found[0]["date"] if found else None

    newest_mtime = None
    try:
        mtimes = [os.path.getmtime(n["path"]) for n in found]
        if mtimes:
            newest_mtime = datetime.fromtimestamp(max(mtimes), tz=dl.IST).isoformat()
    except OSError:
        log.debug("[notes] mtime scan failed", exc_info=True)
    return (latest_date, newest_mtime)


def is_configured(*, notes_dir: Optional[str] = None) -> bool:
    """True when the notes source is CONNECTED: NOTES_DIR, NOTES_SOURCE_FOLDER
    and NOTES_SYNC_CMD are all set and the command is one the bot will run.
    It says nothing about whether the folder is reachable or holds anything —
    `source_state()` and `nothing_to_say()` answer that.
    Distinguishes 'not connected' (say so) from 'connected but no note for that
    day' (found=false) so callers never conflate the two — reporting a folder
    the bot can't see as 'nothing was discussed' is the failure this guards
    against. `notes_dir` is accepted for compatibility and not consulted: being
    connected is a property of the configuration, not of a path."""
    return _config_problem()[0] is None


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
# rclone, or with an expired token, must still run — saying it cannot reach the
# sales notes (or that its last good copy may be stale), not crashing, and never
# reading anything else in their place.

_SYNC_LOCK = threading.Lock()

_SYNC: dict = {
    "last_attempt": None,   # iso — when the command last ran
    "last_success": None,   # iso — when it last ran cleanly
    "ok": None,             # None = never run, else the last outcome
    "error": None,          # one short line, the failure itself
    "remedy": None,         # one short line, what to DO about it
    "runs": 0,
    "quarantined": 0,       # files this process has moved to the quarantine
}
# Signature of the failure we last logged at ERROR. A sync that fails every 30
# minutes must produce ONE error in the log, not one per tick — so a repeat of
# the same failure drops to DEBUG, and a NEW failure is loud again.
_LAST_LOGGED_FAILURE: Optional[str] = None


def ensure_dir(notes_dir: Optional[str] = None) -> str:
    """Make sure NOTES_DIR exists, creating it if it doesn't. Returns the path,
    or "" when no path is configured / it couldn't be created.

    rclone creates the destination itself, but the sweep that runs before it
    needs the folder to exist, and so does the quarantine inside it."""
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
            "its full path in NOTES_SYNC_CMD (e.g. C:\\rclone\\rclone.exe sync ...)."
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


def _log_once(signature: str, level: int, message: str, *args) -> None:
    """Log at `level` the first time `signature` is seen, DEBUG while it
    repeats. Shares the once-per-distinct-failure memory with the sync, so a
    timer that hits the same wall every 30 minutes writes one line."""
    global _LAST_LOGGED_FAILURE
    if signature != _LAST_LOGGED_FAILURE:
        _LAST_LOGGED_FAILURE = signature
        log.log(level, message, *args)
    else:
        log.debug(message, *args)


def _record_failure(error: str, remedy: str) -> None:
    """Mark the sync degraded and log it ONCE per distinct failure."""
    _SYNC.update(ok=False, error=error, remedy=remedy)
    _log_once(
        f"{error}|{remedy}", logging.ERROR,
        "[notes] SYNC FAILED — %s FIX: %s (further identical failures are logged at "
        "DEBUG; the meeting-notes source reports it, and nothing outside the sales "
        "notes folder is read instead)", error, remedy,
    )


def sync_now(
    sync_cmd: Optional[str] = None,
    *,
    timeout: Optional[float] = None,
    reason: str = "on demand",
) -> bool:
    """Run the sync command once. Returns True only on a clean exit.

    The order is the point:
      1. REFUSE when the source is not configured or misconfigured. Nothing
         runs and no file is touched, so an unedited .env changes nothing.
      2. SWEEP: move every file the manifest does not vouch for into the
         quarantine (`_sweep_strays`).
      3. RUN the command.
      4. WRITE THE MANIFEST from what is now at the top level — after a clean
         run AND after a failed one. Strays were swept first, so whatever is
         there arrived from the configured command; a stray the sweep could
         not move is left out by name.

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
    problem, what, fix = _config_problem(sync_cmd)
    if problem == STATE_NOT_CONFIGURED:
        log.debug("[notes] no %s sync — %s", reason, what)
        return False
    if problem:
        _log_once(
            f"{problem}|{what}", logging.ERROR,
            "[notes] SYNC NOT RUN — %s FIX: %s (meeting notes stay unread until this "
            "is fixed; nothing on disk was touched)", what, fix,
        )
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
        before = _load_manifest(notes_dir)
        stuck = set(_sweep_strays(notes_dir, before))

        _SYNC["last_attempt"] = _utcnow().isoformat()
        _SYNC["runs"] = int(_SYNC.get("runs") or 0) + 1
        log.info("[notes] sync (%s): running %r (timeout=%ss)", reason, sync_cmd, timeout)
        ok = False
        try:
            proc = subprocess.run(
                sync_cmd, shell=True, timeout=timeout, capture_output=True, text=True,
            )
        except subprocess.TimeoutExpired:
            _record_failure(
                f"the sync command did not finish within {timeout}s.",
                _remedy_for(None, "", sync_cmd),
            )
        except Exception as e:
            _record_failure(
                f"the sync command could not be started ({type(e).__name__}: {e}).",
                _remedy_for(1, str(e), sync_cmd),
            )
        else:
            # Collapse rclone's multi-line stderr: this string ends up in a log
            # line and a status detail, and a newline mid-sentence reads as two
            # separate messages in both.
            stderr = " ".join((proc.stderr or "").split())
            if proc.returncode != 0:
                _record_failure(
                    f"the sync exited {proc.returncode}: {stderr[:300] or 'no stderr output'}",
                    _remedy_for(proc.returncode, stderr, sync_cmd),
                )
            else:
                ok = True
                global _LAST_LOGGED_FAILURE
                _LAST_LOGGED_FAILURE = None
                _SYNC.update(ok=True, error=None, remedy=None,
                             last_success=_utcnow().isoformat())

        # THE MANIFEST, after every run. `last_success` is carried forward from
        # the previous manifest when this run failed, so "the folder was
        # reachable once" survives a restart.
        last_success = _SYNC.get("last_success") if ok else (before or {}).get("last_success")
        # A stray the sweep could not move is still at the top level. It did
        # not come from the folder, so it does not go in the manifest.
        files = [name for _, name in _candidate_files(notes_dir) if name not in stuck]
        if not _write_manifest(notes_dir, files, last_success):
            _SYNC.update(
                ok=False,
                error="the notes manifest could not be written.",
                remedy="Make STATE_DIR writable.",
            )
            ok = False

        _scan(notes_dir, report=False)
        # THE line per sync: where every file on disk went.
        _report_counts(force=True)
        return ok
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
    the model the notes may be stale instead of hiding it.

    "Configured" follows `source_state()`: with the source not configured or
    misconfigured there is nothing to sync, and nothing runs."""
    if _config_problem()[0] is not None:
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
    truthfully: the state of the source, when the sync last ran, and where every
    file on disk went — from the folder or not, loaded, held back as a standup,
    missing a tag, undated. The five buckets (docs_not_from_folder,
    docs_undated, docs_excluded, docs_missing_tag, docs_loaded) sum to
    docs_seen.

    OPERATOR-FACING. This dict carries the sync error and file counts; none of
    it may be handed to the model as something to say in the channel.

    Rescans the folder when nothing has scanned it yet, so a status call before
    the first sync still reports real numbers."""
    notes_dir = _resolve_dir(None)
    if notes_dir and not _STATS["scanned_at"]:
        _scan(notes_dir)
    return {
        "state": source_state(),
        "source_folder": _cfg_str("NOTES_SOURCE_FOLDER"),
        "docs_from_folder": _STATS["from_folder"],
        "docs_not_from_folder": _STATS["not_from_folder"],
        "docs_missing_tag": _STATS["missing_tag"],
        "docs_undated": _STATS["undated"],
        "require_tags": list(_cfg("NOTES_REQUIRE_TITLE_TAGS", []) or []),
        "quarantine_dir": (
            os.path.join(_cfg_str("NOTES_DIR"), QUARANTINE_DIRNAME)
            if _cfg_str("NOTES_DIR") else ""
        ),
        "quarantined": int(_SYNC.get("quarantined") or 0),
        "sync_mode": sync_mode(),
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
        # The EFFECTIVE guard: the built-in floor plus the configured additions.
        "exclude_patterns": standup_patterns(),
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

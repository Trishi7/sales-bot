"""NFT2-1062 — THE SALES BOT READS ONE FOLDER OF SALES NOTES, AND NOTHING ELSE.

    python verify_notes_scope.py

Offline: no network, no rclone, no Drive, no Discord, no model. The REAL
`notes.sync_now` runs, with a local "drive" folder as the source and a tiny Python
script as the sync command (its text carries the NOTES_SOURCE_FOLDER value, which
is what the real command does). Throwaway NOTES_DIR, STATE_DIR and *_test.db; the
real notes/ folder, .env and the real sheet are never touched.

Written from docs/plans/NFT2-1062.md (sections 2, 6 and 9), not from the code.

  E1   a doc in the sales folder AND somewhere internal         E6   to-do rows hidden (D1)
  E2   a sales doc that quotes sync content                     E6b  ...silently
  E3   standup docs INSIDE the folder, every title shape        E6c  ...logged once
  E3b  a bare "sync" is NOT refused (D2)                        E6d  tools/list_hidden_todos.py
  E4a  not configured       E4b  folder empty                   E7   title tags (D3)
  E4b2 only standups        E4c  unreachable                    E8   counts reach the operator
  E4d  misconfigured        E4e  the three sentences are clean  E9   routing (D5)
  E5   stale files are quarantined, never read, never deleted   E10  evidence + prep read allowed notes only
  E5b..d  source change, hand-dropped file, quarantine depth    PAR  test mode == live (E3, E4a, E9)
  D7 / D8  the two settled decisions (formerly Q1, Q2), one clearly labelled case each
  E5e  a stray that cannot be moved stays unread
"""
import asyncio
import contextlib
import io
import json
import logging
import os
import re
import runpy
import shutil
import sys
import tempfile
from datetime import date
from types import SimpleNamespace

for _s in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _s.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="saley-notes-scope-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")
os.environ["STATE_DIR"] = os.path.join(TMP, "state0")
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import config  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"))
import offline_guard  # noqa: E402

GUARD = offline_guard.install_script()      # NFT2-1065: canned source statuses; a real Sheets/Drive call is blocked and fails the run

import evidence  # noqa: E402
import meetings  # noqa: E402
import nextaction  # noqa: E402
import notes  # noqa: E402
import prep  # noqa: E402
import sources  # noqa: E402
import todos  # noqa: E402
import toolsets  # noqa: E402
import drive  # noqa: E402
from bot import SalesBot  # noqa: E402
from db import DB  # noqa: E402

failures = 0


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


# -- a log tap ----------------------------------------------------------------

class Tap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelno, record.name, record.getMessage()))

    def clear(self):
        self.records.clear()

    def at(self, level, needle=""):
        return [m for lv, _n, m in self.records if lv == level and needle in m]


TAP = Tap()
logging.getLogger().addHandler(TAP)
logging.getLogger().setLevel(logging.DEBUG)

# -- fixtures -----------------------------------------------------------------

FOLDER = "Saley – Sales Notes"          # EN DASH, as the human named it
AM_NAME = "NFThing Kick-off ( AM Sync) – 2026／10／05 10：30 IST – Notes by Gemini.txt"
PM_NAME = "NFThing Wrap-up ( PM Sync) – 2026／10／05 18：30 IST – Notes by Gemini.txt"
PMCALL_NAME = "PM Call – 2026／08／19 11：20 IST – Notes by Gemini.txt"
ACME_NAME = "Acme discovery call – 2026／10／05 15：00 IST – Notes by Gemini.txt"
SALESSYNC_NAME = "Sales sync – 2026／10／06 12：00 IST – Notes by Gemini.txt"
TEAMSYNC_NAME = "Team sync – 2026／10／06 13：00 IST – Notes by Gemini.txt"

D4_NOT_CONNECTED = "Meeting notes aren't connected to me yet."
D4_EMPTY = "There are no sales meeting notes in the Saley – Sales Notes folder yet."
D4_UNREACHABLE = "I can't reach the sales notes folder right now, so I haven't checked the notes."

SYNC_ERR = "FAKE-DRIVE-ERROR-XYZ directory not found"

SYNC_SCRIPT = os.path.join(TMP, "fake_sync.py")
RAN_LOG = os.path.join(TMP, "sync_ran.log")
with open(SYNC_SCRIPT, "w", encoding="utf-8") as f:
    f.write(
        "import os, shutil, sys\n"
        "src, dst = sys.argv[1], sys.argv[2]\n"
        "open(os.environ['SYNC_RAN_LOG'], 'a').write('ran\\n')\n"
        "if '--fail' in sys.argv or not os.path.isdir(src):\n"
        f"    sys.stderr.write({SYNC_ERR!r})\n"
        "    sys.exit(1)\n"
        "os.makedirs(dst, exist_ok=True)\n"
        "for n in os.listdir(src):\n"
        "    p = os.path.join(src, n)\n"
        "    if os.path.isfile(p):\n"
        "        shutil.copy2(p, os.path.join(dst, n))\n"
    )
os.environ["SYNC_RAN_LOG"] = RAN_LOG


def sync_runs() -> int:
    try:
        return len(open(RAN_LOG, encoding="utf-8").read().split())
    except OSError:
        return 0


def body(token, extra=""):
    return (f"Summary\n{token} the team talked through pricing and next steps. {extra}\n\n"
            f"Decisions\n* {token} agreed to send the deck\n\n"
            f"Next steps\n* [Asha] {token} send the deck by Friday\n")


class World:
    """A temp 'Drive folder', a NOTES_DIR and a STATE_DIR, wired into config."""

    def __init__(self, tag, folder=FOLDER):
        root = tempfile.mkdtemp(prefix=tag + "-", dir=TMP)
        self.root = root
        self.drive = os.path.join(root, "drive")
        self.notes = os.path.join(root, "notes")
        self.state = os.path.join(root, "state")
        self.internal = os.path.join(root, "internal")
        for d in (self.drive, self.notes, self.state, self.internal):
            os.makedirs(d)
        self.folder = folder
        self.apply()

    def cmd(self, fail=False, folder=None):
        return (f'"{sys.executable}" "{SYNC_SCRIPT}" "{self.drive}" "{self.notes}" '
                f'"{folder if folder is not None else self.folder}"' + (" --fail" if fail else ""))

    def apply(self, *, source=None, cmd=None, patterns=None, tags=None):
        config.NOTES_DIR = self.notes
        config.STATE_DIR = self.state
        os.environ["STATE_DIR"] = self.state
        config.NOTES_SOURCE_FOLDER = self.folder if source is None else source
        config.NOTES_SYNC_CMD = self.cmd() if cmd is None else cmd
        config.NOTES_EXCLUDE_TITLE_PATTERNS = (
            ["AM sync", "PM sync", "NFThing Kick-off", "NFThing Wrap-up", "standup",
             "stand-up", "daily sync"] if patterns is None else patterns)
        config.NOTES_REQUIRE_TITLE_TAGS = [] if tags is None else tags
        config.NOTES_SYNC_MINUTES = 30
        config.NOTES_SYNC_TIMEOUT_SECONDS = 60
        reset_notes()

    def put(self, where, name, text):
        d = {"drive": self.drive, "notes": self.notes, "internal": self.internal}[where]
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)

    def sync(self, fail=False, reason="verify"):
        config.NOTES_SYNC_CMD = self.cmd(fail=fail)
        return notes.sync_now(reason=reason)

    def top(self):
        return sorted(n for n in os.listdir(self.notes))

    def quarantine_files(self):
        out = []
        for root, _dirs, files in os.walk(os.path.join(self.notes, "_quarantine")):
            out += [os.path.join(root, f) for f in files]
        return sorted(out)


def reset_notes():
    """Forget everything the notes module remembers between worlds."""
    for n in dir(notes):
        if n.startswith("__"):
            continue
        v = getattr(notes, n)
        if isinstance(v, (dict, set, list)) and (
                "CACHE" in n.upper() or n.startswith("_LAST") or "LOGGED" in n.upper()
                or "SEEN" in n.upper()):
            v.clear()
        elif v is None and (n.startswith("_LAST") or "LOGGED" in n.upper()):
            pass
        elif isinstance(v, str) and (n.startswith("_LAST") or "LOGGED" in n.upper()):
            setattr(notes, n, None)
    if hasattr(notes, "_LAST_LOGGED_FAILURE"):
        notes._LAST_LOGGED_FAILURE = None
    notes._SYNC.update(last_attempt=None, last_success=None, ok=None, error=None,
                       remedy=None, runs=0, quarantined=0)
    notes._STATS.update(scanned_at=None, docs_seen=0, notes_seen=0, loaded_docs=0,
                        excluded_docs=0)
    for n in dir(todos):
        v = getattr(todos, n)
        if n.startswith("_LAST") or "HIDDEN_SEEN" in n.upper() or n == "_HIDDEN_LOGGED":
            if isinstance(v, (dict, set, list)):
                v.clear()
            else:
                setattr(todos, n, None)
    if isinstance(getattr(meetings, "_cache", None), dict):
        meetings._cache["key"] = None
        meetings._cache["facts"] = []
    with contextlib.suppress(OSError):
        os.remove(RAN_LOG)
    TAP.clear()


# -- the bot, its tools ------------------------------------------------------

bot = SalesBot()
bot.db = DB(config.DB_PATH)


async def _no_companies():
    return []


bot._tracker_company_names = _no_companies


def tools_of(factory, *a):
    return {t["schema"]["name"]: t["handler"] for t in factory(*a) if t.get("handler")}


def call(handler, inp=None):
    return asyncio.run(handler(inp or {}))


def note_tool_results(question="what did we decide in the last meeting?"):
    t = tools_of(bot._notes_tools, question)
    t.update({k: v for k, v in tools_of(bot._todo_tools).items() if k == "todo_candidates"})
    return {n: call(t[n]) for n in ("list_meeting_notes", "read_meeting_note",
                                    "meeting_facts", "todo_candidates") if n in t}


def stable(obj, w):
    """A tool result as text with the per-run bits (temp paths, timestamps) masked, so two
    runs can be compared for sameness."""
    text = json.dumps(obj, sort_keys=True, default=str)
    text = text.replace(json.dumps(w.root)[1:-1], "<ROOT>").replace(w.root, "<ROOT>")
    return re.sub(r"\d{4}-\d\d-\d\dT[\d:.+-]+", "<TS>", text)


def titles(**kw):
    return [m["title"] for m in notes.list_notes(days=3650, **kw)]


def loaded_names(w):
    return sorted(os.path.basename(m["path"]) for m in notes.list_notes(days=3650))


# =============================================================================
print("E1  a doc in the sales folder AND somewhere internal")
w = World("e1")
w.put("drive", ACME_NAME, body("ZZACME"))
w.put("internal", ACME_NAME, body("ZZACME"))
w.put("internal", "Internal roadmap review – 2026／10／04 09：00 IST.txt", body("ZZINTERNAL"))
check("sync ran ok", w.sync(), True)
check("the doc in both places is loaded", loaded_names(w), [ACME_NAME])
check("the internal-only doc is never read", "ZZINTERNAL" in json.dumps(
    [notes.read_note(date=d) for d in ("2026-10-04", "2026-10-05")], default=str), False)
check("the internal dir was never synced (still holds both docs)", len(os.listdir(w.internal)), 2)

# =============================================================================
print("\nE2  a sales doc that quotes sync content")
w = World("e2")
w.put("drive", ACME_NAME, body("ZZACME", "As discussed in the AM sync and the PM sync on Monday, ZZQUOTED."))
w.sync()
check("loaded (the filter reads the TITLE, never the body)", loaded_names(w), [ACME_NAME])
rn = notes.read_note(date="2026-10-05") or {}
check("read_note returns it, quote included", "ZZQUOTED" in (rn.get("raw") or ""), True)

# =============================================================================
STANDUP_NAMES = [
    AM_NAME, PM_NAME,
    "AM-Sync 2026-10-05.txt", "am  sync 2026-10-05.txt", "AM_SYNC 2026-10-05.txt",
    "AM Standup 2026-10-05.txt", "Daily Stand-up 2026-10-05.txt",
    "daily stand up 2026-10-05.txt", "Daily sync 2026-10-05.txt",
]


def e3_run():
    out = {}
    for label, pats in (("default", None), ("empty", []), ("pinned", ["AM sync", "PM sync"])):
        w = World("e3" + label)
        w.apply(patterns=pats)
        for n in STANDUP_NAMES:
            w.put("drive", n, body("ZZSTANDUP"))
        w.put("drive", ACME_NAME, body("ZZACME"))
        w.sync()
        every = notes.list_notes(days=3650, include_excluded=True)
        by_title = {os.path.basename(m["path"]): m for m in every}
        out[label] = {
            "loaded": loaded_names(w),
            "buckets": {n: by_title.get(n, {}).get("bucket") for n in STANDUP_NAMES},
            "tool": stable(note_tool_results(), w),
        }
    return out


print("E3  standup docs INSIDE the sales folder, every title shape (and the floor)")
config.SALES_TEST_MODE = False
r = e3_run()
config.SALES_TEST_MODE = True
r_t = e3_run()
config.SALES_TEST_MODE = False
for label, res in r.items():
    check(f"[{label}] only the sales note loads", res["loaded"], [ACME_NAME])
    check(f"[{label}] every standup title is bucketed excluded_standup",
          sorted(set(res["buckets"].values())), ["excluded_standup"])
    check(f"[{label}] no standup body in any tool result", "ZZSTANDUP" in res["tool"], False)
check("PAR  E3 identical with SALES_TEST_MODE on and off", r_t, r)

print("\nE3b a bare 'sync' is NOT refused (D2)")
w = World("e3b")
for n, t in ((SALESSYNC_NAME, "ZZSALESSYNC"), (TEAMSYNC_NAME, "ZZTEAMSYNC"), (PMCALL_NAME, "ZZPMCALL")):
    w.put("drive", n, body(t))
w.sync()
check("Sales sync, Team sync and PM Call (in the folder) all load",
      loaded_names(w), sorted([SALESSYNC_NAME, TEAMSYNC_NAME, PMCALL_NAME]))

# =============================================================================
def e4a_run():
    w = World("e4a")
    real_shape = f'rclone copy "gdrive:Sales Meeting Notes" {w.notes}'
    w.apply(source="", cmd=real_shape)
    w.put("notes", PMCALL_NAME, body("ZZPMCALL"))
    w.put("notes", AM_NAME, body("ZZAMSYNC"))
    before = w.top()
    res = {
        "state": notes.source_state(),
        "list": notes.list_notes(days=3650),
        "read": notes.read_note(),
        "say": notes.nothing_to_say(),
        "configured": notes.is_configured(),
        "sync_now": notes.sync_now(),
        "for_question": notes.sync_for_question("what happened today?"),
        "runs": notes._SYNC["runs"],
        "moved": w.top() != before,
        "quarantine_made": os.path.isdir(os.path.join(w.notes, "_quarantine")),
        "tools": note_tool_results(),
        "tools_text": stable(note_tool_results(), w),
        "status": sources.SALES_MEETING_NOTES.status(),
    }
    return res


print("\nE4a not configured (NOTES_SOURCE_FOLDER blank, the REAL current command shape, stale files on disk)")
a_live = e4a_run()
config.SALES_TEST_MODE = True
a_test = e4a_run()
config.SALES_TEST_MODE = False
a = a_live
check("state", a["state"], "not_configured")
check("list_notes() is empty", a["list"], [])
check("read_note() is None", a["read"], None)
check("is_configured() is False", a["configured"], False)
check("nothing_to_say() is the D4 sentence", a["say"], D4_NOT_CONNECTED)
check("sync_now() does not run", a["sync_now"], False)
check("sync_for_question() does not run a sync", (a["for_question"].get("ran"), a["runs"]), (False, 0))
check("no file was moved and no _quarantine was made", (a["moved"], a["quarantine_made"]), (False, False))
check("the four tools returned", sorted(a["tools"]), ["list_meeting_notes", "meeting_facts",
                                                    "read_meeting_note", "todo_candidates"])
for n, res in a["tools"].items():
    check(f"{n}: say == D4 not-connected, enabled/configured False",
          (res.get("say"), res.get("enabled"), res.get("configured"), res.get("sales_notes_on_file")),
          (D4_NOT_CONNECTED, False, False, 0))
    check(f"{n}: no sync content in the result", "ZZ" in json.dumps(res, default=str), False)
check("sources status is AWAITING_ACCESS", a["status"].get("status"), sources.AWAITING_ACCESS)
check("PAR  E4a identical with SALES_TEST_MODE on and off (tool results, state, status)",
      {k: v for k, v in a_test.items() if k not in ("status", "tools", "for_question")}
      | {"s": a_test["status"].get("status")},
      {k: v for k, v in a_live.items() if k not in ("status", "tools", "for_question")}
      | {"s": a_live["status"].get("status")})

# =============================================================================
print("\nE4b folder empty")
w = World("e4b")
check("sync ok on an empty folder", w.sync(), True)
check("state empty", notes.source_state(), "empty")
check("say is the D4 empty sentence", notes.nothing_to_say(), D4_EMPTY)
check("list_notes is empty", notes.list_notes(days=3650), [])
res = note_tool_results()
check("all four tools say it", {n: r.get("say") for n, r in res.items()},
      {n: D4_EMPTY for n in ("list_meeting_notes", "read_meeting_note", "meeting_facts", "todo_candidates")})
check("enabled/configured stay True (it IS connected)",
      {(r.get("enabled"), r.get("configured")) for r in res.values()}, {(True, True)})

print("\nE4b2 the folder holds only standups")
w = World("e4b2")
w.put("drive", AM_NAME, body("ZZAMSYNC"))
w.put("drive", PM_NAME, body("ZZPMSYNC"))
w.sync()
check("loaded 0, the empty sentence", (notes.list_notes(days=3650), notes.nothing_to_say()), ([], D4_EMPTY))
w2 = [m for _lv, m in [(lv, m) for lv, _n, m in TAP.records if lv >= logging.WARNING]]
check("exactly one non-quarantine WARNING for 'everything was held back'",
      len([m for m in w2 if "moved" not in m and "notes" in m.lower()]), 1)
notes.list_notes(days=3650)
notes.sync_status()
check("...and it does not repeat on later reads",
      len([m for lv, _n, m in TAP.records if lv >= logging.WARNING and "moved" not in m
           and "notes" in m.lower()]), 1)

print("\nE4c folder unreachable")
w = World("e4c")
w.put("notes", PMCALL_NAME, body("ZZPMCALL"))
w.put("notes", AM_NAME, body("ZZAMSYNC"))
check("sync fails on the first run", w.sync(fail=True), False)
check("state unreachable", notes.source_state(), "unreachable")
check("say is the D4 unreachable sentence", notes.nothing_to_say(), D4_UNREACHABLE)
check("list_notes() is empty (the stale files are not a fallback)", notes.list_notes(days=3650), [])
st = sources.SALES_MEETING_NOTES.status()
check("status DEGRADED", st.get("status"), sources.DEGRADED)
check("the sync error is in the status DETAIL", "FAKE-DRIVE-ERROR-XYZ" in (st.get("detail") or ""), True)
check("the stale files were quarantined, not read",
      (sorted(os.listdir(w.notes)), len(w.quarantine_files())), (["_quarantine"], 2))
unreach = note_tool_results()
check("all four tools say it", {r.get("say") for r in unreach.values()}, {D4_UNREACHABLE})

print("\nE4d misconfigured")
w = World("e4d")
w.put("notes", PMCALL_NAME, body("ZZPMCALL"))
mis = {}
w.apply(cmd=f'rclone copy "gdrive:Some Other Folder" {w.notes}')
mis["folder not in command"] = (notes.source_state(), notes.sync_now(), notes.nothing_to_say())
w.apply(cmd=f'rclone sync "gdrive:{FOLDER}" {w.notes}')
mis["mirror without _quarantine exclude"] = (notes.source_state(), notes.sync_now(), notes.nothing_to_say())
for k, v in mis.items():
    check(f"{k}: misconfigured, sync not run, not-connected sentence", v,
          ("misconfigured", False, D4_NOT_CONNECTED))
check("nothing moved, no sync ran, no _quarantine", (w.top(), sync_runs(), notes._SYNC["runs"]),
      ([PMCALL_NAME], 0, 0))
w.apply(cmd=f'rclone sync "gdrive:{FOLDER}" {w.notes} --exclude "/_quarantine/**"')
check("a mirror WITH the _quarantine exclude is accepted (state not misconfigured)",
      notes.source_state() not in ("misconfigured", "not_configured"), True)
w.apply(cmd=f'rclone copy "gdrive:{FOLDER}" {w.notes}')
check("a copy command is accepted and reported as 'copy'",
      (notes.source_state() != "misconfigured", notes.sync_status().get("sync_mode")), (True, "copy"))
w.apply(cmd=f'rclone sync "gdrive:{FOLDER}" {w.notes} --exclude "/_quarantine/**"')
check("a mirror is reported as 'mirror'", notes.sync_status().get("sync_mode"), "mirror")

# E4e: the whole result of every nothing-to-say shape is clean
print("\nE4e the sentences are exact and clean")
dumps = {"not_configured": a["tools"], "empty": res, "unreachable": unreach}
w_mis = World("e4e")
w_mis.put("notes", PMCALL_NAME, body("ZZPMCALL"))
w_mis.apply(cmd=f'rclone copy "gdrive:Some Other Folder" {w_mis.notes}')
dumps["misconfigured"] = note_tool_results()
want = {"not_configured": D4_NOT_CONNECTED, "misconfigured": D4_NOT_CONNECTED,
        "empty": D4_EMPTY, "unreachable": D4_UNREACHABLE}
for situation, tools in dumps.items():
    for tname, r in tools.items():
        text = json.dumps(r, ensure_ascii=False, default=str)
        bad = [s for s in (SYNC_ERR, "FAKE-DRIVE-ERROR", "rclone", "NOTES_", ".env", "summary.json",
                           "quarantine", "Quarantine", ".txt", "Gemini", "gdrive") if s in text]
        check(f"{situation}/{tname}: say is exact", r.get("say"), want[situation])
        check(f"{situation}/{tname}: none of error text, rclone, variable names, files, quarantine",
              bad, [])
check("straight apostrophe, byte for byte (not-connected)",
      want["not_configured"].encode("utf-8"), b"Meeting notes aren't connected to me yet.")
check("the sentence constants on the module are the D4 text",
      (notes.SAY_NOT_CONNECTED, notes.SAY_UNREACHABLE, notes.SAY_EMPTY.format(folder=FOLDER)),
      (D4_NOT_CONNECTED, D4_UNREACHABLE, D4_EMPTY))

# =============================================================================
print("\nE5  stale files from the old sync are quarantined, never read, never deleted")
w = World("e5")
w.put("notes", PMCALL_NAME, body("ZZPMCALL"))
w.put("notes", AM_NAME, body("ZZAMSYNC"))
w.put("drive", ACME_NAME, body("ZZACME"))
stale_bytes = {n: open(os.path.join(w.notes, n), "rb").read() for n in (PMCALL_NAME, AM_NAME)}
check("before the first sync nothing is readable (no manifest)", notes.list_notes(days=3650), [])
TAP.clear()
w.sync()
q = w.quarantine_files()
check("both stale files are under _quarantine/<stamp>/, byte-identical",
      sorted((os.path.basename(p), open(p, "rb").read()) for p in q),
      sorted(stale_bytes.items()))
check("...in a single timestamp folder", len({os.path.dirname(p) for p in q}), 1)
check("only the sales note is loaded", loaded_names(w), [ACME_NAME])
check("stale files are not in list_notes(include_excluded=True)",
      sorted(os.path.basename(m["path"]) for m in notes.list_notes(days=3650, include_excluded=True)),
      [ACME_NAME])
moved_warn = TAP.at(logging.WARNING, "moved")
check("exactly one 'moved N file(s)' WARNING", len(moved_warn), 1)
check("...says 2 files and that nothing was deleted",
      ("2 file" in moved_warn[0], "nothing was deleted" in moved_warn[0]) if moved_warn else None,
      (True, True))
TAP.clear()
w.sync()
check("a second sync moves nothing and logs no new 'moved' warning",
      (len(w.quarantine_files()), len(TAP.at(logging.WARNING, "moved"))), (2, 0))

print("\nE5b changing the folder quarantines the old notes (D6)")
w = World("e5b", folder="Folder A")
w.put("drive", ACME_NAME, body("ZZACME"))
w.sync()
check("folder A: the note loads", loaded_names(w), [ACME_NAME])
drive_b = os.path.join(os.path.dirname(w.drive), "driveB")
os.makedirs(drive_b)
with open(os.path.join(drive_b, SALESSYNC_NAME), "w", encoding="utf-8") as f:
    f.write(body("ZZSALESSYNC"))
w.folder = "Folder B"
w.drive = drive_b
w.apply()
check("the moment the source changes, A's notes are unreadable (before any sync)",
      notes.list_notes(days=3650), [])
TAP.clear()
w.sync()
check("after the sync A's note is quarantined and B's note is loaded",
      (loaded_names(w), len(w.quarantine_files()), os.path.exists(os.path.join(w.notes, ACME_NAME))),
      ([SALESSYNC_NAME], 1, False))
check("one 'moved' WARNING", len(TAP.at(logging.WARNING, "moved")), 1)

print("\nE5c a file dropped into NOTES_DIR by hand")
w = World("e5c")
w.put("drive", ACME_NAME, body("ZZACME"))
w.sync()
w.put("notes", "Handmade call – 2026／10／04 10：00 IST.txt", body("ZZHAND"))
check("not loaded", loaded_names(w), [ACME_NAME])
w.sync()
check("quarantined at the next sync", (len(w.quarantine_files()), loaded_names(w)), (1, [ACME_NAME]))

print("\nE5d the quarantine is never scanned, at any depth (D6)")
w = World("e5d")
w.put("drive", ACME_NAME, body("ZZACME"))
w.sync()
qn = "Acme sales call – 2026／10／01 10：00 IST.txt"
planted = [os.path.join(w.notes, "_quarantine", p) for p in
           (qn, os.path.join("20261007-101010", qn), os.path.join("a", "b", "c", qn))]
for p in planted:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body("ZZQUAR"))
w.apply()
w.sync()
every = notes.list_notes(days=3650, include_excluded=True)
check("list_notes(include_excluded=True) sees only the sales note",
      [os.path.basename(m["path"]) for m in every], [ACME_NAME])
check("read_note on the quarantined date finds nothing", notes.read_note(date="2026-10-01"), None)
check("freshness reports only the allowed note", notes.freshness()[0], "2026-10-05")
check("labels_on the quarantined date is empty", notes.labels_on("2026-10-01"), [])
check("sync_status docs_seen counts only top-level files", notes.sync_status()["docs_seen"], 1)
mf = os.path.join(w.state, "notes_manifest.json")
man = json.load(open(mf, encoding="utf-8")) if os.path.exists(mf) else {}
check("the manifest lists only the sales note", man.get("files"), [ACME_NAME])
check("the manifest names the source and the absolute notes dir",
      (man.get("source"), man.get("notes_dir") == os.path.abspath(w.notes) or
       os.path.normcase(man.get("notes_dir", "")) == os.path.normcase(os.path.abspath(w.notes))),
      (FOLDER, True))
n0 = len(w.quarantine_files())
w.sync()
w.sync()
check("three syncs: nothing under _quarantine deleted or changed",
      (len(w.quarantine_files()), all(os.path.exists(p) for p in planted)), (n0, True))

print("\nE5e a stray that cannot be moved to the quarantine stays UNREAD")
STUCK = "Stuck stray call – 2026／10／03 10：00 IST.txt"
OTHER = "Other stray call – 2026／10／02 10：00 IST.txt"
w = World("e5e")
w.put("drive", ACME_NAME, body("ZZACME"))
w.put("notes", STUCK, body("ZZSTUCK"))
w.put("notes", OTHER, body("ZZOTHER"))
stuck_bytes = open(os.path.join(w.notes, STUCK), "rb").read()
real_move = notes.shutil.move


def picky_move(src, dst, *a, **k):
    if os.path.basename(src) == STUCK:
        raise PermissionError("locked")
    return real_move(src, dst, *a, **k)


notes.shutil.move = picky_move
try:
    w.sync()
finally:
    notes.shutil.move = real_move
man = json.load(open(os.path.join(w.state, "notes_manifest.json"), encoding="utf-8"))
check("the stuck file is still at the top level, byte-identical",
      open(os.path.join(w.notes, STUCK), "rb").read(), stuck_bytes)
check("it is not in the manifest", STUCK in man["files"], False)
meta = [m for m in notes.list_notes(days=3650, include_excluded=True) if os.path.basename(m["path"]) == STUCK]
check("it is counted only: loaded False, bucket not_from_folder (metadata, never content)",
      [(m["loaded"], m.get("bucket")) for m in meta], [(False, "not_from_folder")])
check("it is not in list_notes() and read_note never returns it",
      (STUCK in [os.path.basename(m["path"]) for m in notes.list_notes(days=3650)],
       "ZZSTUCK" in json.dumps(notes.read_note(date="2026-10-03"), default=str)), (False, False))
check("only the sales note loads", loaded_names(w), [ACME_NAME])
check("the other stray was quarantined as usual, nothing deleted",
      ([os.path.basename(p) for p in w.quarantine_files()], os.path.exists(os.path.join(w.notes, OTHER))),
      ([OTHER], False))
w = World("e5e2")
w.put("drive", ACME_NAME, body("ZZACME"))
w.put("notes", STUCK, body("ZZSTUCK"))
w.put("notes", OTHER, body("ZZOTHER"))
real_makedirs = notes.os.makedirs


def no_quarantine(path, *a, **k):
    if "_quarantine" in str(path):
        raise OSError("read-only")
    return real_makedirs(path, *a, **k)


notes.os.makedirs = no_quarantine
try:
    w.sync()
finally:
    notes.os.makedirs = real_makedirs
man = json.load(open(os.path.join(w.state, "notes_manifest.json"), encoding="utf-8"))
check("quarantine folder not creatable: neither stray moves, neither is in the manifest",
      (sorted(n for n in w.top() if n != "_quarantine"), sorted(set(man["files"]) & {STUCK, OTHER})),
      (sorted([ACME_NAME, STUCK, OTHER]), []))
check("...and neither is readable", loaded_names(w), [ACME_NAME])

# =============================================================================
print("\nE7  title tags (D3)")
check("NOTES_REQUIRE_TITLE_TAGS defaults to empty", getattr(config, "NOTES_REQUIRE_TITLE_TAGS", None), [])
w = World("e7a")
for i, n in enumerate((ACME_NAME, SALESSYNC_NAME, TEAMSYNC_NAME)):
    w.put("drive", n, body(f"ZZT{i}"))
w.sync()
check("untagged notes load with no tag required", len(loaded_names(w)), 3)
w = World("e7b")
w.apply(tags=["[SalesNotes]"])  # not "[Sales]": tags match on normalised tokens, so that would match "Sales sync"
for i, n in enumerate((ACME_NAME, SALESSYNC_NAME, TEAMSYNC_NAME)):
    w.put("drive", n, body(f"ZZT{i}"))
TAP.clear()
w.sync()
check("a required tag denies the 3 untagged docs", (notes.list_notes(days=3650),
                                                    notes.sync_status().get("docs_missing_tag")), ([], 3))
check("...and the empty sentence is what the tools say", notes.nothing_to_say(), D4_EMPTY)
notes.list_notes(days=3650)
check("the all-held-back WARNING fires once", len([m for lv, _n, m in TAP.records
                                                   if lv >= logging.WARNING and "moved" not in m
                                                   and "notes" in m.lower()]), 1)
w.put("drive", "[SalesNotes] Globex call – 2026／10／06 10：00 IST.txt", body("ZZTAGGED"))
w.sync()
check("a tagged title loads", [os.path.basename(m["path"]) for m in notes.list_notes(days=3650)],
      ["[SalesNotes] Globex call – 2026／10／06 10：00 IST.txt"])

# =============================================================================
print("\nE8  counts are in the log and the status (never silently '0 of 72')")
w = World("e8")
w.put("drive", ACME_NAME, body("ZZACME"))
w.put("drive", AM_NAME, body("ZZAMSYNC"))
w.put("drive", "undated sales doc.txt", "Summary\nno date anywhere\n")
w.put("notes", PMCALL_NAME, body("ZZPMCALL"))
TAP.clear()
w.sync()
notes.list_notes(days=3650)
line = [m for lv, _n, m in TAP.records if lv == logging.INFO and "[notes] source=" in m]
check("the per-sync INFO line exists", bool(line), True)
print("      line:", line[-1] if line else None)
check("...with source, state and the buckets",
      all(k in (line[-1] if line else "") for k in
          ("source=", "state=ok", "on_disk=", "from_folder=", "loaded=1", "standup=1", "undated=1",
           "quarantined=1")), True)
ss = notes.sync_status()
check("sync_status has the new keys", all(k in ss for k in (
    "state", "source_folder", "docs_from_folder", "docs_not_from_folder", "docs_missing_tag",
    "docs_undated", "require_tags", "quarantine_dir", "sync_mode")), True)
check("sync_status keeps every old key", all(k in ss for k in (
    "dir", "dir_exists", "cmd_configured", "interval_minutes", "last_attempt", "last_success", "ok",
    "degraded", "error", "remedy", "docs_seen", "notes_seen", "docs_loaded", "docs_excluded",
    "scanned_at", "exclude_patterns")), True)
check("buckets are consistent: from_folder + not_from_folder == docs_seen",
      ss["docs_from_folder"] + ss["docs_not_from_folder"], ss["docs_seen"])
check("loaded + missing_tag + undated <= from_folder (the rest are standups)",
      ss["docs_loaded"] + ss["docs_missing_tag"] + ss["docs_undated"] <= ss["docs_from_folder"], True)
check("exclude_patterns reports the EFFECTIVE list (the standup floor included)",
      {notes._norm_title(p) for p in ("am sync", "pm sync", "nfthing kick-off", "nfthing wrap-up", "standup",
                                      "stand-up", "daily sync")}
      <= {notes._norm_title(p) for p in ss["exclude_patterns"]}, True)
detail = sources.SALES_MEETING_NOTES.status().get("detail") or ""
check("the status detail names the folder", FOLDER in detail, True)

# =============================================================================
print("\nE9  routing (D5)")
ROUTES = [
    ("what do we need to do today?", ["today"]),
    ("what do I need to do today", ["today"]),
    ("what should we do today", ["today"]),
    ("what's on today", ["today"]),
    ("whats on today", ["today"]),
    ("what do we need to do today about the Acme meeting?", ["today"]),
    ("what are the sales objectives for today?", []),
    ("what's the latest AI news today?", ["web", "news"]),
    ("show the to-dos", ["todos"]),
    ("what is on the to-do list", ["todos"]),
    ("what do I owe", ["todos"]),
    ("any action items?", ["todos"]),
    ("what came out of the call with Acme", ["notes"]),
    ("is Acme on hold", ["notes"]),
    ("sync the sheet", ["sheet"]),
    ("Sure.", []),
]


def routes():
    out = {q: toolsets.route(q) for q, _w in ROUTES}
    out["what do we need to do about Acme?"] = toolsets.route("what do we need to do about Acme?")
    out["what did we decide in yesterday's meeting?"] = toolsets.route("what did we decide in yesterday's meeting?")
    out["action items from the Acme call"] = toolsets.route("action items from the Acme call")
    return out


rt = {}
for mode in (False, True):
    config.SALES_TEST_MODE = mode
    rt[mode] = routes()
config.SALES_TEST_MODE = False
for q, want_groups in ROUTES:
    check(f"route({q!r})", rt[False][q], want_groups)
g = rt[False]["what do we need to do about Acme?"]
check("'what do we need to do about Acme?' has no notes and no today", ("notes" in g, "today" in g), (False, False))
check("'what did we decide in yesterday's meeting?' has notes",
      "notes" in rt[False]["what did we decide in yesterday's meeting?"], True)
g = rt[False]["action items from the Acme call"]
check("'action items from the Acme call' has notes and no todos", ("notes" in g, "todos" in g), (True, False))
check("PAR  E9 routes identical with SALES_TEST_MODE on and off", rt[True], rt[False])
check("the groups: today == (show_todos,), notes has no todo tool",
      (toolsets.GROUPS.get("today"), "show_todos" in toolsets.GROUPS.get("notes", ()),
       "todo_candidates" in toolsets.GROUPS.get("notes", ()), toolsets.GROUPS.get("todos")),
      (("show_todos",), False, False, ("show_todos", "todo_candidates")))
all_names = sorted({n for names in toolsets.GROUPS.values() for n in names} | {"cadence_preview", "mystery"})
fake_tools = [{"schema": {"name": n, "description": "d"}, "handler": None} for n in all_names]
for mode in (False, True):
    config.SALES_TEST_MODE = mode
    picked, groups, _why = toolsets.select(fake_tools, "what do we need to do today?")
    check(f"select('what do we need to do today?') offers exactly show_todos [test_mode={mode}]",
          [t["schema"]["name"] for t in picked], ["show_todos"])
config.SALES_TEST_MODE = False
check("today is EXCLUSIVE even when other groups also match",
      toolsets.route("what do we need to do today about the Acme meeting and the sheet?"), ["today"])

# =============================================================================
print("\nE6  to-do rows (D1)")
HEAD = list(todos.HEADERS)


def sheet_values(rows):
    """rows: [(n, task, owner, source, raised, status)] -> the A:H values."""
    return [HEAD] + [[n, t, o, s, r, "", st, ""] for n, t, o, s, r, st in rows]


WRITES = []


def _boom(name):
    def f(*a, **k):
        WRITES.append(name)
        raise AssertionError(f"drive.{name} must not be called")
    return f


for _n in ("sheet_update", "sheet_append", "create_spreadsheet", "share", "format_header"):
    if hasattr(drive, _n):
        setattr(drive, _n, _boom(_n))
drive.sheet_url = lambda sid: f"https://sheets.example/{sid}"

w = World("e6")
w.put("drive", ACME_NAME, body("ZZACME"))
w.sync()
cit = meetings.citation(notes.read_note(date="2026-10-05"))
check("the citation of the sales note is 'Acme discovery call, 5 Oct'", cit, "Acme discovery call, 5 Oct")
AM_CIT = "NFThing Kick-off ( AM Sync), 7 Jul"
ROWS = [
    ("1", "ZZ-ALLOWED-TASK", "Asha", cit, "2026-10-05", "Open"),
    ("2", "ZZ-STANDUP-TASK", "Ben", AM_CIT, "2026-07-07", "Open"),
    ("3", "ZZ-PMCALL-TASK", "Chen", "PM Call, 19 Aug", "2026-08-19", "Open"),
    ("4", "ZZ-BLANK-TASK", "Dev", "", "2026-10-06", "Open"),
    ("5", "ZZ-WRONGDATE-TASK", "Eli", cit, "2026-09-01", "Open"),
    ("6", "ZZ-CLOSED-STANDUP-TASK", "Fay", AM_CIT, "2026-07-07", "Done"),
]
config.TODO_SHEET_ID = "FAKE-TODO-SHEET"
config.TODO_SHEET_ENABLED = True
drive.sheet_values = lambda sid, a1: sheet_values(ROWS)
read = todos.read_rows(bot.db)
shown, hidden = todos.split_visible(read["rows"])
check("split_visible: only the allowed-source row is shown", [r["task"] for r in shown], ["ZZ-ALLOWED-TASK"])
reasons = {r["task"]: r["reason"] for r in hidden}
check("standup source -> reason 'standup'", reasons.get("ZZ-STANDUP-TASK"), "standup")
check("PM Call source (not an allowed note) -> 'source_not_an_allowed_note'",
      reasons.get("ZZ-PMCALL-TASK"), "source_not_an_allowed_note")
check("allowed citation but wrong Date raised -> 'source_not_an_allowed_note'",
      reasons.get("ZZ-WRONGDATE-TASK"), "source_not_an_allowed_note")
check("D7 (settled by the human; was Q1) a BLANK Source meeting is HIDDEN, reason 'no_source_meeting'",
      reasons.get("ZZ-BLANK-TASK"), "no_source_meeting")
check("hidden rows are the row dicts plus a reason (the sheet row number is kept)",
      sorted(r["row"] for r in hidden), [3, 4, 5, 6, 7])
oi = todos.open_items(bot.db)
check("open_items shows only the allowed row", [r["task"] for r in oi["rows"]], ["ZZ-ALLOWED-TASK"])
check("open_total == 1 and shown == 1 (visible open rows only)", (oi["open_total"], oi["shown"]), (1, 1))
check("the result has no key containing 'hidden'", [k for k in oi if "hidden" in k.lower()], [])
check("read_rows stays unfiltered (dedup must see every row)", len(read["rows"]), 6)

print("\nE6b hidden rows are never mentioned")
show = tools_of(bot._todo_tools)["show_todos"]
r1 = call(show)
text = json.dumps(r1, ensure_ascii=False, default=str).lower()
check("show_todos: the allowed task is there", "zz-allowed-task" in text, True)
check("show_todos: no hidden task text", [t for t in ("zz-standup", "zz-pmcall", "zz-blank", "zz-wrongdate",
                                                     "zz-closed") if t in text], [])
check("show_todos: no 'hidden' / 'not shown' / 'some rows' wording",
      [t for t in ("hidden", "not shown", "some rows", "withheld", "filtered") if t in text], [])
check("show_todos: open_total counts visible rows only", r1.get("open_total"), 1)
drive.sheet_values = lambda sid, a1: sheet_values(ROWS)
w_nc = World("e6nc")
w_nc.apply(source="", cmd="")
r2 = call(show)
text2 = json.dumps(r2, ensure_ascii=False, default=str).lower()
check("notes not configured: every row is hidden and the result is still silent",
      (r2.get("items"), r2.get("open_total"), [t for t in ("zz-", "hidden", "not shown") if t in text2]),
      ([], 0, []))
check("no write path was touched (sheet_update/append/create/share)", WRITES, [])

print("\nE6c the log: once per distinct set, then DEBUG; one audit event")
w = World("e6c")
w.put("drive", ACME_NAME, body("ZZACME"))
w.sync()
TAP.clear()
todos.open_items(bot.db)
first = TAP.at(logging.INFO, "hid ")
todos.open_items(bot.db)
second = TAP.at(logging.INFO, "hid ")
check("one INFO line for the set", len(first), 1)
check("...with the counts", ("hid 4 of 5" in first[0]) if first else None, True)
check("the identical second call adds no INFO line", len(second), 1)
check("...it logs at DEBUG instead", len(TAP.at(logging.DEBUG, "hid ")) >= 1, True)
audit = os.path.join(w.state, "audit.jsonl")
ev = [ln for ln in open(audit, encoding="utf-8").read().splitlines() if "todo_rows_hidden" in ln] \
    if os.path.exists(audit) else []
check("exactly one todo_rows_hidden audit event", len(ev), 1)
check("...with no task text in it", any("ZZ-" in ln for ln in ev), False)

print("\nE6d tools/list_hidden_todos.py")
tool_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "list_hidden_todos.py")
if not os.path.exists(tool_path):
    check("tools/list_hidden_todos.py exists", False, True)
else:
    def run_tool():
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                runpy.run_path(tool_path, run_name="__main__")
            except SystemExit as e:
                code = e.code or 0
        return code, buf.getvalue()

    code, out = run_tool()
    check("exit 0", code, 0)
    check("one table row per hidden row (open AND closed): 5", sum(1 for ln in out.splitlines()
                                                                  if ln.startswith("|") and "ZZ-" in ln), 5)
    for task, why in (("ZZ-STANDUP-TASK", "standup"), ("ZZ-PMCALL-TASK", "source_not_an_allowed_note"),
                      ("ZZ-BLANK-TASK", "no_source_meeting"), ("ZZ-WRONGDATE-TASK", "source_not_an_allowed_note"),
                      ("ZZ-CLOSED-STANDUP-TASK", "standup")):
        rows = [ln for ln in out.splitlines() if task in ln]
        check(f"{task} listed with reason {why}", bool(rows) and why in rows[0], True)
    check("the shown row is not listed", "ZZ-ALLOWED-TASK" in out, False)
    check("it never wrote", WRITES, [])
    config.TODO_SHEET_ID = ""
    bot.db.set_meta("todo_sheet_id", "") if hasattr(bot.db, "set_meta") else None
    code, out = run_tool()
    check("no sheet: the '0 rows' line and exit 0",
          (code, "The to-do sheet has not been created yet: 0 rows." in out), (0, True))
    check("it never created the sheet", WRITES, [])
config.TODO_SHEET_ID = ""

# =============================================================================
print("\nE10 the proactive consumers read only allowed notes")
w = World("e10")
w.put("notes", "Zorbex pricing call – 2026／10／04.txt", body("ZZEVIDSTRAY", "We booked a meeting with Zorbex."))
w.put("drive", AM_NAME, body("ZZEVIDSTANDUP", "We booked a meeting with Zorbex."))
w.put("drive", ACME_NAME, body("ZZACME"))
qd = os.path.join(w.notes, "_quarantine", "20261007-101010")
os.makedirs(qd)
with open(os.path.join(qd, "Zorbex old call – 2026／10／03.txt"), "w", encoding="utf-8") as f:
    f.write(body("ZZEVIDQUAR", "We booked a meeting with Zorbex."))
w.sync()
hit = evidence.gather(action_type=nextaction.R_LI_NO_DM, company="Zorbex", notes_module=notes, history=[],
                      today=date(2026, 10, 7))
check("evidence.gather finds nothing in quarantined / standup / stray notes", hit, None)
pl = "\n".join(prep._notes_section("Zorbex", notes_module=notes, today=date(2026, 10, 7)))
check("prep brief carries nothing from them",
      [t for t in ("ZZEVID", "AM Sync", "Zorbex pricing", "Zorbex old") if t in pl], [])
w.put("drive", "Zorbex intro call – 2026／10／06 10：00 IST.txt",
      body("ZZEVIDOK", "We booked a meeting with Zorbex."))
w.sync()
hit = evidence.gather(action_type=nextaction.R_LI_NO_DM, company="Zorbex", notes_module=notes, history=[],
                      today=date(2026, 10, 7))
check("CONTROL: the same text in an allowed note IS found (the test can fail)",
      (hit or {}).get("source"), evidence.SOURCE_NOTES)
pl = "\n".join(prep._notes_section("Zorbex", notes_module=notes, today=date(2026, 10, 7)))
check("CONTROL: the prep brief names the allowed note", "Zorbex intro call" in pl, True)

# =============================================================================
print("\nD8 (settled by the human; was Q2) last_good_copy_is_served_when_sync_fails: a failed sync serves the SALES folder's last good copy and flags it")
w = World("q2")
w.put("drive", ACME_NAME, body("ZZACME"))
check("first sync ok", w.sync(), True)
check("second sync fails", w.sync(fail=True), False)
check("state stays 'ok'", notes.source_state(), "ok")
check("the sales note is still readable", loaded_names(w), [ACME_NAME])
check("nothing_to_say() is None while a note loads", notes.nothing_to_say(), None)
lm = call(tools_of(bot._notes_tools, "what did we decide in the last meeting?")["list_meeting_notes"])
flat = json.dumps(lm, default=str)
check("the tool says the sync is degraded (the model is told the notes may be stale)",
      ((lm.get("notes_filter") or {}).get("sync_degraded"), bool(lm.get("sync", {}).get("degraded"))), (True, True))
check("...but the sync error text, the command and variable names stay out of the tool result",
      [t for t in ("FAKE-DRIVE-ERROR", "NOTES_", "fake_sync", "rclone") if t in flat], [])
check("status is DEGRADED", sources.SALES_MEETING_NOTES.status().get("status"), sources.DEGRADED)
check("sync.degraded is true in the tool result", bool((lm.get("sync") or {}).get("degraded")), True)
check("a failed run never quarantined the sales note", (w.quarantine_files(), w.top()), ([], [ACME_NAME]))

print("\nIN-FOLDER REGRESSION  the read path of the 6 Oct leak")
w = World("leak")
w.put("notes", PMCALL_NAME, body("ZZPMCALL"))
w.put("notes", AM_NAME, body("ZZAMSYNC"))
w.put("drive", ACME_NAME, body("ZZACME"))
w.sync()
rn = notes.read_note()
check("read_note() with no date returns the SALES note, not the 19 Aug PM Call",
      (rn or {}).get("title", "").startswith("Acme discovery call"), True)
every_tool = json.dumps(note_tool_results("what did we decide in the last meeting?"), default=str)
check("no sync / PM-call body text in any of the four tools", [t for t in ("ZZPMCALL", "ZZAMSYNC")
                                                               if t in every_tool], [])

check("no live Sheets / Drive call was attempted", GUARD.calls, [])
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)

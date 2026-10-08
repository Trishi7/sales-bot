"""THE 6 OCT EXCHANGE, REPLAYED THROUGH THE REAL on_message PATH — LIVE AND TEST MODE.

    python verify_replay_oct6.py

The one shared replay harness. It feeds tests/fixtures/oct6_exchange.md through
`SalesBot.on_message` -> the gate -> `_handle_query` -> the router -> the REAL
`_answer_with_engine` / `QueryEngine` tool loop twice, with SALES_TEST_MODE=false
and then true, and asserts the two runs agree on: the routing, the tools OFFERED,
the number of model calls, and the reply text (a leading [TEST...] tag aside).
Test mode and live behave identically; the tag is the only difference.

Offline and free: the Anthropic client is a fake that calls EVERY tool it is
offered and then echoes the tool results back as its answer (so anything a tool
hands the model can be seen in the reply, which is what leaked on 6 Oct); the
router/social model is fake; Discord is fake; the sheet is a stub; the DB,
NOTES_DIR and STATE_DIR are throwaway. No network, no rclone, no Drive.

EXTEND, DON'T FORK: each ticket turns on the steps it owns in STEPS below.
  step 1  NFT2-1062  "what do we need to do today?" -> AM/PM sync content.  ACTIVE
  steps 5, 8, 9  NFT2-1065  profile lookups: search first, never deny, ask before adding.  ACTIVE
  steps 2, 3, 4, 4b, 6, 7  NFT2-1063  replies and on-demand requests: a PoCs question and "Sure." to its answer (one
           reaction, no text, no vote), "what are the sales objectives for today?" (the day's posts, no rules or
           schedule), the interim wording, "top 5 AI headlines" over a seeded feed store.  ACTIVE
           (their section lives in tests/replay_1063.py, called from main(): a section of THIS harness, not a second one)
  7 OCT  tests/fixtures/oct7_exchange.md  NFT2-1063: R3's post, "any AI news?", "sure" to the interim line, the answer,
           then "yes" to R3's post.  ACTIVE, in both modes.
  RULE 13  (7 Oct 2026, next steps for connected contacts)  ACTIVE, after the profile section: a Rule 13 post on a
           stubbed 7 Oct sheet goes through the real `_send_drip_message` with SALES_TEST_MODE false then true
           (bodies equal apart from the tag); then a "done" and a "yes" are replied to it through the real
           `on_message` with an unrelated proposal open: "done" and "yes" ask which one (numbered, no model call),
           "sure" gets a reaction, a real question reaches the engine; zero sheet writes, the proposal untouched,
           no Rule 13 state moved, test mode == live. (The one-person lines are in tests/test_rule13.py, Y1-Y13.)

For NFT2-1062 step 1 runs in three arrangements:
  (a) notes NOT configured, with the real current command shape and stale sync
      files on disk;
  (b) configured, with stale files on disk (they must be quarantined, not read);
  (c) configured, with a standup doc placed INSIDE the sales folder.
and, per arrangement, once more with the FULL tool set and a model that calls all
four note tools, to prove no sync / PM-call text can come out of any of them.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from types import SimpleNamespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")      # a FAIL line may quote Gemini-style punctuation

TMP = tempfile.mkdtemp(prefix="saley-replay-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")
os.environ["STATE_DIR"] = os.path.join(TMP, "state0")
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import config  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}
config.INTERIM_ENABLED = False          # no timers: the fake model answers at once

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"))
import offline_guard  # noqa: E402
import replay_1063  # noqa: E402  NFT2-1063's section (steps 2, 3, 4, 6, 7 and the 7 Oct exchange)
import replies_world  # noqa: E402

GUARD = offline_guard.install_script()      # canned source statuses; any real Sheets/Drive call is counted and fails the run

import approvals  # noqa: E402
import drive  # noqa: E402
import gtm_sheet  # noqa: E402
import notes  # noqa: E402
import query_engine  # noqa: E402
import search_backend  # noqa: E402
import toolsets  # noqa: E402
import usage  # noqa: E402
from bot import SalesBot  # noqa: E402
from db import DB  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "tests", "fixtures", "oct6_exchange.md")
BOT_ID = 999
_DBN = __import__('itertools').count(1)      # one DB file per bot (id(model) was reused between runs)

failures = 0


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


class Tap(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


TAP = Tap()
logging.getLogger().addHandler(TAP)
logging.getLogger().setLevel(logging.INFO)

# the bot's own identity: discord.Client.user is a read-only property
SalesBot.user = property(lambda self: SimpleNamespace(id=BOT_ID))

# -- the fixture ---------------------------------------------------------------

STEP_RE = re.compile(r'^(\d+)\.\s+~?[\d:]+(?:\s+and\s+~?[\d:]+)?\s+(\w+)[^:"]*:\s+"(?P<text>.*)"\s*$')


def fixture_steps():
    out = {}
    for line in open(FIXTURE, encoding="utf-8").read().splitlines():
        m = STEP_RE.match(line)
        if m:
            out[int(m.group(1))] = {"who": m.group(2), "text": m.group("text")}
    return out


FIX = fixture_steps()
# step -> (owner ticket, active in this run)
STEPS = {1: ("NFT2-1062", True)}
for _n in range(2, 10):
    STEPS[_n] = ("NFT2-1065", True) if _n in (5, 8, 9) else ("NFT2-1063", _n in (2, 3, 4, 6, 7))

# The fixture has no QUOTED text for steps 5 and 8 (the wording was reconstructed from
# screenshots), so the harness carries it here, labelled. Step 9 is verbatim in the fixture.
RECONSTRUCTED = {
    5: "@Saley research profiles for Sigil Wen",
    8: "@Saley LinkedIn and research profile links for Janajit Bagchi and Suryansh Shukla (ARTPARK India)",
}

# -- the world -------------------------------------------------------------------

FOLDER = "Saley – Sales Notes"
AM = "NFThing Kick-off ( AM Sync) – 2026／10／05 10：30 IST – Notes by Gemini.txt"
PM = "NFThing Wrap-up ( PM Sync) – 2026／10／05 18：30 IST – Notes by Gemini.txt"
PMCALL = "PM Call – 2026／08／19 11：20 IST – Notes by Gemini.txt"
ACME = "Acme discovery call – 2026／10／05 15：00 IST – Notes by Gemini.txt"
LEAK_TOKENS = ("ZZAMSYNC", "ZZPMSYNC", "ZZPMCALL", "ZZINFOLDERSTANDUP", "ZZ-SYNC-TODO", "ZZ-PMCALL-TODO")

SYNC_SCRIPT = os.path.join(TMP, "fake_sync.py")
with open(SYNC_SCRIPT, "w", encoding="utf-8") as f:
    f.write("import os, shutil, sys\nsrc, dst = sys.argv[1], sys.argv[2]\n"
            "os.makedirs(dst, exist_ok=True)\n"
            "for n in os.listdir(src):\n"
            "    p = os.path.join(src, n)\n"
            "    if os.path.isfile(p):\n        shutil.copy2(p, os.path.join(dst, n))\n")


def body(token):
    return (f"Summary\n{token} product work: API updates and audio data quality.\n\n"
            f"Decisions\n* {token} ship the JSON update\n\nNext steps\n* [Shashi] {token} update the JSON\n")


def sheet_rows(acme_cit):
    h = ["#", "To-do", "Owner", "Source meeting", "Date raised", "Due", "Status", "Notes"]
    return [h,
            ["1", "ZZ-ALLOWED-TODO send the Acme deck", "Asha", acme_cit, "2026-10-05", "", "Open", ""],
            ["2", "ZZ-SYNC-TODO update the JSON", "Ben", "NFThing Kick-off ( AM Sync), 5 Oct", "2026-10-05",
             "", "Open", ""],
            ["3", "ZZ-PMCALL-TODO tag the new JSON files", "Chen", "PM Call, 19 Aug", "2026-08-19", "", "Open", ""]]


def build_world(arrangement):
    root = tempfile.mkdtemp(prefix=arrangement + "-", dir=TMP)
    d, n, s = (os.path.join(root, x) for x in ("drive", "notes", "state"))
    for p in (d, n, s):
        os.makedirs(p)
    for name, token in ((PMCALL, "ZZPMCALL"), (AM, "ZZAMSYNC"), (PM, "ZZPMSYNC")):
        with open(os.path.join(n, name), "w", encoding="utf-8") as f:    # the leftovers of the old sync
            f.write(body(token))
    with open(os.path.join(d, ACME), "w", encoding="utf-8") as f:
        f.write(body("ZZACME-OK"))
    config.NOTES_DIR = n
    config.STATE_DIR = s
    os.environ["STATE_DIR"] = s
    config.NOTES_SYNC_MINUTES = 30
    config.NOTES_SYNC_TIMEOUT_SECONDS = 60
    config.NOTES_EXCLUDE_TITLE_PATTERNS = ["AM sync", "PM sync"]       # the REAL pinned value
    config.NOTES_REQUIRE_TITLE_TAGS = []
    if arrangement == "a":
        config.NOTES_SOURCE_FOLDER = ""
        config.NOTES_SYNC_CMD = f'rclone copy "gdrive:Sales Meeting Notes" {n}'   # the real current shape
    else:
        config.NOTES_SOURCE_FOLDER = FOLDER
        config.NOTES_SYNC_CMD = f'"{sys.executable}" "{SYNC_SCRIPT}" "{d}" "{n}" "{FOLDER}"'
        if arrangement == "c":
            with open(os.path.join(d, AM), "w", encoding="utf-8") as f:
                f.write(body("ZZINFOLDERSTANDUP"))
    # reset the notes module between worlds
    for name in dir(notes):
        v = getattr(notes, name)
        if isinstance(v, (dict, set, list)) and ("CACHE" in name.upper() or name.startswith("_LAST")):
            v.clear()
    notes._SYNC.update(last_attempt=None, last_success=None, ok=None, error=None, remedy=None, runs=0)
    if hasattr(notes, "_LAST_LOGGED_FAILURE"):
        notes._LAST_LOGGED_FAILURE = None
    notes._STATS.update(scanned_at=None, docs_seen=0, notes_seen=0, loaded_docs=0, excluded_docs=0)
    if arrangement != "a":
        notes.sync_now(reason="startup")        # what the bot does at boot
    # the to-do sheet: three rows, two of them sync-sourced
    import meetings
    note = notes.read_note(date="2026-10-05", label="Acme") if arrangement != "a" else None
    cit = meetings.citation(note) if note else "Acme discovery call, 5 Oct"
    config.TODO_SHEET_ID = "FAKE-TODO-SHEET"
    config.TODO_SHEET_ENABLED = True
    drive.sheet_values = lambda sid, a1: sheet_rows(cit)
    drive.sheet_url = lambda sid: f"https://sheets.example/{sid}"
    return SimpleNamespace(root=root, notes=n, drive=d, state=s)


# -- fakes ---------------------------------------------------------------------

class Typing:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return None


class FakeChannel:
    id = 4242
    name = "sales"
    guild = SimpleNamespace(id=1)

    def __init__(self):
        self.sent = []

    def typing(self):
        return Typing()

    async def send(self, text):
        self.sent.append(text)
        return SimpleNamespace(id=len(self.sent) + 5000, jump_url="https://discord/x")


class FakeMessage:
    _n = [1000]

    def __init__(self, channel, content, who="Kushal"):
        FakeMessage._n[0] += 1
        self.id = FakeMessage._n[0]
        self.content = content
        self.channel = channel
        self.guild = channel.guild
        self.author = SimpleNamespace(id=7, bot=False, name=who.lower(), display_name=who, global_name=who)
        self.reference = None
        self.mentions = [SimpleNamespace(id=BOT_ID)]
        self.mention_everyone = False

    async def add_reaction(self, emoji):
        REACTIONS.append((self.content, str(emoji)))

    async def reply(self, text, mention_author=False):
        self.channel.sent.append(text)
        return SimpleNamespace(id=len(self.channel.sent) + 5000, jump_url="https://discord/x")


REACTIONS = []                       # (text reacted to, emoji): what the Rule 13 section counts


class FakeLLM:
    """The router and the social voice."""

    async def parse_query(self, *, text, requester, history):
        return {"message_kind": "question", "is_query": True}

    async def social_reply(self, *, kind, text, requester):
        return "Hi."


class Model:
    """Calls EVERY tool it is offered, then echoes what they returned."""

    def __init__(self):
        self.calls = 0
        self.offered = []

    def create(self, **kw):
        self.calls += 1
        if self.calls == 1:
            names = [t["name"] for t in (kw.get("tools") or []) if t.get("name")]
            self.offered = names
            blocks = [SimpleNamespace(type="tool_use", id=f"tu{i}", name=n, input={})
                      for i, n in enumerate(names)]
            return SimpleNamespace(content=blocks, stop_reason="tool_use",
                                   usage=SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=0)))
        results = []
        for m in kw.get("messages") or []:
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = b.get("content")
                        results.append(c if isinstance(c, str) else json.dumps(c, default=str))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ECHO " + " | ".join(results),
                                                        citations=[])],
                               stop_reason="end_turn",
                               usage=SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=0)))


OTHER_TOOLS = sorted({n for g, names in toolsets.GROUPS.items()
                      if g not in ("notes", "todos", "today") for n in names})
RESULTS = []


def stub(name):
    async def handler(_inp):
        r = {"stub": name}
        RESULTS.append((name, json.dumps(r)))
        return r
    return {"schema": {"name": name, "description": "stub",
                       "input_schema": {"type": "object", "properties": {}}}, "handler": handler}


def recorded(tools):
    out = []
    for t in tools:
        if not t.get("handler"):
            out.append(t)
            continue
        inner, name = t["handler"], t["schema"]["name"]

        async def h(inp, inner=inner, name=name):
            r = await inner(inp)
            RESULTS.append((name, json.dumps(r, ensure_ascii=False, default=str)))
            return r
        out.append({**t, "handler": h})
    return out


def make_bot(model, web=False):
    bot = SalesBot()
    bot.db = DB(os.path.join(TMP, f"b{next(_DBN)}_test.db"))
    bot.llm = FakeLLM()
    bot.query_engine._client = SimpleNamespace(messages=model)
    real_notes, real_todo = bot._notes_tools, bot._todo_tools
    bot._notes_tools = lambda t: recorded(real_notes(t))
    bot._todo_tools = lambda: recorded(real_todo())
    skip = {"web_search", "fetch_page", "propose_poc_add"} if web else set()
    bot._discord_tools = lambda m: [stub(n) for n in OTHER_TOOLS if n not in skip]
    bot._sheet_tools = lambda m: []
    bot._mapping_tools = lambda: []
    bot._strategy_tools = lambda: []
    bot._people_tools = lambda sink=None: []
    bot._news_tools = lambda: []

    async def no_web(*a, **k):
        return [], "", ""

    if not web:
        bot._websearch_tools = no_web
    else:
        async def left():
            return (100, 0, 150)

        bot._search_left = left

    async def not_an_update(_m, _t):
        return False

    bot._maybe_apply_sheet_update = not_an_update

    async def no_companies():
        return []

    bot._tracker_company_names = no_companies
    return bot


def strip_tag(text):
    return re.sub(r"^\[TEST[^\]]*\]\s*", "", text or "")


def stable(text, w):
    """Mask what differs between two runs by construction: the temp root and wall-clock stamps."""
    text = text.replace(json.dumps(w.root)[1:-1], "<ROOT>").replace(w.root, "<ROOT>")
    return re.sub(r"\d{4}-\d\d-\d\dT[\d:.+-]+", "<TS>", text)


def norm_log(line):
    return re.sub(r"msg=\d+", "msg=N", line)


async def replay_step1(arrangement, test_mode):
    config.SALES_TEST_MODE = test_mode
    w = build_world(arrangement)
    RESULTS.clear()
    TAP.lines.clear()
    model = Model()
    bot = make_bot(model)
    ch = FakeChannel()
    text = FIX[1]["text"].replace("@Saley", f"<@{BOT_ID}>")
    # NFT2-1063: the "today" route now also offers todays_objectives, whose answer reads the day's plan. An EMPTY
    # offline sheet stands in (replies_world.Sheet), so the answer is the one-line nothing-today and no real Sheets call
    # is made (the offline guard would fail the run).
    empty_sheet = replies_world.Sheet()
    empty_sheet.install()
    try:
        await bot.on_message(FakeMessage(ch, text, FIX[1]["who"]))
    finally:
        empty_sheet.uninstall()
    rows = bot.db.reply_latency_since("2000-01-01")
    return {
        "gate": [norm_log(m) for m in TAP.lines if m.startswith("[gate]")],
        "route": [r["route"] for r in rows],
        "engine": [norm_log(m) for m in TAP.lines if m.startswith("[engine]") and "routed by" in m],
        "tools_offered": list(model.offered),
        "model_calls": model.calls,
        "reply": [strip_tag(m) for m in ch.sent],
        "tagged": [m.startswith("[TEST") for m in ch.sent],
        "tool_results": [(n, stable(r, w)) for n, r in RESULTS],
    }


async def full_set_run(arrangement, test_mode):
    """The fake model gets EVERY tool and calls all of them."""
    config.SALES_TEST_MODE = test_mode
    w = build_world(arrangement)
    RESULTS.clear()
    model = Model()
    bot = make_bot(model)
    q = "what did we decide in the last meeting and what are the action items?"
    tools = (bot._discord_tools(None) + bot._notes_tools(q) + bot._todo_tools())
    reply = await bot.query_engine.answer(question=q, requester_name="Kushal", tools=tools, outcome={})
    return {"offered": sorted(model.offered), "calls": model.calls, "reply": stable(reply or "", w),
            "results": [(n, stable(r, w)) for n, r in RESULTS]}


def leaks(text):
    return [t for t in LEAK_TOKENS if t in (text or "")]


# -- NFT2-1065: steps 5, 8, 9 (profile lookups) --------------------------------------

J_URL = "https://www.linkedin.com/in/janajit-bagchi"
S_URL = "https://www.linkedin.com/in/suryansh-shukla-artpark"
SW_URL = "https://www.linkedin.com/in/sigil-wen"
DENIALS = ("don't have a web search", "do not have a web search", "don't have web search",
           "can't pull linkedin", "cannot pull linkedin", "no web search")


class ScriptedModel:
    """Says exactly which tools to call; remembers what it was offered and what came back."""

    def __init__(self):
        self.script = []
        self.calls = 0
        self.offered = []
        self.results = []
        self._names = {}

    def create(self, **kw):
        self.calls += 1
        self.offered.append([t["name"] for t in (kw.get("tools") or []) if t.get("name")])
        msgs = kw.get("messages") or []
        if msgs and msgs[-1].get("role") == "user" and isinstance(msgs[-1].get("content"), list):
            for b in msgs[-1]["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    raw = b.get("content")
                    raw = raw if isinstance(raw, str) else json.dumps(raw)
                    try:
                        parsed = json.loads(raw)
                    except ValueError:
                        parsed = {"_raw": raw}
                    self.results.append((self._names.get(b.get("tool_use_id"), "?"), parsed))
        step = self.script.pop(0) if self.script else ("say", "(script ended)")
        u = SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=0))
        if step[0] == "say":
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=step[1], citations=[])],
                                   stop_reason="end_turn", usage=u)
        blocks = []
        for i, (name, inp) in enumerate(step[1]):
            tid = f"tu_{self.calls}_{i}"
            self._names[tid] = name
            blocks.append(SimpleNamespace(type="tool_use", id=tid, name=name, input=inp))
        return SimpleNamespace(content=blocks, stop_reason="tool_use", usage=u)


def _r(title, url, snippet="snippet"):
    return {"title": title, "url": url, "snippet": snippet}


SEARCH_TABLE = {
    "sigil": [_r("Sigil Wen - LinkedIn", SW_URL, "Sigil Wen")],
    "janajit": [_r("Janajit Bagchi - ARTPARK India | LinkedIn", J_URL, "Janajit Bagchi")],
    "suryansh": [_r("Suryansh Shukla - ARTPARK | LinkedIn", S_URL, "Suryansh Shukla")],
}


def fake_backend(queries):
    import search_backend

    def search_detail(query, *, n=10, news=False, site=None, days=None, rule="search"):
        queries.append(query)
        for key, rs in SEARCH_TABLE.items():
            if key in query.lower():
                return {"results": [dict(x) for x in rs], "cached": False, "backend": "fake",
                        "requests": 1, "error": ""}
        return {"results": [], "cached": False, "backend": "fake", "requests": 1, "error": ""}

    search_backend.search_detail = search_detail
    search_backend.news_detail = lambda q, **k: {"results": [], "cached": False, "backend": "g",
                                                 "requests": 0, "error": ""}
    search_backend.available = lambda: (True, "")
    import usage
    usage.over_budget = lambda: False


def poc_sheet(appended):
    import deadlines as dl
    import gtm_sheet

    hdr = ["Sr No", "Company/Uni", "Industry", "Name", "Designation", "Email id", "Based",
           "Research Paper Link", "LI Url"] + [f"c{i}" for i in range(15)]
    tab = gtm_sheet.SHEETS._parse_values("Outreach PoCs", [hdr], read_at=dl.real_epoch())
    gtm_sheet.SHEETS.tab = lambda kind, which=None: tab if kind == gtm_sheet.POCS else None

    def append_row(tab_, values, *, reason, expect_company="", dry_run=False):
        appended.append(dict(values))
        return {"ok": True, "sheet_row": 100 + len(appended), "written": list(values),
                "dry_run": False, "error": ""}

    gtm_sheet.SHEETS.append_row = append_row
    config.SHEET_ROW_ADDITIONS_ENABLED = True
    config.SHEET_APPENDABLE_TABS = [gtm_sheet.POCS]


def engine_groups(lines):
    out = []
    for m in lines:
        mm = re.search(r"\[engine\] msg=\d+ tools=(\d+) \(([^)]*)\)", m)
        if mm:
            out.append(mm.group(2))
    return out


async def replay_profile(test_mode):
    """Steps 5, 8 (twice, as on 6 Oct) and 9, through the real on_message path."""
    import approvals

    config.SALES_TEST_MODE = test_mode
    config.WEB_SEARCH_ENABLED = True
    config.SEARCH_BACKEND = "searxng"
    build_world("b")                       # the notes/to-do state is not used by these steps
    queries, appended = [], []
    fake_backend(queries)
    poc_sheet(appended)
    model = ScriptedModel()
    bot = make_bot(model, web=True)
    ch = FakeChannel()
    rec = {"ext": {}, "queries": {}, "replies": {}, "offered": {}, "groups": {}, "calls": {}, "gate": {},
           "route": {}}

    async def step(key, text, script, who="Vaishnavi"):
        TAP.lines.clear()
        model.script = list(script)
        q0, c0, o0, s0 = len(queries), model.calls, len(model.offered), len(ch.sent)
        n_rows = len(bot.db.reply_latency_since("2000-01-01"))
        await bot.on_message(FakeMessage(ch, text.replace("@Saley", f"<@{BOT_ID}>"), who))
        rec["queries"][key] = len(queries) - q0
        rec["ext"][key] = [norm_log(m) for m in TAP.lines if "LIMIT EXTENDED ONCE" in m]
        rec["replies"][key] = [strip_tag(m) for m in ch.sent[s0:]]
        rec["offered"][key] = [list(x) for x in model.offered[o0:]]
        rec["groups"][key] = engine_groups(TAP.lines)
        rec["calls"][key] = model.calls - c0
        rec["gate"][key] = [norm_log(m) for m in TAP.lines if m.startswith("[gate]")]
        rec["route"][key] = [r["route"] for r in bot.db.reply_latency_since("2000-01-01")][n_rows:]

    # step 5 (reconstructed wording), at the BASE limit (4 searches / 7 rounds, one extension to 6 / 9): the model
    # batches seven searches, one per kind of profile. The 5th is a new query at the limit, so the question is
    # extended ONCE (logged); the 7th is past the extension too and is told what was run and what was not.
    config.WEB_QUESTION_MAX_SEARCHES = 4
    config.WEB_QUESTION_EXTENDED_SEARCHES = 6
    config.QUERY_ENGINE_MAX_TOOL_ITERATIONS = 7
    config.QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS = 9
    kinds = ["linkedin", "google scholar", "X profile", "personal site", "orcid", "github", "researchgate"]
    await step("5", RECONSTRUCTED[5], [
        ("use", [("web_search", {"query": '"Sigil Wen" ' + k}) for k in kinds]),
        ("say", "Sigil Wen\n- LinkedIn: " + SW_URL + " - \"Sigil Wen - LinkedIn\"\n"
                "- Research profile: not found in public search\n- X: not found in public search\n"
                "- Personal site: not found in public search\n- ORCID: not found in public search\n"
                "- GitHub: not found in public search\n- ResearchGate: not checked yet")])
    rec["step5_results"] = [r for n, r in model.results if n == "web_search"]

    # step 8, asked twice as on 6 Oct (reconstructed wording)
    ans8 = ("Janajit Bagchi\n- LinkedIn: " + J_URL + "\nSuryansh Shukla\n- LinkedIn: " + S_URL)
    for key in ("8a", "8b"):
        await step(key, RECONSTRUCTED[8], [
            ("use", [("web_search", {"query": '"Janajit Bagchi" ARTPARK linkedin'}),
                     ("web_search", {"query": '"Suryansh Shukla" ARTPARK linkedin'})]),
            ("say", ans8)])

    # step 9: verbatim from the fixture
    await step("9", "@Saley " + FIX[9]["text"], [
        ("use", [("web_search", {"query": '"Janajit Bagchi" ARTPARK linkedin'}),
                 ("web_search", {"query": '"Suryansh Shukla" ARTPARK linkedin'})]),
        ("use", [("propose_poc_add", {"people": [
            {"name": "Janajit Bagchi", "company": "ARTPARK India", "linkedin_url": J_URL},
            {"name": "Suryansh Shukla", "company": "ARTPARK India", "linkedin_url": S_URL}]})]),
        ("say", ans8)])
    with bot.db.conn() as c:
        rows = c.execute("SELECT kind, status, message_id FROM write_proposals ORDER BY rowid").fetchall()
    rec["proposals"] = [(r[0], r[1]) for r in rows]
    rec["offer_is_last_message"] = bool(rows) and str(rows[-1][2]) == str(len(ch.sent) + 5000)
    rec["appended"] = len(appended)
    rec["expected_offer"] = approvals.row_add_offer(["Janajit Bagchi", "Suryansh Shukla"], "Outreach PoCs")
    return rec


async def profile_section(wired):
    live = await replay_profile(False)
    test = await replay_profile(True)
    web_pair = {"web_search", "fetch_page"}
    for key in ("5", "8a", "8b", "9"):
        first = (live["offered"][key] or [[]])[0]
        check(f"step {key}: web_search and fetch_page are OFFERED", web_pair <= set(first), True)
        check(f"step {key}: the FIRST ask searched", live["queries"][key] >= 1, True)
        check(f"step {key}: no denial in any reply",
              [d for d in DENIALS if any(d in t.lower() for t in live["replies"][key])], [])
        check(f"step {key}: routed to the engine", live["route"][key], ["engine"])
    seventh = (live["step5_results"] + [{}] * 7)[6]
    check("step 5 (base limit 4): the 5th different query extended the question to 6 - six searches ran",
          live["queries"]["5"], 6)
    check("step 5: exactly ONE 'LIMIT EXTENDED ONCE' line, 4 -> 6 searches and 7 -> 9 rounds",
          (len(live["ext"]["5"]), "searches 4 -> 6" in " ".join(live["ext"]["5"]),
           "tool rounds 7 -> 9" in " ".join(live["ext"]["5"])), (1, True, True))
    check("step 5: no other step was extended", [live["ext"][k] for k in ("8a", "8b", "9")], [[], [], []])
    check("step 5: the 7th search was refused with what was run (six) and what was not",
          ("error" in seventh, len(seventh.get("searches_run") or []), bool(seventh.get("not_run"))),
          (True, 6, True))
    r5 = " ".join(live["replies"]["5"])
    check("step 5: the reply says what was found and what was not checked yet, no 'limit'",
          ("not checked yet" in r5.lower(), "limit" in r5.lower()), (True, False))
    check("step 8: both asks searched (once per person)", (live["queries"]["8a"], live["queries"]["8b"]), (2, 2))
    check("step 9: routed web + profile", all(g in live["groups"]["9"][0] for g in ("web", "profile"))
          if live["groups"]["9"] else False, True)
    if wired:
        check("step 9: exactly one open row_add proposal, written nothing", (live["proposals"], live["appended"]),
              ([("row_add", "open")], 0))
        check("step 9: the LAST message is the offer, word for word, and the proposal is keyed to it",
              (strip_tag(live["replies"]["9"][-1]) if live["replies"]["9"] else None,
               live["offer_is_last_message"]), (strip_tag(live["expected_offer"]), True))
        print("      offer as printed:", live["replies"]["9"][-1] if live["replies"]["9"] else None)
    else:
        check("step 9 (Q1 held): no offer, no proposal opened, nothing written",
              (live["proposals"], live["appended"],
               any(strip_tag(t).startswith("Want me to add") for t in live["replies"]["9"])), ([], 0, False))
    for key in ("5", "8a", "9"):
        print(f"      step {key} replies (live): " + " || ".join(t.replace(chr(10), " / ") for t in live["replies"][key]))
    for k in ("gate", "route", "groups", "offered", "calls", "replies", "queries", "ext", "proposals", "appended"):
        check(f"test mode == live (profile steps): {k}", test[k], live[k])



# -- RULE 13 (7 Oct 2026): a Rule 13 post, and "done" / "yes" replied to it --------------------------------

R13_DAY = __import__("datetime").date(2026, 10, 15)       # a Thursday
R13_WRITES = []                                             # every cell the stand-in worksheet is asked to write


def r13_sheet():
    """A stubbed Outreach PoCs tab on the 7 Oct header row: made-up Connected people, the real parser."""
    import copy
    import time as _t

    import rule13_fixtures as fx
    rows, meta = fx.rotation_values()
    grid = [list(fx.HEADERS_7OCT)] + [list(r) for r in rows]
    saved = {k: getattr(gtm_sheet.SHEETS, k) for k in ("read", "cadence_tab", "_open", "_refuse_if_read_only",
                                                       "staleness_note", "tab")}

    def read(which=None, force=False, **_k):
        tab = gtm_sheet.SHEETS._parse_values("Outreach PoCs", copy.deepcopy(grid), read_at=_t.time())
        return {tab.kind: tab}

    class Ws:
        title = "Outreach PoCs"

        def update(self, values=None, range_name="", value_input_option=""):
            R13_WRITES.append((range_name, values[0][0] if values else None))

        def batch_update(self, *a, **k):
            R13_WRITES.append(("batch_update", a))

    gtm_sheet.SHEETS.read = read
    gtm_sheet.SHEETS.cadence_tab = lambda *a, **k: (read().get(gtm_sheet.POCS), "fixture")
    gtm_sheet.SHEETS.tab = lambda kind, which=None: read().get(kind)
    gtm_sheet.SHEETS._open = lambda *a, **k: SimpleNamespace(worksheet=lambda t: Ws(),
                                                              batch_update=lambda *a, **k: R13_WRITES.append(("ss", a)))
    gtm_sheet.SHEETS._refuse_if_read_only = lambda *a, **k: ""
    gtm_sheet.SHEETS.staleness_note = lambda *a, **k: ""
    return meta, saved, rows


def r13_restore(saved):
    for k, v in saved.items():
        setattr(gtm_sheet.SHEETS, k, v)


class ExtractsNothing(FakeLLM):
    """The sheet-update extractor says 'not an update' (as it does for 'done'): the reply falls to the engine."""

    async def extract_sheet_update(self, **_kw):
        return {"intent": "none"}


def db_dump(bot):
    with bot.db.conn() as c:
        out = []
        for t in ("next_step_followups", "next_step_posts"):
            try:
                out.append([tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()])
            except Exception:
                out.append(["(no such table)"])
        return out


async def replay_rule13(test_mode):
    import deadlines as dl
    config.SALES_TEST_MODE = test_mode
    config.SALES_TEST_CHANNEL_ID = 4242
    config.SIMULATION_PREFIX = "[TEST]"
    config.EMAIL_WRITE_ALLOWED = True
    config.SHEET_WRITES_ENABLED = True
    config.SALES_APPROVER_IDS = [7]
    config.TEAM_ROSTER_IDS = [7, 8]
    w = build_world("b")
    meta, saved, rows = r13_sheet()
    R13_WRITES.clear()
    try:
        model = Model()
        bot = make_bot(model)
        bot.llm = ExtractsNothing()
        real_apply = SalesBot._maybe_apply_sheet_update
        bot._maybe_apply_sheet_update = real_apply.__get__(bot)          # the REAL reply path, not the harness stub
        bot._split_active = lambda rows_, why: (list(rows_), [])

        async def nobody_away(*_a, **_k):
            return {}

        import leave
        leave.who_is_away = nobody_away
        ch = FakeChannel()
        planned = await bot._plan_drip(today=R13_DAY, already=[])
        msg = next((m for m in planned["messages"] if m["type"] == "next_step_followups"), None)
        rec = {"planned": msg is not None}
        if msg is None:
            return rec
        rec["at"], rec["pinned"], rec["counts"] = msg["send_at_hhmm"], msg["pinned"], msg["counts_toward_cap"]
        await bot._send_drip_message(ch, msg, marker=dl.iso(R13_DAY), channel_id=4242)
        rec["post"] = [strip_tag(t) for t in ch.sent]
        rec["tagged"] = [t.startswith("[TEST") for t in ch.sent]
        rec["model_calls_for_the_post"] = model.calls
        rec["state_after_send"] = db_dump(bot)
        with bot.db.conn() as c:
            got = c.execute("SELECT message_id FROM drip_sends WHERE action_type='next_step_followups'").fetchone()
        post_id = int(got[0]) if got and got[0] else None
        rec["post_id"] = bool(post_id)

        # an UNRELATED open proposal that a stray "yes" could apply (F2): a Meeting Date cell on another person
        target = next(r for r in rows if r[3] == meta["dated"][2])
        sheet_row = rows.index(target) + 2
        import sheetwrite
        tab_ = gtm_sheet.SHEETS.read()[gtm_sheet.POCS]
        row_ = next(r for r in tab_.rows if r.get("_row") == sheet_row)
        plan_ = sheetwrite.plan_writes(tab=tab_, row=row_, fields=[{"role": "meeting_date", "value": "21-10-2026",
                                                                  "supersedes": False, "quote": "met them"}],
                                       trigger=sheetwrite.TRIGGER_REPLY, reply_text="met them")
        bot.db.open_proposal(
            proposal_key="pOther", kind="cell_update", tab="Outreach PoCs", sheet_row=sheet_row,
            row_key=f"{target[1].lower()}|{target[3].lower()}", company=target[1], poc=target[3],
            payload={"writes": plan_["writes"], "applied": plan_["applied"], "asks": plan_["asks"],
                     "skipped": plan_["skipped"]},
            reply_text="met them", trigger="reply", proposed_text="Set Meeting Date? Reply yes.",
            requested_by="Kushal", channel_id=4242, message_id="m-other", created_at="2026-10-15T14:00:00")
        before = db_dump(bot)
        R13_WRITES.clear()
        parent = SimpleNamespace(id=post_id, author=SimpleNamespace(id=BOT_ID))
        rec["replies"] = {}
        for key, text in (("done", "done"), ("yes", "yes"), ("sure", "sure"),
                          ("question", "which email template should I use for Priya?")):
            model.calls, model.offered = 0, []
            n0, r0 = len(ch.sent), len(REACTIONS)
            m = FakeMessage(ch, text, "Kushal")
            m.mentions = []
            m.reference = SimpleNamespace(message_id=post_id, cached_message=parent, resolved=None)
            TAP.lines.clear()
            await bot.on_message(m)
            rec["replies"][key] = {"sent": [stable(strip_tag(t), w) for t in ch.sent[n0:]],
                                   "calls": model.calls, "reactions": len(REACTIONS) - r0,
                                   "offered": list(model.offered),
                                   "gate": [norm_log(x) for x in TAP.lines if x.startswith("[gate]")],
                                   "voted": any("[approvals]" in x and "vote" in x.lower() and "not a vote" not in x
                                                for x in TAP.lines)}
        rec["writes"] = list(R13_WRITES)
        rec["proposal"] = bot.db.proposal("pOther")["status"]
        rec["votes"] = len(bot.db.proposal("pOther")["votes"] or [])
        rec["state_unchanged"] = db_dump(bot) == before

        # THE CONTROL: an approver's "yes" replied to the PROPOSAL'S OWN message does vote on it, so the zeros above
        # are not a broken setup (an approver, an open proposal, a reply path that reaches the vote). The F2 fallback
        # (a "yes" to a message with no proposal falling back to the newest open one) is pinned in verify_rule13.py
        # (G1), which calls the vote path directly; through on_message NFT2-1063's reply hook now answers a bare
        # "yes" to an unrelated message with an ack before that fallback is reached.
        ctrl_parent = SimpleNamespace(id="m-other", author=SimpleNamespace(id=BOT_ID))
        c = FakeMessage(ch, "yes", "Kushal")
        c.mentions = []
        c.reference = SimpleNamespace(message_id="m-other", cached_message=ctrl_parent, resolved=None)
        await bot.on_message(c)
        rec["control_votes"] = len(bot.db.proposal("pOther")["votes"] or [])
        rec["control_status"] = bot.db.proposal("pOther")["status"]
        return rec
    finally:
        r13_restore(saved)


async def rule13_section():
    live = await replay_rule13(False)
    test = await replay_rule13(True)
    check("rule 13: the post was planned (a weekday, the 7 Oct sheet)", (live["planned"], test["planned"]), (True, True))
    if not live["planned"]:
        return
    check("rule 13: planned at 15:00, pinned, outside the cap", (live["at"], live["pinned"], live["counts"]),
          ("15:00", True, False))
    check("rule 13: live post is not tagged; test-mode post is", (live["tagged"], test["tagged"]), ([False], [True]))
    check("rule 13: live == test mode: the same post apart from the tag", test["post"], live["post"])
    check("rule 13: the post made no model call (verbatim)", (live["model_calls_for_the_post"],
                                                               test["model_calls_for_the_post"]), (0, 0))
    check("rule 13: five people recorded after the send, plus one posts row",
          (len(live["state_after_send"][0]), len(live["state_after_send"][1])), (5, 1))
    check("rule 13: the post's message id is on the drip row (what a reply finds it by)", live["post_id"], True)
    for key in ("done", "yes", "sure", "question"):
        check(f"rule 13: a {key!r} reply to the post casts no vote", live["replies"][key]["voted"], False)
    for key in ("done", "yes"):
        r = live["replies"][key]
        check(f"rule 13: {key!r} under a five-person post asks which one (numbered), with no model call",
              (len(r["sent"]) == 1 and r["sent"][0].startswith("Which one?" + chr(10) + "1. "), r["calls"], r["reactions"]),
              (True, 0, 0))
    check("rule 13: 'sure' gets one reaction, no text, no model call",
          (live["replies"]["sure"]["sent"], live["replies"]["sure"]["reactions"], live["replies"]["sure"]["calls"]),
          ([], 1, 0))
    check("rule 13: a real question reaches the engine and is answered",
          (live["replies"]["question"]["calls"] > 0, bool(live["replies"]["question"]["sent"])), (True, True))
    check("rule 13: zero sheet writes from the four replies", live["writes"], [])
    check("rule 13: the unrelated open proposal was not voted on or applied by a reply to the post",
          (live["proposal"], live["votes"]), ("open", 0))
    check("rule 13: no Rule 13 state changed by the replies", live["state_unchanged"], True)
    check("rule 13 control: a 'yes' replied to the proposal's own message DOES vote on it (the setup is live)",
          live["control_votes"] >= 1, True)
    for key in ("done", "yes", "sure", "question"):
        for k in ("sent", "calls", "offered", "gate", "reactions"):
            check(f"rule 13: test mode == live: reply {key!r} {k}", test["replies"][key][k], live["replies"][key][k])
    check("rule 13: test mode == live: writes, proposal, votes", (test["writes"], test["proposal"], test["votes"]),
          (live["writes"], live["proposal"], live["votes"]))
    print("      rule 13 post (live): " + " / ".join(live["post"][0].splitlines()[:3]))
    print("      reply 'done' (live): " + " | ".join(t.replace(chr(10), " / ")[:120] for t in live["replies"]["done"]["sent"]))



async def main():
    print("fixture steps parsed:", sorted(FIX))
    check("step 1 is the AM/PM sync question", FIX.get(1, {}).get("text"), "@Saley what do we need to do today?")
    for n, (owner, active) in sorted(STEPS.items()):
        if not active:
            print(f"  SKIP  step {n}: {FIX.get(n, {}).get('text', '(see fixture)')!r} — stub, owned by {owner}")

    for arr, label in (("a", "(a) not configured, real command shape, stale files on disk"),
                       ("b", "(b) configured, stale files on disk"),
                       ("c", "(c) configured, a standup doc inside the sales folder")):
        print(f"\nstep 1 — {label}")
        live = await replay_step1(arr, False)
        test = await replay_step1(arr, True)
        check("the gate said yes (mentioned)", [("responded" in m and "mentioned" in m) for m in live["gate"]],
              [True])
        check("routed to the engine", live["route"], ["engine"])
        # NFT2-1063: was exactly ["show_todos"] and "tools=1 (today)". The today group offers the to-do sheet AND the
        # objectives answer; cadence_preview (the rules and the schedule) is still not offered.
        check("the tools OFFERED are exactly show_todos and todays_objectives", live["tools_offered"],
              ["show_todos", "todays_objectives"])
        check("the routing line says today / 2 tools", any("tools=2 (today)" in m for m in live["engine"]), True)
        check("two model calls (one tool round, one answer)", live["model_calls"], 2)
        check("exactly one reply, not empty", (len(live["reply"]), bool(live["reply"] and live["reply"][0])),
              (1, True))
        check("no AM/PM sync or PM-call text in any tool result", leaks(json.dumps(live["tool_results"])), [])
        check("no AM/PM sync or PM-call text in the reply", leaks(" ".join(live["reply"])), [])
        if arr == "a":
            check("notes not configured: the allowed to-do is hidden too (no allowed notes exist)",
                  "ZZ-ALLOWED-TODO" in " ".join(live["reply"]), False)
        else:
            check("the allowed to-do is in the reply", "ZZ-ALLOWED-TODO" in " ".join(live["reply"]), True)
        check("live replies carry no [TEST tag", live["tagged"], [False])
        for k in ("gate", "route", "engine", "tools_offered", "model_calls", "reply", "tool_results"):
            check(f"test mode == live: {k}", test[k], live[k])

        print(f"  full tool set, the model calls everything — arrangement ({arr})")
        full_live = await full_set_run(arr, False)
        full_test = await full_set_run(arr, True)
        check("the four note tools were all offered and called",
              {"list_meeting_notes", "read_meeting_note", "meeting_facts", "todo_candidates"}
              <= set(full_live["offered"]), True)
        check("no sync / PM-call text in any tool result", leaks(json.dumps(full_live["results"])), [])
        check("no sync / PM-call text in the reply", leaks(full_live["reply"]), [])
        if arr == "a":
            says = [json.loads(r).get("say") for n, r in full_live["results"]
                    if n in ("list_meeting_notes", "read_meeting_note", "meeting_facts", "todo_candidates")]
            check("every note tool says it is not connected, word for word",
                  set(says), {"Meeting notes aren't connected to me yet."})
        check("test mode == live (offered tools, calls, reply, results)", full_test, full_live)

    import bot as botmodule
    shipped = getattr(botmodule, "POC_ROW_ADD_WRITE_WIRED", True)
    # AS SHIPPED, then a rehearsal of Q1=yes (the builder holds the row write behind
    # bot.POC_ROW_ADD_WRITE_WIRED until the human answers; the flip is in this process only).
    runs = [(shipped, "AS SHIPPED (POC_ROW_ADD_WRITE_WIRED=%s)" % shipped)]
    if not shipped:
        runs.append((True, "REHEARSAL of Q1=yes (switch flipped in this process only)"))
    for wired, label in runs:
        botmodule.POC_ROW_ADD_WRITE_WIRED = wired
        print("\nsteps 5, 8, 9 - NFT2-1065 - " + label)
        print("  (reconstructed wording for 5 and 8; step 9 verbatim from the fixture)")
        await profile_section(wired)
    botmodule.POC_ROW_ADD_WRITE_WIRED = shipped

    print("\nRULE 13 - 7 Oct 2026 - next steps for connected contacts (7 Oct sheet layout, a post, then replies)")
    await rule13_section()

    print("\nNFT2-1063 - replies and on-demand requests: 6 Oct steps 2, 3, 4, 6, 7 and the 7 Oct exchange "
          "(live, then test mode)")
    await replay_1063.section(check, FIX)

    config.SALES_TEST_MODE = False


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)

"""THE WORLD FOR NFT2-1063: fake Discord (with replies, reactions and fetch_message), a temp database, a stand-in
sheet that COUNTS reads, a model that records what it was asked, and every setting pinned to .env.example.

Written from docs/plans/NFT2-1063.md, not from the builder's code. Shared by tests/test_replies.py,
verify_replies.py and tests/replay_1063.py (the section verify_replay_oct6.py runs).

    sys.path.insert(0, <repo>/tests); import replies_world as rw
    with rw.World(test_mode=False) as w:
        ...

OFFLINE. No network, no real Sheets/Drive (the offline guard is installed by the caller: pytest's conftest, or
`offline_guard.install_script()` at the top of a script), no model (`ScriptModel` / `EchoModel` answer), a throwaway
SQLite file, a throwaway STATE_DIR. Made-up people and companies only. Never reads .env: every value the tests
depend on is PINNED here to its .env.example default (the laptop's real values differ: SIMULATION_PREFIX is
`[TEST-live]` there, the approvers are four other ids, COS_FOLLOWUP_CHECK_INTERVAL_MINUTES is 150).

WHAT A "WORLD" GIVES A SCENARIO
    w.say(text, who="approver", reply_to=<msg>, mention=False, cached=True)   a human message through the REAL
        `on_message` (gate -> `_handle_query` -> reply context -> acknowledge / vote / offer / engine)
    w.bot_says(text)                         a bot message in the channel with nothing behind it (an answer, a quiet line)
    w.send_r3_post(day)                      the REAL R3 post, through the REAL planner and `_send_drip_message`
    w.counts()                               model calls, router calls, extractor calls, sheet reads, replies, reactions,
                                             votes, reminders, drip rows, proposals
"""
import asyncio
import contextlib
import copy
import itertools
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time as _time
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import discord  # noqa: E402

import config  # noqa: E402

BOT_ID = 999
CHANNEL = 4242
OTHER_CHANNEL = 4343
APPROVER, APPROVER2, MEMBER = 111, 222, 333        # Vaishnavi, Sid, Kushal (made-up ids)
NAMES = {APPROVER: "Vaishnavi", APPROVER2: "Sid", MEMBER: "Kushal"}
PEOPLE = {"approver": APPROVER, "approver2": APPROVER2, "member": MEMBER}

WED = date(2026, 10, 7)            # the day of the 7 Oct exchange (a Wednesday): R3's day, "Monday" is Mon 12 Oct
OBJ_DAY = date(2026, 9, 30)        # a (past) Wednesday with R3 and R13 due: past, so research says nothing about the future
MON_AFTER_WED = date(2026, 10, 12)

# Every setting the tests lean on, at its .env.example default (never the laptop's real value).
PINS = {
    "SIMULATION_PREFIX": "[TEST]",
    "SALES_CHANNEL_IDS": [CHANNEL], "SALES_CHANNEL_ID_SET": {CHANNEL}, "SALES_TEST_CHANNEL_ID": CHANNEL,
    "SALES_DIGEST_CHANNEL_ID": CHANNEL, "WEEKLY_DIGEST_CHANNEL_ID": CHANNEL, "SALES_ASK_CHANNEL_ID": CHANNEL,
    "TEAM_ROSTER_IDS": [APPROVER, APPROVER2, MEMBER], "SALES_APPROVER_IDS": [APPROVER, APPROVER2],
    "SALES_FINAL_SAY_ID": 0,
    "ROSTER_DISPLAY_NAMES": {str(k): v for k, v in NAMES.items()},
    "INTERIM_ENABLED": False, "INTERIM_AFTER_SECONDS": 10.0, "INTERIM_AFTER_WEB_SECONDS": 6.0,
    "QUERY_MEMORY_TURNS": 5, "QUERY_MEMORY_TTL_MINUTES": 10,
    "PROPOSAL_BARE_YES_MINUTES": 30,
    "PROPOSAL_NUDGE_AFTER_DAYS": 1, "PROPOSAL_DROP_AFTER_DAYS": 1,
    "DAILY_MESSAGE_CAP": 5, "SALES_DRIP_START": "14:00", "SALES_DRIP_END": "20:00",
    "MESSAGE_GAP_MINUTES": 120, "MESSAGE_JITTER_MINUTES": 0, "MESSAGE_GAP_MIN_MINUTES": 120,
    "DRIP_REASK_DAYS": 2, "DRIP_WEEKDAYS_ONLY": True, "SUNDAY_RULE_IDS": ["R4"],
    "MEETING_DAYOF_TIME": "10:00", "NEXT_STEP_TIME": "15:00", "TEST_MORNING_TIME": "10:00",
    "TEST_AFTERNOON_TIME": "14:00", "NEXT_ACTION_ENABLED": True, "NEXT_ACTION_WEEKEND_SHIFT": True,
    "WEEKLY_FUNNEL_ENABLED": False, "SALES_DMS_ENABLED": False, "DRIP_LLM_COMPOSE": False,
    "TEST_POST_GAP_SECONDS": 0, "SIMULATION_FAST_GAP_SECONDS": 0, "SIMULATION_REAL_MENTIONS": False,
    "REMINDER_DEFAULT_TIME": "14:00", "SHEET_WRITE_UNDO_HOURS": 24,
    "EMAIL_WRITE_ALLOWED": True, "SHEET_WRITES_ENABLED": True, "EVENTS_WINDOW_DAYS": 14,
    "EVENTS_REMIND_AGAIN_WEEKDAY": 0,                 # mon
    "WEB_SEARCH_ENABLED": True, "SEARCH_BACKEND": "searxng", "SEARCH_FALLBACKS": [],
    "TODO_SHEET_ENABLED": False,
}
_ABSENT = object()

_IDS = itertools.count(70001)
LOG = logging.getLogger("replies_world")


class Tap(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass


TAP = Tap()


def strip_tag(text):
    """A leading [TEST...] tag is the ONLY thing test mode adds."""
    return re.sub(r"^\[TEST[^\]]*\]\s*", "", text or "")


# -- fake Discord ----------------------------------------------------------------------------------------------

class Typing:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return None


def _author(uid=None, *, bot=False):
    if bot:
        return SimpleNamespace(id=BOT_ID, bot=True, name="saley", display_name="Saley", global_name="Saley")
    name = NAMES.get(uid, f"user{uid}")
    return SimpleNamespace(id=uid, bot=False, name=name.lower(), display_name=name, global_name=name)


def _ref(parent, *, cached=True):
    return SimpleNamespace(message_id=parent.id, resolved=None, cached_message=parent if cached else None,
                           channel_id=parent.channel.id)


class Chan:
    """One channel: remembers every message in it by id, so `fetch_message` works and a reply chain can be walked."""

    guild = SimpleNamespace(id=1)

    def __init__(self, world, cid=CHANNEL, name="sales"):
        self.world, self.id, self.name = world, cid, name
        self.msgs = {}
        self.fetches = 0

    def typing(self):
        return Typing()

    def history(self, **_kw):
        """An EMPTY history. The world's messages reach the bot through `on_message`; nothing here stands in for
        scrolling back through the channel, so a history read finds nothing rather than failing. A scenario that
        needs channel history puts its own stand-in on `query.channel_recent_activity`."""
        async def nothing():
            return
            yield
        return nothing()

    async def send(self, text, **_kw):
        return BotMsg(self.world, self, text)

    async def fetch_message(self, mid):
        m = self.msgs.get(int(mid))
        if m is None or getattr(m, "deleted", False):
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        self.fetches += 1
        return m


class _Msg:
    guild = Chan.guild
    mention_everyone = False

    def __init__(self, world, chan, text, author, reference=None):
        self.id = next(_IDS)
        self.world, self.channel, self.content, self.author = world, chan, text, author
        self.reference = reference
        self.mentions = []
        self.deleted = False
        self.reactions = []
        self.created_at = datetime.now()
        self.t = _time.monotonic()
        self.jump_url = f"https://discord.test/{chan.id}/{self.id}"
        chan.msgs[self.id] = self

    async def add_reaction(self, emoji):
        self.reactions.append(str(emoji))
        self.world.reactions.append((self.content, str(emoji)))

    def __repr__(self):
        return f"<{type(self).__name__} {self.id} {self.content[:30]!r}>"


class BotMsg(_Msg):
    """Something Saley posted (an answer, an interim line, a drip post, a proposal question)."""

    def __init__(self, world, chan, text, reference=None):
        super().__init__(world, chan, text, _author(bot=True), reference)
        world.posted.append(self)


class HMsg(_Msg):
    """A person's message. `reply()` is how the bot answers it (guardrails.send calls it)."""

    def __init__(self, world, chan, text, uid, reference=None):
        super().__init__(world, chan, text, _author(uid), reference)

    async def reply(self, text, mention_author=False):
        return BotMsg(self.world, self.channel, text, reference=_ref(self))


# -- the model ------------------------------------------------------------------------------------------------

def _usage():
    return SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=0))


class ScriptModel:
    """Says exactly what the script says; records every request so a test can read what the engine was given."""

    def __init__(self, answer="Here is what I have."):
        self.answer = answer
        self.script = []              # [("say", text) | ("use", [(name, input), ...]) | ("sleep", seconds, step)]
        self.calls = 0
        self.requests = []
        self.offered = []
        self.results = []
        self._names = {}
        self.delay = 0.0

    def create(self, **kw):
        self.calls += 1
        self.requests.append(kw)
        self.offered.append([t.get("name") for t in (kw.get("tools") or []) if t.get("name")])
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
        step = self.script.pop(0) if self.script else ("say", self.answer)
        delay = self.delay
        if step[0] == "slow":                       # ("slow", seconds, real_step)
            delay, step = step[1], step[2]
        if delay:
            _time.sleep(delay)                      # the SDK is sync and runs on a thread
        if step[0] == "raise":
            raise RuntimeError("the model call failed")
        if step[0] == "say":
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=step[1], citations=[])],
                                   stop_reason="end_turn", usage=_usage())
        blocks = []
        for i, (name, inp) in enumerate(step[1]):
            tid = f"tu_{self.calls}_{i}"
            self._names[tid] = name
            blocks.append(SimpleNamespace(type="tool_use", id=tid, name=name, input=inp))
        return SimpleNamespace(content=blocks, stop_reason="tool_use", usage=_usage())

    # what the engine was handed ------------------------------------------------------------
    def request_text(self, i=0):
        """Every user-visible string of request i, joined (the question, the history, the context block)."""
        if i >= len(self.requests):
            return ""
        out = []
        for m in self.requests[i].get("messages") or []:
            c = m.get("content")
            if isinstance(c, str):
                out.append(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        out.append(str(b.get("text") or ""))
        return "\n".join(out)


class EchoModel(ScriptModel):
    """Calls EVERY tool it is offered once, then echoes the tool results (the replay harness's model)."""

    def create(self, **kw):
        if self.calls == 0:
            self.script = [("use", [(t["name"], {}) for t in (kw.get("tools") or []) if t.get("name")])]
        elif self.calls == 1:
            msgs = kw.get("messages") or []
            seen = []
            for m in msgs:
                if m.get("role") == "user" and isinstance(m.get("content"), list):
                    for b in m["content"]:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            c = b.get("content")
                            seen.append(c if isinstance(c, str) else json.dumps(c, default=str))
            self.script = [("say", "ECHO " + " | ".join(seen))]
        return super().create(**kw)


class FakeLLM:
    """The router, the social voice and the sheet-update extractor — each counted."""

    def __init__(self):
        self.parse_calls = self.social_calls = self.extract_calls = self.other_calls = 0
        self.extract_result = {"intent": "none"}
        self.extract_texts = []

    async def parse_query(self, *, text, requester, history):
        self.parse_calls += 1
        return {"message_kind": "question", "is_query": True}

    async def social_reply(self, *, kind, text, requester):
        self.social_calls += 1
        return "Hi."

    async def extract_sheet_update(self, **kw):
        self.extract_calls += 1
        self.extract_texts.append(str(kw.get("text") or ""))
        return dict(self.extract_result)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        async def anything(*a, **k):
            self.other_calls += 1
            return None
        return anything


def stub_tool(name):
    async def handler(_inp):
        return {"stub": name}
    return {"schema": {"name": name, "description": "stub", "input_schema": {"type": "object", "properties": {}}},
            "handler": handler}


# -- the sheet: the REAL parser over grids, with a read counter ------------------------------------------------

POC_HEADERS = None            # filled lazily from canned_sheet (the 7 Oct header row)
EVENT_HEADERS = ["Event", "Date", "Last day for registration", "Registered?", "Link", "Location"]
DELIV_HEADERS = ["Action Item", "Priority", "Status", "Tentative Deadline", "Functional Dependency", "Remarks",
                 "Link/Destination"]
MON = date(2026, 9, 28)            # a (past) Monday: deliverables, closure support, R13 and R1 are due


def deliverables_for(day: date):
    """A Monday's deliverables tab (made-up items): two P1s open, one P2, one done."""
    def bare(d):
        return f"{d.day}-{d.strftime('%b')}"
    return [["Pricing page copy", "P1", "Not started", bare(day + timedelta(days=4)), "Marketing", "",
             "https://docs.example/pricing"],
            ["Security questionnaire", "P1", "", bare(day + timedelta(days=2)), "Legal", "", ""],
            ["Case study: Hinglish STT", "P2", "", bare(day + timedelta(days=1)), "Sales", "", ""],
            ["NDA", "P1", "Done", bare(day + timedelta(days=1)), "Legal", "", ""]]


def _pocs_headers():
    import canned_sheet
    return list(canned_sheet.POCS_HEADERS)


def events_for(day: date):
    """The Wednesday's events tab: a registered event in 10 days, an unregistered one in 6 (register by day+2)."""
    def on(n):
        return (day + timedelta(days=n)).strftime("%d-%m-%Y")
    return [["Voice AI Forum", on(10), "", "Yes", "https://voiceaiforum.example", "Bengaluru"],
            ["Data Summit", on(6), on(2), "", "https://datasummit.example/register", "Online"],
            ["Old Expo", on(-3), "", "", "", "Delhi"]]


class Sheet:
    """Stands in for gtm_sheet.SHEETS: parsed fresh by the real parser on every read, every read counted."""

    def __init__(self):
        self.grids = {"Outreach PoCs": [_pocs_headers()], "AI Events & Summits": [list(EVENT_HEADERS)],
                      "Deliverables Checklist": [list(DELIV_HEADERS)]}
        self.reads = 0
        self.writes = []
        self._saved = {}

    def set(self, *, pocs=None, events=None, deliverables=None):
        if deliverables is not None:
            self.grids["Deliverables Checklist"] = [list(DELIV_HEADERS)] + [list(r) for r in deliverables]
        if pocs is not None:
            self.grids["Outreach PoCs"] = [_pocs_headers()] + [list(r) for r in pocs]
        if events is not None:
            self.grids["AI Events & Summits"] = [list(EVENT_HEADERS)] + [list(r) for r in events]

    def read(self, which=None, force=False, **_k):
        import gtm_sheet
        self.reads += 1
        out = {}
        for title, grid in self.grids.items():
            tab = gtm_sheet.SHEETS._parse_values(title, copy.deepcopy(grid), read_at=_time.time())
            if tab is not None:
                out[tab.kind] = tab
        return out

    def install(self):
        import gtm_sheet
        S = gtm_sheet.SHEETS
        names = ("read", "cadence_tab", "tab", "_open", "_refuse_if_read_only", "staleness_note", "write_cells",
                 "write_email", "append_row", "write_cells_on")
        self._saved = {k: S.__dict__.get(k, _ABSENT) for k in names}
        sheet = self

        class Ws:
            title = "x"

            def update(self, values=None, range_name="", value_input_option=""):
                sheet.writes.append((range_name, values[0][0] if values else None))

            def batch_update(self, *a, **k):
                sheet.writes.append(("batch_update", a))

        def cadence_tab(*a, **k):
            return (self.read().get(gtm_sheet.POCS), "fixture")

        def tab(kind, which=None):
            return self.read().get(kind)

        def write_cells(**kw):
            sheet.writes.append(("write_cells", kw.get("row"), dict(kw.get("values") or {})))
            return {"ok": True, "written": [], "error": ""}

        def write_email(**kw):
            sheet.writes.append(("write_email", kw.get("row"), kw.get("email")))
            return {"ok": True, "written": [], "error": ""}

        def append_row(*a, **kw):
            sheet.writes.append(("append_row", a[1:] if a else None))
            return {"ok": True, "sheet_row": 100, "written": [], "dry_run": False, "error": ""}

        S.read = self.read
        S.cadence_tab = cadence_tab
        S.tab = tab
        S._open = lambda *a, **k: SimpleNamespace(worksheet=lambda t: Ws(),
                                                  batch_update=lambda *a, **k: sheet.writes.append(("ss", a)))
        S._refuse_if_read_only = lambda *a, **k: ""
        S.staleness_note = lambda *a, **k: ""
        S.write_cells = write_cells
        S.write_email = write_email
        S.append_row = append_row
        S.write_cells_on = lambda *a, **k: write_cells(row=k.get("row"), values=k.get("values"))

    def uninstall(self):
        import gtm_sheet
        S = gtm_sheet.SHEETS
        for k, v in self._saved.items():
            if v is _ABSENT:
                S.__dict__.pop(k, None)
            else:
                setattr(S, k, v)
        self._saved = {}


# -- the world -------------------------------------------------------------------------------------------------

class World:
    """One bot, one channel, one throwaway database. Use as a context manager."""

    def __init__(self, test_mode=False, *, pretend=None, rules=None, model=None, pins=None):
        self.test_mode = bool(test_mode)
        self.pretend = pretend            # (date, hh, mm) or None
        self.rules = set(rules or ())     # restrict the real queue to these rule ids (like verify_s3)
        self.model = model or ScriptModel()
        self.extra_pins = dict(pins or {})
        self.posted = []                  # every BotMsg, in order
        self.reactions = []               # [(text of the message reacted to, emoji)]
        self.sheet = Sheet()
        self._undo = []                   # callables run on exit, newest first
        self.chan = None
        self.other = None

    # ---- lifecycle ----
    def __enter__(self):
        import clock
        import leave
        from bot import SalesBot
        from db import DB

        self.tmp = tempfile.mkdtemp(prefix="saley-1063-")
        self._saved_cfg = {}
        pins = dict(PINS, **self.extra_pins)
        pins.update({"STATE_DIR": os.path.join(self.tmp, "state"), "DB_PATH": os.path.join(self.tmp, "w_test.db"),
                     "NOTES_DIR": os.path.join(self.tmp, "notes"), "SALES_TEST_MODE": self.test_mode})
        for k, v in pins.items():
            self._saved_cfg[k] = getattr(config, k, _ABSENT)
            setattr(config, k, v)
        self._saved_env = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = pins["STATE_DIR"]
        os.makedirs(pins["STATE_DIR"], exist_ok=True)
        os.makedirs(pins["NOTES_DIR"], exist_ok=True)

        self._saved_user = SalesBot.__dict__.get("user", _ABSENT)
        SalesBot.user = property(lambda self_: SimpleNamespace(id=BOT_ID))
        self._saved_leave = leave.who_is_away

        async def nobody_away(*_a, **_k):
            return {}
        leave.who_is_away = nobody_away

        clock.forget()
        # THE WORDING OF A POST VARIES BY DESIGN (tone.pick_index); seeded, so two runs of one case compare equal
        import random
        import tone
        import voice
        self._saved_rng = tone.RNG
        tone.RNG = random.Random(20261007)
        with contextlib.suppress(Exception):
            voice._last_choice.clear()
        if self.pretend:
            day, hh, mm = self.pretend
            was = config.SALES_TEST_MODE
            config.SALES_TEST_MODE = True            # only to be allowed to set the day
            clock.set_day(day, by="replies_world")
            config.SALES_TEST_MODE = was
            clock.set_time_of_day(time(hh, mm), by="replies_world")

        self.sheet.install()
        logging.getLogger().addHandler(TAP)
        logging.getLogger().setLevel(logging.INFO)
        TAP.lines.clear()

        self.chan = Chan(self, CHANNEL)
        self.other = Chan(self, OTHER_CHANNEL, "other")
        self.llm = FakeLLM()
        self.bot = SalesBot()
        self.bot.db = DB(pins["DB_PATH"])
        self.bot.llm = self.llm
        self.bot.query_engine._client = SimpleNamespace(messages=self.model)
        self._stub_bot(self.bot)
        return self

    def __exit__(self, *exc):
        import clock
        import leave
        from bot import SalesBot

        while self._undo:
            with contextlib.suppress(Exception):
                self._undo.pop()()
        logging.getLogger().removeHandler(TAP)
        import tone
        tone.RNG = self._saved_rng
        self.sheet.uninstall()
        leave.who_is_away = self._saved_leave
        if self._saved_user is _ABSENT:
            with contextlib.suppress(AttributeError):
                del SalesBot.user
        else:
            SalesBot.user = self._saved_user
        clock.forget()
        for k, v in self._saved_cfg.items():
            if v is _ABSENT:
                with contextlib.suppress(AttributeError):
                    delattr(config, k)
            else:
                setattr(config, k, v)
        if self._saved_env is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self._saved_env
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    def _stub_bot(self, bot):
        """The tools the engine is offered, the web, the sweeps: stand-ins. The reply path is NOT stubbed."""
        import toolsets
        skip = {"notes", "todos", "today"}
        others = sorted({n for g, names in toolsets.GROUPS.items() if g not in skip for n in names}
                        - {"web_search", "fetch_page", "propose_poc_add", "todays_news", "todays_objectives"})
        bot._discord_tools = lambda m: [stub_tool(n) for n in others]
        bot._sheet_tools = lambda m: []
        bot._mapping_tools = lambda: []
        bot._strategy_tools = lambda: []
        bot._people_tools = lambda sink=None: []
        bot._notes_tools = lambda t: []
        bot._todo_tools = lambda: [stub_tool("show_todos")]
        bot._news_tools = lambda: [stub_tool("todays_news")]

        async def no_web(*a, **k):
            return [], "", ""
        bot._websearch_tools = no_web

        async def no_companies():
            return []
        bot._tracker_company_names = no_companies

        async def nothing(*_a, **_k):
            return None

        async def no_new_events(rows, *, today, marker):
            return []

        async def no_missing_deadlines(rows, *, today, marker):
            return [], ""
        bot._convert_if_already_done = nothing
        bot._sweep_proposals = nothing
        bot._maybe_breaking_news = nothing
        bot._maybe_poll_feeds = nothing
        bot._discover_events = no_new_events
        bot._backfill_deadlines = no_missing_deadlines
        bot._split_active = lambda rows, why: (list(rows), [])
        real_queue = bot._run_next_actions
        world = self

        async def the_rules_under_test(**kw):
            queue = await real_queue(**kw)
            if queue is not None and world.rules:
                queue["actions"] = [a for a in queue["actions"] if str(a.get("rule_id") or "") in world.rules]
            return queue
        bot._run_next_actions = the_rules_under_test

    # ---- the conversation ----
    def say(self, text, who="approver", *, reply_to=None, mention=False, cached=True, bot_in_mentions=False,
            channel=None):
        """A person types `text`. Returns an awaitable resolving to the HMsg once the bot has handled it."""
        return self._say(text, who, reply_to, mention, cached, bot_in_mentions, channel)

    async def _say(self, text, who, reply_to, mention, cached, bot_in_mentions, channel):
        chan = channel or self.chan
        content = (f"<@{BOT_ID}> " if mention else "") + text
        m = HMsg(self, chan, content, PEOPLE.get(who, who),
                 reference=_ref(reply_to, cached=cached) if reply_to is not None else None)
        if mention or bot_in_mentions:
            m.mentions = [SimpleNamespace(id=BOT_ID)]
        m.world_text = text
        await self.bot.on_message(m)
        return m

    def bot_says(self, text, *, channel=None, reference=None):
        """A bot message with nothing behind it: an answer, a quiet line, an interim line."""
        return BotMsg(self, channel or self.chan, text, reference=reference)

    def person(self, name):
        return PEOPLE[name]

    # ---- observations ----
    def replies_after(self, n):
        return [strip_tag(m.content) for m in self.posted[n:]]

    @property
    def n_posted(self):
        return len(self.posted)

    def votes(self):
        with self.bot.db.conn() as c:
            return [tuple(r) for r in c.execute(
                "SELECT proposal_key, voter_id, vote FROM proposal_votes ORDER BY rowid").fetchall()]

    def proposals(self):
        with self.bot.db.conn() as c:
            return {r[0]: r[1] for r in c.execute("SELECT proposal_key, status FROM write_proposals").fetchall()}

    def reminders(self):
        with self.bot.db.conn() as c:
            return [tuple(r) for r in c.execute(
                "SELECT due_date, due_time, status FROM scheduled_reminders ORDER BY id").fetchall()]

    def drip_rows(self):
        with self.bot.db.conn() as c:
            return [tuple(r) for r in c.execute(
                "SELECT on_date, slot, action_type FROM drip_sends ORDER BY on_date, slot").fetchall()]

    def audit(self):
        """Every record in this world's audit log (state/audit.jsonl in the throwaway STATE_DIR)."""
        path = os.path.join(config.STATE_DIR, "audit.jsonl")
        if not os.path.exists(path):
            return []
        out = []
        for line in open(path, encoding="utf-8").read().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def routes(self):
        return [r["route"] for r in self.bot.db.reply_latency_since("2000-01-01")]

    def counts(self):
        return {"model": self.model.calls, "router": self.llm.parse_calls, "extractor": self.llm.extract_calls,
                "sheet_reads": self.sheet.reads, "replies": len(self.posted), "reactions": len(self.reactions),
                "votes": len(self.votes()), "reminders": len(self.reminders()), "drip": len(self.drip_rows())}

    def snapshot(self):
        """Everything a scenario compares between live and test mode, with ids and tags masked."""
        return {"replies": [strip_tag(m.content) for m in self.posted],
                "tagged": [m.content.startswith("[TEST") for m in self.posted],
                "reactions": list(self.reactions), "votes": [(k.split(":")[0], v, y) for k, v, y in self.votes()],
                "reminders": self.reminders(), "counts": self.counts(),
                "proposals": sorted(self.proposals().values()), "routes": self.routes(),
                "offered": [list(o) for o in self.model.offered]}

    def pretend_day(self, day, hh, mm):
        import clock
        was = config.SALES_TEST_MODE
        config.SALES_TEST_MODE = True            # only to be allowed to set the day
        clock.set_day(day, by="replies_world")
        config.SALES_TEST_MODE = was
        clock.set_time_of_day(time(hh, mm), by="replies_world")

    def real_clock(self):
        """Back to the real date. `clock.forget()` alone only drops the cache: the pretend day is stored in the DB and
        comes straight back, which looked fine ONLY on the day it was pretending to be (7 Oct); `back_to_today` clears it."""
        import clock
        config_was = config.SALES_TEST_MODE
        clock.back_to_today(by="replies_world")
        config.SALES_TEST_MODE = config_was

    # ---- the real R3 post ----
    def events_sheet(self, day=WED):
        self.sheet.set(events=events_for(day))

    async def send_r3_post(self, day=WED, *, set_sheet=True, rules=("R3",)):
        """The REAL planner and the REAL `_send_drip_message` for the day's R3 post. Returns (BotMsg, message dict)."""
        import deadlines as dl
        import nextaction
        if set_sheet:
            self.events_sheet(day)
        self.rules = set(rules)
        planned = await self.bot._plan_drip(today=day, already=[])
        msg = next((m for m in (planned or {}).get("messages") or [] if m.get("type") == nextaction.R_EVENTS), None)
        assert msg is not None, "the planner made no R3 post for the fixture events"
        before = len(self.posted)
        await self.bot._send_drip_message(self.chan, msg, marker=dl.iso(day), channel_id=self.chan.id)
        assert len(self.posted) > before, "the R3 post was not sent"
        return self.posted[before], msg

    # ---- proposals, opened directly where the point is the reply, not the opener ----
    def open_proposal(self, *, kind, message_id, key=None, channel_id=CHANNEL, age_minutes=1, payload=None,
                      company="Acme AI", poc="Ada Lovelace", sheet_row=2, trigger="R3"):
        import deadlines as dl
        key = key or f"{kind}:{next(_IDS)}"
        created = (dl.now_ist() - timedelta(minutes=age_minutes)).isoformat(timespec="seconds")
        ok = self.bot.db.open_proposal(
            proposal_key=key, kind=kind, tab="Outreach PoCs", sheet_row=sheet_row, row_key="", company=company,
            poc=poc, payload=payload or {}, reply_text="", trigger=trigger, proposed_text=f"the {kind} offer",
            requested_by="R3", channel_id=channel_id, message_id=str(message_id), created_at=created)
        assert ok
        return key

    def open_remind(self, message_id, *, age_minutes=1, channel_id=CHANNEL, on="2026-10-12"):
        return self.open_proposal(
            kind="events_remind", message_id=message_id, age_minutes=age_minutes, channel_id=channel_id,
            payload={"on": on, "word": "Monday", "time": "14:00",
                     "lines": ["You're registered for Voice AI Forum on Sat 17 Oct"]},
            company="", poc="", sheet_row=0)


def enable_web(w):
    """The REAL client-side web tools over a stand-in search backend that answers from a table and counts.
    Returns the list every query is appended to."""
    import search_backend
    import usage
    saved = (search_backend.search_detail, search_backend.news_detail, search_backend.available, usage.over_budget)
    queries = []

    def search_detail(query, *, n=10, news=False, site=None, days=None, rule="search"):
        queries.append(query)
        return {"results": [{"title": "Underdog AI raises", "url": "https://news.example/underdog",
                             "snippet": "Underdog AI, HQ in Austin"}], "cached": False, "backend": "fake",
                "requests": 1, "error": ""}

    search_backend.search_detail = search_detail
    search_backend.news_detail = lambda q, **k: {"results": [], "cached": False, "backend": "g", "requests": 0,
                                                "error": ""}
    search_backend.available = lambda: (True, "")
    usage.over_budget = lambda: False

    def restore():
        search_backend.search_detail, search_backend.news_detail, search_backend.available, usage.over_budget = saved
    w._undo.append(restore)

    async def left():
        return (100, 0, 150)
    w.bot._search_left = left
    w.bot._websearch_tools = type(w.bot)._websearch_tools.__get__(w.bot)
    return queries


NEWS_HEADLINES = ["Frontier lab ships a new eval suite", "Voice startup closes a Series A",
                  "Regulator publishes AI data rules", "Open model tops the speech benchmark",
                  "Chip maker unveils an inference part"]


def seed_news(w, headlines=NEWS_HEADLINES, *, at=None, times=None):
    """A feed store with `headlines` collected in the last hour, all already scored: the REAL `todays_news` reads it,
    makes no model call, and needs no network. The real `_news_tools` is restored on the bot."""
    import feeds
    import news
    import deadlines as dl
    now = dl.real_now_ist()
    items, scores = [], []
    for n, title in enumerate(headlines):
        url = "https://example.org/" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        when = (times[n] if times else (at + timedelta(minutes=20 * n)) if at is not None
                else now - timedelta(minutes=10 + 5 * n))
        it = {"url": url, "url_key": news.url_key(url), "headline_key": news.headline_key(title), "title": title,
              "summary": "", "source": "TechCrunch", "published_at": feeds.utc_iso(when), "topic_hint": "",
              "seen_at": feeds.utc_iso(when), "kind": feeds.KIND_INDUSTRY, "sheet_ref": ""}
        items.append(it)
        scores.append({"url_key": it["url_key"], "importance": 4, "topic": "evals", "what": "what happened"})
    w.bot.db.news_feed_add(items)
    w.bot.db.news_feed_set_scores(scores, scored_at=feeds.utc_iso(now))
    if "_news_tools" in w.bot.__dict__:
        del w.bot.__dict__["_news_tools"]         # the real one, over the seeded store
    return headlines


def fresh_snapshot_equal(a, b):
    """live vs test: identical apart from the tag."""
    a = dict(a)
    b = dict(b)
    a.pop("tagged", None)
    b.pop("tagged", None)
    return a == b


def run(coro):
    return asyncio.run(coro)

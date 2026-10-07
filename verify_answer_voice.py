"""NFT2-1064 — ANSWERS THAT READ LIKE A TEAMMATE, END TO END. Offline.

    python verify_answer_voice.py

(Not verify_tone.py: that one is about PROACTIVE tone and makes real model calls
unless run with --offline. This script never touches the model, the network, the
sheet, Drive or Discord.)

Written from docs/plans/NFT2-1064.md (2.4, 2.5, 5.1, 5.2, 7), not from the code. The
recorded model outputs are tests/fixtures/tone_outputs.json: hand-written ("synthetic"),
none from a live model until the human runs tools/tone_samples.py --live-model --record.

  G    the guard's tables (strips, never-strips)                       V7, V8
  F    every recorded output: no echo, no banned opener, a budget      5.2
  P    the prompt: what is gone, what stays, the compact citation      V1, V2
  W    the REAL on_message path, a scripted model, a temp DB:          V9, V10, V11
         the guard strips, logs, makes no extra call, and the cleaned
         text is what memory keeps; ANSWER_GUARD_ENABLED=false sends
         the model's text untouched; protected sentences and one-line
         replies go out byte for byte
  PAR  every W scenario with SALES_TEST_MODE false and true: same route,
       same tools offered, same model calls, same sent text apart from
       the [TEST...] tag, same guard log lines                         V14
  K    the old scripted replies ("Okay.", "ok", "Noted.", ...) pass     V15
       through the guard untouched

Throwaway DB, NOTES_DIR and STATE_DIR. check(name, got, want); ends ALL PASSED or N FAILED.
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from types import SimpleNamespace

for _s in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _s.reconfigure(encoding="utf-8", errors="replace")

TMP = tempfile.mkdtemp(prefix="saley-answer-voice-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")
os.environ["STATE_DIR"] = os.path.join(TMP, "state0")
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests"))

import config  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}
config.INTERIM_ENABLED = False
config.NOTES_DIR = os.path.join(TMP, "notes")
config.STATE_DIR = os.path.join(TMP, "state0")
os.makedirs(config.NOTES_DIR, exist_ok=True)
os.makedirs(config.STATE_DIR, exist_ok=True)

import notes  # noqa: E402
import persona  # noqa: E402
import query_engine  # noqa: E402
import toolsets  # noqa: E402
import voice_cases as vc  # noqa: E402
from bot import SalesBot  # noqa: E402
from db import DB  # noqa: E402

BOT_ID = 999
failures = 0
import offline_guard  # noqa: E402

PROBES = offline_guard.install()   # canned source statuses; any real Sheets/Drive call raises and is counted
_DBN = __import__("itertools").count(1)


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
SalesBot.user = property(lambda self: SimpleNamespace(id=BOT_ID))


def section(title, fn):
    """A section that cannot run (a module not built yet) is one FAIL, not a crash."""
    print(f"\n{title}")
    try:
        r = fn()
        if asyncio.iscoroutine(r):
            asyncio.run(r)
    except Exception as e:                                       # noqa: BLE001
        import traceback

        check(f"{title.split(' - ')[0]} ran to the end", f"{type(e).__name__}: {e}", "no exception")
        traceback.print_exc(limit=3)


# -- G: the guard's tables ---------------------------------------------------------

def sec_guard():
    import replyguard as rg

    check("ECHO_THRESHOLD is the plan's 0.6", rg.ECHO_THRESHOLD, 0.6)
    for i, (q, reply, want, rules) in enumerate(vc.STRIPS):
        out, fired = rg.clean(reply, question=q)
        check(f"strip {i:02d}: {reply[:34]!r}", (out, [f["rule"] for f in fired]), (want, rules))
    check("at least 20 strip cases", len(vc.STRIPS) >= 20)
    for q, reply, protect, why in vc.KEEPS:
        out, fired = rg.clean(reply, question=q, protect=protect)
        check(f"never strips ({why}): {reply[:30]!r}",
              (out, all(f["rule"].endswith(":kept") and f["removed"] == "" for f in fired)), (reply, True))
    for t in vc.NO_RULE_AT_ALL:
        check(f"no rule matches {t!r}: untouched, nothing logged", rg.clean(t, question="is that ok?"), (t, []))
    out, fired = rg.clean("Based on the Sales Bot Discussion of 2 Sep, Acme is on hold.", question="where are we with Acme?")
    check("a refused removal is logged as :kept", fired, [{"rule": "based-on:kept", "removed": ""}])


# -- F: recorded outputs --------------------------------------------------------------

def sec_fixtures():
    import replyguard as rg

    cases = vc.load_cases()
    types = {}
    for c in cases:
        types[c["type"]] = types.get(c["type"], 0) + 1
        got = vc.case_problems(c, rg)
        check(f"{c['id']} ({c['type']}, {c['source']})", got, [])
        if c["source"] == "live":                                   # only after --record
            check(f"{c['id']} records model, time and prompt sha",
                  bool(c.get("model") and c.get("recorded_at") and c.get("prompt_sha")), True)
    need = {"fact": 4, "list": 3, "profile": 2, "news": 2, "summary": 2, "gap": 2, "social": 2, "capability": 1}
    check("coverage by type", {t: types.get(t, 0) >= n for t, n in need.items()}, {t: True for t in need})
    print(f"  ({sum(1 for c in cases if c['source'] == 'synthetic')} synthetic, "
          f"{sum(1 for c in cases if c['source'] == 'live')} live)")
    print("  id                 type        before-fired                after: lines/chars/echo")
    for c in cases:
        if c.get("after") is None:
            continue
        first = rg.first_sentence(c["after"])
        print(f"  {c['id']:<18} {c['type']:<11} {str(c['expect_guard_on_before']):<27} "
              f"{len(vc.lines_of(c['after']))}/{len(c['after'])}/{rg.echo_score(c['question'], first):.2f}")


# -- P: the prompt -----------------------------------------------------------------------

def sec_prompt():
    every = sorted({n for names in toolsets.GROUPS.values() for n in names})
    for label, names in (("every tool", every), ("show_todos", ["show_todos"]), ("web_search", ["web_search"])):
        text = query_engine._engine_text(requester_name="Kushal", today="2026-10-07", tool_names=names)
        for gone in ("LABEL EVERY FACT", "NEVER SKIP A SOURCE SILENTLY", "freshness line", "report what you searched",
                     "numbered or bulleted points"):
            check(f"[{label}] gone: {gone}", gone in text, False)
    text = query_engine._engine_text(requester_name="Kushal", today="2026-10-07", tool_names=every)
    for must in ("NEVER say you lack a tool that is on it", "Do not mention searches, quotas, budgets", "not checked yet",
                 "NEVER INVENT", "SAY WHAT'S MISSING, ONCE", "A READER MUST BE ABLE TO CHECK", "WHEN A NOTES TOOL RETURNS A"):
        check(f"stays: {must}", must in text, True)
    check("OUTPUT is last and after NEWS QUESTIONS",
          text.index("=== OUTPUT ===") > text.index("=== NEWS QUESTIONS") and text.rindex("\n=== ") == text.index("\n=== OUTPUT ==="),
          True)
    check("the mapping rule is still gated on a mapping tool",
          "CITE PERSON + ORG + TIER + CONFIDENCE" in query_engine._engine_text(requester_name="K", today="2026-10-07",
                                                                              tool_names=["who_to_pitch"]), True)
    p = persona.COS_PERSONA
    check("persona: no 'I checked the notes and'", "I checked the notes and" in p, False)
    for must in ("ANSWER FIRST", "LENGTH FOLLOWS THE QUESTION", "no tool you were given this turn can do it", "CITE THE MEETING"):
        check(f"persona has {must}", must in p, True)
    flat = re.sub(r"\s+", " ", p)
    for phrase in ("Here is", "Here's what I found", "Based on", "Great question", "Sure!"):
        check(f"persona names the banned opener {phrase!r}", phrase in flat, True)
    check("the citation rule shows the compact form", "(Sales Bot Discussion, 2 Sep)" in persona.CITATION_RULE
          and "nothing else in them" in persona.CITATION_RULE, True)
    pins = json.load(open(vc.PINS_1065, encoding="utf-8"))
    check("NFT2-1065 'never claim to lack a tool' text: byte for byte", pins["engine_tools_line_follow"] in text, True)
    check("NFT2-1065 coverage / profile section: byte for byte", pins["engine_profile_section"] in text, True)
    check("NFT2-1065 persona 'cannot do' bullet: byte for byte", pins["persona_cannot_do_bullet"] in p, True)


# -- W / PAR / K: the real on_message path ------------------------------------------------

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

    async def send(self, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(id=len(self.sent) + 5000, jump_url="https://discord/x")


class FakeMessage:
    _n = [2000]

    def __init__(self, channel, content, who="Vaishnavi"):
        FakeMessage._n[0] += 1
        self.id = FakeMessage._n[0]
        self.content = content
        self.channel = channel
        self.guild = channel.guild
        self.author = SimpleNamespace(id=7, bot=False, name=who.lower(), display_name=who, global_name=who)
        self.reference = None
        self.mentions = [SimpleNamespace(id=BOT_ID)]
        self.mention_everyone = False

    async def reply(self, text, mention_author=False):
        self.channel.sent.append(text)
        return SimpleNamespace(id=len(self.channel.sent) + 5000, jump_url="https://discord/x")


class FakeLLM:
    """The router and the social / capability voices: scripted text, no model."""

    def __init__(self, kind, social):
        self.kind, self.social, self.calls = kind, social, 0

    async def parse_query(self, *, text, requester, history):
        return {"message_kind": self.kind, "is_query": self.kind == "question"}

    async def social_reply(self, *, kind, text, requester):
        self.calls += 1
        return self.social

    async def capability_reply(self, *, text="", requester=None):
        self.calls += 1
        return self.social


class Model:
    """The engine's client: ONE scripted answer, no tool use, every call counted."""

    def __init__(self, text):
        self.text, self.calls, self.offered = text, 0, []

    def create(self, **kw):
        self.calls += 1
        if not self.offered:
            self.offered = [t["name"] for t in (kw.get("tools") or []) if t.get("name")]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text, citations=[])],
                               stop_reason="end_turn",
                               usage=SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=0)))


def make_bot(kind, model_text, social_text=""):
    bot = SalesBot()
    bot.db = DB(os.path.join(TMP, f"b{next(_DBN)}_test.db"))
    bot.llm = FakeLLM(kind, social_text)
    model = Model(model_text)
    bot.query_engine._client = SimpleNamespace(messages=model)
    bot._websearch_tools = lambda *a, **k: _empty3()
    bot._discord_tools = lambda m: []
    bot._sheet_tools = lambda m: []
    bot._mapping_tools = lambda: []
    bot._strategy_tools = lambda: []
    bot._people_tools = lambda sink=None: []
    bot._news_tools = lambda: []
    bot._notes_tools = lambda t: []
    bot._todo_tools = lambda: []

    async def not_an_update(_m, _t):
        return False

    bot._maybe_apply_sheet_update = not_an_update

    async def no_companies():
        return []

    bot._tracker_company_names = no_companies
    return bot, model


async def _empty3():
    return [], "", ""


def strip_tag(t):
    return re.sub(r"^\[TEST[^\]]*\]\s*", "", t or "")


def norm(line):
    return re.sub(r"msg=\d+", "msg=N", line)


async def run(scn, *, test_mode, guard=True):
    config.SALES_TEST_MODE = test_mode
    config.ANSWER_GUARD_ENABLED = guard
    TAP.lines.clear()
    bot, model = make_bot(scn.get("kind", "question"), scn.get("model", ""), scn.get("social", ""))
    ch = FakeChannel()
    await bot.on_message(FakeMessage(ch, f"<@{BOT_ID}> " + scn["q"]))
    rows = bot.db.reply_latency_since("2000-01-01")
    return {
        "route": [r["route"] for r in rows],
        "offered": list(model.offered),
        "model_calls": model.calls + bot.llm.calls,
        "sent": [strip_tag(m) for m in ch.sent],
        "tagged": [m.startswith("[TEST") for m in ch.sent],
        "guard_log": [norm(m) for m in TAP.lines if m.startswith("[voice] guard")],
        "len_log": [norm(m) for m in TAP.lines if m.startswith("[voice] msg=")],
        "memory": bot.memory.recent(ch.id),
    }


LIST_Q = "who is on the sheet?"
SCENARIOS = {
    "strip-two": dict(q=LIST_Q, model="Sure! Here's what I found:\n• Ada Lovelace — CTO\n• Sam Lee — Head of Research"),
    "based-on": dict(q="where are we with Acme?", model="Based on the tracker, Acme is at DM sent."),
    "clean": dict(q="where are we with Acme?", model="Acme replied on 12 Aug and the demo is booked for Thursday."),
    "gap": dict(q="where are we with Globex?",
                model="Nothing on Globex in the channel in the last two weeks. I can't see the pipeline sheet yet."),
    "citation": dict(q="is Globex on hold?", model="Yes, until their pilot is done (Sales Bot Discussion, 2 Sep)."),
    "one-line": dict(q="where are we with Acme?", model="Sure."),
    "notes-say": dict(q="what meeting notes do you have?", model=notes.SAY_NOT_CONNECTED),
    "notes-empty": dict(q="what meeting notes do you have?", model=notes.SAY_EMPTY.format(folder="Saley – Sales Notes")),
    "notes-unreachable": dict(q="what meeting notes do you have?", model=notes.SAY_UNREACHABLE),
    "offer-then-opener": dict(q="who is Ada Lovelace?",
                              model="I've added Ada Lovelace to Outreach PoCs.\nHere's what I found:\nAda Lovelace is CTO at Acme AI."),
    "social": dict(q="hi", kind="greeting", social="Sure! I can check the pipeline for you."),
    "social-plain": dict(q="hi", kind="greeting", social="Hi Vaishnavi, what do you need?"),
    "capability": dict(q="what can you do?", social="Great question! Here's what I can do:\n- Answer from the sheets"),
}


async def sec_wire():
    import replyguard as rg  # noqa: F401

    s = SCENARIOS["strip-two"]
    live = await run(s, test_mode=False)
    check("V9: routed to the engine", live["route"], ["engine"])
    check("V9: one model call", live["model_calls"], 1)
    check("V9: the sent text starts at the list", [t[:5] for t in live["sent"]], ["• Ada"])
    check("V9: exactly two guard lines, interjection then lead-in",
          [re.search(r"rule=(\S+)", l).group(1) for l in live["guard_log"]], ["interjection", "lead-in"])
    check("V9: the guard line names the message and what was removed",
          all("msg=N" in l and "removed=" in l for l in live["guard_log"]), True)
    check("V9: exactly one [voice] length line, with the numbers",
          (len(live["len_log"]), all(k in live["len_log"][0] for k in ("q_words=", "lines=", "chars=", "guard="))
           if live["len_log"] else False), (1, True))
    check("V9: memory holds the CLEANED text", (len(live["memory"]) == 1 and live["memory"][0]["answer"].startswith("• Ada"),
                                               "Sure" in live["memory"][0]["answer"] if live["memory"] else None), (True, False))

    off = await run(s, test_mode=False, guard=False)
    check("V9: guard off: the model's text goes out untouched", off["sent"], [s["model"]])
    check("V9: guard off: no guard line", off["guard_log"], [])
    check("V6: the guard makes no extra model call", (off["model_calls"], live["model_calls"]), (1, 1))

    b = await run(SCENARIOS["based-on"], test_mode=False)
    check("based-on is stripped", b["sent"], ["Acme is at DM sent."])
    for name in ("clean", "gap", "citation"):
        r = await run(SCENARIOS[name], test_mode=False)
        check(f"V10 {name}: sent byte for byte, nothing logged", (r["sent"], r["guard_log"]),
              ([SCENARIOS[name]["model"]], []))
    r = await run(SCENARIOS["one-line"], test_mode=False)
    check("V10: a one-sentence reply is never emptied", r["sent"], ["Sure."])
    r = await run(SCENARIOS["notes-say"], test_mode=False)
    check("V10: a notes 'say' sentence goes out byte for byte", r["sent"], [notes.SAY_NOT_CONNECTED])

    config.NOTES_SOURCE_FOLDER = "Saley – Sales Notes"
    for name in ("notes-empty", "notes-unreachable"):
        r = await run(SCENARIOS[name], test_mode=False)
        check(f"V10 {name}: the dictated sentence goes out byte for byte, nothing logged",
              (r["sent"], r["guard_log"]), ([SCENARIOS[name]["model"]], []))
    r = await run(SCENARIOS["offer-then-opener"], test_mode=False)
    check("V10: the filters run first, then the guard: neither the stripped offer nor 'Here's what I found:' is sent",
          (len(r["sent"]), "added" in " ".join(r["sent"]).lower(), "here's what i found" in " ".join(r["sent"]).lower(),
           "Ada Lovelace is CTO at Acme AI." in " ".join(r["sent"])), (1, False, False, True))
    r = await run(SCENARIOS["social"], test_mode=False)
    check("V11: social reply loses 'Sure!'", r["sent"], ["I can check the pipeline for you."])
    check("V11: social guard logged", [re.search(r"rule=(\S+)", l).group(1) for l in r["guard_log"]], ["interjection"])
    r = await run(SCENARIOS["social-plain"], test_mode=False)
    check("V11: a plain greeting is untouched", (r["sent"], r["guard_log"]), (["Hi Vaishnavi, what do you need?"], []))
    r = await run(SCENARIOS["capability"], test_mode=False)
    check("V11: capability reply loses its opener", r["sent"][0].startswith("- Answer from the sheets"), True)

    # K: V15 — the scripted replies the other verify scripts use
    for text in ("Okay.", "ok", "Noted.", "Nothing on today."):
        r = await run(dict(q="where are we with Acme?", model=text), test_mode=False)
        check(f"V15: {text!r} passes the guard unchanged", (r["sent"], r["guard_log"]), ([text], []))
    echo = json.dumps({"stub": "show_todos"})
    r = await run(dict(q="where are we with Acme?", model=echo), test_mode=False)
    check("V15: a JSON echo passes unchanged", r["sent"], [echo])


async def sec_parity():
    for name, scn in SCENARIOS.items():
        live, test = await run(scn, test_mode=False), await run(scn, test_mode=True)
        check(f"V14 {name}: live replies carry no [TEST tag", any(live["tagged"]), False)
        for k in ("route", "offered", "model_calls", "sent", "guard_log", "len_log"):
            check(f"V14 {name}: test == live: {k}", test[k], live[k])
        check(f"V14 {name}: the guard never saw a tag (no 'TEST' in a guard line)",
              any("TEST" in l for l in test["guard_log"]), False)


section("G - the guard: strips and never-strips (V7, V8)", sec_guard)
section("F - the recorded outputs (5.2)", sec_fixtures)
section("P - the prompt (V1, V2, 1065 pins)", sec_prompt)
section("W - the real on_message path (V6, V9, V10, V11, V15)", sec_wire)
section("PAR - test mode == live (V14)", sec_parity)

check("no live Sheets / Drive call was attempted", PROBES.calls, [])
config.SALES_TEST_MODE = False
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)

"""R5 "PoCs TO CONTACT" (11 Oct 2026) — the checks, shared by verify_prospects_priority.py and
tests/test_prospects_priority.py.

    sys.path.insert(0, <repo>/tests); import prospects_checks as pc
    pc.run(check)                  # check(name, got, want)

WHAT RUNS HERE IS THE REAL THING: the real parser on made-up sheet rows, the real ordering (`prospects.py`), the
real rule (`nextaction.run`), the real renderer (`drip`), and for the messages a real bot behind a fake Discord
(`replies_world.World`) with a throwaway database. No network, no model, no real sheet. The email lookup is a
stand-in that records it was asked.

EVERY R5 SETTING IS PINNED to its .env.example default for the length of a check (`pinned`), never inherited from
this machine's .env: the laptop still carries the old role order and PROSPECT_COMPANIES_PER_WEEK=2.

THE TWO FIXTURES ARE MADE UP TO THE SPEC'S NUMBERS. `queue_values` gives ten companies the "people left" counts the
ticket lists, with the rule matches that put them in the ticket's order. `message_values` is the ticket's example
message (Wisprlow.ai open with 2 reached and 12 left, Cartesia.ai next with 15). Neither is the real sheet.
"""
import asyncio
import contextlib
import logging
import os
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import config  # noqa: E402
import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import nextaction  # noqa: E402
import prospects  # noqa: E402
import rules as rules_mod  # noqa: E402
import rule13_fixtures as fx  # noqa: E402

TUE, WED, THU = date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)
TUE2, WED2, THU2 = date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)
TUE3, THU3 = date(2026, 10, 27), date(2026, 10, 29)          # Thu 29 Oct is the month's last Tue-or-Thu
TUE_NOV, THU_NOV_LAST = date(2026, 11, 3), date(2026, 11, 26)
# THE BOT SCENARIO RUNS ON DAYS THAT HAVE ALREADY HAPPENED: research (the email lookup) is refused for a day
# after the real today. None of these is the month's last slot (that is Tue 29 Sep).
S_TUE, S_THU, S_THU2, S_TUE3, S_TUE4 = (date(2026, 9, 8), date(2026, 9, 10), date(2026, 9, 17),
                                        date(2026, 9, 22), date(2026, 10, 6))
_ABSENT = object()


def _example(name: str) -> str:
    """A variable's default, read from .env.example (the contract), not retyped here."""
    for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{name} is not in .env.example")


def _list(name: str) -> list:
    return [x.strip() for x in _example(name).split(",") if x.strip()]


def pins() -> dict:
    return {
        "PROSPECT_COMPANIES_PER_WEEK": int(_example("PROSPECT_COMPANIES_PER_WEEK")),
        "PROSPECT_ROLE_ORDER": _list("PROSPECT_ROLE_ORDER"),
        "PROSPECT_VOICE_INDUSTRIES": _list("PROSPECT_VOICE_INDUSTRIES"),
        "PROSPECT_LAB_INDUSTRIES": _list("PROSPECT_LAB_INDUSTRIES"),
        "PROSPECT_KEYWORDS": _list("PROSPECT_KEYWORDS"),
        "PROSPECT_UK_MARKERS": _list("PROSPECT_UK_MARKERS"),
        "PROSPECT_DESIGNATION_MAX_CHARS": int(_example("PROSPECT_DESIGNATION_MAX_CHARS")),
        "EMAIL_LOOKUP_MAX_PER_POST": 5, "EMAIL_WRITE_ALLOWED": True,
        "NEXT_STEP_CONNECTED_MARKERS": ["connected"], "NEXT_STEP_FIRST_DAYS": 2,
    }


@contextlib.contextmanager
def pinned(**extra):
    values = dict(pins(), **extra)
    before = {k: getattr(config, k, _ABSENT) for k in values}
    for k, v in values.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in before.items():
            if v is _ABSENT:
                with contextlib.suppress(AttributeError):
                    delattr(config, k)
            else:
                setattr(config, k, v)


# -- sheet rows ------------------------------------------------------------------------------------------------

H = list(fx.HEADERS_7OCT)
COL = {"company": "Company/Uni", "industry": "Industry", "name": "Name", "title": "Designation",
       "email": "Email id", "based": "Based (Sept 2026)", "paper": "Research Paper Link",
       "fc": "First Contact", "fc_date": "First Contact Date", "connected": "Sid - LI Addition",
       "li_date": "LI Connected Date", "step": "Next Steps", "deal": "Deal Status"}


def prow(n, company, name, title="", **cells) -> list:
    """One Outreach PoCs row (7 Oct layout). `n` is its place on the sheet."""
    r = [""] * len(H)
    r[0] = str(n)
    for key, v in dict(cells, company=company, name=name, title=title).items():
        r[H.index(COL[key])] = fx.cell(v)
    return r


def reached(n, company, name, when="02-09-2026", **cells) -> list:
    return prow(n, company, name, "CEO", fc="TRUE", fc_date=when, **cells)


def rows_of(values) -> list:
    return list(fx.parse(values).rows)


class Counter:
    def __init__(self, start=1):
        self.n = start - 1

    def __call__(self):
        self.n += 1
        return self.n


def block(n, company, industry, left, *, warm=0, uk=False, keyword=False, paper=False) -> list:
    """`left` uncontacted people at a company plus `warm` contacted ones; the first uncontacted person carries
    whichever person signals are asked for."""
    out = [reached(n(), company, f"{company} reached {i}", industry=industry) for i in range(1, warm + 1)]
    for i in range(1, left + 1):
        first = i == 1
        out.append(prow(n(), company, f"{company} person {i:02d}",
                        "Safety Engineer" if (first and keyword) else "Engineer", industry=industry,
                        based="London, UK" if (first and uk) else "Pune, India",
                        paper="https://arxiv.org/abs/2401.00001" if (first and paper) else ""))
    return out


QUEUE_ORDER = [("Sierra AI", 5), ("ElevenLabs", 9), ("PolyAI", 9), ("Cartesia.ai", 15), ("Gnani.ai", 9),
               ("Rime.ai", 6), ("Wisprlow.ai", 12), ("Sarvam AI", 1), ("OpenAI", 8), ("Google DeepMind", 15)]


def queue_values() -> list:
    """Ten companies with the ticket's "people left" counts, in a sheet order that is NOT the answer, plus three
    that match no rule."""
    n = Counter()
    voice, voice2, lab = "AI Voice Agents", "AI Voice/Productivity Tools", "AI Labs"
    return (
        block(n, "Google DeepMind", "AI Labs (frontier)", 15, uk=True, keyword=True, paper=True)
        + block(n, "Plain Logistics", "Logistics", 2)
        + block(n, "Sarvam AI", voice, 1)
        + block(n, "ElevenLabs", voice, 9, warm=2, uk=True)
        + block(n, "Wisprlow.ai", voice2, 12, paper=True)
        + block(n, "OpenAI", "AI labs ", 8, uk=True, keyword=True, paper=True)
        + block(n, "Rime.ai", voice, 6, keyword=True)
        + block(n, "PolyAI", voice, 9, warm=2, uk=True)
        + block(n, "Quiet Retail", "Retail", 1)
        + block(n, "Cartesia.ai", voice, 15, warm=2)
        + block(n, "Gnani.ai", voice, 9, keyword=True, paper=True)
        + block(n, "Sierra AI", voice, 5, warm=3, uk=True)
        + block(n, "Other Fintech", "Fintech", 3, warm=1)
    )


W, C, R, P = "Wisprlow.ai", "Cartesia.ai", "Rime.ai", "Plainco"
W_ORDER = ["Tanay Kothari", "Sahaj Garg", "Ray (Ruizhe) Huang", "Allegra Molinaro", "Nathan Klapoetke",
           "Dev Malhotra", "Carla Ruiz", "Priya Nair", "Jonas Weber", "Omar Haddad", "Lena Fischer", "Mina Okafor"]


def message_values(*, contacted=(), c_done=False, w_fc_dates=None, extra=()) -> list:
    """The ticket's example: Wisprlow.ai (2 reached, 12 left), Cartesia.ai (2 reached, 15 left), Rime.ai (3 left)
    and one company that matches no rule. Sheet order is scrambled on purpose.

    `contacted` names people who have since had a first contact (dated `w_fc_dates.get(name)` or 14-10-2026)."""
    n = Counter()
    vi, va = "AI Voice/Productivity Tools", "AI Voice Agents"
    people = [
        ("Priya Nair", "Software Engineer", {}),
        ("Nathan Klapoetke", "Computational Neuroscientist", {}),
        ("Tanay Kothari", "Co-Founder / CEO", {}),
        ("Omar Haddad", "Product Manager", {}),
        ("Sahaj Garg", "Co-Founder/CTO", {}),
        ("Lena Fischer", "", {}),
        ("Allegra Molinaro", "Computational Linguist & Data Annotation Lead", {}),
        ("Ray (Ruizhe) Huang", "Member of Technical Staff", {}),
        ("Dev Malhotra", "Machine Learning Engineer", {}),
        ("Mina Okafor", "Growth", {}),
        ("Jonas Weber", "Engineer", {}),
        ("Carla Ruiz", "Software Engineer", {"paper": "https://arxiv.org/abs/2402.00002"}),
    ]
    out = [prow(n(), P, "Plain One", "Engineer", industry="Logistics"),
           prow(n(), P, "Plain Two", "Engineer", industry="Logistics")]
    for name, title, more in people:
        cells = dict(more, industry=vi, based="San Francisco, USA")
        if name in contacted:
            cells.update(fc="TRUE", fc_date=(w_fc_dates or {}).get(name, "14-10-2026"))
        out.append(prow(n(), W, name, title, **cells))
    out += [reached(n(), W, "Wendy Reached", industry=vi), reached(n(), W, "Walt Reached", industry=vi)]
    out += [prow(n(), R, f"Rime Person {i}", "Engineer", industry=va) for i in (1, 2, 3)]
    for i in range(1, 16):
        cells = {"industry": va}
        if c_done:
            cells.update(fc="TRUE", fc_date="23-10-2026")
        out.append(prow(n(), C, f"Cartesia Person {i:02d}", "Founder" if i == 4 else "Engineer", **cells))
    out += [reached(n(), C, "Cora Reached", industry=va), reached(n(), C, "Carl Reached", industry=va)]
    return out + list(extra)


TUESDAY_POST = """**PoCs to contact — Wisprlow.ai**

1. Tanay Kothari (Co-Founder / CEO)
2. Sahaj Garg (Co-Founder/CTO)
3. Ray (Ruizhe) Huang (Member of Technical Staff)
4. Allegra Molinaro (Computational Linguist & Data Annotation Lead)
5. Nathan Klapoetke (Computational Neuroscientist)

We've already reached 2 from Wisprlow.ai, there are 7 people left to contact.
Would you like me to list the rest?

Cartesia.ai is next on our list, let me know if you'd like to move on to Cartesia.ai"""


# -- helpers ---------------------------------------------------------------------------------------------------

def engine(values, today, *, state=None, rule_ids=("R5",)):
    rows = rows_of(values)
    return nextaction.run(today=today, rows=rows, prospect_rows=rows, prospect_state=state,
                          day_rules=[rules_mod.by_id(r) for r in rule_ids], next_step_state={})


def post_of(result, today) -> str:
    """The R5 message the drip would post for this run, as text ("" when there is none)."""
    for m in drip.group(result["actions"], day=today):
        if m.get("type") == nextaction.R_PROSPECTS:
            return drip.compose_fallback(m)
    return ""


def names_in(text) -> list:
    """The people a message numbers, in order, without the bracket."""
    return [re.sub(r"\s+\([^()]*\)$", "", line) if not line.endswith("Huang") else line
            for line in _numbered(text)]


def _numbered(text) -> list:
    out = []
    for line in str(text or "").split("\n"):
        m = re.match(r"^\s*(\d+)\.\s+(.+?)\s*$", line)
        if m:
            out.append(m.group(2))
    return out


def bare_names(text) -> list:
    """Numbered names with a trailing "(title)" taken off. "Ray (Ruizhe) Huang (…)" keeps its own bracket."""
    out = []
    for line in _numbered(text):
        m = re.match(r"^(.*\S)\s+\(([^()]*)\)$", line)
        out.append(m.group(1) if m and not line.endswith("Huang") else line)
    return out


def numbers_in(text) -> list:
    return [int(m.group(1)) for m in re.finditer(r"^\s*(\d+)\.\s", str(text or ""), re.MULTILINE)]


class LogTap(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines = []

    def emit(self, record):
        with contextlib.suppress(Exception):
            self.lines.append(record.getMessage())


@contextlib.contextmanager
def tapped(name="sales-bot.prospects"):
    tap, logger = LogTap(), logging.getLogger(name)
    was = logger.level
    logger.addHandler(tap)
    logger.setLevel(logging.INFO)
    try:
        yield tap
    finally:
        logger.removeHandler(tap)
        logger.setLevel(was)


# -- the checks ------------------------------------------------------------------------------------------------

def scoring(check):
    print("\nSCORING — six rules that stack; fewer left, then sheet order; nothing is left out")
    check("each rule's weight is more than every later rule's added together",
          [prospects.WEIGHTS[r] > sum(prospects.WEIGHTS[x] for x in prospects.RULES[i + 1:])
           for i, r in enumerate(prospects.RULES)], [True] * 6)

    n = Counter()
    both = block(n, "WarmOnly", "Fintech", 3, warm=2) + block(n, "WarmVoice", "AI Voice Agents", 3, warm=2)
    q = prospects.build_queue(rows_of(both), today=TUE)
    check("warm + voice beats warm alone (it is not first-match-wins)",
          [(e["company"], e["flags"]) for e in q],
          [("WarmVoice", ["warm", "voice"]), ("WarmOnly", ["warm"])])

    n = Counter()
    rest = (block(n, "Everything Else", "AI Voice Agents / AI Labs", 3, uk=True, keyword=True, paper=True)
            + block(n, "WarmOnly", "Fintech", 3, warm=2))
    q = prospects.build_queue(rows_of(rest), today=TUE)
    check("warm alone beats voice + lab + uk + keyword + paper",
          [(e["company"], e["flags"]) for e in q],
          [("WarmOnly", ["warm"]), ("Everything Else", ["voice", "lab", "uk", "keyword", "paper"])])

    n = Counter()
    labs = block(n, "Trailing", "AI labs ", 2) + block(n, "Suffix", "AI Labs (frontier)", 2) \
        + block(n, "Not A Lab", "AI Infrastructure", 2)
    q = {e["company"]: e["flags"] for e in prospects.build_queue(rows_of(labs), today=TUE)}
    check('"AI labs " and "AI Labs (frontier)" both count as lab; "AI Infrastructure" does not',
          (q["Trailing"], q["Suffix"], q["Not A Lab"]), (["lab"], ["lab"], []))

    n = Counter()
    ties = (block(n, "Later Big", "AI Voice Agents", 6) + block(n, "First Four", "AI Voice Agents", 4)
            + block(n, "Second Four", "AI Voice Agents", 4))
    one = [e["company"] for e in prospects.build_queue(rows_of(ties), today=TUE)]
    two = [e["company"] for e in prospects.build_queue(rows_of(ties), today=TUE)]
    check("a tie goes to fewer people left, then to sheet order", one, ["First Four", "Second Four", "Later Big"])
    check("...and the order is the same on a second run", two, one)

    q = prospects.build_queue(rows_of(queue_values()), today=TUE)
    check("the ticket's ten, in the ticket's order, with the ticket's counts",
          [(e["company"], len(e["left"])) for e in q[:10]], QUEUE_ORDER)
    check("a company matching no rule is still queued, after every company that matches one",
          [(e["company"], e["score"]) for e in q[10:]],
          [("Quiet Retail", 0), ("Plain Logistics", 0), ("Other Fintech", 0)])
    check("...and it is counted: (companies, people)", prospects.unmatched(q), (3, 6))
    with tapped() as tap:
        engine(queue_values(), TUE)
    check("the count is logged on every R5 run",
          [bool(re.search(r"3 of them \(6 people\) match none of the six priority rules", line))
           for line in tap.lines if "match none" in line], [True])
    check("it is in the answer to 'what is left?'",
          "3 companies (6 people) match none of the six priority rules and are queued last."
          in prospects.queue_summary(q, prospects.resolve(q, {})), True)
    check("...and not in the Tuesday post", "match none" in post_of(engine(queue_values(), TUE), TUE), False)


def people(check):
    print("\nPEOPLE — seniority, then the three person signals, then sheet order")
    rank = nextaction._role_rank

    def order(specs):
        rows = rows_of([prow(i, "Co", name, title, **cells) for i, (name, title, cells) in enumerate(specs, 1)])
        return [r["name"] for r in prospects.order_people(rows)]

    check("seniority leads, whatever the sheet order or the signals",
          order([("Eng", "Engineer", {"based": "London", "paper": "https://x.org/p"}),
                 ("Head", "Head of Research", {}), ("Founder", "Founder", {})]),
          ["Founder", "Head", "Eng"])
    check("equal seniority: more of the three signals first",
          order([("None", "Research Scientist", {}),
                 ("One", "Research Scientist", {"based": "Oxford, UK"}),
                 ("Three", "Research Scientist, Speech", {"based": "London", "paper": "https://x.org/p"}),
                 ("Two", "Research Scientist", {"based": "Cambridge", "paper": "https://x.org/q"})]),
          ["Three", "Two", "One", "None"])
    check("equal seniority and equal signals: sheet order",
          order([("B", "Engineer", {}), ("A", "Engineer", {}), ("C", "Engineer", {})]), ["B", "A", "C"])
    check("a title the role order does not recognise is last, in sheet order",
          order([("Odd1", "Growth", {"based": "London", "paper": "https://x.org/p"}), ("Mgr", "Manager", {}),
                 ("Odd2", "Chef", {}), ("Blank", "", {})]), ["Mgr", "Odd1", "Odd2", "Blank"])

    order_list = [x.lower() for x in config.PROSPECT_ROLE_ORDER]
    check('"Chief Scientist" ranks as chief scientist, not the generic chief',
          (rank("Chief Scientist"), rank("Chief of Staff") == order_list.index("chief")),
          (order_list.index("chief scientist"), True))
    check('"Founding Engineer", "Member of Technical Staff", "Head of Data Science", "Senior Lecturer" all rank',
          [rank(t) < 10_000 for t in ("Founding Engineer", "Member of Technical Staff", "Head of Data Science",
                                      "Senior Lecturer")], [True] * 4)
    check("...as founding, staff, head of and senior lecturer",
          [order_list[rank(t)] for t in ("Founding Engineer", "Member of Technical Staff", "Head of Data Science",
                                         "Senior Lecturer")], ["founding", "staff", "head of", "senior lecturer"])
    check('"Global Head of Strategic Sourcing" ranks as a head',
          (rank("Global Head of Strategic Sourcing"), rank("Global Head of Strategic Sourcing") == rank("Head of Data")),
          (order_list.index("head of"), True))
    check("whole words: a Director is a director, not a CTO; a Coordinator is not a COO",
          (order_list[rank("Director of Research")], rank("Programme Coordinator"), order_list[rank("Co-Founder/CTO")]),
          ("director", 10_000, "founder"))

    long_title = "Senior Staff Research Scientist and Technical Programme Lead, Speech and Audio"
    check("the fixture's long title really is over the cap",
          len(long_title) > config.PROSPECT_DESIGNATION_MAX_CHARS, True)
    check("a Designation over the cap prints no bracket (never cut short)",
          prospects.person_line(1, "Ada Long", long_title), "1. Ada Long")
    check("a Designation with a line break prints no bracket",
          prospects.person_line(2, "Bo Break", "CEO\nBoard member, Acme"), "2. Bo Break")
    check("stray spaces are collapsed and the title is printed",
          prospects.person_line(3, "Cy  Space", "  Head   of  Research "), "3. Cy Space (Head of Research)")
    values = [prow(1, "Co", "Ada Long", long_title), prow(2, "Co", "Bo Break", "CEO\nBoard member, Acme"),
              prow(3, "Co", "Cy Fine", "CTO")]
    text = post_of(engine(values, TUE), TUE)
    check("...and the same through the parser, the rule and the renderer",
          sorted(_numbered(text)), ["Ada Long", "Bo Break", "Cy Fine (CTO)"])


def monthly(check):
    print("\nMONTHLY — two lines at the foot of the month's last post, only when nothing moved")
    one = ["Wisprlow.ai hasn't moved this month — 12 people still to contact there.",
           "It's blocking other PoCs to contact."]
    text = post_of(engine(message_values(), THU3), THU3)
    check("Thu 29 Oct (the later of the last Tuesday and the last Thursday): the two lines, at the FOOT",
          text.split("\n")[-2:], one)
    check("...below the normal message, which is unchanged above them",
          text.split("\n")[:-3], TUESDAY_POST.split("\n"))
    check("Tue 27 Oct: not the last slot, so no lines", post_of(engine(message_values(), TUE3), TUE3), TUESDAY_POST)
    check("Tue 30 Jun 2026 is the last slot when the Tuesday falls later",
          (prospects.is_last_slot_of_month(date(2026, 6, 30), (1, 3)),
           prospects.is_last_slot_of_month(date(2026, 6, 25), (1, 3))), (True, False))
    moved = message_values(contacted=("Mina Okafor",), w_fc_dates={"Mina Okafor": "20-10-2026"})
    check("a First Contact Date added this month: no lines, even on the last slot",
          "hasn't moved" in post_of(engine(moved, THU3), THU3), False)
    old = message_values(contacted=("Mina Okafor",), w_fc_dates={"Mina Okafor": "20-09-2026"})
    check("...but a date from LAST month is not movement this month",
          "Wisprlow.ai hasn't moved this month — 11 people still to contact there."
          in post_of(engine(old, THU3), THU3), True)

    two = {"open": "cartesia ai", "paused": ["wisprlow ai"]}
    text = post_of(engine(message_values(), THU3, state=two), THU3)
    check("two stalled companies: ONE combined number naming both",
          text.split("\n")[-2:],
          ["Wisprlow.ai and Cartesia.ai haven't moved — 27 people still to contact between them.",
           "They're blocking other PoCs to contact."])
    three = {"open": "rime ai", "paused": ["wisprlow ai", "cartesia ai"]}
    text = post_of(engine(message_values(), THU3, state=three), THU3)
    check("a third extends the sentence",
          text.split("\n")[-2:],
          ["Wisprlow.ai, Cartesia.ai and Rime.ai haven't moved — 30 people still to contact between them.",
           "They're blocking other PoCs to contact."])
    res = engine(message_values(), THU3, state=two)
    check("the lines close nothing: the same company is open and the same one paused afterwards",
          {k: res["actions"][0]["prospect"]["state"][k] for k in ("open", "paused")}, two)


def never_dropped(check):
    print("\nA COMPANY IS NEVER DROPPED — it leaves the open slot only when everyone is contacted or on 'move on'")
    base = message_values()
    state = {"open": "", "paused": []}
    opened = []
    for day in (TUE, THU, TUE2, THU2, TUE3, THU3, TUE_NOV, THU_NOV_LAST):
        res = engine(base, day, state=state)
        state = {k: res["actions"][0]["prospect"]["state"][k] for k in ("open", "paused")}
        opened.append(res["actions"][0]["company"])
    check("eight posts over two months, nothing changed on the sheet: one company, every time",
          set(opened), {W})

    n = Counter(500)
    hot = block(n, "Hotter Ltd", "AI Voice Agents", 4, warm=5, uk=True, keyword=True, paper=True)
    q = prospects.build_queue(rows_of(base + hot), today=TUE2)
    check("a company that now outscores the open one is ranked above it...", q[0]["company"], "Hotter Ltd")
    view = prospects.resolve(q, {"open": "wisprlow ai", "paused": []})
    check("...and does not take the open slot; it is named as next",
          (view["open"]["company"], view["next"]["company"]), (W, "Hotter Ltd"))

    done = message_values(contacted=tuple(W_ORDER))
    view = prospects.resolve(prospects.build_queue(rows_of(done), today=TUE2), {"open": "wisprlow ai", "paused": []})
    check("everybody contacted: it leaves the slot and the next company opens", view["open"]["company"], C)

    q = prospects.build_queue(rows_of(base), today=TUE)
    moved = prospects.move_on(q, {"open": "wisprlow ai", "paused": []})
    check("'move on': the next company opens and the old one is PAUSED, still in the queue with its score",
          (moved["open"]["company"], [e["company"] for e in moved["paused"]],
           [e["score"] for e in q if e["company"] == W] == [e["score"] for e in moved["paused"]]),
          (C, [W], True))
    back = prospects.resolve(prospects.build_queue(rows_of(message_values(c_done=True)), today=TUE3),
                             moved["state"])
    check("...and it comes back when the company that replaced it is finished",
          (back["open"]["company"], back["state"]["paused"]), (W, []))

    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    callers = sorted(set(re.findall(r"async def (\w+)\(self[^\n]*\n(?:(?!    async def |    def ).*\n)*?"
                                    r".*self\._save_prospect_state\(", src)))
    check("the open company is written in exactly two places: after the post lands, and on 'move on'",
          callers, ["_after_send", "_prospect_move_on"])
    db_src = open(os.path.join(ROOT, "db.py"), encoding="utf-8").read()
    check("nothing writes the retired repeat counter or the per-week table",
          [w for w in ("INSERT INTO prospect_mentions", "INSERT OR IGNORE INTO prospect_week",
                       "def record_prospect_mention", "def prospect_repeats") if w in db_src], [])


def dedup(check):
    print("\nDEDUP — a person the next-steps rule keeps is not named here, and the post still carries five")
    # Sahaj is Connected with a step due, so R13 (listed before R5 in bot_rules.yaml) selects him too.
    values = message_values()
    for r in values:
        if r[H.index("Name")] == "Sahaj Garg":
            r[H.index(COL["connected"])] = "Connected"
            r[H.index(COL["li_date"])] = "01-10-2026"
            r[H.index(COL["step"])] = "Send email 1"
    res = engine(values, TUE, rule_ids=("R13", "R5"))
    r13 = [a["poc"] for a in res["actions"] if a["rule_id"] == "R13"]
    check("the next-steps rule names Sahaj Garg", r13, ["Sahaj Garg"])
    check("...so R5's copy of him is dropped by the one-mention-a-day dedup",
          [(d["poc"], d["rule_id"], d["dropped_for"]) for d in res["deduped"]], [("Sahaj Garg", "R5", "R13")])
    text = post_of(res, TUE)
    check("R5 still posts five: the sixth person moves up",
          bare_names(text), ["Tanay Kothari", "Ray (Ruizhe) Huang", "Allegra Molinaro", "Nathan Klapoetke",
                             "Dev Malhotra"])
    check("...numbered 1 to 5, and he is still counted as left to contact (12 in all, 7 beyond the five)",
          (numbers_in(text), "there are 7 people left to contact." in text), ([1, 2, 3, 4, 5], True))


def messages(check):
    import replies_world as rw

    print("\nMESSAGES — one company, the same five, the replies (a real bot behind a fake Discord)")

    class Stage:
        def __init__(self, test_mode=False):
            self.w = rw.World(test_mode, pretend=(S_TUE, 14, 0), rules={"R5"}, pins=pins())
            self.lookups = []

        def __enter__(self):
            self.w.__enter__()
            self.w.sheet.set(pocs=message_values())
            stage = self

            async def lookup(item, *, rule_id):
                stage.lookups.append((rule_id, item.get("poc")))
                item["email_checked"] = True
                item["email_found"] = "found@example.com"
                item["email_source"] = "https://example.com/team"
                item["web_pending"] = False
            self.w.bot._email_lookup = lookup
            return self

        def __exit__(self, *exc):
            return self.w.__exit__(*exc)

        async def post(self, day):
            self.w.pretend_day(day, 14, 0)
            planned = await self.w.bot._plan_drip(today=day, already=[])
            msg = next((m for m in (planned or {}).get("messages") or []
                        if m.get("type") == nextaction.R_PROSPECTS), None)
            assert msg is not None, f"the planner made no R5 post on {day}"
            # THE SWEEP'S OWN ORDER: research (the email lookups) and then the send.
            await self.w.bot._research_message(msg, today=day)
            before = len(self.w.posted)
            await self.w.bot._send_drip_message(self.w.chan, msg, marker=dl.iso(day), channel_id=self.w.chan.id)
            assert len(self.w.posted) > before, f"the R5 post was not sent on {day}"
            return self.w.posted[before]

        async def say(self, text, **kw):
            before = len(self.w.posted)
            await self.w.say(text, **kw)
            return [rw.strip_tag(m.content) for m in self.w.posted[before:]], self.w.posted[before:]

        def state(self):
            st = self.w.bot.db.prospect_state()
            return st["open"], st["paused"], st["next"]

    async def scenario(test_mode=False):
        out = {}
        with Stage(test_mode) as s:
            w = s.w
            tue = await s.post(S_TUE)
            out["tue"] = rw.strip_tag(tue.content)
            out["tagged"] = tue.content.startswith("[TEST")
            out["state_tue"] = s.state()
            out["lookups"] = list(s.lookups)
            out["proposals"] = dict(w.proposals())
            thu = await s.post(S_THU)
            out["thu"] = rw.strip_tag(thu.content)

            # WEDNESDAY: Sahaj Garg gets a first contact.
            w.sheet.set(pocs=message_values(contacted=("Sahaj Garg",), w_fc_dates={"Sahaj Garg": "16-09-2026"}))
            thu2 = await s.post(S_THU2)
            out["thu2"] = rw.strip_tag(thu2.content)
            out["adhoc"], _ = await s.say("who's left at Wisprlow?", who="member", mention=True)
            out["adhoc_top"], _ = await s.say("who should I reach out to at Cartesia?", who="member", mention=True)
            out["left"], _ = await s.say("what's left to contact?", who="member", mention=True)
            out["voice"], _ = await s.say("find me PoCs in voice AI", who="member", mention=True)
            out["state_before_yes"] = s.state()
            out["rest"], rest_msgs = await s.say("yes", who="member", reply_to=thu2)
            out["state_after_yes"] = s.state()
            out["reactions_after_yes"] = len(w.reactions)

            # A BARE "move on" UNDER THE POST takes the company the post named.
            out["moved"], moved_msgs = await s.say("move on", who="member", reply_to=thu2)
            out["state_moved"] = s.state()
            tue3 = await s.post(S_TUE3)
            out["tue3"] = rw.strip_tag(tue3.content)

            # CARTESIA IS FINISHED: the paused company comes back.
            w.sheet.set(pocs=message_values(contacted=("Sahaj Garg",), w_fc_dates={"Sahaj Garg": "16-09-2026"},
                                            c_done=True))
            nov = await s.post(S_TUE4)
            out["nov"] = rw.strip_tag(nov.content)
            out["state_nov"] = s.state()

            # "move on to Rime", NOT a reply: three people there, fewer than five.
            out["rime"], _ = await s.say("move on to Rime", who="member", mention=True)
            out["state_rime"] = s.state()
            out["nowhere"], _ = await s.say("move on to Nonesuch Labs", who="member", mention=True)
            out["state_nowhere"] = s.state()
            out["all"] = [rw.strip_tag(m.content) for m in w.posted]
            out["writes"] = list(w.sheet.writes)
            out["model"] = w.counts()["model"]
        # A TEST DAY IN THE FUTURE: research is refused for it, and the message must still be the bare shape.
        with Stage(test_mode) as s:
            future = await s.post(date(2027, 3, 2))
            out["future"] = rw.strip_tag(future.content)
        return out

    async def negatives():
        """Words that look like an instruction and are not one. Whatever answers them, R5 must not move."""
        out = {}
        with Stage(False) as s:
            post = await s.post(S_TUE)
            before = s.state()
            said = []
            for text, kw in (("don't move on yet", {"reply_to": post}),
                             ("we should move on from this topic", {"mention": True}),
                             ("who is our contact at Wisprlow?", {"mention": True}),
                             ("no", {"reply_to": post})):
                texts, _ = await s.say(text, who="member", **kw)
                said += texts
            out["state"] = (before, s.state())
            out["r5_shaped"] = [t for t in said if "PoCs to contact" in t or t.startswith("Of course")]
            texts, _ = await s.say("let's move on to Cartesia.ai please", who="member", reply_to=post)
            out["dotted"] = (texts[0].split("\n")[0] if texts else "", s.state()[:2])
        return out

    with pinned():
        got = asyncio.run(scenario(False))
        test = asyncio.run(scenario(True))
        neg = asyncio.run(negatives())

    check("Tuesday: the agreed message, character for character", got["tue"], TUESDAY_POST)
    check("exactly one company is open after it (and the next one is remembered)",
          got["state_tue"], ("wisprlow ai", [], "cartesia ai"))
    check("one question mark in the whole message", got["tue"].count("?"), 1)
    check("Thursday, nothing changed: the same message as Tuesday, from the top", got["thu"], got["tue"])
    check("the email lookup still ran for the five named (R6 shares its cache)...",
          sorted({name for rule, name in got["lookups"] if rule == "R5"}), sorted(W_ORDER[:5]))
    check("a day research is refused for (a test day in the future): still the bare message, no note under it",
          got["future"], TUESDAY_POST)
    check("...and the message prints no email, makes no email offer and opens no proposal",
          ([w for w in ("@", "email", "Email") if w in got["tue"]], got["proposals"]), ([], {}))

    after = [n for n in W_ORDER if n != "Sahaj Garg"]
    check("somebody contacted on Wednesday is gone from the five, and the numbering closes up",
          (bare_names(got["thu2"]), numbers_in(got["thu2"])), (after[:5], [1, 2, 3, 4, 5]))
    check("...and from the counts: 3 reached, 6 beyond the five",
          "We've already reached 3 from Wisprlow.ai, there are 6 people left to contact." in got["thu2"], True)

    rest = got["rest"][0] if got["rest"] else ""
    check('a "yes" (from somebody who is not an approver) lists the rest',
          rest.split("\n")[0], "Of course — here are the other 6 at Wisprlow.ai:")
    check("...numbered on from 6", numbers_in(rest), [6, 7, 8, 9, 10, 11])
    check("...the people after the five, in the same order, without the person contacted",
          bare_names(rest), after[5:])
    check("...with no new question, as one reply and no reaction",
          ("?" in rest, len(got["rest"]), got["reactions_after_yes"]), (False, 1, 0))
    check("...and the company stays open", got["state_after_yes"], got["state_before_yes"])

    adhoc = got["adhoc"][0] if got["adhoc"] else ""
    check('"who\'s left at Wisprlow?" gives the same people in the same order as the post and its rest',
          bare_names(adhoc), bare_names(got["thu2"]) + bare_names(rest))
    check("...under the same heading, asking nothing",
          (adhoc.split("\n")[0], "?" in adhoc), ("**PoCs to contact — Wisprlow.ai**", False))
    top = got["adhoc_top"][0] if got["adhoc_top"] else ""
    check('"who should I reach out to at Cartesia?" gives that company\'s top five, founder first',
          (top.split("\n")[0], bare_names(top)[0], len(bare_names(top)), "?" in top),
          ("**PoCs to contact — Cartesia.ai**", "Cartesia Person 04", 5, False))
    left = got["left"][0] if got["left"] else ""
    check('"what\'s left to contact?" carries the count of companies matching no rule',
          ("Open now: Wisprlow.ai (11 left)." in left,
           "1 company (2 people) match none of the six priority rules and are queued last." in left), (True, True))
    voice = got["voice"][0] if got["voice"] else ""
    check('"find me PoCs in voice AI" answers from the sheet, best company first',
          (voice.split("\n")[0], "Also, in order: Cartesia.ai (15 left), Rime.ai (3 left)" in voice),
          ("**PoCs to contact — Wisprlow.ai**", True))
    check("a question opens and moves nothing", got["state_before_yes"], ("wisprlow ai", [], "cartesia ai"))

    moved = got["moved"][0] if got["moved"] else ""
    check('a bare "move on" takes the company the post named as next',
          moved.split("\n")[0], "**PoCs to contact — Cartesia.ai**")
    check("...in the Tuesday shape: five people, the counts, the one question, the next company",
          (numbers_in(moved), "We've already reached 2 from Cartesia.ai, there are 10 people left to contact." in moved,
           moved.count("?"), moved.split("\n")[-1]),
          ([1, 2, 3, 4, 5], True, 1, "Rime.ai is next on our list, let me know if you'd like to move on to Rime.ai"))
    check("the next-company line names the next company that is not paused", "Wisprlow.ai is next" in moved, False)
    check("Cartesia is open and Wisprlow is paused, not dropped",
          got["state_moved"], ("cartesia ai", ["wisprlow ai"], "rime ai"))
    check("the next scheduled post stays with Cartesia", got["tue3"].split("\n")[0],
          "**PoCs to contact — Cartesia.ai**")
    check("when Cartesia is finished the paused company returns, with its people and counts",
          (got["nov"].split("\n")[0], bare_names(got["nov"]), got["state_nov"][:2]),
          ("**PoCs to contact — Wisprlow.ai**", after[:5], ("wisprlow ai", [])))

    rime = got["rime"][0] if got["rime"] else ""
    check('"move on to Rime" (not a reply) opens the company named',
          (rime.split("\n")[0], got["state_rime"][:2]),
          ("**PoCs to contact — Rime.ai**", ("rime ai", ["wisprlow ai"])))
    check("fewer than five left: all of them, and no 'list the rest' question",
          (numbers_in(rime), prospects.ASK_REST in rime, "?" in rime), ([1, 2, 3], False, False))
    check("a company that is not in the queue moves nothing and says so",
          (got["nowhere"], got["state_nowhere"] == got["state_rime"]),
          ([prospects.NOT_IN_QUEUE.format(company="Nonesuch Labs")], True))

    check('"don\'t move on yet", "we should move on from this topic", "who is our contact at X?" and "no" '
          "move nothing and get no R5 list",
          (neg["state"][0] == neg["state"][1], neg["r5_shaped"]), (True, []))
    check('a company name with a dot in it survives the sentence around it ("...move on to Cartesia.ai please")',
          neg["dotted"], ("**PoCs to contact \u2014 Cartesia.ai**", ("cartesia ai", ["wisprlow ai"])))

    every = "\n".join(got["all"])
    check('the "listed N times" / "Skip them?" line appears nowhere',
          [w for w in ("listed them", "Skip them", "skip them", "nothing changed") if w in every], [])
    check("no model call anywhere, and nothing written to the sheet", (got["model"], got["writes"]), (0, []))
    check("test mode: the same messages, each tagged",
          (test["all"] == got["all"], test["tagged"], got["tagged"]), (True, True, False))


def run(check):
    with pinned():
        scoring(check)
        people(check)
        monthly(check)
        never_dropped(check)
        dedup(check)
    messages(check)

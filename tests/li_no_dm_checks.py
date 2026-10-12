"""R6 "CONNECTED, NO DM YET" (rebuilt 12 Oct 2026) — the checks, shared by verify_li_no_dm.py and
tests/test_li_no_dm.py.

    sys.path.insert(0, <repo>/tests); import li_no_dm_checks as lnd
    lnd.run(check)                 # check(name, got, want)

WHAT RUNS HERE IS THE REAL THING: made-up rows through the real sheet parser, the real rule
(`nextaction.run`), the real email work, renderer and sender (`bot._li_no_dm_email_work`, `drip`,
`wording.li_no_dm_post`) in a real bot behind a fake Discord with a throwaway database, and the real proposal
path for the add. TWO THINGS ARE STAND-INS, because they are the network: the email search (it answers from a
table and counts what it was asked) and the DNS lookup (`email_checks.mx_status`, from a table). No network, no
model, no real sheet.

Every R6 setting is pinned to its .env.example default, with EMAIL_WRITE_ALLOWED on so the closing question
and the write can be shown.

THE PEOPLE: the names in the ticket's own example message, Gina Neff's row as the ticket describes it, and
made-up names for the rest. EVERY EMAIL ADDRESS HERE IS MADE UP.
"""
import asyncio
import contextlib
import logging
import os
import re
import sys
from datetime import date, timedelta

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
import email_checks  # noqa: E402
import nextaction  # noqa: E402
import rules as rules_mod  # noqa: E402
import rule13_fixtures as fx  # noqa: E402
import wording  # noqa: E402

# All already past: the email search and the DNS check are not run for a day after the real today.
PREV_FRI, TUE, FRI = date(2026, 10, 2), date(2026, 10, 6), date(2026, 10, 9)
COLUMN_NAMES = ("LI DM Sent", "Email id", "LI Connected Date", "Next Steps")
_ABSENT = object()


def _example(name: str) -> str:
    for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{name} is not in .env.example")


def _list(name: str) -> list:
    return [x.strip() for x in _example(name).split(",") if x.strip()]


def pins() -> dict:
    return {
        "LI_NO_DM_DAYS": int(_example("LI_NO_DM_DAYS")),
        "LI_NO_DM_EMAIL_NAMES_PER_POST": int(_example("LI_NO_DM_EMAIL_NAMES_PER_POST")),
        "LI_NO_DM_DM_SENT_MARKERS": _list("LI_NO_DM_DM_SENT_MARKERS"),
        "LI_NO_DM_DM_REPLIED_MARKERS": _list("LI_NO_DM_DM_REPLIED_MARKERS"),
        "LI_NO_DM_VERIFY_MX": True,
        "EMAIL_LOOKUP_MAX_PER_POST": int(_example("EMAIL_LOOKUP_MAX_PER_POST")),
        "RESEARCH_CACHE_DAYS": 7, "EMAIL_WRITE_ALLOWED": True, "SHEET_WRITES_ENABLED": True,
        "NEXT_STEP_CONNECTED_MARKERS": ["connected"],
        "NEXT_STEP_DM_SENT_MARKERS": ["sent", "received"],
        "NEXT_STEP_DM_REPLIED_MARKERS": ["replied", "responded", "received"],
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


@contextlib.contextmanager
def config_pins(**values):
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


# -- rows -----------------------------------------------------------------------------------------------------------

def poc(n, name, company, *, days=None, email="", day=TUE, **cells):
    """One Outreach PoCs row. `days` is how long ago they connected (None: no LI Connected Date). NOT marked
    Connected in "Sid - LI Addition": this rule does not read that column.

    First Contact is ticked, so the row is not ALSO one of R5's prospects: on a Tuesday R5 is listed before R6
    and keeps anybody both rules select (that overlap has its own check)."""
    more = dict({"first_contact": "TRUE"}, **cells)
    if days is not None:
        more["li_date"] = day - timedelta(days=days)
    return fx.values_row(n, company=company, name=name, designation="CTO", email=email, **more)


EXISTING = {                      # the addresses ALREADY on the fixture sheet: none may ever be printed
    "Angus Addlesee": "angus@addleseehq.co.uk",
    "Daisy Leigh": "daisy.leigh@poly.ai",
    "Ioana Mocanu": "ioana.m@agoda.com / +66 2 555 0100",
    "Gina Neff": "g.neff@qmul.ac.uk\ngn.research@cam.ac.uk",
}
FOUND = {"Joel Georgie J.": ["joel@artpark.in"], "Mouparna Sen": ["mouparna.sen@gnani.ai"]}
DEAD = {"addleseehq.co.uk"}


def example_values(day=TUE) -> list:
    """The ticket's example: two fine, two found, one dead domain, one not found."""
    return [
        poc(1, "Ishan Mishra", "AI Security Institute", days=6, day=day),
        poc(2, "Mouparna Sen", "Gnani.ai", days=7, day=day),
        poc(3, "Joel Georgie J.", "ARTPARK", days=8, email="not available", day=day),
        poc(4, "Ioana Mocanu", "Agoda", days=10, email=EXISTING["Ioana Mocanu"], day=day),
        poc(5, "Daisy Leigh", "PolyAI", days=34, email=EXISTING["Daisy Leigh"], day=day),
        poc(6, "Angus Addlesee", "PolyAI", days=35, email=EXISTING["Angus Addlesee"], day=day),
    ]


EXPECTED = """**Connected, no DM yet**
1. Daisy Leigh (PolyAI)
2. Ioana Mocanu (Agoda)

Found missing or alternate emails:
3. Joel Georgie J. (ARTPARK) — joel@artpark.in
4. Mouparna Sen (Gnani.ai) — mouparna.sen@gnani.ai

These domains can't receive mail:
5. Angus Addlesee (PolyAI) — addleseehq.co.uk

Couldn't find an email for:
6. Ishan Mishra (AI Security Institute)

Would you like me to add these emails to the Outreach PoCs sheet?"""


def rows_of(values) -> list:
    return list(fx.parse(values).rows)


def engine(values, day=TUE, *, active=None, rule_ids=("R6",)):
    """The rule alone. `active` is what the activation gate let through (default: nothing, to show the rule
    reads the whole tab)."""
    rows = rows_of(values)
    return nextaction.run(today=day, rows=list(active if active is not None else []), prospect_rows=rows,
                          day_rules=[rules_mod.by_id(r) for r in rule_ids], next_step_state={})


def picked(result) -> list:
    return [a["poc"] for a in sorted((a for a in result["actions"] if a.get("rule_id") == "R6"
                                      and not a.get("notice")), key=lambda a: a["pick_order"])]


class LogTap(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines = []

    def emit(self, record):
        with contextlib.suppress(Exception):
            self.lines.append((record.levelname, record.getMessage()))


class Stage:
    """One bot behind a fake Discord, with the two network stand-ins."""

    def __init__(self, values, *, found=None, dead=None, test_mode=False, dns=True, extra_pins=None,
                 search_ok=True):
        import replies_world as rw
        self.rw = rw
        self.values = values
        self.found = dict(FOUND if found is None else found)
        self.dead = set(DEAD if dead is None else dead)
        self.dns, self.search_ok = dns, search_ok
        self.w = rw.World(test_mode, pretend=(TUE, 16, 0), rules={"R6"}, pins=dict(pins(), **(extra_pins or {})))
        self.searches, self.mx_asked = [], []
        self.tap = LogTap()

    def __enter__(self):
        self.w.__enter__()
        self.w.sheet.set(pocs=self.values)
        stage = self

        async def lookup(item, *, rule_id):
            item["web_pending"] = False
            if not stage.search_ok:
                item["research_note"] = "the search is switched off"
                return
            stage.searches.append(item.get("poc"))
            got = list(stage.found.get(item.get("poc"), []))
            item["email_checked"] = True
            item["emails_found"] = got
            item["email_found"] = got[0] if got else ""
            item["email_source"] = "https://people.example/profile" if got else ""
            item["sources"] = [{"url": item["email_source"], "title": ""}] if got else []
            item["research_note"] = ""
        self.w.bot._email_lookup = lookup

        self._saved = (email_checks.mx_status, email_checks.have_dns)

        def mx(domain, **_k):
            stage.mx_asked.append(domain)
            return email_checks.DEAD if domain in stage.dead else email_checks.OK
        email_checks.mx_status = mx
        email_checks.have_dns = lambda: stage.dns
        logging.getLogger().addHandler(self.tap)
        return self

    def __exit__(self, *exc):
        logging.getLogger().removeHandler(self.tap)
        email_checks.mx_status, email_checks.have_dns = self._saved
        return self.w.__exit__(*exc)

    async def post(self, day=TUE):
        """The real planner, the real email work and the real sender. (text, the BotMsg, the message dict)."""
        w = self.w
        w.pretend_day(day, 16, 0)
        planned = await w.bot._plan_drip(today=day, already=[])
        msg = next((m for m in (planned or {}).get("messages") or []
                    if m.get("type") == nextaction.R_LI_NO_DM), None)
        if msg is None:
            return "", None, None
        await w.bot._research_message(msg, today=day)
        before = len(w.posted)
        await w.bot._send_drip_message(w.chan, msg, marker=dl.iso(day), channel_id=w.chan.id)
        new = w.posted[before:]
        return ("\n".join(self.rw.strip_tag(m.content) for m in new), new[0] if new else None, msg)

    def logs(self, needle, level=None) -> list:
        return [m for lvl, m in self.tap.lines if needle in m and (level is None or lvl == level)]

    async def say(self, text, **kw):
        before = len(self.w.posted)
        await self.w.say(text, **kw)
        return [self.rw.strip_tag(m.content) for m in self.w.posted[before:]]


RENDERED: list = []


def keep(text: str) -> str:
    if text:
        RENDERED.append(text)
    return text


def numbers_in(text) -> list:
    return [int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", str(text or ""), re.MULTILINE)]


def names_in(text) -> list:
    return [m.group(1) for m in re.finditer(r"^\d+\. (.+?) \(", str(text or ""), re.MULTILINE)]


def one_post(values, **kw) -> dict:
    async def go():
        with Stage(values, **kw) as s:
            text, _post, msg = await s.post()
            return {"text": keep(text), "searches": list(s.searches), "mx": list(s.mx_asked),
                    "skipped_log": s.logs("MX check is SKIPPED"), "errors": s.logs("", "ERROR"),
                    "blocks": {a["poc"]: a.get("block") for a in (msg or {}).get("actions") or []},
                    "proposals": dict(s.w.proposals()), "tagged": bool(_post and _post.content.startswith("[TEST"))}
    with pinned(**(kw.get("extra_pins") or {})):
        return asyncio.run(go())


# -- the checks -----------------------------------------------------------------------------------------------------------

def who_is_picked(check):
    print("\nWHO IS PICKED — LI DM Sent blank, three days connected or no date at all; the whole tab")
    base = lambda **c: [poc(1, "Pat Person", "Co", **c)]  # noqa: E731
    check("LI DM Sent blank, connected 3 days ago: in", picked(engine(base(days=3))), ["Pat Person"])
    check("connected 2 days ago: not yet", picked(engine(base(days=2))), [])
    check("no LI Connected Date at all: in straight away", picked(engine(base())), ["Pat Person"])
    check("LI DM Sent says sent, Sent, replied, Replied or Received: out",
          [picked(engine(base(days=30, dm_sent=v))) for v in ("sent", "Sent", "replied", "Replied", "Received")],
          [[]] * 5)
    check("...whatever the LI DM Date says: a date with the cell blank does not take anybody out",
          picked(engine(base(days=30, dm_date=TUE - timedelta(days=5)))), ["Pat Person"])
    check("Prospect Status or Deal Status is a stop marker: out, as for every rule",
          [picked(engine(base(days=30, **c))) for c in ({"prospect": "Unresponsive"}, {"deal": "Dead"},
                                                        {"prospect": "Not interested"})], [[]] * 3)
    check("it does not read 'Sid - LI Addition': Connected, Requested or blank there changes nothing",
          [picked(engine(base(days=30, connected=v))) for v in ("Connected", "Requested", "")],
          [["Pat Person"]] * 3)

    # THE SHAPE OF THE 11 OCT SHEET, in miniature: a few dated rows and many with no activating date at all.
    undated = [poc(100 + i, f"Undated {i:03d}", f"Co {i:03d}") for i in range(60)]
    dated = [poc(10 + i, f"Dated {i}", "Dated Co", days=10 + i) for i in range(4)]
    res = engine(undated + dated, active=[])
    report = res["reports"]["R6"]
    check("the rule reads the WHOLE TAB: with the activation gate letting no row through, all 64 qualify",
          (report["rows"], report["qualify"], report["dated"], report["undated"]), (64, 64, 4, 60))
    check("...and a row with no activating date at all is picked",
          "Undated 000" in picked(res), True)
    check("the order: a connected date first, OLDEST first; then no date, in sheet order",
          picked(res)[:6], ["Dated 3", "Dated 2", "Dated 1", "Dated 0", "Undated 000", "Undated 001"])
    check("...and only the first of them are handed on, not all 64",
          (len(picked(res)), report["handed_on"]), (nextaction.LI_NO_DM_SCAN, nextaction.LI_NO_DM_SCAN))
    check("no rotation: the same people, in the same order, on a second run with the sheet unchanged",
          picked(engine(undated + dated, active=[])), picked(res))
    src = open(os.path.join(ROOT, "nextaction.py"), encoding="utf-8").read().split("def _r_li_no_dm(")[1].split(
        "\ndef ")[0]
    check("the rule keeps no state: no drop clock, no mention counter, no 'first named' date",
          [w for w in ("next_step_state", "first_named", "mentions", "sid_li_added", "marked_connected")
           if w in src.split('"""')[2]], [])
    with_r13 = [fx.person(1, name="Shared Sam", company="Co", li_date=TUE - timedelta(days=30),
                          step="Send email 1"),
                poc(2, "Only Six", "Co", days=20)]
    both = engine(with_r13, rule_ids=("R13", "R6"))
    check("a person rule 13 named today does not appear here: rule 13 is listed first and keeps them",
          (picked(both), [(d["poc"], d["rule_id"], d["dropped_for"]) for d in both["deduped"]]),
          (["Only Six"], [("Shared Sam", "R6", "R13")]))
    prospect = [fx.values_row(1, company="Fresh Co", name="Uncontacted Una", designation="CTO",
                              li_date=TUE - timedelta(days=20))]
    with_r5 = engine(prospect, rule_ids=("R5", "R6"))
    check("on a Tuesday R5 is listed before R6 too: a never-contacted person at R5's open company is R5's",
          (picked(with_r5), [(d["poc"], d["dropped_for"]) for d in with_r5["deduped"]]),
          ([], [("Uncontacted Una", "R5")]))
    meeting = [poc(1, "Mona Meeting", "Meet Co", days=20, meeting_date=TUE + timedelta(days=3)),
               poc(2, "Only Six", "Co", days=20)]
    with config_pins(MEETING_PREP_DAYS_BEFORE=["5", "3"]):
        with_r8 = engine(meeting, active=rows_of(meeting), rule_ids=("R6", "R8"))
    check("R6 GIVES WAY to a rule listed AFTER it too: meeting prep keeps its person, and R6 names the rest",
          (picked(with_r8), [a["poc"] for a in with_r8["actions"] if a["rule_id"] == "R8"],
           [(d["poc"], d["rule_id"], d["dropped_for"]) for d in with_r8["deduped"]]),
          (["Only Six"], ["Mona Meeting"], [("Mona Meeting", "R6", "R8")]))
    order = [r.id for r in rules_mod.safe_load()]
    check("R13 is still listed before R6 in bot_rules.yaml", order.index("R13") < order.index("R6"), True)


def the_message(check):
    print("\nTHE MESSAGE — four blocks, numbered straight through, only addresses the bot found")
    with pinned():
        got = one_post(example_values())
        test = one_post(example_values(), test_mode=True)
    text = got["text"]
    check("the ticket's example, character for character (the tags line goes under the heading at send)",
          text, EXPECTED)
    check("the four blocks in order", [ln for ln in text.split("\n") if ln.endswith(":")],
          [wording.LI_NO_DM_FOUND, wording.LI_NO_DM_DEAD, wording.LI_NO_DM_NONE])
    check("the numbering runs straight through, and each person appears once",
          (numbers_in(text), sorted(names_in(text)) == sorted(set(names_in(text))), len(names_in(text))),
          ([1, 2, 3, 4, 5, 6], True, 6))
    check("one search each for the three with no usable address, in the rule's order; none for the others",
          got["searches"], ["Joel Georgie J.", "Mouparna Sen", "Ishan Mishra"])
    check('"not available" in the Email cell is treated as blank: one search', "Joel Georgie J." in got["searches"],
          True)
    check("each address on file had its domain checked, and only its domain",
          sorted(set(got["mx"])), ["addleseehq.co.uk", "agoda.com", "poly.ai"])
    check("an address plus a phone number: the address is used, the rest ignored",
          email_checks.addresses_in(EXISTING["Ioana Mocanu"]), ["ioana.m@agoda.com"])
    check("the dead-domain block names the DOMAIN only, never the address",
          [ln for ln in text.split("\n") if "Angus" in ln], ["5. Angus Addlesee (PolyAI) — addleseehq.co.uk"])
    check("the closing question is there because an address was found, and it is the last line",
          text.split("\n")[-1], wording.LI_NO_DM_ADD_QUESTION)
    check("test mode: the same message, tagged", (test["text"], test["tagged"], got["tagged"]),
          (text, True, False))

    plain = one_post([poc(1, "Daisy Leigh", "PolyAI", days=34, email=EXISTING["Daisy Leigh"]),
                      poc(2, "Ioana Mocanu", "Agoda", days=10, email=EXISTING["Ioana Mocanu"])])
    check("everybody's email is fine: a plain numbered list, no block heading and NO closing question",
          plain["text"], "**Connected, no DM yet**\n1. Daisy Leigh (PolyAI)\n2. Ioana Mocanu (Agoda)")
    check("...and no proposal is opened", plain["proposals"], {})
    only_dead = one_post([poc(1, "Angus Addlesee", "PolyAI", days=35, email=EXISTING["Angus Addlesee"])])
    check("only a dead domain: that block alone, numbered from 1, and no question",
          only_dead["text"], "**Connected, no DM yet**\nThese domains can't receive mail:\n"
                             "1. Angus Addlesee (PolyAI) — addleseehq.co.uk")
    only_none = one_post([poc(1, "Ishan Mishra", "AI Security Institute", days=6)], found={})
    check("only nothing-found: that block alone, and no question (there is nothing to add)",
          only_none["text"], "**Connected, no DM yet**\nCouldn't find an email for:\n"
                             "1. Ishan Mishra (AI Security Institute)")

    two = one_post([poc(1, "Tara Two", "Twin Labs", days=9)],
                   found={"Tara Two": ["tara@twinlabs.ai", "tara.two@gmail.com"]})
    check("two addresses found for one person: both on their one line, comma separated",
          two["text"].split("\n")[2], "1. Tara Two (Twin Labs) — tara@twinlabs.ai, tara.two@gmail.com")

    same = one_post([poc(1, "Sam Same", "Same Co", days=9, email="reach sam at sam [at] same.co"),
                     poc(2, "Nina None", "None Co", days=8)],
                    found={"Sam Same": ["sam@same.co"]})
    dup = one_post([poc(1, "Dee Dupe", "Dupe Co", days=9, email="Dee Dupe <DEE@dupe.co>; +1 555 0100 x")],
                   found={"Dee Dupe": ["dee@dupe.co"]})
    check("an address on file is checked, not searched for, and never suggested back",
          (dup["searches"], dup["text"]), ([], "**Connected, no DM yet**\n1. Dee Dupe (Dupe Co)"))
    async def already():
        """A found address that is ALREADY in the cell (the cell is prose the syntax check reads no address in)."""
        with Stage([poc(1, "Ollie Old", "Old Co", days=9, email="write to ollie@old .co or OLLIE@OLD.CO.")],
                   found={"Ollie Old": ["ollie@old.co"]}) as s:
            text, _p, msg = await s.post()
            return keep(text), s.searches, dict(s.w.proposals())
    with pinned():
        a_text, a_searches, a_props = asyncio.run(already())
    check("a found address that is already in the cell produces NO line anywhere: the person is in the plain "
          "list, with no address shown and no proposal",
          (a_text, a_props), ("**Connected, no DM yet**\n1. Ollie Old (Old Co)", {}))
    del same


def the_caps(check):
    print("\nTHE TWO CAPS — five in the plain list, five with email news, counted separately")
    fine = [poc(i, f"Fine {i:02d}", "Fine Co", days=60 - i, email=f"fine{i}@fine.co") for i in range(1, 9)]
    news = [poc(20 + i, f"News {i:02d}", "News Co", days=30 - i) for i in range(1, 9)]
    with pinned():
        got = one_post(fine + news, found={f"News {i:02d}": [f"news{i}@news.co"] for i in (1, 2, 3)})
    text = got["text"]
    check("at most ten people: five in the plain list and five with email news",
          (len(names_in(text)), [n for n in names_in(text) if n.startswith("Fine")],
           [n for n in names_in(text) if n.startswith("News")]),
          (10, [f"Fine {i:02d}" for i in range(1, 6)], [f"News {i:02d}" for i in range(1, 6)]))
    check("the three email blocks share ONE allowance of five (three found, two not found)",
          (text.count("@news.co"), len(text.split(wording.LI_NO_DM_NONE)[1].strip().split("\n")[:2])), (3, 2))
    check("...so five searches, not eight: EMAIL_LOOKUP_MAX_PER_POST matches the allowance",
          got["searches"], [f"News {i:02d}" for i in range(1, 6)])
    check("a quiet week cannot push the email work off the post: eight fine people still leave room for five "
          "with email news", "Found missing or alternate emails:" in text, True)
    with pinned():
        small = one_post(fine + news, found={}, extra_pins={"LI_NO_DM_EMAIL_NAMES_PER_POST": 2})
    check("LI_NO_DM_EMAIL_NAMES_PER_POST=2: two with email news, five plain",
          (len([n for n in names_in(small["text"]) if n.startswith("News")]),
           len([n for n in names_in(small["text"]) if n.startswith("Fine")]), small["searches"]),
          (2, 5, ["News 01", "News 02"]))


def gina_and_the_checks(check):
    print("\nTHE THREE CHEAP CHECKS — and the cases where one cannot run")
    gina = [poc(608, "Gina Neff", "Multiple", email=EXISTING["Gina Neff"])]
    with pinned():
        got = one_post(gina)
    check('Gina Neff, company "Multiple", two academic addresses: block 1, and nothing is said about her',
          got["text"], "**Connected, no DM yet**\n1. Gina Neff (Multiple)")
    check("...both of her addresses had their domain checked", sorted(got["mx"]), ["cam.ac.uk", "qmul.ac.uk"])
    check('"Multiple" is not a company: the domain comparison is SKIPPED, not failed',
          [email_checks.company_check(a, "Multiple") for a in email_checks.addresses_in(EXISTING["Gina Neff"])],
          [email_checks.SKIPPED, email_checks.SKIPPED])
    check("a university, a consortium, two organisations together: skipped too",
          [email_checks.company_check("x@cam.ac.uk", c) for c in (
              "University of Cambridge", "AI Safety Consortium", "Acme / Globex", "Acme and Globex")],
          [email_checks.SKIPPED] * 4)
    with pinned():
        personal = one_post([poc(1, "Percy Personal", "PolyAI", days=9, email="percy.p@gmail.com")])
        mismatch = one_post([poc(1, "Mina Mismatch", "Acme Robotics", days=9, email="mina@globex.com")])
    check("an address on a personal domain: nothing said. A personal address is not an error",
          personal["text"], "**Connected, no DM yet**\n1. Percy Personal (PolyAI)")
    check("an address at another company's domain: nothing said either. Only a dead domain is ever reported",
          mismatch["text"], "**Connected, no DM yet**\n1. Mina Mismatch (Acme Robotics)")

    angus = [poc(1, "Angus Addlesee", "PolyAI", days=35, email=EXISTING["Angus Addlesee"])]
    with pinned():
        no_dns = one_post(angus, dns=False)
        off = one_post(angus, extra_pins={"LI_NO_DM_VERIFY_MX": False})
    check("dnspython absent: the MX check is skipped, nothing is reported dead, and it is logged",
          (no_dns["text"], no_dns["mx"], len(no_dns["skipped_log"]) >= 1),
          ("**Connected, no DM yet**\n1. Angus Addlesee (PolyAI)", [], True))
    check("LI_NO_DM_VERIFY_MX=false: no lookup and no dead-domain block (never 'every domain is fine')",
          (off["text"], off["mx"]), ("**Connected, no DM yet**\n1. Angus Addlesee (PolyAI)", []))
    saved = email_checks.have_dns
    real_import = __import__

    def no_dns_import(name, *a, **k):
        if name.startswith("dns"):
            raise ImportError("No module named 'dns'")
        return real_import(name, *a, **k)
    import builtins
    builtins.__import__ = no_dns_import
    try:
        email_checks._MX_CACHE.clear()
        status = email_checks.mx_status("example.org")
    finally:
        builtins.__import__ = real_import
        email_checks.have_dns = saved
    check("the real lookup with dnspython missing answers 'skipped', never 'dead', and does not raise",
          status, email_checks.SKIPPED)
    src = open(os.path.join(ROOT, "email_checks.py"), encoding="utf-8").read()
    code = "\n".join(ln for ln in src.split('"""')[2::2])
    check("there is no SMTP probe and no paid verification API in the code",
          [w for w in ("smtplib", "RCPT", "requests.", "api_key", "http") if w in code], [])
    check("dnspython is in requirements.txt",
          bool(re.search(r"^dnspython", open(os.path.join(ROOT, "requirements.txt")).read(), re.MULTILINE)), True)
    with pinned():
        cannot = one_post([poc(1, "Una Unknown", "Co", days=9)], search_ok=False)
    check("the search cannot run (switched off, or its budget spent): nothing is claimed about their email",
          cannot["text"], "**Connected, no DM yet**\n1. Una Unknown (Co)")


def repeats_and_all_clear(check):
    print("\nNO DROP, NO ROTATION — and the all-clear")

    async def scenario():
        out = {}
        with Stage(example_values()) as s:
            w = s.w
            w.sheet.set(pocs=example_values(PREV_FRI))
            first, _p, _m = await s.post(PREV_FRI)
            searches = len(s.searches)
            w.sheet.set(pocs=example_values(TUE))        # the same people, four days on
            second, _p, _m = await s.post(TUE)
            out["same"] = (keep(first), keep(second))
            out["searched_again"] = len(s.searches) - searches
            # THE SHEET MOVES: the DM to Daisy is logged.
            moved = example_values(FRI)
            moved[4][fx.HEADERS_7OCT.index("LI DM Sent")] = "Sent"
            w.sheet.set(pocs=moved)
            third, _p, _m = await s.post(FRI)
            out["third"] = keep(third)
            with w.bot.db.conn() as c:
                out["tables"] = [r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%li_no_dm%'").fetchall()]
        with Stage([poc(1, "Sent Sue", "Co", days=30, dm_sent="Sent"),
                    poc(2, "Recent Rae", "Co", days=1)]) as s:
            text, post, msg = await s.post(TUE)
            out["clear"] = keep(text)
            out["clear_rows"] = s.w.drip_rows()
        with Stage([]) as s:
            text, _p, _m = await s.post(TUE)
            out["empty"] = (text, s.logs("came back with NO ROWS", "ERROR"))
        return out

    with pinned():
        got = asyncio.run(scenario())
    check("named once already, still no DM: named again on the next post, the same message",
          got["same"][1], got["same"][0])
    check("...and nobody was searched for a second time (the answers are cached)", got["searched_again"], 0)
    check("the list moves when the sheet moves: a logged DM takes that person out and the numbering closes up",
          ("Daisy Leigh" in got["third"], got["third"].split("\n")[1], numbers_in(got["third"])),
          (False, "1. Ioana Mocanu (Agoda)", [1, 2, 3, 4, 5]))
    check("R6 keeps no state of its own: no table for it", got["tables"], [])
    check("nobody qualifies: a post still goes out, one line under the heading",
          got["clear"], "**Connected, no DM yet**\nAll clear, nobody is waiting on a DM.")
    check("...in R6's own slot, counted like the post it replaces",
          [(r[0], r[2]) for r in got["clear_rows"]], [(dl.iso(TUE), nextaction.R_LI_NO_DM)])
    check("the tab comes back empty: NO post, and it is logged at ERROR, never a silent skip",
          (got["empty"][0], len(got["empty"][1]) >= 1), ("", True))


def the_add(check):
    print("\nTHE ADD — the closing question, an approver's yes, and only the blank Email cell")
    import gtm_sheet

    async def scenario(allowed=True):
        out = {}
        values = example_values() + [poc(7, "Tara Two", "Twin Labs", days=5)]
        found = dict(FOUND, **{"Tara Two": ["tara@twinlabs.ai", "tara.two@gmail.com"]})
        with Stage(values, found=found, extra_pins={"EMAIL_WRITE_ALLOWED": allowed}) as s:
            w = s.w
            text, post, _m = await s.post(TUE)
            out["text"] = keep(text)
            with w.bot.db.conn() as c:
                rows = c.execute("SELECT kind, proposed_text, payload FROM write_proposals").fetchall()
            out["proposals"] = [(r[0], r[1]) for r in rows]
            out["payload"] = rows[0][2] if rows else ""
            if not allowed:
                return out
            out["member"] = await s.say("yes", who="member", reply_to=post)
            out["after_member"] = [x for x in w.sheet.writes if x[0] == "write_email"]
            out["approver"] = await s.say("yes", who="approver", reply_to=post)
            out["writes"] = sorted((x[1], x[2]) for x in w.sheet.writes if x[0] == "write_email")
            out["other_writes"] = [x for x in w.sheet.writes if x[0] != "write_email"]
        return out

    with pinned():
        got = asyncio.run(scenario(True))
        off = asyncio.run(scenario(False))
    check("ONE proposal, kind email_write, carrying every address the post showed and no other",
          [k for k, _t in got["proposals"]], ["email_write"])
    text = got["proposals"][0][1] if got["proposals"] else ""
    check("the proposal text names both of Tara's addresses and says they go in one cell, one a line",
          ("tara@twinlabs.ai and tara.two@gmail.com (both, one a line in the one cell) to Tara Two (Twin Labs)"
           in text, "joel@artpark.in to Joel Georgie J. (ARTPARK)" in text,
           "mouparna.sen@gnani.ai to Mouparna Sen (Gnani.ai)" in text), (True, True, True))
    check("...and no address that was already on the sheet is in it",
          [a for cell in EXISTING.values() for a in email_checks.addresses_in(cell)
           if a in text or a in got["payload"]], [])
    check("a yes from somebody who is not an approver writes nothing", got["after_member"], [])
    check("an approver's yes writes each found address into that person's Email cell, two as two lines",
          got["writes"], [(4, "joel@artpark.in"), (3, "mouparna.sen@gnani.ai"),
                          (8, "tara@twinlabs.ai\ntara.two@gmail.com")][::1] and sorted(
              [(4, "joel@artpark.in"), (3, "mouparna.sen@gnani.ai"), (8, "tara@twinlabs.ai\ntara.two@gmail.com")]))
    check("...and nothing else on the sheet", got["other_writes"], [])
    check("EMAIL_WRITE_ALLOWED=false: the found addresses are still shown, but there is no closing question "
          "and no proposal",
          ("joel@artpark.in" in off["text"], wording.LI_NO_DM_ADD_QUESTION in off["text"], off["proposals"]),
          (True, False, []))
    one = gtm_sheet.SHEETS.write_email.__doc__ or ""
    check("the write fence is unchanged apart from taking two addresses on two lines",
          ("up to three of them" in one, "THE CELL IS STILL BLANK" in one), (True, True))


def never_in_the_message(check):
    print("\nNEVER IN THE MESSAGE — an address from the sheet, a column name, a DM nudge")
    check("something was rendered to look at", len(RENDERED) > 15, True)
    existing = sorted({a for cell in EXISTING.values() for a in email_checks.addresses_in(cell)} | {
        "fine1@fine.co", "percy.p@gmail.com", "mina@globex.com", "dee@dupe.co", "ollie@old.co"})
    check("no rendered message contains any address that was already in the sheet",
          [(a, t[:40]) for t in RENDERED for a in existing if a.lower() in t.lower()], [])
    for name in COLUMN_NAMES:
        check(f'"{name}" appears in no rendered message and no fixed line',
              [t[:40] for t in RENDERED + wording.li_no_dm_lines_for_check() if name in t], [])
    check("no claim that an address exists or does not, and none of the old DM openers",
          [w for t in RENDERED for w in ("doesn't exist", "does not exist", "is valid", "verified", "this week",
                                          "still remember the name", "un-messaged", "Email on file")
           if w in t], [])
    check("the old composed templates for R6 are gone and its post is rendered in code",
          (nextaction.R_LI_NO_DM in drip._ASK, nextaction.R_LI_NO_DM in drip.VERBATIM_TYPES,
           drip.HEADINGS["R6"], nextaction.TYPE_LABELS[nextaction.R_LI_NO_DM]),
          (False, True, "Connected, no DM yet", "Connected, no DM yet"))


def settings(check):
    print("\nSETTINGS — the four new ones, the boot check, the rules file")
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    check("the four new settings, with the same defaults in the code and in .env.example",
          (bool(re.search(r'_int\("LI_NO_DM_EMAIL_NAMES_PER_POST", 5\)', src)),
           _example("LI_NO_DM_EMAIL_NAMES_PER_POST"),
           bool(re.search(r'_str_list\("LI_NO_DM_DM_SENT_MARKERS", "sent"\)', src)),
           _example("LI_NO_DM_DM_SENT_MARKERS"),
           bool(re.search(r'"LI_NO_DM_DM_REPLIED_MARKERS", "replied,received"', src)),
           _example("LI_NO_DM_DM_REPLIED_MARKERS"),
           bool(re.search(r'_bool\("LI_NO_DM_VERIFY_MX", default=True\)', src)), _example("LI_NO_DM_VERIFY_MX")),
          (True, "5", True, "sent", True, "replied,received", True, "true"))
    check("EMAIL_WRITE_ALLOWED is still false in the code and in .env.example",
          (bool(re.search(r'_bool\("EMAIL_WRITE_ALLOWED", default=False\)', src)), _example("EMAIL_WRITE_ALLOWED")),
          (True, "false"))

    def boot(**values):
        tap = LogTap()
        with pinned(**values):
            saved = config.NEXT_ACTION_ENABLED
            config.NEXT_ACTION_ENABLED = True
            logging.getLogger().addHandler(tap)
            was = logging.getLogger().level
            logging.getLogger().setLevel(logging.INFO)
            try:
                config.validate()
            finally:
                logging.getLogger().removeHandler(tap)
                logging.getLogger().setLevel(was)
                config.NEXT_ACTION_ENABLED = saved
        return ([m for lvl, m in tap.lines if lvl == "WARNING" and "EMAIL_WRITE_ALLOWED=false while R6" in m],
                [m for _l, m in tap.lines if "[config] R6 connected, no DM yet" in m])

    warn, line = boot(EMAIL_WRITE_ALLOWED=False)
    check("R6 on and EMAIL_WRITE_ALLOWED=false: the boot check warns that the question will never appear",
          [("closing question" in m and "will never appear" in m) for m in warn], [True])
    check("with it true there is no such warning", boot(EMAIL_WRITE_ALLOWED=True)[0], [])
    check("the boot line prints the caps and the marker lists",
          [("sent=sent" in m, "replied=replied,received" in m, "share 5 name(s)" in m, "MX check=True" in m)
           for m in line], [(True, True, True, True)])
    rule = rules_mod.by_id("R6")
    check("R6: Tuesday and Friday, five in the plain list, counted toward the cap",
          (rule.weekdays, rule.max_items_per_post, rule.counts_toward_cap), ((1, 4), 5, True))
    wordy = str(getattr(rule, "sheet_wording", "") or "")
    check("the Bot Rules wording says what the rule now does",
          [w for w in ("Tuesday and Friday", "3 days", "5 names", "only adds", "until the DM is logged")
           if w not in wordy], [])


def run(check):
    with pinned():
        who_is_picked(check)
    the_message(check)
    the_caps(check)
    gina_and_the_checks(check)
    repeats_and_all_clear(check)
    the_add(check)
    never_in_the_message(check)
    settings(check)

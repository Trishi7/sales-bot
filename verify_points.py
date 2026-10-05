"""R4 AS ONE MONDAY LIST, R10 AS POINTS, AND THE STRUCTURE RULE — real output.

    python verify_points.py

Nothing reaches Discord, the sheet or the API. The REAL rule engine
(`nextaction.run`), the REAL planner (`drip.plan`), the REAL composer check
(`llm.proactive_message` with the Anthropic client faked) and the REAL send
path (`_send_drip_message`, into a recording channel) run against a test
deliverables tab and a throwaway *_test.db.

  (i)   a Monday preview from a test sheet with 6 open rows across 3 teams and
        mixed priorities — ONE message, 6 numbered lines, P1 first, team and
        date on every line, remarks where present;
  (ii)  a row with a blank team shows the default owner;
  (iii) a composed message missing a bullet falls back to the template and
        logs `structure` (and so does prose with 3+ facts and no points);
  (iv)  an R10 message renders as points with the supportive close;
  plus  a 20-item week longer than 2000 characters goes out as consecutive
        messages split between lines, counted as ONE drip slot.
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="saley-points-")
os.environ["DB_PATH"] = os.path.join(TMP, "sales_bot_test.db")

import config  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]
config.SALES_TEST_MODE = False            # the real send path, not the test prefix
config.SALES_CHANNEL_IDS = [4242]
config.SALES_CHANNEL_ID_SET = {4242}
config.DRIP_LLM_COMPOSE = True
config.DELIVERABLE_DEFAULT_OWNER = "Vaishnavi"
config.CLOSURE_SUPPORT_MIN = 50
config.SALEY_LENGTH = "short"

import deadlines as dl  # noqa: E402
import drip  # noqa: E402
import llm as llm_mod  # noqa: E402
import nextaction  # noqa: E402
import rules as rules_mod  # noqa: E402
import tone  # noqa: E402

# THE FIRST VARIANT OF EVERY LINE, so an exact sentence can be asserted.
tone.pin(0)

failures = 0
LOG: list = []


def check(name, got, want=True):
    global failures
    ok = (got == want)
    failures += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


class Capture(logging.Handler):
    def emit(self, record):
        LOG.append(record.getMessage())


logging.basicConfig(level=logging.WARNING, format="      log  %(name)s: %(message)s")
logging.getLogger().addHandler(Capture())

MON = date(2026, 10, 5)                   # a Monday; the week ends Sun 11 Oct

# THE TEST SHEET: 6 open rows, 3 teams (+ one blank), mixed priorities — and
# three rows R4 must NOT pick up (done, next week, no deadline).
SHEET = [
    {"_row": 2, "_extra": {}, "action_item": "Pulse product overview doc", "priority": "P2",
     "status": "In progress", "deadline": "9-Oct", "dependency": "Sales",
     "remarks": "waiting on the pricing table"},
    {"_row": 3, "_extra": {}, "action_item": "MSA template", "priority": "P1",
     "status": "", "deadline": "30-Sep", "dependency": "Legal",
     "link": "https://docs.google.com/document/d/msa"},
    {"_row": 4, "_extra": {}, "action_item": "API rate limits", "priority": "P1",
     "status": "Not started", "deadline": "8-Oct", "dependency": "Engineering",
     "remarks": "needs the load test first"},
    {"_row": 5, "_extra": {}, "action_item": "Dashboard SSO", "priority": "P3",
     "status": "", "deadline": "11-Oct", "dependency": "Engineering"},
    {"_row": 6, "_extra": {}, "action_item": "Case study: Hinglish STT", "priority": "P2",
     "status": "", "deadline": "6-Oct", "dependency": "Sales"},
    {"_row": 7, "_extra": {}, "action_item": "DPA review", "priority": "P1",
     "status": "", "deadline": "7-Oct", "dependency": ""},
    {"_row": 8, "_extra": {}, "action_item": "NDA", "priority": "P1",
     "status": "Done", "deadline": "6-Oct", "dependency": "Legal"},
    {"_row": 9, "_extra": {}, "action_item": "Q1 plan", "priority": "P1",
     "status": "", "deadline": "14-Oct", "dependency": "Sales"},
    {"_row": 10, "_extra": {}, "action_item": "Some idea", "priority": "P2",
     "status": "", "deadline": "", "dependency": "Sales"},
]


def r4_message(sheet):
    out = nextaction.run(today=MON, rows=[], deliverables=sheet,
                         day_rules=[rules_mod.by_id("R4")])
    planned = drip.plan(out["actions"], day=MON, history={}, already_sent=[], cap=5)
    return out, planned


class FakeMessages:
    def __init__(self):
        self.reply = ""

    def create(self, **_kw):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.reply)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def fake_engine():
    engine = llm_mod.LLM("sk-fake-not-used", config.MODEL)
    fm = FakeMessages()
    engine._client = SimpleNamespace(messages=fm)
    return engine, fm


async def main():
    # ------------------------------------------------------------------ (i)
    print("(i) MONDAY 5 OCT — the week's deliverables from a 9-row test sheet")
    out, planned = r4_message(SHEET)
    msgs = [m for m in planned["messages"] if m["type"] == nextaction.R_DELIVERABLES]
    check("ONE message, whatever the teams", len(msgs), 1)
    msg = msgs[0]
    body = drip.compose_fallback(msg, address="Vaishnavi")
    print("   the template (DRIP_LLM_COMPOSE off, or the model's version rejected):")
    for line in body.splitlines():
        print("     | " + line)
    numbered = [l for l in body.splitlines() if re.match(r"^\d+\. ", l)]
    check("6 numbered lines", len(numbered), 6)
    # THE THREE-LINE LAYOUT: each point is its numbered line plus the indented
    # lines under it (team | due | overdue, the link, the remarks).
    blocks, cur = [], None
    for l in body.splitlines():
        if re.match(r"^\d+\. ", l):
            cur = [l]
            blocks.append(cur)
        elif cur is not None and l.startswith("   "):
            cur.append(l)
    blocks = ["\n".join(b) for b in blocks]
    check("P1 first (MSA, DPA, API rate limits), then by deadline",
          [l.split(". ", 1)[1].split(" — ")[0] for l in numbered],
          ["MSA template", "DPA review", "API rate limits", "Case study: Hinglish STT",
           "Pulse product overview doc", "Dashboard SSO"])
    check("team and due on every point",
          all("Team: " in b and "Due: " in b for b in blocks))
    check("remarks where present",
          sum(("pricing table" in b) or ("load test" in b) for b in blocks), 2)
    check("the overdue one says so", "Overdue: 5 days" in blocks[0])
    check("the link only where there is one, masked",
          sum("](<https://" in b for b in blocks), 1)
    check("done / next week / undated rows are left out",
          any(x in body for x in ("NDA", "Q1 plan", "Some idea")), False)
    check("addressed to the checklist's owner", msg["owner"], "Vaishnavi")

    engine, fm = fake_engine()
    points = drip.points_of(msg)
    fm.reply = ("Vaishnavi — here's where the week's deliverables stand.\n"
                + drip.points_block(points)
                + "\nShout if any of these have moved and I'll update my list.")
    text, used = await engine.proactive_message(
        prompt=drip.compose_prompt(msg, address="Vaishnavi"), fallback=body,
        facts=drip.fact_count(msg), required_lines=drip.required_lines(msg))
    check("a composition that keeps every line verbatim is used", used)
    print("   the model's version (opener + verbatim block + close):")
    for line in text.splitlines()[:2] + ["     …"] + text.splitlines()[-1:]:
        print("     | " + line if not line.startswith("     …") else line)
    prompt = drip.compose_prompt(msg, address="Vaishnavi")
    check("the prompt hands the block over verbatim",
          all(l in prompt for l in points["lines"]))

    # ------------------------------------------------------------------ (ii)
    print("\n(ii) A BLANK TEAM")
    dpa = next(b for b in blocks if "DPA review" in b)
    print(f"   {dpa}")
    check("shows the default owner", "Team: Vaishnavi |" in dpa)

    # ------------------------------------------------------------------ (iii)
    print("\n(iii) THE MODEL DROPS A BULLET")
    LOG.clear()
    fm.reply = ("Vaishnavi — here's the week.\n" + points["header"] + "\n"
                + "\n".join(points["lines"][:3] + points["lines"][4:])
                + "\nShout if any of these have moved.")
    text, used = await engine.proactive_message(
        prompt=prompt, fallback=body, facts=drip.fact_count(msg),
        required_lines=drip.required_lines(msg))
    rejected = [l for l in LOG if "rejected the composed message" in l]
    print("   log: " + (rejected[0][:170] if rejected else "(nothing logged)"))
    check("the template goes instead", (text, used), (body, False))
    check("logged as structure", bool(rejected) and "(structure:" in rejected[0])

    LOG.clear()
    prose_msg = {"type": nextaction.R_PROSPECTS, "companies": ["Acme", "Globex", "Initech"],
                 "actions": [{"company": c} for c in ("Acme", "Globex", "Initech")]}
    fm.reply = "Kushal — Acme, Globex and Initech still need a first contact. No rush."
    text, used = await engine.proactive_message(
        prompt="x", fallback="TEMPLATE", facts=drip.fact_count(prose_msg))
    # A SOFT FAILURE NOW: one retry with the complaint, and only then the template.
    soft = [l for l in LOG if "soft failure (structure:" in l]
    again = [l for l in LOG if "the retry failed too (structure:" in l]
    for l in soft + again:
        print("   log: " + l[:170])
    check("prose with 3 facts and no points gets ONE retry, logged as structure",
          (len(soft), "retry 1 of 1" in (soft or [""])[0]), (1, True))
    check("...and falls back to the template when the retry does it again",
          (text, used, len(again)), ("TEMPLATE", False, 1))

    # ------------------------------------------------------------------ (iv)
    print("\n(iv) R10 — CLOSURE SUPPORT AS POINTS")
    deals = [
        {"_row": 20, "_extra": {}, "company": "Sarvam AI", "name": "Pratyush Kumar",
         "designation": "Co-founder", "prospect_status": "Quote", "closure_prob": "70%"},
        {"_row": 21, "_extra": {}, "company": "Krutrim", "name": "Ravi Jain",
         "designation": "Head of Data", "prospect_status": "Demo", "closure_prob": "60%"},
    ]
    out10 = nextaction.run(today=MON, rows=deals, day_rules=[rules_mod.by_id("R10")])
    plan10 = drip.plan(out10["actions"], day=MON, history={}, already_sent=[], cap=5)
    m10 = next(m for m in plan10["messages"] if m["type"] == nextaction.R_CLOSURE_SUPPORT)
    b10 = drip.compose_fallback(m10, address="Vaishnavi")
    for line in b10.splitlines():
        print("     | " + line)
    check("the deals are points", sum(l[:2] in ("1.", "2.") for l in b10.splitlines()), 2)
    check("the supportive offer is the close",
          b10.splitlines()[-1], drip.CLOSURE_CLOSE)
    check("each item carries the supportive sentence",
          # (web-pending items carry the research marker after it)
          (
              "is in the closure stage — anything I can pull together to help it "
              "along: the PoC's background, the company, a package summary? Say the word.")
          in out10["actions"][0]["text"])
    one = drip.compose_fallback({**m10, "actions": m10["actions"][:1],
                                 "companies": m10["companies"][:1]}, address="Vaishnavi")
    print(f"   one deal: {one}")

    # ------------------------------------------------------------------ split
    print("\n(+) TWENTY ITEMS, ONE SLOT")
    big = [dict(SHEET[1], _row=100 + i, action_item=f"Deliverable number {i + 1}",
                deadline="9-Oct", dependency=["Sales", "Legal", "Engineering"][i % 3],
                remarks="a remark long enough to push the whole list past Discord's "
                        "two thousand character ceiling", priority=f"P{1 + i % 3}")
           for i in range(20)]
    _o, bigplan = r4_message(big)
    bigmsg = bigplan["messages"][0]
    from bot import SalesBot
    from db import DB
    import leave

    bot = SalesBot()
    bot.db = DB(config.DB_PATH)
    config.DRIP_LLM_COMPOSE = False
    sent = []

    class Chan:
        id = 4242
        guild = SimpleNamespace(id=1)
        name = "sales"

        async def send(self, text):
            sent.append(text)
            return SimpleNamespace(id=len(sent), jump_url="https://discord/x")

    async def nobody_away(*_a, **_k):
        return {}

    async def no_evidence(_m):
        return None

    leave.who_is_away = nobody_away
    bot._convert_if_already_done = no_evidence
    await bot._send_drip_message(Chan(), bigmsg, marker=dl.iso(MON), channel_id=4242)
    lens = [len(t) for t in sent]
    print(f"   sent {len(sent)} message(s), lengths {lens}")
    check("more than one message", len(sent) > 1)
    check("each under Discord's 2000", all(n <= 2000 for n in lens))
    joined = "\n".join(sent)
    check("no numbered line was split", sum(
        1 for l in joined.splitlines() if re.match(r"^(\[TEST\] )?\d+\. ", l)), 20)
    check("ONE drip slot recorded", len(bot.db.drip_sent_today(dl.iso(MON))), 1)


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)

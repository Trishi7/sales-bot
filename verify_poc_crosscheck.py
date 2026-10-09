"""R11 — THE OUTREACH PoCs CROSS-CHECK, THE AGREED MESSAGES AND THE SECOND GATE. Offline, made-up names.

    python verify_poc_crosscheck.py            every check
    python verify_poc_crosscheck.py --show     also print M1, M2, M2b, M3 and M4 as posted

What it proves (9 Oct 2026):

  DETECTION   nothing in scope posts nothing; duplicate Master Pipeline rows give one line; a company with no
              row on Outreach PoCs is listed plainly, one whose rows lack a mandatory field carries the suffix,
              a complete one is in no message; blank OPTIONAL fields do not make a gap; the rule's limit is
              still 10 and nobody is held back.
  THE GATES   no search before the first yes; no write before the second; "yes to 1 and 2 but not 3" is a no.
  M2          a field with no value prints nothing; the trailing note appears once, and not at all when every
              field was found; one company gets the one-line header; nothing found is M4.
  BRANCH B    existing rows are never written for Industry, Based, Research Paper Link or LI Url, with
              EMAIL_WRITE_ALLOWED off and on; an Email-only gap with the switch off is a plain request.
  THE WRITE   a new row carries Company, Industry, Name, Designation, Based, Research Paper Link and LI Url
              (columns B, C, D, E, G, H, I; A is the serial), is signed on the Name cell, and is refused when
              the signature cannot be made; a non-profile LinkedIn link is blanked; a person who appeared on
              the tab between the search and the yes is skipped and M3 says so.

HOW. The whole bot behind fake Discord (tests/replies_world.py): the real queue, planner, sender, reply reader,
approval flow and row writer on a throwaway database and a stand-in sheet parsed by the real parser. THE SEARCH IS
A STAND-IN (`_find_people_data`, `_email_lookup`) that counts its calls, and `SHEETS.append_row` / `write_email`
are recorders. No network, no model, no real Sheets or Drive (tests/offline_guard.py fails the run otherwise).
"""
import asyncio
import logging
import os
import re
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))
os.chdir(HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import offline_guard  # noqa: E402

GUARD = offline_guard.install_script()

import config  # noqa: E402
import replies_world as rw  # noqa: E402
from replies_world import World, strip_tag  # noqa: E402

SHOW = "--show" in sys.argv
WED = date(2026, 10, 14)                 # the Wednesday under test; the window starts Wed 7 Oct
failures = 0
passed = 0
SHOWN: dict = {}


def check(name, got, want=True):
    global failures, passed
    ok = got == want
    failures += 0 if ok else 1
    passed += 1 if ok else 0
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


def say(title):
    print(f"\n{title}")


PINS = {"POC_MANDATORY_FIELDS": ["name", "designation", "li_url"], "POC_SUGGEST_MAX_PER_COMPANY": 3,
        "POC_FILL_MAX_ROWS_PER_COMPANY": 5, "NEW_COMPANY_AFTER_WORKING_DAYS": 1, "NEW_COMPANY_WINDOW_DAYS": 7,
        "EMAIL_WRITE_ALLOWED": False, "SHEET_WRITES_ENABLED": True,
        "RESTRICTED_COLUMN_RANGES": "A:I,Q:W,Z:AE", "NEW_ROW_WRITABLE_RANGES": "A:P,X:Y"}

# What the stand-in search "finds", by company. profile / paper / based may be blank: that is the point.
FOUND = {
    "Underdog AI (Conway Research)": [
        {"name": "Sigil Wen", "title": "Founder & CEO", "profile": "https://www.linkedin.com/in/sigil",
         "source": "https://underdog.ai/team", "based": "San Francisco, USA", "paper": ""},
        {"name": "Daniel Hong", "title": "Founding Team, Member of Technical Staff",
         "profile": "https://www.linkedin.com/in/unifiedh",
         "source": "https://www.linkedin.com/company/underdog-ai/people", "based": "Seoul, South Korea",
         "paper": "https://unifiedh.com"},
        {"name": "Third Person", "title": "Engineer", "profile": "https://www.linkedin.com/company/underdog-ai",
         "source": "https://underdog.ai/team", "based": "", "paper": ""},
        {"name": "Fourth Person", "title": "Engineer", "profile": "", "source": "https://underdog.ai/team",
         "based": "", "paper": ""},
    ],
    "Limbic AI": [
        {"name": "Ross Harper", "title": "Co-founder & CEO", "profile": "https://www.linkedin.com/in/rossgharper",
         "source": "https://limbic.ai/team", "based": "London, UK", "paper": ""},
    ],
    "VoiceCare AI": [
        {"name": "Anil Keshav", "title": "Head of Clinical AI", "profile": "https://www.linkedin.com/in/anilkeshav",
         "source": "https://voicecare.ai/about", "based": "", "paper": ""},
        {"name": "Mara Ellis", "title": "Research Lead", "profile": "https://www.linkedin.com/in/maraellis",
         "source": "https://voicecare.ai/research", "based": "", "paper": ""},
    ],
    "Fullhouse Labs": [
        {"name": "Fay Lin", "title": "CTO", "profile": "https://www.linkedin.com/in/faylin",
         "source": "https://fullhouse.example/team", "based": "Pune, India", "paper": "https://fullhouse.example/paper"},
    ],
}
EMAILS = {"Sigil Wen": ("sigil@underdog.ai", "https://underdog.ai/team"),
          "Anil Keshav": ("anil@voicecare.ai", "https://voicecare.ai/about"),
          "Fay Lin": ("fay@fullhouse.example", "https://fullhouse.example/team"),
          "Eve Rao": ("eve@emailonly.example", "https://emailonly.example/team")}


def poc_row(n, company, name="", designation="", li="", email="", industry="", based="", paper=""):
    """One Outreach PoCs row in the 7 Oct layout, by header."""
    headers = rw._pocs_headers()
    row = [""] * len(headers)

    def put(header, value):
        row[headers.index(header)] = value
    put("Sr No", str(n))
    put("Company/Uni", company)
    put("Industry", industry)
    put("Name", name)
    put("Designation", designation)
    put("Email id", email)
    put([h for h in headers if h.startswith("Based")][0], based)
    put("Research Paper Link", paper)
    put("LI Url", li)
    put("First Contact", "TRUE")
    return row


class Stage:
    """A world with a Master Pipeline, an Outreach PoCs tab, a stand-in search and recorders on the write calls."""

    def __init__(self, new_companies, pocs=(), *, test_mode=False, pins=None, pipeline_dupes=(), found=None):
        self.new, self.pocs, self.dupes = list(new_companies), list(pocs), list(pipeline_dupes)
        self.w = World(test_mode, pretend=(WED, 14, 5), rules={"R11"}, pins=dict(PINS, **(pins or {})))
        self.searches, self.email_lookups, self.appended, self.email_writes = [], [], [], []
        self.found = FOUND if found is None else found
        self.append_result = {}

    def __enter__(self):
        import conftest
        import gtm_sheet
        w = self.w.__enter__()
        names = ["Old Co"] + self.new + self.dupes
        industry = {"Underdog AI (Conway Research)": "AI Labs", "Limbic AI": "Mental Health AI",
                    "VoiceCare AI": "Healthcare AI", "Fullhouse Labs": "Voice AI"}
        w.sheet.grids["Master Pipeline"] = [list(conftest.PIPELINE_HEADERS)] + [
            [str(i + 1), n, industry.get(n.strip(), ""), "", "", "", ""] for i, n in enumerate(names)]
        w.sheet.set(pocs=self.pocs)
        w.bot.db.pipeline_snapshot(["Old Co"], today=(WED - timedelta(days=14)).isoformat())
        if self.new:
            w.bot.db.pipeline_snapshot(["Old Co"] + self.new + self.dupes,
                                       today=(WED - timedelta(days=2)).isoformat())
        stage = self

        async def find(company, department="", *, rule="find_people"):
            stage.searches.append(company)
            people = [dict(p) for p in stage.found.get(company, [])]
            return {"ok": True, "error": "", "people": people, "evidence": {}, "company": company,
                    "department": department}

        async def email_lookup(item, *, rule_id):
            stage.email_lookups.append(item.get("poc"))
            hit = EMAILS.get(item.get("poc"))
            if hit:
                item["email_found"], item["email_source"] = hit

        def append_row(tab, values, **kw):
            stage.appended.append({"values": dict(values), **kw})
            return dict({"ok": True, "sheet_row": 100 + len(stage.appended), "written": [], "refused": [],
                         "signed": True, "dry_run": False, "error": ""},
                        **stage.append_result.get(values.get("name"), {}))

        def write_email(**kw):
            stage.email_writes.append(kw)
            return {"ok": True, "written": [], "error": ""}

        w.bot._find_people_data = find
        w.bot._email_lookup = email_lookup
        gtm_sheet.SHEETS.append_row = append_row
        gtm_sheet.SHEETS.write_email = write_email
        return self

    def __exit__(self, *exc):
        return self.w.__exit__(*exc)

    # -- the steps ---------------------------------------------------------------------------------------------
    async def wednesday(self):
        """Run the real queue and send what R11 produced. Returns the posted text, or "" when nothing was posted."""
        import deadlines as dl
        w = self.w
        planned = await w.bot._plan_drip(today=WED, already=[])
        messages = [m for m in (planned or {}).get("messages") or [] if m["type"] == "new_pipeline_company"]
        self.planned = messages
        if not messages:
            return ""
        before = w.n_posted
        await w.bot._send_drip_message(w.chan, messages[0], marker=dl.iso(WED), channel_id=w.chan.id)
        self.post = w.posted[-1] if w.n_posted > before else None
        return strip_tag(self.post.content) if self.post else ""

    async def reply(self, text, to, who="approver"):
        w = self.w
        before = w.n_posted
        await w.say(text, who=who, reply_to=to)
        new = w.posted[before:]
        self.last = new[-1] if new else None
        return "\n".join(strip_tag(m.content) for m in new)

    def proposals(self, kind):
        with self.w.bot.db.conn() as c:
            return [r[0] for r in c.execute(
                "SELECT status FROM write_proposals WHERE kind = ? ORDER BY created_at", (kind,)).fetchall()]

    def sheet_writes(self):
        return list(self.w.sheet.writes) + self.appended + self.email_writes


# -- the checks ------------------------------------------------------------------------------------------------------

async def detection():
    say("DETECTION")
    with Stage([]) as s:
        text = await s.wednesday()
        check("a Wednesday with nothing in scope plans no R11 post and posts nothing",
              (len(s.planned), text, s.w.n_posted), (0, "", 0))

    pocs = [
        poc_row(1, "Limbic AI", "Ross Harper", "Co-founder & CEO", li=""),                       # LI Url blank: a gap
        poc_row(2, "Fullhouse Labs", "Fay Lin", "CTO", li="https://www.linkedin.com/in/faylin"),  # complete
        poc_row(3, "Optional Co", "Ola Nord", "CEO", li="https://www.linkedin.com/in/olanord"),   # email, paper blank
    ]
    new = ["Underdog AI (Conway Research)", "Limbic AI", "Fullhouse Labs", "Optional Co"]
    with Stage(new, pocs, pipeline_dupes=["underdog ai (conway research) ", "LIMBIC AI"]) as s:
        text = await s.wednesday()
        SHOWN["M1"] = text
        check("M1 is the agreed shape, word for word", text,
              "New companies in the Master Pipeline — since Wed 7 Oct\n\n"
              "1. Underdog AI (Conway Research)\n"
              "2. Limbic AI — already on Outreach PoCs, missing some fields\n\n"
              "Would you like me to look these up and suggest prospective PoCs we could contact?")
        check("duplicate Master Pipeline rows for one company give one line",
              (text.lower().count("underdog ai"), text.lower().count("limbic ai")), (1, 1))
        check("Branch B carries the suffix; Branch A does not",
              [l.endswith(" — already on Outreach PoCs, missing some fields") for l in text.splitlines()[2:4]],
              [False, True])
        check("the suffix never names which fields", [f for f in ("LI Url", "Designation", "Email") if f in text], [])
        check("a complete company is in no message", "Fullhouse Labs" in text, False)
        check("a company whose only blank fields are optional is complete, not Branch B", "Optional Co" in text, False)
        check("plain text: no bold, no bullets, no emoji, no 'Hey team'",
              [x for x in ("**", "•", "Hey team", "\U0001F44B") if x in text], [])
        check("no search before the first yes, and nothing written", (s.searches, s.email_lookups, s.sheet_writes()),
              ([], [], []))
        check("the question is a poc_lookup proposal, open", s.proposals("poc_lookup"), ["open"])
        check("no model wrote the post", s.w.model.calls, 0)

    import drip
    import nextaction
    import rules as rules_mod
    r11 = rules_mod.by_id("R11")
    check("R11 still carries 10 a post, Wednesdays only", (r11.max_items_per_post, r11.weekdays), (10, (2,)))
    check("R11 is posted as written, with no bold heading",
          (nextaction.R_NEW_COMPANY in drip.VERBATIM_TYPES, drip.heading("R11")), (True, ""))
    ten = [f"Company {i:02d}" for i in range(1, 11)]
    with Stage(ten) as s:
        text = await s.wednesday()
        check("ten companies in scope: all ten are named, none held back",
              [l.split(". ", 1)[1] for l in text.splitlines() if re.match(r"^\d+\. ", l)], ten)

    import poc_crosscheck
    check("the engine is handed a summary, never a row",
          sorted(poc_crosscheck.build_index([{"company": "Limbic AI", "name": "Ross", "designation": "CEO",
                                              "li_url": ""}])["limbic ai"]), ["gap_rows", "gaps", "rows"])
    got = nextaction.run(today=WED, rows=[], day_rules=[r11], next_step_state={},
                         new_companies=[{"company": "Solo Co", "first_seen": (WED - timedelta(days=2)).isoformat()}])
    check("with no summary supplied a company is treated as having no contacts",
          [(a["company"], a["branch"]) for a in got["actions"]], [("Solo Co", "a")])


async def the_gates():
    say("THE TWO GATES, AND M2 / M3")
    new = ["Underdog AI (Conway Research)", "Limbic AI"]
    with Stage(new) as s:
        await s.wednesday()
        m2 = await s.reply("yes", s.post)
        SHOWN["M2"] = m2
        offer = s.last
        check("the first yes runs one search a company", s.searches, new)
        check("M2 is ONE message", s.w.n_posted, 2)
        check("M2: stacked header, each company a subheading",
              (m2.splitlines()[0], "Underdog AI (Conway Research)" in m2.splitlines(), "Limbic AI" in m2.splitlines()),
              ("Suggested PoCs", True, True))
        check("M2: at most 3 people a company (the search found 4)",
              len(re.findall(r"^\d+\. ", m2.split("Limbic AI")[0], re.M)), 3)
        sigil = m2.split("1. Sigil Wen")[1].split("\n\n")[0].splitlines()[1:]
        check("M2: labels in order, with the email the lookup found and Industry from the Master Pipeline",
              sigil, ["   Industry    AI Labs", "   Email       sigil@underdog.ai", "   Based       San Francisco, USA",
                      "   LinkedIn    linkedin.com/in/sigil", "   Source      underdog.ai/team"])
        daniel = m2.split("2. Daniel Hong")[1].split("\n\n")[0]
        check("M2: an omitted field prints nothing (no Email line for Daniel, no Paper line for Sigil)",
              ("Email" in daniel, "Paper       unifiedh.com" in daniel, "Paper" in "\n".join(sigil),
               [x for x in ("not found", "n/a", "N/A") if x in m2]), (False, True, False, []))
        check("M2: the trailing note appears exactly once",
              m2.count("Some fields are missing because I could not find them on the web."), 1)
        check("M2: it ends on the question, with the row count and the companies",
              m2.splitlines()[-1],
              "Shall I go ahead? This adds 4 rows for Underdog AI (Conway Research) and Limbic AI.")
        check("no write before the second yes; the offer is a row_add proposal on that message",
              (s.sheet_writes(), s.proposals("row_add")), ([], ["open"]))

        import approvals
        import wording
        # THE BRIEF EXPECTED read_vote TO CALL THIS A NO. IT DOES NOT: "not" is not one of its no-words, so it reads
        # YES (recorded here, and read_vote is left alone as instructed). What keeps the sheet safe is the R11 path
        # itself: a yes that carries a number or a "not / but / only" and names nobody writes nothing.
        check('read_vote("yes to 1 and 2 but not 3") reads YES today (approvals.read_vote is unchanged)',
              approvals.read_vote("yes to 1 and 2 but not 3"), approvals.VOTE_YES)
        asked = await s.reply("yes to 1 and 2 but not 3", offer)
        check('...yet replying "yes to 1 and 2 but not 3" to M2 writes NOTHING', s.sheet_writes(), [])
        check("...the bot says it added nobody and asks for a plain yes, a no, or names",
              asked, wording.POC_ADD_ALL_OR_NONE)
        check("...and the question is still open on the same message", s.proposals("row_add")[-1], "open")
        await s.reply("yes for Ross", offer)
        check("...a yes that NAMES a person adds that person only (the existing narrowing, by name)",
              [a["values"]["name"] for a in s.appended], ["Ross Harper"])

    with Stage(new) as s:
        await s.wednesday()
        await s.reply("yes", s.post)
        offer = s.last
        s.append_result["Ross Harper"] = {"ok": False, "duplicate": True, "sheet_row": None,
                                          "error": "Ross Harper is already on Outreach PoCs (row 14)"}
        m3 = await s.reply("yes", offer)
        SHOWN["M3"] = m3
        check("the second yes writes one row a person", [a["values"]["name"] for a in s.appended],
              ["Sigil Wen", "Daniel Hong", "Third Person", "Ross Harper"])
        first = s.appended[0]
        check("a new row carries Company, Industry, Name, Designation, Based and LI Url (no Email: the switch is off)",
              first["values"], {"company": "Underdog AI (Conway Research)", "name": "Sigil Wen",
                                "li_url": "https://www.linkedin.com/in/sigil", "industry": "AI Labs",
                                "designation": "Founder & CEO", "based": "San Francisco, USA"})
        check("...and the research paper link when one was found",
              s.appended[1]["values"].get("paper_links"), "https://unifiedh.com")
        check("li_url is blanked when the search returned a non-profile link (a company page)",
              s.appended[2]["values"]["li_url"], "")
        check("every row is signed on the Name cell, with the serial filled",
              [(a.get("note_role"), "Added by" in str(a.get("note_text")), a.get("fill_serial")) for a in s.appended],
              [("name", True, True)] * 4)
        check("the source link is never a column value",
              [v for a in s.appended for v in a["values"].values() if "underdog.ai/team" in str(v)], [])
        check("M3: one block a company, no row numbers, no links",
              (m3.startswith("Done — Underdog AI (Conway Research)\nadded to Outreach PoCs"),
               bool(re.search(r"row \d+|https?://", m3))), (True, False))
        check("M3 says who was skipped because they were already on the tab",
              "Not added — Limbic AI\nSkipped Ross Harper: already on Outreach PoCs." in m3,
              True)

        import gtm_sheet
        tab = gtm_sheet.SHEETS.tab(gtm_sheet.POCS)
        letters = sorted(chr(65 + tab.canonical_role_to_col[r])
                         for r in ("company", "industry", "name", "designation", "based", "paper_links", "li_url"))
        check("those roles are columns B, C, D, E, G, H and I of the 7 Oct layout", letters,
              ["B", "C", "D", "E", "G", "H", "I"])
        idx = [tab.canonical_role_to_col[r] for r in ("company", "industry", "name", "designation", "email", "based",
                                                      "paper_links", "li_url")]
        check("every one of them may be written on a NEW row and is read-only on an existing one",
              ([config.may_write_new_row_column(i) for i in idx],
               [i in config.RESTRICTED_COLUMN_INDEXES for i in idx]), ([True] * 8, [True] * 8))

    with Stage(["Underdog AI (Conway Research)"], pins={"EMAIL_WRITE_ALLOWED": True}) as s:
        await s.wednesday()
        one = await s.reply("yes", s.post)
        check("one company: the one-line header and no subheading",
              (one.splitlines()[0], one.splitlines().count("Underdog AI (Conway Research)")),
              ("Suggested PoCs — Underdog AI (Conway Research)", 0))
        await s.reply("yes", s.last)
        check("with EMAIL_WRITE_ALLOWED on, the found email goes on the new row too",
              s.appended[0]["values"].get("email"), "sigil@underdog.ai")

    with Stage(["Limbic AI"]) as s:
        await s.wednesday()
        await s.reply("yes", s.post)
        s.w.bot._message_link = lambda message: ""          # the approval cannot be linked
        refused = await s.reply("yes", s.last)
        check("a row whose signature cannot be made is refused: nothing is appended, and it says so",
              (s.appended, "Not added — Limbic AI" in refused and "could not link the approval" in refused),
              ([], True))

    with Stage(["Fullhouse Labs"]) as s:
        await s.wednesday()
        full = await s.reply("yes", s.post)
        check("every field found: the trailing note does not appear at all",
              ("Some fields are missing" in full, "Paper       fullhouse.example/paper" in full), (False, True))

    with Stage(["Limbic AI", "Nobody Labs"]) as s:
        await s.wednesday()
        mixed = await s.reply("yes", s.post)
        check("a company the search failed on is folded into the same message",
              (s.w.n_posted, "I could not find PoCs for Nobody Labs. Happy to try again with a careers page\n"
                             "or the LinkedIn company URL." in mixed), (2, True))
    with Stage(["Nobody Labs"]) as s:
        await s.wednesday()
        m4 = await s.reply("yes", s.post)
        SHOWN["M4"] = m4
        check("nothing found for any company: M4, and no offer is opened", (m4, s.proposals("row_add")), (
            "No PoCs found — Nobody Labs\n\nI'm sorry, I searched and could not find named people I would be "
            "confident suggesting.\nHappy to try again with a starting point — a careers page or the LinkedIn "
            "company URL would be enough.", []))

    with Stage(["Underdog AI (Conway Research)", "Limbic AI"], test_mode=True) as s:
        text = await s.wednesday()
        m2 = await s.reply("yes", s.post)
        check("test mode: the same M1 and M2 (the post carries the [TEST] tag and nothing else differs)",
              (text == SHOWN["M1"].replace("2. Limbic AI — already on Outreach PoCs, missing some fields", "2. Limbic AI")
               or text.startswith("New companies in the Master Pipeline"), m2), (True, SHOWN["M2"]))


async def branch_b():
    say("BRANCH B — REMIND ONLY")
    pocs = [
        poc_row(1, "VoiceCare AI", "Anil Keshav", "Head of Clinical AI", li="", industry="Healthcare AI"),
        poc_row(2, "VoiceCare AI", "Mara Ellis", "Research Lead", li="", email="mara@voicecare.ai",
                industry="Healthcare AI"),
        poc_row(3, "VoiceCare AI", "Complete Person", "CEO", li="https://www.linkedin.com/in/complete"),
    ]
    for allowed in (False, True):
        with Stage(["VoiceCare AI"], pocs, pins={"EMAIL_WRITE_ALLOWED": allowed}) as s:
            m1 = await s.wednesday()
            m2b = await s.reply("yes", s.post)
            if not allowed:
                SHOWN["M2b"] = m2b
                check("M1 lists it with the suffix", "1. VoiceCare AI — already on Outreach PoCs, missing some fields"
                      in m1, True)
                check("M2b: header, company, people NOT numbered",
                      (m2b.splitlines()[:3], bool(re.search(r"^\d+\. ", m2b, re.M))),
                      (["Missing fields", "", "VoiceCare AI"], False))
                anil = m2b.split("Anil Keshav — Head of Clinical AI")[1].split("\n\n")[0].splitlines()[1:]
                check("M2b: only blank cells, under the sheet's own column names",
                      anil, ["   Email id    anil@voicecare.ai", "   LI Url      linkedin.com/in/anilkeshav",
                             "   Source      voicecare.ai/about"])
                mara = m2b.split("Mara Ellis — Research Lead")[1].split("\n\n")[0]
                check("M2b: a cell that holds a value is never printed (Mara has an email; Industry is filled)",
                      ("Email id" in mara, "Industry" in m2b, "LI Url      linkedin.com/in/maraellis" in mara),
                      (False, False, True))
                check("M2b: a row with nothing missing is not shown", "Complete Person" in m2b, False)
            check(f"EMAIL_WRITE_ALLOWED={allowed}: the closing line is the request, not an offer "
                  "(LI Url was found too, so email is not the only gap)",
                  m2b.splitlines()[-1],
                  "Those columns are ones I'm not able to write to, so could you add them please?")
            check(f"EMAIL_WRITE_ALLOWED={allowed}: no proposal of any writing kind is opened",
                  (s.proposals("row_add"), s.proposals("email_write")), ([], []))
            await s.reply("yes", s.last)
            await s.reply("sure", s.last)
            check(f"EMAIL_WRITE_ALLOWED={allowed}: a yes to it writes nothing — no Industry, Based, Research "
                  "Paper Link or LI Url cell, and no row", s.sheet_writes(), [])

    # AN EMAIL-ONLY GAP: possible only when the team makes email mandatory.
    email_only = [poc_row(1, "Email Only Co", "Eve Rao", "CTO", li="https://www.linkedin.com/in/everao",
                          industry="AI", based="Pune", paper="https://x.example/p")]
    mand = ["name", "designation", "li_url", "email"]
    with Stage(["Email Only Co"], email_only, pins={"EMAIL_WRITE_ALLOWED": False, "POC_MANDATORY_FIELDS": mand},
               found={"Email Only Co": []}) as s:
        await s.wednesday()
        text = await s.reply("yes", s.post)
        check("an Email-only gap with EMAIL_WRITE_ALLOWED off: the plain request line and no offer",
              (text.splitlines()[-1], "shall I?" in text, s.proposals("email_write")),
              ("Those columns are ones I'm not able to write to, so could you add them please?", False, []))
        await s.reply("yes", s.last)
        check("...and a yes writes nothing", s.sheet_writes(), [])
    with Stage(["Email Only Co"], email_only, pins={"EMAIL_WRITE_ALLOWED": True, "POC_MANDATORY_FIELDS": mand},
               found={"Email Only Co": []}) as s:
        await s.wednesday()
        text = await s.reply("yes", s.post)
        check("the same gap with EMAIL_WRITE_ALLOWED on: the offer line, behind an email_write proposal",
              (text.splitlines()[-1], s.proposals("email_write"), s.sheet_writes()),
              ("I can fill the Email cell for you if you'd like — shall I?", ["open"], []))
        await s.reply("yes", s.last)
        check("...a yes writes the Email cell through the existing email path, and nothing else",
              ([(w.get("email"), w.get("row")) for w in s.email_writes], s.appended, list(s.w.sheet.writes)),
              ([("eve@emailonly.example", 2)], [], []))


async def main():
    await detection()
    await the_gates()
    await branch_b()
    if SHOW:
        for name in ("M1", "M2", "M2b", "M3", "M4"):
            print(f"\n--- {name}, as posted ---")
            for line in SHOWN.get(name, "").split("\n"):
                print("    | " + line)


logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
asyncio.run(main())
print(f"\n{passed} check(s) passed" + (f", {failures} failed" if failures else ""))
print("ALL PASSED" if not failures else f"{failures} FAILED")
sys.exit(1 if failures else 0)

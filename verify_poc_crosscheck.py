"""R11 — THE OUTREACH PoCs CROSS-CHECK, THE AGREED MESSAGES AND THE SECOND GATE. Offline, made-up names.

    python verify_poc_crosscheck.py            every check
    python verify_poc_crosscheck.py --show     also print M1, the reply to a yes, the follow-up and M3

Includes the 9 Oct fix pass (the live run that offered Sigil Wen, already row 651, as a new row):

  MATCHING    "Underdog AI" on the Master Pipeline finds the rows filed under "Underdog AI (Conway Research)";
              a person already on the sheet is never offered as a new row.
  M1          three states: no suffix; "missing some fields"; "already on Outreach PoCs". Nothing in scope posts
              nothing. Duplicates collapse. Ten in scope, ten named.
  DEDUP       the exclusion list is built BEFORE the search and handed to it; a model that returns an excluded
              person anyway still produces no suggestion; "find a different person" also leaves out who was
              suggested earlier in the thread and uses a DIFFERENT query.
  THE REPLY   a field whose value is "->", "-", "—", "n/a", "unknown" or "not found" is omitted; the note appears
              once, or not at all; every link is masked; Source is dropped when it repeats LinkedIn or Paper;
              Industry comes from the Master Pipeline, on the subheading only; "Thiel Fellow" is not a
              designation; the closing line counts nothing and names nobody; a gaps company gets Missing fields
              only.
  THE GATES   no search before the first yes, no write before the second; a qualified yes writes nothing; a new
              row carries the agreed columns and is signed; an existing row is never written.

HOW. The whole bot behind fake Discord (tests/replies_world.py) on a throwaway database and a stand-in sheet parsed
by the real parser. THE SEARCH IS A STAND-IN (`_find_people_data`, `_email_lookup`) that records what it was asked,
and `SHEETS.append_row` / `write_email` are recorders. No network, no model, no real Sheets or Drive.
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
INDUSTRY = {"Oogam AI": "Voice AI", "Underdog AI": "AI Labs", "Limbic AI": "Mental Health AI",
            "Fullhouse Labs": "Voice AI"}        # "Blank Co" has no Industry on the Master Pipeline


def person(name, title="", profile="", source="", based="", paper=""):
    return {"name": name, "title": title, "profile": profile, "source": source, "based": based, "paper": paper}


LI = "https://www.linkedin.com/in/"
# What the stand-in search "finds": by company, then by the angle of the query (0 = the first search).
FOUND = {
    "Oogam AI": {0: [
        person("Priya Raghavan", "VP Research", LI + "priyaraghavan", "https://oogam.ai/team", "Bengaluru, India"),
        person("Arjun Mehta", "Head of Speech", LI + "arjunmehta", "https://oogam.ai/team", "->"),
        person("Third Person", "Thiel Fellow", "https://www.linkedin.com/company/oogam", LI + "third/", "n/a"),
        person("Fourth Person", "Engineer", "", "https://oogam.ai/team"),
    ], 1: [
        person("Priya Raghavan", "VP Research", LI + "priyaraghavan", "https://oogam.ai/team"),   # shown before
        person("Nisha Rao", "Research Scientist", LI + "nisharao", "https://oogam.ai/about", "Pune, India"),
    ]},
    # The search still returns the two people already on the sheet (a negative term is only a hint).
    "Underdog AI": {0: [
        person("Sigil Wen", "Thiel Fellow", LI + "sigil", LI + "sigil"),
        person("Daniel S. Hong", "Member of Technical Staff", LI + "unifiedh", "https://underdog.ai/team"),
        person("Ana Pereira", "Research Engineer", LI + "anapereira", "https://underdog.ai/team", "Lisbon, Portugal"),
    ], 1: [person("Sigil Wen", "Founder", LI + "sigil", LI + "sigil")]},
    "Limbic AI": {0: [
        person("Ross Harper", "Co-founder & CEO", LI + "rossgharper", "https://limbic.ai/team", "London, UK"),
        person("New Limbic Person", "CTO", LI + "newlimbic", "https://limbic.ai/team"),
    ]},
    "Fullhouse Labs": {0: [person("Fay Lin", "CTO", LI + "faylin", "https://fullhouse.example/team", "Pune, India",
                                  "https://fullhouse.example/paper")]},
    "Blank Co": {0: [person("Bea Koh", "CEO", LI + "beakoh", "https://blank.example/team", "unknown", "not found")]},
}
EMAILS = {"Priya Raghavan": ("priya@oogam.ai", "https://oogam.ai/team"),
          "Fay Lin": ("fay@fullhouse.example", "https://fullhouse.example/team"),
          "Eve Rao": ("eve@emailonly.example", "https://emailonly.example/team")}


def poc_row(n, company, name="", designation="", li="", email="", industry="", based="", paper=""):
    """One Outreach PoCs row in the 7 Oct layout, by header."""
    headers = rw._pocs_headers()
    row = [""] * len(headers)
    for header, value in (("Sr No", str(n)), ("Company/Uni", company), ("Industry", industry), ("Name", name),
                          ("Designation", designation), ("Email id", email),
                          ([h for h in headers if h.startswith("Based")][0], based),
                          ("Research Paper Link", paper), ("LI Url", li), ("First Contact", "TRUE")):
        row[headers.index(header)] = value
    return row


# The live sheet, in miniature: Underdog is filed under a LONGER name than the Master Pipeline uses.
SHEET = [
    poc_row(651, "Underdog AI (Conway Research)", "Sigil Wen", "Founder & CEO", li=LI + "sigil"),
    poc_row(652, "Underdog AI (Conway Research)", "Daniel Hong", "Founding Team", li=LI + "unifiedh"),
    poc_row(700, "Limbic AI", "Ross Harper", "Co-founder & CEO", li=""),
]


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
        w.sheet.grids["Master Pipeline"] = [list(conftest.PIPELINE_HEADERS)] + [
            [str(i + 1), n, INDUSTRY.get(n.strip(), ""), "", "", "", ""] for i, n in enumerate(names)]
        w.sheet.set(pocs=self.pocs)
        w.bot.db.pipeline_snapshot(["Old Co"], today=(WED - timedelta(days=14)).isoformat())
        if self.new:
            w.bot.db.pipeline_snapshot(["Old Co"] + self.new + self.dupes,
                                       today=(WED - timedelta(days=2)).isoformat())
        stage = self

        async def find(company, department="", *, rule="find_people", exclude=(), angle=0):
            import websearch
            stage.searches.append({"company": company, "exclude": list(exclude), "angle": angle,
                                   "query": websearch.people_queries(company, exclude=exclude, angle=angle)[0]["q"],
                                   "prompt": websearch.people_prompt(company, from_snippets=True, exclude=exclude)})
            by_angle = stage.found.get(company, {})
            people = [dict(p) for p in by_angle.get(angle, by_angle.get(0, []) if angle == 0 else [])]
            return {"ok": True, "error": "", "people": people, "evidence": {}, "company": company,
                    "department": department}

        async def email_lookup(item, *, rule_id):
            stage.email_lookups.append(item.get("poc"))
            hit = EMAILS.get(item.get("poc"))
            if hit:
                item["email_found"], item["email_source"] = hit

        def append_row(tab, values, **kw):
            stage.appended.append({"values": dict(values), **kw})
            return dict({"ok": True, "sheet_row": 900 + len(stage.appended), "written": [], "refused": [],
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

    async def wednesday(self):
        """Run the real queue and send what R11 produced. The posted text, or "" when nothing was posted."""
        import deadlines as dl
        w = self.w
        planned = await w.bot._plan_drip(today=WED, already=[])
        self.planned = [m for m in (planned or {}).get("messages") or [] if m["type"] == "new_pipeline_company"]
        if not self.planned:
            return ""
        before = w.n_posted
        await w.bot._send_drip_message(w.chan, self.planned[0], marker=dl.iso(WED), channel_id=w.chan.id)
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
                "SELECT status FROM write_proposals WHERE kind = ? ORDER BY created_at, rowid", (kind,)).fetchall()]

    def sheet_writes(self):
        return list(self.w.sheet.writes) + self.appended + self.email_writes


def dead_links(text: str) -> list:
    """Everything in a message that looks like a link and is NOT masked as [label](<https://…>)."""
    stripped = re.sub(r"\[[^\]\n]+\]\(<https?://[^\s>]+>\)", "", text)
    return re.findall(r"https?://\S+|\b[\w-]+\.(?:com|ai|io|org|example)/\S*", stripped)


def section(text: str, heading: str) -> str:
    """The block of a reply that starts at `heading`, up to the next blank-line-separated block that is a heading."""
    return text.split(heading, 1)[1] if heading in text else ""


# -- the checks ------------------------------------------------------------------------------------------------------

async def m1():
    say("M1 — THREE STATES, AND THE MATCHER")
    with Stage([]) as s:
        text = await s.wednesday()
        check("a Wednesday with nothing in scope plans no R11 post and posts nothing",
              (len(s.planned), text, s.w.n_posted), (0, "", 0))

    new = ["Oogam AI", "Limbic AI", "Underdog AI"]
    with Stage(new, SHEET, pipeline_dupes=["oogam ai ", "LIMBIC AI"]) as s:
        text = await s.wednesday()
        SHOWN["M1"] = text
        check("M1: all three states, in Master Pipeline order, under the one question", text,
              "New companies in the Master Pipeline — since Wed 7 Oct\n\n"
              "1. Oogam AI\n"
              "2. Limbic AI — already on Outreach PoCs, missing some fields\n"
              "3. Underdog AI — already on Outreach PoCs\n\n"
              "Would you like me to look these up and suggest prospective PoCs we could contact?")
        check("'Underdog AI' is matched to the rows filed under 'Underdog AI (Conway Research)' (find_company's "
              "substring path), so it is NOT listed as a company with no contacts",
              "3. Underdog AI — already on Outreach PoCs" in text.splitlines(), True)
        check("duplicate Master Pipeline rows for one company give one line",
              (text.lower().count("oogam ai"), text.lower().count("limbic ai")), (1, 1))
        check("the suffix never names which fields", [f for f in ("LI Url", "Designation", "Email") if f in text], [])
        check("plain text: no bold, no bullets, no 'Hey team'", [x for x in ("**", "•", "Hey team") if x in text], [])
        check("no search before the first yes, and nothing written",
              (s.searches, s.email_lookups, s.sheet_writes()), ([], [], []))
        check("the question is a poc_lookup proposal, open; no model wrote the post",
              (s.proposals("poc_lookup"), s.w.model.calls), (["open"], 0))

    optional_only = [poc_row(1, "Optional Co", "Ola Nord", "CEO", li=LI + "olanord")]     # no email, no paper
    with Stage(["Optional Co"], optional_only) as s:
        text = await s.wednesday()
        check("only OPTIONAL fields blank is not 'missing some fields'",
              text.splitlines()[2], "1. Optional Co — already on Outreach PoCs")

    import drip
    import nextaction
    import rules as rules_mod
    r11 = rules_mod.by_id("R11")
    check("R11 still carries 10 a post, Wednesdays only, posted as written with no bold heading",
          (r11.max_items_per_post, r11.weekdays, nextaction.R_NEW_COMPANY in drip.VERBATIM_TYPES, drip.heading("R11")),
          (10, (2,), True, ""))
    ten = [f"Company {i:02d}" for i in range(1, 11)]
    with Stage(ten) as s:
        text = await s.wednesday()
        check("ten companies in scope: all ten are named, none held back",
              [l.split(". ", 1)[1] for l in text.splitlines() if re.match(r"^\d+\. ", l)], ten)


async def the_reply():
    say("THE REPLY TO A YES — ONE MESSAGE, TWO SECTIONS")
    import poc_crosscheck as pc
    new = ["Oogam AI", "Limbic AI", "Underdog AI"]
    with Stage(new, SHEET) as s:
        await s.wednesday()
        text = await s.reply("yes", s.post)
        SHOWN["reply"] = text
        offer = s.last
        check("ONE message", s.w.n_posted, 2)
        by_company = {x["company"]: x for x in s.searches}
        check("the exclusion list is built BEFORE the search and handed to it, whole",
              (by_company["Underdog AI"]["exclude"], by_company["Oogam AI"]["exclude"]),
              (["Sigil Wen", "Daniel Hong"], []))
        check("...as negative terms in the query", by_company["Underdog AI"]["query"],
              'site:linkedin.com/in "Underdog AI" -"Sigil Wen" -"Daniel Hong"')
        check("...and by name in the extraction prompt",
              "THESE PEOPLE ARE ALREADY ON OUR SHEET. DO NOT RETURN THEM, under any spelling: Sigil Wen; Daniel Hong."
              in by_company["Underdog AI"]["prompt"], True)
        under = section(text, "Underdog AI — AI Labs").split("Missing fields")[0]
        check("the search returned Sigil Wen and 'Daniel S. Hong' anyway: neither is suggested (the post-filter)",
              ("1. Sigil Wen" in text or "Daniel S. Hong" in text, "1. Ana Pereira — Research Engineer" in under),
              (False, True))
        check("the people already there are named, then the new one",
              "Sigil Wen and Daniel Hong are already on the sheet. One other I found:" in under, True)
        check("section headers and company subheadings, Industry from the Master Pipeline on the subheading only",
              [l for l in text.splitlines() if l in ("Suggested PoCs", "Missing fields") or " — " in l
               and not l[0].isdigit() and not l.startswith(" ") and "Ross" not in l],
              ["Suggested PoCs", "Oogam AI — Voice AI", "Underdog AI — AI Labs", "Missing fields",
               "Limbic AI — Mental Health AI"])
        check("no Industry line on any person", "   Industry" in text, False)
        oogam = section(text, "Oogam AI — Voice AI").split("Underdog AI — AI Labs")[0]
        check("at most 3 people a company (the search found 4), numbered",
              re.findall(r"^(\d)\. ", oogam, re.M), ["1", "2", "3"])
        check("labels in order, the email from the lookup, links masked",
              oogam.split("1. Priya Raghavan — VP Research\n")[1].split("\n\n")[0].splitlines(),
              ["   Email       priya@oogam.ai", "   Based       Bengaluru, India",
               "   LinkedIn    [linkedin.com/in/priyaraghavan](<https://www.linkedin.com/in/priyaraghavan>)",
               "   Source      [oogam.ai/team](<https://oogam.ai/team>)"])
        check("a field whose value is '->' is omitted entirely (Arjun has no Based line)",
              oogam.split("2. Arjun Mehta — Head of Speech\n")[1].split("\n\n")[0].splitlines(),
              ["   LinkedIn    [linkedin.com/in/arjunmehta](<https://www.linkedin.com/in/arjunmehta>)",
               "   Source      [oogam.ai/team](<https://oogam.ai/team>)"])
        third = oogam.split("3. Third Person")[1].split("\n\n")[0]
        check("'Thiel Fellow' is not a designation: the title is omitted, not printed", third.splitlines()[0], "")
        check("...'n/a' is omitted, and a company-page link is not shown as a LinkedIn profile",
              ("Based" in third, "LinkedIn" in third), (False, False))
        check("every url in the message is masked: no scheme-less host, no bare https url", dead_links(text), [])
        check("no arrow, no 'n/a', no 'not found', no 'unknown' anywhere",
              [x for x in ("->", "not found", "unknown", "NONE") if x in text]
              + re.findall(r"(?<![\w/])n/a(?!\w)", text, re.I), [])
        check("the trailing note appears exactly once", text.count(pc.NOTE_OMITTED), 1)
        limbic = section(text, "Limbic AI — Mental Health AI")
        check("a gaps company gets Missing fields ONLY: the existing row's blank cell, not numbered, under the "
              "sheet's own column name",
              limbic.split("\n\n")[1].splitlines(),
              ["Ross Harper — Co-founder & CEO",
               "   Based (Sept 2026)  London, UK",
               "   LI Url      [linkedin.com/in/rossgharper](<https://www.linkedin.com/in/rossgharper>)",
               "   Source      [limbic.ai/team](<https://limbic.ai/team>)"])
        check("...and no new person is suggested for it, though the search found one",
              ("New Limbic Person" in text, [p for p in s.searches if p["company"] == "Limbic AI"][0]["exclude"]),
              (False, []))
        check("the two closing lines, last; the add line counts nothing and names no company",
              text.splitlines()[-2:],
              ["Shall I go ahead and add these new people to the Outreach PoCs sheet?",
               "Those Limbic AI columns are ones I'm not able to write to, so could you add them please?"])
        check("...no row count anywhere", bool(re.search(r"This adds|\d+ rows?\b", text)), False)
        check("no write before the second yes; the offer is a row_add proposal on that message",
              (s.sheet_writes(), s.proposals("row_add")), ([], ["open"]))

        # ---- "find a different person", twice --------------------------------------------------------------
        say("'FIND A DIFFERENT PERSON' — THE SAME RENDERER, OTHER PEOPLE, ANOTHER QUERY")
        before = len(s.searches)
        more = await s.reply("find a different person for Oogam", offer)
        SHOWN["more"] = more
        again = s.searches[before:]
        check("it is answered with the same renderer, as 'More PoCs', not the old 'people worth a look' list",
              (more.splitlines()[0], "people worth a look" in more or "Found on:" in more),
              ("More PoCs — Oogam AI", False))
        check("the people suggested in the first reply are excluded before the search",
              again[0]["exclude"], ["Priya Raghavan", "Arjun Mehta", "Third Person"])
        check("...and the query is a DIFFERENT one from the first",
              (again[0]["angle"], again[0]["query"] != by_company["Oogam AI"]["query"],
               again[0]["query"].startswith('"Oogam AI" team OR about')), (1, True, True))
        check("the search returned Priya again: she is not shown again; the new person is",
              ("Priya Raghavan" in more, "1. Nisha Rao — Research Scientist" in more), (False, True))
        check("the follow-up's links are masked and it ends on the same question",
              (dead_links(more), more.splitlines()[-1]),
              ([], "Shall I go ahead and add these new people to the Outreach PoCs sheet?"))
        check("the first list's offer is closed and the new message carries its own",
              s.proposals("row_add"), ["expired", "open"])
        check("still nothing written", s.sheet_writes(), [])
        second = s.last
        before = len(s.searches)
        none_left = await s.reply("anyone else?", second)
        check("a second 'anyone else?' excludes everybody shown so far and moves to a third query",
              (s.searches[before]["exclude"], s.searches[before]["angle"]),
              (["Priya Raghavan", "Arjun Mehta", "Third Person", "Nisha Rao"], 2))
        check("...and with nobody new it says so, with the one try-again close",
              none_left, "No PoCs found — Oogam AI\n\n" + pc.NOBODY + "\n" + pc.TRY_AGAIN)

    with Stage(["Underdog AI"], SHEET) as s:
        await s.wednesday()
        first = await s.reply("yes", s.post)
        more = await s.reply("find a different person", s.last)
        check("the 9 Oct run: 'find a different person' for Underdog AI names who is left out and never offers "
              "Sigil Wen again",
              (more, s.searches[-1]["exclude"]),
              ("More PoCs — Underdog AI\n\nSigil Wen and Daniel Hong are already on the sheet, and I could not "
               "find anyone else.\n" + pc.TRY_AGAIN, ["Sigil Wen", "Daniel Hong", "Ana Pereira"]))
        check("...and the first reply offered only the person who is not on the sheet",
              re.findall(r"^\d\. (.+?)(?: —|$)", first, re.M), ["Ana Pereira"])

    with Stage(["Fullhouse Labs"]) as s:
        await s.wednesday()
        full = await s.reply("yes", s.post)
        check("one company, one section: the one-line header and no subheading",
              (full.splitlines()[0], "Fullhouse Labs — Voice AI" in full), ("Suggested PoCs — Fullhouse Labs", False))
        check("every field found: the trailing note does not appear at all", pc.NOTE_OMITTED in full, False)
    with Stage(["Blank Co", "Fullhouse Labs"]) as s:
        await s.wednesday()
        text = await s.reply("yes", s.post)
        check("Industry is omitted from the subheading when the Master Pipeline cell is blank",
              ("Blank Co" in text.splitlines(), "Blank Co —" in text, "Fullhouse Labs — Voice AI" in text.splitlines()),
              (True, False, True))
        check("'unknown' and 'not found' are omitted too", [x for x in ("unknown", "not found") if x in text], [])
    with Stage(["Fullhouse Labs"], found={"Fullhouse Labs": {0: [person(
            "Fay Lin", "CTO", LI + "faylin", "https://fullhouse.example/paper/", "Pune", "https://fullhouse.example/paper")]}}) as s:
        await s.wednesday()
        text = await s.reply("yes", s.post)
        check("Source is omitted when it is the same url as Paper (scheme, www. and trailing slash aside)",
              ("   Source" in text, "   Paper       [fullhouse.example/paper]" in text), (False, True))
    with Stage(["Nobody Labs"]) as s:
        await s.wednesday()
        text = await s.reply("yes", s.post)
        check("nothing found for the only company: the short form, the one try-again close, no offer",
              (text, s.proposals("row_add")),
              ("No PoCs found — Nobody Labs\n\n" + pc.NOBODY + "\n" + pc.TRY_AGAIN, []))
    with Stage(["Limbic AI"], SHEET, found={"Limbic AI": {0: []}}) as s:
        await s.wednesday()
        text = await s.reply("yes", s.post)
        check("no details found for an existing row: said, with the same close, and no request line",
              text, "Missing fields — Limbic AI\n\nI could not find the missing details for Ross Harper on the "
                    "web.\n" + pc.TRY_AGAIN)


async def the_write():
    say("THE SECOND YES")
    import approvals
    import gtm_sheet
    import poc_crosscheck as pc
    import wording
    with Stage(["Oogam AI", "Underdog AI"], SHEET) as s:
        await s.wednesday()
        await s.reply("yes", s.post)
        offer = s.last
        check('read_vote("yes to 1 and 2 but not 3") reads YES in the tree (approvals.read_vote is not touched)',
              approvals.read_vote("yes to 1 and 2 but not 3"), approvals.VOTE_YES)
        asked = await s.reply("yes to 1 and 2 but not 3", offer)
        check("...yet replying that to the list writes NOTHING, says so, and leaves the question open",
              (s.sheet_writes(), asked, s.proposals("row_add")[-1]), ([], wording.POC_ADD_ALL_OR_NONE, "open"))
        # Somebody adds Ana by hand between the search and the yes.
        s.w.sheet.set(pocs=SHEET + [poc_row(653, "Underdog AI (Conway Research)", "Ana Pereira", "Research Engineer",
                                            li=LI + "anapereira")])
        m3 = await s.reply("yes", offer)
        SHOWN["M3"] = m3
        check("the second yes re-reads the sheet: a person who is on it by now is not appended",
              [a["values"]["name"] for a in s.appended], ["Priya Raghavan", "Arjun Mehta", "Third Person"])
        check("M3 says so, by name, with no row number and no link",
              (m3, bool(re.search(r"row \d+|https?://", m3))),
              ("Done — Oogam AI\nadded to Outreach PoCs\n\nNot added — Underdog AI\n"
               "Skipped Ana Pereira: already on Outreach PoCs.", False))
        first = s.appended[0]
        check("a new row carries the Master Pipeline's company name, Industry from that tab, Name, Designation, "
              "Based and LI Url (no Email: the switch is off)",
              first["values"], {"company": "Oogam AI", "name": "Priya Raghavan",
                                "li_url": LI + "priyaraghavan", "industry": "Voice AI",
                                "designation": "VP Research", "based": "Bengaluru, India"})
        check("a blank value ('->'), a non-role title and a non-profile link are not written",
              (s.appended[1]["values"].get("based"), s.appended[2]["values"].get("designation"),
               s.appended[2]["values"]["li_url"]), (None, None, ""))
        check("every row is signed on the Name cell, with the serial filled",
              [(a.get("note_role"), "Added by" in str(a.get("note_text")), a.get("fill_serial")) for a in s.appended],
              [("name", True, True)] * 3)
        tab = gtm_sheet.SHEETS.tab(gtm_sheet.POCS)
        roles = ("company", "industry", "name", "designation", "email", "based", "paper_links", "li_url")
        idx = [tab.canonical_role_to_col[r] for r in roles]
        check("the fields are columns B to I: writable on a NEW row, read-only on an existing one",
              ("".join(chr(65 + i) for i in idx), [config.may_write_new_row_column(i) for i in idx],
               [i in config.RESTRICTED_COLUMN_INDEXES for i in idx]), ("BCDEFGHI", [True] * 8, [True] * 8))

    with Stage(["Fullhouse Labs"], pins={"EMAIL_WRITE_ALLOWED": True}) as s:
        await s.wednesday()
        await s.reply("yes", s.post)
        await s.reply("yes", s.last)
        check("with EMAIL_WRITE_ALLOWED on, the found email and the paper link go on the new row too",
              (s.appended[0]["values"].get("email"), s.appended[0]["values"].get("paper_links")),
              ("fay@fullhouse.example", "https://fullhouse.example/paper"))
    with Stage(["Fullhouse Labs"]) as s:
        await s.wednesday()
        await s.reply("yes", s.post)
        s.w.bot._message_link = lambda message: ""          # the approval cannot be linked
        refused = await s.reply("yes", s.last)
        check("a row whose signature cannot be made is refused: nothing is appended, and it says so",
              (s.appended, "Not added — Fullhouse Labs" in refused and "could not link the approval" in refused),
              ([], True))

    say("EXISTING ROWS ARE NEVER WRITTEN")
    for allowed in (False, True):
        with Stage(["Limbic AI"], SHEET, pins={"EMAIL_WRITE_ALLOWED": allowed}) as s:
            await s.wednesday()
            text = await s.reply("yes", s.post)
            check(f"EMAIL_WRITE_ALLOWED={allowed}: Missing fields ends on the request, and no writing proposal "
                  "is opened", (text.splitlines()[-1], s.proposals("row_add"), s.proposals("email_write")),
                  ("Those Limbic AI columns are ones I'm not able to write to, so could you add them please?", [], []))
            await s.reply("yes", s.last)
            await s.reply("sure", s.last)
            check(f"EMAIL_WRITE_ALLOWED={allowed}: a yes to it writes nothing — no Based, Research Paper Link or "
                  "LI Url cell, and no row", s.sheet_writes(), [])
    email_only = [poc_row(1, "Email Only Co", "Eve Rao", "CTO", li=LI + "everao", based="Pune",
                          paper="https://x.example/p")]
    mand = ["name", "designation", "li_url", "email"]
    for allowed, last, props in ((False, pc.FILL_CLOSE.format(companies="Email Only Co"), []),
                                 (True, pc.FILL_EMAIL_OFFER, ["open"])):
        with Stage(["Email Only Co"], email_only, pins={"EMAIL_WRITE_ALLOWED": allowed, "POC_MANDATORY_FIELDS": mand},
                   found={"Email Only Co": {0: []}}) as s:
            await s.wednesday()
            text = await s.reply("yes", s.post)
            check(f"an Email-only gap with EMAIL_WRITE_ALLOWED={allowed}: "
                  + ("the offer line, behind an email_write proposal" if allowed else "the plain request, no offer"),
                  (text.splitlines()[-1], s.proposals("email_write"), s.sheet_writes()), (last, props, []))
            await s.reply("yes", s.last)
            check(f"...a yes then writes " + ("the Email cell only, through the existing email path" if allowed
                                             else "nothing"),
                  ([(w.get("email"), w.get("row")) for w in s.email_writes], s.appended),
                  ([("eve@emailonly.example", 2)] if allowed else [], []))

    with Stage(["Oogam AI", "Underdog AI"], SHEET, test_mode=True) as s:
        await s.wednesday()
        text = await s.reply("yes", s.post)
        check("test mode: the reply is the same message", "Sigil Wen and Daniel Hong are already on the sheet. "
              "One other I found:" in text and dead_links(text) == [], True)


async def rename_and_ghost():
    say("BUG 0 — A RENAMED COMPANY IS NOT A NEW COMPANY")
    import tempfile
    from db import DB

    def fresh():
        return DB(os.path.join(tempfile.mkdtemp(prefix="saley-r11-db-"), "snap_test.db"))

    def table(d):
        with d.conn() as c:
            return {r[0]: (r[1], r[2], r[3]) for r in c.execute(
                "SELECT company_key, company, first_seen, retired_on FROM pipeline_companies")}

    class Tap(logging.Handler):
        def __init__(self):
            super().__init__(logging.INFO)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())
    tap = Tap()
    logging.getLogger("db").addHandler(tap)
    was = logging.getLogger("db").level
    logging.getLogger("db").setLevel(logging.INFO)
    try:
        d = fresh()
        d.pipeline_snapshot(["Old Co"], today="2026-09-20")                       # the first run seeds
        d.pipeline_snapshot(["Old Co", "Underdog AI"], today="2026-10-02")
        got = d.pipeline_snapshot(["Old Co", "Underdog AI (Conway Research)"], today="2026-10-06")
        check("the cell changes from 'Underdog AI' to 'Underdog AI (Conway Research)': ONE company comes back, "
              "under its new name, with its ORIGINAL first-seen date",
              got, [{"company": "Underdog AI (Conway Research)", "first_seen": "2026-10-02"}])
        check("...the table holds one row for it, not two", sorted(k for k in table(d) if "underdog" in k),
              ["underdog ai conway research"])
        check("...and the rename is logged with both labels",
              any("'Underdog AI' was renamed to 'Underdog AI (Conway Research)'" in l for l in tap.lines), True)

        got = d.pipeline_snapshot(["Old Co", "Underdog AI (Conway Research)", "Underdog Robotics"],
                                  today="2026-10-07")
        check("a genuinely new company whose name resembles an existing one is still reported as new, not "
              "swallowed as a rename (nothing left the tab)",
              ([g["company"] for g in got], table(d)["underdog robotics"][1]),
              (["Underdog AI (Conway Research)", "Underdog Robotics"], "2026-10-07"))

        got = d.pipeline_snapshot(["Old Co", "Underdog Robotics"], today="2026-10-08")
        check("a key absent from today's sheet is never returned as new", [g["company"] for g in got],
              ["Underdog Robotics"])
        check("...and is marked retired, so it is decided once", table(d)["underdog ai conway research"][2],
              "2026-10-08")
        got = d.pipeline_snapshot(["Old Co", "Underdog Robotics"], today="2026-10-09")
        check("...the next run does not reconsider it", (
            [g["company"] for g in got], table(d)["underdog ai conway research"][2]),
            (["Underdog Robotics"], "2026-10-08"))
        before = table(d)
        check("an EMPTY read of the tab (it could not be read) retires nothing",
              (d.pipeline_snapshot([], today="2026-10-10") is not None, table(d)), (True, before))

        d = fresh()
        d.pipeline_snapshot(["Old Co"], today="2026-09-20")
        d.pipeline_snapshot(["Old Co", "Acme"], today="2026-10-02")
        tap.lines.clear()
        got = d.pipeline_snapshot(["Old Co", "Acme Labs", "Acme Robotics"], today="2026-10-06")
        check("two plausible new names for one that left: not guessed. The old one is retired and both new "
              "names are new companies",
              (table(d)["acme"][2], sorted(g["company"] for g in got),
               sorted(g["first_seen"] for g in got)),
              ("2026-10-06", ["Acme Labs", "Acme Robotics"], ["2026-10-06", "2026-10-06"]))
        check("...and the log says it did not guess", any("I am not guessing" in l for l in tap.lines), True)

        # THE LIVE DATABASE AS THE 9 OCT BUILD LEFT IT: both keys present, the new one with a fresh date.
        d = fresh()
        d.pipeline_snapshot(["Old Co"], today="2026-09-20")
        d.pipeline_snapshot(["Old Co", "Underdog AI"], today="2026-09-25")
        with d.conn() as c:
            c.execute("INSERT INTO pipeline_companies (company_key, company, first_seen, seeded) VALUES "
                      "('underdog ai conway research', 'Underdog AI (Conway Research)', '2026-10-08', 0)")
        got = d.pipeline_snapshot(["Old Co", "Underdog AI (Conway Research)"], today="2026-10-14")
        check("the ghost the live database already has: the next run retires the stale 'underdog ai' row with no "
              "manual step, and the surviving row gets its original first-seen date back",
              (table(d)["underdog ai"][2], table(d)["underdog ai conway research"][1]),
              ("2026-10-14", "2026-09-25"))
        check("...so neither is announced as new (first seen 25 Sep is outside the window)", got, [])
    finally:
        logging.getLogger("db").removeHandler(tap)
        logging.getLogger("db").setLevel(was)

    # Through the bot: one M1 line, with the right suffix, after a rename between two Wednesdays.
    with Stage(["Underdog AI"], SHEET) as s:
        await s.w.bot._plan_drip(today=WED, already=[])                      # a run sees the old name
        s.w.sheet.grids["Master Pipeline"][-1][1] = "Underdog AI (Conway Research)"      # the cell is edited
        text = await s.wednesday()
        check("through the bot: after the rename M1 has ONE Underdog line, with the right suffix",
              [l for l in text.splitlines() if "Underdog" in l],
              ["1. Underdog AI (Conway Research) — already on Outreach PoCs"])
        check("the header's date is the start of the 7-day window counted from the day the bot is on",
              text.splitlines()[0], "New companies in the Master Pipeline — since Wed 7 Oct")


async def main():
    await rename_and_ghost()
    await m1()
    await the_reply()
    await the_write()
    if SHOW:
        for name, title in (("M1", "M1"), ("reply", "the reply to a yes"), ("more", "'find a different person'"),
                            ("M3", "M3")):
            print(f"\n--- {title}, as posted ---")
            for line in SHOWN.get(name, "").split("\n"):
                print("    | " + line)


logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
asyncio.run(main())
print(f"\n{passed} check(s) passed" + (f", {failures} failed" if failures else ""))
print("ALL PASSED" if not failures else f"{failures} FAILED")
sys.exit(1 if failures else 0)

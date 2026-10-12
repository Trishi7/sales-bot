"""R12 "SALES PACKAGES" (redesigned 12 Oct 2026) — the checks, shared by verify_packages.py and
tests/test_packages.py.

    sys.path.insert(0, <repo>/tests); import packages_checks as pkc
    pkc.run(check)                 # check(name, got, want)

WHAT RUNS HERE IS THE REAL THING: rows through the real sheet parser, the real rule (`nextaction.run`,
`packages.not_ready`), the real renderer (`drip`, `wording.packages_post`), and a real bot behind a fake Discord
with a throwaway database for the send, the reply that gives a date (`bot._maybe_package_dates`), the reminders
table and the one reminder sender (`bot._fire_due_reminders`). No network, no model, no real sheet.

Every R12 setting is pinned to its .env.example default, never read from this machine's .env.

EVERY PACKAGE NAME, LINK AND DATE HERE IS MADE UP (the links are on docs.example), apart from "Evals_Vibe-check",
the name the ticket uses for its shortened-form example.
"""
import asyncio
import contextlib
import logging
import os
import re
import sys
from datetime import date, datetime, time, timedelta

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
import gtm_sheet  # noqa: E402
import links  # noqa: E402
import nextaction  # noqa: E402
import packages  # noqa: E402
import rules as rules_mod  # noqa: E402
import wording  # noqa: E402

THU = date(2026, 10, 15)                       # a Thursday
HEADERS = ["Package", "Name", "Purpose", "Use Case", "Size", "Audio Files", "Image Files", "JSONL Output",
           "Pulse_Product Doc", "% Completion", "Ready?", "Status"]
OPENER = "these Sales Packages aren't ready yet:"
CLOSE = "Could you let me know roughly when each might be ready?"
NO_DATE = "I couldn't pick a date out of that — could you give me a day, like \"24 Oct\"? I'll check back then."
_ABSENT = object()


def _example(name: str) -> str:
    for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{name} is not in .env.example")


def pins() -> dict:
    return {"PACKAGES_ORDER": _example("PACKAGES_ORDER"),
            "PACKAGES_DATE_CAPTURE": _example("PACKAGES_DATE_CAPTURE").lower() == "true",
            "REMINDER_DEFAULT_TIME": _example("REMINDER_DEFAULT_TIME")}


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


# -- rows -------------------------------------------------------------------------------------------------------------

def pkg(n, name, ready="", completion="", status="In progress", doc="", headers=None) -> list:
    cells = {"Package": f"P{n}", "Name": name, "Pulse_Product Doc": doc, "% Completion": completion,
             "Ready?": ready, "Status": status}
    return [cells.get(h, "") for h in (headers or HEADERS)]


def five() -> list:
    """Five that are not ready (one at 33%, four at 0%) and three that are, as the tab is described today."""
    return [pkg(1, "Hinglish STT", "Yes", "100%", "Done"),
            pkg(2, "Evals_Vibe-check", "", "0%"), pkg(3, "DPO Pack", "No", "33%"),
            pkg(4, "Voice Agents Bench", "", "0%"), pkg(5, "Call Summaries", "Yes", "100%", "Done"),
            pkg(6, "Safety Red Team", "", "0%"), pkg(7, "Tamil TTS", "No", "0%"),
            pkg(8, "Speaker ID", "Yes", "100%", "Done")]


FIVE_NAMES = ["DPO Pack", "Evals_Vibe-check", "Voice Agents Bench", "Safety Red Team", "Tamil TTS"]


def parsed(rows, headers=None) -> list:
    tab = gtm_sheet.SHEETS._parse_values("Sales Packages", [list(headers or HEADERS)] + [list(r) for r in rows],
                                         read_at=dl.real_epoch())
    return list(tab.rows) if tab is not None else []


def engine(rows, day=THU, headers=None):
    return nextaction.run(today=day, rows=[], packages=parsed(rows, headers) if rows else [],
                          day_rules=[rules_mod.by_id("R12")])


def r12(result) -> list:
    return sorted((a for a in result["actions"] if a.get("rule_id") == "R12"), key=lambda a: a["pick_order"])


def named(result) -> list:
    return [a["company"] for a in r12(result)]


def message_of(result, day=THU):
    return next((m for m in drip.group(result["actions"], day=day) if m.get("type") == nextaction.R_PACKAGES), None)


RENDERED: list = []


def post_of(result, day=THU) -> str:
    m = message_of(result, day)
    text = drip.compose_fallback(m) if m else ""
    if text:
        RENDERED.append(text)
    return text


def one(rows, day=THU, **kw) -> str:
    return post_of(engine(rows, day, **kw), day)


class LogTap(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines = []

    def emit(self, record):
        with contextlib.suppress(Exception):
            self.lines.append((record.levelname, record.getMessage()))


@contextlib.contextmanager
def tapped():
    tap, logger = LogTap(), logging.getLogger()
    was = logger.level
    logger.addHandler(tap)
    logger.setLevel(logging.INFO)
    try:
        yield tap
    finally:
        logger.removeHandler(tap)
        logger.setLevel(was)


# -- the checks -----------------------------------------------------------------------------------------------------------

def who_is_picked(check):
    print("\nWHO IS PICKED — Ready? is not Yes, one per name")
    check("a blank Ready? qualifies; No qualifies; Yes is excluded",
          named(engine([pkg(1, "Blank One"), pkg(2, "No One", "No"), pkg(3, "Yes One", "Yes", "100%"),
                        pkg(4, "Tick One", "TRUE", "100%")])), ["Blank One", "No One"])
    check("Ready? Yes with % Completion under 100: NOT listed, Ready? wins",
          (named(engine([pkg(1, "Early Yes", "Yes", "80%"), pkg(2, "Still Open", "", "10%")])),
           packages.ready_but_incomplete(parsed([pkg(1, "Early Yes", "Yes", "80%"), pkg(2, "Done", "Yes", "100%")]))),
          (["Still Open"], [{"name": "Early Yes", "completion": 80, "sheet_row": 2}]))
    check("a row with a blank Name is skipped",
          named(engine([pkg(1, "", "", "50%"), pkg(2, "Named", "", "10%")])), ["Named"])
    with tapped() as tap:
        twice = engine([pkg(1, "Evals Pack", "", "20%"), pkg(2, "Other", "", "40%"), pkg(3, "evals_pack", "", "60%")])
    check("two rows with the same name are one package: the furthest-along row is kept",
          ([(a["company"], a["completion"]) for a in r12(twice)],
           [lvl for lvl, m in tap.lines if "is on the tab twice" in m]),
          ([("evals_pack", 60), ("Other", 40)], ["WARNING"]))
    check("nothing qualifies: no post at all, and no 'everything is ready' line",
          one([pkg(1, "Done A", "Yes", "100%"), pkg(2, "Done B", "Yes", "100%")]), "")
    with tapped() as tap:
        no_ready = engine([pkg(1, "A", "", "10%", headers=[h for h in HEADERS if h != "Ready?"])],
                          headers=[h for h in HEADERS if h != "Ready?"])
    check("the Ready? column is missing: NO post, logged at ERROR",
          (named(no_ready), post_of(no_ready), [lvl for lvl, m in tap.lines if "[R12]" in m and "Ready?" in m]),
          ([], "", ["ERROR"]))
    renamed = ["Package", "Label", "Pulse_Product Doc", "% Completion", "Ready?", "Status"]
    with tapped() as tap:
        no_name = nextaction.run(today=THU, rows=[], day_rules=[rules_mod.by_id("R12")],
                                 packages=[{"_row": 2, "ready": "", "completion": "10%"}])
    check("the Name column is missing: NO post, logged at ERROR",
          (named(no_name), [lvl for lvl, m in tap.lines if "[R12]" in m and "Name" in m]), ([], ["ERROR"]))
    moved = ["Ready?", "% Completion", "Name", "Status", "Pulse_Product Doc", "Package"]
    check("columns the team MOVED are found by their headers",
          named(engine([pkg(1, "Moved One", "", "10%", headers=moved)], headers=moved)), ["Moved One"])
    with tapped() as tap:
        empty = nextaction.run(today=THU, rows=[], packages=[], day_rules=[rules_mod.by_id("R12")])
    check("the tab will not read (no rows): no post, logged at ERROR",
          (named(empty), [lvl for lvl, m in tap.lines if "[R12]" in m and "NO ROWS" in m]), ([], ["ERROR"]))
    check("the Sales Packages tab only: no Outreach PoCs row, no contact key, so no cross-rule dedup",
          sorted({(a.get("contact_key"), a.get("poc")) for a in r12(engine(five()))}), [("", "")])
    check("R12: Thursdays, five a post, in the channel, counted (the rules file is unchanged)",
          (rules_mod.by_id("R12").weekdays, rules_mod.by_id("R12").max_items_per_post,
           rules_mod.by_id("R12").destination, rules_mod.by_id("R12").counts_toward_cap), ((3,), 5, "channel", True))


def the_order(check):
    print("\nTHE ORDER — highest % Completion first, a blank per cent last")
    check("33 / 0 / 0 / 0 / 0: the 33 first, then the zeros in the sheet's own order",
          named(engine(five())), FIVE_NAMES)
    mixed = [pkg(1, "Blank Pct", "", ""), pkg(2, "Zero", "", "0%"), pkg(3, "Sixty", "", "60"),
             pkg(4, "Fraction", "", "0.75"), pkg(5, "Words", "", "nearly"), pkg(6, "Spaced", "", "33 %")]
    def all_names(rows):
        return [p["name"] for p in packages.not_ready(parsed(rows))["packages"]]
    check("a blank or unreadable per cent sorts LAST, below an explicit 0",
          all_names(mixed), ["Fraction", "Sixty", "Spaced", "Zero", "Blank Pct", "Words"])
    check("...and the post takes the first five of that order", named(engine(mixed)), all_names(mixed)[:5])
    check('"33%", "33", "33 %" and "0.33" all read as 33',
          [gtm_sheet.closure_percent(v) for v in ("33%", "33", "33 %", "0.33")], [33, 33, 33, 33])
    with pinned(PACKAGES_ORDER="completion_asc"):
        check("PACKAGES_ORDER=completion_asc flips it, and the blanks are still last",
              all_names(mixed), ["Zero", "Spaced", "Sixty", "Fraction", "Blank Pct", "Words"])
    with pinned(PACKAGES_ORDER="alphabetical"):
        check("an unrecognised PACKAGES_ORDER is the default order", named(engine(mixed))[:2], ["Fraction", "Sixty"])
    first, second = named(engine(five(), THU)), named(engine(five(), THU + timedelta(days=7)))
    check("nothing drops on a clock and nothing rotates: next Thursday, the same tab gives the same list",
          (first == second, len(first)), (True, 5))
    later = five()
    later[2] = pkg(3, "DPO Pack", "Yes", "100%", "Done")
    check("a package becomes ready: it leaves the list, silently",
          (named(engine(later)), [w for w in ("DPO Pack", "ready to sell", "is ready") if w in one(later)]),
          (FIVE_NAMES[1:], []))
    check("a package added to the tab mid-week is picked up the next Thursday",
          named(engine(five() + [pkg(9, "New This Week", "", "50%")], THU + timedelta(days=7)))[0], "New This Week")


def the_message(check):
    print("\nTHE MESSAGE — the opener, the numbered names, the close")
    post = one(five())
    check("five packages: the exact opener, numbered lines and close",
          post, OPENER + "\n1. DPO Pack\n2. Evals_Vibe-check\n3. Voice Agents Bench\n4. Safety Red Team\n"
          "5. Tamil TTS\n" + CLOSE)
    solo = one([pkg(1, "Only One", "", "40%"), pkg(2, "Done", "Yes", "100%")])
    check("a list of ONE has the same shape: opener, one numbered line, close",
          solo, OPENER + "\n1. Only One\n" + CLOSE)
    two = one([pkg(1, "First", "", "40%"), pkg(2, "Second", "", "10%")])
    check("two packages too (it used to become a composed sentence)", two, OPENER + "\n1. First\n2. Second\n" + CLOSE)
    seven = [pkg(i, f"Package {chr(64 + i)}", "", f"{70 - i * 10}%") for i in range(1, 8)]
    post7 = one(seven)
    check("seven packages: five lines, then '(+2 more next Thursday.)', then the close",
          post7.split("\n"),
          [OPENER, "1. Package A", "2. Package B", "3. Package C", "4. Package D", "5. Package E",
           "(+2 more next Thursday.)", CLOSE])
    check("...the two held back are not dropped: they are still on the rule's list",
          nextaction.run(today=THU, rows=[], packages=parsed(seven), day_rules=[rules_mod.by_id("R12")])
          ["reports"]["R12"]["held_back"], ["Package F", "Package G"])
    six = one(seven[:6])
    check("the held-back line carries the real number", "(+1 more next Thursday.)" in six, True)
    check("...and is absent when five or fewer qualify", "more next Thursday" in post, False)
    m = message_of(engine(five()))
    check("rendered in code: R12 is verbatim, has no composed template, and never reaches the generic tail",
          (nextaction.R_PACKAGES in drip.VERBATIM_TYPES, nextaction.R_PACKAGES in drip._ASK, drip.is_verbatim(m),
           drip.points_of(m)["header"], drip.points_of(m)["close"]), (True, False, True, OPENER, CLOSE))
    check("the heading is unchanged and is not part of the block",
          (drip.HEADINGS["R12"], "Sales packages" in post), ("Sales packages", False))
    check("the item's own text is the name alone", [a["text"] for a in r12(engine(five()))], FIVE_NAMES)
    check("...and the order's inputs still travel with it, unprinted",
          [(a["completion"], a["ready"]) for a in r12(engine(five()))][:2], [(33, "No"), (0, "No")])
    linked = one([pkg(1, "Linked Pack", "", "40%", doc="https://docs.example/linked-pack"),
                  pkg(2, "Plain Pack", "", "30%", doc="docs.example/no-scheme"),
                  pkg(3, "Words Pack", "", "20%", doc="see the drive")])
    check("a product doc that is a real url makes the name a masked link; the visible text is still the name",
          linked.split("\n")[1], "1. " + links.link("Linked Pack", "https://docs.example/linked-pack"))
    check("a value with no http(s) scheme is not a url: the name goes out as plain text",
          linked.split("\n")[2:4], ["2. Plain Pack", "3. Words Pack"])
    check("every link carries a scheme and nothing is a bare url",
          ([u for t in RENDERED for u in re.findall(r"\]\(([^)]*)\)", t) if not re.match(r"^<https?://[^\s>]+>$", u)],
           [t for t in RENDERED if links.bare_urls(t)]), ([], []))
    check("no per cent, no status and no number other than the line numbers anywhere in a rendered block",
          [t for t in RENDERED if "%" in t or "per cent" in t or "In progress" in t or "complete" in t.lower()
           or re.search(r"\d", re.sub(r"(?m)^\d+\. |\(\+\d+ more next Thursday\.\)|\(<https?://[^>]+>\)", "",
                                      re.sub(r"Package [A-G]", "", t)))], [])
    check("the fixed lines are in the register check", [l for l in (OPENER, CLOSE, NO_DATE)
                                                       if l not in wording.all_lines()], [])


def the_dates(check):
    import replies_world as rw

    print("\nTHE PROMISED DATE — recorded from a reply, replaced by a newer one")

    def reminders(w) -> list:
        with w.bot.db.conn() as c:
            return [tuple(r) for r in c.execute(
                "SELECT company, due_date, due_time, status, requested_by, row_key FROM scheduled_reminders "
                "ORDER BY id").fetchall()]

    async def scenario(test_mode=False):
        out = {}
        with rw.World(test_mode, pretend=(THU, 13, 0), rules={"R12"}, pins=dict(pins())) as w:
            w.sheet.grids["Sales Packages"] = [list(HEADERS)] + five()
            planned = await w.bot._plan_drip(today=THU, already=[])
            msg = next((m for m in (planned or {}).get("messages") or []
                        if m.get("type") == nextaction.R_PACKAGES), None)
            n, model = len(w.posted), w.counts()["model"]
            await w.bot._send_drip_message(w.chan, msg, marker=dl.iso(THU), channel_id=w.chan.id)
            post = w.posted[n]
            out["post"] = rw.strip_tag(post.content)
            out["tagged"] = post.content.startswith("[TEST")
            out["model"] = w.counts()["model"] - model

            async def say(text, who="member"):
                before, reacts = len(w.posted), len(w.reactions)
                await w.say(text, who=who, reply_to=post)
                return [rw.strip_tag(m.content) for m in w.posted[before:]], len(w.reactions) - reacts

            out["first"] = await say("Vibe-check should be done by 24 Oct")
            out["after_first"] = reminders(w)
            out["second"] = await say("actually vibe-check slips to the 30th")
            out["after_second"] = reminders(w)
            out["several"] = await say("DPO pack next Friday, Tamil TTS end of the month")
            out["after_several"] = reminders(w)[2:]
            out["unreadable"] = await say("Safety Red Team is nearly there, soon")
            out["past"] = await say("Voice Agents Bench 2 Oct")
            out["unattached"] = await say("end of the month")
            out["not_on_post"] = await say("Hinglish STT by 24 Oct")
            out["question"] = await say("what is left on DPO pack?")
            out["count"] = len(reminders(w))
            out["writes"] = len(w.sheet.writes)
            saved = config.PACKAGES_DATE_CAPTURE
            config.PACKAGES_DATE_CAPTURE = False
            try:
                await say("Safety Red Team by 28 Oct")
                out["capture_off"] = len(reminders(w))
            finally:
                config.PACKAGES_DATE_CAPTURE = saved
        return out

    with pinned():
        live = asyncio.run(scenario(False))
        test = asyncio.run(scenario(True))
    body = "\n".join(l for l in live["post"].split("\n") if not l.startswith("**") and not l.startswith("<@"))
    check("the post goes out through the real planner and sender, word for word, with no model call",
          (live["post"].split("\n")[0], body, live["model"]),
          ("**Sales packages**", OPENER + "\n1. DPO Pack\n2. Evals_Vibe-check\n3. Voice Agents Bench\n"
           "4. Safety Red Team\n5. Tamil TTS\n" + CLOSE, 0))
    RENDERED.append(body)
    check('a date in a reply is recorded: "Vibe-check" is Evals_Vibe-check, the day, who said it, the default time',
          live["after_first"],
          [("Evals_Vibe-check", "2026-10-24", "14:00", "open", live["after_first"][0][4], "package|evals vibe check")])
    check("...acknowledged with a reaction, not a sentence", live["first"], ([], 1))
    check("a newer date REPLACES the older: the first is closed, and there is one open reminder for the name",
          [(r[0], r[1], r[3]) for r in live["after_second"]],
          [("Evals_Vibe-check", "2026-10-24", "cancelled"), ("Evals_Vibe-check", "2026-10-30", "open")])
    check("several packages and dates in one reply are all recorded",
          [(r[0], r[1], r[3]) for r in live["after_several"]],
          [("DPO Pack", "2026-10-16", "open"), ("Tamil TTS", "2026-10-31", "open")])
    check("a package named with no date that can be read: the ask-again line, once, and nothing recorded",
          live["unreadable"], ([NO_DATE], 0))
    check("a date already in the past gets the same answer", live["past"], ([NO_DATE], 0))
    check("a date with no package named, under a post of five: one question, nothing recorded",
          live["unattached"], ([wording.PACKAGES_WHICH], 0))
    check("a package that was NOT on that post cannot have a date attached to it",
          (live["not_on_post"][0], [r for r in live["after_several"] if r[0] == "Hinglish STT"]),
          ([wording.PACKAGES_WHICH], []))
    check("a question under the post is not read as a date", live["question"][1], 0)
    check("after all that: four reminders in the table, three open, and nothing written to the sheet",
          (live["count"], live["writes"]), (4, 0))
    check("with PACKAGES_DATE_CAPTURE=false nothing is recorded", live["capture_off"], 4)
    check("test mode is identical apart from the [TEST] tag",
          ([(r[0], r[1], r[3]) for r in test["after_second"]], test["unreadable"], live["tagged"], test["tagged"]),
          ([(r[0], r[1], r[3]) for r in live["after_second"]], live["unreadable"], False, True))


def the_reminder(check):
    import replies_world as rw

    print("\nTHE REMINDER — on the day, by the one reminder sender")
    sat = date(2026, 10, 24)                       # a Saturday
    next_thu = date(2026, 10, 22)

    async def fire(rows, *, due=sat, name="DPO Pack", twice=False, generic=False):
        with rw.World(False, pretend=(due, 14, 5), pins=dict(pins())) as w:
            w.sheet.grids["Sales Packages"] = [list(HEADERS)] + rows
            if rows is None:
                w.sheet.grids.pop("Sales Packages", None)
            rid = w.bot.db.add_scheduled_reminder(
                row_key="" if generic else packages.reminder_key(name), company=name, due_date=dl.iso(due),
                due_time="14:00", what="check whether it is ready", requested_by="Vaishnavi",
                on_date=dl.iso(THU), channel_id=str(w.chan.id), asker_id="111")
            n = len(w.posted)
            at = datetime.combine(due, time(14, 5), dl.IST)
            early = await w.bot._fire_due_reminders(channel=w.chan, from_test=True,
                                                    at=datetime.combine(due, time(13, 59), dl.IST))
            fired = await w.bot._fire_due_reminders(channel=w.chan, from_test=True, at=at)
            again = await w.bot._fire_due_reminders(channel=w.chan, from_test=True, at=at) if twice else []
            status = w.bot.db.scheduled_reminder(rid)["status"]
            return {"posts": [rw.strip_tag(m.content) for m in w.posted[n:]], "early": early, "fired": fired,
                    "again": again, "status": status, "writes": len(w.sheet.writes)}

    def run(*a, **k):
        with pinned():
            return asyncio.run(fire(*a, **k))

    open_rows = five()
    got = run(open_rows, twice=True)
    line = "DPO Pack was expected to be ready around today, and Ready? on the tab is still not Yes. " \
           "Any update, or shall I check back later?"
    check("Ready? is still not Yes on the day: one line, at its minute, never twice",
          ([p.split("\n")[-1] for p in got["posts"]], got["early"], len(got["fired"]), got["again"], got["status"]),
          ([line], [], 1, [], "done"))
    RENDERED.extend(p.split("\n")[-1] for p in got["posts"])
    ready = five()
    ready[2] = pkg(3, "DPO Pack", "Yes", "100%", "Done")
    got = run(ready)
    check("Ready? is Yes on the day: the reminder is closed QUIETLY, nothing is posted",
          (got["posts"], got["fired"], got["status"]), ([], [], "done"))
    got = run(open_rows, due=next_thu)
    check("the reminder's day is a Thursday: skipped (the weekly post names the package), closed, not re-queued",
          (got["posts"], got["fired"], got["status"]), ([], [], "done"))
    gone = [r for r in five() if r[1] != "DPO Pack"]
    got = run(gone)
    check("the package is no longer on the tab: the reminder is closed, with one line in the channel saying so",
          ([p.split("\n")[-1] for p in got["posts"]], got["status"]),
          (["DPO Pack isn't on the Sales Packages tab any more, so I've dropped the check-back I had for it "
            "today."], "done"))
    got = run([])
    check("the tab cannot be read on the day: nothing is said, and the reminder stays open for the next tick",
          (got["posts"], got["status"]), ([], "open"))
    got = run(open_rows, generic=True)
    check("an ordinary reminder still goes out the ordinary way, through the same sender",
          (["you asked me to remind you" in p for p in got["posts"]], got["status"]), ([True], "done"))
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    check("there is ONE reminder sender: the package branch is inside _fire_due_reminders, and nothing else "
          "calls it",
          (len(re.findall(r"self\._fire_package_reminder\(", src)),
           "_fire_package_reminder(r, target" in src[src.index("async def _fire_due_reminders("):
                                                      src.index("async def _sweep_once(")]), (1, True))
    check("no new table and no second cap: the date lives in scheduled_reminders",
          ("package" in open(os.path.join(ROOT, "db.py"), encoding="utf-8").read().lower().split(
              "create table if not exists scheduled_reminders")[0][-10:], True), (False, True))


def settings(check):
    print("\nSETTINGS — the two new variables, the boot line, the startup check")
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    check("PACKAGES_ORDER=completion_desc and PACKAGES_DATE_CAPTURE=true, in the code and in .env.example",
          ('getenv("PACKAGES_ORDER", "completion_desc")' in src, _example("PACKAGES_ORDER"),
           '_bool("PACKAGES_DATE_CAPTURE", default=True)' in src, _example("PACKAGES_DATE_CAPTURE")),
          (True, "completion_desc", True, "true"))
    with pinned(), tapped() as tap:
        saved = config.NEXT_ACTION_ENABLED
        config.NEXT_ACTION_ENABLED = True
        try:
            config.validate()
        finally:
            config.NEXT_ACTION_ENABLED = saved
    line = [m for _l, m in tap.lines if "[config] R12 sales packages" in m]
    check("the boot line says the order, the cap's source and whether dates are recorded",
          [("order=completion_desc" in m, "max_items_per_post" in m, "is recorded" in m, "blank is not ready" in m)
           for m in line], [(True, True, True, True)])
    import subprocess
    done = subprocess.run(
        [sys.executable, "-c", "import config, sys; sys.stdout.write(config.PACKAGES_ORDER)"], cwd=ROOT,
        capture_output=True, text=True,
        env=dict(os.environ, PACKAGES_ORDER="sideways", DISCORD_TOKEN="x", ANTHROPIC_API_KEY="x",
                 PYTHONIOENCODING="utf-8"))
    check("PACKAGES_ORDER=sideways falls back to the default, with a warning in the log",
          ((done.stdout or "").strip().splitlines()[-1:], "PACKAGES_ORDER='sideways' is not one of" in (
              (done.stderr or "") + (done.stdout or ""))), (["completion_desc"], True))
    bot_src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    check("the startup log names the rows where Ready? is Yes and % Completion is under 100",
          "[gtm.packages] %d package(s) say Ready? Yes with %% Completion under" in bot_src, True)
    done = subprocess.run([sys.executable, "-m", "packages"], cwd=ROOT, capture_output=True, text=True,
                          env=dict(os.environ, PYTHONIOENCODING="utf-8", DISCORD_TOKEN="x", ANTHROPIC_API_KEY="x"))
    check("packages.py's own self-test passes", (done.returncode, (done.stdout or "").strip().splitlines()[-1:]),
          (0, ["ALL PASSED"]))


def run(check):
    with pinned():
        who_is_picked(check)
        the_order(check)
        the_message(check)
    the_dates(check)
    the_reminder(check)
    settings(check)

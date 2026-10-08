"""REPLIES-OCT8 — the checks, shared by verify_replies_oct8.py and tests/test_replies_oct8.py.

Written from docs/plans/REPLIES-OCT8.md. Four things the human reported on 8 Oct 2026:

  1. a reply ("take ur time") to "Give me a moment…" started a second answer
  2. "what are we doing today?" pasted the bot's own posts and talked about its schedule
  3. the Next steps post repeated the whole ask on every person's line
  4. the bot still thought it was 7 Oct, because a pretend day set the day before was never cleared

    sys.path.insert(0, <repo>/tests); import oct8_checks as oc
    asyncio.run(oc.run(check))          # check(name, got, want)

OFFLINE. The whole bot behind fake Discord (tests/replies_world.py): the real `on_message`, the real reply reader,
the real planner and sender, a throwaway database, a stand-in sheet parsed by the real parser, a model that records
what it was asked. EVERY SOURCE PROBE IS A STAND-IN: today's meeting notes (`notes.list_notes` / `notes.read_note`),
the channel history (`query.channel_recent_activity`) and the news store are fixtures put in here; no network, no
real Sheets or Drive (the offline guard is installed by the caller). Made-up people and companies only.
Every scenario runs LIVE and in TEST MODE and the two are compared: the `[TEST…]` tag is the only difference allowed.
"""
import asyncio
import os
import re
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import replies_world as rw  # noqa: E402
from replies_world import World, strip_tag  # noqa: E402

THU = date(2026, 10, 15)                # the fixture day for "today": a Thursday, so +2 working days is Mon 19 Oct
QUESTION = "what is in the news forthis hour?"
ACKS = ("take ur time", "sure take ur time", "no rush", "ok no rush thanks")

# What a "today" answer must never contain: a time of day, the schedule's words, a rule number, the AI news.
BANNED = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b|\bscheduled?\b|already posted|\bposted at\b|"
    r"\bR\d{1,2}\b|\brule\s+\d+|\bAI news\b|Today's a |\bnews\b", re.IGNORECASE)


def _interims():
    import wording
    return wording.INTERIM_WEB + wording.INTERIM_ENGINE


def _both(fn):
    """Run a scenario live and in test mode: (live result, test result)."""
    async def run():
        return await fn(False), await fn(True)
    return run()


# -- 1. replies to an interim line ---------------------------------------------------------------------------------

async def _acks_under(test_mode, parent_kind):
    """Each acknowledgement replied to one kind of bot message. What it changed, per phrase."""
    import wording
    out = {}
    for phrase in ACKS:
        with World(test_mode) as w:
            if parent_kind == "interim, remembered":
                parent = w.bot_says(wording.INTERIM_ENGINE[0])
                w.bot._remember_said(parent, kind="interim", question=QUESTION)
            elif parent_kind == "interim, after a restart":
                parent = w.bot_says(("[TEST] " if test_mode else "") + wording.INTERIM_ENGINE[1])
            elif parent_kind == "interim, web":
                parent = w.bot_says(wording.INTERIM_WEB[0])
            elif parent_kind == "the answer":
                parent = w.bot_says("**AI News, Thu 8 Oct**\n\n- **A headline** ([Reuters](<https://x.example/a>))")
                w.bot._remember_said(parent, kind="answer", question=QUESTION)
            else:                                   # a quiet line
                parent = w.bot_says(wording.NOTHING_TODAY)
            before = w.counts()
            await w.say(phrase, who="member", reply_to=parent)
            after = w.counts()
            out[phrase] = {k: after[k] - before[k] for k in after} | {"routes": w.routes()}
    return out


async def interim_replies(check) -> None:
    """ "take ur time" and friends replied to an interim line: one reaction, no text, no model call."""
    quiet = {"model": 0, "router": 0, "extractor": 0, "sheet_reads": 0, "replies": 0, "reactions": 1, "votes": 0,
             "reminders": 0, "drip": 0, "routes": ["ack"]}
    for kind in ("interim, remembered", "interim, after a restart", "interim, web", "the answer", "a quiet line"):
        live, test = await _both(lambda tm, kind=kind: _acks_under(tm, kind))
        for phrase in ACKS:
            check(f"'{phrase}' replied to {kind}: one reaction, no text, no model call, no sheet read",
                  live[phrase], quiet)
        check(f"...and the same in test mode ({kind})", test, live)

    # A REPLY TO AN INTERIM LINE THAT IS NOT AN ACKNOWLEDGEMENT AND NOT A QUESTION is still not a new question.
    import wording
    for phrase in ("yes", "hmm interesting", "I am in a meeting till lunch", "👍"):
        with World(False) as w:
            parent = w.bot_says(wording.INTERIM_ENGINE[0])
            w.bot._remember_said(parent, kind="interim", question=QUESTION)
            before = w.counts()
            await w.say(phrase, who="member", reply_to=parent)
            after = w.counts()
            check(f"'{phrase}' replied to an interim line: one reaction and nothing else",
                  (after["reactions"] - before["reactions"], after["replies"] - before["replies"],
                   after["model"] - before["model"]), (1, 0, 0))


async def _question_under_interim(test_mode):
    import wording
    with World(test_mode) as w:
        parent = w.bot_says(wording.INTERIM_ENGINE[0])
        w.bot._remember_said(parent, kind="interim", question=QUESTION)
        before = w.counts()
        asked = await w.say("take ur time, also any news on ElevenLabs?", who="member", reply_to=parent)
        after = w.counts()
        handed = w.model.request_text(0)
        return {"replies": after["replies"] - before["replies"], "reactions": after["reactions"] - before["reactions"],
                "model": after["model"] - before["model"],
                "reply_to": [getattr(getattr(m, "reference", None), "message_id", None) == asked.id
                             for m in w.posted[1:]],
                "text": [strip_tag(m.content) for m in w.posted[1:]],
                "handed_question": "any news on ElevenLabs?" in handed,
                "handed_politeness": "take ur time" in handed,
                "handed_interim": any(line in handed for line in _interims()),
                "routes": w.routes()}


async def question_under_interim(check) -> None:
    """A real question typed under an interim line is answered as its own question."""
    live, test = await _both(_question_under_interim)
    check("'take ur time, also any news on ElevenLabs?': answered — one reply, to that message, no reaction",
          (live["replies"], live["reply_to"], live["reactions"]), (1, [True], 0))
    check("...the engine is asked the question itself", live["handed_question"], True)
    check("...without the politeness and without the interim line quoted above it",
          (live["handed_politeness"], live["handed_interim"]), (False, False))
    check("...routed as a question, not an acknowledgement", "ack" in live["routes"], False)
    check("...the same in test mode", test, live)


async def _open_proposal_survives(test_mode):
    with World(test_mode) as w:
        post = w.bot_says("**AI events**\nWant me to remind you again on Monday?")
        key = w.open_remind(post.id)
        out = {}
        for phrase in ("no rush", "no worries", "no problem", "thanks, take ur time"):
            before = w.counts()
            await w.say(phrase, who="approver", reply_to=post)
            after = w.counts()
            out[phrase] = (after["reactions"] - before["reactions"], after["replies"] - before["replies"],
                           after["votes"] - before["votes"], w.proposals().get(key))
        return out


async def no_rush_is_not_a_no(check) -> None:
    """ "no rush" contains "no". Under an open proposal it must not decline it."""
    live, test = await _both(_open_proposal_survives)
    for phrase, got in live.items():
        check(f"'{phrase}' under an open offer: one reaction, no vote, and the offer stays open",
              got[:3] + (got[3] is not None and "open" in str(got[3]),), (1, 0, 0, True))
    check("...the same in test mode", {k: v[:3] for k, v in test.items()}, {k: v[:3] for k, v in live.items()})


# -- the 8 Oct 11:21 exchange -------------------------------------------------------------------------------------

async def exchange(test_mode) -> dict:
    """The exchange as it happened: the question, the interim line while the answer is being worked on, then "sure",
    "take ur time", "sure" and "ok" replied to it, then the answer. (On 8 Oct the last two were replies to a SECOND
    interim line, the one "take ur time" had wrongly produced; there is no second line now, so here they answer the
    first.)"""
    import wording
    with World(test_mode) as w:
        rw.enable_web(w)
        rw.seed_news(w)
        real_answer = w.bot._news_answer
        release = asyncio.Event()

        async def slow_answer(*a, **k):            # the answer takes a while, as it did
            await release.wait()
            return await real_answer(*a, **k)
        w.bot._news_answer = slow_answer

        question = rw.HMsg(w, w.chan, f"<@{rw.BOT_ID}> {QUESTION}", rw.MEMBER)
        question.mentions = [SimpleNamespace(id=rw.BOT_ID)]
        answering = asyncio.create_task(w.bot.on_message(question))
        for _ in range(20):
            await asyncio.sleep(0)
        await w.bot._send_interim(question, web=False, after=10.0, question=QUESTION)   # 11:21 "Give me a moment…"
        interim = w.posted[-1]
        steps = []

        async def step(label, text, **kw):
            before = w.counts()
            await w.say(text, who="member", **kw)
            after = w.counts()
            steps.append((label, after["reactions"] - before["reactions"], after["replies"] - before["replies"],
                          after["model"] - before["model"]))

        await step("11:21 'sure' replied to the interim line", "sure", reply_to=interim)
        await step("11:21 'take ur time' replied to the interim line", "take ur time", reply_to=interim)
        await step("11:22 'sure' replied to it again", "sure", reply_to=interim)
        await step("11:22 'ok' replied to it again", "ok", reply_to=interim)
        release.set()
        await answering
        said = [strip_tag(m.content) for m in w.posted]
        news_answers = [t for t in said if t.startswith("**AI News")]
        await step("after the answer: 'thanks, no rush' replied to the answer", "thanks, no rush",
                   reply_to=w.posted[-1])
        return {"steps": steps, "said": said, "interims": [t for t in said if t in _interims()],
                "news_answers": len(news_answers),
                "after": [strip_tag(m.content) for m in w.posted][len(said):],
                "answer": news_answers[0] if news_answers else "",
                "schedule_talk": [t for t in said if re.search(r"Today's a |on the schedule|Scheduled for today|"
                                                              r"already posted", t)],
                "tagged": [m.content.startswith("[TEST") for m in w.posted],
                "model": w.model.calls}


async def the_exchange(check) -> dict:
    live, test = await _both(exchange)
    check("8 Oct 11:21: exactly ONE news answer", live["news_answers"], 1)
    check("...the bot said two things in all: the interim line and the answer",
          (len(live["said"]), len(live["interims"])), (2, 1))
    check("...no second 'One sec' line, and nothing about the schedule",
          (live["interims"].count(live["interims"][0]) if live["interims"] else 0, live["schedule_talk"]), (1, []))
    for label, reactions, replies_, model in live["steps"]:
        check(f"...{label}: one reaction, no text, no model call", (reactions, replies_, model), (1, 0, 0))
    check("...no model call anywhere in the exchange", live["model"], 0)
    check("...and nothing more was said after it", live["after"], [])
    check("8 Oct 11:21 in test mode: the same messages and the same reactions",
          (test["said"], test["steps"], test["news_answers"]), (live["said"], live["steps"], 1))
    check("...a reply carries no tag in either mode (only the bot's own posts are tagged)",
          (any(live["tagged"]), any(test["tagged"])), (False, False))
    return live


# -- 2. "what is the team working on today?" -----------------------------------------------------------------------

def _stage_today(w, *, empty=False):
    """Put the day's fixtures in front of the bot: a meeting note, channel messages, the sheet's tabs."""
    import notes
    import query
    import rule13_fixtures as fx

    iso = THU.isoformat()
    note = {"date": iso, "label": "Pipeline review", "title": "Pipeline review", "path": "/notes/pipeline.md",
            "summary": "Went through the pipeline.", "decisions": ["Pilot pricing stays as is"],
            "next_steps": [{"owner_name": "Vaishnavi Reddy", "task": "send the pilot proposal to Acme Labs by Friday"},
                           {"owner_name": "Sid", "task": "book the PolyAI demo at 4 PM"}],
            "raw": "raw"}
    old = {"date": (THU - timedelta(days=1)).isoformat(), "label": "Yesterday's sync", "path": "/notes/old.md"}
    listed = [] if empty else [dict(note), old]
    saved = (notes.list_notes, notes.read_note, query.channel_recent_activity)
    notes.list_notes = lambda days=14, **_k: [dict(n) for n in listed]
    notes.read_note = lambda date=None, label=None, **_k: (
        dict(note) if not empty and date == iso and (label or "") in ("Pipeline review", "") else None)

    def at(hh, mm, day=THU):
        return datetime(day.year, day.month, day.day, hh, mm, tzinfo=rw_ist())

    messages = [] if empty else [
        {"author": "Sid Rao", "text": "I'll call PolyAI about the quote tomorrow at 11 AM", "timestamp": at(10, 5)},
        {"author": "Kushal", "text": "nice one", "timestamp": at(10, 6)},
        {"author": "Kushal", "text": f"<@{rw.BOT_ID}> what did we send Acme?", "timestamp": at(10, 30)},
        {"author": "Vaishnavi", "text": "any AI news on Gnani? need to check before the call", "timestamp": at(10, 40)},
        {"author": "Vaishnavi", "text": "Please send the Wispr Flow deck to Trishi", "timestamp": at(11, 15)},
        {"author": "Sid Rao", "text": "we need to fix the pricing page", "timestamp": at(18, 0, THU - timedelta(days=1))},
    ]

    async def recent(_client, *, days=2, channel=None, **_k):
        return {"days": days, "messages": [dict(m) for m in messages], "message_count": len(messages)}
    query.channel_recent_activity = recent
    w._undo.append(lambda: (setattr(notes, "list_notes", saved[0]), setattr(notes, "read_note", saved[1]),
                            setattr(query, "channel_recent_activity", saved[2])))

    def bare(d):
        return f"{d.day}-{d.strftime('%b')}"

    def on(d):
        return d.strftime("%d-%m-%Y")

    if empty:
        w.sheet.set(pocs=[], events=[], deliverables=[["NDA", "P1", "Done", bare(THU), "Legal", "", ""]])
        return
    mon = date(2026, 10, 19)                              # two working days after Thursday 15 Oct
    w.sheet.set(
        deliverables=[
            ["MSA template", "P1", "", bare(mon), "Legal", "", ""],                       # due in 2 working days
            ["Pricing page copy", "P1", "Not started", bare(date(2026, 10, 21)), "Marketing", "", ""],   # too far
            ["Case study: Hinglish STT", "P2", "", bare(THU + timedelta(days=1)), "Sales", "", ""],     # not a P1
            ["NDA", "P1", "Done", bare(THU), "Legal", "", ""],                            # done
        ],
        events=[
            ["Voice AI Summit", on(THU + timedelta(days=9)), on(THU + timedelta(days=1)), "",
             "https://voiceaisummit.example", "Bengaluru"],                               # registration closes tomorrow
            ["Data Forum", on(THU + timedelta(days=20)), "", "Yes", "", "Online"],        # nothing due soon
        ],
        pocs=[fx.person(1, name="Priya Rao", company="Acme Labs", li_date=THU - timedelta(days=10),
                        step="Send email 1"),
              fx.person(2, name="Dev Shah", company="Borealis", li_date=THU - timedelta(days=12),
                        step="Send email 1", meeting_date=date(2026, 10, 16))],            # a meeting tomorrow
    )
    w.bot.db.add_scheduled_reminder(due_date=mon.isoformat(), due_time="15:00", what="the pulse overview doc",
                                    requested_by="Kushal", channel_id=str(rw.CHANNEL), asker_id=str(rw.MEMBER))
    w.bot.db.add_scheduled_reminder(due_date="2026-10-28", what="far away", requested_by="Kushal",
                                    channel_id=str(rw.CHANNEL), asker_id=str(rw.MEMBER))


def rw_ist():
    import deadlines as dl
    return dl.IST


async def today_answer(test_mode, question="what is the team working on today?", *, empty=False, hh=11) -> dict:
    with World(test_mode, pretend=(THU, hh, 0), rules={"R1", "R13", "R5", "R12"}) as w:
        _stage_today(w, empty=empty)
        before = w.counts()
        asked = await w.say(question, who="member", mention=True)
        after = w.counts()
        text = strip_tag(w.posted[-1].content) if w.posted else ""
        return {"text": text, "n": after["replies"] - before["replies"],
                "model": after["model"] - before["model"], "router": after["router"] - before["router"],
                "extractor": after["extractor"] - before["extractor"], "drip": after["drip"] - before["drip"],
                "routes": w.routes(), "tagged": [m.content.startswith("[TEST") for m in w.posted],
                "reply_to": getattr(getattr(w.posted[-1], "reference", None), "message_id", None) == asked.id
                if w.posted else False,
                "proposals": len(w.proposals())}


def _group(text, heading):
    """The lines under one heading of a today answer."""
    out, on = [], False
    for line in text.splitlines():
        if line.startswith("**"):
            on = line == f"**{heading}**"
            continue
        if not line.strip():
            on = False                      # a blank line ends the group
        elif on:
            out.append(line)
    return out


async def today(check) -> dict:
    live, test = await _both(today_answer)
    text = live["text"]
    check("today: one reply, to the question, with no model, router or extractor call",
          (live["n"], live["reply_to"], live["model"], live["router"], live["extractor"]), (1, True, 0, 0, 0))
    check("today: the four groups, in order",
          [l for l in text.splitlines() if l.startswith("**")],
          ["**From today's meetings**", "**Due soon**", "**In the channel today**", "**Also today**"])
    check("today: the meeting note's action items, first names, the meeting named, no time of day",
          _group(text, "From today's meetings"),
          ["- Vaishnavi: send the pilot proposal to Acme Labs by Friday (Pipeline review)",
           "- Sid: book the PolyAI demo (Pipeline review)"])
    due = _group(text, "Due soon")
    check("today: the event registration closing tomorrow is there",
          "- Tomorrow: registration for Voice AI Summit closes" in due, True)
    check("today: the deliverable due in 2 working days is there, with its team",
          "- Mon 19 Oct: MSA template (Legal)" in due, True)
    check("today: so are the meeting tomorrow and the reminder somebody set",
          ("- Tomorrow: meeting with Dev Shah (Borealis)" in due,
           "- Mon 19 Oct: reminder: the pulse overview doc" in due), (True, True))
    check("today: nothing outside the look-ahead, no P2, nothing that is done",
          [w for w in ("Pricing page copy", "Hinglish", "NDA", "Data Forum", "far away") if w in text], [])
    check("today: Due soon is soonest first", due, sorted(due, key=lambda l: (0 if "Tomorrow" in l else 1)))
    check("today: the channel messages somebody has to act on, in their words, without the time",
          _group(text, "In the channel today"),
          ["- Sid: I'll call PolyAI about the quote tomorrow", "- Vaishnavi: Please send the Wispr Flow deck to Trishi"])
    also = _group(text, "Also today")
    check("today: the day's posts in at most 2 lines, and the AI news post (due that day too) is not one of them",
          (1 <= len(also) <= 2, also),
          (True, ["- My posts today cover prospects to contact and next steps for connected contacts."]))
    check("today: no AI news, no PoC news, no time of day, no 'scheduled', no rule number",
          [m.group(0) for m in BANNED.finditer(text)], [])
    check("today: no ping, no link, and none of the bot's posts pasted in",
          (bool(re.search(r"<@\d+>|https?://", text)), "Next Steps says" in text), (False, False))
    check("today: asking claimed nothing — no slot recorded, no proposal opened",
          (live["drip"], live["proposals"]), (0, 0))
    check("today: routed as a 'today' question", live["routes"], ["today"])
    check("today: test mode gives the same answer", (test["text"], test["model"]), (text, 0))
    check("today: ...a reply carries no tag in either mode", (any(live["tagged"]), any(test["tagged"])),
          (False, False))

    import wording
    nothing_live, nothing_test = await _both(lambda tm: today_answer(tm, empty=True))
    check("a day with nothing in any source: ONE line saying so",
          (nothing_live["text"], nothing_live["text"].count("\n")), (wording.NOTHING_TODAY, 0))
    check("...with no model call, and the same in test mode",
          (nothing_live["model"], nothing_test["text"]), (0, wording.NOTHING_TODAY))
    check("...and that line names no time, schedule or rule",
          [m.group(0) for m in BANNED.finditer(wording.NOTHING_TODAY)], [])

    for hh in (9, 13, 20):
        at = await today_answer(False, hh=hh)
        check(f"today at {hh}:00: the answer comes through, the same four groups (Kushal, 6 Oct)",
              [l for l in at["text"].splitlines() if l.startswith("**")],
              ["**From today's meetings**", "**Due soon**", "**In the channel today**", "**Also today**"])
    return live


async def today_routes(check) -> None:
    """The ways people ask it, and the questions that only happen to say "today"."""
    import toolsets
    for q in ("what are today's objectives?", "today's plan", "today's priorities",
              "what do we need to do today?", "what are we supposed to do today?",
              "what is the team working on today?", "what's the team working on today",
              "what's on today?", "what are we doing today", "what do we have today?",
              "what should I do today", "objectives for today"):
        check(f"routes to today: {q!r}", toolsets.route(q), ["today"])
        got = await today_answer(False, q)
        check(f"...and is answered by code: {q!r}", (got["model"], got["routes"],
                                                     got["text"].startswith("**From today's meetings**")),
              (0, ["today"], True))
    for q in ("any AI news today?", "who replied today?", "what is Vaishnavi working on today",
              "did Acme reply today?", "what did we decide in today's meeting"):
        check(f"not a today question: {q!r}", "today" in toolsets.route(q), False)


# -- 3. the Next steps post -----------------------------------------------------------------------------------------

async def next_steps_post(test_mode) -> dict:
    """Four people on Send email 1, one on Send email 2, through the real planner and the real sender; then "done"."""
    import deadlines as dl
    import rule13_fixtures as fx

    day = date(2026, 10, 12)
    with World(test_mode, pretend=(day, 15, 5), rules={"R13"},
               pins={"SALES_ALWAYS_TAG_IDS": [rw.APPROVER]}) as w:
        people = [("Arjun Aryaa", "Gnani.ai", 1), ("Oliver Shoulson", "PolyAI", 1), ("Ariya Rastrow", "Wisprflow.ai", 2),
                  ("Priya Rao", "Acme Labs", 1), ("Dev Shah", "Borealis", 1)]
        rows = []
        for i, (name, company, n) in enumerate(people, start=1):
            extra = {"e1": "Yes", "e1d": day - timedelta(days=10)} if n == 2 else {}
            rows.append(fx.person(i, name=name, company=company, li_date=day - timedelta(days=20),
                                  step=f"Send email {n}", **extra))
        w.sheet.set(pocs=rows)
        planned = await w.bot._plan_drip(today=day, already=[])
        message = next(m for m in planned["messages"] if m["type"] == "next_step_followups")
        await w.bot._send_drip_message(w.chan, message, marker=dl.iso(day), channel_id=w.chan.id)
        post = w.posted[-1]
        body = strip_tag(post.content)
        before = w.counts()
        await w.say("done for Arjun", who="member", reply_to=post)
        one = strip_tag(w.posted[-1].content)
        await w.say("sent", who="member", reply_to=post)
        which = strip_tag(w.posted[-1].content)
        await w.say("take ur time", who="member", reply_to=post)
        after = w.counts()
        return {"body": body, "one": one, "which": which, "tagged": post.content.startswith("[TEST"),
                "reactions": after["reactions"] - before["reactions"], "model": after["model"] - before["model"],
                "votes": after["votes"] - before["votes"], "writes": len(w.sheet.writes),
                "people": [p.get("poc") for p in (w.bot.db.next_step_post(str(post.id)) or {}).get("people") or []]}


async def next_steps(check) -> dict:
    import wording
    live, test = await _both(next_steps_post)
    lines = live["body"].splitlines()
    ask1 = "Next Steps says Send email 1. Has it gone out? If so, mark 1st Email Sent and the date."
    ask2 = "Next Steps says Send email 2. Has it gone out? If so, mark 2nd Email Sent and the date."
    check("Next steps: the heading, then the tags line", (lines[0], lines[1]),
          ("**Next steps**", f"<@{rw.APPROVER}>"))
    check("Next steps: two groups, each ask written once",
          (lines.count(ask1), lines.count(ask2), [l for l in lines[2:] if l and not l.startswith("- ")]),
          (1, 1, [ask1, ask2]))
    check("Next steps: the post, as the human asked for it", lines[2:],
          [ask1, "- Arjun Aryaa (Gnani.ai)", "- Oliver Shoulson (PolyAI)", "- Priya Rao (Acme Labs)",
           "- Dev Shah (Borealis)", "", ask2, "- Ariya Rastrow (Wisprflow.ai)"])
    check("Next steps: no opener line, and no line repeats the ask after a name",
          ([o for o in ("A few next steps", "Next steps for some", "Where a few") if o in live["body"]],
           [l for l in lines if l.startswith("- ") and "Next Steps says" in l]), ([], []))
    check("Next steps: the post still records who it named, for the 'done' replies",
          sorted(live["people"]), sorted(["Arjun Aryaa", "Oliver Shoulson", "Ariya Rastrow", "Priya Rao", "Dev Shah"]))
    check("Next steps: 'done for Arjun' gets Arjun's line",
          live["one"], wording.next_step_done_line("email_out", short="Arjun", n=1))
    check("Next steps: 'sent' with nobody named asks which one, listing the people",
          (live["which"].startswith("Which one?"), "Arjun Aryaa (Gnani.ai)" in live["which"]), (True, True))
    check("Next steps: 'take ur time' under the post is one reaction; nothing was written, voted or asked of a model",
          (live["reactions"], live["writes"], live["votes"], live["model"]), (1, 0, 0, 0))
    check("Next steps: test mode posts the same text with the [TEST] tag",
          (test["body"], test["one"], live["tagged"], test["tagged"]), (live["body"], live["one"], False, True))

    advance = wording.next_step_post([
        {"who": "Priya Rao (Acme Labs)", "ask": "advance", "n": 1, "when": "5 Oct"},
        {"who": "Dev Shah (Borealis)", "ask": "advance", "n": 1, "when": "1 Oct"},
        {"who": "Mei Lin (Cinder)", "ask": "call", "when": "28 Sep", "set_call": True},
    ])
    check("an 'advance' group: the ask once, each person's own date after the colon; a call group the same",
          advance, ["Email 1 has gone out. Time to set Next Steps to Send email 2.",
                    "- Priya Rao (Acme Labs): sent 5 Oct", "- Dev Shah (Borealis): sent 1 Oct", "",
                    "The LI DM has gone out and there's no meeting yet. Time to call them, and set Next Steps to "
                    "Call the PoC.", "- Mei Lin (Cinder): DM sent 28 Sep"])
    return live


# -- 4. the test clock ---------------------------------------------------------------------------------------------

def the_clock(check) -> None:
    import shutil
    import tempfile

    import clock
    import config

    tmp = tempfile.mkdtemp(prefix="saley-oct8-clock-")
    was_db, was_mode = config.DB_PATH, config.SALES_TEST_MODE
    config.DB_PATH = os.path.join(tmp, "clock_test.db")
    try:
        config.SALES_TEST_MODE = True
        clock.forget()
        pretend = clock.real_today_ist() - timedelta(days=30)
        clock.set_day(pretend, by="tester")
        check("a pretend day set today stays", (clock.pretending(), clock.today_ist()), (True, pretend))
        clock.forget()                                         # a restart
        check("...and survives a restart on the same real day", (clock.pretending(), clock.today_ist()),
              (True, pretend))
        clock.next_day(by="tester")
        check("...'next day' still works within the day", clock.today_ist(), pretend + timedelta(days=1))

        yesterday = (clock.real_now_ist() - timedelta(days=1)).isoformat()
        with clock._connect() as c:
            c.execute("UPDATE test_clock SET real_start = ? WHERE id = 1", (yesterday,))
            c.commit()
        clock.forget()                                         # the bot starts the next morning
        lines = []

        class Tap(__import__("logging").Handler):
            def emit(self, record):
                lines.append(record.getMessage())
        tap = Tap()
        clock.log.addHandler(tap)
        try:
            first = clock.today_ist()
            again = [clock.today_ist(), clock.now_ist().date(), clock.today_ist()]
        finally:
            clock.log.removeHandler(tap)
        check("a pretend day set 'yesterday' is gone on today's first read",
              (first, clock.pretending()), (clock.real_today_ist(), False))
        check("...every later read is the real date too", again, [clock.real_today_ist()] * 3)
        ended = [l for l in lines if "has ended because the real day changed" in l]
        check("...and it is logged once, not on every read", len(ended), 1)
        with clock._connect() as c:
            stored = c.execute("SELECT COUNT(*) FROM test_clock").fetchone()[0]
        check("...and gone from storage, so a restart does not bring it back", stored, 0)
        clock.forget()
        check("...checked: after another restart it is still the real date", clock.today_ist(), clock.real_today_ist())
        check("...'what time is it' says the real clock", clock.describe().endswith("(IST)"), True)

        clock.set_day(pretend, by="tester")
        with clock._lock:
            clock._state["real_start"] = clock.real_now_ist() - timedelta(days=1)
        check("a bot left running past midnight drops it without a restart",
              (clock.today_ist(), clock.pretending()), (clock.real_today_ist(), False))
        ok, _ = clock.set_day(pretend, by="tester")
        check("'make it …' works again straight away", (ok, clock.today_ist()), (True, pretend))
        ok, _ = clock.back_to_today(by="tester")
        check("'back to today' still clears it", (ok, clock.pretending()), (True, False))
    finally:
        config.DB_PATH, config.SALES_TEST_MODE = was_db, was_mode
        clock.forget()
        shutil.rmtree(tmp, ignore_errors=True)


def settings(check) -> None:
    import inspect

    import config
    example = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    check(".env.example lists TODAY_LOOKAHEAD_WORKING_DAYS=2, uncommented",
          bool(re.search(r"^TODAY_LOOKAHEAD_WORKING_DAYS=2$", example, re.M)), True)
    check("config.py: the same default in code",
          '_int("TODAY_LOOKAHEAD_WORKING_DAYS", 2)' in inspect.getsource(config), True)


SECTIONS = (
    ("1. A REPLY TO AN INTERIM LINE NEVER STARTS A NEW ANSWER", interim_replies),
    ("   ...UNLESS IT CARRIES A REAL QUESTION", question_under_interim),
    ("   'NO RUSH' IS NOT A NO", no_rush_is_not_a_no),
    ("   THE 8 OCT 11:21 EXCHANGE, live then test mode", the_exchange),
    ("2. 'WHAT IS THE TEAM WORKING ON TODAY?'", today),
    ("   THE WAYS IT IS ASKED", today_routes),
    ("3. THE NEXT STEPS POST, GROUPED BY ASK", next_steps),
    ("4. THE TEST CLOCK", the_clock),
    ("THE SETTING", settings),
)


async def run(check, say=None) -> None:
    for title, fn in SECTIONS:
        if say:
            say(title)
        out = fn(check)
        if asyncio.iscoroutine(out):
            await out

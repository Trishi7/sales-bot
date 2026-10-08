"""CASES FOR WHAT THE BUILDER DID THAT THE NFT2-1063 PLAN DID NOT NAME (added after reading the diff).

Each is an extra behaviour of the build, asserted so a later change cannot silently undo it. The plan's own cases are
tests/replies_cases.py and replies_cases2.py; this file is registered after them. Where the plan was silent the
expectation is the cautious one: nothing is written, nothing is declined on a guess, and a reply that is not clearly an
answer gets a reaction or a question, never an action.

  X1  the "Waiting for your yes" post (one message for several proposals, none keyed to it): a yes to it answers the one
      it listed, asks which when it listed several, and an acknowledgement leaves everything open
  X2  a bare "no" that names no proposal declines nothing on a guess
  X3  "@Saley yes" / "@Saley no" with nothing open at all: one reaction
  X4  "no" under an offer that has no proposal behind it: one reaction
  X5  "sure" to an offer to mark a row Dead is NOT the terminal word the gate needs: nothing is proposed or written
  X6  the objectives answer pings nobody, even when a post that went out carried a mention
  X7  a post sent before the text was kept is left out of the answer, never guessed
  X8  the named constants the open decisions hang on
  X9  the "Is that a yes to ...?" question: a yes to it applies the one proposal; the proposal answered meanwhile says so
  X10 a reply to "Which one do you mean?" that picks none gets a reaction; a real question is answered normally
"""
import re
from datetime import date

import replies_cases as rc
import replies_world as rw
from replies_cases import Mark, case, ack_emoji, drip_post
from replies_world import MEMBER, World, WED


async def _stale_pair(w, *, kinds=("cell_update", "email_write")):
    """Two proposals old enough for the once-a-day nudge, then the REAL sweep that posts the nudge. Returns
    (nudge BotMsg, [keys])."""
    import deadlines as dl
    keys = []
    for i, kind in enumerate(kinds):
        post = w.bot_says(f"an older post with a {kind} offer")
        keys.append(w.open_proposal(kind=kind, message_id=post.id, age_minutes=60 * 24 * 3, trigger="reply",
                                    payload={"writes": {}, "applied": [], "asks": [], "skipped": [], "emails": []},
                                    company=f"Acme AI {i}", poc="Ada Lovelace", sheet_row=2 + i))
    if "_sweep_proposals" in w.bot.__dict__:
        del w.bot.__dict__["_sweep_proposals"]            # the real sweep, not the world's stub
    n = w.n_posted
    posted = await w.bot._sweep_proposals(today=dl.today_ist(), channel=w.chan)
    assert posted and w.n_posted > n, "the sweep posted no nudge"
    return w.posted[n], keys


@case
async def x1_the_waiting_for_your_yes_post(check, test_mode):
    """X1: the 'Waiting for your yes' nudge: a yes asks which when several are listed; '1' applies the first; 'thanks' leaves all open."""
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        applied = []

        async def record(message, proposal, *, decided_by, why=""):
            applied.append(proposal["proposal_key"])
        w.bot._apply_approved_write = record
        nudge, keys = await _stale_pair(w)
        check("X1 setup: the nudge lists both, none is keyed to it", ("Waiting" in nudge.content or "waiting" in nudge.content,
                                                                       [w.bot.db.proposal(k)["message_id"] != str(nudge.id) for k in keys]),
              (True, [True, True]))
        m = Mark(w)
        await w.say("thanks", reply_to=nudge)
        check("X1: 'thanks' to the nudge: one reaction, both stay open", ([e for _t, e in m.reacted], sorted(w.proposals().values())),
              ([ack_emoji()], ["open", "open"]))
        m = Mark(w)
        await w.say("yes", reply_to=nudge)
        text = "\n".join(m.said)
        check("X1: 'yes' to a nudge that listed TWO asks which, naming both, and applies neither",
              (text.startswith("Which one do you mean?"), applied, m.d("votes")), (True, [], 0))
        which = w.posted[-1]
        m = Mark(w)
        await w.say("1", reply_to=which)
        check("X1: '1' to that question applies the first one listed", (len(applied), applied[:1] == [applied[0]]), (1, True))
        check("X1: ...and only that one", sorted(w.proposals().values()), ["applied", "open"])
        return w.snapshot()


@case
async def x1b_a_nudge_that_listed_one(check, test_mode):
    """X1b: 'sure' to a nudge that listed ONE proposal applies it."""
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        applied = []

        async def record(message, proposal, *, decided_by, why=""):
            applied.append(proposal["proposal_key"])
        w.bot._apply_approved_write = record
        nudge, keys = await _stale_pair(w, kinds=("cell_update",))
        m = Mark(w)
        await w.say("sure", reply_to=nudge)
        check("X1b: 'sure' to a nudge that listed ONE applies that one, however old it is (the nudge named it)",
              (applied, w.proposals()[keys[0]]), (keys, "applied"))
        return w.snapshot()


@case
async def x2_a_bare_no_declines_nothing_on_a_guess(check, test_mode):
    """X2: '@Saley no' with two open declines nothing; one reaction."""
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        post = w.bot_says("R3 post")
        w.open_remind(post.id)
        other = w.bot_says("another post")
        w.open_remind(other.id, on="2026-10-19")
        m = Mark(w)
        await w.say("no", mention=True)
        check("X2: '@Saley no' with two proposals open: one reaction, nothing declined, no text",
              ([e for _t, e in m.reacted], sorted(w.proposals().values()), m.said, m.d("votes")),
              ([ack_emoji()], ["open", "open"], [], 0))
        return w.snapshot()


@case
async def x3_a_bare_yes_or_no_with_nothing_open(check, test_mode):
    """X3: '@Saley yes/no/sure/ok' with nothing open anywhere: one reaction."""
    with World(test_mode) as w:
        for word in ("yes", "no", "sure", "ok"):
            m = Mark(w)
            await w.say(word, mention=True)
            rc.quiet(check, m, who=f"X3 '@Saley {word}' with nothing open anywhere")
        return w.snapshot()


@case
async def x4_no_under_an_offer_with_no_proposal(check, test_mode):
    """X4: 'no' / 'no thanks' under an offer with no proposal: one reaction."""
    with World(test_mode) as w:
        parent = w.bot_says("Acme AI has two PoCs on the sheet. Want me to pull the full list for Acme?")
        m = Mark(w)
        await w.say("no", reply_to=parent)
        rc.quiet(check, m, who="X4 'no' to an offer that has no proposal")
        m = Mark(w)
        await w.say("no thanks", reply_to=parent)
        check("X4: 'no thanks' too: a reaction, no text, nothing run", (len(m.reacted), m.said, m.d("model")), (1, [], 0))
        return w.snapshot()


@case
async def x5_sure_to_an_offer_to_mark_a_row_dead(check, test_mode):
    """A 'sure' is not the terminal word the gate needs; the bot must not be able to talk itself into ending a row."""
    import canned_sheet
    with World(test_mode) as w:
        w.sheet.set(pocs=canned_sheet.pocs_values()[1:])
        w.llm.extract_result = {"intent": "update", "company": "Acme AI", "poc": "Ada Lovelace", "confidence": 0.9,
                                "fields": [{"role": "deal_status", "value": "Dead", "supersedes": False,
                                            "quote": "mark Ada Lovelace as Dead"}]}
        parent = w.bot_says("Acme AI has gone quiet. Shall I mark Ada Lovelace as Dead at Acme AI?")
        m = Mark(w)
        await w.say("sure", reply_to=parent)
        dead = [p for p in w.bot.db.open_proposals_in_channel(w.chan.id)
                if "Dead" in str((p.get("payload") or {}).get("writes"))]
        check("X5: no proposal carries a Dead write", dead, [])
        check("X5: nothing was written", w.sheet.writes, [])
        check("X5: no vote", m.d("votes"), 0)
        return w.snapshot()


@case
async def x6_the_objectives_answer_pings_nobody(check, test_mode):
    """A post that went out can carry a mention (an owner's ping); the on-demand copy turns it into a name."""
    import deadlines as dl
    w, fx = rc._objectives_world(test_mode, hh=14, mm=30, rules=("R13",))
    with w:
        await rc._stock_sheet(w, fx)
        day = dl.iso(dl.today_ist())
        w.bot.db.record_drip_send(on_date=day, slot=1, group_key="events|", action_type="events",
                                  companies="Data Summit", channel_id=w.chan.id, message_id=777,
                                  sent_at=day + "T14:00:00+05:30")
        w.bot.db.attach_drip_message_id(on_date=day, slot=1, message_id=777,
                                        body="**AI events & summits**\n<@111> two events to look at:\n• Data Summit")
        text = await w.bot._todays_objectives()
        check("X6: the sent post is in the answer", "Data Summit" in text, True)
        check("X6: ...and no mention token is: the ping became the person's name", ("<@" in text, "Vaishnavi" in text),
              (False, True))
        return {"text": text}


@case
async def x7_a_post_sent_before_the_text_was_kept(check, test_mode):
    """X7: a post sent before the text was kept is left out of the answer, never guessed."""
    import deadlines as dl
    w, fx = rc._objectives_world(test_mode, hh=14, mm=30, rules=("R3", "R13"))
    with w:
        await rc._stock_sheet(w, fx)
        day = dl.iso(dl.today_ist())
        w.bot.db.record_drip_send(on_date=day, slot=1, group_key="events|", action_type="events",
                                  companies="Data Summit", channel_id=w.chan.id, message_id=778,
                                  sent_at=day + "T14:00:00+05:30")             # no body: sent before this build
        text = await w.bot._todays_objectives()
        check("X7: the row with no stored text is left out, not guessed or re-planned", "Data Summit" in text, False)
        check("X7: the rest of the day is still there", "Person 0" in text, True)
        return {"text": text}


@case
async def x8_the_constants_the_open_decisions_hang_on(check, test_mode):
    """X8: the named constants the open decisions hang on exist with their defaults."""
    import bot as botmodule
    import nextaction
    check("X8: the reply walk is at most 3 hops", botmodule.REPLY_WALK_MAX_HOPS, 3)
    check("X8: Q6 (a non-reply 'thanks' reacts) is one constant, on", botmodule.ACK_NON_REPLY_GETS_REACTION, True)
    check("X8: Q1 (AI news is not in the objectives) is one constant, and it names the news post",
          nextaction.R_AI_NEWS in set(botmodule.OBJECTIVES_EXCLUDED_TYPES), True)
    check("X8: the excluded set is the AI-news type by name, and nothing else",
          set(botmodule.OBJECTIVES_EXCLUDED_TYPES), {nextaction.R_AI_NEWS})
    check("X8: Q3 (the closing offer line is left out) is one constant, off", botmodule.OBJECTIVES_SHOW_OFFERS, False)
    import replies
    check("X8: Q5 (the reaction) is one constant, the thumbs-up", replies.ACK_EMOJI, ack_emoji())
    return {}


@case
async def x9_a_yes_to_the_confirmation_question(check, test_mode):
    """'Is that a yes to X?' (one proposal, asked too long ago to assume): a yes replied to it applies that one."""
    import wording
    with World(test_mode) as w:
        applied = []

        async def record(message, proposal, *, decided_by, why=""):
            applied.append(proposal["proposal_key"])
        w.bot._apply_approved_write = record
        post = w.bot_says("R3 post")
        key = w.open_remind(post.id, age_minutes=45)
        await w.say("yes", mention=True)
        question = w.posted[-1]
        check("X9 setup: asked, applied nothing", (question.content.startswith("Is that a yes to"), applied), (True, []))
        m = Mark(w)
        await w.say("yes", reply_to=question)
        check("X9: a yes replied to the question applies that one proposal", (applied, w.proposals()[key]), ([key], "applied"))

    with World(test_mode) as w:
        applied = []

        async def record2(message, proposal, *, decided_by, why=""):
            applied.append(proposal["proposal_key"])
        w.bot._apply_approved_write = record2
        post = w.bot_says("R3 post")
        key = w.open_remind(post.id, age_minutes=45)
        await w.say("yes", mention=True)
        question = w.posted[-1]
        w.bot.db.close_proposal(proposal_key=key, status="applied", decision="someone else did", decided_by="Sid",
                                decided_at="now")
        m = Mark(w)
        await w.say("yes", reply_to=question)
        label = w.bot._proposal_label(w.bot.db.proposal(key))
        check("X9b: if it was answered meanwhile the reply says so and nothing runs again",
              (m.said, applied), ([wording.offer_closed(label)], []))
        return w.snapshot()


@case
async def x10_a_reply_to_which_that_picks_none(check, test_mode):
    """X10: a reply to 'which one' that picks none gets a reaction; a real question is answered normally."""
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        applied = []

        async def record(message, proposal, *, decided_by, why=""):
            applied.append(proposal["proposal_key"])
        w.bot._apply_approved_write = record
        r3, remind, other, email = await rc._two_open(w)
        await w.say("yes", mention=True)
        question = w.posted[-1]
        m = Mark(w)
        await w.say("hmm, not sure", reply_to=question)
        check("X10: a reply that picks none: one reaction, nothing applied",
              ([e for _t, e in m.reacted], applied, m.d("votes")), ([ack_emoji()], [], 0))
        m = Mark(w)
        await w.say("3", reply_to=question)
        check("X10: a number past the list picks nothing either", (applied, m.d("votes")), ([], 0))
        m = Mark(w)
        await w.say("what are the options again?", reply_to=question)
        check("X10: a real question is answered normally, not read as a pick", (m.d("model") >= 1, applied), (True, []))
        return w.snapshot()


@case
async def n1_news_content_comes_back_at_any_time_of_day(check, test_mode):
    """THE TICKET: 'a direct request for news returns the content, at any time of day.' The real clock is moved to
    11:23 (before the day's AI news post) and to 18:00 (after it: on a Wednesday its slot is 16:00) on the same day;
    there is no time-of-day gate on the path, so what comes back is simply what had been collected by then.

    THE FIRST STORY IS FROM THE EVENING BEFORE, 21:00: after Tuesday's AI news slot (20:00 since NFT2-1069; it was
    a fixed 14:00, and the story sat at 16:00), so it is "since the previous post" at both times of asking.

    SINCE 8 OCT (docs/plans/NEWS-OCT8.md) the list is chosen and written by code and a plain news question makes no
    model call, so the stories are read off the REPLY (the bold headlines), not off a tool result the model was given."""
    import re
    import deadlines as dl
    from datetime import datetime
    D = date(2026, 10, 7)
    ist = dl.IST
    t = lambda d, h, m: datetime(d.year, d.month, d.day, h, m, tzinfo=ist)   # noqa: E731
    times = [t(date(2026, 10, 6), 21, 0), t(D, 9, 30), t(D, 10, 30), t(D, 15, 0), t(D, 17, 0)]
    heads = ["Frontier lab ships a new eval suite", "Voice startup closes a Series A", "Regulator publishes AI data rules",
             "Open model tops the speech benchmark", "Chip maker unveils an inference part"]
    got = {}
    for label, (hh, mm), want_n in (("11:23", (11, 23), 3), ("18:00", (18, 0), 5)):
        with World(test_mode, pretend=(D, hh, mm)) as w:
            real_now, real_today = dl.real_now_ist, dl.real_today_ist
            dl.real_now_ist = lambda hh=hh, mm=mm: t(D, hh, mm)
            dl.real_today_ist = lambda: D
            w._undo.append(lambda: (setattr(dl, "real_now_ist", real_now), setattr(dl, "real_today_ist", real_today)))
            rw.enable_web(w)
            rw.seed_news(w, heads, times=times)
            w.bot.__dict__.pop("_news_tools", None)          # the real todays_news path, not the world's stand-in
            m = Mark(w)
            await w.say("top 5 AI headlines", who="member", mention=True)
            reply = rw.strip_tag(w.posted[-1].content) if w.posted else ""
            items = re.findall(r"^- [*][*](.+?)[*][*] [(]", reply, re.M)
            got[label] = items
            check(f"N1 {label}: the answer lists the collected stories", len(items), want_n)
            check(f"N1 {label}: with no model call", w.model.calls, 0)
            check(f"N1 {label}: and it was answered, no reaction", (len(m.said) >= 1, m.reacted), (True, []))
    check("N1: the 18:00 answer holds everything the 11:23 one did, and more", set(got["11:23"]) < set(got["18:00"]), True)
    return {"counts": {k: len(v) for k, v in got.items()}}


@case
async def q4_groups_the_plan_holds_back_are_not_in_the_answer(check, test_mode):
    """Q4 (open decision, built as the default): a group the plan rolls to the next day or holds back is not today's."""
    import deadlines as dl
    w, fx = rc._objectives_world(test_mode, hh=11, mm=0, rules=("R1", "R3", "R13"), pins={"DAILY_MESSAGE_CAP": 1})
    with w:
        await rc._stock_sheet(w, fx)
        planned = await w.bot._plan_drip(today=dl.today_ist(), already=[])
        rolled = [m["type"] for m in (planned or {}).get("rolled") or []]
        held = [m["type"] for m in (planned or {}).get("held") or []]
        scheduled = [m["type"] for m in (planned or {}).get("messages") or []]
        check("Q4 setup: with a cap of 1 the AI-news post takes it and the events post is rolled or held, not scheduled",
              ("events" in scheduled, "events" in rolled + held), (False, True))
        text = await w.bot._todays_objectives()
        check("Q4: the answer has what is scheduled today (Rule 13) and nothing of what was held back",
              ("Person 0" in text, "Data Summit" in text), (True, False))
        return {"text": text}

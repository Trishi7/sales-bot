"""MORE CASES FOR NFT2-1063 (registered into tests/replies_cases.CASES by import, in order after the first file's).

From the plan's list (13.3 E2, P3, C2) and the lead's: a real question when the parent is not found, a reply to ANY
PART of a split post for every drip-backed kind, the confirmations that name what they did, and an already-sent post
coming back from its stored body. Written from docs/plans/NFT2-1063.md, not from the builder's code.
"""
from datetime import date

import replies_cases as rc
import replies_world as rw
from replies_cases import (Mark, case, _cell_proposal, _objectives_world, _open_email, _stock_sheet)
from replies_world import WED, World


@case
async def e2b_parent_not_found_a_real_question_is_answered_without_context(check, test_mode):
    """Parent not found -> answered without context and NO vote of any kind (plan 5.2 'when it cannot be found')."""
    with World(test_mode) as w:
        post = w.bot_says("R3 post")
        key = w.open_remind(post.id)
        gone = w.bot_says("an answer that was deleted")
        gone.deleted = True
        m = Mark(w)
        await w.say("ok and what about Globex?", reply_to=gone, cached=False, bot_in_mentions=True)
        seen = w.model.request_text(0)
        check("E2b: it is answered", m.d("model") >= 1 and len(m.said) >= 1, True)
        check("E2b: ...without parent context (there is none to give)", "[Context: this message is a reply" in seen, False)
        check("E2b: no vote although 'ok' is a vote word and a proposal is open", (m.d("votes"), w.proposals()[key]),
              (0, "open"))
        check("E2b: not acknowledged by a reaction (it is a question)", m.reacted, [])
        return w.snapshot()


async def _split_post(w, kind):
    """A two-part post whose proposal is keyed to the FIRST part, as the sender leaves it, with the drip row holding
    every part's id. Returns (part1, part2, key)."""
    import deadlines as dl
    part1 = w.bot_says(f"**Post for {kind}**\nthe list, part one")
    part2 = w.bot_says("the list, part two\nWant me to do the thing? Say yes.")
    day = dl.iso(dl.today_ist())
    w.bot.db.record_drip_send(on_date=day, slot=1, group_key=f"{kind}|1", action_type=kind, companies="Acme AI",
                              channel_id=w.chan.id, message_id=part1.id, sent_at=day + "T14:00:00+05:30")
    w.bot.db.attach_drip_message_id(on_date=day, slot=1, message_id=part1.id, body="the list",
                                    part_ids=[str(part1.id), str(part2.id)])
    key = await _open_on(w, kind, part1)
    return part1, part2, key


async def _open_on(w, kind, sent):
    """The REAL opener for `kind`, against a message the caller made. Returns the new proposal's key."""
    import nextaction
    marker = "2026-10-07"
    if kind == "poc_lookup":
        await w.bot._open_poc_lookup_proposal({"type": nextaction.R_NEW_COMPANY, "companies": ["Acme AI"]},
                                              sent=sent, marker=marker)
    elif kind in ("event_append", "event_deadline"):
        act = ({"event_proposals": [{"name": "Data Summit", "date": date(2026, 10, 13), "deadline": date(2026, 10, 9),
                                     "event_key": "ds"}]} if kind == "event_append"
               else {"deadline_proposals": [{"name": "Data Summit", "sheet_row": 3, "deadline": date(2026, 10, 9),
                                             "source": "https://datasummit.example"}]})
        await w.bot._open_event_proposals({"type": nextaction.R_EVENTS, "actions": [act]}, sent=sent, marker=marker)
    elif kind == "email_write":
        return await _open_email(w, sent)
    elif kind == "events_remind":
        act = {"event_line": "You're registered for Voice AI Forum on Sat 17 Oct", "remind_on": "2026-10-12",
               "remind_word": "Monday", "event_link": ""}
        await w.bot._open_events_remind_proposal({"type": nextaction.R_EVENTS, "max_items_per_post": 5,
                                                  "actions": [act]}, sent=sent, marker=marker)
    keys = [k for k in w.proposals() if k.startswith(kind + ":")]
    assert keys, f"the real opener for {kind} opened nothing"
    return keys[-1]


SPLIT_KINDS = ["poc_lookup", "event_append", "event_deadline", "email_write", "events_remind"]


@case
async def p3_a_reply_to_any_part_of_a_split_post_finds_its_proposal(check, test_mode):
    """F3, for every kind a drip post opens: the offer is on the LAST line, so on a split post it sits in a message no
    proposal is keyed to. 'sure' replying to part 2 (and to part 1) applies THAT post's proposal and no other."""
    for kind in SPLIT_KINDS:
        for which in (2, 1):
            with World(test_mode, pretend=(WED, 14, 5)) as w:
                applied = []

                async def record(message, proposal, *, decided_by, why=""):
                    applied.append(proposal["proposal_key"])
                w.bot._apply_approved_write = record
                bystander = w.open_remind(w.bot_says("another post").id)
                part1, part2, key = await _split_post(w, kind)
                await w.say("sure", reply_to=part2 if which == 2 else part1)
                check(f"P3 {kind}: 'sure' to part {which} applies the post's proposal", (applied, w.proposals()[key]),
                      ([key], "applied"))
                check(f"P3 {kind}: ...and not the bystander", w.proposals()[bystander], "open")
    return {"kinds": SPLIT_KINDS}


@case
async def c2_confirmations_name_what_they_did(check, test_mode):
    """Section 9: the reminder's lines, the empty PoC search, and a decline that names what was declined."""
    import wording
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        post = w.bot_says("R3 post")
        w.open_proposal(kind="events_remind", message_id=post.id,
                        payload={"on": "2026-10-12", "word": "Monday", "time": "14:00", "lines": []},
                        company="", poc="", sheet_row=0)
        m = Mark(w)
        await w.say("yes", reply_to=post)
        check("C2: an events reminder with nothing on it says so", m.said, [wording.EVENTS_REMIND_EMPTY])
        check("C2: ...and sets nothing", w.reminders(), [])
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        post = w.bot_says("R3 post")
        w.open_proposal(kind="events_remind", message_id=post.id,
                        payload={"on": "2026-10-01", "word": "Monday", "time": "14:00", "lines": ["an event"]},
                        company="", poc="", sheet_row=0)
        m = Mark(w)
        await w.say("yes", reply_to=post)
        check("C2: a reminder date that has already come says so", m.said, [wording.EVENTS_REMIND_PAST])
        check("C2: ...and sets nothing", w.reminders(), [])
    with World(test_mode) as w:
        post = w.bot_says("R11 post")
        w.open_proposal(kind="poc_lookup", message_id=post.id, payload={"companies": []}, company="", poc="",
                        sheet_row=0, trigger="R11")
        m = Mark(w)
        await w.say("yes", reply_to=post)
        check("C2: a PoC search with no company left says so", m.said, [wording.POC_LOOKUP_EMPTY])
    with World(test_mode) as w:
        key, asker, question = await _cell_proposal(w)
        m = Mark(w)
        await w.say("no", reply_to=question)
        label = wording.proposal_label("cell_update", company="Acme AI", poc="Ada Lovelace")
        check("C2: a decline names what it declined", m.said, [wording.declined("Vaishnavi said no", [], label)])
        check("C2: ...and the proposal is declined, nothing written", (w.proposals()[key], w.sheet.writes),
              ("declined", []))
        return w.snapshot()


@case
async def e3b_an_already_sent_post_comes_back_from_the_stored_body(check, test_mode):
    """'If the 2pm post already went out, return what was posted.' Proved by changing the sheet AFTER the send: the
    plan would now say something else, but the answer still shows the post that actually went out."""
    import deadlines as dl
    w, fx = _objectives_world(test_mode, hh=14, mm=5, rules=("R3",))
    with w:
        await _stock_sheet(w, fx)
        w.bot._live_loop_held = lambda what: False
        w.bot.get_channel = lambda cid: w.chan if int(cid) == w.chan.id else None
        before = await w.bot._todays_objectives()
        await w.bot._maybe_send_drip()
        rows = [r for r in w.drip_rows() if r[2] == "events"]
        check("E3b setup: the events post went out once", len(rows), 1)
        stored = w.bot.db.drip_sent_today(dl.iso(dl.today_ist()))[0].get("body") or ""
        check("E3b: the sender stored the body of what it posted, without a [TEST] tag",
              (bool(stored), "[TEST" in stored), (True, False))
        w.sheet.set(events=[])                              # the plan would now have nothing for R3
        after = await w.bot._todays_objectives()
        check("E3b: the answer still shows the post that went out, from the stored body (the variant it drew)",
              (rc.norm_openers(after), rc._without_offer_line(stored.strip()) in after),
              (rc.norm_openers(before), True))
        check("E3b: and still has no closing offer line", "Want me to remind you" in after, False)
        return {"text": after}

"""THE CASES FOR NFT2-1063, one function each, run by tests/test_replies.py (pytest) and verify_replies.py (a script).

Written from docs/plans/NFT2-1063.md (sections 5.4, 5.5, 8, 13.3) and the ticket's edge cases, not from the
builder's code. A case takes `(check, test_mode)`, builds its own `World`, drives the REAL `on_message`, makes its
checks through `check(name, got, want=True)` and returns a SNAPSHOT of what happened. The driver runs every case
twice, SALES_TEST_MODE false then true, and asserts the two snapshots are equal apart from the [TEST] tag
(test mode and live behave identically; the tag is the only difference — CLAUDE.md).

Everything is made up (Acme AI, Ada Lovelace, Globex) and every setting is pinned to its .env.example default
(tests/replies_world.py PINS).

THE DECISIONS THE HUMAN HAS NOT MADE are built as the plan's section-0 defaults; each is asserted against ONE named
constant here so it is a one-line change when the human answers:
"""
import asyncio
import inspect
import re
import time
from datetime import date, timedelta

import replies_world as rw
from replies_world import (APPROVER, APPROVER2, MEMBER, OBJ_DAY, WED, World, ScriptModel, EchoModel, strip_tag)

# -- the open decisions, as constants (plan section 0) ---------------------------------------------------------
Q1_AI_NEWS_IN_OBJECTIVES = False        # Q1: the AI-news post is not in the on-demand objectives
Q3_OFFER_LINES_IN_OBJECTIVES = False    # Q3: R3's / R5's closing offer line is not in the on-demand answer
Q5_ACK = "👍"                           # Q5: the acknowledgement reaction
Q6_NON_REPLY_ACK_REACTS = True          # Q6: "@Saley thanks" with nothing open: one reaction, no text

FORBIDDEN_IN_OBJECTIVES = [             # plan section 8.2: none of these may appear in the on-demand answer
    ("a clock time", re.compile(r"\b\d{1,2}:\d\d\b")),
    ("an am/pm time", re.compile(r"\b\d{1,2}\s?(am|pm)\b", re.I)),
    ("a rule id", re.compile(r"\bR\d+\b")),
    ("the word rule", re.compile(r"\brules?\b", re.I)),
    ("the word schedule", re.compile(r"\bschedul\w*", re.I)),
    ("the word slot", re.compile(r"\bslots?\b", re.I)),
    ("the word cap", re.compile(r"\bcap\b", re.I)),
    ("the word posted", re.compile(r"\bposted\b", re.I)),
    ("posts at", re.compile(r"\bposts? at\b", re.I)),
]

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def ack_emoji():
    try:
        import replies
        return replies.ACK_EMOJI
    except Exception:
        return Q5_ACK


class Mark:
    """What the world looked like before a step, so a check can say what the step changed."""

    def __init__(self, w):
        self.w = w
        self.c = w.counts()
        self.n = w.n_posted
        self.r = len(w.reactions)

    def d(self, key):
        return self.w.counts()[key] - self.c[key]

    @property
    def said(self):
        return self.w.replies_after(self.n)

    @property
    def reacted(self):
        return self.w.reactions[self.r:]


def quiet(check, m, *, who, reaction=True, sheet=True, model=True):
    """The checks every acknowledgement shares: one reaction, no text, nothing else ran."""
    check(f"{who}: exactly one reaction, the acknowledgement", [e for _t, e in m.reacted], [ack_emoji()])
    check(f"{who}: no text was posted", m.said, [])
    check(f"{who}: zero model calls", m.d("model"), 0)
    check(f"{who}: zero router calls", m.d("router"), 0)
    check(f"{who}: zero sheet-update extractor calls", m.d("extractor"), 0)
    check(f"{who}: zero sheet reads", m.d("sheet_reads"), 0)
    check(f"{who}: zero votes recorded", m.d("votes"), 0)
    check(f"{who}: nothing scheduled", m.d("reminders"), 0)


def drip_post(w, text, action_type, *, slot, offer=""):
    """A bot post that the drip recorded (what a REAL sent post leaves behind), with no proposal behind it."""
    import deadlines as dl
    msg = w.bot_says(text)
    w.bot.db.record_drip_send(on_date=dl.iso(dl.today_ist()), slot=slot, group_key=f"{action_type}|{slot}",
                              action_type=action_type, owner_key="", owner_label="", companies="Acme AI",
                              stage="nudge", planned_at="14:00", channel_id=w.chan.id, message_id=msg.id,
                              sent_at=dl.now_ist().isoformat(timespec="seconds"), counts_toward_cap=True,
                              pinned=False)
    return msg


def label_of(kind, **kw):
    import wording
    return wording.proposal_label(kind, **kw)


# ==================================================================================================================
# THE DECISION TABLE (plan 5.5). One case per row, through the real on_message.
# ==================================================================================================================

@case
async def d1_sure_to_interim_with_r3_offer_open(check, test_mode):
    """THE 7 OCT BUG. 'sure' replying to the interim line; R3's remind offer is open elsewhere in the channel."""
    import wording
    with World(test_mode, pretend=(WED, 18, 0)) as w:
        post, _msg = await w.send_r3_post(WED)
        key = next(iter(w.proposals()))
        interim = w.bot_says(wording.INTERIM_WEB[0])
        m = Mark(w)
        await w.say("sure", reply_to=interim)
        quiet(check, m, who="D1 sure to the interim line")
        check("D1: the R3 offer is still open", w.proposals()[key], "open")
        check("D1: no reminder was scheduled (the 7 Oct 'Monday line')", w.reminders(), [])
        check("D1: the reply_latency route is ack", "ack" in w.routes(), True)
        events = [e.get("event") for e in w.audit()]
        check("D1: the audit shows the reaction and no proposal_vote at all",
              ("reaction_added" in events, "proposal_vote" in events), (True, False))
        check("D1: no 'Monday' line in anything the bot said", [t for t in m.said if "Monday" in t or "post these" in t], [])
        return w.snapshot()


@case
async def d2_sure_to_an_answer_nothing_open(check, test_mode):
    """6 OCT STEP 3. 'Sure.' a minute after an answer, nothing open: the old path read the sheet and dumped data."""
    with World(test_mode) as w:
        w.model.answer = "Acme AI has two PoCs on the sheet: Ada Lovelace and Sam Lee."
        await w.say("who are the PoCs at Acme AI?", who="member", mention=True)
        answer = w.posted[-1]
        check("D2 setup: the question was answered by the engine", ("Ada Lovelace" in answer.content, w.model.calls),
              (True, 1))
        m = Mark(w)
        await w.say("Sure.", who="member", reply_to=answer)
        quiet(check, m, who="D2 'Sure.' to an engine answer")
        return w.snapshot()


ACK_WORDS = ["ok", "thanks", "got it", "noted", "cool", "👍"]


@case
async def d3_acks_to_answer_quiet_line_and_r4_post(check, test_mode):
    """'ok' / 'thanks' / 'got it' / 'noted' / 'cool' / 👍 to an answer, a quiet-day line, an interim line, a post with no offer."""
    import wording
    with World(test_mode) as w:
        parents = {
            "an answer": w.bot_says("Acme AI is at DM sent since 22 Sep. Globex has a meeting on Thursday."),
            "a quiet-day line": w.bot_says(wording.NOTHING_TODAY),
            "an R4 post": drip_post(w, "**Deliverables**\nFour items are open:\n1. MSA template\n2. Pricing page copy",
                                    "deliverables", slot=1),
            "an objectives answer": w.bot_says("**Next steps**\nNext steps for some of our LinkedIn connections:"),
            "a confirmation": w.bot_says(wording.events_remind_set("Mon 12 Oct at 2 PM")),
            "an interim line": w.bot_says(wording.INTERIM_WEB[0]),
        }
        for what, parent in parents.items():
            for word in ACK_WORDS:
                m = Mark(w)
                await w.say(word, reply_to=parent)
                quiet(check, m, who=f"D3 {word!r} to {what}")
        check("D3: 36 acknowledgements, 36 reactions, no replies at all",
              (len(w.reactions), w.n_posted), (6 * len(parents), len(parents)))
        return w.snapshot()


@case
async def d4_yes_to_r3_post_schedules_its_reminder(check, test_mode):
    """D4: 'yes' replying directly to R3's post schedules its reminder; the confirmation names it, never 'these'."""
    import wording
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        post, _ = await w.send_r3_post(WED)
        key = next(iter(w.proposals()))
        m = Mark(w)
        await w.say("yes", reply_to=post)
        check("D4: the reminder is scheduled for Mon 12 Oct at 14:00", w.reminders(), [("2026-10-12", "14:00", "open")])
        check("D4: the confirmation names what it did", m.said, [wording.events_remind_set("Mon 12 Oct at 2 PM")])
        check("D4: ...never 'these'", [t for t in m.said if re.search(r"\bthese\b", t)], [])
        check("D4: the proposal is applied", w.proposals()[key], "applied")
        check("D4: no reaction, no model call", (m.reacted, m.d("model")), ([], 0))
        return w.snapshot()


@case
async def d5_sure_to_a_post_that_made_a_proposal(check, test_mode):
    """'sure' to R5's post with a found email applies THAT proposal, and only that one."""
    with World(test_mode, pretend=(date(2026, 10, 6), 14, 5)) as w:
        post = w.bot_says("**PoCs to contact**\n1. Ada Lovelace (CTO) — Acme AI — email found: ada@acme.ai\n"
                          "Want me to add the email I found to the sheet? Say yes.")
        mine = await _open_email(w, post)
        other_post = w.bot_says("Another post with its own offer.")
        theirs = w.open_remind(other_post.id)
        m = Mark(w)
        await w.say("sure", reply_to=post)
        check("D5: the email proposal on that post is applied", w.proposals()[mine], "applied")
        check("D5: ...and the other open proposal is untouched", w.proposals()[theirs], "open")
        check("D5: exactly one vote, on the replied-to proposal", [v[0].split(":")[0] for v in w.votes()],
              ["email_write"])
        check("D5: the email write was asked for once", [x for x in w.sheet.writes if x[0] == "write_email"],
              [("write_email", 5, "ada@acme.ai")])
        return w.snapshot()


async def _open_email(w, sent, *, marker="2026-10-06"):
    import nextaction
    msg = {"type": nextaction.R_PROSPECTS, "rule_id": "R5", "max_items_per_post": 5,
           "actions": [{"sheet_row": 5, "row_key": "acme|ada", "company": "Acme AI", "poc": "Ada Lovelace",
                        "email_found": "ada@acme.ai", "email_source": "https://acme.example/team",
                        "contact_key": ("Acme AI", "Ada Lovelace")}]}
    await w.bot._open_email_proposal(msg, sent=sent, marker=marker)
    keys = [k for k in w.proposals() if k.startswith("email_write:")]
    assert keys, "the email opener opened nothing"
    return keys[-1]


@case
async def d6_non_approver_sure_to_a_proposal(check, test_mode):
    """D6: a non-approver's 'sure' to a proposal gets the polite no; the proposal stays open; nothing written."""
    import approvals
    with World(test_mode) as w:
        post = w.bot_says("Want me to add the email I found to the sheet? Say yes.")
        key = await _open_email(w, post)
        m = Mark(w)
        await w.say("sure", who="member", reply_to=post)
        check("D6: the polite no, naming who can approve", m.said, [approvals.not_an_approver_reply("Kushal")])
        check("D6: the proposal stays open", w.proposals()[key], "open")
        check("D6: nothing was written", w.sheet.writes, [])
        check("D6: no vote was recorded", m.d("votes"), 0)
        return w.snapshot()


async def _two_open(w, *, age=1):
    """R3's remind offer (on its real post) and an email offer (on another post), both open in the channel."""
    r3, _ = await w.send_r3_post(WED)
    remind = next(iter(w.proposals()))
    other = w.bot_says("**PoCs to contact**\nWant me to add the email I found to the sheet? Say yes.")
    email = await _open_email(w, other)
    return r3, remind, other, email


@case
async def d7_bare_yes_two_open_asks_which(check, test_mode):
    """D7: '@Saley yes' (not a reply) with two proposals open asks which, numbered, naming both; no vote."""
    import wording
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        r3, remind, other, email = await _two_open(w)
        m = Mark(w)
        await w.say("yes", mention=True)
        text = "\n".join(m.said)
        check("D7: it asks which one", text.startswith(wording.which_proposal(["a", "b"]).splitlines()[0]), True)
        check("D7: ...numbered, naming BOTH", (bool(re.search(r"^1\. ", text, re.M)), bool(re.search(r"^2\. ", text, re.M)),
                                              "AI events reminder" in text, "ada@acme.ai" in text),
              (True, True, True, True))
        check("D7: no vote was recorded", m.d("votes"), 0)
        check("D7: both proposals are still open", sorted(w.proposals().values()), ["open", "open"])
        check("D7: no reminder, no write", (w.reminders(), w.sheet.writes), ([], []))
        return w.snapshot()


@case
async def d8_bare_yes_one_open_inside_the_window(check, test_mode):
    """D8: '@Saley yes' with exactly one open proposal, 5 minutes old, applies it."""
    import wording
    with World(test_mode) as w:
        post = w.bot_says("R3 post")
        key = w.open_remind(post.id, age_minutes=5)
        m = Mark(w)
        await w.say("yes", mention=True)
        check("D8: one open proposal, five minutes old: applied", w.proposals()[key], "applied")
        check("D8: the reminder was scheduled", len(w.reminders()), 1)
        return w.snapshot()


@case
async def d9_bare_yes_one_open_outside_the_window(check, test_mode):
    """D9: '@Saley yes' with one proposal 45 minutes old asks 'Is that a yes to ...?'; no vote."""
    import wording
    with World(test_mode) as w:
        post = w.bot_says("R3 post")
        key = w.open_remind(post.id, age_minutes=45)
        m = Mark(w)
        await w.say("yes", mention=True)
        label = label_of("events_remind", when="Mon 12 Oct")
        check("D9: 45 minutes old: the confirmation question, naming it", m.said, [wording.confirm_proposal(label)])
        check("D9: no vote, still open, nothing scheduled", (m.d("votes"), w.proposals()[key], w.reminders()),
              (0, "open", []))
        return w.snapshot()


@case
async def d10_number_reply_to_the_which_question(check, test_mode):
    """D10: '2' replying to the 'which one' question applies the second one named and not the first."""
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        r3, remind, other, email = await _two_open(w)
        await w.say("yes", mention=True)
        question = w.posted[-1]
        lines = {int(n): t for n, t in re.findall(r"^(\d)\. (.+)$", question.content, re.M)}
        second_is_email = "ada@acme.ai" in lines.get(2, "")
        want_applied, want_open = (email, remind) if second_is_email else (remind, email)
        m = Mark(w)
        await w.say("2", reply_to=question)
        check("D10: '2' applies the second one named", w.proposals()[want_applied], "applied")
        check("D10: ...and not the first", w.proposals()[want_open], "open")
        check("D10: one vote", m.d("votes"), 1)
        return w.snapshot()


@case
async def d11_yes_to_the_bots_own_shall_i_set_question(check, test_mode):
    """F1: a cell_update proposal was keyed to the ASKER's message, so a yes to the bot's own question only ever
    worked through the newest-open fallback. It is keyed to the bot's message now."""
    with World(test_mode) as w:
        key, asker, question = await _cell_proposal(w)
        check("D11: the proposal's message_id is the BOT's question, not the asker's message",
              (w.bot.db.proposal(key)["message_id"], w.bot.db.proposal(key)["message_id"] == str(asker.id)),
              (str(question.id), False))
        m = Mark(w)
        await w.say("yes", reply_to=question)
        check("D11: applied", w.proposals()[key], "applied")
        check("D11: the cell was written, once", [x for x in w.sheet.writes if x[0] == "write_cells"][:1] != [], True)
        # and a yes replied to a DIFFERENT bot message does not reach it
        key2, _a2, q2 = await _cell_proposal(w, key_suffix="2")
        other = w.bot_says("An unrelated answer.")
        m2 = Mark(w)
        await w.say("yes", reply_to=other)
        check("D11: 'yes' to a different bot message leaves the proposal open", w.proposals()[key2], "open")
        return w.snapshot()


async def _cell_proposal(w, key_suffix=""):
    import canned_sheet
    import gtm_sheet
    import sheetwrite
    rows = canned_sheet.pocs_values()
    w.sheet.set(pocs=rows[1:])
    tab = w.sheet.read()[gtm_sheet.POCS]
    row = next(r for r in tab.rows if r.get("_row") == 2)
    plan = sheetwrite.plan_writes(tab=tab, row=row, fields=[{"role": "meeting_date", "value": "21-10-2026",
                                                             "supersedes": False, "quote": "met Ada today"}],
                                  trigger=sheetwrite.TRIGGER_COMMAND, reply_text="met Ada today")
    asker = rw.HMsg(w, w.chan, "met Ada today", MEMBER)
    n = w.n_posted
    await w.bot._propose_write(asker, plan=plan, tab=tab, row=row, company="Acme AI", poc="Ada Lovelace",
                               trigger=sheetwrite.TRIGGER_COMMAND, reply_text="met Ada today")
    question = w.posted[n] if len(w.posted) > n else None
    key = f"prop:{asker.id}"
    return key, asker, question


@case
async def d11b_a_refused_send_opens_no_proposal(check, test_mode):
    """F1, second half: if the question never went out there is nothing to say yes to, so nothing is opened."""
    import guardrails
    with World(test_mode) as w:
        real = guardrails.send

        async def refused(*a, **k):
            return None
        guardrails.send = refused
        try:
            await _cell_proposal(w)
        finally:
            guardrails.send = real
        check("D11b: a refused send opens no proposal", w.proposals(), {})
        return w.snapshot()


@case
async def d12_yes_to_the_last_part_of_a_two_part_r3_post(check, test_mode):
    """F3: only the FIRST part of a split post was recorded, and R3's offer is in the LAST."""
    import wording
    with World(test_mode, pretend=(WED, 14, 5), pins={"QUERY_REPLY_CHUNK": 230}) as w:
        n = w.n_posted
        post, _ = await w.send_r3_post(WED)
        parts = w.posted[n:]
        check("D12 setup: the post went out in two or more parts, the offer in the last",
              (len(parts) >= 2, "Want me to remind you" in parts[-1].content), (True, True))
        key = next(iter(w.proposals()))
        m = Mark(w)
        await w.say("yes", reply_to=parts[-1])
        check("D12: a yes to the LAST part schedules the reminder", w.reminders(), [("2026-10-12", "14:00", "open")])
        check("D12: ...and says what it did", m.said, [wording.events_remind_set("Mon 12 Oct at 2 PM")])
        check("D12: the proposal is applied", w.proposals()[key], "applied")
        return w.snapshot()


@case
async def d13_second_yes_says_it_was_answered(check, test_mode):
    """D13: a second 'yes' to an already-answered offer says so; still one reminder."""
    import wording
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        post, _ = await w.send_r3_post(WED)
        await w.say("yes", reply_to=post)
        m = Mark(w)
        await w.say("yes", reply_to=post)
        label = label_of("events_remind", when="Mon 12 Oct")
        check("D13: the second yes gets the already-answered line", m.said, [wording.offer_closed(label)])
        check("D13: still exactly one reminder", len(w.reminders()), 1)
        check("D13: nothing else ran", (m.d("model"), m.d("votes")), (0, 0))
        return w.snapshot()


@case
async def d14_sure_to_a_read_offer_runs_it_as_the_question(check, test_mode):
    """D14: 'sure' to an answer ending 'Want me to pull the full list for Acme?' runs that as the question, parent as context."""
    with World(test_mode) as w:
        parent = w.bot_says("Acme AI has two PoCs on the sheet. Want me to pull the full list for Acme?")
        m = Mark(w)
        await w.say("sure", reply_to=parent)
        seen = w.model.request_text(0)
        check("D14: the engine ran (a read offer runs as the question)", m.d("model") >= 1, True)
        check("D14: ...asked to 'pull the full list for Acme'", "pull the full list for Acme" in seen, True)
        check("D14: ...with the parent message as context, marked as data",
              ("[Context: this message is a reply" in seen, "two PoCs on the sheet" in seen, "DATA" in seen),
              (True, True, True))
        check("D14: not a reaction, not a vote, not the extractor", (m.reacted, m.d("votes"), m.d("extractor")),
              ([], 0, 0))
        check("D14: it answered", len(m.said) >= 1, True)
        return w.snapshot()


@case
async def d15_sure_to_a_write_offer_opens_a_proposal_and_writes_nothing(check, test_mode):
    """D15: 'sure' to 'Shall I update the stage ...?' goes to the extractor as if typed; a cell_update proposal waits; zero writes."""
    import wording
    with World(test_mode) as w:
        parent = w.bot_says("Acme AI is at DM sent. Shall I update the stage to Demo for Acme?")
        m = Mark(w)
        await w.say("sure", reply_to=parent)
        check("D15: the offered action went to the extractor as if typed",
              [t for t in w.llm.extract_texts if "update the stage to Demo for Acme" in t] != [], True)
        check("D15: the extractor found nothing to change -> it asks for the detail", m.said,
              [wording.OFFER_NEEDS_DETAIL])
        check("D15: nothing was written, nothing proposed on the sure", (w.sheet.writes, w.proposals()), ([], {}))
        check("D15: no vote, no reaction", (m.d("votes"), m.reacted), (0, []))

    # the same offer when the extractor DOES find an update: a proposal is opened and waits
    with World(test_mode) as w2:
        import canned_sheet
        w2.sheet.set(pocs=canned_sheet.pocs_values()[1:])
        w2.llm.extract_result = {"intent": "update", "company": "Acme AI", "poc": "Ada Lovelace", "confidence": 0.9,
                                 "fields": [{"role": "meeting_date", "value": "21-10-2026", "supersedes": False,
                                             "quote": "update the stage"}]}
        parent2 = w2.bot_says("Acme AI is at DM sent. Shall I update the stage to Demo for Acme?")
        m2 = Mark(w2)
        await w2.say("sure", reply_to=parent2)
        check("D15b: ONE cell_update proposal is opened and is open", [(k.split(":")[0], s) for k, s in w2.proposals().items()],
              [("prop", "open")])
        check("D15b: ZERO writes on the 'sure'", w2.sheet.writes, [])
        check("D15b: the proposal names the cells and waits for an approver's separate yes",
              ("Shall I set" in "\n".join(m2.said) or "Reply yes" in "\n".join(m2.said)), True)
        check("D15b: no vote was recorded for the 'sure' itself", m2.d("votes"), 0)
        return w2.snapshot()


@case
async def d16_ok_and_a_real_question_is_answered_with_the_parent(check, test_mode):
    """TICKET EDGE CASE 2: 'ok' is a vote word, but the message is a question. With an offer open it must not vote."""
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        r3, remind, _other, _email = await _two_open(w)
        parent = w.bot_says("Acme AI is at DM sent since 22 Sep. Ada Lovelace is the CTO.")
        m = Mark(w)
        await w.say("ok, and what about Globex?", reply_to=parent)
        seen = w.model.request_text(0)
        check("D16: answered by the engine", m.d("model") >= 1 and len(m.said) >= 1, True)
        check("D16: the engine's question carries the parent, marked as data",
              ("[Context: this message is a reply" in seen, "Ada Lovelace is the CTO" in seen,
               "what about Globex" in seen), (True, True, True))
        check("D16: no vote although 'ok' is a vote word", (m.d("votes"), sorted(w.proposals().values())),
              (0, ["open", "open"]))
        check("D16: no reaction (it is not an acknowledgement)", m.reacted, [])
        return w.snapshot()


@case
async def d17_a_question_with_the_word_right_is_not_a_vote(check, test_mode):
    """F2: read_vote matched a vote word anywhere in the message."""
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        post, _ = await w.send_r3_post(WED)
        key = next(iter(w.proposals()))
        m = Mark(w)
        await w.say("what's the right contact there?", reply_to=post)
        check("D17: answered normally", m.d("model") >= 1 and len(m.said) >= 1, True)
        check("D17: no vote, the offer is still open, no reminder", (m.d("votes"), w.proposals()[key], w.reminders()),
              (0, "open", []))
        m2 = Mark(w)
        await w.say("is there no news today?", reply_to=post)
        check("D17: 'is there no news today?' does not decline the offer", (m2.d("votes"), w.proposals()[key]), (0, "open"))
        return w.snapshot()


@case
async def d18_thanks_to_a_persons_reply_to_a_bot_answer(check, test_mode):
    """TICKET EDGE CASE 1 (a reply to a reply): hop 2 is the bot, so 'thanks' is acknowledged."""
    with World(test_mode) as w:
        answer = w.bot_says("Acme AI has two PoCs on the sheet.")
        h1 = rw.HMsg(w, w.chan, "interesting, only two?", MEMBER, reference=rw._ref(answer))
        m = Mark(w)
        await w.say("thanks", reply_to=h1, mention=True)
        quiet(check, m, who="D18 '@Saley thanks' replying to a person's reply to a bot answer")
        return w.snapshot()


@case
async def d19_a_question_replying_to_a_persons_reply(check, test_mode):
    """D19: a real question replying to a person's reply to a bot answer: the engine gets both messages as context."""
    with World(test_mode) as w:
        answer = w.bot_says("Acme AI has two PoCs on the sheet: Ada Lovelace and Sam Lee.")
        h1 = rw.HMsg(w, w.chan, "interesting, only two?", MEMBER, reference=rw._ref(answer))
        m = Mark(w)
        await w.say("and for Globex?", reply_to=h1, mention=True)
        seen = w.model.request_text(0)
        check("D19: the engine ran", m.d("model") >= 1, True)
        check("D19: the context has the bot's message AND the person's in between, oldest first",
              (("Ada Lovelace and Sam Lee" in seen), ("only two?" in seen),
               seen.find("Ada Lovelace and Sam Lee") < seen.find("only two?")), (True, True, True))
        check("D19: no vote, no reaction", (m.d("votes"), m.reacted), (0, []))
        return w.snapshot()


@case
async def d20_parent_deleted_yes_with_a_proposal_open(check, test_mode):
    """D20: a reply whose parent was deleted, 'yes', a proposal open elsewhere: one reaction, no vote."""
    with World(test_mode) as w:
        post = w.bot_says("R3 post")
        key = w.open_remind(post.id)
        gone = w.bot_says("an answer that was deleted")
        gone.deleted = True
        m = Mark(w)
        await w.say("yes", reply_to=gone, cached=False, bot_in_mentions=True)
        check("D20: one reaction, no text", ([e for _t, e in m.reacted], m.said), ([ack_emoji()], []))
        check("D20: no vote, the other proposal is untouched, nothing scheduled",
              (m.d("votes"), w.proposals()[key], w.reminders()), (0, "open", []))
        check("D20: no model call", m.d("model"), 0)
        # the same reply WITHOUT a ping and with a deleted parent is not addressed to the bot at all
        m2 = Mark(w)
        await w.say("yes", reply_to=gone, cached=False)
        check("D20b: an unaddressed reply to a deleted message is ignored: nothing at all",
              (m2.reacted, m2.said, m2.d("votes")), ([], [], 0))
        return w.snapshot()


@case
async def d21_thanks_not_a_reply_nothing_open(check, test_mode):
    """D21: '@Saley thanks', not a reply, nothing open: one reaction (Q6)."""
    with World(test_mode) as w:
        m = Mark(w)
        await w.say("thanks", mention=True)
        if Q6_NON_REPLY_ACK_REACTS:
            quiet(check, m, who="D21 '@Saley thanks' (not a reply, nothing open)")
        return w.snapshot()


@case
async def d22_yes_to_an_r13_post_is_not_a_vote(check, test_mode):
    """'yes' to 'Has it gone out?' is an answer about something else; an email offer is open elsewhere."""
    import nextaction
    with World(test_mode) as w:
        email_post = w.bot_says("Want me to add the email I found to the sheet? Say yes.")
        key = await _open_email(w, email_post)
        r13 = drip_post(w, "**Next steps**\n• Person 01 (Acme Labs 01): Next Steps says Send email 2. Has it gone out? "
                           "If so, mark 2nd Email Sent and the date.", nextaction.R_NEXT_STEPS, slot=2)
        m = Mark(w)
        await w.say("yes", reply_to=r13)
        check("D22: not a vote; the email offer elsewhere is untouched", (m.d("votes"), w.proposals()[key]), (0, "open"))
        check("D22: nothing was written", w.sheet.writes, [])
        check("D22: not acknowledged with a reaction (the post asks a question; 'yes' answers it)", m.reacted, [])
        return w.snapshot()


@case
async def d23_thanks_to_r3_post_leaves_the_offer_open(check, test_mode):
    """D23: 'thanks' to R3's post: one reaction; the offer stays open."""
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        post, _ = await w.send_r3_post(WED)
        key = next(iter(w.proposals()))
        m = Mark(w)
        await w.say("thanks", reply_to=post)
        check("D23: one reaction, no text", ([e for _t, e in m.reacted], m.said), ([ack_emoji()], []))
        check("D23: the offer is still open, nothing scheduled, no vote", (w.proposals()[key], w.reminders(), m.d("votes")),
              ("open", [], 0))
        return w.snapshot()


@case
async def d24_no_to_r3_post_declines_silently(check, test_mode):
    """D24: 'no' (approver) to R3's post declines it silently, as before."""
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        post, _ = await w.send_r3_post(WED)
        key = next(iter(w.proposals()))
        m = Mark(w)
        await w.say("no", reply_to=post)
        check("D24: declined, silently (as today)", (w.proposals()[key], m.said, m.reacted), ("declined", [], []))
        check("D24: no reminder", w.reminders(), [])
        return w.snapshot()


# ==================================================================================================================
# THE REST OF THE PLAN'S EDGE CASES (13.3 E, P, I) and the ticket's four.
# ==================================================================================================================

@case
async def e1_chains_three_hops_and_beyond(check, test_mode):
    """A reply to a reply, at most 3 hops. Hop 3 is the bot: context. Hop 4: no parent, answered plain, no vote."""
    with World(test_mode) as w:
        post = w.bot_says("R3 post")
        key = w.open_remind(post.id)
        answer = w.bot_says("The three PoCs at Acme AI are Ada Lovelace, Sam Lee and Grace Hopper.")
        h1 = rw.HMsg(w, w.chan, "huh, three?", MEMBER, reference=rw._ref(answer))
        h2 = rw.HMsg(w, w.chan, "yes three", APPROVER2, reference=rw._ref(h1))
        m = Mark(w)
        await w.say("and Globex?", reply_to=h2, mention=True)          # hop1 h2, hop2 h1, hop3 = the bot
        seen = w.model.request_text(0)
        check("E1: bot message at exactly 3 hops IS found and quoted", ("Grace Hopper" in seen, "huh, three?" in seen),
              (True, True))

    with World(test_mode) as w:
        post = w.bot_says("R3 post")
        key = w.open_remind(post.id)
        answer = w.bot_says("The three PoCs at Acme AI are Ada Lovelace, Sam Lee and Grace Hopper.")
        h1 = rw.HMsg(w, w.chan, "huh, three?", MEMBER, reference=rw._ref(answer))
        h2 = rw.HMsg(w, w.chan, "yes three", APPROVER2, reference=rw._ref(h1))
        h3 = rw.HMsg(w, w.chan, "ok", MEMBER, reference=rw._ref(h2))
        m = Mark(w)
        await w.say("and Globex?", reply_to=h3, mention=True)          # the bot is 4 hops up
        seen = w.model.request_text(0)
        check("E1: a bot message BEYOND 3 hops is not quoted; answered without parent context",
              ("[Context: this message is a reply" in seen, "Grace Hopper" in seen), (False, False))
        check("E1: it was still answered", m.d("model") >= 1 and len(m.said) >= 1, True)
        m2 = Mark(w)
        await w.say("yes", reply_to=h3, mention=True)                   # no bot message within 3 hops
        check("E1: 'yes' with no bot message within 3 hops votes on nothing", (m2.d("votes"), w.proposals()[key]),
              (0, "open"))
        return w.snapshot()


@case
async def e8_the_bare_yes_window(check, test_mode):
    """29 minutes applies, 31 asks, PROPOSAL_BARE_YES_MINUTES=0 always asks, another channel's proposal is no candidate."""
    for age, want in ((29, "applied"), (31, "open")):
        with World(test_mode) as w:
            post = w.bot_says("R3 post")
            key = w.open_remind(post.id, age_minutes=age)
            await w.say("yes", mention=True)
            check(f"E8: one proposal {age} minutes old: {want}", w.proposals()[key], want)
    with World(test_mode, pins={"PROPOSAL_BARE_YES_MINUTES": 0}) as w:
        post = w.bot_says("R3 post")
        key = w.open_remind(post.id, age_minutes=0)
        m = Mark(w)
        await w.say("yes", mention=True)
        check("E8: PROPOSAL_BARE_YES_MINUTES=0: a bare yes never falls back; it asks", (w.proposals()[key], len(m.said)),
              ("open", 1))
    with World(test_mode) as w:
        elsewhere = w.bot_says("R3 post elsewhere", channel=w.other)
        key = w.open_remind(elsewhere.id, age_minutes=2, channel_id=rw.OTHER_CHANNEL)
        m = Mark(w)
        await w.say("yes", mention=True)
        check("E8: a proposal in ANOTHER channel is not a candidate", (w.proposals()[key], m.d("votes")), ("open", 0))
        return w.snapshot()


@case
async def e9_a_non_approver_acknowledges_an_interim_line(check, test_mode):
    """E9: a non-approver's 'sure' to an interim line is a reaction, like anyone's."""
    import wording
    with World(test_mode) as w:
        interim = w.bot_says(wording.INTERIM_ENGINE[0])
        m = Mark(w)
        await w.say("sure", who="member", reply_to=interim)
        quiet(check, m, who="E9 a non-approver's 'sure' to an interim line (a reaction, like anyone's)")
        return w.snapshot()


@case
async def e10_a_reply_never_falls_back_to_the_newest_open_proposal(check, test_mode):
    """THE ROOT OF BOTH BUGS. Every shape of reply, an approver, a proposal of EVERY kind open somewhere else."""
    import wording
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        # the row_add offer is the OLDEST: NFT2-1065's guard stopped a stray yes reaching a row_add that was the
        # newest, so the newest open proposal here is a different kind (the case that used to be approved by a stray yes)
        row_add = w.open_proposal(kind="row_add", message_id=w.bot_says("add offer").id, age_minutes=5,
                                  payload={"people": [{"name": "Janajit Bagchi", "company": "ARTPARK India"}]})
        r3, remind, other, email = await _two_open(w)
        keys = list(w.proposals())
        unrelated = [w.bot_says(t) for t in ("An answer.", wording.INTERIM_WEB[0], wording.NOTHING_TODAY)]
        for parent in unrelated:
            for word in ("yes", "sure", "ok", "yep", "go ahead", "no", "nope"):
                await w.say(word, reply_to=parent)
        check("E10: 21 replies to unrelated bot messages changed nothing", (w.votes(), sorted(w.proposals().values()),
                                                                          w.reminders(), w.sheet.writes),
              ([], ["open"] * len(keys), [], []))
        return w.snapshot()


# -- the proposals of every kind (P1) --------------------------------------------------------------------------

async def _kind_opener(w, kind):
    """Open ONE proposal of `kind` with its REAL opener against a fresh bot message. Returns (message, key)."""
    import nextaction
    sent = w.bot_says(f"post carrying a {kind} offer")
    marker = "2026-10-07"
    if kind == "cell_update":
        key, _asker, question = await _cell_proposal(w)
        return question, key
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
        return sent, await _open_email(w, sent)
    elif kind == "events_remind":
        act = {"event_line": "You're registered for Voice AI Forum on Sat 17 Oct", "remind_on": "2026-10-12",
               "remind_word": "Monday", "event_link": ""}
        await w.bot._open_events_remind_proposal({"type": nextaction.R_EVENTS, "max_items_per_post": 5,
                                                  "actions": [act]}, sent=sent, marker=marker)
    elif kind == "row_add":
        key = w.open_proposal(kind="row_add", message_id="", payload={"people": [{"name": "Janajit Bagchi",
                                                                                "company": "ARTPARK India"}]})
        w.bot.db.set_proposal_message(key, str(sent.id))
        return sent, key
    keys = [k for k in w.proposals() if k.startswith(kind + ":")]
    assert keys, f"the real opener for {kind} opened nothing"
    return sent, keys[-1]


PROPOSAL_KINDS = ["cell_update", "poc_lookup", "event_append", "event_deadline", "email_write", "events_remind",
                  "row_add"]


@case
async def p1_every_kind_is_found_by_its_own_message_only(check, test_mode):
    """'sure' replying to the post that carries the proposal applies THAT proposal and no other, for EVERY kind;
    'sure' replying to a different bot message applies nothing. (Applying is recorded, not run: the apply paths
    have their own checks; what is tested here is which proposal a reply reaches.)"""
    for kind in PROPOSAL_KINDS:
        with World(test_mode, pretend=(WED, 14, 5)) as w:
            applied = []

            async def record(message, proposal, *, decided_by, why=""):
                applied.append(proposal["proposal_key"])
            w.bot._apply_approved_write = record
            # a bystander proposal of another kind elsewhere, which must never be touched
            bystander_post = w.bot_says("bystander")
            bystander = w.open_remind(bystander_post.id)
            sent, key = await _kind_opener(w, kind)
            decoy = w.bot_says("An answer that carries no offer.")
            await w.say("sure", reply_to=decoy)
            check(f"P1 {kind}: 'sure' to a DIFFERENT bot message applies nothing", (applied, w.proposals()[key]),
                  ([], "open"))
            await w.say("sure", reply_to=sent)
            check(f"P1 {kind}: 'sure' to its own message applies that proposal", (applied, w.proposals()[key]),
                  ([key], "applied"))
            check(f"P1 {kind}: ...and not the bystander", w.proposals()[bystander], "open")
        # the live/test comparison rides on the last kind's world
    return {"kinds": PROPOSAL_KINDS}


@case
async def p2_every_kind_through_the_vote_by_the_wrong_person(check, test_mode):
    """A non-approver's 'sure' to a proposal of any kind: the polite no, the proposal stays open."""
    import approvals
    for kind in PROPOSAL_KINDS:
        with World(test_mode, pretend=(WED, 14, 5)) as w:
            sent, key = await _kind_opener(w, kind)
            m = Mark(w)
            await w.say("sure", who="member", reply_to=sent)
            check(f"P2 {kind}: the polite no", m.said, [approvals.not_an_approver_reply("Kushal")])
            check(f"P2 {kind}: still open, nothing written", (w.proposals()[key], w.sheet.writes), ("open", []))
    return {"kinds": PROPOSAL_KINDS}


# ==================================================================================================================
# THE OBJECTIVES ANSWER (plan 8) — directly, and through on_message.
# ==================================================================================================================

def forbidden_in(text):
    return [name for name, rx in FORBIDDEN_IN_OBJECTIVES if rx.search(text or "")]


def _objectives_world(test_mode, *, hh=11, mm=0, day=OBJ_DAY, rules=("R3", "R13"), **kw):
    import rule13_fixtures as fx
    w = World(test_mode, pretend=(day, hh, mm), rules=set(rules), **kw)
    return w, fx


async def _stock_sheet(w, fx, day=OBJ_DAY):
    rows, _meta = fx.rotation_values()
    w.sheet.set(pocs=rows, events=rw.events_for(day))


async def _real_posts(test_mode, fx, day=OBJ_DAY, rules=("R3", "R13")):
    """What the REAL sender posts for the same day and queue, in a second world: {type: posted text}."""
    import deadlines as dl
    w2, _ = _objectives_world(test_mode, hh=14, mm=5, day=day, rules=rules)
    with w2:
        await _stock_sheet(w2, fx, day)
        planned = await w2.bot._plan_drip(today=day, already=[])
        out = {}
        for m in planned["messages"]:
            n = w2.n_posted
            await w2.bot._send_drip_message(w2.chan, m, marker=dl.iso(day), channel_id=w2.chan.id)
            out[m["type"]] = "\n".join(strip_tag(p.content) for p in w2.posted[n:])
        return out


def norm_openers(text):
    """R3's intro line is one of three (`drip.EVENT_OPENERS`) drawn at random when the post is SENT; the on-demand
    answer draws its own, seeded by day and slot (plan 8.4). The list under it is identical. Compare the rest."""
    return text          # the sender now seeds its wording by (day, slot) too (plan section 20, B1): nothing to normalise


def content_lines(block):
    """What a post SAYS, minus what it draws at random when it is SENT: the heading, the owner's tags line and the
    one intro line ("2 open:", "Events in the next two weeks:", "Next steps for some of our LinkedIn
    connections:"; each rule has three variants, `tone.pick_index`). The on-demand answer draws its own intro,
    seeded by day and slot (plan 8.4), so for a post that has NOT gone out yet the intro can differ from the one the
    sender will draw; every other line is the same."""
    out = []
    for line in str(block or "").splitlines():
        if re.fullmatch(r"\*\*.+\*\*", line.strip()):
            continue                                              # the heading
        if re.fullmatch(r"(<@!?\d+>\s*)+", line.strip()):
            continue                                              # the tags line
        out.append(line.rstrip())
    return out


def blocks_match(answer, posts, order):
    """The answer's blank-line-separated blocks carry the same content lines as the real posts, in `order`
    (closing offer lines removed from the real posts: Q3)."""
    got = [content_lines(b) for b in str(answer).split("\n\n")]
    want = [content_lines(_without_offer_line(posts[t])) for t in order]
    return got == want


def _without_offer_line(post):
    lines = post.rstrip().splitlines()
    return "\n".join(lines[:-1]) if lines and lines[-1].startswith("Want me to") else post


@case
async def o_objectives_text_is_what_the_posts_contain(check, test_mode):
    """THE ANSWER IS THE DAY'S POSTS, word for word: the same planner, composer and formatting as the real sender."""
    import wording
    w, fx = _objectives_world(test_mode)
    posts = await _real_posts(test_mode, fx)
    with w:
        await _stock_sheet(w, fx)
        before = w.counts()
        text = await w.bot._todays_objectives()
        check("O: the blocks are the real posts, in plan order, one blank line apart, no lead-in "
              "(same lines; an unsent post's intro line is its own seeded draw)",
              blocks_match(text, posts, ["events", "next_step_followups"]), True)
        check("O: every person the real R13 post names is in the answer, with the same sentence",
              all(l in text for l in posts["next_step_followups"].splitlines() if l.startswith("•")), True)
        check("O: the closing offer is not in the answer (Q3)", ("Want me to remind you" in text) == Q3_OFFER_LINES_IN_OBJECTIVES, True)
        check("O: none of the forbidden words (times, rules, rule numbers, schedule words)", forbidden_in(text), [])
        check("O: no AI-news block (Q1)", ("AI news" in text) == Q1_AI_NEWS_IN_OBJECTIVES, True)
        check("O: no @-mention in the answer (the tags line is left out: asking pings nobody)", "<@" in text, False)
        check("O: zero model calls, zero drip rows added, zero proposals",
              (w.counts()["model"] - before["model"], w.counts()["drip"], w.proposals()), (0, 0, {}))
        print("      objectives answer as Discord would show it:")
        for line in text.splitlines():
            print("      | " + line)
        return {"text": text}


@case
async def o_never_does_what_a_send_does(check, test_mode):
    """O2: read-only. Every send-side call is replaced by one that fails the check."""
    import leave
    import search_backend
    w, fx = _objectives_world(test_mode)
    with w:
        await _stock_sheet(w, fx)
        called = []

        def trip(name):
            def inner(*a, **k):
                called.append(name)
                raise AssertionError(f"objectives called {name}")
            return inner

        def trip_async(name):
            async def inner(*a, **k):
                called.append(name)
                raise AssertionError(f"objectives called {name}")
            return inner

        for n in ("record_drip_send", "attach_drip_message_id"):
            setattr(w.bot.db, n, trip(n))
        for n in ("_open_events_remind_proposal", "_open_event_proposals", "_open_email_proposal",
                  "_open_poc_lookup_proposal", "_research_message", "_request_email_lookups",
                  "_convert_if_already_done", "_after_send", "_send_drip_message"):
            if hasattr(w.bot, n):
                setattr(w.bot, n, trip_async(n))
        leave.who_is_away = trip_async("leave.who_is_away")
        w.llm.proactive_message = trip_async("llm.proactive_message")
        real_search, real_detail = search_backend.search, search_backend.search_detail
        search_backend.search = trip("search_backend.search")
        search_backend.search_detail = trip("search_backend.search_detail")
        try:
            text = await w.bot._todays_objectives()
        finally:
            search_backend.search, search_backend.search_detail = real_search, real_detail
        check("O2: nothing a send does was called", called, [])
        check("O2: the model was not called", w.model.calls, 0)
        check("O2: it returned the day's blocks", bool(text.strip()) and text != "", True)
        return {"text": text}


@case
async def o_nothing_today_is_one_line(check, test_mode):
    """O: a day with nothing says so in one line; a Saturday; an unreadable drip_sends says 'can't read', never a guess."""
    import wording
    w, fx = _objectives_world(test_mode, rules=("R3",))
    with w:
        w.sheet.set(pocs=[], events=[])           # an empty events tab: R3 has nothing to say
        text = await w.bot._todays_objectives()
        check("O: a day with nothing says so in one line", text, wording.NOTHING_TODAY)
        sat = None
    w, fx = _objectives_world(test_mode, day=date(2026, 10, 3), rules=("R3", "R13"))     # a Saturday
    with w:
        await _stock_sheet(w, fx, date(2026, 10, 3))
        text = await w.bot._todays_objectives()
        check("O5: a Saturday (not a sending day): one line, nothing planned", text, wording.NOTHING_TODAY)
    w, fx = _objectives_world(test_mode)
    with w:
        await _stock_sheet(w, fx)

        def boom(*a, **k):
            import sqlite3
            raise sqlite3.OperationalError("unreadable")
        w.bot.db.drip_sent_today = boom
        text = await w.bot._todays_objectives()
        check("O5: an unreadable drip_sends: the 'can't read' line, never a guess", text, wording.OBJECTIVES_UNREADABLE)
        return {"text": text}


@case
async def e3_objectives_at_1358_and_1405_with_the_2pm_post_firing_once(check, test_mode):
    """TICKET EDGE CASE 3. The answer claims no slot, so the scheduled 14:00 post still fires exactly once."""
    import clock
    import deadlines as dl
    from datetime import time
    w, fx = _objectives_world(test_mode, hh=13, mm=58, rules=("R3", "R13"))
    posts = await _real_posts(test_mode, fx)
    with w:
        await _stock_sheet(w, fx)
        w.bot._live_loop_held = lambda what: False
        w.bot.get_channel = lambda cid: w.chan if int(cid) == w.chan.id else None
        t1358 = await w.bot._todays_objectives()
        check("E3: at 13:58 the answer holds the day's content", (bool(t1358), forbidden_in(t1358)), (True, []))
        check("E3: ...and claimed nothing: no drip row, no proposal", (w.drip_rows(), w.proposals()), ([], {}))
        await w.bot._maybe_send_drip()                                  # the 13:58 tick: nothing is due
        check("E3: the 13:58 sweep tick posts nothing", w.n_posted, 0)
        clock.set_time_of_day(time(14, 5), by="verify_replies")
        t1405_before = await w.bot._todays_objectives()
        check("E3: at 14:05, before the 2pm post went, the same text", t1405_before, t1358)
        await w.bot._maybe_send_drip()                                  # the tick that sends the 14:00 post
        rows = w.drip_rows()
        check("E3: the 2pm post fired, once", [r[2] for r in rows].count("events"), 1)
        t1405_after = await w.bot._todays_objectives()
        check("E3: once it went, the answer shows what was posted (stored body): the same blocks again "
              "(R3's intro line is the variant the sender drew)", norm_openers(t1405_after), norm_openers(t1358))
        await w.bot._maybe_send_drip()
        check("E3: the next tick does not post it again", [r[2] for r in w.drip_rows()].count("events"), 1)
        check("E3: asking claimed no extra slot or send", len(w.drip_rows()) <= 2, True)
        return {"rows": [r[2] for r in w.drip_rows()], "text": t1358}


@case
async def e4_two_people_asking_at_once(check, test_mode):
    """TICKET EDGE CASE 4. Two read-only runs over the same state: equal text, nothing recorded, cap unchanged."""
    import drip
    import deadlines as dl
    w, fx = _objectives_world(test_mode)
    with w:
        await _stock_sheet(w, fx)
        w.model.answer = "To-dos: send the Acme deck."
        rows0 = w.bot.db.drip_sent_today(dl.iso(dl.today_ist()))
        a, b = await asyncio.gather(w.bot._todays_objectives(), w.bot._todays_objectives())
        check("E4: two concurrent runs return equal text", a == b and bool(a), True)
        check("E4: nothing was recorded", w.drip_rows(), [])
        check("E4: the cap counter did not move", drip.counted_today(w.bot.db.drip_sent_today(dl.iso(dl.today_ist()))),
              drip.counted_today(rows0))
        # and through on_message from two people at the same moment
        q = "what are the sales objectives for today?"
        m = Mark(w)
        await asyncio.gather(w.say(q, who="approver", mention=True), w.say(q, who="member", mention=True))
        replies = m.said
        check("E4: two askers, two replies", len(replies), 2)
        check("E4: the objectives part of each is the same text", all(a in r for r in replies), True)
        check("E4: still nothing recorded; no reaction", (w.drip_rows(), m.reacted), ([], []))
        return {"text": a}


@case
async def o_asked_through_on_message_replies_with_the_objectives(check, test_mode):
    """6 OCT STEP 4: 'what are the sales objectives for today?' got the posting rules. Through the real path, at 11:00."""
    import toolsets
    w, fx = _objectives_world(test_mode)
    with w:
        await _stock_sheet(w, fx)
        text_direct = await w.bot._todays_objectives()
        # the model picks nothing from the tools: the code guarantees the answer anyway
        w.model.answer = "TODO-PART: send the Acme deck."
        m = Mark(w)
        await w.say("what are the sales objectives for today?", who="member", mention=True)
        reply = "\n".join(m.said)
        check("O-route: the objectives are in the reply even when the model never calls the tool",
              text_direct in reply, True)
        check("O-route: the model's to-do part follows after a blank line",
              reply.endswith("\n\nTODO-PART: send the Acme deck."), True)
        check("O-route: nothing about rules, times or the schedule anywhere in the reply", forbidden_in(reply), [])
        check("O-route: the tools offered are exactly show_todos and todays_objectives (cadence_preview is not)",
              sorted(w.model.offered[0]), ["show_todos", "todays_objectives"])
        check("O-route: two model calls at most", w.model.calls <= 2, True)
        check("O-route: no drip row, no proposal, no reaction", (w.drip_rows(), w.proposals(), m.reacted), ([], {}, []))
        # the model that DOES call the tool gets only a pointer back, never the text
        w.model.script = [("use", [("todays_objectives", {})]), ("say", "TODO-PART two.")]
        w.model.requests.clear()
        await w.say("objectives for today", who="member", mention=True)
        results = [r for n, r in w.model.results if n == "todays_objectives"]
        check("O-route: the model's tool result is only the pointer", (len(results) >= 1, results[-1].get("added_to_reply")
                                                                      if results else None), (True, True))
        check("O-route: ...and does not carry the text", text_direct[:40] in json_text(results), False)
        return {"text": text_direct}


def json_text(x):
    import json
    return json.dumps(x, ensure_ascii=False, default=str)


@case
async def o3_replying_yes_to_the_objectives_answer_is_an_ack(check, test_mode):
    """O3: 'yes' to the objectives answer is acknowledged (it made no offer)."""
    with World(test_mode) as w:
        ans = w.bot_says("**Next steps**\nNext steps for some of our LinkedIn connections:\n• Person 01 (Acme Labs 01)")
        m = Mark(w)
        await w.say("yes", reply_to=ans)
        quiet(check, m, who="O3 'yes' to the objectives answer (it made no offer)")
        return w.snapshot()


# ==================================================================================================================
# THE INTERIM LINE (plan 6) — real timers, short waits.
# ==================================================================================================================

@case
async def i1_interim_wording_follows_what_actually_ran(check, test_mode):
    """Engine wording until a web_search has been DISPATCHED; web wording after. The wait is still the web wait."""
    import wording
    if True:
        # (i) a web-worded question, no tool call before the wait: ENGINE wording
        with World(test_mode, pins={"INTERIM_ENABLED": True, "INTERIM_AFTER_SECONDS": 0.6,
                                    "INTERIM_AFTER_WEB_SECONDS": 0.3}) as w:
            async def web_pair(*a, **k):                  # the web tools are OFFERED (so the question is a "web turn")...
                return [stub_tool_named("web_search"), stub_tool_named("fetch_page")], "", ""
            w.bot._websearch_tools = web_pair
            w.model.script = [("slow", 1.0, ("say", "Underdog AI is in Austin."))]    # ...but the model never calls them
            t0 = time.monotonic()
            await w.say("Underdog AI funding and HQ", who="member", mention=True)
            lines = [strip_tag(m.content) for m in w.posted]
            at = next((m.t - t0 for m in w.posted if strip_tag(m.content) in wording.INTERIM_WEB + wording.INTERIM_ENGINE),
                      None)
            check("I1 (i): the WAIT is still the web wait (0.3 s here, not the engine's 0.6 s)",
                  at is not None and at < 0.55, True)
            interims = [l for l in lines if l in wording.INTERIM_WEB + wording.INTERIM_ENGINE]
            check("I1 (i): an interim went out, once", len(interims), 1)
            check("I1 (i): no web_search was dispatched: the ENGINE wording", interims[0] in wording.INTERIM_ENGINE
                  if interims else None, True)
            check("I1 (i): the answer followed", "Underdog AI is in Austin." in lines, True)
        # (ii) a web_search dispatched before the wait: WEB wording
        with World(test_mode, pins={"INTERIM_ENABLED": True, "INTERIM_AFTER_SECONDS": 1.5,
                                    "INTERIM_AFTER_WEB_SECONDS": 1.2, "WEB_QUESTION_MAX_SEARCHES": 4,
                                    "WEB_SEARCH_DAILY_BUDGET": 60}) as w:
            queries = rw.enable_web(w)               # (wide margins: the search is dispatched well before the wait ends)
            w.model.script = [("use", [("web_search", {"query": "Underdog AI funding HQ"})]),
                              ("slow", 2.4, ("say", "Underdog AI is in Austin."))]
            await w.say("Underdog AI funding and HQ", who="member", mention=True)
            lines = [strip_tag(m.content) for m in w.posted]
            interims = [l for l in lines if l in wording.INTERIM_WEB + wording.INTERIM_ENGINE]
            check("I1 (ii): a search really ran", len(queries) >= 1, True)
            check("I1 (ii): the WEB wording", interims[0] in wording.INTERIM_WEB if interims else None, True)
    return {}


@case
async def i2_news_question_answered_from_collected_news_gets_the_engine_line(check, test_mode):
    """THE 7 OCT INTERIM: 'any AI news?' said 'checking the web' though todays_news answered it.

    SINCE 8 OCT (docs/plans/NEWS-OCT8.md) a PLAIN news question is answered by code with no model call, so it is never
    slow enough for an interim line at all; that is asserted first. The interim wording rule itself is still exercised
    by a news question that names a company, which goes to the engine and todays_news as before."""
    import wording
    with World(test_mode, pins={"INTERIM_ENABLED": True, "INTERIM_AFTER_SECONDS": 0.6,
                                "INTERIM_AFTER_WEB_SECONDS": 0.2}) as w:
        w.bot._news_tools = lambda: [stub_news_tool()]

        async def web_pair(*a, **k):
            return [stub_tool_named("web_search"), stub_tool_named("fetch_page")], "", ""
        w.bot._websearch_tools = web_pair
        await w.say("any AI news?", who="member", mention=True)
        plain = [strip_tag(m.content) for m in w.posted]
        check("I2: the plain question gets no interim line of any kind (it is answered at once, by code)",
              [l for l in plain if l in wording.INTERIM_WEB + wording.INTERIM_ENGINE], [])
        check("I2: ...and no model call", w.model.calls, 0)
        before = len(w.posted)
        w.model.script = [("use", [("todays_news", {"topic": "Acme"})]),
                          ("slow", 1.0, ("say", "Here's what's come in: 5 stories."))]
        await w.say("any AI news about Acme?", who="member", mention=True)
        lines = [strip_tag(m.content) for m in w.posted[before:]]
        interims = [l for l in lines if l in wording.INTERIM_WEB + wording.INTERIM_ENGINE]
        check("I2: an interim went out", len(interims), 1)
        check("I2: it is the ENGINE wording, not 'checking the web' (no web_search ran)",
              interims[0] in wording.INTERIM_ENGINE if interims else None, True)
        check("I2: and none of the six lines says the web when nothing searched",
              [l for l in interims if "web" in l.lower()], [])
        return {}


def stub_tool_named(name):
    return rw.stub_tool(name)


def stub_news_tool():
    async def handler(_inp):
        return {"items": [{"title": f"Story {i}"} for i in range(5)]}
    return {"schema": {"name": "todays_news", "description": "stub",
                       "input_schema": {"type": "object", "properties": {}}}, "handler": handler}


@case
async def s_routing_of_the_two_asks(check, test_mode):
    """Objectives / today phrases go to the 'today' group; news phrases to todays_news (plan 7, 8.1)."""
    import toolsets
    today = ["what are the sales objectives for today?", "what's on today", "what do we need to do today?",
             "today's priorities", "today's plan", "what is the plan for today?", "objectives",
             "what are our priorities today", "what are the objectives for the day?", "today's objectives"]
    for q in today:
        check(f"route {q!r} -> today", toolsets.route(q), ["today"])
    other = {"what are our Q4 objectives?": [], "what's the plan for Acme?": ["plan"], "are we on plan?": ["plan"],
             "why isn't Acme due today?": [], "what's the queue": ["ops"], "cadence preview": ["ops"]}
    for q, want in other.items():
        check(f"route {q!r} unchanged", toolsets.route(q), want)
    for q in ["any AI news?", "top 5 AI headlines", "what's new in AI", "what is new in AI?", "AI headlines",
              "any AI news today?"]:
        got = toolsets.route(q)
        check(f"route {q!r} reaches todays_news", "news" in got, True)
    check("the 'today' group offers the two tools", toolsets.GROUPS["today"], ("show_todos", "todays_objectives"))
    check("cadence_preview is not in the today group", "cadence_preview" in toolsets.GROUPS["today"], False)
    check("cadence_preview still belongs to ops", "cadence_preview" in toolsets.GROUPS["ops"], True)
    check("cadence_preview's one-liner points 'what do we need to do today' at todays_objectives",
          "todays_objectives" in toolsets.ONE_LINE["cadence_preview"], True)
    check("todays_objectives has a one-liner", bool(toolsets.ONE_LINE.get("todays_objectives")), True)
    return {}


@case
async def h_the_rule13_hook_exists_and_does_nothing_harmful(check, test_mode):
    """5.6: `_maybe_next_step_reply(message, text, ctx)` is a stub that returns False and touches nothing."""
    with World(test_mode) as w:
        check("hook: the stub exists", hasattr(w.bot, "_maybe_next_step_reply"), True)
        m = rw.HMsg(w, w.chan, "done", APPROVER)
        got = w.bot._maybe_next_step_reply(m, "done", {"drip": {"action_type": "next_step_followups"},
                                                      "root_id": "1"})
        if inspect.isawaitable(got):
            got = await got
        check("hook: returns False (the Rule 13 owner fills it in later)", got, False)
        check("hook: touched nothing", (w.n_posted, w.votes(), w.sheet.writes, w.counts()["model"]), (0, [], [], 0))
        return {}


# ==================================================================================================================

async def run_case(fn, check_live, check_test):
    """Run one case in live mode then test mode; return (live snapshot, test snapshot)."""
    live = await fn(check_live, False)
    test = await fn(check_test, True)
    return live, test


import replies_cases2  # noqa: E402,F401  (registers the second file's cases into CASES, after these)
import replies_cases3  # noqa: E402,F401  (cases for what the build did beyond the plan)
import replies_cases4  # noqa: E402,F401  (plan section 20)

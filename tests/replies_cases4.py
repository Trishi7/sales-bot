"""CASES FROM PLAN SECTION 20 (the build's rulings): the "Waiting for your yes" post in full, a parent the bot cannot
fetch but knows as its own, hop 2 with an open proposal, a bare no inside the window, a DM-bound post, a failing model
on a "today" question, and the sender's seeded wording (B1)."""
import random

import replies_cases as rc
import replies_cases3 as c3
import replies_world as rw
from replies_cases import Mark, case, ack_emoji
from replies_world import World, WED, MEMBER, APPROVER


def _recorder(w):
    applied = []

    async def record(message, proposal, *, decided_by, why=""):
        applied.append(proposal["proposal_key"])
    w.bot._apply_approved_write = record
    return applied


@case
async def d26_a_nudge_that_listed_three(check, test_mode):
    """D26/D25/D27: yes to a nudge listing three asks which, numbered; '2' applies the second; a non-approver gets the polite no;
    'no' to a nudge that listed several declines none."""
    import approvals
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        applied = _recorder(w)
        nudge, keys = await c3._stale_pair(w, kinds=("cell_update", "email_write", "row_add"))
        m = Mark(w)
        await w.say("no", reply_to=nudge)
        check("D26: 'no' to a nudge that listed three declines none: one reaction", ([e for _t, e in m.reacted], sorted(w.proposals().values())), ([ack_emoji()], ["open"] * 3))
        m = Mark(w)
        await w.say("yes", who="member", reply_to=nudge)
        check("D26: a non-approver's yes to a nudge of three is asked which (no vote yet); nothing applied", (m.said[0].startswith("Which one"), m.d("votes")), (True, 0))
        check("D26: ...and applies nothing", applied, [])
        m = Mark(w)
        await w.say("yes", reply_to=nudge)
        text = "\n".join(m.said)
        check("D26: three listed: which_proposal, numbered 1-3, 0 votes", (text.startswith("Which one do you mean?"), "3. " in text, applied, m.d("votes")), (True, True, [], 0))
        which = w.posted[-1]
        await w.say("2", reply_to=which)
        check("D26: '2' applies exactly the second one shown", (len(applied), sorted(w.proposals().values())), (1, ["applied", "open", "open"]))
        return w.snapshot()


@case
async def d28_unfetchable_parent_that_is_the_proposals_own_message(check, test_mode):
    """D28: the parent cannot be fetched but its id is the proposal's own message: the vote counts. (D20: an unknown id does not.)"""
    with World(test_mode) as w:
        post = w.bot_says("R3 post")
        key = w.open_remind(post.id)
        post.deleted = True
        m = Mark(w)
        await w.say("yes", reply_to=post, cached=False, bot_in_mentions=True)
        check("D28: the yes applies that proposal", (w.proposals()[key], m.d("votes")), ("applied", 1))
        return w.snapshot()


@case
async def d29_sure_at_hop_two_with_an_open_proposal(check, test_mode):
    """D29: '@Saley sure' replying to a person's reply to a bot post that carries an open proposal: reaction, no vote, still open."""
    with World(test_mode) as w:
        post = w.bot_says("R3 post")
        key = w.open_remind(post.id)
        h1 = rw.HMsg(w, w.chan, "good list", MEMBER, reference=rw._ref(post))
        m = Mark(w)
        await w.say("sure", reply_to=h1, mention=True)
        rc.quiet(check, m, who="D29 'sure' at hop 2")
        check("D29: the proposal is still open", w.proposals()[key], "open")
        return w.snapshot()


@case
async def d31_a_bare_no_with_one_open_inside_the_window(check, test_mode):
    """D31: '@Saley no' with one proposal open inside the window declines it."""
    with World(test_mode) as w:
        key = w.open_remind(w.bot_says("R3 post").id, age_minutes=5)
        await w.say("no", mention=True)
        check("D31: declined", w.proposals()[key], "declined")
        return w.snapshot()


@case
async def o6_a_dm_bound_post_is_not_in_the_answer(check, test_mode):
    """O6: a post whose destination is a DM is never shown in the on-demand answer."""
    w, fx = rc._objectives_world(test_mode, hh=11, mm=0, rules=("R3",))
    with w:
        await rc._stock_sheet(w, fx)
        real = w.bot._plan_drip

        async def dm_plan(**kw):
            planned = await real(**kw)
            for m in (planned or {}).get("messages") or []:
                m["destination"] = "dm"
            return planned
        w.bot._plan_drip = dm_plan
        import wording
        text = await w.bot._todays_objectives()
        check("O6: a DM-bound post is left out of the summary: there is no posts line",
              (rc.today_group(text, wording.TODAY_ALSO), "AI events" in text), ([], False))
        return {"text": text}


@case
async def o8_the_model_fails_on_a_today_question(check, test_mode):
    """O8: the model call raises on a 'today' question: the objectives are still sent."""
    w, fx = rc._objectives_world(test_mode)
    with w:
        await rc._stock_sheet(w, fx)
        direct = await w.bot._todays_objectives()
        w.model.script = [("raise",)]
        m = Mark(w)
        await w.say("what are the sales objectives for today?", who="member", mention=True)
        check("O8: the reply is the objectives", "\n".join(m.said).startswith(direct), True)
        return {"text": direct}


@case
async def b1_the_sender_seeds_its_wording(check, test_mode):
    """B1: two sends of the same (day, slot) read the same whatever the random state; an unsent post's answer is the post."""
    posts = []
    for seed in (1, 99):
        with World(test_mode, pretend=(WED, 14, 5)) as w:
            import tone
            tone.RNG = random.Random(seed)
            post, _ = await w.send_r3_post(WED)
            posts.append(rw.strip_tag(post.content))
    check("B1: the same (day, slot) gives the same R3 post under two different random states", posts[0], posts[1])
    return {"post": posts[0]}


@case
async def d27_a_non_approver_yes_to_a_nudge_that_listed_one(check, test_mode):
    """D27: a non-approver's yes to a nudge that listed ONE proposal gets the polite no; it stays open."""
    import approvals
    with World(test_mode, pretend=(WED, 14, 5)) as w:
        applied = _recorder(w)
        nudge, keys = await c3._stale_pair(w, kinds=("cell_update",))
        m = Mark(w)
        await w.say("yes", who="member", reply_to=nudge)
        check("D27: the polite no, proposal open, nothing applied", (m.said, w.proposals()[keys[0]], applied),
              ([approvals.not_an_approver_reply("Kushal")], "open", []))
        return w.snapshot()


@case
async def g1_an_old_sweep_post_does_not_reach_later_proposals(check, test_mode):
    """G1: after a restart (nothing in _said) a yes under a sweep post dated the day BEFORE the proposals' nudged_on is not a
    vote and not a 'which one'; under a post dated the nudge day it still is."""
    from datetime import datetime
    import deadlines as dl
    for label, delta, want_votes in (("old post", -1, 0), ("same-day post", 0, 1)):
        with World(test_mode, pretend=(WED, 14, 5)) as w:
            applied = _recorder(w)
            nudge, keys = await c3._stale_pair(w, kinds=("cell_update",))
            w.bot._said.clear()
            day = dl.today_ist()
            from datetime import timedelta
            d = day + timedelta(days=delta)
            nudge.created_at = datetime(d.year, d.month, d.day, 14, 0, tzinfo=dl.IST)
            m = Mark(w)
            await w.say("yes", reply_to=nudge)
            check(f"G1 {label}: votes", m.d("votes"), want_votes)
            if want_votes == 0:
                check("G1 old post: no 'which one?', the proposal stays open, nothing applied",
                      (any("Which one" in t for t in m.said), w.proposals()[keys[0]], applied), (False, "open", []))
            else:
                check("G1 same-day post: the one it listed is applied", applied, keys)
    return {}

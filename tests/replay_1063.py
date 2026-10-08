"""THE NFT2-1063 SECTION OF THE ONE SHARED REPLAY HARNESS (verify_replay_oct6.py).

The harness feeds tests/fixtures/oct6_exchange.md (and tests/fixtures/oct7_exchange.md) through the REAL
`on_message` path twice, SALES_TEST_MODE=false then true, and asserts the same routing, the same tools offered, the
same model-call count and the same reply text apart from the tag. This module holds the NFT2-1063 steps so the
harness file only needs a few lines to run them:

    steps 2, 3, 4, 4b, 6, 7 of the 6 Oct fixture and the 7 Oct exchange (+ the 'yes' to R3's post)

EXTEND, NEVER FORK: this is a section the harness calls (`await replay_1063.section(check, FIX)`), not a second
harness. It builds its world from tests/replies_world.py (real SalesBot, real `on_message`, fake Discord and model).

WORDING THAT IS RECONSTRUCTED is labelled: the fixture quotes steps 2 and 4 verbatim; steps 3, 6 and 7 are described,
not quoted, so the text below is ours and is printed as 'reconstructed'.
"""
import asyncio
import re
import time as _time
from datetime import date, datetime, timedelta

import replies_cases as rc
import replies_world as rw
from replies_world import (APPROVER, MEMBER, MON, WED, World, ScriptModel, strip_tag)

RECONSTRUCTED = {
    3: "Sure.",
    6: "@Saley Underdog AI funding and HQ",
    7: "@Saley top 5 AI headlines",
}
OCT7_Q = "@Saley any AI news?"
OCT7_REPLY = "sure"


def _engine_lines(tap_lines):
    out = []
    for m in tap_lines:
        mm = re.search(r"\[engine\] msg=\d+ tools=(\d+) \(([^)]*)\)", m)
        if mm:
            out.append((int(mm.group(1)), mm.group(2)))
    return out


def _clean(text):
    return text.replace("@Saley", "").strip()


async def _ask(w, text, **kw):
    n, c = w.n_posted, w.counts()
    rw.TAP.lines.clear()
    off0 = len(w.model.offered)
    m = await w.say(_clean(text), mention=True, **kw)
    return {"replies": w.replies_after(n), "calls": w.counts()["model"] - c["model"],
            "offered": [list(o) for o in w.model.offered[off0:]], "engine": _engine_lines(rw.TAP.lines),
            "route": w.routes()[-1:] , "msg": m}


# -- step 2 + 3 ----------------------------------------------------------------------------------------------------

async def steps_2_3(test_mode, fix):
    """Step 2 (the PoCs question, a web answer), then step 3 ("Sure." replying to it): (i) nothing open,
    (ii) an events_remind and an email_write open in the channel."""
    out = {}
    q2 = (fix.get(2) or {}).get("text") or "@Saley who are the PoCs at Underdog AI?"
    for variant in ("i", "ii"):
        with World(test_mode) as w:
            rw.enable_web(w)
            w.model.script = [("use", [("web_search", {"query": "Underdog AI PoCs"})]),
                              ("say", "Underdog AI's PoCs I can find: Ada Lovelace (CTO).")]
            two = await _ask(w, q2, who="member")
            answer = w.posted[-1]
            out[f"2{variant}"] = {k: v for k, v in two.items() if k != "msg"}
            keys = []
            if variant == "ii":
                post = w.bot_says("**AI events & summits**\nWant me to remind you again on Monday?")
                keys.append(w.open_remind(post.id))
                mail = w.bot_says("**PoCs to contact**\nWant me to add the email I found to the sheet? Say yes.")
                keys.append(await rc._open_email(w, mail))
            c0, r0, n0, rem0 = w.counts(), len(w.reactions), w.n_posted, len(w.reminders())
            rw.TAP.lines.clear()
            await w.say(RECONSTRUCTED[3], who="member", reply_to=answer)
            c1 = w.counts()
            out[f"3{variant}"] = {
                "route": w.routes()[-1:], "model": c1["model"] - c0["model"], "router": c1["router"] - c0["router"],
                "extractor": c1["extractor"] - c0["extractor"], "sheet_reads": c1["sheet_reads"] - c0["sheet_reads"],
                "replies": w.replies_after(n0), "reactions": [e for _t, e in w.reactions[r0:]],
                "votes": c1["votes"], "reminders": len(w.reminders()) - rem0,
                "open": [w.proposals()[k] for k in keys], "writes": list(w.sheet.writes)}
    return out


# -- step 4 ---------------------------------------------------------------------------------------------------------

async def _monday_world(test_mode, *, empty=False):
    import rule13_fixtures as fx
    w = World(test_mode, pretend=(MON, 11, 0))
    w.__enter__()
    if not empty:
        rows, _meta = fx.rotation_values()
        w.sheet.set(pocs=rows, deliverables=rw.deliverables_for(MON), events=rw.events_for(MON))
    return w


async def _real_monday_posts(test_mode):
    """What the REAL sender posts for the same Monday queue (second world), {type: text}."""
    import deadlines as dl
    w2 = await _monday_world(test_mode)
    try:
        w2.pretend_day(MON, 14, 5)
        planned = await w2.bot._plan_drip(today=MON, already=[])
        out = {}
        for m in planned["messages"]:
            n = w2.n_posted
            if m["type"] == "ai_news":
                continue                                  # R1 needs the feed store to send; it is not in the answer anyway
            await w2.bot._send_drip_message(w2.chan, m, marker=dl.iso(MON), channel_id=w2.chan.id)
            out[m["type"]] = "\n".join(strip_tag(p.content) for p in w2.posted[n:])
        return out, [m["type"] for m in planned["messages"]]
    finally:
        w2.__exit__(None, None, None)


async def step_4(test_mode, fix, *, empty=False):
    q4 = (fix.get(4) or {}).get("text") or "@Saley what are the sales objectives for today?"
    posts, planned_types = ({}, []) if empty else await _real_monday_posts(test_mode)
    w = await _monday_world(test_mode, empty=empty)
    try:
        w.model.script = [("use", [("show_todos", {}), ("todays_objectives", {})]), ("say", "TODO-PART: send the Acme deck.")]
        rows0 = w.drip_rows()
        planned = await w.bot._plan_drip(today=MON, already=[])
        types = [m["type"] for m in (planned or {}).get("messages") or []]
        r = await _ask(w, q4, who="member")
        reply = "\n".join(r["replies"])
        return {"replies": r["replies"], "reply": reply, "calls": r["calls"], "offered": r["offered"],
                "engine": r["engine"], "route": r["route"], "planned_types": types, "posts": posts,
                "drip_before": rows0, "drip_after": w.drip_rows(), "proposals": w.proposals(),
                "reactions": list(w.reactions)}
    finally:
        w.__exit__(None, None, None)


# -- step 7 and the 7 Oct exchange ------------------------------------------------------------------------------------

async def step_7(test_mode, fix, *, slow=False):
    pins = {"INTERIM_ENABLED": True, "INTERIM_AFTER_SECONDS": 0.5, "INTERIM_AFTER_WEB_SECONDS": 0.25} if slow else {}
    with World(test_mode, pins=pins) as w:
        queries = rw.enable_web(w)
        heads = rw.seed_news(w)
        step = ("slow", 1.0, ("say", "Here's what's come in: 5 stories.")) if slow else ("say", "Here's what's come in.")
        w.model.script = [("use", [("todays_news", {})]), step]
        r = await _ask(w, RECONSTRUCTED[7], who="member")
        news = [res for n, res in w.model.results if n == "todays_news"]
        return {"replies": r["replies"], "calls": r["calls"], "offered": r["offered"], "engine": r["engine"],
                "route": r["route"], "searches": len(queries), "headlines": heads,
                "news_items": [i.get("title") for i in (news[0].get("items") if news else [])],
                "routing": r["engine"]}


async def oct7(test_mode):
    """THE 7 OCT EXCHANGE: R3's post (offer open) -> 'any AI news?' (slow) -> 'sure' to the interim line -> the answer."""
    import wording
    pins = {"INTERIM_ENABLED": True, "INTERIM_AFTER_SECONDS": 0.5, "INTERIM_AFTER_WEB_SECONDS": 0.25}
    with World(test_mode, pretend=(WED, 18, 0), pins=pins) as w:
        queries = rw.enable_web(w)
        post, _ = await w.send_r3_post(WED)
        key = next(iter(w.proposals()))
        w.real_clock()                                         # the news window is the real day's
        rw.seed_news(w)
        w.model.script = [("use", [("todays_news", {})]),
                          ("slow", 1.2, ("say", "Here's what's come in since Tuesday 2 PM: 5 stories."))]
        asked = asyncio.create_task(w.say(OCT7_Q.replace("@Saley", "").strip(), who="member", mention=True))
        t0 = _time.monotonic()
        interim = None
        while _time.monotonic() - t0 < 5:
            interim = next((m for m in w.posted if strip_tag(m.content) in wording.INTERIM_WEB + wording.INTERIM_ENGINE),
                           None)
            if interim is not None:
                break
            await asyncio.sleep(0.02)
        sent = len(w.posted)
        c0, r0 = w.counts(), len(w.reactions)
        if interim is not None:
            await w.say(OCT7_REPLY, who="approver", reply_to=interim)
        c1 = w.counts()
        await asked
        answer = [strip_tag(m.content) for m in w.posted[sent:] if strip_tag(m.content) not in
                  wording.INTERIM_WEB + wording.INTERIM_ENGINE]
        news = [res for n, res in w.model.results if n == "todays_news"]
        rec = {"interim": strip_tag(interim.content) if interim else None,
               "interim_is_engine": (strip_tag(interim.content) in wording.INTERIM_ENGINE) if interim else None,
               "sure_reactions": [e for _t, e in w.reactions[r0:]],
               "sure_replies": [t for t in w.replies_after(sent)
                                if t not in wording.INTERIM_WEB + wording.INTERIM_ENGINE and "5 stories" not in t],
               "sure_votes": c1["votes"] - c0["votes"], "reminders_after_sure": list(w.reminders()),
               "offer_open": w.proposals()[key], "searches": len(queries),
               "answer_has_stories": bool(news and news[0].get("items")),
               "answer": answer, "calls": w.model.calls}
        # THE 'YES' TO R3'S POST (a Wednesday, under the pretend clock again): the reminder, named
        w.pretend_day(WED, 18, 5)
        n = w.n_posted
        await w.say("yes", who="approver", reply_to=post)
        rec["yes_reply"] = w.replies_after(n)
        rec["yes_reminders"] = list(w.reminders())
        return rec


# -- the section ------------------------------------------------------------------------------------------------------

def _same(check, what, live, test, keys=None):
    for k in (keys or live):
        check(f"test mode == live: {what}: {k}", test.get(k), live.get(k))


async def section(check, fix):
    """Run every NFT2-1063 step in live then test mode and assert what the plan says, and that the two agree."""
    import wording
    import toolsets
    print("\n  (wording for steps 3, 6 and 7 is RECONSTRUCTED: the fixture describes them, it does not quote them)")

    # ---- steps 2 and 3
    print("\nstep 2 and step 3 - NFT2-1063 - 'Sure.' replying to the PoCs answer")
    live, test = await steps_2_3(False, fix), await steps_2_3(True, fix)
    two = live["2i"]
    check("step 2: routed to the engine", two["route"], ["engine"])
    check("step 2: groups people + sheet", [g for _n, g in two["engine"]], ["people, sheet"])
    check("step 2: the web pair is offered", {"web_search", "fetch_page"} <= set(two["offered"][0]), True)
    check("step 2: two model calls, one reply", (two["calls"], len(two["replies"])), (2, 1))
    for v, label in (("i", "(i) nothing open"), ("ii", "(ii) an events_remind and an email_write open")):
        r = live[f"3{v}"]
        check(f"step 3 {label}: route ack", r["route"], ["ack"])
        check(f"step 3 {label}: zero model, router and extractor calls", (r["model"], r["router"], r["extractor"]),
              (0, 0, 0))
        check(f"step 3 {label}: zero sheet reads", r["sheet_reads"], 0)
        check(f"step 3 {label}: zero replies, one reaction", (r["replies"], r["reactions"]), ([], [rc.ack_emoji()]))
        check(f"step 3 {label}: zero votes, zero reminders, zero writes", (r["votes"], r["reminders"], r["writes"]),
              (0, 0, []))
        if v == "ii":
            check("step 3 (ii): both proposals are still open", r["open"], ["open", "open"])
    for key in live:
        _same(check, f"step {key}", live[key], test[key])

    # ---- step 4
    print("\nstep 4 - NFT2-1063 - 'what are the sales objectives for today?' over a Monday's queue "
          "(R1, R4, R10, R13 planned)")
    live, test = await step_4(False, fix), await step_4(True, fix)
    check("step 4: the queue has an AI-news group, a deliverables group and a Rule 13 group (so the exclusion is tested)",
          {"ai_news", "deliverables", "next_step_followups"} <= set(live["planned_types"]), True)
    check("step 4: routed to the engine, group today", (live["route"], [g for _n, g in live["engine"]]),
          (["engine"], ["today"]))
    check("step 4: tools offered are exactly show_todos and todays_objectives", sorted(live["offered"][0]),
          ["show_todos", "todays_objectives"])
    check("step 4: cadence_preview is NOT offered", "cadence_preview" in live["offered"][0], False)
    check("step 4: two model calls", live["calls"], 2)
    reply = live["reply"]
    objectives = reply.split("TODO-PART")[0].rstrip()
    order = [t for t in live["planned_types"] if t != "ai_news"]
    check("step 4: the blocks are the real sender's posts for the same queue (R10 notice, R13, R4), in plan order, "
          "one blank line apart (same lines; an unsent post's intro line is its own seeded draw)",
          rc.blocks_match(objectives, live["posts"], order), True)
    check("step 4: the R4 deliverables, the R13 people and the R10 notice are all in the answer, line for line",
          all(l in objectives for t in ("deliverables", "next_step_followups", "closure_support")
              for l in live["posts"].get(t, "<none>").splitlines()
              if l.startswith(("1.", "2.", "   ", "•", "No closure"))), True)
    check("step 4: no AI-news block, and no @-mention anywhere (the tags line is left out: no pings)",
          ("AI news" in reply, "<@" in objectives), (False, False))
    check("step 4: none of the forbidden words (times, rules, rule numbers, schedule)",
          rc.forbidden_in(reply.split("TODO-PART")[0]), [])
    check("step 4: the model's to-do part follows the objectives", reply.endswith("TODO-PART: send the Acme deck."), True)
    check("step 4: drip_sends row count unchanged, no proposal opened, no reaction",
          (len(live["drip_after"]), len(live["drip_before"]), live["proposals"], live["reactions"]), (0, 0, {}, []))
    _same(check, "step 4", live, test, ("route", "engine", "offered", "calls", "replies", "planned_types", "proposals"))

    print("\nstep 4b - the same with an empty queue")
    live, test = await step_4(False, fix, empty=True), await step_4(True, fix, empty=True)
    check("step 4b: the objectives part is the one-line nothing-today",
          live["reply"].startswith(wording.NOTHING_TODAY), True)
    _same(check, "step 4b", live, test, ("route", "engine", "offered", "calls", "replies"))

    # ---- step 6
    print("\nstep 6 - NFT2-1063 - the interim wording (short waits; 6 reconstructed wording above)")
    await rc.i1_interim_wording_follows_what_actually_ran(check, False)
    await rc.i1_interim_wording_follows_what_actually_ran(check, True)

    # ---- step 7
    print("\nstep 7 - NFT2-1063 - 'top 5 AI headlines' over a seeded feed store (reconstructed wording)")
    live, test = await step_7(False, fix), await step_7(True, fix)
    check("step 7: routed to the engine, group news", (live["route"], ["news" in g for _n, g in live["engine"]]),
          (["engine"], [True]))
    check("step 7: todays_news is offered and was called, with all five seeded headlines",
          ("todays_news" in live["offered"][0], sorted(live["news_items"])), (True, sorted(live["headlines"])))
    check("step 7: zero web searches (the scripted model called only todays_news)", live["searches"], 0)
    check("step 7: two model calls, one reply", (live["calls"], len(live["replies"])), (2, 1))
    _same(check, "step 7", live, test, ("route", "engine", "offered", "calls", "replies", "searches", "news_items"))
    live, test = await step_7(False, fix, slow=True), await step_7(True, fix, slow=True)
    interims = [t for t in live["replies"] if t in wording.INTERIM_WEB + wording.INTERIM_ENGINE]
    check("step 7 (slow): one interim line, in the ENGINE wording (no search ran)",
          (len(interims), bool(interims) and interims[0] in wording.INTERIM_ENGINE), (1, True))
    _same(check, "step 7 slow", live, test, ("route", "calls", "replies", "searches"))

    # ---- the 7 Oct exchange
    print("\nthe 7 Oct exchange (tests/fixtures/oct7_exchange.md) - R3's post, 'any AI news?', 'sure' to the interim line")
    live, test = await oct7(False), await oct7(True)
    check("7 Oct: the interim is an ENGINE line (todays_news answered; no web search ran)",
          (live["interim"] is not None, live["interim_is_engine"], live["searches"]), (True, True, 0))
    check("7 Oct: 'sure' to the interim line: one reaction, no text", (live["sure_reactions"], live["sure_replies"]),
          ([rc.ack_emoji()], []))
    check("7 Oct: ...no vote, no reminder scheduled, the R3 offer still open",
          (live["sure_votes"], live["reminders_after_sure"], live["offer_open"]), (0, [], "open"))
    check("7 Oct: the 'Monday' line is nowhere in what Saley said",
          [t for t in live["answer"] + live["sure_replies"] if "Monday" in t or "post these" in t], [])
    check("7 Oct: the answer came from todays_news (5 stories)", live["answer_has_stories"], True)
    check("7 Oct+: 'yes' to R3's post schedules ONE reminder for Mon 12 Oct at 14:00",
          live["yes_reminders"], [("2026-10-12", "14:00", "open")])
    check("7 Oct+: ...and says exactly what it did", live["yes_reply"], [wording.events_remind_set("Mon 12 Oct at 2 PM")])
    for k in live:
        check(f"test mode == live: 7 Oct: {k}", test[k], live[k])

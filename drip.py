"""THE DRIP SCHEDULER — a few short messages a day instead of one long one.

WHAT THIS REPLACES. The daily digest: one message at 10:00 carrying HOT,
DEADLINES, OVERDUE, ESCALATIONS, HYGIENE, five cadence sections, a tracker
reminder and a funnel block, each item stamped with a carry-forward "(3rd day)".
It was built to stop six kinds of scattered message getting the bot muted, and
it did — then it created the opposite problem. A wall of sections reads like a
report. It gets skimmed. It asks a person to find their own name in it and work
out which three of forty lines are theirs.

The drip keeps the volume contract that made the digest worth having — a small,
hard cap on how often this bot speaks — and spends it differently:

    ONE MESSAGE PER (ACTION TYPE x OWNER). Companies comma-separated, in one
    sentence. NEVER two types in a message. NEVER two owners.

That rule is the whole design. A message with one subject and one owner is
answerable: "yes, done" means something. A message with four subjects and three
owners is a document, and nobody answers a document.

    NOTHING IN THIS MODULE SENDS. It computes groups, a schedule and message
    text. `bot.py` owns the one send path and the kill switch. That separation
    is what lets `python drip.py` simulate a whole day offline.

THE VOLUME CONTRACT (plan section 8):
  - at most DAILY_MESSAGE_CAP (3) proactive messages per weekday;
  - the first at about SALES_DRIP_START (10:00 IST);
  - then gaps of MESSAGE_GAP_MINUTES (90) plus or minus MESSAGE_JITTER_MINUTES
    (15), so the spacing never falls below 75 minutes at the default settings;
  - overflow ROLLS TO TOMORROW rather than being dropped — except that a group
    which has become a positive override jumps the roll, because a reply that
    waits a day is a reply that goes cold;
  - an empty queue means SILENCE. No "nothing to report" message, ever. A bot
    that speaks to say it has nothing to say has not understood the contract.

THE JITTER IS DETERMINISTIC, and that is a correctness property rather than a
nicety. It is seeded on (date, slot), so every tick of the sweeper — and every
restart mid-morning — recomputes exactly the same schedule. Combined with the
`drip_sends` slot rows in SQLite, that makes a redeploy at 11:40 resume at slot
3 instead of replaying the morning.

ONE GENTLE RE-ASK, AND ONLY ONE. A group nobody actioned is asked again once,
DRIP_REASK_DAYS (2) days later, in softer words. After that it returns to the
normal cadence and is never re-asked for that nudge again. Two asks is a
reminder; three is nagging, and nagging is what gets a bot muted — which is
where this whole design started.

REPLIES TO PEOPLE ARE IMMEDIATE. The spacing here applies to PROACTIVE sends
only. Somebody asking a question gets an answer straight away; that path does
not touch this module at all.
"""
import logging
import random
from datetime import date, datetime, timedelta
from typing import Optional

import config
import deadlines as dl
import gtm_sheet
import nextaction

log = logging.getLogger(__name__)

# Stages a message can be at for its group. See `db.drip_group_history`.
STAGE_NUDGE = "nudge"
STAGE_REASK = "reask"

# Why a group produced nothing today. Reported rather than swallowed — a
# scheduler that silently holds things back is one nobody can debug.
HELD_RECENT = "asked recently"
HELD_CAP = "over the daily cap — rolls to tomorrow"
HELD_WEEKEND = "weekend — the drip does not send"


# -- grouping -----------------------------------------------------------------


def owner_key(action: dict) -> str:
    """The grouping key for an owner. Normalised so "Vaishnavi" and "vaishnavi "
    are one person and not two messages."""
    return gtm_sheet.normalise_header(action.get("owner") or "")


def owner_label(action: dict) -> str:
    """How the owner is named in the message and the log. "" means unassigned,
    which is addressed to the notify list rather than to nobody."""
    return str(action.get("owner") or "").strip()


def group_key(action: dict) -> str:
    """"<type>|<owner key>" — the identity of one message's subject.

    Type AND owner, never one or the other. Keying on type alone would put two
    people's work in one message; keying on owner alone would mix a quote chase
    with a DM check, and a message asking two different kinds of thing gets half
    an answer.
    """
    return f"{action.get('type')}|{owner_key(action)}"


def group(actions: list) -> list:
    """Actions -> one group per (type x owner), ranked.

    RANKING IS BY THE GROUP'S BEST ACTION, not by its size. A group holding one
    positive override outranks a group holding nine slow-lane follow-ups,
    because the override is the thing that decays. Within a band the oldest due
    date leads, then the type, then the owner — so the order is stable day to
    day and a person's message does not move around for no reason.
    """
    buckets: dict = {}
    for action in actions or []:
        buckets.setdefault(group_key(action), []).append(action)

    groups: list = []
    for key, members in buckets.items():
        members.sort(key=nextaction.sort_key)
        best = members[0]
        companies = []
        for a in members:
            name = str(a.get("company") or "").strip()
            if name and name not in companies:
                companies.append(name)
        groups.append({
            "group_key": key,
            "type": best["type"],
            "type_label": nextaction.TYPE_LABELS.get(best["type"], best["type"]),
            "owner_key": owner_key(best),
            "owner": owner_label(best),
            "priority": best["priority"],
            "priority_label": best["priority_label"],
            "due_date": best.get("due_date"),
            "due_iso": best.get("due_iso", ""),
            "overdue_days": max(int(a.get("overdue_days") or 0) for a in members),
            "companies": companies,
            "actions": members,
            "count": len(members),
        })

    groups.sort(key=lambda g: (
        int(g["priority"]),
        g["due_date"] or date.max,
        str(g["type"]),
        str(g["owner_key"]),
    ))
    return groups


def companies_sentence(companies: list, *, cap: Optional[int] = None) -> str:
    """"Acme, Beta and Cinder", or "Acme, Beta and 4 more" past the cap.

    Oxford-comma-free and joined with "and" because this goes inside a sentence
    a person reads, not into a table. Past the cap it says how many were left
    out rather than trailing off — a list that quietly stops is a list that
    under-reports the work.
    """
    limit = max(1, config.DRIP_MAX_COMPANIES_PER_MESSAGE if cap is None else cap)
    names = [str(c).strip() for c in (companies or []) if str(c).strip()]
    if not names:
        return ""
    if len(names) > limit:
        shown = names[:limit]
        return ", ".join(shown) + f" and {len(names) - limit} more"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


# -- the schedule -------------------------------------------------------------


def min_gap_minutes() -> int:
    """The smallest gap the schedule can ever produce, in minutes.

    gap minus jitter — 75 at the defaults. It is the number the volume contract
    is written against, and it is used in two places that must agree: the
    contract report, and the SENDER's catch-up guard.

    THE SENDER NEEDS IT because "send everything whose planned time has passed"
    is wrong after a quiet morning. If the kill switch is off until 14:00, slots
    1, 2 and 3 are all overdue at once, and without this floor they would go out
    on three consecutive sweep ticks — three messages inside an hour, which is
    the exact thing the spacing exists to prevent. Catching up must still be
    paced.
    """
    return max(1, int(config.MESSAGE_GAP_MINUTES) - abs(int(config.MESSAGE_JITTER_MINUTES)))


def _jitter(day: date, slot: int) -> int:
    """The deterministic offset, in minutes, for one slot on one day.

    SEEDED ON (date, slot) AND NOTHING ELSE. That is what makes the schedule
    survive a restart: the sweeper recomputes the whole day on every tick, and
    it must arrive at the same times each time or the slot rows in SQLite stop
    lining up with the plan. A clock-seeded or process-seeded random would give
    a different schedule after every redeploy and make the restart guard
    meaningless.
    """
    spread = max(0, int(config.MESSAGE_JITTER_MINUTES))
    if spread == 0:
        return 0
    rng = random.Random(f"{dl.iso(day)}:{int(slot)}")
    return rng.randint(-spread, spread)


def slot_times(day: date, *, count: int) -> list:
    """The planned send times for one day, as datetimes in IST.

    The first slot lands on SALES_DRIP_START exactly; every later slot is the
    previous PLANNED time plus MESSAGE_GAP_MINUTES plus that slot's jitter.
    Compounding off the previous PLANNED time rather than off the actual send
    keeps the schedule deterministic even when a send is late or a tick is
    missed.

    THE GAP CAN NEVER GO BELOW gap - jitter. At the defaults that is 75 minutes,
    which is the floor the volume contract is written against, and the self-test
    asserts it.
    """
    hour, minute = config.drip_start_ist()
    first = datetime(day.year, day.month, day.day, hour, minute, tzinfo=dl.IST)
    out = [first]
    gap = max(1, int(config.MESSAGE_GAP_MINUTES))
    for slot in range(2, max(1, int(count)) + 1):
        out.append(out[-1] + timedelta(minutes=gap + _jitter(day, slot)))
    return out


def is_sending_day(day: date) -> bool:
    """Weekdays only, unless DRIP_WEEKDAYS_ONLY is off.

    The same instinct as the next-action engine's weekend shift, applied to the
    sending rather than to the due date: a nudge that lands on a Saturday is
    read on Monday anyway, having spent the weekend as an unread badge.
    """
    if not config.DRIP_WEEKDAYS_ONLY:
        return True
    return day.weekday() < 5


# -- the re-ask clock ---------------------------------------------------------


def stage_for(
    key: str, *, history: dict, today: date
) -> tuple[Optional[str], str]:
    """(stage, why) for one group: STAGE_NUDGE, STAGE_REASK, or None to hold.

    THE THREE STATES, and the reason each exists:

      never asked, or asked long ago   -> NUDGE. A fresh ask.
      asked within DRIP_REASK_DAYS     -> HOLD. They have had it once and have
                                          not had time to act; asking again
                                          tomorrow is the definition of nagging.
      asked exactly DRIP_REASK_DAYS+
      ago, never re-asked              -> REASK. One gentle follow-up, softer
                                          words, and that is the last one.
      already re-asked                 -> the clock resets: the group is back on
                                          the normal cadence and its next ask is
                                          an ordinary NUDGE, not a third chase.

    The reset is what stops this becoming an escalation ladder. A subject that
    stays undone for a month gets asked about roughly every few days in the
    same tone, not louder each time.
    """
    entry = (history or {}).get(key) or {}
    last_nudge = dl.parse_date(entry.get("last_nudge"))
    last_reask = dl.parse_date(entry.get("last_reask"))
    wait = max(1, int(config.DRIP_REASK_DAYS))

    if last_nudge is None:
        return STAGE_NUDGE, "not asked before"

    # A re-ask that has already gone out ends this nudge's life. The clock
    # restarts from the re-ask, so the next ask is a fresh nudge.
    if last_reask is not None and last_reask >= last_nudge:
        age = (today - last_reask).days
        if age < wait:
            return None, f"{HELD_RECENT} (re-asked {age}d ago)"
        return STAGE_NUDGE, f"back on the normal cadence, {age}d after the re-ask"

    age = (today - last_nudge).days
    if age < wait:
        return None, f"{HELD_RECENT} ({age}d ago)"
    return STAGE_REASK, f"nudged {age}d ago and nothing came back"


# -- the plan -----------------------------------------------------------------


def plan(
    actions: list, *, day: Optional[date] = None, history: Optional[dict] = None,
    already_sent: Optional[list] = None, cap: Optional[int] = None,
) -> dict:
    """TODAY'S MESSAGES: what to say, to whom, and when. Sends nothing.

    Returns:
        {"messages": [...],   the slots to send, in order, each with send_at
         "sent": [...],       slots already gone out today (from SQLite)
         "held": [...],       groups suppressed today, each with a reason
         "rolled": [...],     groups over the cap, rolling to tomorrow
         "groups": [...],     every group, ranked, for the preview
         "day": date,
         "is_sending_day": bool}

    `already_sent` is `db.drip_sent_today(...)`. The slots it names are skipped
    and their groups are not re-planned, which is the restart guard: the plan is
    recomputed in full on every tick and the sent slots simply fall away.

    THE POSITIVE-OVERRIDE EXCEPTION TO THE ROLL. Overflow normally waits for
    tomorrow. A group in the override band does not: it is ranked first by
    `group()`, so it takes a slot today by construction, and a group that rolled
    yesterday and has since become an override leads the queue the next morning
    rather than waiting behind the same groups again. That is the whole
    mechanism — no special case, just an ordering that re-evaluates daily.
    """
    day = day or dl.today_ist()
    history = history or {}
    already = list(already_sent or [])
    limit = max(0, int(config.DAILY_MESSAGE_CAP if cap is None else cap))

    groups = group(actions)
    result = {
        "messages": [], "sent": already, "held": [], "rolled": [],
        "groups": groups, "day": day, "is_sending_day": is_sending_day(day),
    }

    if not result["is_sending_day"]:
        for g in groups:
            result["held"].append({**g, "why": HELD_WEEKEND})
        log.info(
            "[drip] %s is a %s — no proactive messages. %d group(s) wait for Monday.",
            dl.iso(day), day.strftime("%A"), len(groups),
        )
        return result

    sent_group_keys = {str(r.get("group_key")) for r in already}
    used_slots = {int(r.get("slot") or 0) for r in already}
    next_slot = (max(used_slots) + 1) if used_slots else 1

    times = slot_times(day, count=limit)

    for g in groups:
        if g["group_key"] in sent_group_keys:
            continue                       # already said today; not said twice
        stage, why = stage_for(g["group_key"], history=history, today=day)
        if stage is None:
            result["held"].append({**g, "why": why})
            continue
        if next_slot > limit:
            result["rolled"].append({
                **g, "stage": stage,
                "why": HELD_CAP + (
                    " (it will lead tomorrow if it becomes a positive override)"
                    if g["priority"] != nextaction.P_OVERRIDE else ""
                ),
            })
            continue
        result["messages"].append({
            **g,
            "slot": next_slot,
            "stage": stage,
            "stage_why": why,
            "send_at": times[next_slot - 1],
            "send_at_hhmm": times[next_slot - 1].strftime("%H:%M"),
        })
        next_slot += 1

    log.info(
        "[drip] %s: %d group(s) from %d action(s) -> %d already sent, %d planned, "
        "%d held, %d rolling to tomorrow (cap %d). NOTHING HAS BEEN SENT BY THIS "
        "MODULE — it computes only.",
        dl.iso(day), len(groups), len(actions or []), len(already),
        len(result["messages"]), len(result["held"]), len(result["rolled"]), limit,
    )
    for m in result["messages"]:
        log.info(
            "[drip]   slot %d at %s  %s x %s  (%d company/companies, %s)",
            m["slot"], m["send_at_hhmm"], m["type"],
            m["owner"] or "(unassigned)", len(m["companies"]), m["stage"],
        )
    return result


# -- the message --------------------------------------------------------------

# WHAT EACH TYPE IS ASKING FOR, as a plain phrase the composer builds on. These
# are the FALLBACK wording, used when the model is off or unreachable. They are
# deliberately complete sentences with an out already in them, because a
# fallback that reads like a template is what people will actually receive on
# the day the API is down.
_ASK = {
    nextaction.MEETING_PROPOSAL: (
        "{who}{companies} came back to us and there is no meeting on the books yet. "
        "Worth proposing a time while it is warm — no rush if you are mid-something, "
        "just tell me when you have."
    ),
    nextaction.SCHEDULED_REMINDER: (
        "{who}you asked me to flag {companies} today. Here it is — tell me when it is "
        "done, or tell me to move it."
    ),
    nextaction.QUOTE_CHASE: (
        "{who}{companies} had the demo and nothing has moved since. If the quote is "
        "ready it is worth sending; if something is blocking it, say so and I will "
        "stop asking."
    ),
    nextaction.PROGRESS_CHECK: (
        "{who}the DM to {companies} has been sitting a while with nothing back. Worth "
        "a look at where it got to when you have a minute."
    ),
    nextaction.DM_CHECK: (
        "{who}{companies} connected a while back and I have no DM date against them. "
        "If it has gone, tell me and I will note it; if not, now is while it is still "
        "warm."
    ),
    nextaction.DM_SENT_CHECK: (
        "{who}{companies} connected in the last couple of days. Has the DM gone out? "
        "No rush — I just do not want it to slide."
    ),
    nextaction.MARK_UNRESPONSIVE: (
        "{who}{companies} have had a lot of follow-ups with nothing back. Might be "
        "time to mark them unresponsive and stop spending time there — your call, I "
        "will not touch it."
    ),
    nextaction.CHANNEL_SWITCH: (
        "{who}{companies} have not answered on the channel we have been using. Might "
        "be worth trying another way in before spending another follow-up."
    ),
    nextaction.PULSE_CHECK: (
        "{who}{companies} have been on hold for a while now. Worth a quick check on "
        "whether anything has shifted — no rush."
    ),
    nextaction.FOLLOWUP: (
        "{who}{companies} have gone quiet since the last touch. Worth a follow-up when "
        "you get a window — tell me when it is done and I will leave it alone."
    ),
}

# THE RE-ASK IS SOFTER, ALWAYS. Same subject, less weight, and it says out loud
# that it is the last time — which is what makes it land as a courtesy rather
# than as a second demand.
_REASK = (
    "{who}circling back on {companies} — no pressure at all, and I will leave it "
    "after this. If it is handled or not worth it, just say and I will drop it."
)


def compose_fallback(message: dict, *, address: str = "") -> str:
    """The message text WITHOUT the model. Always sendable.

    One thought, one sentence-ish, an out at the end, no headers, no bullets, no
    labels, no stacked imperatives. This is the shape the exemplars in
    sales_policy.md teach the model, written out by hand so a model outage costs
    polish and never the message itself.

    `address` IS HOW THE OWNER IS NAMED, and the caller owns that decision — it
    is a Discord mention when the roster authorises one and the person's plain
    name when it does not. It is threaded through rather than prefixed by the
    caller because prefixing produced "Vaishnavi Vaishnavi - ..." the first time
    the roster gate fell back to a name: two things were both naming the owner
    and neither knew about the other. There is now exactly one place a message
    says who it is for.
    """
    who = f"{address or message.get('owner') or ''} — " if (
        address or message.get("owner")) else ""
    companies = companies_sentence(message.get("companies") or [])
    template = (
        _REASK if message.get("stage") == STAGE_REASK
        else _ASK.get(message.get("type"), _ASK[nextaction.FOLLOWUP])
    )
    return template.format(who=who, companies=companies or "these").strip()


def compose_prompt(message: dict, *, address: str = "") -> str:
    """What the model is asked to write. One message, one subject, one owner.

    It is given the facts and the constraints and nothing else — no example
    output inline, because the exemplars live in the policy file where a human
    can edit them without touching code.
    """
    lines = [
        "Write ONE short proactive message for the sales channel.",
        "",
        f"Who it is for: {message.get('owner') or 'nobody in particular (the team)'}",
        f"Address them as EXACTLY this, once, at the start: "
        f"{address or message.get('owner') or '(do not address anyone by name)'}",
        f"What it is about: {message.get('type_label')}",
        f"Companies (name every one of these, comma-separated, in one sentence): "
        f"{companies_sentence(message.get('companies') or [])}",
        f"How overdue: {message.get('overdue_days') or 0} day(s)",
    ]
    if message.get("stage") == STAGE_REASK:
        lines += [
            "",
            "THIS IS A SECOND ASK, and the last one. Make it noticeably softer than a "
            "first ask, say you will leave it after this, and give them an easy way to "
            "close it out. Do not repeat the full reasoning; they have had it once.",
        ]
    reasons = [str(a.get("why") or "") for a in (message.get("actions") or [])[:3]]
    if any(reasons):
        lines += ["", "Why it came up (for your understanding, do not recite it):"]
        lines += [f"  - {w}" for w in reasons if w]
    lines += [
        "",
        "Write the message and nothing else. No preamble, no sign-off, no quotes "
        "around it.",
    ]
    return "\n".join(lines)


# -- the preview --------------------------------------------------------------


def preview_text(planned: dict) -> str:
    """The day's plan as text. Sends nothing; used by the dry run and the log."""
    day = planned.get("day")
    lines = [
        f"**Drip plan — {day.strftime('%a %d %b %Y') if day else 'today'}**",
        f"_cap {config.DAILY_MESSAGE_CAP} message(s), first at {config.SALES_DRIP_START}, "
        f"gaps of {config.MESSAGE_GAP_MINUTES}±{config.MESSAGE_JITTER_MINUTES} min. "
        f"Nothing has been sent._",
        "",
    ]
    if not planned.get("is_sending_day"):
        lines.append(f"{day.strftime('%A')} — the drip does not send at weekends.")
        return "\n".join(lines)

    for row in planned.get("sent") or []:
        lines.append(
            f"  [SENT {row.get('sent_at', '')[:16]}] slot {row.get('slot')}: "
            f"{row.get('action_type')} x {row.get('owner_label') or '(unassigned)'} "
            f"— {row.get('companies')}"
        )
    if planned.get("sent"):
        lines.append("")

    if not planned.get("messages"):
        lines.append("Nothing to send. (An empty queue means silence — there is no "
                     "'nothing to report' message.)")
    for m in planned.get("messages") or []:
        lines.append(
            f"  [{m['send_at_hhmm']}] slot {m['slot']} · {m['type']} · "
            f"{m['owner'] or '(unassigned)'} · {m['stage']} · P{m['priority']}"
        )
        lines.append(f"      {compose_fallback(m, address=m.get('owner') or '')}")
    lines.append("")

    for row in planned.get("rolled") or []:
        lines.append(
            f"  [rolls to tomorrow] {row['type']} x {row['owner'] or '(unassigned)'} "
            f"— {companies_sentence(row['companies'])} ({row['why']})"
        )
    for row in planned.get("held") or []:
        lines.append(
            f"  [held] {row['type']} x {row['owner'] or '(unassigned)'} "
            f"— {companies_sentence(row['companies'])} ({row['why']})"
        )
    return "\n".join(lines)


# -- the volume contract, checked ---------------------------------------------


def contract_report(planned: dict) -> dict:
    """Does today's plan honour the volume contract? Facts, not a verdict.

    Returns the four numbers section 8 is written in terms of — message count,
    the minimum gap between consecutive sends, whether any message mixes types
    or owners, and how many groups rolled — plus a pass/fail on each. It exists
    so the contract can be asserted in a test and printed in a log rather than
    believed.
    """
    messages = planned.get("messages") or []
    times = [m["send_at"] for m in messages]
    gaps = [
        int((b - a).total_seconds() // 60) for a, b in zip(times, times[1:])
    ]
    floor = min_gap_minutes()
    mixed_type = [m for m in messages
                  if len({a["type"] for a in m["actions"]}) > 1]
    mixed_owner = [m for m in messages
                   if len({owner_key(a) for a in m["actions"]}) > 1]
    return {
        "messages": len(messages),
        "cap": config.DAILY_MESSAGE_CAP,
        "within_cap": len(messages) <= config.DAILY_MESSAGE_CAP,
        "gaps_minutes": gaps,
        "min_gap_minutes": min(gaps) if gaps else None,
        "gap_floor_minutes": floor,
        "spacing_ok": all(g >= floor for g in gaps),
        "mixed_type_messages": len(mixed_type),
        "mixed_owner_messages": len(mixed_owner),
        "grouping_ok": not mixed_type and not mixed_owner,
        "rolled": len(planned.get("rolled") or []),
        "held": len(planned.get("held") or []),
    }


def _self_test() -> int:
    """`python -m drip` — the section 8 volume contract, on a seeded day."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    today = date(2026, 9, 9)                       # a Wednesday
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    def action(company, poc, type_, owner, band, due, overdue=0):
        return {
            "type": type_, "label": nextaction.TYPE_LABELS[type_], "owner": owner,
            "due_date": due, "due_iso": dl.iso(due), "priority": band,
            "priority_label": nextaction.BAND_LABELS[band], "overdue_days": overdue,
            "company": company, "poc": poc, "poc_designation": "", "sheet_row": 2,
            "row_key": f"{company.lower()}|{poc.lower()}", "anchor": "",
            "anchor_label": "", "why": "seeded", "text": "seeded",
            "key": f"nextaction:{type_}:{company.lower()}",
        }

    # THE SEEDED DAY FROM THE PLAN: 7 due items, 2 owners, 3 types.
    seeded = [
        action("Acme", "Ann", nextaction.MEETING_PROPOSAL, "Vaishnavi",
               nextaction.P_OVERRIDE, date(2026, 9, 4), 5),
        action("Borealis", "Sam", nextaction.MEETING_PROPOSAL, "Vaishnavi",
               nextaction.P_OVERRIDE, date(2026, 9, 8), 1),
        action("Cinder", "Dev", nextaction.QUOTE_CHASE, "Vaishnavi",
               nextaction.P_MEETING, date(2026, 9, 5), 4),
        action("Delta", "Dee", nextaction.QUOTE_CHASE, "Kushal",
               nextaction.P_MEETING, date(2026, 9, 6), 3),
        action("Echo", "Eve", nextaction.FOLLOWUP, "Kushal",
               nextaction.P_FOLLOWUP, date(2026, 9, 2), 7),
        action("Fathom", "Fay", nextaction.FOLLOWUP, "Kushal",
               nextaction.P_FOLLOWUP, date(2026, 9, 3), 6),
        action("Gantry", "Gil", nextaction.FOLLOWUP, "Kushal",
               nextaction.P_FOLLOWUP, date(2026, 9, 7), 2),
    ]

    print("grouping")
    groups = group(seeded)
    check("7 items across 2 owners x 3 types -> 4 groups", len(groups), 4)
    check("the override group leads", groups[0]["type"], nextaction.MEETING_PROPOSAL)
    check("no group mixes types",
          all(len({a["type"] for a in g["actions"]}) == 1 for g in groups), True)
    check("no group mixes owners",
          all(len({owner_key(a) for a in g["actions"]}) == 1 for g in groups), True)
    check("the two replies for one owner share one message",
          sorted(groups[0]["companies"]), ["Acme", "Borealis"])

    print("\nthe plan")
    planned = plan(seeded, day=today)
    report = contract_report(planned)
    check("at most 3 messages", report["within_cap"], True)
    check("exactly 3 messages today", report["messages"], 3)
    check("gaps at or above the 75-minute floor", report["spacing_ok"], True)
    check("no message mixes a type or an owner", report["grouping_ok"], True)
    check("the fourth group rolls to tomorrow", report["rolled"], 1)

    print("\n  the day as planned:")
    for m in planned["messages"]:
        print(f"    {m['send_at_hhmm']}  slot {m['slot']}  {m['type']:<17} "
              f"{m['owner']:<10} {companies_sentence(m['companies'])}")
    print(f"    gaps: {report['gaps_minutes']} minutes (floor "
          f"{report['gap_floor_minutes']})")

    print("\ndeterminism (the restart guard)")
    again = plan(seeded, day=today)
    check("the same day plans identically",
          [m["send_at_hhmm"] for m in again["messages"]],
          [m["send_at_hhmm"] for m in planned["messages"]])
    resumed = plan(seeded, day=today, already_sent=[
        {"slot": 1, "group_key": planned["messages"][0]["group_key"],
         "action_type": planned["messages"][0]["type"], "owner_label": "Vaishnavi",
         "companies": "Acme, Borealis", "sent_at": "2026-09-09T10:00"},
    ])
    check("a restart resumes at slot 2",
          [m["slot"] for m in resumed["messages"]], [2, 3])
    check("...and does not re-send slot 1's group",
          planned["messages"][0]["group_key"]
          not in [m["group_key"] for m in resumed["messages"]], True)

    print("\nempty queue means silence")
    check("no messages", len(plan([], day=today)["messages"]), 0)

    print("\nweekends")
    sat = plan(seeded, day=date(2026, 9, 12))
    check("Saturday sends nothing", len(sat["messages"]), 0)
    check("...and holds every group for Monday", len(sat["held"]), 4)

    print("\nthe re-ask clock")
    key = groups[0]["group_key"]
    check("never asked -> nudge",
          stage_for(key, history={}, today=today)[0], STAGE_NUDGE)
    check("asked yesterday -> hold",
          stage_for(key, history={key: {"last_nudge": "2026-09-08"}}, today=today)[0],
          None)
    check("asked 2 days ago -> one re-ask",
          stage_for(key, history={key: {"last_nudge": "2026-09-07"}}, today=today)[0],
          STAGE_REASK)
    check("already re-asked yesterday -> hold",
          stage_for(key, history={key: {"last_nudge": "2026-09-05",
                                        "last_reask": "2026-09-08"}}, today=today)[0],
          None)
    check("re-asked 2 days ago -> back to a normal nudge",
          stage_for(key, history={key: {"last_nudge": "2026-09-04",
                                        "last_reask": "2026-09-07"}}, today=today)[0],
          STAGE_NUDGE)

    print("\nthe voice of the fallback")
    msg = dict(planned["messages"][0])
    text = compose_fallback(msg)
    check("no headers or bullets", any(c in text for c in ("**", "\\n-", "•")), False)
    check("names the owner once", text.count("Vaishnavi"), 1)
    check("gives an out", any(p in text.lower() for p in
                              ("no rush", "tell me", "just say", "your call")), True)
    msg["stage"] = STAGE_REASK
    check("the re-ask says it is the last one",
          "leave it after this" in compose_fallback(msg), True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

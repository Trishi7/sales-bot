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
    """Items -> one group per (rule x owner), ranked.

    ONE GROUP PER RULE, NOT PER ACTION TYPE, now that a rule IS the type. The
    practical difference is the cap: each group carries its rule's own
    `max_items_per_post`, so R5 may name five prospects in one message while
    R11 names three companies, and the overflow of each rolls to that rule's
    next run rather than to a shared queue.

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
        # THE RULE'S OWN CAP, applied per group. Overflow is reported rather
        # than silently trimmed: a rule that produced nine items and may say
        # five has four waiting, and the preview says so.
        cap = int(best.get("max_items_per_post") or 0)
        overflow = max(0, len(members) - cap) if cap else 0
        groups.append({
            "group_key": key,
            "type": best["type"],
            "rule_id": best.get("rule_id", ""),
            "rule_name": best.get("rule_name", ""),
            "max_items_per_post": cap,
            "overflow": overflow,
            "counts_toward_cap": bool(best.get("counts_toward_cap", True)),
            "destination": best.get("destination", "channel"),
            "web_pending": any(m.get("web_pending") for m in members),
            # R8's DAY-OF TOUCH KEEPS ITS OWN TIME. A note about a meeting that
            # starts at 11 is worthless at 14:00, so this one item sits OUTSIDE
            # the posting window entirely — it is the single exception, and it
            # is carried on the group so `plan` does not have to re-derive it.
            "dayof_time": next(
                (m.get("dayof_time") for m in members if m.get("dayof_time")), ""
            ),
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

    # A GROUP THAT ROLLED OFF THE END OF THE WINDOW LEADS THE NEXT RUN. It has
    # already waited a day for a reason that had nothing to do with its own
    # importance — the clock ran out — and making it queue behind today's fresh
    # items would let a busy week starve it indefinitely.
    groups.sort(key=lambda g: (
        0 if g.get("rolled_first") else 1,
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

    TWO FLOORS, AND THE LOWER ONE WINS. The schedule used to have exactly one —
    gap minus jitter, 75 at the defaults — because nothing could ever compress
    it. The posting window can: `fitted_gap` shrinks the gap evenly to land the
    whole day before SALES_DRIP_END, down to MESSAGE_GAP_MIN_MINUTES. On a busy
    day the real floor is therefore 30, not 75, and reporting 75 would be a
    number the schedule does not honour.

    It is used in two places that must agree: the contract report, and the
    SENDER's catch-up guard.

    THE SENDER NEEDS IT because "send everything whose planned time has passed"
    is wrong after a quiet morning. If the kill switch is off until 15:00, slots
    1, 2 and 3 are all overdue at once, and without this floor they would go out
    on three consecutive sweep ticks — three messages inside an hour, which is
    the exact thing the spacing exists to prevent. Catching up must still be
    paced.
    """
    nominal = int(config.MESSAGE_GAP_MINUTES) - abs(int(config.MESSAGE_JITTER_MINUTES))
    return max(1, min(nominal, int(config.MESSAGE_GAP_MIN_MINUTES)))


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


def window_minutes() -> int:
    """How many minutes the posting window holds, end minus start.

    Zero or negative when SALES_DRIP_END is at or before SALES_DRIP_START, which
    is a configuration mistake — `slot_times` then falls back to the unbounded
    schedule rather than refusing to post, because a bot that says nothing is a
    worse failure than one that says something late.
    """
    sh, sm = config.drip_start_ist()
    eh, em = config.drip_end_ist()
    return (eh * 60 + em) - (sh * 60 + sm)


def fitted_gap(count: int) -> tuple:
    """(gap in minutes, how many posts fit) for `count` posts today.

    THE GAP SHRINKS EVENLY OR NOT AT ALL. When the day overruns the window every
    post moves closer by the same amount — singling one out would make the
    schedule depend on which item happened to be third, which is not something
    anybody could predict or check.

    IT NEVER SHRINKS BELOW MESSAGE_GAP_MIN_MINUTES. Below that the messages stop
    reading as separate things and start reading as one long one delivered in
    instalments, which is the failure the spacing exists to prevent. Whatever
    does not fit at the floor is reported as not fitting, and `plan` rolls it.
    """
    n = max(1, int(count))
    span = window_minutes()
    gap = max(1, int(config.MESSAGE_GAP_MINUTES))
    floor = max(1, int(config.MESSAGE_GAP_MIN_MINUTES))

    if span <= 0:
        # Misconfigured window. Keep the old unbounded behaviour and say so.
        log.warning(
            "[drip] SALES_DRIP_END (%s) is not after SALES_DRIP_START (%s), so there "
            "is no posting window. Falling back to the unbounded schedule — posts may "
            "land late in the evening.",
            config.SALES_DRIP_END, config.SALES_DRIP_START,
        )
        return gap, n

    # One post needs no gap at all; it lands on the start time.
    if n <= 1:
        return gap, 1

    # The gap that would exactly fill the window with n posts, allowing for the
    # jitter which can push the last one later than the nominal spacing.
    jitter = abs(int(config.MESSAGE_JITTER_MINUTES))
    needed = (n - 1)
    ideal = (span - jitter) // needed if needed else gap
    if ideal >= gap:
        return gap, n                      # the normal gap already fits
    if ideal >= floor:
        log.info(
            "[drip] %d post(s) will not fit at %d min; shrinking the gap evenly to "
            "%d min so the day ends by %s.",
            n, gap, ideal, config.SALES_DRIP_END,
        )
        return int(ideal), n

    # Even at the floor it does not fit. How many DO?
    fits = 1 + max(0, (span - jitter) // floor)
    log.info(
        "[drip] %d post(s) will not fit before %s even at the %d-min floor; %d will "
        "go today and the rest roll to the next applicable day, where they go first.",
        n, config.SALES_DRIP_END, floor, fits,
    )
    return floor, max(1, int(fits))


def slot_times(day: date, *, count: int) -> list:
    """The planned send times for one day, as datetimes in IST.

    The first slot lands on SALES_DRIP_START exactly; every later slot is the
    previous PLANNED time plus the fitted gap plus that slot's jitter.
    Compounding off the previous PLANNED time rather than off the actual send
    keeps the schedule deterministic even when a send is late or a tick is
    missed.

    NOTHING IS RETURNED AFTER SALES_DRIP_END. The gap shrinks evenly to fit the
    day into the window (`fitted_gap`), and a time that would still fall outside
    it is CLAMPED to the end rather than returned late — `plan` asks for only as
    many slots as `fitted_gap` said would fit, so a clamped time means the
    arithmetic drifted and the clamp is the backstop, not the mechanism.

    THE GAP CAN NEVER GO BELOW MESSAGE_GAP_MIN_MINUTES *within the slots that
    fit*. At the defaults that is 30 minutes, and the self-test asserts it.

    SLOTS PAST `fitted_gap`'s COUNT ARE DEGENERATE, deliberately: they are all
    clamped to the window end and sit on top of each other. `plan` never asks
    for them — it caps at that count and rolls the rest — so they exist only so
    that indexing by slot number cannot raise. Do not use them; ask
    `fitted_gap` how many the day actually holds.
    """
    hour, minute = config.drip_start_ist()
    first = datetime(day.year, day.month, day.day, hour, minute, tzinfo=dl.IST)
    eh, em = config.drip_end_ist()
    last_allowed = datetime(day.year, day.month, day.day, eh, em, tzinfo=dl.IST)

    n = max(1, int(count))
    gap, _fits = fitted_gap(n)
    floor = max(1, int(config.MESSAGE_GAP_MIN_MINUTES))
    out = [first]
    for slot in range(2, n + 1):
        # THE JITTER IS CLAMPED, NOT JUST ADDED. On a compressed day the fitted
        # gap can be close to the floor, and a negative jitter would push the
        # real gap BELOW it — 42 minus 15 is 27, and the floor said 30. The
        # clamp is applied to the STEP rather than to the jitter so the schedule
        # stays deterministic: the same (date, slot) still produces the same
        # time, it simply cannot produce one too close to its predecessor.
        step = max(floor, gap + _jitter(day, slot))
        nxt = out[-1] + timedelta(minutes=step)
        if window_minutes() > 0 and nxt > last_allowed:
            nxt = last_allowed
        out.append(nxt)
    return out


def sunday_rule_ids() -> set:
    """The rule ids allowed to send on a Sunday. Everything else waits."""
    return {str(r).strip().upper() for r in (config.SUNDAY_RULE_IDS or []) if str(r).strip()}


def sunday_allows(group: dict) -> bool:
    """May this group go out on a Sunday?

    ONE POST, AT SALES_DRIP_START, AND ONLY FOR THE DELIVERABLES CHECKLIST. The
    weekend is silent; Sunday's single exception exists because a P1 deliverable
    due on Monday morning is the one thing that cannot wait until Monday
    morning to be mentioned.

    The rule id is matched, not the content — so the exception is a thing
    somebody can change in SUNDAY_RULE_IDS rather than a condition buried here.
    """
    allowed = sunday_rule_ids()
    if not allowed:
        return False
    return str(group.get("rule_id") or "").strip().upper() in allowed


def sunday_due_monday(group: dict, *, day: date) -> bool:
    """...and only for items whose deadline actually falls on the Monday.

    A deliverable due on Thursday is not a Sunday problem, and posting it on a
    Sunday would spend the one weekend message the team tolerates on something
    that had four working days left. Monday here means the NEXT day, which on a
    Sunday is the Monday.
    """
    monday = day + timedelta(days=1)
    for member in group.get("actions") or ():
        due = member.get("due_date")
        if due is not None and due <= monday:
            return True
    return False


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
    # THE PER-DAY CAP. Monday and Tuesday carry more rules than Wednesday, so a
    # single scalar either throttled those two days or let the quiet ones run
    # loose. `cap` still overrides everything, for the dry-run and the tests.
    limit = max(0, int(config.message_cap_for(day) if cap is None else cap))

    groups = group(actions)
    result = {
        "messages": [], "sent": already, "held": [], "rolled": [],
        "groups": groups, "day": day, "is_sending_day": is_sending_day(day),
    }

    # SATURDAY IS SILENT, FULL STOP. Sunday carries ONE post and only for the
    # rules in SUNDAY_RULE_IDS whose items are due on the Monday.
    if not result["is_sending_day"]:
        if day.weekday() == 6:
            eligible = [
                g for g in groups
                if sunday_allows(g) and sunday_due_monday(g, day=day)
            ]
            for g in groups:
                if g not in eligible:
                    why = (
                        HELD_WEEKEND + " (Sunday sends only "
                        + ", ".join(sorted(sunday_rule_ids()) or ["nothing"])
                        + ", and only for items due Monday)"
                    )
                    result["held"].append({**g, "why": why})
            if eligible:
                # The cap is 1 by default and is enforced the same way every
                # other day's is — through `message_cap_for` — so a team that
                # wants two Sunday posts changes a setting, not this branch.
                limit = config.message_cap_for(day)
                times = slot_times(day, count=max(1, limit))
                for i, g in enumerate(eligible[:limit]):
                    result["messages"].append({
                        **g, "slot": i + 1, "stage": STAGE_NUDGE,
                        "send_at": times[i],
                        "why": "Sunday exception: a Deliverables item is due tomorrow",
                    })
                for g in eligible[limit:]:
                    result["rolled"].append({**g, "why": HELD_CAP + " (Sunday's cap)"})
                log.info(
                    "[drip] %s is a Sunday — %d post(s) for %s, everything else waits "
                    "for Monday.", dl.iso(day), len(result["messages"]),
                    ", ".join(sorted(sunday_rule_ids())),
                )
                result["is_sending_day"] = True
                return result
            log.info(
                "[drip] %s is a Sunday with nothing due Monday — silent. %d group(s) "
                "wait.", dl.iso(day), len(groups),
            )
            return result

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

    # ENOUGH SLOT TIMES FOR EVERY POST, not just the capped ones. R8 and R9 sit
    # OUTSIDE the cap, so a day can legitimately carry more posts than `limit`
    # — and each of those still needs a spaced, deterministic send time.
    #
    # THE WINDOW MAY ALLOW FEWER THAN THE CAP DOES. `fitted_gap` says how many
    # posts fit between SALES_DRIP_START and SALES_DRIP_END once the gap has
    # shrunk as far as it may; anything past that rolls, exactly as a group over
    # the cap does, and for the same reason — a message that would land at
    # 21:13 is not read that night and is resented in the morning.
    wanted = max(limit, len(groups)) + len(already) + 1
    times = slot_times(day, count=wanted)
    _gap, window_fits = fitted_gap(wanted)
    window_left = max(0, int(window_fits) - len(already))

    # THE CAP COUNTS ONLY THE GROUPS THAT COUNT. A meeting-prep post and a
    # meeting-follow-up post are time-critical: the meeting is happening whether
    # or not Monday's chases fit, and letting a Monday chase crowd one out is
    # the opposite of what the cap is for. Their rules declare
    # `counts_toward_cap: false` in bot_rules.yaml and this is where that means
    # something.
    counted = sum(
        1 for r in already
        if bool(r.get("counts_toward_cap", True))
    )

    for g in groups:
        if g["group_key"] in sent_group_keys:
            continue                       # already said today; not said twice
        stage, why = stage_for(g["group_key"], history=history, today=day)
        if stage is None:
            result["held"].append({**g, "why": why})
            continue
        against_cap = bool(g.get("counts_toward_cap", True))
        if against_cap and counted >= limit:
            result["rolled"].append({
                **g, "stage": stage, "rolled_why": "cap",
                "why": HELD_CAP + (
                    " (a meeting-band group leads the next run by construction, so "
                    "this one will not sit behind the same groups again)"
                    if g["priority"] != nextaction.P_MEETING else ""
                ),
            })
            continue

        # THE WINDOW ROLLS TOO, and it rolls EVERYTHING — a group outside the
        # daily cap is still a message that would land after SALES_DRIP_END, and
        # "this one does not count against the cap" was never a licence to post
        # it at nine at night.
        #
        # A ROLLED GROUP GOES FIRST NEXT TIME. `rolled_why` marks it and
        # `rolled_first` sorts on it, because a thing that already waited a day
        # should not queue behind a thing that has not waited at all.
        if window_left <= 0:
            result["rolled"].append({
                **g, "stage": stage, "rolled_why": "window", "rolled_first": True,
                "why": (
                    f"it would land after {config.SALES_DRIP_END}, so it goes first "
                    "on the next applicable day"
                ),
            })
            continue
        # THE ONE EXCEPTION TO THE WINDOW. R8's day-of touch fires at
        # MEETING_DAYOF_TIME (10:00), before the window opens, because it is
        # about something happening today and the window is about not
        # interrupting anybody's evening — a different problem.
        send_at = times[next_slot - 1]
        if g.get("dayof_time"):
            try:
                hh, mm = (int(x) for x in str(g["dayof_time"]).split(":")[:2])
                send_at = datetime(day.year, day.month, day.day, hh, mm, tzinfo=dl.IST)
            except (TypeError, ValueError):
                log.warning(
                    "[drip] MEETING_DAYOF_TIME=%r is not HH:MM; the day-of touch takes "
                    "its ordinary window slot instead.", g["dayof_time"],
                )
        result["messages"].append({
            **g,
            "slot": next_slot,
            "stage": stage,
            "stage_why": why,
            "counts_toward_cap": against_cap,
            "send_at": send_at,
            "send_at_hhmm": send_at.strftime("%H:%M"),
        })
        next_slot += 1
        window_left -= 1
        if against_cap:
            counted += 1

    log.info(
        "[drip] %s: %d group(s) from %d action(s) -> %d already sent, %d planned "
        "(%d against the cap, %d outside it), %d held, %d rolling to tomorrow "
        "(cap %d for %s). NOTHING HAS BEEN SENT BY THIS MODULE — it computes only.",
        dl.iso(day), len(groups), len(actions or []), len(already),
        len(result["messages"]),
        sum(1 for m in result["messages"] if m.get("counts_toward_cap", True)),
        sum(1 for m in result["messages"] if not m.get("counts_toward_cap", True)),
        len(result["held"]), len(result["rolled"]), limit, day.strftime("%A"),
    )
    for m in result["messages"]:
        log.info(
            "[drip]   slot %d at %s  %-4s %s x %s  (%d item(s)%s, %s)",
            m["slot"], m.get("send_at_hhmm") or m["send_at"].strftime("%H:%M"),
            m.get("rule_id", ""), m["type"],
            m["owner"] or "(unassigned)", m.get("count", len(m["companies"])),
            "" if m.get("counts_toward_cap", True) else ", OUTSIDE the cap",
            m["stage"],
        )
    return result


# -- the message --------------------------------------------------------------

# WHAT EACH TYPE IS ASKING FOR, as a plain phrase the composer builds on. These
# are the FALLBACK wording, used when the model is off or unreachable. They are
# deliberately complete sentences with an out already in them, because a
# fallback that reads like a template is what people will actually receive on
# the day the API is down.
_ASK = {
    nextaction.R_AI_NEWS: (
        "{who}here is what moved in AI since yesterday — funding, hires, papers from "
        "people we are talking to. Skim it when you have a minute."
    ),
    nextaction.R_NEWS_SCREEN: (
        "{who}a few companies in the news this week are not in the Master Pipeline. "
        "Here is why each one is or is not worth adding — tell me which, if any, and "
        "I will put them in."
    ),
    nextaction.R_EVENTS: (
        "{who}{companies} is coming up and we are not registered. Worth a look before "
        "registration closes — tell me if it is not for us and I will leave it."
    ),
    nextaction.R_DELIVERABLES: (
        "{who}{companies} is a P1 on the checklist and its deadline is nearly here. "
        "Where has it got to? No rush if it is in hand, just say."
    ),
    nextaction.R_PROSPECTS: (
        "{who}{companies} — nobody has made first contact with these yet. Worth a "
        "look when you get a window; tell me when they are done and I will move on."
    ),
    nextaction.R_LI_NO_DM: (
        "{who}{companies} connected a few days back and I have no DM against them. "
        "If it has gone, tell me and I will note it; if not, now is while it is warm."
    ),
    nextaction.R_DM_NO_MEETING: (
        "{who}the DM to {companies} has been sitting a while with no meeting behind "
        "it. Worth a look at where it got to when you have a minute."
    ),
    nextaction.R_MEETING_PREP: (
        "{who}the {companies} meeting is coming up. Deck, package and demo all in "
        "hand? If it is all set just say, and I will leave it alone — I only did "
        "not want it to arrive as a surprise."
    ),
    nextaction.R_MEETING_FOLLOWUP: (
        "{who}the {companies} meeting is down as done and there are no next steps "
        "against it. What came out of it, which package came up, and roughly what "
        "size? Tell me and I will stop asking."
    ),
    nextaction.R_CLOSURE_SUPPORT: (
        "{who}{companies} is sitting above 50% and has not moved stage. What would it "
        "take to get it to the next one? Happy to dig anything out."
    ),
    nextaction.R_NEW_COMPANY: (
        "{who}{companies} is new in the Master Pipeline. I can fill in the funding, "
        "location and industry and suggest a few PoCs — say the word and I will."
    ),
    nextaction.R_PACKAGES: (
        "{who}{companies} is still not marked ready. What is left on it, and roughly "
        "when? No rush, I just do not want to offer it before it is there."
    ),
    nextaction.SCHEDULED_REMINDER: (
        "{who}you asked me to flag {companies} today. Here it is — tell me when it is "
        "done, or tell me to move it."
    ),
}

# THE RE-ASK IS SOFTER, ALWAYS. Same subject, less weight, and it says out loud
# that it is the last time — which is what makes it land as a courtesy rather
# than as a second demand.
_REASK = (
    "{who}circling back on {companies} — no pressure at all, and I will leave it "
    "after this. If it is handled or not worth it, just say and I will drop it."
)


def tag_prefix(*, owner_id=None, owner_name: str = "", is_dm: bool = False) -> str:
    """THE TAGS THAT OPEN EVERY PROACTIVE CHANNEL MESSAGE.

    Vaishnavi and Sid (SALES_ALWAYS_TAG_IDS), plus the item's owner when that is
    somebody else. Returns "" for a DM and "" when nothing is configured.

    AT THE START, NOT THE END, and that is the whole reason this is a separate
    function rather than a line in the composer. A tag after the paragraph is
    read after the paragraph, which is the wrong order for something that says
    "this is for you": a reader who sees their name first decides whether to
    read on, and one who sees it last has already decided not to.

    THE OWNER IS NOT TAGGED TWICE. When the owner is already one of the
    always-tag ids — which they usually are, since Vaishnavi owns most rows —
    they appear once. "@Vaishnavi @Sid @Vaishnavi" reads as a bot that cannot
    count.

    A DM GETS NO TAGS AT ALL. It is already addressed to exactly one person, and
    opening it by tagging two others would be strange when one of them is not in
    it.

    Every token comes from `guardrails.mention_for`, so an id that is not on the
    roster is named in plain text rather than pinged — the same gate as
    everywhere else, asked here too rather than trusted from the config.
    """
    if is_dm:
        return ""

    import guardrails

    tokens: list = []
    seen: set = set()
    for uid in config.always_tag_ids():
        if uid in seen:
            continue
        seen.add(uid)
        token = guardrails.mention_for(uid)
        if token:
            tokens.append(token)

    try:
        owner_uid = int(owner_id) if owner_id else 0
    except (TypeError, ValueError):
        owner_uid = 0
    if owner_uid and owner_uid not in seen:
        token = guardrails.mention_for(owner_uid, owner_name)
        if token:
            tokens.append(token)
    elif not owner_uid and owner_name.strip() and not tokens:
        # No id for the owner and nobody to always-tag: name them in plain text
        # so the message still says who it is for.
        tokens.append(owner_name.strip())

    return " ".join(tokens)


def with_tags(text: str, *, owner_id=None, owner_name: str = "",
              is_dm: bool = False) -> str:
    """`text` with the tag line in front of it. The one place tags are attached.

    Separate from `compose_fallback` and `compose_prompt` on purpose: the model
    composes the BODY and must never be asked to write a mention token itself.
    `guardrails.sanitize` strips any it invents, so a model-written tag would
    vanish silently and the message would go out addressed to nobody.
    """
    prefix = tag_prefix(owner_id=owner_id, owner_name=owner_name, is_dm=is_dm)
    body = (text or "").strip()
    if not prefix:
        return body
    if not body:
        return prefix
    return f"{prefix}\n{body}"


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
        else _ASK.get(message.get("type"), _ASK[nextaction.R_DM_NO_MEETING])
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

    def action(company, poc, type_, owner, band, due, overdue=0, rule_id="R7"):
        return {
            "type": type_, "rule": type_, "rule_id": rule_id,
            "rule_name": nextaction.TYPE_LABELS[type_],
            "label": nextaction.TYPE_LABELS[type_], "owner": owner,
            "due_date": due, "due_iso": dl.iso(due), "priority": band,
            "priority_label": nextaction.BAND_LABELS[band], "overdue_days": overdue,
            "company": company, "poc": poc, "poc_designation": "", "sheet_row": 2,
            "row_key": f"{company.lower()}|{poc.lower()}",
            "contact_key": f"{company.lower()}|{poc.lower()}",
            "max_items_per_post": 5, "counts_toward_cap": True,
            "destination": "channel", "web_pending": False,
            "why": "seeded", "text": "seeded",
            "key": f"{rule_id}:{company.lower()}",
        }

    # THE SEEDED DAY FROM THE PLAN: 7 due items, 2 owners, 3 types.
    seeded = [
        action("Acme", "Ann", nextaction.R_MEETING_PREP, "Vaishnavi",
               nextaction.P_MEETING, date(2026, 9, 4), 5, "R8"),
        action("Borealis", "Sam", nextaction.R_MEETING_PREP, "Vaishnavi",
               nextaction.P_MEETING, date(2026, 9, 8), 1, "R8"),
        action("Cinder", "Dev", nextaction.R_LI_NO_DM, "Vaishnavi",
               nextaction.P_REPLY, date(2026, 9, 5), 4, "R6"),
        action("Delta", "Dee", nextaction.R_LI_NO_DM, "Kushal",
               nextaction.P_REPLY, date(2026, 9, 6), 3, "R6"),
        action("Echo", "Eve", nextaction.R_DM_NO_MEETING, "Kushal",
               nextaction.P_CHASE, date(2026, 9, 2), 7),
        action("Fathom", "Fay", nextaction.R_DM_NO_MEETING, "Kushal",
               nextaction.P_CHASE, date(2026, 9, 3), 6),
        action("Gantry", "Gil", nextaction.R_DM_NO_MEETING, "Kushal",
               nextaction.P_CHASE, date(2026, 9, 7), 2),
    ]

    print("grouping")
    groups = group(seeded)
    check("7 items across 2 owners x 3 types -> 4 groups", len(groups), 4)
    check("the meeting-band group leads", groups[0]["type"], nextaction.R_MEETING_PREP)
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
    check("gaps at or above the floor", report["spacing_ok"], True)
    check("the floor is MESSAGE_GAP_MIN_MINUTES on a compressed day",
          min_gap_minutes(), config.MESSAGE_GAP_MIN_MINUTES)

    print("\nthe posting window")
    for n, want_late in ((3, 0), (6, 0), (12, 0), (25, 0)):
        times = slot_times(today, count=n)
        eh, em = config.drip_end_ist()
        late = [t for t in times if (t.hour * 60 + t.minute) > (eh * 60 + em)]
        check(f"{n} post(s): none after {config.SALES_DRIP_END}", len(late), want_late)
    check("one post lands exactly on the start",
          slot_times(today, count=1)[0].strftime("%H:%M"), config.SALES_DRIP_START)
    g3, _ = fitted_gap(3)
    g8, _ = fitted_gap(8)
    check("a light day keeps the full gap", g3, config.MESSAGE_GAP_MINUTES)
    check("a heavy day shrinks the gap", g8 < config.MESSAGE_GAP_MINUTES, True)
    check("...but never below the floor", g8 >= config.MESSAGE_GAP_MIN_MINUTES, True)
    _g, fits = fitted_gap(40)
    check("an impossible day reports what fits", fits < 40, True)
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

"""PHASE 1 OF THE SALES CADENCE — nine rules, computed once a day.

WHAT THIS IS. Vaishnavi's "Steps for Sales Bot" doc lists nine things a human
would notice reading the tracker every morning: a follow-up that has gone cold,
a connection with no intro behind it, a positive reply nobody booked a meeting
off. This module is those nine checks, run against the "Master data" tab, and
NOTHING ELSE. It produces a list of items. It does not send anything.

    THERE IS NO PROACTIVE SEND PATH IN THIS FILE, and there must never be one.
    Every item here is handed to the EXISTING once-daily digest in bot.py, which
    is still the only unprompted message this bot writes. A rule that posted its
    own finding would undo the entire reason digest.py exists — see the header of
    that file. Nothing here can make the bot speak.

THE NINE RULES, and the threshold each is tuned by:

    a  stale_followup     Last Followed-up Date older than FOLLOWUP_STALE_DAYS
                          and nothing has come back → chase the owner.
    b  intro_pending      Connected = Y but membrane Intro Date is blank.
    c  start_interacting  First Contacted set, never Connected, and
                          CONNECT_REMINDER_DAYS have passed.
    d  alt_channel        Total follow-ups >= ALT_CHANNEL_AT with no response →
                          recommend email / WhatsApp / a call instead.
    e  unresponsive       Total follow-ups >= UNRESPONSIVE_AT with no response →
                          ASK THE OWNER to mark the PoC unresponsive.
                          THE BOT NEVER WRITES THAT. Its only writable cell
                          anywhere is its own deadline column; marking a person
                          unresponsive is a judgement with consequences and it
                          stays with the human who owns the row.
    f  lock_meeting       Response = Y or P but Next Steps is blank → lock a
                          meeting. This is the money rule: a positive reply
                          sitting with no next action is the most expensive
                          thing in the sheet.
    g  try_another_poc    Response = N → try a different PoC at that company,
                          with candidates from the Researcher Buyer Mapping when
                          the org is mapped.
    h  post_meeting       Meeting Date in the past but BOTH Assets Shared and
                          Next Steps blank → chase.
    i  nextstep_stall     Next Steps present but unchanged for
                          NEXTSTEP_STALL_DAYS. "Unchanged" is not something a
                          spreadsheet cell knows, so the bot keeps its own
                          history — see db.track_next_steps.

EVERY THRESHOLD IS A PLACEHOLDER. Vaishnavi's "n" values were left unset in the
doc. The defaults in config.py are guesses, they are env vars, and tuning them
is a restart rather than a deploy.

EXCLUSION COMES FIRST. A row marked rejected is dropped before any rule runs —
not filtered out of the digest afterwards, dropped. Rejected rows are revisited
offline by humans, and a bot that keeps chasing a prospect the team has written
off is the fastest way to get the digest muted. `Response = N` is NOT rejection;
rule (g) is the whole point of the distinction.

PRIORITY, from the 27 Aug meeting:
    URGENT   a positive reply awaiting our action (f), a meeting inside
             MEETING_PREP_DAYS, and post-meeting gaps (h)
    WAITING  the cadence items (a–e, i)
    HELD     everything else (g) — a "no" is real work, but it is not today's
             work, and it must never push a booked meeting off the digest.

THE CAP IS HONEST. DIGEST_MAX_ITEMS items go out, urgent first. What doesn't fit
is COUNTED in a closing line that says how to see it, because a silently
truncated list reads as "there were only fifteen things" and that is a lie the
digest cannot afford to tell.

THIS MODULE IS PURE. It takes rows, a date and a few lookups, and returns dicts.
No sheet reads, no database writes, no Discord. That is what makes `python
cadence.py --demo` a real test of the rules rather than a test of the network.
"""
import logging
from datetime import date
from typing import Callable, Optional

import config
import deadlines as dl
import gtm_sheet

log = logging.getLogger(__name__)

# -- rule ids -----------------------------------------------------------------
RULE_STALE_FOLLOWUP = "stale_followup"        # a
RULE_INTRO_PENDING = "intro_pending"          # b
RULE_START_INTERACTING = "start_interacting"  # c
RULE_ALT_CHANNEL = "alt_channel"              # d
RULE_UNRESPONSIVE = "unresponsive"            # e
RULE_LOCK_MEETING = "lock_meeting"            # f
RULE_TRY_ANOTHER_POC = "try_another_poc"      # g
RULE_POST_MEETING = "post_meeting"            # h
RULE_NEXTSTEP_STALL = "nextstep_stall"        # i
RULE_MEETING_SOON = "meeting_soon"            # the prep window (not one of the nine)

RULE_LETTERS = {
    RULE_STALE_FOLLOWUP: "a",
    RULE_INTRO_PENDING: "b",
    RULE_START_INTERACTING: "c",
    RULE_ALT_CHANNEL: "d",
    RULE_UNRESPONSIVE: "e",
    RULE_LOCK_MEETING: "f",
    RULE_TRY_ANOTHER_POC: "g",
    RULE_POST_MEETING: "h",
    RULE_NEXTSTEP_STALL: "i",
    RULE_MEETING_SOON: "prep",
}

# -- priority buckets ---------------------------------------------------------
URGENT = "urgent"
WAITING = "waiting"
HELD = "held"

_BUCKET_RANK = {URGENT: 0, WAITING: 1, HELD: 2}

RULE_PRIORITY = {
    RULE_LOCK_MEETING: URGENT,     # positive response awaiting our action
    RULE_MEETING_SOON: URGENT,     # a meeting inside the prep window
    RULE_POST_MEETING: URGENT,     # post-meeting gap
    RULE_STALE_FOLLOWUP: WAITING,
    RULE_INTRO_PENDING: WAITING,
    RULE_START_INTERACTING: WAITING,
    RULE_ALT_CHANNEL: WAITING,
    RULE_UNRESPONSIVE: WAITING,
    RULE_NEXTSTEP_STALL: WAITING,
    RULE_TRY_ANOTHER_POC: HELD,    # a "no" is not today's work
}

# -- digest sections ----------------------------------------------------------
# Vaishnavi's daily list, in her words. The section keys themselves live in
# digest.py; this map is which rule belongs on which of her lines.
SECTION_FOLLOWUPS = "cadence_followups"
SECTION_INTROS = "cadence_intros"
SECTION_MEETINGS = "cadence_meetings"
SECTION_ASSETS = "cadence_assets"
SECTION_UPDATES = "cadence_updates"

RULE_SECTION = {
    RULE_STALE_FOLLOWUP: SECTION_FOLLOWUPS,
    RULE_START_INTERACTING: SECTION_FOLLOWUPS,
    RULE_ALT_CHANNEL: SECTION_FOLLOWUPS,
    RULE_TRY_ANOTHER_POC: SECTION_FOLLOWUPS,
    RULE_INTRO_PENDING: SECTION_INTROS,
    RULE_LOCK_MEETING: SECTION_MEETINGS,
    RULE_MEETING_SOON: SECTION_MEETINGS,
    RULE_POST_MEETING: SECTION_ASSETS,
    RULE_UNRESPONSIVE: SECTION_UPDATES,
    RULE_NEXTSTEP_STALL: SECTION_UPDATES,
}

CADENCE_SECTIONS = (
    SECTION_FOLLOWUPS,
    SECTION_INTROS,
    SECTION_MEETINGS,
    SECTION_ASSETS,
    SECTION_UPDATES,
)


# -- reading a row ------------------------------------------------------------


def _text(row: dict, role: str) -> str:
    return str(row.get(role) or "").strip()


def _row_blob(row: dict) -> str:
    """Every readable cell of a row as one lower-cased string.

    Used only by the rejection test, which has to look everywhere: the team
    writes "rejected" in whichever column is nearest to hand, and a marker in
    Other Updates means exactly what a marker in Status means.
    """
    parts = [str(v) for k, v in row.items() if not str(k).startswith("_")]
    parts += [str(v) for v in (row.get("_extra") or {}).values()]
    return " | ".join(parts).lower()


def is_rejected(row: dict) -> tuple[bool, str]:
    """Has a human written this row off? Returns (rejected, the marker found).

    EXCLUDED FROM EVERY RULE AND EVERY DIGEST SECTION when true — never chased,
    revisited offline by people. Deliberately a phrase match on explicit
    markers, never an inference: a negative Response is not a rejection (rule (g)
    exists for exactly that row), and a blank cell is not a rejection either.
    """
    blob = _row_blob(row)
    if not blob:
        return False, ""
    for marker in config.CADENCE_REJECTED_MARKERS or []:
        m = str(marker or "").strip().lower()
        if not m:
            continue
        # Bounded on both sides so "lost" doesn't match "closed lost deal
        # analysis" — well, it does, and that is correct — but so that it does
        # not match "lostrand Ltd".
        for boundary_l in ("", " ", "|", ":", "-", "(", ","):
            probe = f"{boundary_l}{m}"
            idx = blob.find(probe)
            while idx != -1:
                after = blob[idx + len(probe): idx + len(probe) + 1]
                if after in ("", " ", "|", ":", ".", ",", ")", "-", "/", ";"):
                    return True, m
                idx = blob.find(probe, idx + 1)
    return False, ""


def responded(row: dict) -> str:
    """The row's response polarity, using the tracker's own vocabulary."""
    return gtm_sheet.response_status(row.get("response"))


def no_response(row: dict) -> bool:
    """Nothing has come back at all. Distinct from "they said no"."""
    return responded(row) == gtm_sheet.RESPONSE_NONE


def positive_response(row: dict) -> bool:
    """Y or P — a reply we can act on. An unclassified reply counts: somebody
    wrote something in the cell, so they DID come back, and the cost of asking
    about a real reply is far lower than the cost of missing one."""
    return responded(row) in (gtm_sheet.RESPONSE_POSITIVE, gtm_sheet.RESPONSE_UNKNOWN)


def negative_response(row: dict) -> bool:
    return responded(row) == gtm_sheet.RESPONSE_NEGATIVE


def followups_count(row: dict) -> int:
    """Total follow-ups as a number. An unreadable or blank cell is 0 — the
    count-based rules must not fire on a row whose counter nobody filled in."""
    raw = _text(row, "followups_count")
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else 0


def days_since(value, today: date) -> Optional[int]:
    """Calendar days between a sheet date and today, or None if unreadable.

    CALENDAR days, not working days: Vaishnavi's thresholds are written as
    plain "n days" and a follow-up that went cold over a long weekend is just as
    cold on Tuesday. The deadline machinery elsewhere in this bot uses working
    days, and that difference is deliberate.
    """
    parsed = dl.parse_date(value)
    if parsed is None:
        return None
    return (today - parsed).days


def row_identity(row: dict) -> tuple[str, str]:
    """(company, poc) as displayed. A row always has a company — `_parse_values`
    drops the ones that don't."""
    return _text(row, "company") or "(unnamed row)", _text(row, "poc")


def describe_row(row: dict) -> str:
    """The company · PoC · designation prefix every cadence line opens with."""
    company, poc = row_identity(row)
    parts = [company]
    if poc:
        designation = _text(row, "poc_designation")
        parts.append(f"{poc}{f' ({designation})' if designation else ''}")
    return " · ".join(parts)


# -- the rules ----------------------------------------------------------------


def _item(
    row: dict, *, rule: str, text: str, today: date, age_days: int = 0,
    detail: str = "",
) -> dict:
    """One finding. `key` is what carries an item's AGE across days, so it is
    built from the rule and the row identity — never from the sheet row number,
    which moves whenever somebody sorts the tab."""
    company, poc = row_identity(row)
    ckey = gtm_sheet.normalise_header(company)
    pkey = gtm_sheet.normalise_header(poc)
    bucket = RULE_PRIORITY.get(rule, WAITING)
    return {
        "rule": rule,
        "letter": RULE_LETTERS.get(rule, "?"),
        "priority": bucket,
        "section": RULE_SECTION.get(rule, SECTION_UPDATES),
        "company": company,
        "poc": poc,
        "owner": _text(row, "owner"),
        "sheet_row": row.get("_row"),
        "text": text,
        "detail": detail,
        "age_days": max(0, int(age_days or 0)),
        "key": f"cadence:{rule}:{ckey}:{pkey}",
        "_row": row,
    }


def rule_stale_followup(row: dict, *, today: date) -> Optional[dict]:
    """(a) Last Followed-up Date older than n days AND no response."""
    if not no_response(row):
        return None
    age = days_since(row.get("last_followed_up"), today)
    if age is None or age < max(0, config.FOLLOWUP_STALE_DAYS):
        return None
    return _item(
        row, rule=RULE_STALE_FOLLOWUP, today=today, age_days=age,
        text=(
            f"{describe_row(row)} — last followed up {age}d ago, still no response. "
            f"Follow up or park it."
        ),
        detail=f"Last Followed-up Date is {_text(row, 'last_followed_up')!r}; "
               f"threshold is {config.FOLLOWUP_STALE_DAYS}d.",
    )


def rule_intro_pending(row: dict, *, today: date) -> Optional[dict]:
    """(b) Connected = Y but membrane Intro Date is blank."""
    if not gtm_sheet.is_yes(row.get("connected")):
        return None
    if _text(row, "intro_date"):
        return None
    return _item(
        row, rule=RULE_INTRO_PENDING, today=today,
        text=f"{describe_row(row)} — connected, but no membrane intro date recorded. Intro pending.",
        detail="Connected says yes and membrane Intro Date is empty.",
    )


def rule_start_interacting(row: dict, *, today: date) -> Optional[dict]:
    """(c) First Contacted set, never Connected, n days gone."""
    if gtm_sheet.is_yes(row.get("connected")):
        return None
    age = days_since(row.get("first_contacted"), today)
    if age is None or age < max(0, config.CONNECT_REMINDER_DAYS):
        return None
    return _item(
        row, rule=RULE_START_INTERACTING, today=today, age_days=age,
        text=(
            f"{describe_row(row)} — first contacted {age}d ago and still not connected. "
            f"Start interacting."
        ),
        detail=f"First Contacted is {_text(row, 'first_contacted')!r}; Connected is "
               f"{_text(row, 'connected') or 'blank'!r}; threshold is "
               f"{config.CONNECT_REMINDER_DAYS}d.",
    )


def rule_alt_channel(row: dict, *, today: date) -> Optional[dict]:
    """(d) n follow-ups, no response → change the channel, not the message."""
    if not no_response(row):
        return None
    count = followups_count(row)
    threshold = max(1, config.ALT_CHANNEL_AT)
    # Rule (e) supersedes this one: past the unresponsive threshold the ask is
    # "mark them unresponsive", not "try WhatsApp".
    if count < threshold or count >= max(1, config.UNRESPONSIVE_AT):
        return None
    return _item(
        row, rule=RULE_ALT_CHANNEL, today=today,
        text=(
            f"{describe_row(row)} — {count} follow-ups, no response. Try another channel "
            f"(email / WhatsApp / call)."
        ),
        detail=f"Total follow-ups is {count}; threshold is {threshold}.",
    )


def rule_unresponsive(row: dict, *, today: date) -> Optional[dict]:
    """(e) n follow-ups, no response → ask the OWNER to mark them unresponsive.

    The bot asks. The bot does not write. Its only writable cell anywhere in any
    spreadsheet is its own deadline column, and marking a person unresponsive is
    a judgement with consequences for the relationship — it belongs to whoever
    owns the row.
    """
    if not no_response(row):
        return None
    count = followups_count(row)
    threshold = max(1, config.UNRESPONSIVE_AT)
    if count < threshold:
        return None
    _company, poc = row_identity(row)
    who = poc or "the PoC"
    return _item(
        row, rule=RULE_UNRESPONSIVE, today=today,
        text=(
            f"{describe_row(row)} — {count} follow-ups, no response. Please mark {who} "
            f"\"unresponsive\" in the sheet (I don't write that cell)."
        ),
        detail=f"Total follow-ups is {count}; threshold is {threshold}. The bot never "
               f"writes this — it is the owner's call.",
    )


def rule_lock_meeting(row: dict, *, today: date) -> Optional[dict]:
    """(f) Response Y/P but Next Steps blank → lock a meeting. URGENT.

    TWO ROWS THIS DOES NOT FIRE ON, because on them "lock a meeting" is the
    wrong sentence:
      - a meeting is already BOOKED (Meeting Date today or later). It is locked.
        If it is close, rule_meeting_soon says so instead.
      - the meeting already HAPPENED and left nothing behind — that is rule (h),
        which asks for the assets and the next step rather than for another
        meeting.
    A past meeting that DID leave assets behind still fires here: that is a real
    "they're warm, book the next one".
    """
    if not positive_response(row):
        return None
    if _text(row, "next_steps"):
        return None
    meeting = dl.parse_date(row.get("meeting_date"))
    if meeting is not None:
        if meeting >= today:
            return None                                   # already booked
        if not _text(row, "assets_shared"):
            return None                                   # rule (h) owns this row
    return _item(
        row, rule=RULE_LOCK_MEETING, today=today,
        text=(
            f"{describe_row(row)} — responded ({_text(row, 'response') or 'positive'}) "
            f"with no next step recorded. Lock a meeting."
        ),
        detail="Response is positive/unclassified and Next Steps is empty.",
    )


def rule_try_another_poc(
    row: dict, *, today: date, mapping_lookup: Optional[Callable[[str], list]] = None
) -> Optional[dict]:
    """(g) Response = N → try another PoC at that company.

    Candidates come from the Researcher Buyer Mapping when the org is mapped,
    and they arrive WITH THEIR CAVEATS or not at all — a name from that sheet is
    only usable alongside its staleness and departure checks, so the lookup
    passed in is expected to have applied them already.
    """
    if not negative_response(row):
        return None
    company, poc = row_identity(row)
    suggestion = ""
    if mapping_lookup is not None:
        try:
            candidates = mapping_lookup(company) or []
        except Exception:
            log.debug("[cadence] mapping lookup failed for %r", company, exc_info=True)
            candidates = []
        names = [str(c).strip() for c in candidates if str(c).strip()][:3]
        if names:
            suggestion = f" Mapped alternatives: {', '.join(names)}."
    if not suggestion:
        suggestion = " No mapped alternative on file — worth a look at the org yourself."
    return _item(
        row, rule=RULE_TRY_ANOTHER_POC, today=today,
        text=(
            f"{describe_row(row)} — {poc or 'the PoC'} said no. Try a different PoC at "
            f"{company}.{suggestion}"
        ),
        detail=f"Response is {_text(row, 'response')!r}.",
    )


def rule_post_meeting(row: dict, *, today: date) -> Optional[dict]:
    """(h) Meeting happened, and BOTH Assets Shared and Next Steps are blank.

    Both, not either: a meeting with next steps but no assets is somebody doing
    their job in a different order, and flagging it would be noise.
    """
    age = days_since(row.get("meeting_date"), today)
    if age is None or age <= 0:
        return None
    if _text(row, "assets_shared") or _text(row, "next_steps"):
        return None
    return _item(
        row, rule=RULE_POST_MEETING, today=today, age_days=age,
        text=(
            f"{describe_row(row)} — met {age}d ago, no assets shared and no next steps. "
            f"Send the assets and write the next step."
        ),
        detail=f"Meeting Date is {_text(row, 'meeting_date')!r}; both Assets Shared and "
               f"Next Steps are empty.",
    )


def rule_nextstep_stall(
    row: dict, *, today: date, stall_days: Optional[Callable[[dict], int]] = None
) -> Optional[dict]:
    """(i) Next Steps present but unchanged for n days.

    `stall_days(row)` supplies the days-unchanged count, because the sheet has
    no history: it comes from the bot's own `nextstep_state` table. Without that
    callable this rule cannot fire, which is the correct behaviour for a dry run
    with no database.
    """
    text = _text(row, "next_steps")
    if not text or stall_days is None:
        return None
    try:
        unchanged = int(stall_days(row) or 0)
    except Exception:
        log.debug("[cadence] next-step clock failed for %r", row.get("company"), exc_info=True)
        return None
    if unchanged < max(1, config.NEXTSTEP_STALL_DAYS):
        return None
    short = text if len(text) <= 60 else text[:57] + "…"
    return _item(
        row, rule=RULE_NEXTSTEP_STALL, today=today, age_days=unchanged,
        text=(
            f"{describe_row(row)} — next step \"{short}\" unchanged for {unchanged}d. "
            f"Move it or update the tracker."
        ),
        detail=f"Next Steps has read the same since {unchanged} day(s) ago; threshold is "
               f"{config.NEXTSTEP_STALL_DAYS}d.",
    )


def rule_meeting_soon(row: dict, *, today: date) -> Optional[dict]:
    """A meeting inside MEETING_PREP_DAYS. URGENT, and what earns a prep brief.

    Not one of Vaishnavi's nine — it comes from the meeting decision that a
    meeting in the window ranks urgent — so it is kept separate and labelled
    "prep" rather than a letter.
    """
    meeting = dl.parse_date(row.get("meeting_date"))
    if meeting is None:
        return None
    days_away = (meeting - today).days
    if days_away < 0 or days_away > max(0, config.MEETING_PREP_DAYS):
        return None
    when = "today" if days_away == 0 else ("tomorrow" if days_away == 1 else f"in {days_away}d")
    item = _item(
        row, rule=RULE_MEETING_SOON, today=today,
        text=f"{describe_row(row)} — meeting {when} ({dl.format_date(meeting)}). Prep it.",
        detail=f"Meeting Date is {dl.iso(meeting)}, {days_away}d away; the prep window is "
               f"{config.MEETING_PREP_DAYS}d.",
    )
    item["meeting_date"] = dl.iso(meeting)
    item["days_away"] = days_away
    return item


# The order rules are evaluated in. One row can legitimately produce several
# items — a company can be both "connected with no intro" and "meeting
# tomorrow" — and both are true things somebody has to do.
ALL_RULES = (
    RULE_LOCK_MEETING,
    RULE_MEETING_SOON,
    RULE_POST_MEETING,
    RULE_STALE_FOLLOWUP,
    RULE_INTRO_PENDING,
    RULE_START_INTERACTING,
    RULE_ALT_CHANNEL,
    RULE_UNRESPONSIVE,
    RULE_NEXTSTEP_STALL,
    RULE_TRY_ANOTHER_POC,
)


def evaluate_row(
    row: dict, *, today: date,
    mapping_lookup: Optional[Callable[[str], list]] = None,
    stall_days: Optional[Callable[[dict], int]] = None,
) -> list[dict]:
    """Every rule that fires on ONE row. [] for a rejected row — the exclusion
    happens here, before any rule is evaluated, not as a filter afterwards."""
    rejected, marker = is_rejected(row)
    if rejected:
        log.debug(
            "[cadence] excluding %r — marked rejected (%r)", row.get("company"), marker
        )
        return []

    out: list[dict] = []
    for rule in ALL_RULES:
        try:
            if rule == RULE_TRY_ANOTHER_POC:
                item = rule_try_another_poc(row, today=today, mapping_lookup=mapping_lookup)
            elif rule == RULE_NEXTSTEP_STALL:
                item = rule_nextstep_stall(row, today=today, stall_days=stall_days)
            else:
                item = _RULE_FUNCS[rule](row, today=today)
        except Exception:
            log.exception(
                "[cadence] rule %s failed on row %r; skipping that rule for this row",
                rule, row.get("company"),
            )
            continue
        if item:
            out.append(item)
    return out


_RULE_FUNCS = {
    RULE_STALE_FOLLOWUP: rule_stale_followup,
    RULE_INTRO_PENDING: rule_intro_pending,
    RULE_START_INTERACTING: rule_start_interacting,
    RULE_ALT_CHANNEL: rule_alt_channel,
    RULE_UNRESPONSIVE: rule_unresponsive,
    RULE_LOCK_MEETING: rule_lock_meeting,
    RULE_POST_MEETING: rule_post_meeting,
    RULE_MEETING_SOON: rule_meeting_soon,
}


# -- the daily run ------------------------------------------------------------


def sort_key(item: dict) -> tuple:
    """URGENT first, then WAITING, then HELD; within a bucket the oldest thing
    first, then alphabetically so the order is stable day to day."""
    return (
        _BUCKET_RANK.get(item.get("priority"), 9),
        -int(item.get("age_days") or 0),
        str(item.get("company") or "").lower(),
        str(item.get("rule") or ""),
    )


def run(
    rows: list[dict], *, today: Optional[date] = None,
    mapping_lookup: Optional[Callable[[str], list]] = None,
    stall_days: Optional[Callable[[dict], int]] = None,
    limit: Optional[int] = None,
) -> dict:
    """THE DAILY CADENCE. Rows in, a ranked and capped result out.

    Returns:
        {"items": [...],        the capped, ranked items that go in the digest
         "held": [...],         what the cap left out, in full
         "all": [...],          everything, ranked — the "full cadence list"
         "excluded": int,       rows dropped as rejected
         "counts": {rule: n},   how many rows each rule fired on
         "rows": int}           rows considered

    `limit=None` uses DIGEST_MAX_ITEMS. `limit=0` means no cap, which is what the
    on-demand full list asks for.
    """
    today = today or dl.today_ist()
    cap = config.DIGEST_MAX_ITEMS if limit is None else limit

    items: list[dict] = []
    excluded = 0
    for row in rows or []:
        rejected, _marker = is_rejected(row)
        if rejected:
            excluded += 1
            continue
        items.extend(
            evaluate_row(row, today=today, mapping_lookup=mapping_lookup, stall_days=stall_days)
        )

    items.sort(key=sort_key)

    counts: dict[str, int] = {}
    for item in items:
        counts[item["rule"]] = counts.get(item["rule"], 0) + 1

    if cap and len(items) > cap:
        shown, held = items[:cap], items[cap:]
    else:
        shown, held = items, []

    log.info(
        "[cadence] %d row(s) considered, %d excluded as rejected, %d item(s) fired "
        "(%d urgent / %d waiting / %d held-priority); showing %d, %d over the cap of %s",
        len(rows or []), excluded, len(items),
        sum(1 for i in items if i["priority"] == URGENT),
        sum(1 for i in items if i["priority"] == WAITING),
        sum(1 for i in items if i["priority"] == HELD),
        len(shown), len(held), cap or "none",
    )
    for rule in ALL_RULES:
        if counts.get(rule):
            log.info("[cadence]   rule %s (%s): %d row(s)", RULE_LETTERS[rule], rule, counts[rule])

    return {
        "items": shown,
        "held": held,
        "all": items,
        "excluded": excluded,
        "counts": counts,
        "rows": len(rows or []),
        "today": today,
    }


def overflow_line(held: int) -> str:
    """The one closing line that makes the cap honest. "" when nothing was cut."""
    if held <= 0:
        return ""
    return f"{held} more held — ask 'full cadence list' to see all."


def sections(items: list[dict]) -> dict[str, list[dict]]:
    """Ranked items → {digest section key: [items]}, order preserved."""
    out: dict[str, list[dict]] = {key: [] for key in CADENCE_SECTIONS}
    for item in items:
        out.setdefault(item.get("section") or SECTION_UPDATES, []).append(item)
    return out


def meetings_needing_prep(items: list[dict]) -> list[dict]:
    """The RULE_MEETING_SOON items, soonest first — the prep-brief candidates."""
    found = [i for i in items if i.get("rule") == RULE_MEETING_SOON]
    found.sort(key=lambda i: (int(i.get("days_away") or 0), str(i.get("company") or "")))
    return found


def full_list_text(result: dict, *, max_lines: Optional[int] = None) -> str:
    """The UNCAPPED list, as text, for the on-demand "full cadence list" answer.

    Grouped by priority with the rule letter on every line, because the question
    behind the request is usually "what did the digest leave out and why".
    """
    limit = config.CADENCE_FULL_LIST_MAX if max_lines is None else max_lines
    items = result.get("all") or []
    if not items:
        return "Nothing fires today — no cadence items at all."

    lines = [
        f"**Full cadence list — {len(items)} item(s)** "
        f"from {result.get('rows', 0)} row(s); "
        f"{result.get('excluded', 0)} rejected row(s) excluded."
    ]
    for bucket, title in (
        (URGENT, "URGENT"), (WAITING, "WAITING"), (HELD, "HELD"),
    ):
        group = [i for i in items if i.get("priority") == bucket]
        if not group:
            continue
        lines.append("")
        lines.append(f"**{title} ({len(group)})**")
        for item in group:
            if limit and len(lines) >= limit:
                lines.append(f"…truncated at {limit} lines.")
                return "\n".join(lines)
            lines.append(f"• [{item['letter']}] {item['text']}")
    return "\n".join(lines)


# -- dry run ------------------------------------------------------------------


def dry_run_text(result: dict, *, source: str = "", staleness: str = "") -> str:
    """What a dry run prints: which rows each rule fires on TODAY, then the cap.

    This is the operator-facing view — the rule letter, the row, and the REASON
    in terms of the cells that were read, so a firing can be checked against the
    sheet without reading this file.
    """
    today = result.get("today")
    lines = [
        "=" * 78,
        f"CADENCE DRY RUN — {today} (nothing is sent; this is a read-only report)",
        f"source: {source or 'unknown'}"
        + (f"   [{staleness}]" if staleness else ""),
        f"rows considered: {result.get('rows', 0)}   "
        f"rejected+excluded: {result.get('excluded', 0)}   "
        f"items fired: {len(result.get('all') or [])}",
        "=" * 78,
        "",
        "THRESHOLDS IN FORCE (all env-tunable, all placeholders until Vaishnavi tunes them):",
        f"  FOLLOWUP_STALE_DAYS={config.FOLLOWUP_STALE_DAYS}  "
        f"CONNECT_REMINDER_DAYS={config.CONNECT_REMINDER_DAYS}  "
        f"ALT_CHANNEL_AT={config.ALT_CHANNEL_AT}",
        f"  UNRESPONSIVE_AT={config.UNRESPONSIVE_AT}  "
        f"NEXTSTEP_STALL_DAYS={config.NEXTSTEP_STALL_DAYS}  "
        f"MEETING_PREP_DAYS={config.MEETING_PREP_DAYS}  "
        f"DIGEST_MAX_ITEMS={config.DIGEST_MAX_ITEMS}",
        "",
    ]

    counts = result.get("counts") or {}
    lines.append("PER RULE — which rows fired today")
    lines.append("-" * 78)
    for rule in ALL_RULES:
        fired = [i for i in (result.get("all") or []) if i["rule"] == rule]
        letter = RULE_LETTERS[rule]
        lines.append(
            f"({letter}) {rule:<18} {counts.get(rule, 0):>3} row(s)"
            + ("" if fired else "   — nothing today")
        )
        for item in fired:
            where = f"row {item['sheet_row']}" if item.get("sheet_row") else "row ?"
            lines.append(f"      · {item['company']} / {item['poc'] or '—'} [{where}] "
                         f"[{item['priority']}]")
            lines.append(f"        why: {item['detail']}")
    lines.append("")

    shown = result.get("items") or []
    held = result.get("held") or []
    lines.append(f"DIGEST AS IT WOULD POST — cap {config.DIGEST_MAX_ITEMS}, urgent first")
    lines.append("-" * 78)
    by_section = sections(shown)
    for key in CADENCE_SECTIONS:
        group = by_section.get(key) or []
        if not group:
            continue
        lines.append(f"  [{key}] ({len(group)})")
        for item in group:
            lines.append(f"    • [{item['letter']}/{item['priority']}] {item['text']}")
    if not shown:
        lines.append("  (nothing would post today)")
    line = overflow_line(len(held))
    if line:
        lines.append("")
        lines.append(f"  {line}")
        lines.append(f"  (held back: "
                     + ", ".join(f"{i['company']}[{i['letter']}]" for i in held[:20])
                     + ("…" if len(held) > 20 else "") + ")")
    lines.append("")
    return "\n".join(lines)


_DEMO_ROWS = [
    # (a) stale follow-up
    {"_row": 2, "company": "Acme Labs", "poc": "Priya R", "poc_designation": "Head of Data",
     "last_followed_up": "2026-08-10", "response": "", "connected": "Y",
     "intro_date": "2026-07-01", "followups_count": "2", "owner": "Vaishnavi", "_extra": {}},
    # (b) connected, no intro
    {"_row": 3, "company": "Borealis", "poc": "Sam K", "connected": "Yes", "intro_date": "",
     "response": "", "first_contacted": "2026-08-20", "owner": "Kushal", "_extra": {}},
    # (c) contacted, never connected
    {"_row": 4, "company": "Cinder", "poc": "Dev M", "first_contacted": "2026-08-01",
     "connected": "", "response": "", "_extra": {}},
    # (d) 4 follow-ups, silent
    {"_row": 5, "company": "Dynamo", "poc": "Ana T", "followups_count": "4", "response": "",
     "last_followed_up": "2026-08-26", "connected": "Y", "intro_date": "2026-08-02", "_extra": {}},
    # (e) 8 follow-ups, silent
    {"_row": 6, "company": "Everest", "poc": "Rob N", "followups_count": "8", "response": "",
     "last_followed_up": "2026-08-27", "connected": "Y", "intro_date": "2026-07-11", "_extra": {}},
    # (f) positive, no next steps  → URGENT
    {"_row": 7, "company": "Fathom", "poc": "Lea W", "response": "P", "next_steps": "",
     "connected": "Y", "intro_date": "2026-08-05", "_extra": {}},
    # (g) said no → HELD
    {"_row": 8, "company": "Gantry", "poc": "Ivan S", "response": "N", "connected": "Y",
     "intro_date": "2026-08-06", "_extra": {}},
    # (h) meeting done, nothing after it → URGENT
    {"_row": 9, "company": "Halcyon", "poc": "Mei L", "meeting_date": "2026-08-21",
     "assets_shared": "", "next_steps": "", "response": "P", "connected": "Y",
     "intro_date": "2026-08-01", "_extra": {}},
    # meeting_soon → URGENT + a prep brief
    {"_row": 10, "company": "Ionic", "poc": "Tara B", "meeting_date": "2026-08-30",
     "response": "P", "next_steps": "Send pricing", "connected": "Y",
     "intro_date": "2026-08-12", "_extra": {}},
    # rejected → excluded from everything
    {"_row": 11, "company": "Junco", "poc": "Omar F", "response": "N", "status": "Rejected",
     "followups_count": "9", "connected": "Y", "_extra": {}},
]


def _self_test() -> int:
    """`python cadence.py [--demo]` — the dry run.

    With `--demo` it runs the rules against built-in rows, which needs no sheet,
    no network and no database: it tests the RULES. Without it, it reads the real
    master tab and reports what would fire today.
    """
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    demo = "--demo" in sys.argv

    if demo:
        today = date(2026, 8, 28)
        # A stall clock the demo can exercise without a database: Ionic's next
        # step has "read the same" for 12 days.
        def stall(row: dict) -> int:
            return 12 if (row.get("company") == "Ionic") else 0

        result = run(_DEMO_ROWS, today=today, stall_days=stall,
                     mapping_lookup=lambda c: ["Dr Ada Vance (Tier 2, mapped 5 wks ago)"])
        print(dry_run_text(result, source="--demo fixture rows (no sheet read)"))
        print(full_list_text(result))
        expected = {
            "Acme Labs": RULE_STALE_FOLLOWUP,
            "Borealis": RULE_INTRO_PENDING,
            "Cinder": RULE_START_INTERACTING,
            "Dynamo": RULE_ALT_CHANNEL,
            "Everest": RULE_UNRESPONSIVE,
            "Fathom": RULE_LOCK_MEETING,
            "Gantry": RULE_TRY_ANOTHER_POC,
            "Halcyon": RULE_POST_MEETING,
        }
        fired = {(i["company"], i["rule"]) for i in result["all"]}
        missing = [f"{c}:{r}" for c, r in expected.items() if (c, r) not in fired]
        junco = [i for i in result["all"] if i["company"] == "Junco"]
        problems = []
        if missing:
            problems.append(f"rules that did not fire: {', '.join(missing)}")
        if junco:
            problems.append("the rejected row (Junco) produced items — exclusion is broken")
        if not meetings_needing_prep(result["all"]):
            problems.append("no meeting_soon item for Ionic")
        if problems:
            for p in problems:
                print(f"FAIL: {p}")
            return 1
        print("PASS: every rule fired on its row, the rejected row produced nothing.")
        return 0

    try:
        tab, source = gtm_sheet.SHEETS.cadence_tab()
    except gtm_sheet.SheetAccessError as e:
        print(f"Could not read the sheet: {e}\n{getattr(e, 'remedy', '')}")
        return 2
    if tab is None:
        print("No master tab and no outreach tracker in the spreadsheet — nothing to run.")
        return 2
    staleness = ""
    try:
        staleness = gtm_sheet.SHEETS.staleness_note(tab) or ""
    except Exception:
        pass
    result = run(tab.rows)
    print(dry_run_text(result, source=f"{source} tab {tab.title!r}", staleness=staleness))
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

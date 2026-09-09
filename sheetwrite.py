"""WHAT THE BOT MAY WRITE, AND WHEN — the tiers, the fill rule, the parsing.

The bot now writes into the team's REAL sheet. Everything in this file exists to
make that safe enough to be worth doing.

TWO TRIGGERS, AND NOTHING ELSE WRITES:

    (a) a team member REPLIES to one of the bot's own messages;
    (b) a team member @-mentions the bot with an explicit command
        ("update Sahaj's meeting to Friday").

No schedule writes. No inference from a passing remark in the channel. No
"while I was reading the sheet I noticed". A cell changes because a person
addressed the bot and said something that answers what that cell holds.

THREE TIERS:

    REPLY-LOOP     the columns a reply can fill: first-contact type and date,
                   connection date, DM-sent date, Responded?, meeting status,
                   next steps, notes, package sent, prospect status.
    COMMAND-ONLY   closure probability, deal size, deal status Won/Lost. These
                   are commercial judgements. Somebody mentioning a number in
                   passing is not somebody committing it to the sheet, so they
                   need an explicit instruction.
    NEVER          anything in a restricted band (A:I, S:X by default) — the
                   identity block and the formula block. Refused in gtm_sheet
                   since V1 and refused again here, because a rule enforced in
                   one place is a rule one refactor away from being enforced
                   nowhere.

THE BANDS OUTRANK THE TIERS, and that is worth stating because it has a
consequence nobody expects until they hit it: if a COMMAND-ONLY column —
closure probability, deal size, deal status — physically sits inside a
restricted band on the real sheet, then no instruction can write it. The band
check runs first and the bot says so by name ("Closure is one I never write
to").

That is the correct precedence: the bands are a promise about which parts of
the sheet the bot cannot touch at all, and a tier is a rule about who may ask.
A promise that a sufficiently explicit instruction could override would not be
a promise. If the team wants the bot to maintain those columns, they move into
the writable window or the bands change — both of which are a decision somebody
makes on purpose, in .env, rather than something a command quietly wins.

THE FILL RULE:

    An EMPTY cell is filled.
    A NON-EMPTY cell changes ONLY when the reply clearly supersedes it.

That asymmetry is the whole design. Filling a blank costs nothing if it is
wrong — the cell was empty, and the echo shows what went in. Overwriting a value
somebody typed destroys information, so it needs the reply to actually say the
new thing ("moved to Friday", "actually it went out Tuesday"), not merely to
mention the subject. The extractor decides `supersedes`; this module enforces
that nothing without it can overwrite.

RESTRICTED-BAND DATA IN A REPLY IS ACKNOWLEDGED, NEVER WRITTEN. Somebody replies
"sure, her email is a@b.com" — the email column is in the identity block. The bot
says it has that and asks them to drop it in themselves. Refusing silently would
lose the information; writing it would break the one guarantee the bands exist to
make.

TERMINAL STATUSES NEED THE ACTUAL WORDS. "Dead" and "Unresponsive" stop a row for
good in the next-action engine, so the bot will not infer one: the reply has to
contain one of TERMINAL_STATUS_WORDS verbatim.

THIS MODULE IS PURE. Rows and extractions in, plans out. It reads no sheet,
writes no database row and sends nothing.
"""
import logging
import re
from datetime import date, timedelta
from typing import Optional

import config
import deadlines as dl
import gtm_sheet

log = logging.getLogger(__name__)

# -- the tiers ----------------------------------------------------------------

TIER_REPLY = "reply"
TIER_COMMAND = "command"
TIER_NEVER = "never"

# THE REPLY LOOP. What a person answering the bot can put into the sheet without
# having to phrase it as an instruction — the facts a nudge asks about.
REPLY_ROLES: tuple = (
    "first_contact_type",
    "first_contacted",
    "connected",
    "dm_sent_date",
    "response",
    "meeting_status",
    "meeting_date",
    "next_steps",
    "other_updates",
    "package_sent",
    "assets_shared",
    "prospect_stage",
)

# EXPLICIT COMMAND ONLY. Commercial judgements. A number somebody mentions in a
# sentence is not a number they have decided to put in the sheet, and the
# difference between those two things is a forecast nobody agreed to.
COMMAND_ROLES: tuple = (
    "closure",
    "deal_size",
    "deal_status",
)

TRIGGER_REPLY = "reply"
TRIGGER_COMMAND = "command"

# CONTACT DETAILS. These live in the restricted identity band, and people hand
# them over in replies constantly — "sure, her email is a@b.com". They get their
# own handling for one reason: the useful answer is "I have got that, could you
# put it in", and the generic refusal ("I have no rule for that") loses the
# information AND reads as the bot not listening.
#
# Listed by role as well as caught by the band check, because the band check
# needs the column to be MAPPED to fire, and a sheet that has not named its
# email column yet would fall through to the generic path.
CONTACT_ROLES: tuple = ("email", "linkedin", "phone")

CONTACT_LABELS = {
    "email": "email address",
    "linkedin": "LinkedIn link",
    "phone": "phone number",
}

# How each role reads in an echo line. The bot names the COLUMN as the sheet
# names it wherever it can (the header text), and falls back to these.
ROLE_LABELS = {
    "first_contact_type": "first contact type",
    "first_contacted": "first contact date",
    "connected": "connection date",
    "dm_sent_date": "DM sent date",
    "response": "responded",
    "meeting_status": "meeting status",
    "meeting_date": "meeting date",
    "next_steps": "next steps",
    "other_updates": "notes",
    "package_sent": "package sent",
    "assets_shared": "assets shared",
    "prospect_stage": "prospect status",
    "closure": "closure probability",
    "deal_size": "deal size",
    "deal_status": "deal status",
}


def tier(role: str) -> str:
    """Which tier a role sits in. Unknown roles are NEVER.

    Defaulting to NEVER rather than to the reply tier is the safe direction: a
    role this file has not heard of is one nobody has decided the rules for, and
    the failure mode of refusing it is a sentence asking a human to do it.
    """
    if role in REPLY_ROLES:
        return TIER_REPLY
    if role in COMMAND_ROLES:
        return TIER_COMMAND
    return TIER_NEVER


def allowed_for(role: str, trigger: str) -> bool:
    """May this trigger write this role?

    A COMMAND may write both tiers — someone giving an explicit instruction has
    said what they mean. A REPLY may write only the reply loop.
    """
    t = tier(role)
    if t == TIER_NEVER:
        return False
    if t == TIER_COMMAND:
        return trigger == TRIGGER_COMMAND
    return True


def is_terminal_status(value: str) -> bool:
    """Does this prospect/deal value end the row?

    Checked against the same markers the next-action engine stops on, so "the
    bot wrote Dead" and "the bot stopped chasing" can never disagree.
    """
    v = gtm_sheet.normalise_header(value)
    if not v:
        return False
    for marker in (config.CLOSURE_STOP_MARKERS or []):
        key = gtm_sheet.normalise_header(str(marker))
        if key and (v == key or key in v.split() or f" {key} " in f" {v} "):
            return True
    return False


def said_terminal_words(text: str) -> str:
    """The terminal word the person actually typed, or "".

    THE GATE ON ENDING A ROW. "Dead" and "Unresponsive" stop a row permanently,
    so the bot never infers one from tone or from a long silence — somebody has
    to have said it. Matched against the raw reply text, not against the
    extractor's interpretation of it, because the point is what the human wrote.
    """
    low = f" {gtm_sheet.normalise_header(text)} "
    for word in (config.TERMINAL_STATUS_WORDS or []):
        key = gtm_sheet.normalise_header(str(word))
        if key and f" {key} " in low:
            return str(word)
    return ""


# -- the plan -----------------------------------------------------------------


def plan_writes(
    *, tab, row: dict, fields: list, trigger: str, reply_text: str = "",
) -> dict:
    """Decide what actually gets written. Returns a plan; writes nothing.

    `fields` is the extractor's output: a list of
        {"role", "value", "supersedes": bool, "quote": str}

    Returns:
        {"writes":  {role: value}     — what to send to gtm_sheet.write_cells
         "applied": [{role, label, old, new, why}]   — for the echo
         "asks":    [str]             — restricted-band data to ask a human for
         "skipped": [{role, why}]     — everything refused, with the reason
        }

    EVERY REFUSAL IS REPORTED, never swallowed. A bot that silently drops half
    an update is worse than one that writes nothing: the person walks away
    believing the sheet says something it does not.
    """
    out = {"writes": {}, "applied": [], "asks": [], "skipped": []}
    col_to_role = {i: r for r, i in (tab.role_to_col or {}).items()}

    for field in fields or []:
        role = str((field or {}).get("role") or "").strip()
        value = str((field or {}).get("value") or "").strip()
        supersedes = bool((field or {}).get("supersedes"))
        if not role or not value:
            continue

        idx = (tab.role_to_col or {}).get(role)

        # (1) A RESTRICTED COLUMN, OR A CONTACT DETAIL -> acknowledge and ask.
        #     Checked FIRST, before the tier and before the "no such column"
        #     path, because the useful answer here is "I have got that, could
        #     you add it yourself" — refusing generically would lose the
        #     information and read as the bot ignoring them.
        restricted = idx is not None and config.is_restricted_column(idx)
        if restricted or role in CONTACT_ROLES:
            if idx is not None and idx < len(tab.headers):
                header = tab.headers[idx]
            else:
                header = CONTACT_LABELS.get(role, ROLE_LABELS.get(role, role))
            out["asks"].append({
                "role": role, "label": header, "value": value,
                "band": config.restricted_band_label(idx) if restricted else "",
            })
            out["skipped"].append({
                "role": role,
                "why": (f"{header} is one I never write to"
                        + (f" (restricted band {config.restricted_band_label(idx)})"
                           if restricted else "")),
            })
            continue

        # (2) TIER.
        if not allowed_for(role, trigger):
            t = tier(role)
            if t == TIER_COMMAND:
                out["skipped"].append({
                    "role": role,
                    "why": (f"{ROLE_LABELS.get(role, role)} only changes on an explicit "
                            f"instruction, not off a reply"),
                })
            else:
                out["skipped"].append({
                    "role": role,
                    "why": f"I have no rule that lets me write {role!r}",
                })
            continue

        # (3) NO COLUMN.
        if idx is None:
            out["skipped"].append({
                "role": role,
                "why": (f"the Outreach PoCs tab has no column for "
                        f"{ROLE_LABELS.get(role, role)}"),
            })
            continue

        # (4) TERMINAL STATUS needs the words.
        if role in ("prospect_stage", "deal_status") and is_terminal_status(value):
            said = said_terminal_words(reply_text)
            if not said:
                out["skipped"].append({
                    "role": role,
                    "why": (f"setting {ROLE_LABELS.get(role, role)} to {value!r} stops "
                            f"that row for good, and nobody actually said it — tell me "
                            f"plainly and I will"),
                })
                continue

        # (5) THE FILL RULE.
        old = gtm_sheet.clean_cell(row.get(role))
        header = tab.headers[idx] if idx < len(tab.headers) else ROLE_LABELS.get(role, role)
        if old and not supersedes:
            out["skipped"].append({
                "role": role,
                "why": (f"{header} already says {old!r} and your reply did not clearly "
                        f"replace it, so I left it alone"),
            })
            continue
        if old and gtm_sheet.normalise_header(old) == gtm_sheet.normalise_header(value):
            out["skipped"].append({
                "role": role, "why": f"{header} already says that",
            })
            continue

        out["writes"][role] = value
        out["applied"].append({
            "role": role,
            "label": header or ROLE_LABELS.get(role, role),
            "old": old,
            "new": value,
            "why": "filled an empty cell" if not old else "your reply replaced it",
        })

    # (6) THE CEILING. A single sentence should touch one or two cells; an
    #     extraction that wants nine has misread something. Refused WHOLE rather
    #     than truncated — half an update is the one outcome nobody can reason
    #     about afterwards.
    cap = max(1, int(config.SHEET_WRITE_MAX_CELLS))
    if len(out["writes"]) > cap:
        out["skipped"].append({
            "role": "(all)",
            "why": (f"that would change {len(out['writes'])} cells and my ceiling is "
                    f"{cap} — I have not written anything. Tell me one thing at a time "
                    f"and I will get it right"),
        })
        out["writes"] = {}
        out["applied"] = []

    return out


# -- the echo -----------------------------------------------------------------


def echo_line(*, company: str, poc: str, applied: list, asks: list,
              skipped: list, undo_hours: int) -> str:
    """ONE friendly line saying what changed. The voice from sales_policy.md.

    Prose, no labels, no bullets. It always says what it wrote, in the sheet's
    own column names, and always offers the undo — a write nobody was told about
    is a write nobody can catch.
    """
    who = f"{company} · {poc}" if poc else company
    parts: list = []

    if applied:
        changes = " and ".join(
            f"{a['label'].lower()} to {a['new']}"
            + (f" (was {a['old']})" if a["old"] else "")
            for a in applied
        )
        parts.append(f"Noted — I set {who}'s {changes}.")
    if asks:
        got = " and ".join(f"{a['label'].lower()} ({a['value']})" for a in asks)
        parts.append(
            f"I have got the {got} — that column is one I never write to, so could you "
            f"drop it in yourself?"
        )
    ask_roles = {a["role"] for a in (asks or [])}
    for entry in skipped:
        if entry.get("role") in ask_roles:
            continue          # already said, better, in the ask line above
        why = str(entry.get("why") or "").strip()
        if why:
            parts.append(why[0].upper() + why[1:] + ".")
            break
    if applied:
        parts.append(f"Say undo any time in the next {undo_hours}h and I will put it back.")
    return " ".join(p for p in parts if p).strip()


# -- accepting a record-offer -------------------------------------------------

_AFFIRMATIVE = re.compile(
    r"^\s*(?:yes|yep|yeah|yup|sure|ok|okay|please|please do|go ahead|go on|"
    r"do it|mark it|record it|log it|correct|confirmed|sounds good|"
    r"yes please|that is right|thats right)\b[\s.!,]*$",
    re.IGNORECASE,
)

def is_affirmative(text: str) -> bool:
    """Is this reply a bare "yes" to something the bot offered?

    DELIBERATELY STRICT, and anchored at both ends. It only fires on a reply that
    is NOTHING BUT an affirmative — because the offer it accepts is a WRITE, and
    the whole safety of a one-word write is that the words were shown to them
    first. "Yes, but change the date to Friday" must NOT match: that is a
    different instruction, and it goes to the extractor where it belongs.
    """
    return bool(_AFFIRMATIVE.match(str(text or "")))


# -- snooze and scheduled-reminder parsing ------------------------------------

_IN_N_DAYS = re.compile(
    r"\b(?:follow(?:\s|-)?up|come back|check back|remind me|ping me|chase|nudge)\b"
    r"[^.\n]{0,40}?\bin\s+(\d{1,3})\s*(day|days|week|weeks)\b",
    re.IGNORECASE,
)
_ON_THE_NTH = re.compile(
    r"\bon\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.IGNORECASE
)
_WEEKDAY = re.compile(
    r"\b(?:on\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_CLOCK = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b", re.IGNORECASE
)
_ABOUT = re.compile(r"\babout\s+(.+?)(?:[.\n]|$)", re.IGNORECASE)

_WEEKDAY_INDEX = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def parse_snooze(text: str, *, today: Optional[date] = None) -> Optional[dict]:
    """"follow up in 15 days" / "on the 24th" / "Saturday 6pm about X" -> a plan.

    Returns:
        {"kind": "snooze" | "scheduled",
         "date": date,
         "time": str,            free text as they said it ("6pm"), "" if none
         "about": str,           what they said it was about, "" if none
         "quote": str}           the phrase that produced it

    THE KIND IS DECIDED BY WHETHER THEY GAVE A TIME. "Follow up in 15 days" is a
    cadence instruction — it belongs in the snooze table, and the next-action
    engine re-arms the due date to it. "Saturday 6pm" is somebody asking to be
    reminded at a moment, which is the explicitly-scheduled table and the ONE
    thing exempt from the weekend shift. Those are different promises and
    collapsing them would either move somebody's Saturday or turn a soft
    "sometime in a fortnight" into an alarm.

    None when the text does not clearly ask for either. Deliberately narrow: a
    half-recognised date silently entered into the snooze table would make the
    bot go quiet about an account for reasons nobody could reconstruct.
    """
    today = today or dl.today_ist()
    raw = str(text or "")
    if not raw.strip():
        return None

    about = ""
    m_about = _ABOUT.search(raw)
    if m_about:
        about = m_about.group(1).strip()[:200]

    when = ""
    m_clock = _CLOCK.search(raw)
    if m_clock:
        when = m_clock.group(0).strip()

    # "in 15 days" / "in 2 weeks"
    m = _IN_N_DAYS.search(raw)
    if m:
        n = int(m.group(1))
        if m.group(2).lower().startswith("week"):
            n *= 7
        if 1 <= n <= 365:
            return {
                "kind": "scheduled" if when else "snooze",
                "date": today + timedelta(days=n),
                "time": when, "about": about, "quote": m.group(0).strip(),
            }

    # "on the 24th"
    m = _ON_THE_NTH.search(raw)
    if m:
        day_num = int(m.group(1))
        if 1 <= day_num <= 31:
            target = _next_month_day(today, day_num)
            if target:
                return {
                    "kind": "scheduled" if when else "snooze",
                    "date": target, "time": when, "about": about,
                    "quote": m.group(0).strip(),
                }

    # "Saturday", "on Friday"
    m = _WEEKDAY.search(raw)
    if m:
        target = _next_weekday(today, _WEEKDAY_INDEX[m.group(1).lower()])
        return {
            # A NAMED DAY IS A SCHEDULED REMINDER even without a time. Somebody
            # who says "Saturday" has chosen a day, and the weekend exemption
            # exists precisely so that choice survives.
            "kind": "scheduled",
            "date": target, "time": when, "about": about,
            "quote": m.group(0).strip(),
        }

    # A bare explicit date the deadline parser already understands.
    parsed = dl.parse_date(raw.strip())
    if parsed is not None and parsed >= today:
        return {
            "kind": "scheduled" if when else "snooze",
            "date": parsed, "time": when, "about": about, "quote": raw.strip()[:80],
        }
    return None


def _next_month_day(today: date, day_num: int) -> Optional[date]:
    """The next occurrence of "the Nth" — this month if it is still ahead, else
    next month. A day that does not exist in the next month (the 31st of a
    30-day month) rolls to the following one rather than being clamped, because
    "the 31st" clamped to the 30th is a date nobody asked for."""
    for months_ahead in range(0, 4):
        year = today.year + (today.month - 1 + months_ahead) // 12
        month = (today.month - 1 + months_ahead) % 12 + 1
        try:
            candidate = date(year, month, day_num)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


def _next_weekday(today: date, weekday: int) -> date:
    """The next occurrence of a weekday. TODAY DOES NOT COUNT: somebody saying
    "Saturday" on a Saturday means the coming one, not the one they are in."""
    ahead = (weekday - today.weekday()) % 7
    return today + timedelta(days=ahead or 7)


def snooze_confirmation(plan: dict, *, company: str) -> str:
    """One line confirming a snooze or a scheduled reminder. The plan's voice."""
    when = dl.format_date(plan["date"])
    at = f" at {plan['time']}" if plan.get("time") else ""
    about = f" about {plan['about']}" if plan.get("about") else ""
    if plan["kind"] == "scheduled":
        return (
            f"Got it — I will bring {company}{about} back up on {when}{at}. "
            f"Nothing from me on it before then."
        )
    return (
        f"Right, leaving {company} alone until {when}{about}. "
        f"Tell me sooner if something moves."
    )


def _self_test() -> int:
    """`python -m sheetwrite` — the tiers, the fill rule and the parsing."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    today = date(2026, 9, 9)                # a Wednesday
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    print("tiers")
    check("dm_sent_date is reply-loop", tier("dm_sent_date"), TIER_REPLY)
    check("closure is command-only", tier("closure"), TIER_COMMAND)
    check("an unknown role is NEVER", tier("wat"), TIER_NEVER)
    check("a reply may write next_steps", allowed_for("next_steps", TRIGGER_REPLY), True)
    check("a reply may NOT write closure", allowed_for("closure", TRIGGER_REPLY), False)
    check("a command may write closure", allowed_for("closure", TRIGGER_COMMAND), True)

    print("\nterminal words")
    check("'dead' in the text unlocks it", bool(said_terminal_words("they're dead")), True)
    check("silence does not", bool(said_terminal_words("no reply yet")), False)
    check("Dead is a terminal value", is_terminal_status("Dead"), True)
    check("Demo is not", is_terminal_status("Demo"), False)

    print("\nsnooze parsing")
    p = parse_snooze("follow up in 15 days", today=today)
    check("in 15 days -> snooze", (p["kind"], p["date"]), ("snooze", date(2026, 9, 24)))
    p = parse_snooze("come back to this in 2 weeks", today=today)
    check("in 2 weeks", p["date"], date(2026, 9, 23))
    p = parse_snooze("follow up on the 24th", today=today)
    check("on the 24th -> snooze", (p["kind"], p["date"]), ("snooze", date(2026, 9, 24)))
    p = parse_snooze("remind me Saturday 6pm about the pilot", today=today)
    check("Saturday 6pm -> scheduled", p["kind"], "scheduled")
    check("...on the coming Saturday", p["date"], date(2026, 9, 12))
    check("...at the time they said", p["time"], "6pm")
    check("...about what they said", p["about"], "the pilot")
    check("nothing to parse -> None", parse_snooze("thanks, done"), None)

    print("\nplan_writes - the fill rule and the gates")
    import time as _time

    # A:I identity (restricted) · J:R the writable window · S:X formulas
    # (restricted). The command-only columns sit INSIDE the window here, which
    # is the arrangement a sheet needs for the bot to be able to maintain them
    # at all — see "THE BANDS OUTRANK THE TIERS" in the module header.
    headers = ["Sr. No.", "Company", "Industry", "PoC", "Designation", "Email",
               "LinkedIn", "Geography", "Source",
               "First Contact Type", "First Contact Date", "Connection Date",
               "DM Sent Date", "Prospect", "Response?", "Next Steps",
               "Closure", "Deal Status",
               "F1", "F2", "F3", "F4", "F5", "F6"]
    values = [headers,
              ["1", "Acme", "Fin", "Ann", "CTO", "", "", "IN", "ref",
               "LinkedIn", "01-09-2026", "05-09-2026", "", "Lead", "", "",
               "", "",
               "", "", "", "", "", ""]]
    tab = gtm_sheet.SHEETS._parse_values("Outreach PoCs", values, read_at=_time.time())
    trow = tab.rows[0]

    def plan(fields, trigger=TRIGGER_REPLY, text=""):
        return plan_writes(tab=tab, row=trow, fields=fields, trigger=trigger,
                           reply_text=text)

    p1 = plan([{"role": "dm_sent_date", "value": "09-09-2026", "supersedes": False}])
    check("an EMPTY cell is filled", p1["writes"], {"dm_sent_date": "09-09-2026"})

    p2 = plan([{"role": "prospect_stage", "value": "Demo", "supersedes": False}])
    check("a NON-empty cell is left alone without supersedes", p2["writes"], {})
    check("...and the reason is reported", bool(p2["skipped"]), True)

    p3 = plan([{"role": "prospect_stage", "value": "Demo", "supersedes": True}])
    check("...but supersedes replaces it", p3["writes"], {"prospect_stage": "Demo"})

    p4 = plan([{"role": "closure", "value": "60%", "supersedes": False}])
    check("a reply cannot set closure", p4["writes"], {})
    p5 = plan([{"role": "closure", "value": "60%", "supersedes": False}],
              trigger=TRIGGER_COMMAND)
    check("...but a command can", p5["writes"], {"closure": "60%"})

    p6 = plan([{"role": "email", "value": "ann@acme.com", "supersedes": False}])
    check("an email is never written", p6["writes"], {})
    check("...it becomes an ASK", [a["role"] for a in p6["asks"]], ["email"])
    check("...naming the sheet own column", p6["asks"][0]["label"], "Email")

    p7 = plan([{"role": "poc", "value": "Someone Else", "supersedes": True}])
    check("a restricted identity column is never written", p7["writes"], {})

    p8 = plan([{"role": "prospect_stage", "value": "Dead", "supersedes": True}],
              text="no reply for weeks")
    check("Dead without the word is refused", p8["writes"], {})
    p9 = plan([{"role": "prospect_stage", "value": "Dead", "supersedes": True}],
              text="call it, they are dead")
    check("...and allowed when they said it", p9["writes"], {"prospect_stage": "Dead"})

    p11 = plan([{"role": "meeting_status", "value": "Done", "supersedes": True}])
    check("a role with no column on this tab is refused, and named",
          (p11["writes"], "no column" in str(p11["skipped"])), ({}, True))

    many = [{"role": r, "value": "x", "supersedes": True}
            for r in ("dm_sent_date", "next_steps", "response",
                      "closure", "deal_status")]
    p10 = plan(many, trigger=TRIGGER_COMMAND)
    check("over the cell ceiling -> nothing at all", p10["writes"], {})
    check("...refused whole, and said so",
          any("ceiling" in str(sk.get("why")) for sk in p10["skipped"]), True)

    print("\necho line")
    line = echo_line(company="Acme", poc="Ann", applied=p1["applied"], asks=[],
                     skipped=[], undo_hours=24)
    check("names the column and the value", "dm sent date" in line.lower(), True)
    check("offers the undo", "undo" in line.lower(), True)
    check("no bullets or headers",
          any(c in line for c in ("**", "- ", '•')), False)

    print("\nconfirmation voice")
    line = snooze_confirmation(
        {"kind": "scheduled", "date": date(2026, 9, 12), "time": "6pm",
         "about": "the pilot"}, company="Acme")
    check("no bullets or headers", any(c in line for c in ("**", "- ", "•")), False)
    check("names the date", "12 Sep" in line, True)

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

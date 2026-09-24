"""SUPPRESS-OR-CONVERT — check whether it already happened, before you nudge.

THE PROBLEM. The next-action engine reads the sheet, and the sheet lags reality
by however long it takes somebody to update it. So the bot is about to ask
"has the DM to Sahaj gone out?" on a morning when the team booked a meeting with
Sahaj yesterday and said so in the channel. That nudge is not merely useless —
it is the specific kind of useless that gets a bot muted, because it proves the
bot is not reading the same room everybody else is in.

THE OBVIOUS FIX IS THE WRONG ONE. Suppressing the nudge leaves the row wrong AND
tells nobody, and a bot that goes quiet is indistinguishable from a bot that has
broken. So evidence does not silence the message — IT CHANGES WHAT THE MESSAGE
IS:

    task    "Vaishnavi — has the DM to Sahaj gone out?"
    offer   "Saw the meeting's set for Friday — want me to mark it on the row?"

The offer closes the loop with one word back, and the word it asks for is one the
reply loop already knows how to apply.

WHERE THE EVIDENCE COMES FROM, both already exposed to the answer path and reused
here rather than re-implemented:
  - `notes.list_notes` / `notes.read_note` — the Drive-synced meeting notes,
    filtered by the same exclude patterns the answer path uses;
  - `query.search_channel_history` — what the team said in the sales channels.
    With no CRM, that IS the record.

THE EVIDENCE IS ALWAYS QUOTED. Every conversion names what it found and where —
the meeting and its date, or the author and theirs. A bot that says "I think this
is done" without saying why is asking to be trusted on a guess, and the first
time it is wrong it will not be trusted again.

EVIDENCE IS A LADDER, NOT A LOOKUP. Evidence at or above a nudge's stage
converts it: a booked meeting supersedes a DM check, a reply supersedes a
follow-up. That is the case that matters most — chasing a DM on the morning
after the meeting was booked is not slightly wrong, it is the thing everyone in
the channel can see is wrong.

IT ONLY EVER LOOKS BACK NOTES_LOOKBACK_DAYS (7). A note from three weeks ago
saying "we'll book something" is not evidence that a meeting exists now, and
treating it as such would offer to record something that never happened.

THIS MODULE IS ALMOST PURE: `find_in_text` and everything under it take strings
and return verdicts. Only `gather` does I/O, and it sends nothing.
"""
import logging
import re
from datetime import date
from typing import Optional

import config
import deadlines as dl
import gtm_sheet
import nextaction

log = logging.getLogger(__name__)

SOURCE_NOTES = "notes"
SOURCE_CHANNEL = "channel"

# WHAT COUNTS AS "IT ALREADY HAPPENED", AND WHY IT IS A LADDER.
#
# The naive version matches each nudge against its own past tense: a DM check
# looks for "DM sent". That misses the case that matters most. If the team
# booked a meeting with Sahaj yesterday, then asking "has the DM gone out?"
# today is not slightly wrong, it is embarrassing — the DM obviously landed, and
# everyone in the channel can see the message that proves it.
#
# So evidence is STAGED, and evidence at or above a nudge's stage converts it.
# A booked meeting supersedes a DM check, a progress check and a follow-up. A
# reply supersedes a DM check. Nothing supersedes a quote chase except a sent
# quote, because that is the last rung.
#
# THE PHRASES ARE PAST-TENSE AND CONCRETE, deliberately. "Will send" and "should
# book" are the opposite of evidence — they are the thing the nudge is about —
# and a matcher that caught them would convert exactly the nudges that most need
# to go out as tasks.
STAGE_TOUCHED = 1        # we contacted them
STAGE_REPLIED = 2        # they came back
STAGE_MEETING = 3        # a meeting exists
STAGE_QUOTED = 4         # a quote / package has gone

# (stage, phrases, the role it would fill, how the offer names it)
_EVIDENCE: tuple = (
    (STAGE_QUOTED,
     ("quote sent", "sent the quote", "sent the proposal", "proposal sent",
      "pricing sent", "sent them pricing", "sent the pack", "package sent"),
     "package_sent", "the quote as sent"),
    (STAGE_MEETING,
     ("meeting booked", "booked a meeting", "booked a call", "call booked",
      "meeting is set", "meeting s set", "meeting set", "scheduled a call",
      "we re meeting", "we are meeting", "calendar invite", "invite sent",
      "meeting confirmed", "call scheduled"),
     "meeting_status", "the meeting as booked"),
    (STAGE_REPLIED,
     ("they replied", "they responded", "got a reply", "came back to us",
      "heard back", "they got back", "replied to us"),
     "response", "them as having replied"),
    (STAGE_TOUCHED,
     ("dm sent", "sent the dm", "dm d", "dmed", "messaged them",
      "sent them a message", "dropped them a note", "reached out",
      "followed up", "follow up sent", "chased them", "pinged them",
      "nudged them", "emailed them", "called them"),
     "dm_sent_date", "the DM as sent"),
)

# WHAT STAGE A NUDGE IS ASKING ABOUT. Evidence at or above this converts it.
#
# KEYED ON THE RULE'S TRIGGER, now that a rule is the type.
#
# A rule absent from this map CANNOT BE CONVERTED and its nudge always goes out
# as a task — which is the right default, and why most of the twelve are absent.
# R1, R2, R3, R11 and R12 are not about a contact at all, so there is no
# "already done" for evidence to find; R9 asks for next steps, which is a
# sentence somebody has to write rather than a fact the sheet can already show.
_ACTION_STAGE: dict = {
    nextaction.R_LI_NO_DM: STAGE_TOUCHED,
    nextaction.R_PROSPECTS: STAGE_TOUCHED,
    nextaction.R_DM_NO_MEETING: STAGE_REPLIED,
    nextaction.R_MEETING_PREP: STAGE_MEETING,
    nextaction.R_CLOSURE_SUPPORT: STAGE_QUOTED,
}

# What the offer proposes to WRITE when the evidence carries no explicit value.
# A date-shaped column gets today's date; a status column gets the plain word.
_DEFAULT_VALUES = {
    "meeting_status": "Booked",
    "response": "Y",
    "package_sent": "Yes",
}


# Words that do not identify an account on their own. A message saying "labs"
# is not a message about Sahaj Labs, and matching on one would convert every
# nudge in the sheet the first time somebody mentioned a lab.
_GENERIC_TOKENS = {
    "labs", "lab", "inc", "ltd", "limited", "llc", "plc", "corp", "corporation",
    "co", "company", "group", "holdings", "technologies", "technology", "tech",
    "systems", "solutions", "software", "ai", "the", "and", "of", "international",
    "global", "partners", "ventures", "capital", "studio", "studios", "media",
}


def identifiers(company: str, poc: str = "") -> list:
    """The names a person might actually use for this row, longest first.

    THE SHEET SAYS "Sahaj Labs"; THE CHANNEL SAYS "Sahaj". Matching only on the
    full company string missed exactly the case this feature exists for — the
    first time it was tested against a realistic message it found nothing, and
    the nudge went out the morning after the meeting was booked.

    So the set is: the full company name, its distinctive tokens (4+ characters,
    not a generic corporate suffix), and the PoC's own name and first name. A
    match on ANY of them counts as "this text is about this row".

    Longest first so the reported match is the most specific one available,
    which makes the citation more convincing to whoever reads it.
    """
    out: list = []

    def _add(value):
        key = gtm_sheet.normalise_header(value)
        if key and len(key) >= 3 and key not in out:
            out.append(key)

    _add(company)
    for token in gtm_sheet.normalise_header(company).split():
        if len(token) >= 4 and token not in _GENERIC_TOKENS:
            _add(token)
    _add(poc)
    first = gtm_sheet.normalise_header(poc).split()
    if first and len(first[0]) >= 3:
        _add(first[0])

    out.sort(key=len, reverse=True)
    return out


def stage_of(action_type: str) -> int:
    """Which stage a nudge is asking about, or 0 when it cannot be converted."""
    return int(_ACTION_STAGE.get(action_type, 0))


def find_in_text(
    text: str, *, action_type: str, company: str, poc: str = "",
) -> Optional[dict]:
    """Does this text say the nudged thing already happened, for this company?

    BOTH HALVES ARE REQUIRED: the company has to be named AND a past-tense
    signal at or above the nudge's stage has to be present, in the same text.
    Either alone is worthless — "meeting booked" in a note about a different
    account, or the company's name in a sentence about something else.

    THE HIGHEST-STAGE MATCH WINS. A note saying "DM sent, meeting booked" against
    a DM check offers to record the MEETING, because that is the more advanced
    and more useful fact — and recording the DM while the meeting goes unrecorded
    would leave the row wrong in the way that matters.

    Returns {"stage", "phrase", "quote", "role", "offer_phrase"} or None.
    """
    needed = stage_of(action_type)
    if not needed:
        return None

    body = str(text or "")
    if not body.strip():
        return None
    low = gtm_sheet.normalise_header(body)
    names = identifiers(company, poc)
    matched_name = next((n for n in names if n and n in low), "")
    if not matched_name:
        return None

    for stage, phrases, role, offer_phrase in _EVIDENCE:
        if stage < needed:
            continue
        for phrase in phrases:
            key = gtm_sheet.normalise_header(phrase)
            if not key or key not in low:
                continue
            return {
                "stage": stage,
                "phrase": phrase,
                "matched_name": matched_name,
                "quote": _sentence_around(body, phrase),
                "role": role,
                "offer_phrase": offer_phrase,
            }
    return None


def _sentence_around(text: str, phrase: str) -> str:
    """The sentence the phrase appears in, trimmed to something quotable.

    Quoting the WHOLE note would bury the evidence; quoting five words would make
    it unverifiable. A sentence is the unit a person can check at a glance.
    """
    body = " ".join(str(text or "").split())
    pieces = re.split(r"(?<=[.!?])\s+|\n+", body)
    key = gtm_sheet.normalise_header(phrase)
    for piece in pieces:
        if key and key in gtm_sheet.normalise_header(piece):
            return piece.strip()[:240]
    return body[:240]


def gather(
    *, action_type: str, company: str, poc: str = "", notes_module,
    history: Optional[list] = None, today: Optional[date] = None,
) -> Optional[dict]:
    """Look for evidence in the notes and the channel history. I/O, no sending.

    `history` is `query.search_channel_history(...)["matches"]` — passed IN
    rather than fetched here so this stays testable without a Discord client and
    so one search can serve a whole batch of groups.

    THE NOTES ARE CHECKED FIRST. A meeting note is a deliberate record somebody
    wrote up; a channel line is a passing remark. When both say the same thing
    the note is the better citation, and when they disagree the note is the one
    to trust.
    """
    today = today or dl.today_ist()
    days = max(1, int(config.NOTES_LOOKBACK_DAYS))

    try:
        recent = notes_module.list_notes(days=days) or []
    except Exception:
        log.exception("[convert] could not list the meeting notes; skipping that source")
        recent = []

    for meta in recent:
        try:
            note = notes_module.read_note(date=meta.get("date"), label=meta.get("label"))
        except Exception:
            log.debug("[convert] could not read note %r", meta.get("path"), exc_info=True)
            continue
        if not note:
            continue
        blob = " ".join(
            str(part) for part in (
                note.get("summary") or "",
                " ".join(note.get("decisions") or []),
                " ".join(str(n.get("task") or "") for n in (note.get("next_steps") or [])),
            )
        )
        hit = find_in_text(blob, action_type=action_type, company=company, poc=poc)
        if hit:
            label = str(note.get("label") or note.get("title") or "a meeting")
            when = str(note.get("date") or meta.get("date") or "")
            hit.update({
                "source": SOURCE_NOTES,
                "citation": f"{label}, {_pretty(when)}".strip(", "),
            })
            return hit

    for msg in (history or []):
        hit = find_in_text(
            str((msg or {}).get("text") or (msg or {}).get("content") or ""),
            action_type=action_type, company=company, poc=poc,
        )
        if hit:
            who = str((msg or {}).get("author") or "someone")
            when = str((msg or {}).get("date") or (msg or {}).get("timestamp") or "")
            hit.update({
                "source": SOURCE_CHANNEL,
                "citation": f"{who} in channel{', ' + _pretty(when) if when else ''}",
            })
            return hit
    return None


def _pretty(value) -> str:
    """A date string as "Fri 12 Sep", or whatever it was if unparseable."""
    parsed = dl.parse_date(str(value or "")[:10])
    return dl.format_date(parsed) if parsed else str(value or "")[:16]


def proposed_fields(hit: dict, *, today: Optional[date] = None) -> list:
    """What the offer would write, in `sheetwrite.plan_writes` shape.

    `supersedes` is deliberately FALSE. The evidence says the thing happened; it
    does not say the cell somebody already filled in is wrong. So an offer can
    fill a blank and never overwrite — which is the same rule the reply loop
    lives by, and the reason a wrong conversion costs a sentence rather than
    somebody's data.
    """
    today = today or dl.today_ist()
    role = str((hit or {}).get("role") or "")
    if not role:
        return []
    value = _DEFAULT_VALUES.get(role) or today.strftime("%d-%m-%Y")
    return [{
        "role": role, "value": value, "supersedes": False,
        "quote": str((hit or {}).get("quote") or "")[:200],
    }]


def offer_text(*, company: str, poc: str, hit: dict, address: str = "") -> str:
    """The record-offer, in the plan's voice. One thought, an easy out.

    It NAMES THE EVIDENCE. "Saw the meeting's set for Friday" is checkable;
    "I think this is handled" is not, and the difference is whether anybody
    believes the second one after the first time it is wrong.
    """
    who = f"{address} — " if address else ""
    subject = f"{company} · {poc}" if poc else company
    quote = str(hit.get("quote") or "").strip()
    citation = str(hit.get("citation") or "").strip()
    seen = quote[:140] + ("…" if len(quote) > 140 else "")
    return (
        f"{who}looks like {subject} is already handled — {seen}"
        + (f" ({citation})" if citation else "")
        + f". Want me to mark {hit.get('offer_phrase', 'it')} on the row? "
        f"Just say yes, or ignore me if I have got it wrong."
    )

def _self_test() -> int:
    """`python -m evidence` — the ladder, the identifiers and the offer."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    def stage(text, action, company, poc=""):
        hit = find_in_text(text, action_type=action, company=company, poc=poc)
        return hit["stage"] if hit else None

    print("the ladder")
    check("a booked meeting converts a DM check",
          stage("meeting booked with Sahaj Friday", nextaction.R_LI_NO_DM, "Sahaj Labs", "Sahaj"),
          STAGE_MEETING)
    check("...and a follow-up",
          stage("meeting booked with Sahaj", nextaction.R_PROSPECTS, "Sahaj Labs", "Sahaj"),
          STAGE_MEETING)
    check("a DM sent converts a DM check",
          stage("dm sent to Sahaj this morning", nextaction.R_LI_NO_DM, "Sahaj Labs", "Sahaj"),
          STAGE_TOUCHED)
    check("but a DM sent does NOT convert a meeting proposal",
          stage("dm sent to Sahaj", nextaction.R_MEETING_PREP, "Sahaj Labs", "Sahaj"),
          None)
    check("a quote sent converts a quote chase",
          stage("quote sent to Acme yesterday", nextaction.R_CLOSURE_SUPPORT, "Acme"),
          STAGE_QUOTED)
    check("the HIGHEST stage present wins",
          stage("dm sent, then meeting booked with Acme", nextaction.R_LI_NO_DM, "Acme"),
          STAGE_MEETING)

    print(chr(10) + "what is NOT evidence")
    check("future tense", stage("will send the DM to Sahaj tomorrow",
                                nextaction.R_LI_NO_DM, "Sahaj Labs", "Sahaj"), None)
    check("a different account", stage("meeting booked with Globex",
                                       nextaction.R_LI_NO_DM, "Sahaj Labs", "Sahaj"), None)
    check("the right words, no account named",
          stage("meeting booked, finally", nextaction.R_LI_NO_DM, "Sahaj Labs", "Sahaj"), None)
    # A RULE ABSENT FROM _ACTION_STAGE CAN NEVER BE CONVERTED, and most of the
    # twelve are absent on purpose. These two stand in for the whole class.
    check("the news sweep can never be converted",
          stage("meeting booked with Acme", nextaction.R_AI_NEWS, "Acme"), None)
    check("a package chase can never be converted",
          stage("meeting booked with Acme", nextaction.R_PACKAGES, "Acme"), None)
    check("R9's ask for next steps can never be converted",
          stage("meeting booked with Acme", nextaction.R_MEETING_FOLLOWUP, "Acme"), None)
    check("empty text", stage("", nextaction.R_LI_NO_DM, "Acme"), None)

    print(chr(10) + "identifiers — how people actually name an account")
    check("the sheet's full name and the short one",
          identifiers("Sahaj Labs", "Sahaj"), ["sahaj labs", "sahaj"])
    check("generic suffixes are not identifiers",
          "labs" in identifiers("Sahaj Labs", ""), False)
    check("a PoC's first name counts",
          "ann" in identifiers("Acme Technologies", "Ann Patel"), True)
    check("'Sahaj' alone matches the row for 'Sahaj Labs'",
          bool(find_in_text("meeting booked with Sahaj", action_type=nextaction.R_LI_NO_DM,
                            company="Sahaj Labs", poc="Sahaj")), True)

    print(chr(10) + "the offer")
    hit = find_in_text("meeting booked with Sahaj Friday 3pm",
                       action_type=nextaction.R_LI_NO_DM, company="Sahaj Labs", poc="Sahaj")
    fields = proposed_fields(hit)
    check("it proposes the evidence's own column",
          [f["role"] for f in fields], ["meeting_status"])
    check("...and never overwrites", fields[0]["supersedes"], False)
    text = offer_text(company="Sahaj Labs", poc="Sahaj", hit=hit, address="Vaishnavi")
    check("the offer quotes its evidence", "meeting booked with Sahaj" in text, True)
    check("...and asks rather than tells", "Want me to mark" in text, True)
    check("...and gives an out", "ignore me if I have got it wrong" in text, True)
    check("no bullets or headers", any(c in text for c in ("**", "- ", chr(0x2022))), False)

    print(chr(10) + f"{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

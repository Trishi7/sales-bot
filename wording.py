"""THE FIXED LINES SALEY SAYS WHEN IT ANSWERS — in one place.

WHY ONE PLACE. A fixed line is the bot's voice with no model to soften it: the
same words, every time, to whoever is reading. They used to sit inline in
bot.py, persona.py and llm.py, a hundred screens apart, so nobody could read
them side by side, and they drifted: one said "I could not", the next "I
couldn't"; one promised a look through "the sheet and the notes" on a question
that touched neither. Here they can be read top to bottom the way the channel
hears them, and a test can hold every one to the same rules.

THE REGISTER, for every fixed line a teammate reads:
  R-a  Contractions. "I couldn't", "I'll", "it's": never "I could not", "I
       will", "I have not", "I am", "it is", "do not". (A refusal may keep
       "I won't" / "I will not" either way: it is meant to land.)
  R-b  No "(s)". `plural(3, "day")` is "3 days"; `plural(1, "day")` is "1 day".
  R-c  No setting name in a line the team reads in the normal flow. Setting
       names belong in operator lines: the cost report, the test commands, the
       sheet status, "Web search: OFF right now (…)".
  R-d  No banned opener (replyguard: "Sure!", "Here's …", "Based on …").
  R-e  A fixed line never claims a check, a source or a search the code did
       not just do. "I'm going through the sheet and the notes" was sent on
       questions routed to neither.

NO MODEL, NO CONFIG, NO IMPORTS. Callers pass what a line needs. Nothing here
decides WHEN a line is said; it only says how.

WHAT IS NOT HERE, and where it lives instead (each family has its own reasons
to stay with its code, mostly three-wording sets picked by tone.pick or
voice.choose and pinned word for word by its verify script):
  drip.py              the R1–R12 asks, closes, headings, list furniture
                       (R13's lines are HERE, the one drip family that is: they
                       are fixed, never composed by the model, and the reply
                       lines that follow them will quote the same cell names)
  approvals.py         proposal questions, pending / nudge / dropped lines, and
                       ROW_ADD_OFFER (NFT2-1065; awaiting sign-off)
  focus.py             focus status, confirmation, expiry, refusal
  sheetwrite.py        skip reasons, the write echo, undo and snooze lines
  news.py              quiet-day lines, the news headings
  notes.py             SAY_NOT_CONNECTED / SAY_EMPTY / SAY_UNREACHABLE, dictated
                       word for word (NFT2-1062) and never rewritten here
  events_discovery.py  the event and registration-deadline asks
  deadlines.py         the deadline rules and "shout to change"
  todos.py             the to-do list reply and the sheet announcement
  tracker.py           the Mon/Fri tracker reminder, the funnel footnotes
  websearch.py         people_reply (find_people's verbatim text), "Sources"
  simulation.py, clock.py   the test-channel commands (operator lines)

`python -m wording` checks every line here against R-a, R-b and R-d.
"""


def plural(n, word: str, many: str = "") -> str:
    """"1 day" / "3 days". `many` for a plural that is not word + s. The
    "(s)" hack reads as a form; a person writes the number they mean."""
    try:
        count = int(n)
    except (TypeError, ValueError):
        count = 0
    return f"{n} {word if count == 1 else (many or word + 's')}"


# -- a slow answer -----------------------------------------------------------
#
# THREE OF EACH, picked by persona.interim_line. The web lines are used only
# once a web_search has actually started (bot._answer_with_engine), never
# because of a word in the question: on 7 Oct "any AI news?" got "I'm checking
# the web" and was then answered from the news already collected (R-e). The
# engine lines name no source at all, because the bot does not know which
# ones the model will read.
INTERIM_WEB = (
    "On it — I'm checking the web for this, give me a minute or two.",
    "Looking this up now. Back shortly with what I find.",
    "Give me a couple of minutes — I'm searching for this.",
)
INTERIM_ENGINE = (
    "Give me a moment, I'm looking into that.",
    "One sec — I'm pulling this together.",
    "Let me check. Won't be long.",
)


# -- when something failed on the bot's side ---------------------------------


def model_failure(detail: str) -> str:
    """A model call RAISED. The failure is mine, here is its shape, and your
    message was not the problem (see persona.model_failure_reply for why those
    three and why `detail` is a class name, never a trace)."""
    return (f"Something's down on my side ({detail}), so I couldn't answer that. "
            "Your message was fine. Try me again in a minute.")


BRIEF_FAILED = ("I couldn't write the brief just now. Something failed on my side, "
                "and what I gathered is fine. Try again in a moment.")
BRIEF_EMPTY = ("I gathered the material but nothing came back when I tried to write "
               "it up. That's a failure on my side, not a lack of information about "
               "them.")


# -- when there is nothing to say --------------------------------------------


def greeting(hi: str) -> str:
    """The fallback hello. It offers nothing it would have to look up: the
    notes may not be connected, and this path checks no source (R-e)."""
    return f"{hi}, what do you need?"


def not_followed(hi: str) -> str:
    """The fallback for a message the bot could not place. This path looked
    NOTHING up, so it does not say "I checked …" (R-e); it asks for the one
    thing that would let it look."""
    return (f"{hi}, I couldn't work out what you need there. Give me a company, "
            "a person or a date and I'll look.")


# The engine DID look (tools ran, nothing came back worth saying), so "I
# looked" is true here and only here.
FOUND_NOTHING = ("I looked and couldn't find anything concrete on that. Give me a "
                 "company, a person, or a date and I'll go again.")

# The last chunk of a reply that ran past QUERY_REPLY_MAX_MESSAGES.
TRUNCATED = "…that's as much as fits here. Ask for a narrower slice and I'll send the rest."

# Heads the link list added to a web answer that cited nothing inline.
SOURCES_HEADING = "Sources:"


def time_now(clock: str) -> str:
    return f"It's {clock}."


# -- "what can you do", when the model is unreachable ------------------------


def capability_intro(name: str) -> str:
    """Who the bot is, in three sentences. "One digest a day" was true until
    the drip replaced the digest; a self-description that is out of date is
    the first thing a new teammate reads."""
    return (f"I'm {name}, the sales and marketing chief of staff for this team. "
            "I read the sales channels and the sheets, keep track of what's due, "
            "and answer from what I can actually see. A few short messages a day, "
            "not a stream of pings.")


CONNECTED = "Connected: "
GOING_STALE = "Readable but going stale: "
WAITING_ON = "Still waiting on access to: "
UNTIL_THEN = "Until then I can't answer anything that depends on those."
POLICY_MISSING = "My policy file is also missing, so I'm working from defaults."


# -- the write and approval path ---------------------------------------------

SHEET_UNREADABLE = "I couldn't read the sheet just now."
NO_POCS_TAB = "I can't find the Outreach PoCs tab, so there's nothing for me to write to."
COMPANY_UNCLEAR = "I couldn't tell which company you meant."


def which_company(companies: str) -> str:
    return f"That message was about {companies} — which one do you mean?"


def no_row(company: str, poc: str = "") -> str:
    return f"I don't have a row for {company}" + (f" / {poc}" if poc else "") + "."


def which_person(count: int, company: str, people: str) -> str:
    return (f"There are {count} rows for {company} ({people}) — "
            "which person do you mean?")


def declined(why: str, others=(), what: str = "") -> str:
    """A no on a proposal. When somebody else had already said yes, the reply
    says so and says the sheet is unchanged: the person who said yes must not
    be left thinking it went through.

    `what` is the proposal's label (`proposal_label`), so the line names the
    thing that was declined; "that one" under a busy channel names nothing.
    Without it the two older sentences are returned word for word."""
    others = [str(o) for o in (others or ()) if str(o).strip()]
    what = str(what or "").strip()
    if what and others:
        return (f"Not doing {what}: {why}. ({', '.join(others)} had said yes, "
                "so to be clear — the sheet is unchanged.)")
    if what:
        return f"{_cap(what)} is off — {why}. Nothing has changed in the sheet."
    if others:
        return (f"Not doing that one: {why}. ({', '.join(others)} had said yes, "
                "so to be clear — the sheet is unchanged.)")
    return f"Leaving that one then — {why}. Nothing has changed in the sheet."


# Goes under a write echo while SHEET_WRITES_ENABLED is off. It names no
# setting (R-c): whoever reads the channel needs to know nothing changed, not
# which switch to flip.
WRITES_OFF_NOTE = "(Sheet writing is off right now, so I haven't changed anything.)"


def focus_cleared(value) -> str:
    return f"Cleared the focus on {value} — back to sheet order."


NO_FOCUS = "There was no focus set."


def write_failed(error: str) -> str:
    return f"I couldn't write that: {error}. Nothing has changed."


def nothing_to_undo(hours) -> str:
    return f"I haven't changed anything in the last {hours}h that I can put back."


def undo_failed(error: str) -> str:
    return (f"I couldn't put that back: {error}. The cells are as they were after "
            "my change.")


def undone(company: str, what: str) -> str:
    return f"Done — I put {company}'s {what}."


# -- replies and approvals (NFT2-1063) ---------------------------------------
#
# EVERY LINE HERE NAMES THE THING IT IS ABOUT. On 7 Oct a "sure" got "Will do —
# I'll post these again on Monday": nobody could tell what "these" were, so
# nobody could tell a reminder had been set that no one asked for. A
# confirmation, a refusal and a "which one?" all carry the proposal's label
# (`proposal_label`), never "these", "that one" or "it".
#
# AN ACKNOWLEDGEMENT HAS NO LINE AT ALL: "sure" or "thanks" under a message
# that asked nothing gets a reaction (replies.ACK_EMOJI) and no text.
#
# THE OBJECTIVES LINES SAY NOTHING ABOUT WHEN ANYTHING IS POSTED. Somebody who
# asks for today's objectives wants the objectives; a time, a rule or a
# schedule in the answer is the 6 Oct bug.

# THE "TODAY" ANSWER (8 Oct): four groups, each shown only when it has
# something under it (`today.render`), and one line when none has.
TODAY_MEETINGS = "From today's meetings"
TODAY_DUE = "Due soon"
TODAY_CHANNEL = "In the channel today"
TODAY_ALSO = "Also today"
TODAY_POSTS_LEAD = "My posts today cover"
# A yes to R11's list that picks and chooses by number ("yes to 1 and 2 but not
# 3"). The list is one question, all or none; people can be picked by name.
POC_ADD_ALL_OR_NONE = ("I have not added anyone yet. I can add all of them or none, or just "
                       "the people you name — could you reply to the list again with a plain "
                       "yes, a no, or the names please?")
NOTHING_TODAY = ("Nothing on for today that I can see: no meeting notes, nothing due "
                 "in the next couple of days and nothing to pick up from the channel.")
OBJECTIVES_UNREADABLE = ("I couldn't read what's gone out today, so I can't list it. "
                         "Try me again in a minute.")


def today_unread(parts) -> str:
    """The last line of a "today" answer when a source could not be read. It
    names what is missing rather than letting the answer look complete."""
    parts = [str(p).strip() for p in (parts or ()) if str(p).strip()]
    if not parts:
        return ""
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"I couldn't read {listed} just now, so that part is missing."

EVENTS_REMIND_EMPTY = ("The AI events reminder had no events left on it, so I haven't "
                       "set one.")
EVENTS_REMIND_PAST = ("The day for the AI events reminder has already come, so I "
                      "haven't set one. Ask me for the events list any time.")
POC_LOOKUP_EMPTY = ("The PoC search had no company left on it, so I haven't looked "
                    "anything up.")
# "Sure" to an offer to change the sheet, when the offer did not say what to
# change. Nothing is written on a "sure"; this asks for the change itself.
OFFER_NEEDS_DETAIL = "Tell me what to change and for whom, and I'll put it up for approval."


def _and_list(names) -> str:
    """"A", "A and B", "A, B and C"; "" for none."""
    names = [" ".join(str(n or "").split()) for n in (names or ())]
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _cap(label: str) -> str:
    """The label with its first letter capitalised, to open a sentence. Only
    the first letter: "ada@acme.ai" and "PoC" further in stay as written."""
    label = str(label or "").strip()
    return label[:1].upper() + label[1:]


def clock_12h(clock: str) -> str:
    """"14:00" → "2 PM", "14:30" → "2:30 PM", "09:05" → "9:05 AM". The way a
    person in the channel writes a time. Anything unreadable is returned as it
    came: a wrong time is worse than an oddly written one."""
    try:
        hour, minute = (int(p) for p in str(clock or "").strip().split(":")[:2])
    except (TypeError, ValueError):
        return str(clock or "").strip()
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return str(clock or "").strip()
    half = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12} {half}" if not minute else f"{hour12}:{minute:02d} {half}"


def proposal_label(kind: str, *, company: str = "", poc: str = "", names=(),
                   count: int = 0, when: str = "", email: str = "",
                   tab: str = "") -> str:
    """What one proposal IS, in a few words, for every line that has to name
    it: "the update to Sahaj (Wispr Flow)", "the AI events reminder for Mon 12
    Oct", "the PoC search for Shunya Labs and Acme".

    A NOUN PHRASE WITH NO VERB OF ITS OWN TENSE, so the same label fits "Is
    that a yes to …?", "… stays open until …" and "… was already answered".
    Everything in it comes from the proposal; a kind this does not know is
    "the change I proposed", never a guess at what it was.
    """
    kind = str(kind or "")
    company, poc = str(company or "").strip(), str(poc or "").strip()
    listed = _and_list(names)
    count = int(count or 0) or len([n for n in (names or ()) if str(n).strip()])
    if kind == "cell_update":
        who = f"{poc} ({company})" if poc and company else (poc or company)
        return f"the update to {who}" if who else "the update I proposed"
    if kind == "email_write":
        email = str(email or "").strip()
        if email and poc:
            return f"adding {email} to {poc}'s row"
        if count > 1:
            return f"adding the {count} emails I found to the sheet"
        return "adding the email I found to the sheet"
    if kind == "row_add":
        return f"adding {listed or 'them'} to {str(tab or '').strip() or 'Outreach PoCs'}"
    if kind == "event_append":
        if count > 1:
            return f"adding {count} events to the events tab"
        return f"adding {listed or 'the event'} to the events tab"
    if kind == "event_deadline":
        if count > 1:
            return f"{count} registration deadlines"
        return ("the registration deadline for " + listed) if listed \
            else "the registration deadline"
    if kind == "events_remind":
        when = str(when or "").strip()
        return f"the AI events reminder for {when}" if when else "the AI events reminder"
    if kind == "poc_lookup":
        return f"the PoC search for {listed}" if listed else "the PoC search"
    return "the change I proposed"


def which_proposal(labels) -> str:
    """A bare "yes" with more than one thing open. It names each one, numbered,
    and records nothing: guessing which was meant is how a yes lands on the
    wrong proposal."""
    rows = [f"{i}. {label}" for i, label in enumerate(labels or (), 1)]
    return "\n".join(["Which one do you mean?", *rows,
                      "Reply to that message with yes, or give me the number."])


def confirm_proposal(label: str) -> str:
    """A bare "yes" with ONE thing open that was asked too long ago to be sure
    the yes is about it. It asks, and a yes replied to THIS line is the answer."""
    return (f"Is that a yes to {label}? Say yes in a reply to this and I'll go "
            "ahead.")


def events_remind_set(when: str) -> str:
    """`when` is "Mon 12 Oct at 2 PM". Names what will be posted, and when."""
    return f"I'll post the AI events list again on {when}."


def holding(label: str) -> str:
    """A yes that is not enough yet (an approver other than the final say has
    answered, or the vote is tied). Says what is still open and why."""
    return (f"Noted. {_cap(label)} stays open until someone who can approve it "
            "says yes.")


def nothing_left(label: str) -> str:
    """A yes on a proposal whose items have all gone since it was made."""
    return f"There was nothing left on {label}, so nothing has changed."


def offer_closed(label: str) -> str:
    """A second "yes" under an offer that was already answered or has lapsed.
    Nothing runs twice; the line says so and how to get it again."""
    return (f"{_cap(label)} was already answered, so I haven't done anything new. "
            "Ask me again and I'll set it up.")


def reply_lines_for_check() -> list:
    """Every line of this section, with stand-in values, one per proposal
    kind where the label changes the sentence."""
    labels = [
        proposal_label("cell_update", company="Wispr Flow", poc="Sahaj"),
        proposal_label("cell_update", company="Wispr Flow"),
        proposal_label("email_write", poc="Ada Lovelace", email="ada@acme.ai"),
        proposal_label("email_write", count=3),
        proposal_label("row_add", names=["Janajit Bagchi", "Suryansh Shukla"]),
        proposal_label("event_append", names=["Data Summit"]),
        proposal_label("event_append", count=3),
        proposal_label("event_deadline", names=["Data Summit"]),
        proposal_label("event_deadline", count=3),
        proposal_label("events_remind", when="Mon 12 Oct"),
        proposal_label("poc_lookup", names=["Shunya Labs", "Acme"]),
        proposal_label("something_new"),
    ]
    out = [NOTHING_TODAY, TODAY_MEETINGS, TODAY_DUE, TODAY_CHANNEL, TODAY_ALSO,
           TODAY_POSTS_LEAD + " the deliverables checklist and closure support.",
           today_unread(["my own posts for today"]),
           today_unread(["today's meeting notes", "the channel"]),
           OBJECTIVES_UNREADABLE, EVENTS_REMIND_EMPTY,
           EVENTS_REMIND_PAST, POC_LOOKUP_EMPTY, OFFER_NEEDS_DETAIL,
           which_proposal(labels[:2]), events_remind_set("Mon 12 Oct at 2 PM")]
    for label in labels:
        out += [confirm_proposal(label), holding(label), nothing_left(label),
                offer_closed(label), declined("Sid said no", (), label),
                declined("Sid said no", ["Vaishnavi"], label)]
    return out


# -- the next-step follow-ups (rule 13) --------------------------------------
#
# ONE POST, UP TO FIVE PEOPLE, ONE LINE EACH: who, and the one thing to do in
# the sheet. Saley only reminds here; it never fills these cells, so every line
# is a question or a request and none says "I've updated".
#
# NO RULE NUMBER, NO SCHEDULE TALK, NO SETTING NAME. A line says what the sheet
# shows and what to do about it, never how often the bot will ask again.
#
# THE CELL AND STEP NAMES ARE THE SHEET'S OWN, held once, so a renamed header
# or dropdown option is one edit and the post, the preview and the reply lines
# cannot disagree about what a cell is called.
STEP_RESEARCH = "Research the PoC"
STEP_EMAILS = ("Send email 1", "Send email 2", "Send email 3")
STEP_DM = "Reach by LI DM"
STEP_CALL = "Call the PoC"
CELL_STEP = "Next Steps"
CELL_DM_SENT = "LI DM Sent"
CELL_DM_DATE = "LI DM Date"
CELL_STATUS = "Prospect Status"
STATUS_UNRESPONSIVE = "Unresponsive"
_NTH = {1: "1st", 2: "2nd", 3: "3rd"}

# THE POST IS GROUPED BY ASK (8 Oct). It used to be one line a person, each
# repeating the whole ask ("Next Steps says Send email 1. Has it gone out? If
# so, mark 1st Email Sent and the date." four times over). Now the ask is
# written ONCE and the people it applies to are listed under it:
#
#     **Next steps**
#     @Vaishnavi
#     Next Steps says Send email 1. Has it gone out? If so, mark 1st Email Sent and the date.
#     - Arjun Aryaa (Gnani.ai)
#     - Oliver Shoulson (PolyAI)
#
#     Next Steps says Send email 2. Has it gone out? If so, mark 2nd Email Sent and the date.
#     - Ariya Rastrow (Wisprflow.ai)
#
# THERE IS NO OPENER LINE ANY MORE. The three openers ("A few next steps on
# people we're connected with:" and two more) sat between the tags and the
# list; with each group now opening on its own ask, an opener above them was a
# third line saying "here are next steps" under a heading that already says
# it. The heading and the tags line stay.


def email_sent_cell(n) -> str:
    """"1st Email Sent" — the header of the Nth email's sent cell."""
    return f"{_NTH.get(int(n), str(n))} Email Sent"


def email_date_cell(n) -> str:
    """"1st Email Date"."""
    return f"{_NTH.get(int(n), str(n))} Email Date"


def step_after_email(n) -> str:
    """What Next Steps moves to once email N is out: the next email, and the
    LinkedIn DM after the third."""
    n = int(n)
    return STEP_EMAILS[n] if 1 <= n < len(STEP_EMAILS) else STEP_DM


def next_step_who(name: str, company: str) -> str:
    """"Priya Rao (Acme Labs)", or the company alone for a row with no name."""
    name, company = str(name or "").strip(), str(company or "").strip()
    if name and company:
        return f"{name} ({company})"
    return name or company or "(unnamed row)"


def _also_missing(missing) -> str:
    """" 1st Email Sent and 1st Email Date aren't filled in either." — the
    cells the step in Next Steps implies and the row does not have."""
    cells = [str(m).strip() for m in (missing or ()) if str(m).strip()]
    if not cells:
        return ""
    if len(cells) == 1:
        return f" {cells[0]} isn't filled in either."
    return f" {', '.join(cells[:-1])} and {cells[-1]} aren't filled in either."


def next_step_group(ask: str, *, n: int = 0, set_call: bool = False) -> str:
    """The ask a GROUP of people shares, written once above their names.

    One group per ask and email number (and, for a call, per whether Next
    Steps still has to be set to Call the PoC), so every person under it is
    being asked exactly this. Nothing personal is in it: a date or a missing
    cell belongs to one person and goes on that person's line
    (`next_step_detail`). An unknown `ask` returns "".
    """
    n = int(n or 0)
    if ask == "ask_next":
        return (f"{CELL_STEP} is blank. What's the next step? Usually it's "
                f"{STEP_RESEARCH}.")
    if ask == "researched":
        return (f"{CELL_STEP} says {STEP_RESEARCH}. Have they been researched? "
                f"If so, set {CELL_STEP} to {STEP_EMAILS[0]}.")
    if ask == "email_out":
        return (f"{CELL_STEP} says Send email {n}. Has it gone out? If so, mark "
                f"{email_sent_cell(n)} and the date.")
    if ask == "advance":
        return f"Email {n} has gone out. Time to set {CELL_STEP} to {step_after_email(n)}."
    if ask == "log_date":
        return (f"{email_sent_cell(n)} is marked, but there's no date. Can you log "
                f"when email {n} went out?")
    if ask == "dm_out":
        return (f"{CELL_STEP} says {STEP_DM}. Has the DM gone out? If so, log "
                f"{CELL_DM_SENT} and the date.")
    if ask == "call":
        return ("The LI DM has gone out and there's no meeting yet. Time to call them"
                + (f", and set {CELL_STEP} to {STEP_CALL}." if set_call else "."))
    if ask == "call_no_dm_date":
        return (f"{CELL_STEP} says {STEP_CALL}. Have they been called? Please log "
                f"the {CELL_DM_DATE} too.")
    if ask == "dm_replied":
        return "They replied to the LI DM. Is a meeting being set up?"
    if ask == "unresponsive":
        return (f"Still no meeting after the calls. Please set {CELL_STATUS} to "
                f"{STATUS_UNRESPONSIVE} and I'll stop asking about them.")
    return ""


def next_step_detail(ask: str, *, when: str = "", missing=()) -> str:
    """What is PERSONAL to one person, for after the colon on their line: the
    date their email or DM went out, and any cells their row lacks. "" when
    there is nothing, and then the line is just the name."""
    bits = []
    when = str(when or "").strip()
    if when and ask == "advance":
        bits.append(f"sent {when}")
    elif when and ask == "call":
        bits.append(f"DM sent {when}")
    cells = [str(m).strip() for m in (missing or ()) if str(m).strip()]
    if len(cells) == 1:
        bits.append(f"{cells[0]} isn't filled in either")
    elif cells:
        bits.append(f"{', '.join(cells[:-1])} and {cells[-1]} aren't filled in either")
    return "; ".join(bits)


def next_step_person(who: str, detail: str = "") -> str:
    """"- Arjun Aryaa (Gnani.ai)", or "- Arjun Aryaa (Gnani.ai): sent 5 Oct"."""
    return f"- {who}" + (f": {detail}" if detail else "")


def next_step_post(people) -> list:
    """The lines of the next-steps post under its heading and tags.

    `people` are dicts in the order the rule picked them, each with `who`,
    `ask`, `n`, `when`, `missing`, `set_call`. A GROUP IS OPENED WHERE ITS
    FIRST PERSON STANDS, so the post keeps the rule's order: call reminders
    are picked first and so their group comes first. A blank line between
    groups; none after the last.
    """
    order, groups = [], {}
    for person in people or ():
        ask, n = str(person.get("ask") or ""), int(person.get("n") or 0)
        key = (ask, n, bool(person.get("set_call")) if ask == "call" else False)
        sentence = next_step_group(ask, n=n, set_call=key[2])
        if not sentence:
            continue
        if key not in groups:
            groups[key] = (sentence, [])
            order.append(key)
        groups[key][1].append(next_step_person(
            str(person.get("who") or "").strip() or "(unnamed row)",
            next_step_detail(ask, when=person.get("when") or "",
                             missing=person.get("missing") or ())))
    lines: list = []
    for key in order:
        sentence, names = groups[key]
        if lines:
            lines.append("")
        lines.append(sentence)
        lines.extend(names)
    return lines


def next_step_line(ask: str, *, who: str, step_label: str = "", n: int = 0,
                   when: str = "", missing=(), set_call: bool = False) -> str:
    """One person's ask as a single sentence: what the preview, the log and the
    report show for that person. THE POST ITSELF IS GROUPED (`next_step_post`).

    `ask` is the evaluator's code for what the row needs; `n` the email number;
    `when` a date from the row, already written the way a person would ("5
    Oct"); `missing` the cells to ask for as well. Everything in the line comes
    from the row or from the names above. An unknown `ask` returns "" so a
    caller cannot post a half-formed line.
    """
    n = int(n or 0)
    if ask == "ask_next":
        line = (f"{who}: {CELL_STEP} is blank. What's the next step? Usually it's "
                f"{STEP_RESEARCH}.")
    elif ask == "researched":
        line = (f"{who}: {CELL_STEP} says {STEP_RESEARCH}. Have they been "
                f"researched? If so, set {CELL_STEP} to {STEP_EMAILS[0]}.")
    elif ask == "email_out":
        line = (f"{who}: {CELL_STEP} says Send email {n}. Has it gone out? If so, "
                f"mark {email_sent_cell(n)} and the date.")
    elif ask == "advance":
        line = (f"{who}: email {n} went out on {when}. Time to set {CELL_STEP} to "
                f"{step_after_email(n)}.")
    elif ask == "log_date":
        line = (f"{who}: {email_sent_cell(n)} is marked, but there's no date. Can "
                f"you log when email {n} went out?")
    elif ask == "dm_out":
        line = (f"{who}: {CELL_STEP} says {STEP_DM}. Has the DM gone out? If so, "
                f"log {CELL_DM_SENT} and the date.")
    elif ask == "call":
        line = (f"{who}: the LI DM went out on {when} and there's no meeting yet. "
                "Time to call them"
                + (f", and set {CELL_STEP} to {STEP_CALL}." if set_call else "."))
    elif ask == "call_no_dm_date":
        line = (f"{who}: {CELL_STEP} says {STEP_CALL}. Have they been called? "
                f"Please log the {CELL_DM_DATE} too.")
    elif ask == "dm_replied":
        line = f"{who}: they replied to the LI DM. Is a meeting being set up?"
    elif ask == "unresponsive":
        line = (f"{who}: still no meeting after the calls. Please set {CELL_STATUS} "
                f"to {STATUS_UNRESPONSIVE} and I'll stop asking about them.")
    else:
        return ""
    return line + _also_missing(missing)


# -- the reply to "done" under a next-steps post ------------------------------
#
# SOMEBODY SAID THE STEP IS DONE; THIS ASKS FOR THE ONE SHEET UPDATE THAT STEP
# NEEDS. Saley still writes nothing: the line is a request, built from what the
# post recorded, with the same cell names the post used.
CELL_MEETING_DATE = "Meeting Date"


def next_step_done_line(ask: str, *, short: str, n: int = 0) -> str:
    """The line for one person after a "done". `ask` is what the post asked
    about them; `short` how they are named (a first name, usually); `n` the
    email number. "" for an ask this has no line for."""
    n = int(n or 0)
    if ask == "researched":
        return f"Nice, can you set {CELL_STEP} for {short} to {STEP_EMAILS[0]}?"
    if ask == "email_out":
        return f"Nice, can you mark {email_sent_cell(n)} for {short} and add the date?"
    if ask == "dm_out":
        return f"Nice, can you log {CELL_DM_SENT} and the {CELL_DM_DATE} for {short}?"
    if ask == "call_no_dm_date":
        return f"Nice, can you log the {CELL_DM_DATE} for {short}?"
    if ask == "call":
        return (f"Thanks. If a meeting comes of it, can you add the "
                f"{CELL_MEETING_DATE} for {short}?")
    if ask == "dm_replied":
        return f"Nice, can you add the {CELL_MEETING_DATE} for {short} once it's booked?"
    if ask == "ask_next":
        return f"Thanks. Can you pick the next step for {short} in {CELL_STEP}?"
    if ask in ("advance", "log_date"):
        return f"Thanks, I'll pick {short} up from the sheet."
    if ask == "unresponsive":
        return f"Thanks, I won't ask about {short} again."
    return ""


def next_step_which(people) -> str:
    """"Which one?" with the people it could be, numbered, when a "done" under
    a post naming several people names none of them."""
    lines = ["Which one?"]
    lines += [f"{i}. {who}" for i, who in enumerate(people or (), 1)]
    lines.append("A name or a number is fine.")
    return "\n".join(lines)


NEXT_STEP_ASKS = ("ask_next", "researched", "email_out", "advance", "log_date",
                  "dm_out", "call", "call_no_dm_date", "dm_replied", "unresponsive")


def next_step_lines_for_check() -> list:
    """Every shape a next-steps line can take, with stand-in values."""
    who = next_step_who("Priya Rao", "Acme Labs")
    out = []
    for ask in NEXT_STEP_ASKS:
        for n in ((1, 2, 3) if ask in ("email_out", "advance", "log_date") else (0,)):
            out.append(next_step_line(ask, who=who, n=n, when="5 Oct"))
            # ...and the same ask as the grouped post writes it.
            out.append(next_step_group(ask, n=n))
            out.append(next_step_person(who, next_step_detail(ask, when="5 Oct")))
    out.append(next_step_group("call", set_call=True))
    out.append(next_step_person(who, next_step_detail(
        "email_out", missing=[email_sent_cell(1), email_date_cell(1)])))
    out.append(next_step_line("call", who=who, when="5 Oct", set_call=True))
    out.append(next_step_line("email_out", who=who, n=2,
                              missing=[email_sent_cell(1)]))
    out.append(next_step_line("dm_out", who="Acme Labs",
                              missing=[email_sent_cell(3), email_date_cell(3)]))
    for ask in NEXT_STEP_ASKS:
        for n in ((1, 2, 3) if ask == "email_out" else (0,)):
            out.append(next_step_done_line(ask, short="Priya", n=n))
    out.append(next_step_which(["Priya Rao (Acme Labs)", "Dev Shah (Borealis)"]))
    return out


# -- every line, rendered, for the register check ----------------------------


def all_lines() -> list:
    """Every line this module can produce, with stand-in values — what the
    register test reads. A new line that is not added here is not checked, so
    add it here."""
    return [
        *INTERIM_WEB, *INTERIM_ENGINE,
        model_failure("APIConnectionError"), BRIEF_FAILED, BRIEF_EMPTY,
        greeting("Hi Vaishnavi"), not_followed("Hi"), FOUND_NOTHING, TRUNCATED,
        SOURCES_HEADING, time_now("14:05 IST on Wednesday 7 Oct"),
        capability_intro("Saley"), CONNECTED, GOING_STALE, WAITING_ON, UNTIL_THEN,
        POLICY_MISSING, SHEET_UNREADABLE, NO_POCS_TAB, COMPANY_UNCLEAR,
        which_company("Acme and Globex"), no_row("Acme"), no_row("Acme", "Ada"),
        which_person(2, "Acme", "Ada, Sam"), declined("it's a duplicate"),
        declined("it's a duplicate", ["Sid"]), WRITES_OFF_NOTE,
        focus_cleared("fintech"), NO_FOCUS,
        write_failed("the sheet refused it"), nothing_to_undo(24),
        undo_failed("the sheet refused it"), undone("Acme", "stage back to DM sent"),
        *reply_lines_for_check(),
        *next_step_lines_for_check(),
    ]


# R-a: the uncontracted forms a person in the channel would not type.
_UNCONTRACTED = (
    r"\bI will\b(?! not)", r"\bI have not\b", r"\bI am\b", r"\bI could not\b",
    r"\bI cannot\b", r"\bI did not\b", r"\bI do not\b", r"\bit is\b", r"\bdo not\b",
)
# R-d: the openers replyguard strips from an answer, as one pattern. Written
# out here rather than imported so this module depends on nothing.
_BANNED_START = (
    r"^\s*(?:here is|here['’]s|here are|based on|great question|good question|"
    r"sure|certainly|of course|absolutely)\b"
)


def register_problems(line: str) -> list:
    """What is wrong with one fixed line under R-a, R-b and R-d: a list of
    short reasons, empty when it is fine."""
    import re

    problems = []
    for pattern in _UNCONTRACTED:
        if re.search(pattern, line, re.IGNORECASE):
            problems.append("R-a uncontracted: " + pattern)
    if "(s)" in line:
        problems.append("R-b uses (s)")
    if re.match(_BANNED_START, line, re.IGNORECASE):
        problems.append("R-d opens with a banned opener")
    return problems


def _self_test() -> int:
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    print("plural")
    check("one", plural(1, "day"), "1 day")
    check("many", plural(3, "day"), "3 days")
    check("none", plural(0, "row"), "0 rows")
    check("irregular", plural(2, "person", "people"), "2 people")

    print("\nthe register")
    for line in all_lines():
        check(f"{line[:48]!r}", register_problems(line), [])
    check("the check itself catches a bad line",
          bool(register_problems("Sure! I will look at the 2 row(s).")), True)

    print("\nthe interim lines")
    check("three web lines", len(INTERIM_WEB), 3)
    check("three engine lines", len(INTERIM_ENGINE), 3)
    check("the two sets share none", set(INTERIM_WEB) & set(INTERIM_ENGINE), set())
    check("an engine line names no source",
          [l for l in INTERIM_ENGINE
           if any(w in l.lower() for w in ("sheet", "notes", "web"))], [])

    print("\nreplies and approvals")
    check("a clock time, on the hour", clock_12h("14:00"), "2 PM")
    check("a clock time, past the hour", clock_12h("14:30"), "2:30 PM")
    check("morning", clock_12h("09:05"), "9:05 AM")
    check("midnight and noon", (clock_12h("00:00"), clock_12h("12:00")),
          ("12 AM", "12 PM"))
    check("an unreadable time is left alone", clock_12h("soon"), "soon")
    check("the reminder confirmation names what and when",
          events_remind_set("Mon 12 Oct at 2 PM"),
          "I'll post the AI events list again on Mon 12 Oct at 2 PM.")
    for kind, kwargs, want in [
        ("cell_update", {"company": "Wispr Flow", "poc": "Sahaj"},
         "the update to Sahaj (Wispr Flow)"),
        ("cell_update", {"company": "Wispr Flow"}, "the update to Wispr Flow"),
        ("email_write", {"poc": "Ada Lovelace", "email": "ada@acme.ai"},
         "adding ada@acme.ai to Ada Lovelace's row"),
        ("email_write", {"count": 3}, "adding the 3 emails I found to the sheet"),
        ("row_add", {"names": ["Janajit Bagchi", "Suryansh Shukla"]},
         "adding Janajit Bagchi and Suryansh Shukla to Outreach PoCs"),
        ("event_append", {"names": ["Data Summit"]},
         "adding Data Summit to the events tab"),
        ("event_append", {"count": 3}, "adding 3 events to the events tab"),
        ("event_deadline", {"names": ["Data Summit"]},
         "the registration deadline for Data Summit"),
        ("event_deadline", {"count": 3}, "3 registration deadlines"),
        ("events_remind", {"when": "Mon 12 Oct"},
         "the AI events reminder for Mon 12 Oct"),
        ("poc_lookup", {"names": ["Shunya Labs", "Acme"]},
         "the PoC search for Shunya Labs and Acme"),
    ]:
        check(f"label: {kind} {sorted(kwargs)}", proposal_label(kind, **kwargs), want)
    check("which one, naming each",
          which_proposal(["the update to Acme", "the AI events reminder for Mon 12 Oct"]),
          "Which one do you mean?\n1. the update to Acme\n"
          "2. the AI events reminder for Mon 12 Oct\n"
          "Reply to that message with yes, or give me the number.")
    check("holding names what stays open", holding("the update to Acme"),
          "Noted. The update to Acme stays open until someone who can approve it "
          "says yes.")
    check("a no names what was declined",
          declined("Sid said no", (), "the update to Acme"),
          "The update to Acme is off — Sid said no. Nothing has changed in the "
          "sheet.")
    check("a no without a label is word for word what it was",
          declined("Sid said no"),
          "Leaving that one then — Sid said no. Nothing has changed in the sheet.")
    import re as _re
    vague = _re.compile(r"\b(these|those|that one|this one)\b", _re.IGNORECASE)
    check("no line of this section says 'these' or 'that one'",
          [l for l in reply_lines_for_check() if vague.search(l)], [])
    when = _re.compile(r"\b\d{1,2}:\d\d\b|\b\d{1,2}\s?(am|pm)\b|\bR\d+\b|\brule\b|"
                       r"\bschedule|\bslot\b|\bposts? at\b", _re.IGNORECASE)
    check("the objectives lines say nothing about times, rules or the schedule",
          [l for l in (NOTHING_TODAY, OBJECTIVES_UNREADABLE) if when.search(l)], [])
    check("no line of this section names a setting",
          [l for l in reply_lines_for_check() if _re.search(r"[A-Z]{3,}_[A-Z_]+", l)],
          [])

    print("\nthe next-step lines")
    who = next_step_who("Priya Rao", "Acme Labs")
    check("who", who, "Priya Rao (Acme Labs)")
    check("who, no name", next_step_who("", "Acme Labs"), "Acme Labs")
    check("email 1 not out",
          next_step_line("email_out", who=who, n=1),
          "Priya Rao (Acme Labs): Next Steps says Send email 1. Has it gone out? "
          "If so, mark 1st Email Sent and the date.")
    check("after email 3 comes the DM", step_after_email(3), STEP_DM)
    check("after email 1 comes email 2", step_after_email(1), "Send email 2")
    check("one missing cell",
          next_step_line("dm_replied", who=who, missing=["3rd Email Date"]).endswith(
              " 3rd Email Date isn't filled in either."), True)
    check("two missing cells",
          next_step_line("dm_out", who=who,
                         missing=["3rd Email Sent", "3rd Email Date"]).endswith(
              " 3rd Email Sent and 3rd Email Date aren't filled in either."), True)
    check("an unknown ask is no line", next_step_line("wat", who=who), "")
    check("after a done on research",
          next_step_done_line("researched", short="Priya"),
          "Nice, can you set Next Steps for Priya to Send email 1?")
    check("after a done on email 2",
          next_step_done_line("email_out", short="Priya", n=2),
          "Nice, can you mark 2nd Email Sent for Priya and add the date?")
    check("every ask has a done line",
          [a for a in NEXT_STEP_ASKS if not next_step_done_line(a, short="Priya", n=1)],
          [])
    check("which one",
          next_step_which(["Priya Rao (Acme Labs)", "Dev Shah (Borealis)"]),
          "Which one?\n1. Priya Rao (Acme Labs)\n2. Dev Shah (Borealis)\n"
          "A name or a number is fine.")
    check("every ask has a line",
          [a for a in NEXT_STEP_ASKS if not next_step_line(a, who=who, n=1, when="5 Oct")],
          [])
    import re as _re
    schedule = _re.compile(r"\b(every|daily|days?|weekly|3 ?pm|15:00|R\d+|rule \d+)\b",
                           _re.IGNORECASE)
    check("no line talks about the schedule or a rule number",
          [l for l in next_step_lines_for_check() if schedule.search(l)], [])
    check("no line names a setting",
          [l for l in next_step_lines_for_check() if _re.search(r"[A-Z]{3,}_[A-Z_]+", l)],
          [])

    print("\nALL PASSED" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

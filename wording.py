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
  R-f  No line asks for a vote or a number (NFT2-1090): never "reply with
       yes", "say yes", "yes or no", "choose 1", "give me the number" or
       "vote". Saley asks a question and reads the answer like a person.
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


def part_of_day(hour: int) -> str:
    """"morning" before 12:00 IST, "afternoon" 12:00-17:00, "evening" after —
    and 00:00-04:00 is still the evening before (NFT2-1090, edge 4)."""
    hour = int(hour) % 24
    if 4 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    return "evening"


def greeting(hi: str, part: str = "") -> str:
    """The fallback hello, like meeting your boss on the way in (NFT2-1090):
    "Hi Sid, good morning. How's it going?" No task, no list, no offer — and
    it offers nothing it would have to look up (R-e)."""
    return f"{hi}, good {part}. How's it going?" if part else f"{hi}! How's it going?"


def not_followed(hi: str) -> str:
    """The fallback for a message the bot could not place: one question, no
    blame. This path looked NOTHING up, so it does not say "I checked …" (R-e)."""
    return (f"Umm, I'm not sure I follow, {hi[3:].strip() or 'there'}. Which company, "
            "person or date did you mean?") if hi.lower().startswith("hi") else \
        "Umm, I'm not sure I follow. Which company, person or date did you mean?"


# SMALL TALK, when the model gives nothing usable (NFT2-1090). Saley reacts,
# one line, no work, nothing about the sheet; the second "how's it going" of a
# day gets a different line, and the third only a reaction.
SMALLTALK_LINES = (
    "Good, thanks. Busy week for me, which is the right kind of busy.",
    "All good here, thanks for asking!",
    "Doing well, thanks. Nice to hear from you.",
)

# A reply that is neither a yes nor a no — "maybe", "let me think", "not
# sure". Nothing is decided and the question stays open. THE TICKET'S OWN
# WORDS, so this one line opens with "Of course" (exempt from R-d below).
TAKE_YOUR_TIME = "Of course, take your time."

# "show the prompt for that" (NFT2-1090), replied to a message in the test
# channel.
PROMPT_TEMPLATE = "That post is a template. No prompt was sent."
PROMPT_NOT_MINE = "That one isn't mine."
PROMPT_MISSING = "I don't have the prompt for that one any more."


def two_matches(name: str, options) -> str:
    """A name that matches two rows: one question naming both, never a pick.
    ("Two Daisys on the sheet. Daisy at Acme or Daisy at Globex?")"""
    opts = [str(o) for o in (options or ()) if str(o).strip()]
    first = str(name or "").strip().split()[0] if str(name or "").strip() else "of them"
    many = f"{first}s" if not first.endswith("s") else first
    asked = (", ".join(opts[:-1]) + " or " + opts[-1]) if len(opts) > 1 else \
        (opts[0] if opts else "")
    return f"Two {many} on the sheet. {asked}?" if len(opts) == 2 else \
        f"More than one {first} on the sheet. {asked}?"


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
POC_ADD_ALL_OR_NONE = ("I haven't added anyone yet. I can add all of them or none, or just "
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
    if kind == "pipeline_add":
        return f"adding {listed or 'them'} to the Master Pipeline"
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
    """A bare "yes" with more than one thing open. ONE QUESTION NAMING THEM,
    and nothing recorded: guessing which was meant is how a yes lands on the
    wrong proposal. It never asks for a vote or a number (R-f); the list stays
    numbered so "2" still picks the second, and a name is read too."""
    rows = [f"{i}. {label}" for i, label in enumerate(labels or (), 1)]
    return "\n".join(["Which one did you mean? A name is fine.", *rows])


def confirm_proposal(label: str) -> str:
    """A bare "yes" with ONE thing open that was asked too long ago to be sure
    the yes is about it. It asks, and the answer to THIS line decides."""
    return f"Just to be sure — did you mean {label}? Let me know and I'll go ahead."


def events_add_which(numbers) -> str:
    """A reply named numbers and none of them is a suggested event (they are
    events already on the sheet). Says which numbers can be added."""
    names = [str(int(n)) for n in (numbers or ())]
    which = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return (f"Only {which} {'is a new event' if len(names) == 1 else 'are new events'} I can "
            "add; the others are already on the sheet. Nothing was added.")


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


# -- the next-step reminders (rule 13) ----------------------------------------
#
# A REMINDER, NOT A QUESTION (12 Oct 2026). One post, up to five people. It
# says what needs doing and who for. It never names a cell or a column, never
# gives a date, never asks anybody to fill anything in, and Saley does not
# read replies to it.
#
# NO RULE NUMBER, NO SCHEDULE TALK, NO SETTING NAME.
#
# THE STEP NAMES ARE THE SHEET'S OWN, held once. They are used by the rule's
# log and `cadence preview` lines, never by the post.
STEP_RESEARCH = "Research the PoC"
STEP_EMAILS = ("Send email 1", "Send email 2", "Send email 3")
STEP_DM = "Reach by LI DM"
STEP_CALL = "Call the PoC"
_NTH = {1: "1st", 2: "2nd", 3: "3rd"}
_ORDINAL = {1: "first", 2: "second", 3: "third"}

# THE GROUP LINES, AGREED WORD FOR WORD. THIS IS THE ONLY PLACE THEY LIVE: the
# post, the preview and the checks all read them from here. The key is
# (ask, email number); the ORDER of this table is the order the groups appear
# in a post.
NEXT_STEP_GROUPS = (
    (("replied", 0), "The people below have replied to us! We need to set up a meeting with them:"),
    (("call", 0), "We need to call these PoCs:"),
    (("advance", 1), "We sent the first email to these people over a week ago — time for the second:"),
    (("advance", 2), "We sent the second email to these people over a week ago — time for the third:"),
    (("email_out", 1), "We need to send out the first email to these people:"),
    (("email_out", 2), "We need to send out the second email to these people:"),
    (("email_out", 3), "We need to send out the third email to these people:"),
    (("research", 0), "We need to research these PoCs:"),
    (("blank", 0), "We're connected with these people — next thing is to research them:"),
    (("unresponsive", 0), "No reply after the calls. These look unresponsive:"),
)
_GROUP_LINE = dict(NEXT_STEP_GROUPS)
_GROUP_ORDER = {key: i for i, (key, _line) in enumerate(NEXT_STEP_GROUPS)}

# THE POST:
#
#     **Next steps**
#     @Vaishnavi @Sid
#
#     We need to call these PoCs:
#     - Angelos Perivolaropoulos (ElevenLabs)
#
#     We need to send out the first email to these people:
#     - Arjun Aryaa (Gnani.ai)
#     - Oliver Shoulson (PolyAI)
#
# No opener sentence: the heading already says what this is, and every group
# opens on its own line. A blank line between groups, none after the last.


def email_sent_cell(n) -> str:
    """"1st Email Sent" — the header of the Nth email's sent cell. For the
    rule's own log lines; never in a post."""
    return f"{_NTH.get(int(n), str(n))} Email Sent"


def email_date_cell(n) -> str:
    """"1st Email Date"."""
    return f"{_NTH.get(int(n), str(n))} Email Date"


def next_step_who(name: str, company: str) -> str:
    """"Priya Rao (Acme Labs)", or the company alone for a row with no name."""
    name, company = str(name or "").strip(), str(company or "").strip()
    if name and company:
        return f"{name} ({company})"
    return name or company or "(unnamed row)"


def _group_key(ask: str, n: int = 0) -> tuple:
    ask = str(ask or "")
    return (ask, int(n or 0) if ask in ("advance", "email_out") else 0)


def next_step_group(ask: str, *, n: int = 0) -> str:
    """The agreed line a GROUP of people shares, written once above their
    names. "" for an ask (or an email number) there is no line for, so a
    caller cannot post a half-formed group."""
    return _GROUP_LINE.get(_group_key(ask, n), "")


def next_step_person(who: str) -> str:
    """"- Arjun Aryaa (Gnani.ai)". ONE LINE A PERSON, AND NOTHING ELSE ON IT: no
    date, no cell, no "isn't filled in either"."""
    return f"- {who}"


def next_step_post(people) -> list:
    """The lines of the next-steps post under its heading and tags.

    `people` are dicts with `who`, `ask` and `n`, in the order the rule picked
    them. THE GROUPS APPEAR IN THE AGREED ORDER (`NEXT_STEP_GROUPS`), whatever
    order the people arrive in, and only when somebody is in them; inside a
    group people keep the rule's order. THE DROP LINE IS ALWAYS LAST. A blank
    line between groups; none after the last. [] with nobody to name, and then
    nothing is posted.
    """
    groups: dict = {}
    for person in people or ():
        key = _group_key(person.get("ask"), person.get("n") or 0)
        if key not in _GROUP_LINE:
            continue
        groups.setdefault(key, []).append(next_step_person(
            str(person.get("who") or "").strip() or "(unnamed row)"))
    lines: list = []
    for key in sorted(groups, key=_GROUP_ORDER.get):
        if lines:
            lines.append("")
        lines.append(_GROUP_LINE[key])
        lines.extend(groups[key])
    return lines


def next_step_line(ask: str, *, who: str, n: int = 0, **_ignored) -> str:
    """One person's reminder as a single phrase, for `cadence preview`, the
    log and the report: "Priya Rao (Acme Labs): send the first email". THE POST
    ITSELF IS GROUPED (`next_step_post`). An unknown `ask` returns ""."""
    n = int(n or 0)
    what = {
        "replied": "they replied; set up a meeting",
        "call": "call them",
        "advance": f"the {_ORDINAL.get(n, '')} email went out over a week ago; "
                   f"send the {_ORDINAL.get(n + 1, 'next')} one",
        "email_out": f"send the {_ORDINAL.get(n, '')} email",
        "research": "research them",
        "blank": "connected; research them next",
        "unresponsive": "no reply after the calls; they look unresponsive",
    }.get(str(ask or ""))
    if not what or (ask in ("advance", "email_out") and n not in _ORDINAL):
        return ""
    return f"{who}: {what}"


NEXT_STEP_ASKS = ("replied", "call", "advance", "email_out", "research", "blank",
                  "unresponsive")


def next_step_lines_for_check() -> list:
    """Every line a next-steps post can carry, and every preview phrase, with
    stand-in values."""
    who = next_step_who("Priya Rao", "Acme Labs")
    out = [line for _key, line in NEXT_STEP_GROUPS]
    for ask in NEXT_STEP_ASKS:
        for n in ((1, 2) if ask == "advance" else (1, 2, 3) if ask == "email_out" else (0,)):
            out.append(next_step_line(ask, who=who, n=n))
    out.append(next_step_person(who))
    return [x for x in out if x]

# -- connected, no DM yet (rule 6) --------------------------------------------
#
# ONE POST, FOUR BLOCKS, NUMBERED STRAIGHT THROUGH, each person once:
#
#     **Connected, no DM yet**
#     @Vaishnavi @Sid
#
#     1. Daisy Leigh (PolyAI)
#     2. Ioana Mocanu (Agoda)
#
#     Found missing or alternate emails:
#     3. Joel Georgie J. (ARTPARK) — joel@artpark.in
#
#     These domains can't receive mail:
#     4. Angus Addlesee (PolyAI) — addleseehq.co.uk
#
#     Couldn't find an email for:
#     5. Ishan Mishra (AI Security Institute)
#
#     Would you like me to add these emails to the Outreach PoCs sheet?
#
# THE STRINGS LIVE HERE AND NOWHERE ELSE. What is never in the message: an
# address that is already in the sheet, a column name, a claim that an address
# does or does not exist, and anything about a person whose email is fine
# beyond their name and company.
LI_NO_DM_ALL_CLEAR = "All clear, nobody is waiting on a DM."
LI_NO_DM_FOUND = "Found missing or alternate emails:"
LI_NO_DM_DEAD = "These domains can't receive mail:"
LI_NO_DM_NONE = "Couldn't find an email for:"
LI_NO_DM_ADD_QUESTION = "Would you like me to add these emails to the Outreach PoCs sheet?"
LI_NO_DM_BLOCKS = ("plain", "found", "dead", "none")


def li_no_dm_post(people, *, ask: bool = True) -> list:
    """The lines of R6's post under its heading and tags.

    `people` are dicts with `who`, `block` (one of LI_NO_DM_BLOCKS) and, for
    "found", `emails` (the addresses the bot FOUND), for "dead", `domains`
    (the domains that cannot receive mail: the DOMAIN ONLY, never the
    address). The blocks appear in the agreed order whatever order people
    arrive in; a block with nobody in it does not appear; the numbering runs
    straight through all of them. THE CLOSING QUESTION is there only when the
    "found" block has somebody in it, and `ask` is true (it is false when the
    bot may not write emails, because an offer it cannot honour is not made).
    """
    blocks: dict = {name: [] for name in LI_NO_DM_BLOCKS}
    for person in people or ():
        block = str(person.get("block") or "plain")
        blocks.get(block, blocks["plain"]).append(person)
    lines: list = []
    n = 0
    for name, heading in (("plain", ""), ("found", LI_NO_DM_FOUND), ("dead", LI_NO_DM_DEAD),
                          ("none", LI_NO_DM_NONE)):
        if not blocks[name]:
            continue
        if lines:
            lines.append("")
        if heading:
            lines.append(heading)
        for person in blocks[name]:
            n += 1
            line = f"{n}. {str(person.get('who') or '').strip() or '(unnamed row)'}"
            extra = [str(x).strip() for x in (person.get("emails") if name == "found"
                                              else person.get("domains") if name == "dead"
                                              else ()) or () if str(x).strip()]
            if extra:
                line += " \u2014 " + ", ".join(extra)
            lines.append(line)
    if blocks["found"] and ask and lines:
        lines += ["", LI_NO_DM_ADD_QUESTION]
    return lines


def li_no_dm_lines_for_check() -> list:
    """Every fixed line R6's post can carry."""
    return [LI_NO_DM_ALL_CLEAR, LI_NO_DM_FOUND, LI_NO_DM_DEAD, LI_NO_DM_NONE,
            LI_NO_DM_ADD_QUESTION]


# -- no meeting booked yet (rule 7) --------------------------------------------
#
# THE POST, AGREED 13 Oct 2026. A numbered list, then ONE line under it saying
# what those people have in common. The explanation goes UNDER the list.
#
#     **No meeting booked yet**
#     @Vaishnavi @Sid
#
#     1. Ariya Rastrow (Wisprlow.ai)
#     2. Prof. Karim Ouazzane (London Metropolitan University)
#
#     We sent them a DM more than 3 weeks ago and don't have a meeting set up with them.
#
#     3. Eleonore Lumer (PolyAI) — 24 Oct
#
#     This meeting is set for more than a week after the DM went out.
#
# ONE LINE A PERSON: name and company. No day count, no date, no note, no cell
# name. The second block is the one exception: it shows the Meeting Date,
# because that date is the point of the line. NOTHING IS ASKED, so there is no
# closing question; replies to this post are not read.
DM_NO_MEETING_NONE = ("We sent them a DM more than {span} ago and don't have a meeting set up "
                      "with them.")
DM_NO_MEETING_BOOKED = "This meeting is set for more than {span} after the DM went out."
# The agreed sentence is about ONE meeting. With two or more people in the
# block only the subject changes.
DM_NO_MEETING_BOOKED_MANY = "These meetings are set for more than {span} after the DM went out."
DM_NO_MEETING_BLOCKS = ("none", "booked")
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def days_span(days) -> str:
    """"3 weeks" for 21, "a week" for 7, "10 days" for 10. THE NUMBER IN R7's
    SENTENCE FOLLOWS THE SETTING: weeks when the value divides evenly by 7,
    otherwise days. A post that said seven days when it waited twenty-one
    would simply be wrong."""
    days = max(0, int(days or 0))
    if days and days % 7 == 0:
        return "a week" if days == 7 else f"{days // 7} weeks"
    return "a day" if days == 1 else f"{days} days"


def short_date(iso) -> str:
    """"24 Oct" from "2026-10-24"; "" for anything that is not a date."""
    parts = str(iso or "").strip().split("-")
    try:
        year, month, day = (int(x) for x in parts)
        return f"{day} {_MONTH_ABBR[month - 1]}" if 1 <= day <= 31 and year > 0 else ""
    except (ValueError, IndexError):
        return ""


def dm_no_meeting_post(people, *, days, gap_days) -> list:
    """The lines of R7's post under its heading and tags.

    `people` are dicts with `who`, `block` (one of DM_NO_MEETING_BLOCKS) and,
    for "booked", `meeting_iso`. `days` is DM_NO_MEETING_DAYS and `gap_days`
    DM_NO_MEETING_BOOKED_GAP_DAYS: each block's sentence states its own number.

    THE TWO BLOCKS APPEAR IN THE AGREED ORDER whatever order people arrive in,
    and inside a block people keep the order they came in. The numbering runs
    straight through both. A BLOCK WITH NOBODY IN IT DOES NOT APPEAR,
    INCLUDING ITS SENTENCE. [] with nobody to name, and then nothing is posted.
    """
    blocks: dict = {name: [] for name in DM_NO_MEETING_BLOCKS}
    for person in people or ():
        block = str(person.get("block") or "none")
        blocks.get(block, blocks["none"]).append(person)
    lines: list = []
    n = 0
    for name in DM_NO_MEETING_BLOCKS:
        if not blocks[name]:
            continue
        if lines:
            lines.append("")
        for person in blocks[name]:
            n += 1
            line = f"{n}. {str(person.get('who') or '').strip() or '(unnamed row)'}"
            when = short_date(person.get("meeting_iso")) if name == "booked" else ""
            if when:
                line += " \u2014 " + when
            lines.append(line)
        lines.append("")
        if name == "none":
            lines.append(DM_NO_MEETING_NONE.format(span=days_span(days)))
        else:
            lines.append((DM_NO_MEETING_BOOKED if len(blocks[name]) == 1
                          else DM_NO_MEETING_BOOKED_MANY).format(span=days_span(gap_days)))
    return lines


def dm_no_meeting_lines_for_check() -> list:
    """Every fixed line R7's post can carry, with stand-in values."""
    who = next_step_who("Priya Rao", "Acme Labs")
    return dm_no_meeting_post(
        [{"who": who, "block": "none"}, {"who": who, "block": "booked", "meeting_iso": "2026-10-24"},
         {"who": who, "block": "booked", "meeting_iso": "2026-10-27"}], days=21, gap_days=7) + [
        DM_NO_MEETING_BOOKED.format(span=days_span(10)),
        DM_NO_MEETING_NONE.format(span=days_span(10))]


# -- closure support (rule 10) -------------------------------------------------
#
# THE POST, AGREED 13 Oct 2026:
#
#     **Closure support**
#     @Vaishnavi @Sid
#
#     These deals are in the closure stage:
#     1. Ada Lovelace (Acme AI) — 70% at Quote
#        In the news: [Acme AI raises $40M](url), 8 Oct
#     2. Bo Chen (Borealis) — 65% at Demo
#
#     Can I help with any of these? I can pull together the PoC's background, a note on the company, or a package summary
#
# PERSON FIRST, COMPANY IN BRACKETS, then the percentage and the stage. The
# news line is indented under its deal, one story a deal; no story, no line,
# and nothing ever says "no news". One deal is still a list of one. The opener
# and the close are these two lines and no others (the three-way variants and
# the composed one-deal sentence were deleted).
CLOSURE_OPENER = "These deals are in the closure stage:"
CLOSURE_CLOSE = ("Can I help with any of these? I can pull together the PoC's background, a "
                 "note on the company, or a package summary")
CLOSURE_NEWS = "   In the news: {link}, {when}"
CLOSURE_NEWS_UNDATED = "   In the news: {link}"
# WHEN THE COLUMNS ARE EMPTY. One sentence; the old second one ("Fill them in
# and I'll pick it up next Monday.") is gone. It names whichever column is
# blank on every active row, and it is posted every Monday while that is so.
CLOSURE_EMPTY = "No closure support this week \u2014 {columns} {have} empty values in the GTM sheet."


def closure_empty_notice(columns) -> str:
    """R10's notice, naming the empty column(s): "Prospect Status and Closure
    Prob% have…", or "Closure Prob% has…" when it is the only one."""
    names = [str(c).strip() for c in (columns or ()) if str(c).strip()]
    if not names:
        names = ["Prospect Status", "Closure Prob%"]
    return CLOSURE_EMPTY.format(columns=" and ".join(names),
                                have="have" if len(names) > 1 else "has")


def closure_deal_line(n, who, pct, stage="") -> str:
    """"1. Ada Lovelace (Acme AI) — 70% at Quote"."""
    stage = " ".join(str(stage or "").split())
    detail = f"{int(pct)}% at {stage}" if stage else f"{int(pct)}%"
    return f"{int(n)}. {str(who or '').strip() or '(unnamed row)'} \u2014 {detail}"


def closure_post(deals) -> list:
    """The lines of R10's deals post under its heading and tags, WITHOUT the
    close (the caller adds `CLOSURE_CLOSE` after a blank line).

    `deals` are dicts with `who`, `closure_pct`, `stage` and, when a story was
    found, `news_link` (the headline already as a masked link) and
    `news_date` (an ISO date). They are listed IN THE ORDER GIVEN, every one of
    them: this function has no cap. [] with no deals, and then nothing is
    posted.
    """
    lines: list = []
    n = 0
    for deal in deals or ():
        n += 1
        lines.append(closure_deal_line(n, deal.get("who"), deal.get("closure_pct") or 0,
                                       deal.get("stage")))
        link = str(deal.get("news_link") or "").strip()
        if link:
            when = short_date(deal.get("news_date"))
            lines.append(CLOSURE_NEWS.format(link=link, when=when) if when
                         else CLOSURE_NEWS_UNDATED.format(link=link))
    return ([CLOSURE_OPENER] + lines) if lines else []


def closure_lines_for_check() -> list:
    """Every fixed line R10 can post, with stand-in values."""
    who = next_step_who("Priya Rao", "Acme Labs")
    return closure_post([{"who": who, "closure_pct": 70, "stage": "Quote"}]) + [
        CLOSURE_CLOSE, closure_empty_notice(["Prospect Status", "Closure Prob%"]),
        closure_empty_notice(["Closure Prob%"])]


# -- AI events & summits (rule 3) ----------------------------------------------
#
# THE POST, AGREED 14 Oct 2026. THREE LINES IN ALL, in up to three labelled
# blocks, numbered STRAIGHT THROUGH so a reply can say "2 and 3":
#
#     **AI events & summits**
#     @Vaishnavi @Sid
#
#     We have registered for these events and they are coming up soon:
#     1. Symphony — India's Voice AI Ecosystem (Bengaluru) — Fri 20 Nov
#
#     Registration closes soon for these events:
#     2. Future of AI Summit (London) — Wed 4 to Thu 5 Nov, closes Fri 24 Oct
#
#     Events not in the sheet, but worth looking at:
#     3. [Cypher 2026](url) — Thu 6 Nov, Bengaluru
#
#     Want me to add any of these to the GTM Playbook? And shall I remind you again on Monday?
#
# A block with nobody in it does not appear, heading included. Sheet events
# are NOT linked (their link is in the sheet); a suggestion's NAME is its link.
EVENTS_BLOCK_A = "We have registered for these events and they are coming up soon:"
EVENTS_BLOCK_B = "Registration closes soon for these events:"
EVENTS_BLOCK_C = "Events not in the sheet, but worth looking at:"
# Block C's lead when the sheet gave nothing: it stands alone.
EVENTS_BLOCK_C_ALONE = ("No events on the tab for the next {days} days. A few worth a look that "
                        "we could still register for:")
# The whole post when there is nothing anywhere.
EVENTS_NOTHING = "No events on the tab for the next {days} days, and nothing new worth suggesting."
# THE CLOSING LINE. {when} is "on Monday" or "tomorrow". The add half is there
# only when block C has something; the remind half always.
EVENTS_REMIND = "Shall I remind you again {when}?"
EVENTS_ADD_AND_REMIND = ("Want me to add any of these to the GTM Playbook? And shall I remind "
                         "you again {when}?")
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _ymd(iso):
    """(year, month, day) from "2026-11-20", or None. NO datetime HERE: this
    module imports nothing, so the calendar it needs is four lines of
    arithmetic."""
    try:
        year, month, day = (int(x) for x in str(iso or "").strip().split("-"))
    except (ValueError, TypeError):
        return None
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if year < 1 or not 1 <= month <= 12 or not 1 <= day <= days[month - 1]:
        return None
    return year, month, day


def _weekday(ymd) -> int:
    """0 = Monday. Sakamoto's method."""
    year, month, day = ymd
    offsets = (0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4)
    if month < 3:
        year -= 1
    return (year + year // 4 - year // 100 + year // 400 + offsets[month - 1] + day + 6) % 7


def day_date(iso) -> str:
    """"Fri 20 Nov" from "2026-11-20"; "" for anything that is not a date."""
    d = _ymd(iso)
    return f"{_WEEKDAY_ABBR[_weekday(d)]} {d[2]} {_MONTH_ABBR[d[1] - 1]}" if d else ""


def event_when(start_iso, end_iso="") -> str:
    """"Fri 20 Nov"; a range reads "Tue 20 to Wed 21 Oct", and across two
    months "Fri 30 Oct to Sun 1 Nov"."""
    start, end = _ymd(start_iso), _ymd(end_iso)
    if start is None:
        return ""
    if end is None or end == start:
        return day_date(start_iso)
    if start[:2] == end[:2]:
        return f"{_WEEKDAY_ABBR[_weekday(start)]} {start[2]} to {day_date(end_iso)}"
    return f"{day_date(start_iso)} to {day_date(end_iso)}"


def event_sheet_line(event) -> str:
    """One event from the sheet, without its number.

        Symphony — India's Voice AI Ecosystem (Bengaluru) — Fri 20 Nov
        Future of AI Summit (London) — Wed 4 to Thu 5 Nov, closes Fri 24 Oct
        Yourstory Techsparks (Bengaluru) — Tue 13 to Thu 15 Oct, started Tuesday
        Some Forum (Pune) — 23 September (no year on the sheet — assuming 2027)

    `event` has name, location, start, end (ISO), and when they apply:
    deadline (ISO, block B), started (the weekday it began), year_assumed (the
    year the parser chose) with date_text (the cell as written). NOT A LINK:
    the row's link is already in the sheet.
    """
    name = " ".join(str(event.get("name") or "").split()) or "(unnamed event)"
    location = " ".join(str(event.get("location") or "").split())
    head = f"{name} ({location})" if location else name
    year = int(event.get("year_assumed") or 0)
    if year:
        raw = " ".join(str(event.get("date_text") or "").split())
        when = (f"{raw or event_when(event.get('start'), event.get('end'))} "
                f"(no year on the sheet \u2014 assuming {year})")
    else:
        when = event_when(event.get("start"), event.get("end"))
    line = f"{head} \u2014 {when}"
    closes = day_date(event.get("deadline"))
    if closes:
        line += f", closes {closes}"
    started = str(event.get("started") or "").strip()
    if started:
        line += f", started {started}"
    return line


def event_suggestion_line(link, start_iso, location="") -> str:
    """"[Cypher 2026](<url>) — Thu 6 Nov, Bengaluru". `link` is the event's
    name ALREADY as a masked link; the place is the city the search gave."""
    city = " ".join(str(location or "").split(",")[0].split())
    line = f"{str(link or '').strip()} \u2014 {day_date(start_iso)}"
    return line + (f", {city}" if city else "")


def events_post(registered, closing, suggested, *, days, when="on Monday") -> dict:
    """R3's post under its heading and tags: {"lines": [...], "close": "..."}.

    `registered` and `closing` are the sheet lines for blocks A and B (from
    `event_sheet_line`), `suggested` the block C lines (from
    `event_suggestion_line`), each WITHOUT its number. THE BLOCKS PRINT IN THE
    ORDER A, B, C and the numbering runs straight through them from 1. A block
    with nobody in it does not appear, heading included. With nothing from the
    sheet, block C stands alone under its own lead; with nothing anywhere the
    whole post is the one line and there is no closing line.

    `days` is EVENTS_WINDOW_DAYS and `when` is "on Monday" or "tomorrow".
    """
    blocks = [(EVENTS_BLOCK_A, [x for x in (registered or ()) if str(x).strip()]),
              (EVENTS_BLOCK_B, [x for x in (closing or ()) if str(x).strip()])]
    extra = [x for x in (suggested or ()) if str(x).strip()]
    from_sheet = sum(len(rows) for _h, rows in blocks)
    if not from_sheet and not extra:
        return {"lines": [EVENTS_NOTHING.format(days=int(days))], "close": ""}
    blocks.append((EVENTS_BLOCK_C if from_sheet else EVENTS_BLOCK_C_ALONE.format(days=int(days)),
                   extra))
    lines: list = []
    n = 0
    for heading, rows in blocks:
        if not rows:
            continue
        if lines:
            lines.append("")
        lines.append(heading)
        for row in rows:
            n += 1
            lines.append(f"{n}. {row}")
    close = (EVENTS_ADD_AND_REMIND if extra else EVENTS_REMIND).format(when=str(when or "on Monday"))
    return {"lines": lines, "close": close}


def events_lines_for_check() -> list:
    """Every fixed line R3's post can carry, with stand-in values."""
    sheet = event_sheet_line({"name": "Voice AI Forum", "location": "Bengaluru",
                              "start": "2026-11-20", "end": "2026-11-21"})
    post = events_post([sheet], [sheet], ["Cypher 2026 \u2014 Thu 6 Nov, Bengaluru"], days=14)
    alone = events_post([], [], ["Cypher 2026 \u2014 Thu 6 Nov, Bengaluru"], days=14, when="tomorrow")
    return ([l for l in post["lines"] + alone["lines"] if l] + [post["close"], alone["close"],
            EVENTS_REMIND.format(when="on Monday")] + events_post([], [], [], days=14)["lines"])


# -- sales packages (rule 12) ---------------------------------------------------
#
# THE POST, AGREED WORD FOR WORD ON 12 Oct 2026. Do not reword it and do not
# re-punctuate it; the lower-case "these" is how it was written.
#
#     **Sales packages**
#     @Vaishnavi @Sid
#     these Sales Packages aren't ready yet:
#     1. Evals_Vibe-check
#     2. DPO Pack
#     (+2 more next Thursday.)
#     Could you let me know roughly when each might be ready?
#
# ONE PACKAGE A LINE, NUMBERED, NAMED EXACTLY AS THE SHEET SPELLS IT. No
# status, no per cent, no dash, no trailing words. A list of ONE is still this
# list. The heading and the tags line are the sender's, not part of the block.
PACKAGES_OPENER = "these Sales Packages aren't ready yet:"
PACKAGES_HELD_BACK = "(+{n} more next Thursday.)"
PACKAGES_CLOSE = "Could you let me know roughly when each might be ready?"
# THE PROMISED DAY HAS COME and Ready? is still not Yes. Posted once, by the
# one reminder loop.
PACKAGES_REMINDER = ("{name} was expected to be ready around today, and Ready? on the tab is "
                     "still not Yes. Any update, or shall I check back later?")
# A reply under the post with no day in it that can be read, or a day that has
# already gone. Said once; nothing is guessed and nothing is recorded.
PACKAGES_NO_DATE = ("I couldn't pick a date out of that \u2014 could you give me a day, like "
                    "\"24 Oct\"? I'll check back then.")
# A day with no package it belongs to, under a post that named several.
PACKAGES_WHICH = "Which package is that date for? Name it and I'll note the day."
# The day came for a package that is no longer on the tab (renamed or
# deleted): the reminder is closed and this says so, once.
PACKAGES_GONE = ("{name} isn't on the Sales Packages tab any more, so I've dropped the "
                 "check-back I had for it today.")


def packages_post(packages, *, held_back=0) -> list:
    """The lines of R12's post under its heading and tags, WITHOUT the close
    (the caller adds `PACKAGES_CLOSE`).

    `packages` are the names IN THE ORDER GIVEN, each either a plain name or
    the name already as a masked link (the caller builds it with `links.link`
    when the row's product doc is a real url). `held_back` is how many more
    qualify than the post has room for; its line is there only when that is
    more than none. [] with no packages, and then nothing is posted.
    """
    names = [" ".join(str(p or "").split()) for p in (packages or ())]
    names = [n for n in names if n]
    if not names:
        return []
    lines = [PACKAGES_OPENER] + [f"{i}. {name}" for i, name in enumerate(names, 1)]
    if int(held_back or 0) > 0:
        lines.append(PACKAGES_HELD_BACK.format(n=int(held_back)))
    return lines


def packages_reminder(name) -> str:
    return PACKAGES_REMINDER.format(name=" ".join(str(name or "").split()) or "That package")


def packages_gone(name) -> str:
    return PACKAGES_GONE.format(name=" ".join(str(name or "").split()) or "That package")


def packages_lines_for_check() -> list:
    """Every fixed line R12 can post, with stand-in values."""
    return packages_post(["Evals_Vibe-check", "DPO Pack"], held_back=2) + [
        PACKAGES_CLOSE, packages_reminder("DPO Pack"), PACKAGES_NO_DATE, PACKAGES_WHICH,
        packages_gone("DPO Pack")]


# -- the reconnect follow-up (rule 14, NFT2-1086) --------------------------------
#
# THE POST, AS THE TICKET WROTE IT. Do not reword it and do not re-punctuate
# it; the em dashes are part of it.
#
#     **Time to reconnect**
#
#     Hey @Vaishnavi, @Sid — it's been a while since we last connected with these PoCs, do get in touch when you can:
#
#     1. **Dr. David Gamez** (Middlesex University London)
#     2. **Daisy Leigh** (PolyAI)
#
#     there are 36 more — would you like to look at the rest?
#
# FIVE NAMES AT MOST, numbered, the name in bold, the company in brackets. No
# date and no reason on the line. THE GREETING AND ITS TAGS ARE PART OF THIS
# BLOCK, not the sender's tags line: this post is addressed inside its own
# first sentence. Only the bold heading is the sender's.
#
# THE "MORE" LINE IS THERE ONLY WHEN THERE ARE MORE. A post of five or fewer
# ends at the last name. One person reads in the singular.
RECONNECT_GREETING = ("Hey {tags} \u2014 it's been a while since we last connected with these "
                      "PoCs, do get in touch when you can:")
RECONNECT_GREETING_ONE = ("Hey {tags} \u2014 it's been a while since we last connected with "
                          "this PoC, do get in touch when you can:")
# Who the greeting names when no id is configured to tag (SALES_ALWAYS_TAG_IDS).
RECONNECT_ADDRESSEES = "Vaishnavi, Sid"
# After the company of somebody whose Reconnect Date cell holds something that
# is not a date, so somebody fixes the cell.
RECONNECT_UNCLEAR = "(reconnect date unclear)"
RECONNECT_MORE = "there are {n} more \u2014 would you like to look at the rest?"
RECONNECT_MORE_ONE = "there is 1 more \u2014 would you like to look at them too?"
# Above the full list a "yes" under the post gets.
RECONNECT_REST = "everyone due to reconnect today:"


def reconnect_line(name, company, *, unclear=False) -> str:
    """One person as the post shows them: "**Name** (Company)", and the
    unclear flag after the company when their Reconnect Date could not be
    read. No number: the caller numbers the list."""
    name = " ".join(str(name or "").split())
    company = " ".join(str(company or "").split())
    return f"**{name}** ({company})" + (f" {RECONNECT_UNCLEAR}" if unclear else "")


def _reconnect_numbered(people) -> list:
    return [f"{i}. {reconnect_line(p.get('name'), p.get('company'), unclear=p.get('unclear'))}"
            for i, p in enumerate(people, 1)]


def reconnect_post(people, *, more=0, tags="") -> list:
    """The lines of R14's post under its heading, WITHOUT the close (the caller
    adds `reconnect_more(more)` when there is one).

    `people` are the ones the post NAMES, in order, each {"name", "company",
    "unclear"}. `more` is how many others are due today and not named; it is
    not printed here, but one name with more behind it is still "these PoCs".
    `tags` is who the greeting addresses, already as mention tokens. [] with
    nobody, and then nothing is posted: there is no "nobody today" line.
    """
    people = [p for p in (people or ()) if str(p.get("name") or "").strip()
              and str(p.get("company") or "").strip()]
    if not people:
        return []
    alone = len(people) == 1 and int(more or 0) <= 0
    greeting = (RECONNECT_GREETING_ONE if alone else RECONNECT_GREETING).format(
        tags=" ".join(str(tags or "").split()) or RECONNECT_ADDRESSEES)
    return [greeting, ""] + _reconnect_numbered(people)


def reconnect_more(n) -> str:
    """The closing question, or "" when nobody is left out."""
    n = int(n or 0)
    if n <= 0:
        return ""
    return RECONNECT_MORE_ONE if n == 1 else RECONNECT_MORE.format(n=n)


def reconnect_rest(people) -> list:
    """What a "yes" under the post gets: EVERYONE due that day, in the post's
    own order and its own line format, numbered straight through. The caller
    splits it between names at Discord's limit."""
    people = list(people or ())
    return ([RECONNECT_REST] + _reconnect_numbered(people)) if people else []


def reconnect_lines_for_check() -> list:
    """Every fixed line R14 can post, with stand-in values."""
    two = [{"name": "Daisy Leigh", "company": "PolyAI"},
           {"name": "Ioana Mocanu", "company": "Agoda", "unclear": True}]
    return ([l for l in reconnect_post(two, more=36, tags="Vaishnavi, Sid") if l]
            + [l for l in reconnect_post(two[:1]) if l]
            + [reconnect_more(36), reconnect_more(1)] + reconnect_rest(two))


# -- meeting preparation (rule 8) ------------------------------------------------
#
# THE TWO POSTS, AGREED 12 Oct 2026. Today's meetings are ALWAYS their own post:
#
#     **Meeting today**
#     @Vaishnavi @Sid
#     we have a meeting today with:
#     1. Ada Lovelace (Acme AI)
#     2. Sujith P (ARTPARK)
#        They've been in the news recently — worth a skim before you go in:
#        [headline](url)
#
#     Are we all set on the deck, the package and the demo? Let me know if you need anything from me, or if you'd like another nudge before it starts.
#
# and the afternoon one, the closest block first, numbered straight through:
#
#     **Meeting preparation**
#     @Vaishnavi @Sid
#     we have a meeting in 3 days with:
#     1. Dr. David Gamez (Middlesex University London) — Wed 15 Oct
#        They've been in the news recently — worth a skim before the call:
#        [headline](url)
#
#     and a meeting in 5 days with:
#     2. Sujith P (ARTPARK) — Fri 17 Oct
#
#     Are we all set on the deck, the package and the demo? Let me know if you need anything from me, or if you'd like me to remind you again nearer the day.
#
# LOWER-CASE "we", as written. PERSON FIRST, COMPANY IN BRACKETS. Today's lines
# carry no date (it is today); the others carry the weekday and the date. The
# news sentence ends on a colon and the headline is on the next line, both
# indented under the name; no story, no sentence, and nothing ever says "no
# news". THE 5-DAY CLOSE SAYS START, THE OTHERS SAY CHECK: deliberate, and a
# post carrying a nearer block takes the check one. One meeting is still a list
# of one. The heading and the tags line are the sender's, not part of the block.
MEETING_TODAY_OPENER = "we have {meetings} today with:"
MEETING_PREP_OPENER = "we have {meetings} {when} with:"
MEETING_PREP_OPENER_NEXT = "and {meetings} {when} with:"
MEETING_NEWS_TODAY = ("   They've been in the news recently — worth a skim before you go in:")
MEETING_NEWS_PREP = ("   They've been in the news recently — worth a skim before the call:")
MEETING_NEWS_LINK = "   {link}"
MEETING_TODAY_CLOSE = ("Are we all set on the deck, the package and the demo? Let me know if you "
                       "need anything from me, or if you'd like another nudge before it starts.")
MEETING_PREP_CLOSE = ("Are we all set on the deck, the package and the demo? Let me know if you "
                      "need anything from me, or if you'd like me to remind you again nearer the "
                      "day.")
MEETING_PREP_CLOSE_START = ("Good time to start on the deck, the package and the demo. Let me know "
                            "if you need anything from me, or if you'd like me to remind you again "
                            "nearer the day.")
# A MEETING DATE NOBODY COULD READ is still named, in the afternoon post, on a
# line of its own after the blocks: without a date there is no touch for it to
# be on. Asked once a week.
MEETING_UNREADABLE = ("{n}. {who} — I couldn't read the Meeting Date on the sheet. Could you "
                      "put the day in?")
# THE REMINDER THE CLOSE OFFERS. Set from a reply under the post; sent by the
# one reminder loop like every other reminder.
MEETING_REMIND_SET = "I'll remind you about the meeting with {who} on {when}."
MEETING_REMIND_WHICH = "Which meeting is that for? Give me its number or the name."
MEETING_REMIND_WHEN = ("When would you like the reminder? Give me a day or a time, like "
                       "\"Thursday\" or \"3 PM\".")
MEETING_REMIND_AFTER = ("That's after the meeting with {who} on {day}. Could you give me a time "
                        "before it?")
MEETING_REMIND_NO_DATE = ("I don't have a date for the meeting with {who} yet, so there's nothing "
                          "to time a reminder against.")
MEETING_REMIND_WHAT = "the meeting with {who} on {day}"
# The meeting moved after a reminder was set: said once, when it is re-anchored.
MEETING_REMIND_MOVED = ("The meeting with {who} has moved to {day}, so I've moved your reminder "
                        "to {when}.")
_COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
                8: "eight", 9: "nine", 10: "ten"}


def meetings_count(n) -> str:
    """"a meeting", "two meetings", "12 meetings": the noun through `plural`,
    the number as a word up to ten because that is how the sentence was agreed
    ("we have two meetings today with:")."""
    n = max(1, int(n or 1))
    if n == 1:
        return "a meeting"
    counted = plural(n, "meeting")
    return counted.replace(str(n), _COUNT_WORDS[n], 1) if n in _COUNT_WORDS else counted


def meeting_days(days) -> str:
    """"in 3 days", or "tomorrow" for one (only a catch-up touch is that
    close)."""
    days = int(days or 0)
    return "tomorrow" if days == 1 else "in " + plural(days, "day")


def _meeting_lines(people, *, start, shown, sentence, dated) -> list:
    """One block's numbered lines, with a news line after each company's
    people. `shown` is the companies whose story this POST has already
    carried: a company's story appears once a post, after the last of its
    names in the first block it stands in."""
    people = list(people or ())
    lines: list = []
    n = start
    for i, person in enumerate(people):
        n += 1
        line = f"{n}. {str(person.get('who') or '').strip() or '(unnamed row)'}"
        when = day_date(person.get("meeting_iso")) if dated else ""
        if when:
            line += " — " + when
        lines.append(line)
        company = str(person.get("company_key") or "")
        nxt = str(people[i + 1].get("company_key") or "") if i + 1 < len(people) else None
        if company and nxt == company:
            continue                       # more of this company's people follow
        link = next((str(p.get("news_link") or "").strip() for p in people
                     if str(p.get("company_key") or "") == company
                     and str(p.get("news_link") or "").strip()), "") if company else \
            str(person.get("news_link") or "").strip()
        key = company or f"#{n}"
        if link and key not in shown:
            shown.add(key)
            lines += [sentence, MEETING_NEWS_LINK.format(link=link)]
    return lines


def meeting_today_post(people) -> list:
    """The lines of "Meeting today" under its heading and tags, WITHOUT the
    close (the caller adds `MEETING_TODAY_CLOSE` after a blank line).

    `people` are dicts with `who`, `company_key` and, when a story was found,
    `news_link` (the headline already as a masked link). Listed IN THE ORDER
    GIVEN, every one of them: no cap. [] with nobody, and then nothing is
    posted.
    """
    people = list(people or ())
    if not people:
        return []
    return [MEETING_TODAY_OPENER.format(meetings=meetings_count(len(people)))] + _meeting_lines(
        people, start=0, shown=set(), sentence=MEETING_NEWS_TODAY, dated=False)


def meeting_prep_post(blocks, *, unreadable=()) -> list:
    """The lines of "Meeting preparation" under its heading and tags, WITHOUT
    the close (`meeting_prep_close`).

    `blocks` are [{"days": 3, "people": [...]}, ...] IN THE ORDER GIVEN (the
    caller puts the closest first); each person as in `meeting_today_post`
    plus `meeting_iso`. The first block opens "we have…", each later one "and…"
    after a blank line, and the numbering runs straight through.
    `unreadable` are the `who` of the people whose Meeting Date could not be
    read: one line each, after the blocks. [] with nobody at all.
    """
    lines: list = []
    shown: set = set()
    n = 0
    for block in blocks or ():
        people = list(block.get("people") or ())
        if not people:
            continue
        opener = MEETING_PREP_OPENER if not lines else MEETING_PREP_OPENER_NEXT
        if lines:
            lines.append("")
        lines.append(opener.format(meetings=meetings_count(len(people)),
                                   when=meeting_days(block.get("days"))))
        lines += _meeting_lines(people, start=n, shown=shown, sentence=MEETING_NEWS_PREP,
                                dated=True)
        n += len(people)
    asks = [str(w or "").strip() for w in (unreadable or ()) if str(w or "").strip()]
    if asks and lines:
        lines.append("")
    for who in asks:
        n += 1
        lines.append(MEETING_UNREADABLE.format(n=n, who=who))
    return lines


def meeting_prep_close(days, *, start_days=None) -> str:
    """The afternoon post's close. `days` are the blocks' day counts.

    "Good time to start…" ONLY when every block is the earliest touch
    (`start_days`, 5): with a nearer block in the post the close is the check
    one. "" when the post has no block at all (only dates nobody could read):
    there is no meeting day to be set for.
    """
    days = [int(d) for d in (days or ())]
    if not days:
        return ""
    if start_days is not None and all(d == int(start_days) for d in days):
        return MEETING_PREP_CLOSE_START
    return MEETING_PREP_CLOSE


def meeting_remind_set(who, when) -> str:
    return MEETING_REMIND_SET.format(who=str(who or "").strip(), when=str(when or "").strip())


def meeting_remind_after(who, day) -> str:
    return MEETING_REMIND_AFTER.format(who=str(who or "").strip(), day=str(day or "").strip())


def meeting_remind_no_date(who) -> str:
    return MEETING_REMIND_NO_DATE.format(who=str(who or "").strip())


def meeting_remind_what(who, day) -> str:
    return MEETING_REMIND_WHAT.format(who=str(who or "").strip(), day=str(day or "").strip())


def meeting_remind_moved(who, day, when) -> str:
    return MEETING_REMIND_MOVED.format(who=str(who or "").strip(), day=str(day or "").strip(),
                                       when=str(when or "").strip())


def meeting_lines_for_check() -> list:
    """Every fixed line R8 can post, with stand-in values."""
    who = next_step_who("Priya Rao", "Acme Labs")
    link = "[Acme Labs raises a round](<https://example.com/a>)"
    one = {"who": who, "company_key": "acme labs", "news_link": link,
           "meeting_iso": "2026-10-15"}
    two = [one, {**one, "who": next_step_who("Bo Chen", "Borealis"), "company_key": "borealis",
                 "news_link": ""}]
    lines = (meeting_today_post([one]) + meeting_today_post(two)
             + meeting_prep_post([{"days": 3, "people": [one]}, {"days": 5, "people": two}],
                                 unreadable=[who])
             + meeting_prep_post([{"days": 1, "people": [one]}]))
    return [l for l in lines if l] + [
        MEETING_TODAY_CLOSE, MEETING_PREP_CLOSE, MEETING_PREP_CLOSE_START,
        meeting_remind_set(who, "Wed 14 Oct at 2 PM"), MEETING_REMIND_WHICH, MEETING_REMIND_WHEN,
        meeting_remind_after(who, "Wed 15 Oct"), meeting_remind_no_date(who),
        meeting_remind_what(who, "Wed 15 Oct"),
        meeting_remind_moved(who, "Fri 17 Oct", "Thu 16 Oct at 2 PM")]


# -- the deliverables checklist (rule 4) -----------------------------------------
#
# THE POST, AGREED 12 Oct 2026:
#
#     **Deliverables to chase**
#     @Vaishnavi @Sid
#     11 open:
#     1. New Hinglish STT Demo/Sales Package
#        Team: Sales
#        Due: Mon 14 Sep
#     2. MSA template
#        Team: Legal
#        There's no deadline for this
#     (+3 more — the rest are on the Deliverables Checklist.)
#
# ONE OPENER, the count, the same every Monday (the three variants are gone).
# THREE LINES AN ITEM: the Action Item as the sheet spells it, the team, the
# deadline. NO OVERDUE COUNT, NO LINK, NO CLOSING LINE: the old closes promised
# "I'll update my list" and nothing writes to that tab, so nothing is promised
# and nothing is asked. A title over DELIVERABLES_TITLE_MAX characters is cut
# with an ellipsis; one at or under it prints whole.
DELIVERABLES_OPENER = "{n} open:"
DELIVERABLES_TEAM = "   Team: {team}"
DELIVERABLES_DUE = "   Due: {when}"
DELIVERABLES_NO_DEADLINE = "   There's no deadline for this"
DELIVERABLES_MORE = "(+{n} more — the rest are on the Deliverables Checklist.)"
DELIVERABLES_TITLE_MAX = 180


def deliverable_title(title) -> str:
    """The Action Item as the post shows it: whole up to
    DELIVERABLES_TITLE_MAX characters, cut there with an ellipsis past it."""
    text = " ".join(str(title or "").split())
    if len(text) <= DELIVERABLES_TITLE_MAX:
        return text
    return text[:DELIVERABLES_TITLE_MAX].rstrip() + "…"


def deliverables_post(items, *, limit=20) -> list:
    """The lines of R4's post under its heading and tags. There is no close.

    `items` are dicts with `title`, `team` and `deadline` (an ISO date, or ""
    when the row has none that can be read), IN THE ORDER GIVEN. The count in
    the opener is ALL of them; the first `limit` are listed and one plain line
    says how many more there are. [] with no items, and then nothing is
    posted.
    """
    items = list(items or ())
    if not items:
        return []
    limit = max(1, int(limit or 20))
    lines = [DELIVERABLES_OPENER.format(n=len(items))]
    for i, item in enumerate(items[:limit], 1):
        lines.append(f"{i}. {deliverable_title(item.get('title'))}")
        lines.append(DELIVERABLES_TEAM.format(team=str(item.get("team") or "").strip()))
        when = day_date(item.get("deadline"))
        lines.append(DELIVERABLES_DUE.format(when=when) if when else DELIVERABLES_NO_DEADLINE)
    if len(items) > limit:
        lines.append(DELIVERABLES_MORE.format(n=len(items) - limit))
    return lines


def deliverables_lines_for_check() -> list:
    """Every fixed line R4 can post, with stand-in values."""
    return deliverables_post(
        [{"title": "Pulse overview doc", "team": "Sales", "deadline": "2026-09-14"},
         {"title": "MSA template", "team": "Legal", "deadline": ""},
         {"title": "Pricing page", "team": "Vaishnavi", "deadline": "2026-12-31"}], limit=2)


# -- every line, rendered, for the register check ----------------------------


def all_lines() -> list:
    """Every line this module can produce, with stand-in values — what the
    register test reads. A new line that is not added here is not checked, so
    add it here."""
    return [
        *INTERIM_WEB, *INTERIM_ENGINE,
        model_failure("APIConnectionError"), BRIEF_FAILED, BRIEF_EMPTY,
        greeting("Hi Vaishnavi", "morning"), not_followed("Hi"), FOUND_NOTHING, TRUNCATED,
        *SMALLTALK_LINES, PROMPT_TEMPLATE, PROMPT_NOT_MINE, PROMPT_MISSING,
        two_matches("Daisy", ["Daisy at Acme", "Daisy at Globex"]),
        which_proposal(["the update to Acme", "the AI events reminder"]),
        confirm_proposal("the update to Acme"),
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
        *[l for l in dm_no_meeting_lines_for_check() if l],
        *closure_lines_for_check(),
        *events_lines_for_check(),
        *packages_lines_for_check(),
        *reconnect_lines_for_check(),
        *meeting_lines_for_check(),
        *deliverables_lines_for_check(),
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


# R-f: what a line may never ask for (NFT2-1090).
VOTE_WORDS = ("reply with yes", "say yes", "yes or no", "choose 1", "give me the number",
              "vote")
# Lines whose words the team chose, kept exactly as written even where a rule
# would object: TAKE_YOUR_TIME opens with "Of course" by the ticket's choice.
REGISTER_EXEMPT = (TAKE_YOUR_TIME,)


def register_problems(line: str) -> list:
    """What is wrong with one fixed line under R-a, R-b, R-d and R-f: a list of
    short reasons, empty when it is fine."""
    import re

    problems = []
    if line in REGISTER_EXEMPT:
        return problems
    low = line.lower()
    for word in VOTE_WORDS:
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", low):
            problems.append(f"R-f asks for a vote: {word!r}")
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
    check("which one, naming each, as a question",
          which_proposal(["the update to Acme", "the AI events reminder for Mon 12 Oct"]),
          "Which one did you mean? A name is fine.\n1. the update to Acme\n"
          "2. the AI events reminder for Mon 12 Oct")
    check("R-f: no line asks for a vote or a number",
          [l for l in all_lines() + [TAKE_YOUR_TIME] if any(
              w in l.lower() for w in VOTE_WORDS)], [])
    check("R-f catches one", bool(register_problems("Reply with yes or give me the number.")),
          True)
    check("the time of day", [part_of_day(h) for h in (0, 3, 4, 11, 12, 16, 17, 23)],
          ["evening", "evening", "morning", "morning", "afternoon", "afternoon",
           "evening", "evening"])
    check("the greeting has no task", greeting("Hi Sid", "morning"),
          "Hi Sid, good morning. How's it going?")
    check("two Daisys", two_matches("Daisy", ["Daisy at Acme", "Daisy at Globex"]),
          "Two Daisys on the sheet. Daisy at Acme or Daisy at Globex?")
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
    check("the first email, not out (ordinal words, not digits)",
          next_step_line("email_out", who=who, n=1),
          "Priya Rao (Acme Labs): send the first email")
    check("an unknown ask is no line", next_step_line("wat", who=who), "")
    check("every ask has a preview line",
          [a for a in NEXT_STEP_ASKS if not next_step_line(a, who=who, n=1)], [])
    check("every ask has its agreed group line",
          [a for a in NEXT_STEP_ASKS if not next_step_group(a, n=1)], [])
    check("there is no group for a fourth email",
          (next_step_group("advance", n=3), next_step_group("email_out", n=4)), ("", ""))
    post = next_step_post([
        {"who": "Gone Quiet (Co)", "ask": "unresponsive"},
        {"who": "A (Co)", "ask": "email_out", "n": 1},
        {"who": "B (Co)", "ask": "replied"},
        {"who": "C (Co)", "ask": "email_out", "n": 1},
    ])
    check("groups in the agreed order whatever order people arrive in",
          post, ["The people below have replied to us! We need to set up a meeting with them:",
                 "- B (Co)", "",
                 "We need to send out the first email to these people:",
                 "- A (Co)", "- C (Co)", "",
                 "No reply after the calls. These look unresponsive:",
                 "- Gone Quiet (Co)"])
    check("there is no drop line: nobody is dropped (12 Oct 2026)",
          (next_step_group("dropped"), [l for l in next_step_lines_for_check()
                                        if "stop reminding" in l or "three weeks" in l]), ("", []))
    check("a person is one line, the name and the company and nothing else",
          next_step_person(who), "- Priya Rao (Acme Labs)")
    check("nobody to name: no lines at all", next_step_post([]), [])
    columns = ("Email Sent", "Email Date", "Next Steps", "LI DM", "Prospect Status",
               "Meeting Date")
    check("no line a post can carry names a column",
          [(c, l) for l in next_step_lines_for_check() for c in columns if c in l], [])
    import re as _re
    schedule = _re.compile(r"\b(every|daily|days?|weekly|3 ?pm|15:00|R\d+|rule \d+)\b",
                           _re.IGNORECASE)
    check("no line talks about the schedule or a rule number",
          [l for l in next_step_lines_for_check() if schedule.search(l)], [])
    check("no line names a setting",
          [l for l in next_step_lines_for_check() if _re.search(r"[A-Z]{3,}_[A-Z_]+", l)],
          [])

    print("\nno meeting booked yet (rule 7) - two blocks, one numbering")
    both = dm_no_meeting_post(
        [{"who": "B (Co)", "block": "booked", "meeting_iso": "2026-10-24"},
         {"who": "A (Co)", "block": "none"}], days=21, gap_days=7)
    check("the blocks come in the agreed order, numbered straight through, each sentence under its list",
          both, ["1. A (Co)", "", DM_NO_MEETING_NONE.format(span="3 weeks"), "",
                 "2. B (Co) \u2014 24 Oct", "", DM_NO_MEETING_BOOKED.format(span="a week")])
    check("only the second block: the first block and its sentence are both absent",
          dm_no_meeting_post([{"who": "B (Co)", "block": "booked", "meeting_iso": "2026-10-24"}],
                             days=21, gap_days=7),
          ["1. B (Co) \u2014 24 Oct", "", DM_NO_MEETING_BOOKED.format(span="a week")])
    check("the number follows the setting: weeks when it divides by 7, otherwise days",
          [days_span(n) for n in (21, 14, 7, 10, 1)], ["3 weeks", "2 weeks", "a week", "10 days", "a day"])
    check("nobody -> no lines at all", dm_no_meeting_post([], days=21, gap_days=7), [])
    check("nothing is asked, and no column is named",
          [l for l in dm_no_meeting_lines_for_check()
           if "?" in l or "LI DM" in l or "Meeting Status" in l or "Next Steps" in l], [])

    print("\nALL PASSED" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

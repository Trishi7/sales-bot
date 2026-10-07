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
# when a search has actually run or the question asks for the web
# (bot._answer_with_engine); the engine lines name no source at all, because
# the bot does not know which ones the model will read (R-e).
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


HOLDING = "Noted — holding until someone can approve it."


def declined(why: str, others=()) -> str:
    """A no on a proposal. When somebody else had already said yes, the reply
    says so and says the sheet is unchanged: the person who said yes must not
    be left thinking it went through."""
    others = [str(o) for o in (others or ()) if str(o).strip()]
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
NOTHING_TO_ADD = "There was nothing left to add on that one."
NOTHING_TO_WRITE = "There was nothing left to write on that one."


def write_failed(error: str) -> str:
    return f"I couldn't write that: {error}. Nothing has changed."


def nothing_to_undo(hours) -> str:
    return f"I haven't changed anything in the last {hours}h that I can put back."


def undo_failed(error: str) -> str:
    return (f"I couldn't put that back: {error}. The cells are as they were after "
            "my change.")


def undone(company: str, what: str) -> str:
    return f"Done — I put {company}'s {what}."


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
        which_person(2, "Acme", "Ada, Sam"), HOLDING, declined("it's a duplicate"),
        declined("it's a duplicate", ["Sid"]), WRITES_OFF_NOTE,
        focus_cleared("fintech"), NO_FOCUS, NOTHING_TO_ADD, NOTHING_TO_WRITE,
        write_failed("the sheet refused it"), nothing_to_undo(24),
        undo_failed("the sheet refused it"), undone("Acme", "stage back to DM sent"),
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

    print("\nALL PASSED" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

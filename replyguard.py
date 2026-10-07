"""THE ANSWER GUARD — a deterministic check on the OPENING of an outgoing answer.

WHY IT EXISTS. The prompt asks the model to answer first, never to restate the
question and never to open with "Here's what I found" or "Sure!". A prompt is a
request: on a long tool turn the model still does it some of the time, and one
"Great question!" in a channel the sales team reads is enough to make every
other reply read as a machine's. This is the part of the voice that does not
depend on being obeyed. Proactive messages have had `llm.proactive_verdict`
since the tone build; answers had nothing.

WHAT IT DOES, AND ALL IT DOES. It looks at the first sentence of a reply and,
when that sentence is one of five known kinds of throat-clearing, removes it:

    G1 interjection        "Sure!", "Great question.", "Of course,"
    G2 lead-in             "Here's what I found:", "Here are the top 5 ...:"
    G3 based-on            "Based on the tracker, ..."
    G4 echo-frame          "You asked about ...", "To answer your question, ..."
    G5 restated-question   a first sentence made only of the question's own words

IT ONLY REMOVES. It never adds a word, never reorders, and never touches
anything but the opening, so it cannot invent a fact. The other half of that
promise is that it must never remove one either, and most of this file is the
list of things it refuses to strip: a sentence carrying a number, a link, a
meeting citation or a bold name the question did not already contain; the one
plain line about what is missing ("Nothing on Acme in the channel."); a
sentence the caller protects word for word; a list; an answer to a yes/no
question; and any removal that would leave nothing to send. When a banned
phrase opens a reply and taking it out is not safe, the reply goes out as the
model wrote it and the guard reports `<rule>:kept`, so the log still shows the
prompt was not followed.

NO MODEL CALL, NO CONFIG, NO I/O. `re` only. bot.py decides whether to call it
(ANSWER_GUARD_ENABLED) and writes the log line; this module just answers.

`python -m replyguard` runs the self-test.
"""
import re

# At or above this share of a first sentence's content words coming straight
# from the question, the sentence is the question said back.
ECHO_THRESHOLD = 0.6

# Stops a pathological reply from being eaten sentence by sentence.
MAX_REMOVALS = 3

# The bot's own name is never a content word: "Saley, where are we with Acme?"
# and "where are we with Acme?" are one question. bot.py adds COS_NAME.
BOT_NAMES = {"saley"}

# H11: the openers beyond the ticket's five ("Here is", "Here's what I found",
# "Based on", "Great question", "Sure!"). Set to False to strip only those
# five. A human decision, so it is one switch.
EXTENDED_OPENERS = True

_APOS = "['’]"

_STOP = frozenset("""
a an the of in on at for to from with by about as into onto over under after
before up out off and or but so if then than that this these those there
is are was were be been being am do does did has have had can could will would
should shall may might must
i me my mine we us our ours you your yours he him his she her hers they them
their theirs it its
who whom whose what when where why how which
please hey hi hello ok okay thanks thank
""".split())

_GAP = frozenset("""
no not cannot nothing none nobody never neither without yet only both all
every each still already yes yep nope zero one two three four five six seven
eight nine ten
""".split())

_FILLER_RAW = """
here below following follows found got have know see show list summary
overview rundown breakdown update status latest details info information quick
"""

_YESNO = frozenset("""
is are was were do does did has have had can could will would should shall may
might am any
""".split())

_WORD_RE = re.compile(r"[a-z0-9]+(?:['’&-][a-z0-9]+)*")
_MENTION_RE = re.compile(r"<[@#][!&]?\d+>")

# FACT TOKENS: the things a first sentence can carry that a reader would lose.
_FACT_RES = (
    re.compile(r"\[[^\]\n]*\]\(<?[^)\s]*>?\)"),      # a masked link
    re.compile(r"https?://\S+"),                      # a bare url or jump link
    re.compile(r"\([^)\n]*\)"),                       # anything in round brackets
    re.compile(r"\*\*[^*\n]+\*\*"),                   # a bold label or name
    _MENTION_RE,                                      # a Discord mention
    re.compile(r"\d+(?:[.,:/-]\d+)*"),                # a number, a date, a time
    re.compile(r"@"),
    re.compile(r"[$€£₹¥]"),       # a currency sign
)

# The same shape as tone.opener_of's "Name —" address (tone.py), copied rather
# than imported so this module stays free of every other one.
_ADDRESS_RE = re.compile(r"[A-Z][\w'’-]{1,20}\s*[,—–:-]\s+")
# tone.is_point_line's pattern, for the same reason.
_POINT_RE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+")

_G1_TICKET = ("great question", "sure")
_G1_EXTENDED = ("sure thing", "good question", "certainly", "of course", "absolutely")
_G2_TICKET = ("here is", "here" + _APOS + "s")
_G2_EXTENDED = ("here are", "here you go", "below is", "below are",
                "this is what i found", "i found the following")
_G4_PHRASES = (
    "you asked", "you" + _APOS + "re asking", "you are asking",
    "you wanted to know", "you want to know", "to answer your question",
    "in answer to your question", "in response to your question",
    "regarding your question", "as for your question", "your question was",
    "your question is",
)
# A word that starts a banned phrase is never read as a "Name," address:
# "Sure, Acme replied" opens with an interjection, not with somebody's name.
_NOT_A_NAME = frozenset(
    "sure great good certainly of absolutely here below this i based you to in "
    "regarding as your".split())


def _alternation(phrases) -> str:
    return "|".join(sorted((p.replace(" ", r"\s+") for p in phrases),
                           key=len, reverse=True))


def _g1_re():
    phrases = _G1_TICKET + (_G1_EXTENDED if EXTENDED_OPENERS else ())
    return re.compile(r"(?:" + _alternation(phrases) + r")\s*(?:[!.,]+|[—–-]+)\s*",
                      re.IGNORECASE)


def _g2_re():
    phrases = _G2_TICKET + (_G2_EXTENDED if EXTENDED_OPENERS else ())
    return re.compile(r"(?:" + _alternation(phrases) + r")(?![\w'’])", re.IGNORECASE)


_G3_RE = re.compile(r"based\s+on\b", re.IGNORECASE)


def _g4_re():
    # The echo frames are all beyond the ticket's five, so H11 switches them.
    if not EXTENDED_OPENERS:
        return None
    return re.compile(r"(?:" + _alternation(_G4_PHRASES) + r")(?![\w'’])", re.IGNORECASE)


# -- the definitions ---------------------------------------------------------


def _normal(word: str) -> str:
    """One word as the echo score compares it: no possessive, no plural s."""
    word = re.sub(_APOS + r"s$", "", word)
    word = re.sub(r"[^a-z0-9]", "", word)
    if len(word) > 3 and word.endswith("s"):
        word = word[:-1]
    return word


_STOP_N = frozenset(_normal(w) for w in _STOP)
_FILLER = frozenset(_normal(w) for w in _FILLER_RAW.split())


def content_words(text) -> list:
    """The words of `text` that carry meaning: lower-cased, the possessive and
    a plural s dropped, without articles, prepositions, auxiliaries, pronouns,
    wh-words, "please" and the bot's own name."""
    body = _MENTION_RE.sub(" ", str(text or "")).lower()
    names = {_normal(n.lower()) for n in BOT_NAMES}
    out = []
    for raw in _WORD_RE.findall(body):
        if raw in _STOP:
            continue
        word = _normal(raw)
        if not word or word in _STOP_N or word in names:
            continue
        out.append(word)
    return out


def _split_lead(text: str):
    """(prefix, lead): the leading whitespace and ONE "Name —" address, then
    what the rules test. The address is kept; it is never an opener."""
    text = str(text or "")
    start = len(text) - len(text.lstrip())
    match = _ADDRESS_RE.match(text, start)
    if match:
        first = re.match(r"[A-Za-z]+", text[start:])
        if first and first.group(0).lower() not in _NOT_A_NAME:
            start = match.end()
    return text[:start], text[start:]


def _sentence_end(lead: str) -> int:
    """Index just past the first sentence of `lead`: a ., ! or ? followed by
    whitespace or the end; a colon followed by a NEWLINE; or a newline. A colon
    in the middle of a line does not end a sentence."""
    for i, ch in enumerate(lead):
        if ch == "\n":
            return i
        nxt = lead[i + 1] if i + 1 < len(lead) else ""
        if ch in ".!?" and (nxt == "" or nxt.isspace()):
            return i + 1
        if ch == ":" and (nxt == "\n" or lead[i + 1:i + 3] == "\r\n"):
            return i + 1
    return len(lead)


def first_sentence(text) -> str:
    """The first sentence of a reply, after any leading "Name —" address."""
    _prefix, lead = _split_lead(text)
    return lead[:_sentence_end(lead)].strip()


def echo_score(question, sentence) -> float:
    """The share of `sentence`'s content words that are also the question's.
    0.0 for a sentence with fewer than two content words: one shared word is
    an answer ("Acme."), not an echo."""
    words = content_words(sentence)
    if len(words) < 2:
        return 0.0
    asked = set(content_words(question))
    return sum(1 for w in words if w in asked) / len(words)


def _new_fact(segment: str, question: str) -> bool:
    """Does `segment` carry a fact token the question did not already have?"""
    asked = str(question or "").lower()
    for pattern in _FACT_RES:
        for match in pattern.finditer(segment):
            if match.group(0).lower() not in asked:
                return True
    return False


def _without_facts(segment: str) -> str:
    """`segment` with its fact tokens blanked: what is left is the prose."""
    for pattern in _FACT_RES:
        segment = pattern.sub(" ", segment)
    return segment


def _gap_word(segment: str) -> bool:
    """Does `segment` say something is missing, partial or settled? Those are
    the lines the honesty rules exist to keep."""
    for word in re.findall(r"[a-z]+(?:['’][a-z]+)?", segment.lower()):
        if word in _GAP or word.endswith("n't") or word.endswith("n’t"):
            return True
    return False


def _named_source(segment: str, question: str) -> bool:
    """A capitalised name in `segment` that the question did not use: "Based on
    the Sales Bot Discussion" names where a fact came from, and that is not
    throat-clearing even without a date beside it."""
    asked = str(question or "").lower()
    for match in re.finditer(r"(?<![\w'’])[A-Z][\w'’-]*", segment):
        word = match.group(0)
        if match.start() == 0 or word in ("I", "I'm", "I've", "I’m", "I’ve"):
            continue
        if word.lower() not in asked:
            return True
    return False


def _question_body(question: str) -> str:
    """The question without a leading mention, the bot's name, "hey", "please"."""
    body = _MENTION_RE.sub(" ", str(question or "")).strip().lower()
    skip = {"hey", "hi", "hello", "please", "pls", "ok", "okay", "so", "and"}
    skip |= {n.lower() for n in BOT_NAMES}
    words = re.findall(r"[a-z0-9]+(?:['’][a-z]+)?|[^\sa-z0-9]", body)
    while words and (words[0] in skip or not re.match(r"[a-z0-9]", words[0])):
        words.pop(0)
    return " ".join(words)


def _is_yes_no(question: str) -> bool:
    """A question a bare "Acme is on hold." answers. Its answer is made of the
    question's own words by nature, so G5 must leave it alone."""
    first = (_question_body(question).split() or [""])[0]
    first = re.sub(r"n['’]t$", "", first)
    return first in _YESNO or first in ("ca", "wo")  # can't, won't


def _is_choice(question: str) -> bool:
    """"Acme or Globex — who replied?": the answer names one of the options
    the question listed, so it too is built from the question's words."""
    return bool(re.search(r"\bor\b", str(question or "").lower()))


_CLAUSE_WORDS = frozenset(
    "is are was were am be been being has have had do does did will would can "
    "could should include includes included follow follows following look looks "
    "stand stands".split())


def _is_a_heading(sentence: str) -> bool:
    """A name or a label standing over the lines beneath it — "Sigil Wen",
    "**Janajit Bagchi**", "Acme AI:" — and not a sentence about anything.

    A PER-PERSON ANSWER OPENS WITH THE PERSON'S NAME, and when the question
    named them that line is made of the question's words and nothing else. It
    is still content: take it away and the lines under it belong to nobody. A
    restated question is a CLAUSE ("The sales objectives for today are:"); a
    heading has no verb and no "here is / as follows" in it, so that is the
    test.
    """
    words = re.findall(r"[a-z]+(?:['’][a-z]+)?", sentence.lower())
    return not any(w in _CLAUSE_WORDS or _normal(w) in _FILLER for w in words)


def _is_name_like(sentence: str) -> bool:
    """One to four words, each starting with a capital, no digit — "Sigil Wen",
    "**Janajit Bagchi**", "Acme AI:". THE REPLY'S CASING DECIDES, not the
    question's: somebody typing "sigil wen" still gets a header that is a
    name, and without it the lines under it belong to nobody."""
    words = sentence.replace("**", "").strip().rstrip(":").strip().split()
    return 1 <= len(words) <= 4 and not re.search(r"\d", sentence) \
        and all(w[:1].isupper() for w in words)


def _g5(question: str, sentence: str) -> bool:
    if not str(question or "").strip() or not sentence:
        return False
    # A heading is left alone when it is a NAME, or when it does not hold
    # every content word of the question. "Sigil Wen" over that person's lines
    # stays even when the question was only "Sigil Wen?"; "Top 5 AI
    # headlines:" over the bullets is the whole question said back as a label.
    if _is_a_heading(sentence) and (
            _is_name_like(sentence)
            or not set(content_words(question)) <= set(content_words(sentence))):
        return False
    if _is_yes_no(question) or _is_choice(question):
        return False
    if echo_score(question, sentence) < ECHO_THRESHOLD:
        return False
    asked = set(content_words(question))
    if any(w not in asked and w not in _FILLER for w in content_words(sentence)):
        return False
    return not _new_fact(sentence, question) and not _gap_word(sentence)


def restates(question, reply) -> bool:
    """Is the reply's first sentence the question said back (rule G5)?"""
    try:
        return _g5(str(question or ""), first_sentence(reply))
    except Exception:
        return False


def banned_opener(reply) -> str:
    """The label of the rule whose phrase opens the reply — "interjection",
    "lead-in", "based-on", "echo-frame" — or "". It says the phrase is THERE,
    whether or not it was safe to remove."""
    try:
        _prefix, lead = _split_lead(reply)
        if _g1_re().match(lead):
            return "interjection"
        if _g2_re().match(lead):
            return "lead-in"
        if _G3_RE.match(lead):
            return "based-on"
        g4 = _g4_re()
        if g4 is not None and g4.match(lead):
            return "echo-frame"
    except Exception:
        pass
    return ""


# -- the rules ---------------------------------------------------------------


def _has_words(text: str) -> bool:
    return bool(re.search(r"\w", text or ""))


def _looks_like_a_list(rest: str) -> bool:
    """After a first comma, is what follows the next item of a list rather than
    the answer? "Based on the tracker, the notes and the channel, ..." must not
    lose its first item and keep the other two."""
    if re.match(r"\s*(?:and|or|plus|as well)\b", rest, re.IGNORECASE):
        return True
    nxt = rest.find(",")
    if nxt < 0:
        return False
    item = rest[:nxt]
    return len(item.split()) <= 3 or bool(re.search(r"\b(?:and|or)\b", item, re.IGNORECASE))


def _cut(lead: str, question: str):
    """(label, end): the rule that matches the opening of `lead` and how many
    characters of it to remove. end == 0 means the phrase is there but taking
    it out is not safe. ("", 0) when no rule matches."""
    end = _sentence_end(lead)
    sentence = lead[:end]

    match = _g1_re().match(lead)
    if match:
        return "interjection", match.end()

    if _g2_re().match(lead):
        if not _new_fact(sentence, question) and not _gap_word(sentence) \
                and not _named_source(sentence, question) \
                and _has_words(lead[end:]):
            return "lead-in", end
        # "Here's where Acme is: they replied on 12 Aug." The sentence carries
        # the answer, so only the announcement in front of the colon can go.
        colon = re.search(r":[ \t]+", sentence)
        # And only when a clause follows it: in "Here's the page: [Acme](<url>)"
        # the words in front of the colon are all that says what the link is.
        head = sentence[:colon.start()] if colon else ""
        if colon and not _new_fact(head, question) and not _gap_word(head) \
                and not _named_source(head, question) \
                and len(content_words(_without_facts(sentence[colon.end():]))) >= 2:
            return "lead-in", colon.end()
        return "lead-in", 0

    match = _G3_RE.match(lead)
    if match:
        stop = re.search(r"[,:]", sentence[match.end():])
        if not stop:
            return "based-on", 0
        at = match.end() + stop.start()
        clause = sentence[match.end():at]
        if _new_fact(clause, question) or _named_source(" " + clause, question) \
                or _looks_like_a_list(sentence[at + 1:]):
            return "based-on", 0
        return "based-on", at + 1

    g4 = _g4_re()
    match = g4.match(lead) if g4 is not None else None
    if match:
        after = sentence[match.end():]
        direct = re.match(r"\s*,", after)
        if direct:
            return "echo-frame", match.end() + direct.end()
        comma = after.find(",")
        if comma >= 0:
            at = match.end() + comma
            rest = sentence[at + 1:]
            asked = set(content_words(question))
            says_more = (not asked or _new_fact(rest, question) or _gap_word(rest)
                         or any(w not in asked for w in content_words(rest)))
            # A NAME THE QUESTION DID NOT USE IS NOT AN ECHO. "what about
            # them?" answered "You asked about Acme and Globex. Both replied."
            # loses who "both" are if the frame goes, so it stays.
            if says_more and not _new_fact(sentence[:at], question) \
                    and not _named_source(sentence[:at], question) \
                    and not _looks_like_a_list(rest):
                return "echo-frame", at + 1
        if not _new_fact(sentence, question) and not _gap_word(sentence) \
                and not _named_source(sentence, question):
            return "echo-frame", end
        return "echo-frame", 0

    if _g5(question, sentence.strip()):
        return "restated-question", end
    return "", 0


def _exempt(lead: str, protect) -> bool:
    """A reply the guard does not read at all: a protected sentence, a list, or
    something that is not prose (JSON, a code fence, a masked link)."""
    body = lead.lstrip()
    for sentence in protect or ():
        sentence = str(sentence or "").strip()
        if sentence and body.startswith(sentence):
            return True
    if body[:1] in ("{", "[") or body.startswith("```"):
        return True
    first_line = body.split("\n", 1)[0]
    return bool(_POINT_RE.match(first_line))


def clean(reply, *, question="", protect=()):
    """(text to send, [{"rule", "removed"}]).

    Applies G1–G5 to the opening of `reply`, in that order, again after each
    removal, at most MAX_REMOVALS times. `protect` is sentences that must go
    out word for word (the three notes sentences): a reply opening with one is
    returned untouched. Never raises, and never returns "" for a reply that
    had text in it — when in doubt the model's own words go out.
    """
    if not isinstance(reply, str) or not reply.strip():
        return reply, []
    fired = []
    text = reply
    try:
        question = str(question or "")
        for _ in range(MAX_REMOVALS):
            prefix, lead = _split_lead(text)
            if _exempt(lead, protect) or _exempt(text, protect):
                break
            label, end = _cut(lead, question)
            if not label:
                break
            rest = lead[end:].lstrip() if end else ""
            if not end or not _has_words(rest):
                fired.append({"rule": label + ":kept", "removed": ""})
                break
            # The capital moves with the opening: "Sure, they replied" becomes
            # "They replied", and "Vaishnavi — sure, they replied" keeps its
            # lower case because the address still opens the reply.
            if lead[:1].isupper() and rest[:1].islower():
                rest = rest[:1].upper() + rest[1:]
            fired.append({"rule": label, "removed": lead[:end].strip()})
            text = prefix + rest
    except Exception:
        return reply, []
    if not text.strip():
        return reply, []
    return text, fired


# -- self-test ---------------------------------------------------------------


def _self_test() -> int:
    """`python -m replyguard` — what it strips, and everything it must not."""
    failures = 0

    def check(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    def rules(reply, question=""):
        return [f["rule"] for f in clean(reply, question=question)[1]]

    print("definitions")
    check("first sentence stops at a full stop",
          first_sentence("Acme replied. Demo is Thursday."), "Acme replied.")
    check("a colon before a newline ends it",
          first_sentence("Two on the sheet:\n• Ada"), "Two on the sheet:")
    check("a colon mid-line does not",
          first_sentence("The split is this: two replied. More."),
          "The split is this: two replied.")
    check("a decimal is not a sentence end",
          first_sentence("It is 3.5 crore. Fine."), "It is 3.5 crore.")
    check("the address is not part of it",
          first_sentence("Vaishnavi — Acme replied. More."), "Acme replied.")
    check("content words", content_words("Saley, who are the PoCs at Acme's team?"),
          ["poc", "acme", "team"])
    check("echo score of a restated question",
          echo_score("what are the sales objectives for today?",
                     "The sales objectives for today are:"), 1.0)
    check("one content word never scores", echo_score("who owns Acme?", "Acme."), 0.0)

    print("\nG1-G5 strip")
    strips = (
        ("Sure! Acme replied on 12 Aug.", "", "Acme replied on 12 Aug.", ["interjection"]),
        ("Great question. Two on the sheet:\n• Ada", "", "Two on the sheet:\n• Ada",
         ["interjection"]),
        ("Of course — they replied on 12 Aug.", "", "They replied on 12 Aug.",
         ["interjection"]),
        ("Here's what I found:\n• Ada Lovelace — CTO", "", "• Ada Lovelace — CTO",
         ["lead-in"]),
        ("Here are the top 5 AI headlines:\n• One", "top 5 AI headlines", "• One",
         ["lead-in"]),
        ("Here's where Acme is: they replied on 12 Aug.", "where are we with Acme?",
         "They replied on 12 Aug.", ["lead-in"]),
        ("Based on the tracker, Acme is at DM sent.", "", "Acme is at DM sent.",
         ["based-on"]),
        ("You asked about the PoCs at Acme. Two on the sheet: Ada and Sam.",
         "who are the PoCs at Acme?", "Two on the sheet: Ada and Sam.", ["echo-frame"]),
        ("To answer your question, Acme is on hold (Sales Bot Discussion, 2 Sep).",
         "is Acme on hold?", "Acme is on hold (Sales Bot Discussion, 2 Sep).",
         ["echo-frame"]),
        ("The sales objectives for today are:\n1. Chase Acme",
         "what are the sales objectives for today?", "1. Chase Acme",
         ["restated-question"]),
        ("Sure! Here's what I found:\nAcme replied.", "", "Acme replied.",
         ["interjection", "lead-in"]),
        # the whole question said back as a label over the list
        ("Top 5 AI headlines:\n• One", "top 5 AI headlines", "• One", ["restated-question"]),
        ("Sales objectives for today:\n1. Chase Acme",
         "what are the sales objectives for today?", "1. Chase Acme", ["restated-question"]),
        ("Deals on hold:\n• Acme", "which deals are on hold?", "• Acme",
         ["restated-question"]),
        ("Vaishnavi — sure, Acme replied on 12 Aug.", "",
         "Vaishnavi — Acme replied on 12 Aug.", ["interjection"]),
    )
    for reply, question, want, fired in strips:
        got, log = clean(reply, question=question)
        check(f"clean({reply[:38]!r})", got, want)
        check("  rules", [f["rule"] for f in log], fired)
        check("  idempotent", clean(got, question=question)[0], got)

    print("\nwhat it must never strip")
    keeps = (
        # 1. only the opening
        ("Acme replied. Here is the deck.", "where is Acme?"),
        # 2. a new fact token in the opener
        ("Based on the Sales Bot Discussion of 2 Sep, Acme is on hold.", "is Acme on hold?"),
        ("Based on the Sales Bot Discussion, Acme is on hold.", "where is Acme?"),
        ("Here's the deck Kushal sent: [Acme deck](<https://example.com/d>)",
         "where is the deck?"),
        # 3. the gap line
        ("Nothing on Acme in the channel in the last two weeks.", "anything on Acme in the channel?"),
        ("There are no PoCs at Underdog AI on the sheet.", "who are the PoCs at Underdog AI?"),
        ("LinkedIn: not checked yet", "LinkedIn for Ada?"),
        # 5. lists and non-prose
        ("• Ada Lovelace — CTO\n• Sam Lee", "who are the PoCs?"),
        ("1. Chase Acme\n2. Send deck", "what are the objectives?"),
        ('{"ok": true}', "what are the objectives?"),
        # 6. nothing would be left
        ("Sure.", "can you add them?"),
        ("Here's what I found.", "anything?"),
        ("Okay.", ""), ("ok", ""), ("Noted.", ""), ("Nothing on today.", "what's on today?"),
        # 7. a yes/no answer is made of the question's words
        ("Acme is on hold.", "is Acme on hold?"),
        ("Acme is on hold. Until the pilot is done.", "is Acme on hold?"),
        # a new content word is an answer, not an echo
        ("Kushal owns Acme. He took it over in August.", "who owns Acme?"),
        # a choice question's answer names one of its options
        ("Acme replied. Globex hasn't.", "Acme or Globex, who replied?"),
        ("Acme is on hold (Sales Bot Discussion, 2 Sep).", "where are we with Acme?"),
        ("Based on the tracker, the notes and the channel, Acme is quiet.", "where is Acme?"),
        # a name line over a person's own lines is a heading, not the question said back
        ("Sigil Wen\n• LinkedIn: not checked yet\n• Research profile: not found in public search",
         "research profiles for Sigil Wen"),
        ("Sigil Wen:\n• LinkedIn: not checked yet", "research profiles for Sigil Wen"),
        ("Janajit Bagchi\n• LinkedIn: not checked yet\n\nSuryansh Shukla\n• LinkedIn: not checked yet",
         "LinkedIn and research profile links for Janajit Bagchi and Suryansh Shukla (ARTPARK India)"),
        ("**Janajit Bagchi**\n• LinkedIn: not checked yet", "LinkedIn for Janajit Bagchi"),
        # ...even when the question was nothing but the name
        ("Sigil Wen\n• LinkedIn: not checked yet", "Sigil Wen?"),
        ("Sigil Wen\n• LinkedIn: not checked yet", "sigil wen"),
        ("Janajit Bagchi\n• LinkedIn: not checked yet", "@Saley Janajit Bagchi"),
        ("Janajit Bagchi\n• LinkedIn: https://example.com/in/jb\n\nSuryansh Shukla\n"
         "• LinkedIn: not checked yet", "LinkedIn for Janajit Bagchi and Suryansh Shukla"),
        ("Acme AI:\n• Ada", "Acme AI?"),
        ("Underdog AI PoCs\n• Ada Lovelace, CTO", "who are the PoCs at Underdog AI?"),
        # a name the question did not have goes out with the frame that carries it
        ("You asked about the deal with Acme, which replied on 12 Aug.", "any news on that deal?"),
        ("You asked about Acme and Globex. Both replied last week.", "what about them?"),
        ("To answer your question about Initech, they are on hold. More soon.",
         "what about them?"),
        ("Here's what Vaishnavi sent:\nThe deck and the pricing.", "what went out?"),
    )
    for reply, question in keeps:
        check(f"untouched: {reply[:44]!r}", clean(reply, question=question)[0], reply)

    print("\nprotected sentences, and the :kept report")
    said = "I can't reach the sales meeting notes right now."
    check("a protected sentence is untouched",
          clean(said + " Sure! More.", question="notes?", protect=(said,))[0],
          said + " Sure! More.")
    check("a protected sentence after an opener stays",
          clean("Sure! " + said, question="notes?", protect=(said,))[0], said)
    check("unsafe to remove is reported as kept",
          rules("Based on the Sales Bot Discussion of 2 Sep, Acme is on hold."),
          ["based-on:kept"])
    check("a lone interjection is reported as kept", rules("Sure."), ["interjection:kept"])
    check("at most three removals",
          len(rules("Sure! Sure! Sure! Sure! Acme replied.")), MAX_REMOVALS)

    print("\nbanned_opener and restates")
    check("interjection", banned_opener("Sure! Acme replied."), "interjection")
    check("lead-in", banned_opener("Here is the list:\n• a"), "lead-in")
    check("based-on", banned_opener("Based on the sheet, yes."), "based-on")
    check("echo-frame", banned_opener("You asked about Acme. It's on hold."), "echo-frame")
    check("behind an address", banned_opener("Kushal, here's what I found:\n• a"), "lead-in")
    check("a plain answer has none", banned_opener("Acme replied on 12 Aug."), "")
    check("'Sure enough' is not the interjection", banned_opener("Sure enough it went."), "")
    check("restates", restates("what are the sales objectives for today?",
                               "The sales objectives for today are:\n1. x"), True)
    check("a yes/no answer does not restate",
          restates("is Acme on hold?", "Acme is on hold."), False)

    print("\nnever raises, never empties")
    for odd in ("", "   ", None, "\n\n", ":", "Sure", "Here's", "Based on", "—", "(", "**"):
        got, log = clean(odd, question="where is Acme?")
        check(f"clean({odd!r}) is returned as it came", got, odd)

    print("\nALL PASSED" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

"""TONE SETTINGS — how Saley sounds, changeable without a restart.

Five dials. Strategy section 9 says tone should be "adjustable in configuration
without changing this file", and this is that configuration.

    SALEY_WARMTH     low | medium | high        (high)
    SALEY_FORMALITY  casual | balanced | formal (balanced)
    SALEY_EMOJI      none | light | expressive  (light — at most one)
    SALEY_LENGTH     short | medium             (short — 1-3 sentences)
    SALEY_HUMOUR     off | light                (off)

READ FROM THE ENVIRONMENT ON EVERY COMPOSE, not once at import. That is the
whole point: somebody changes SALEY_WARMTH, the next message is warmer, and
nobody restarts anything. `config` caches its values at import — deliberately,
for everything that must not change mid-run — so tone reads `os.environ`
directly and is the one exception.

    THE ENV IS THE SOURCE, THE CONFIG DEFAULT IS THE FALLBACK. An unset or
    unrecognised value logs once and falls back rather than raising: a typo in a
    tone dial must not stop the bot talking.

TWO OF THE FIVE ARE ENFORCED IN CODE, NOT JUST ASKED FOR. SALEY_EMOJI and
SALEY_LENGTH go into the prompt AND into `llm._proactive_problem`, which rejects
a composed message that breaks them and sends the deterministic template
instead. The other three are prompt-only: a message that is 10% too formal is
still a good message, and rejecting it would cost more than it saved.

    THAT ENFORCEMENT REPLACED TWO BLUNTER RULES. The checker used to ban EVERY
    emoji (`ord > 0x2100`) and cap length at 600 characters. The first made
    SALEY_EMOJI=light impossible by construction; the second is a byte count,
    which is not what "1-3 sentences" means — 600 characters is comfortably four
    long ones.
"""
import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

WARMTH_VALUES = ("low", "medium", "high")
FORMALITY_VALUES = ("casual", "balanced", "formal")
EMOJI_VALUES = ("none", "light", "expressive")
LENGTH_VALUES = ("short", "medium")
HUMOUR_VALUES = ("off", "light")

DEFAULTS = {
    "warmth": "high",
    "formality": "balanced",
    "emoji": "light",
    "length": "short",
    "humour": "off",
}

_ALLOWED = {
    "warmth": WARMTH_VALUES,
    "formality": FORMALITY_VALUES,
    "emoji": EMOJI_VALUES,
    "length": LENGTH_VALUES,
    "humour": HUMOUR_VALUES,
}

_ENV = {
    "warmth": "SALEY_WARMTH",
    "formality": "SALEY_FORMALITY",
    "emoji": "SALEY_EMOJI",
    "length": "SALEY_LENGTH",
    "humour": "SALEY_HUMOUR",
}

# How many of each thing the hard checks allow.
EMOJI_BUDGET = {"none": 0, "light": 1, "expressive": 3}
SENTENCE_BUDGET = {"short": 3, "medium": 6}

# Warned about once per bad value, not once per message. A typo in a tone dial
# should be visible in the log, not printed forty times a day.
_warned: set = set()


def _read(key: str) -> str:
    """One dial, from the environment, falling back to its default."""
    raw = (os.getenv(_ENV[key], "") or "").strip().lower()
    if not raw:
        return DEFAULTS[key]
    if raw in _ALLOWED[key]:
        return raw
    if (key, raw) not in _warned:
        _warned.add((key, raw))
        log.warning(
            "%s=%r is not one of %s — using the default (%r). Tone is read fresh "
            "on every message, so fixing the value takes effect immediately.",
            _ENV[key], raw, "/".join(_ALLOWED[key]), DEFAULTS[key],
        )
    return DEFAULTS[key]


def settings() -> dict:
    """All five dials, read NOW. The only way tone is obtained."""
    return {key: _read(key) for key in _ENV}


# What each value asks the model for, in words rather than adjectives. "Warm"
# means nothing to a composer; "open by using their first name" does.
_WARMTH_TEXT = {
    "low": (
        "WARMTH: LOW. Be courteous and brief. Skip the pleasantries — no thanks "
        "unless something was actually done, no acknowledgement of how busy "
        "anyone is. Get to the thing."
    ),
    "medium": (
        "WARMTH: MEDIUM. Sound like a colleague, not a notification. A short "
        "acknowledgement is fine where it is earned; do not manufacture one."
    ),
    "high": (
        "WARMTH: HIGH. Sound like somebody on the team who noticed and cared "
        "enough to mention it. Use their first name. Thank them where a thank "
        "you is earned, notice good news when there is some, and make the ask "
        "feel like a favour between colleagues rather than a task assignment. "
        "Warmth is not padding: it is one clause, not an extra sentence."
    ),
}

_FORMALITY_TEXT = {
    "casual": (
        "FORMALITY: CASUAL. Contractions, everyday words, the way people "
        "actually type in a team channel. \"Worth a look when you get a "
        "window\" rather than \"please review at your earliest convenience\"."
    ),
    "balanced": (
        "FORMALITY: BALANCED. Professional but not stiff. Contractions are "
        "fine. No corporate register, no \"kindly\", no \"please be advised\"."
    ),
    "formal": (
        "FORMALITY: FORMAL. Full words rather than contractions, complete "
        "sentences, no slang. Still plain English — formal does not mean "
        "bureaucratic, and it never means longer."
    ),
}

_EMOJI_TEXT = {
    "none": "EMOJI: NONE. Do not use a single emoji. Not one, not as decoration.",
    "light": (
        "EMOJI: LIGHT. AT MOST ONE emoji in the whole message, and only where it "
        "genuinely adds something — a small acknowledgement of good news, "
        "usually. A message with none is completely fine and is the common case. "
        "Never open with one and never use one to decorate a request."
    ),
    "expressive": (
        "EMOJI: EXPRESSIVE. Up to three, where they carry meaning. Still never "
        "as decoration and never in place of words."
    ),
}

_LENGTH_TEXT = {
    "short": (
        "LENGTH: SHORT. ONE TO THREE SENTENCES. Not a paragraph, not a summary "
        "with a request at the end. If it does not fit in three sentences, the "
        "message is trying to do two things and one of them belongs on another "
        "day."
    ),
    "medium": (
        "LENGTH: MEDIUM. Up to six sentences, and only use them when the thing "
        "genuinely needs them. Shorter is still better."
    ),
}

_HUMOUR_TEXT = {
    "off": (
        "HUMOUR: OFF. No jokes, no wordplay, no wry asides. Friendly is not the "
        "same as funny, and a joke in a nudge somebody is behind on reads badly."
    ),
    "light": (
        "HUMOUR: LIGHT. A dry aside is allowed where it is genuinely warm and "
        "never at anybody's expense. If you are not sure it lands, leave it out."
    ),
}

# THE HUMAN TOUCHES, requested as behaviour rather than as adjectives. These are
# constant across tone settings — they are what makes a message read as written
# by a person, and none of them is a matter of taste.
HUMAN_TOUCHES = """HUMAN TOUCHES (these apply at every tone setting):

- USE FIRST NAMES. "Vaishnavi — ..." not "Hi team" and not "@Vaishnavi, please".
- VARY THE OPENING. The openers used in the last few messages are listed below;
  do not start with any of them again. Repeating an opening is the single
  clearest tell that a human is not writing these.
- ACKNOWLEDGE A REPLY. If this message follows something somebody told you,
  thank them for it in the same breath as the next ask — once, briefly.
- NOTICE GOOD NEWS. A meeting that got booked, a deal that moved, a deliverable
  that landed: say so in one clause before moving on. Do not manufacture it.
- ALWAYS OFFER AN EASY OUT. End somewhere the person can step off without
  guilt: "no rush", "happy to check back Thursday", "if it is handled just say",
  "your call". A nudge with no exit is a demand, and people stop reading
  demands."""


def prompt_block(*, recent_openers: Optional[list] = None) -> str:
    """The tone instructions for one compose call. Read fresh, every time."""
    s = settings()
    parts = [
        "=== TONE (read fresh on every message; these are settings, not "
        "suggestions) ===",
        _WARMTH_TEXT[s["warmth"]],
        _FORMALITY_TEXT[s["formality"]],
        _EMOJI_TEXT[s["emoji"]],
        _LENGTH_TEXT[s["length"]],
        _HUMOUR_TEXT[s["humour"]],
        "",
        HUMAN_TOUCHES,
    ]
    openers = [o for o in (recent_openers or []) if str(o).strip()]
    if openers:
        parts += [
            "",
            "OPENINGS ALREADY USED IN THE LAST FEW MESSAGES — do not start with "
            "any of these, or with a near-variant:",
            "\n".join(f"  - {o}" for o in openers),
        ]
    parts += [
        "",
        "THE TWO THAT ARE CHECKED IN CODE. A message that breaks either is "
        "thrown away and a plain template is sent instead, so they are not "
        "advisory: at most "
        f"{EMOJI_BUDGET[s['emoji']]} emoji, and at most "
        f"{SENTENCE_BUDGET[s['length']]} sentences.",
    ]
    return "\n".join(parts)


# -- the hard checks ----------------------------------------------------------
#
# EMOJI ARE COUNTED, NOT BANNED. The old rule rejected any character above
# U+2100, which made SALEY_EMOJI=light impossible by construction. Counting lets
# the setting mean what it says — and `none` still rejects every one.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"      # pictographs, emoticons, symbols
    "\U00002600-\U000027BF"      # misc symbols and dingbats
    "\U0001F000-\U0001F2FF"      # tiles, enclosed characters
    "\U00002190-\U000021FF"      # arrows
    "\U00002B00-\U00002BFF"      # misc symbols and arrows
    "️⃣"               # variation selector, keycap
    "]"
)

# Sentence ends. Deliberately crude and deliberately generous: a decimal, an
# abbreviation or an ellipsis must not read as three sentences and get a good
# message thrown away. The check exists to catch a five-paragraph essay, not to
# adjudicate punctuation.
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")


def count_emoji(text: str) -> int:
    return len(_EMOJI_RE.findall(str(text or "")))


def count_sentences(text: str) -> int:
    """How many sentences, roughly. Zero-length text is zero.

    An ellipsis counts once, not three times, and a trailing fragment with no
    terminator still counts as a sentence — otherwise "Worth a look" would be
    zero sentences and pass a limit of zero.
    """
    body = re.sub(r"\.{2,}", ".", str(text or "").strip())
    if not body:
        return 0
    ends = len(_SENTENCE_END_RE.findall(body))
    # A trailing fragment with no terminator is still a sentence.
    if not _SENTENCE_END_RE.search(body[-2:]):
        ends += 1
    return max(1, ends)


def check(text: str) -> str:
    """Why this message breaks the ENFORCED tone settings, or "".

    Only the two that are enforced. Warmth, formality and humour are prompt-only
    on purpose: a message that is 10% too formal is still a good message, and
    throwing it away would cost more than it saved.
    """
    s = settings()
    emoji = count_emoji(text)
    allowed = EMOJI_BUDGET[s["emoji"]]
    if emoji > allowed:
        return (
            f"{emoji} emoji but SALEY_EMOJI={s['emoji']} allows {allowed}"
        )
    sentences = count_sentences(text)
    cap = SENTENCE_BUDGET[s["length"]]
    if sentences > cap:
        return (
            f"{sentences} sentences but SALEY_LENGTH={s['length']} allows {cap}"
        )
    return ""


def describe() -> str:
    """One line for the boot log and for "what are your tone settings"."""
    s = settings()
    return (
        f"warmth={s['warmth']} formality={s['formality']} emoji={s['emoji']} "
        f"(<={EMOJI_BUDGET[s['emoji']]}) length={s['length']} "
        f"(<={SENTENCE_BUDGET[s['length']]} sentences) humour={s['humour']}"
    )


def opener_of(text: str, *, words: int = 4) -> str:
    """The first few words of a message, as the key the no-repeat rule uses.

    NORMALISED, AND THE NAME IS STRIPPED. "Vaishnavi — worth a look at Acme" and
    "Kushal — worth a look at Borealis" are the SAME opening wearing two names,
    and a rule that treated them as different would let the bot use one shape
    every day forever.
    """
    body = str(text or "").strip()
    # Drop a leading "<@id>" tag block and a leading "Name —" address.
    body = re.sub(r"^(?:<@!?\d+>\s*)+", "", body)
    body = re.sub(r"^[A-Z][a-zA-Z'\-]{1,20}\s*[—\-–:,]\s*", "", body)
    body = re.sub(r"[^a-z0-9 ]+", " ", body.lower())
    tokens = [t for t in body.split() if t][:max(1, int(words))]
    return " ".join(tokens)


def _self_test() -> int:
    """`python -m tone` — the dials, the counters and the opener key."""
    logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")
    failures = 0

    def check_(name, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")

    for key in _ENV.values():
        os.environ.pop(key, None)

    print("defaults")
    check_("all five defaults", settings(), DEFAULTS)
    check_("emoji budget follows the default", EMOJI_BUDGET[settings()["emoji"]], 1)
    check_("length budget follows the default",
           SENTENCE_BUDGET[settings()["length"]], 3)

    print("\nread from the env, fresh, every call")
    os.environ["SALEY_WARMTH"] = "low"
    check_("a changed dial is seen immediately", settings()["warmth"], "low")
    os.environ["SALEY_WARMTH"] = "HIGH"
    check_("case does not matter", settings()["warmth"], "high")
    os.environ["SALEY_WARMTH"] = "toasty"
    check_("a bad value falls back", settings()["warmth"], "high")
    del os.environ["SALEY_WARMTH"]
    check_("unset falls back", settings()["warmth"], "high")

    print("\ncounting emoji")
    for text, n in (("no emoji here", 0), ("nice one \U0001F44D", 1),
                    ("\U0001F44D \U0001F389", 2), ("", 0),
                    ("arrows → count", 1), ("plain -- dashes do not", 0)):
        check_(f"count_emoji({text!r})", count_emoji(text), n)

    print("\ncounting sentences")
    for text, n in (("One.", 1), ("One. Two.", 2), ("One. Two. Three.", 3),
                    ("No terminator", 1), ("", 0),
                    ("Worth a look... no rush.", 2),
                    # A DECIMAL IS NOT A SENTENCE BREAK: "3.5" has no space
                    # after the point, so the terminator rule skips it. Without
                    # that, a message quoting a deal size would be thrown away
                    # for being too long.
                    ("It is 3.5 metres. Fine.", 2),
                    ("60% closure. Worth a push.", 2)):
        check_(f"count_sentences({text!r})", count_sentences(text), n)

    print("\nthe enforced checks")
    os.environ["SALEY_EMOJI"] = "none"
    check_("none rejects one emoji", bool(check("nice \U0001F44D")), True)
    os.environ["SALEY_EMOJI"] = "light"
    check_("light allows one", check("nice \U0001F44D"), "")
    check_("light rejects two", bool(check("\U0001F44D \U0001F389 nice")), True)
    check_("...and says why", "SALEY_EMOJI=light allows 1" in
           check("\U0001F44D \U0001F389 nice"), True)
    os.environ["SALEY_EMOJI"] = "expressive"
    check_("expressive allows three", check("\U0001F44D \U0001F389 \U0001F680 a"), "")
    del os.environ["SALEY_EMOJI"]

    os.environ["SALEY_LENGTH"] = "short"
    check_("short allows three sentences", check("One. Two. Three."), "")
    check_("short rejects four", bool(check("One. Two. Three. Four.")), True)
    check_("...and says why", "SALEY_LENGTH=short allows 3" in
           check("One. Two. Three. Four."), True)
    os.environ["SALEY_LENGTH"] = "medium"
    check_("medium allows six", check("a. b. c. d. e. f."), "")
    check_("medium rejects seven", bool(check("a. b. c. d. e. f. g.")), True)
    del os.environ["SALEY_LENGTH"]

    print("\nthe prompt block")
    block = prompt_block(recent_openers=["worth a look at", "quick one on"])
    check_("names the warmth setting", "WARMTH: HIGH" in block, True)
    check_("names the emoji rule", "AT MOST ONE emoji" in block, True)
    check_("names the length rule", "ONE TO THREE SENTENCES" in block, True)
    check_("carries the human touches", "USE FIRST NAMES" in block, True)
    check_("lists the recent openers", "worth a look at" in block, True)
    check_("says which two are enforced", "thrown away" in block, True)
    check_("no openers -> no opener section",
           "ALREADY USED" in prompt_block(), False)

    print("\nthe opener key")
    check_("strips a tag and a name",
           opener_of("<@111> <@222>\nVaishnavi — worth a look at Acme when you can"),
           "worth a look at")
    check_("the same shape with another name is the SAME opener",
           opener_of("Kushal — worth a look at Borealis"),
           opener_of("Vaishnavi — worth a look at Acme"))
    check_("a different shape is different",
           opener_of("Vaishnavi — quick one on Acme") ==
           opener_of("Vaishnavi — worth a look at Acme"), False)
    check_("empty is empty", opener_of(""), "")

    print(f"\n{'ALL PASSED' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())

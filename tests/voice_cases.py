"""NFT2-1064 — the shared tables for the answer-voice checks.

Imported by tests/test_answer_voice.py and verify_answer_voice.py so the two cannot
drift. Written from docs/plans/NFT2-1064.md (sections 2.4, 5.1, 5.2), NOT from the
builder's code: the oracles below (fact tokens, gap words, yes/no, budgets) are the
plan's definitions re-stated independently, so a guard that agrees with itself but
not with the plan is caught here.

No model, no network, no config. Pure data and small helpers.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixtures", "tone_outputs.json")
PINS_1065 = os.path.join(HERE, "fixtures", "nft2_1065_pins.json")
SHA_BEFORE = os.path.join(HERE, "fixtures", "prompt_before_sha.json")

THRESHOLD = 0.6                 # the plan's ECHO_THRESHOLD; the module's own value is checked equal

# -- the plan's definitions, re-stated -------------------------------------------------

GAP_WORDS = {"no", "not", "cannot", "nothing", "none", "nobody", "never", "neither", "without",
             "yet", "only", "both", "all", "every", "each", "still", "already", "yes", "yep",
             "nope", "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
             "nine", "ten"}
YES_NO_STARTS = {"is", "are", "was", "were", "do", "does", "did", "has", "have", "had", "can",
                 "could", "will", "would", "should", "shall", "may", "might", "am", "any"}


def words(text):
    return re.findall(r"[A-Za-z0-9'’]+", text.lower())


def has_gap_word(sentence):
    return any(w in GAP_WORDS or w.endswith("n't") for w in words(sentence))


def has_fact_token(sentence, question=""):
    """A digit, a URL or masked link, anything in round brackets, **bold**, a mention,
    '@' or a currency sign that is NOT also in the question."""
    toks = re.findall(r"\d+|https?://\S+|\[[^\]]*\]\(<?[^)]*>?\)|\([^)]*\)|\*\*[^*]+\*\*|<@!?\d+>|@\w*|[$€£₹]",
                      sentence)
    return any(t not in question for t in toks)


def is_yes_no(question):
    q = re.sub(r"<@!?\d+>|@\w+", " ", question.lower())
    ws = words(q)
    while ws and ws[0] in ("hey", "hi", "please"):
        ws = ws[1:]
    return bool(ws) and ws[0] in YES_NO_STARTS


def is_point(line):
    return bool(re.match(r"\s*(?:[-*•]\s|\d+[.)]\s)", line))


def lines_of(text):
    return [l for l in text.splitlines() if l.strip()]


def first_sentence_oracle(text):
    """First line, cut at the first . ! ? followed by space/end. Close enough for the
    fixtures (none uses a mid-line colon as a sentence end)."""
    first = lines_of(text)[0] if lines_of(text) else ""
    m = re.search(r"[.!?](?:\s|$)", first)
    s = first[: m.start()] if m else first
    return s.rstrip(":").strip()


# -- the length budgets (plan 5.2) -----------------------------------------------------

def budget_problems(case, text):
    """[] when `text` (post-guard, before any Sources block) fits the budget of its type."""
    t, items, people = case["type"], min(int(case.get("items") or 0), 20), int(case.get("people") or 0)
    ls, chars = lines_of(text), len(text)
    out = []
    caps = {
        "fact": (3, 400), "gap": (2, 300), "list": (items + 2, 1500),
        "profile": (4 * people + 1, 1400), "news": ((items or 5) + 1, 1500),
        "summary": (8, 900), "social": (2, 220), "capability": (8, 1200),
    }
    max_lines, max_chars = caps[t]
    if len(ls) > max_lines:
        out.append(f"{len(ls)} lines > {max_lines}")
    if chars > max_chars:
        out.append(f"{chars} chars > {max_chars}")
    if chars > 1800:
        out.append(f"{chars} chars > 1800 (one Discord message)")
    if t == "list":
        for l in ls:
            if is_point(l) and len(l) > 160:
                out.append(f"item line {len(l)} chars > 160")
    return out


def load_cases():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)["cases"]


def case_problems(case, rg):
    """Every check the plan 5.2 lists, for one recorded case, against the module `rg`
    (replyguard). Returns a list of strings; empty means the case is clean."""
    q, bad = case["question"], []
    texts = {"before": rg.clean(case["before"], question=q)[0]}
    if case.get("after") is not None:
        texts["after"] = rg.clean(case["after"], question=q)[0]
    for label, text in texts.items():
        if rg.banned_opener(text):
            bad.append(f"{label}: banned opener {rg.banned_opener(text)!r} survives the guard")
        if rg.restates(q, text):
            bad.append(f"{label}: still restates the question")
        first = rg.first_sentence(text)
        if (not is_yes_no(q) and not has_fact_token(first, q) and not has_gap_word(first)
                and not is_point(first) and rg.echo_score(q, first) >= rg.ECHO_THRESHOLD):
            bad.append(f"{label}: echo score {rg.echo_score(q, first):.2f} >= {rg.ECHO_THRESHOLD}")
    if "after" in texts:
        bad += [f"after: budget: {p}" for p in budget_problems(case, texts["after"])]
    if case["source"] in ("synthetic",):
        _, fired_b = rg.clean(case["before"], question=q)
        got = [f["rule"] for f in fired_b]
        if got != case["expect_guard_on_before"]:
            bad.append(f"guard on before fired {got}, wanted {case['expect_guard_on_before']}")
        out_a, fired_a = rg.clean(case["after"], question=q)
        if fired_a or out_a != case["after"]:
            bad.append(f"guard touched an after text: fired {fired_a}")
    if case["source"] == "oct6-verbatim":
        out_b, fired = rg.clean(case["before"], question=q)
        if out_b != case["before"] or fired:
            bad.append(f"guard changed a verbatim 6 Oct line: {out_b!r} {fired}")
    return bad


# -- guard tables (plan 2.4, 5.1 V7 / V8) -----------------------------------------------
# (question, reply, expected text, expected rule labels in order)

STRIPS = [
    ("where are we with Acme?", "Sure! Acme replied on 12 Aug.", "Acme replied on 12 Aug.", ["interjection"]),
    ("who are the PoCs at Acme?", "Great question. Two on the sheet:\n• Ada Lovelace — CTO\n• Sam Lee — Head of Research",
     "Two on the sheet:\n• Ada Lovelace — CTO\n• Sam Lee — Head of Research", ["interjection"]),
    ("who is on the sheet?", "Here's what I found:\n• Ada Lovelace — CTO\n• Sam Lee — Head of Research",
     "• Ada Lovelace — CTO\n• Sam Lee — Head of Research", ["lead-in"]),
    ("top 5 AI headlines", "Here are the top 5 AI headlines:\n• One — [Wire](<https://example.com/1>)\n• Two — [Wire](<https://example.com/2>)",
     "• One — [Wire](<https://example.com/1>)\n• Two — [Wire](<https://example.com/2>)", ["lead-in"]),
    ("where are we with Acme?", "Based on the tracker, Acme is at DM sent.", "Acme is at DM sent.", ["based-on"]),
    ("who are the PoCs at Acme?", "You asked about the PoCs at Acme. Two on the sheet:\n• Ada Lovelace — CTO\n• Sam Lee — Head of Research",
     "Two on the sheet:\n• Ada Lovelace — CTO\n• Sam Lee — Head of Research", ["echo-frame"]),
    ("is Acme on hold?", "To answer your question, Acme is on hold (Sales Bot Discussion, 2 Sep).",
     "Acme is on hold (Sales Bot Discussion, 2 Sep).", ["echo-frame"]),
    ("what are the sales objectives for today?",
     "The sales objectives for today are:\n1. Chase Globex on pricing\n2. Send the Acme deck",
     "1. Chase Globex on pricing\n2. Send the Acme deck", ["restated-question"]),
    ("where are we with Acme?", "Sure! Here's what I found:\nAcme replied.", "Acme replied.", ["interjection", "lead-in"]),
    ("where are we with Acme?", "Certainly! Acme is on hold.", "Acme is on hold.", ["interjection"]),
    ("where are we with Globex?", "Of course, Globex is on hold.", "Globex is on hold.", ["interjection"]),
    ("where are we with Globex?", "Absolutely — Globex is on hold.", "Globex is on hold.", ["interjection"]),
    ("where are we with Acme?", "Good question! Acme is at DM sent.", "Acme is at DM sent.", ["interjection"]),
    ("where are we with Acme?", "Sure thing! Acme is at DM sent.", "Acme is at DM sent.", ["interjection"]),
    ("what is open?", "Below is the list:\n• Send the deck\n• Chase Globex", "• Send the deck\n• Chase Globex", ["lead-in"]),
    ("what is open?", "Below are the open items:\n• Send the deck\n• Chase Globex", "• Send the deck\n• Chase Globex", ["lead-in"]),
    ("what is open?", "I found the following:\n• Send the deck\n• Chase Globex", "• Send the deck\n• Chase Globex", ["lead-in"]),
    ("what is open?", "This is what I found:\n• Send the deck\n• Chase Globex", "• Send the deck\n• Chase Globex", ["lead-in"]),
    ("what is open?", "Here you go:\n• Send the deck\n• Chase Globex", "• Send the deck\n• Chase Globex", ["lead-in"]),
    ("what is open?", "Here's the open list:\n• Send the deck\n• Chase Globex", "• Send the deck\n• Chase Globex", ["lead-in"]),
    ("where are we with Acme?", "In response to your question, Acme is at DM sent.", "Acme is at DM sent.", ["echo-frame"]),
    ("where are we with Acme?", "Based on the meeting notes: Acme is on hold.", "Acme is on hold.", ["based-on"]),
    ("who owns Acme?", "You're asking who owns Acme. Kushal does.", "Kushal does.", ["echo-frame"]),
    ("where are we with Acme?", "Sure! Great question! Here's what I found:\nAcme replied.", "Acme replied.",
     ["interjection", "interjection", "lead-in"]),
    ("where are we with Acme?", "Sure! Sure! Sure! Sure! Acme replied.", "Sure! Acme replied.",
     ["interjection", "interjection", "interjection"]),
]

# replies the guard must hand back byte for byte: (question, reply, protect, why)
KEEPS = [
    ("where are we with Acme?", "Acme replied on 12 Aug. Here is the deck.", (), "1 only the opening is touched"),
    ("where are we with Acme?", "Acme is on hold. Sure! Great.", (), "1 nothing mid-text"),
    ("where are we with Acme?", "Based on the Sales Bot Discussion of 2 Sep, Acme is on hold.", (), "2 new fact token in the opener"),
    ("where are we with Acme?", "Here's the call (Sales Bot Discussion, 2 Sep):\nAcme is on hold.", (), "2 citation in the lead-in"),
    ("who are the PoCs at Acme?", "Here are the 3 PoCs on the sheet:\n• Ada\n• Sam\n• Lee", (), "2 a number the question did not have"),
    ("where are we with Acme?", "Here's the page: [Acme](<https://example.com/acme>)", (), "2 a link"),
    ("where are we with Acme?", "Here's **Acme**:\nreplied on 12 Aug.", (), "2 a bold name"),
    ("anything on Acme in the channel?", "Nothing on Acme in the channel in the last two weeks.", (), "3 gap word"),
    ("who are the PoCs at Underdog AI?", "There are no PoCs at Underdog AI on the sheet.", (), "3 gap word"),
    ("what about LinkedIn for Ada?", "LinkedIn: not checked yet.", (), "3 not checked yet"),
    ("where are we with Acme?", "I can't see the pipeline sheet yet.", (), "3 gap line"),
    ("what meeting notes do you have?", "Meeting notes aren't connected to me yet.", ("Meeting notes aren't connected to me yet.",), "4 protected"),
    ("what is in the notes folder?", "There are no sales meeting notes in the Sales Notes folder yet.",
     ("There are no sales meeting notes in the Sales Notes folder yet.",), "4 protected"),
    ("can you read the notes?", "I can't reach the sales notes folder right now, so I haven't checked the notes.",
     ("I can't reach the sales notes folder right now, so I haven't checked the notes.",), "4 protected"),
    ("where are we with Acme?", "• Sure! Acme replied.", (), "5 a point line first"),
    ("where are we with Acme?", "1. Sure! Acme replied.", (), "5 a numbered line first"),
    ("do that", "{\"ok\": true, \"note\": \"Sure! done\"}", (), "5 starts with {"),
    ("where are we with Acme?", "[Acme](<https://example.com/acme>) is on hold. Sure!", (), "5 starts with ["),
    ("show the snippet", "```\nSure! here's the code\n```", (), "5 code fence"),
    ("where are we with Acme?", "Sure.", (), "6 only an interjection"),
    ("where are we with Acme?", "Sure!", (), "6 only an interjection"),
    ("where are we with Acme?", "Here's what I found:", (), "6 only a lead-in"),
    ("is that ok?", "Okay.", (), "6 matches no rule"),
    ("is that ok?", "ok", (), "6 matches no rule"),
    ("is that ok?", "Noted.", (), "6 matches no rule"),
    ("what's on today?", "Nothing on today.", (), "6 matches no rule"),
    ("is Acme on hold?", "Acme is on hold.", (), "7 yes/no never G5"),
    ("is Globex on hold?", "Globex is on hold.", (), "7 yes/no never G5"),
    ("can we send the deck?", "We can send the deck.", (), "7 yes/no never G5"),
    ("who owns Acme?", "Kushal owns Acme.", (), "G5 needs EVERY word to be the question's: kushal is new"),
]

KEEPS += [
    ("any news on that deal?", "You asked about the deal with Acme, which replied on 12 Aug.", (), "builder: a name the question lacks"),
    ("what about them?", "You asked about Acme and Globex. Both replied last week.", (), "builder: names the question lacks"),
    ("what about them?", "To answer your question about Initech, they are on hold. More soon.", (), "builder: a name in the comma frame"),
    ("what went out?", "Here's what Vaishnavi sent:\nThe deck and the pricing.", (), "builder: a name in the lead-in"),
]

STRIPS += [
    ("top 5 AI headlines", "Top 5 AI headlines:\n• One", "• One", ["restated-question"]),
    ("what are the sales objectives for today?", "Sales objectives for today:\n1. Chase Acme", "1. Chase Acme", ["restated-question"]),
    ("which deals are on hold?", "Deals on hold:\n• Acme", "• Acme", ["restated-question"]),
]

_SW = "Sigil Wen\n• LinkedIn: not checked yet"
KEEPS += [
    ("research profiles for Sigil Wen", "Sigil Wen\n• LinkedIn: not checked yet\n• Research profile: not found in public search", (), "name header"),
    ("research profiles for Sigil Wen", "Sigil Wen:\n• LinkedIn: not checked yet", (), "name header with colon"),
    ("Sigil Wen?", _SW, (), "name header, the question is the name"),
    ("sigil wen", _SW, (), "name header, lower-case question"),
    ("@Saley Janajit Bagchi", "Janajit Bagchi\n• LinkedIn: not checked yet", (), "name header"),
    ("LinkedIn and research profile links for Janajit Bagchi and Suryansh Shukla (ARTPARK India)",
     "Janajit Bagchi\n• LinkedIn: not checked yet\n\nSuryansh Shukla\n• LinkedIn: not checked yet", (), "two people, each name over its lines"),
    ("LinkedIn for Janajit Bagchi and Suryansh Shukla",
     "Janajit Bagchi\n• LinkedIn: https://example.com/in/jb\n\nSuryansh Shukla\n• LinkedIn: not checked yet", (), "two people with a link"),
    ("Acme AI?", "Acme AI:\n• Ada", (), "company header"),
    ("LinkedIn for Janajit Bagchi", "**Janajit Bagchi**\n• LinkedIn: not checked yet", (), "bold name header"),
    ("who are the PoCs at Underdog AI?", "Underdog AI PoCs\n• Ada Lovelace, CTO", (), "three capitalised words are name-like"),
]


NO_RULE_AT_ALL = ["Okay.", "ok", "Noted.", "Nothing on today."]   # banned_opener "" and fired == []


# -- no live source probe: see tests/offline_guard.py ---------------------------------------------

from offline_guard import CANNED_STATUS  # noqa: E402,F401

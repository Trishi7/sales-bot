"""The bot's voice, the STRATEGY it thinks with, and the POLICY it answers under.

Three things live here, and the split matters:

  THE PERSONA (`COS_PERSONA`) — hard-coded voice rules. Direct, brief, no emojis,
  first person. This is style, and style shouldn't need a file edit.

  THE STRATEGY (`sales_strategy.md`) — THE CORE BRAIN. What the team is trying to
  do and how: the motions, the targets, the segments, the judgement calls. It
  goes into the system prompt of EVERY model call this bot makes — answers,
  proactive composition, extraction, research — and it is re-read on each one.

  THE POLICY (`sales_policy.md`) — what the bot is FOR: the role, what it
  enforces, its hard limits. That is Sid's to write and to change, so it lives in
  a markdown file at the repo root and is RE-READ ON EVERY QUERY. Edit the file,
  ask the next question, and the new policy is already in force — no restart, no
  redeploy. Both loaders cache on the file's mtime+size, so re-reading costs a
  stat() call, not a disk read, on every turn.

WHERE THE TWO DOCUMENTS DISAGREE, THE STRATEGY DOC WINS, and the prompt says so
in those words. That precedence is stated rather than engineered because it has
to hold for rules nobody has got round to de-duplicating by hand: a conflict
that survives an edit is resolved the same way as one that was never spotted.

`system_preamble()` is what every reply-path prompt is built on: persona, then
policy, then the live source statuses. The last of those is why the bot can
answer "what can you do" honestly — the statuses come from sources.py, the same
place state/summary.json gets them, so what it tells a person and what it tells
its supervisor cannot drift apart.

Nothing here decides what the bot DOES. The hard guardrails — never DM, never
message anyone off the roster, post only in the sales channels — are enforced in
guardrails.py, in code. A rule that only exists in a prompt is a suggestion.
"""
import logging
import os
from typing import Optional

import config
import sources

log = logging.getLogger(__name__)

# The bot's name — one identity everywhere it speaks. Naming is open; override
# with COS_NAME. Read through config so there is a single definition.
NAME = config.COS_NAME


COS_PERSONA = f"""You are {NAME}, the sales & marketing Chief of Staff for the
NFThing team. You are a colleague in this team's working life — not a bot, not a
form, not a dashboard. You have already done the legwork before you reply.

VOICE (this section governs ONLY how you sound):
- FIRST PERSON, always: "I checked the notes and…", "I don't have the sheet yet."
  Never call yourself "the bot" or "the system", never refer to yourself in the
  third person, and never open with "Here is…" or by restating the question.
- DIRECT AND BRIEF. Sales channels are busy. Lead with the answer, then the
  evidence. No preamble, no padding, no throat-clearing, no summarising what you
  are about to say before you say it.
- NO EMOJIS. Not in answers, not in nudges, not as decoration, not "just one".
- Plain sentences over bullet-point theatre. Use a short list only when you are
  genuinely listing things.
- Say the uncomfortable thing plainly. If outreach has drifted from the plan, if
  a deadline has slipped twice, if the strategy hasn't been touched in two
  months — say so, once, without softening it into meaninglessness and without
  moralising about it.
- Never flatter, never open with praise, never end by asking if that was helpful.

HONESTY (not negotiable, and not a style rule):
- You may only state what a source you can actually READ told you. If a source is
  awaiting access, say so in those words and say what you'd need.
- Never guess a number, a date, a name, or a deal stage. "I don't know" and "I
  can't see that yet" are complete answers.
- If someone asks for something you cannot do, say so in one sentence and stop.
- CITE THE MEETING. Whenever meeting knowledge shapes what you say — a hold, a
  decision, a commitment — the sentence names the meeting and its date, in
  brackets: "Acme is on hold (Sales Bot Discussion, 2 Sep)". Use the citation
  string the tool gave you, verbatim. A claim from a sheet can be checked by
  opening the sheet; a claim from a meeting cannot be checked at all unless you
  say which meeting, so an uncited one is indistinguishable from something you
  made up. If a tool gave you no citation, you do not have the fact.

This persona changes ONLY your voice. It does not change what you are allowed to
do — that is the POLICY below and the guardrails enforced in code."""




# -- the PROACTIVE voice (drip messages and event reminders) ------------------
#
# THE VOICE THE BOT SPEAKS IN WHEN NOBODY ASKED. Different from the answer voice
# and deliberately so: an answer is a response to a question somebody chose to
# ask, and can be as brisk as it likes. A proactive message is an interruption.
# It arrives in the middle of somebody's afternoon and asks them for something,
# and the difference between one that gets acted on and one that gets muted is
# almost entirely tone.
#
# THE EXEMPLARS ARE NOT IN THIS FILE. They live in sales_policy.md, which is
# re-read on every message — so the team can rewrite the bot's voice by editing
# a markdown file, with no restart and no deploy. That is the whole point of
# keeping them there: a voice nobody but a developer can change is a voice that
# never gets fixed.

PROACTIVE_VOICE = """You are writing a PROACTIVE message: nobody asked for it. It
will arrive in the middle of someone's afternoon. Everything below is about
making that welcome rather than annoying.

SOUND LIKE A WARM SALES HEAD who has already looked at the sheet and is
mentioning one thing on the way past. Not a dashboard. Not a ticketing system.

ONE THOUGHT PER MESSAGE. One subject, one person, one ask. If you find yourself
writing "and also", stop — the second thing is a different message on a
different day.

ALWAYS GIVE AN OUT. End somewhere the person can step off without guilt: "no
rush", "tell me when", "if it is handled just say", "your call". A nudge with no
exit is a demand, and people stop reading demands.

THANK WHERE IT IS EARNED, and only there. If something got done, say so once and
move on. Manufactured gratitude for ordinary work is worse than none.

NEVER:
- headers, bullets, bold, or a "Company - status - action" shape. Prose only.
- STACKED IMPERATIVES. "Follow up with Acme. Send the deck. Update the tracker."
  is three demands wearing one message.
- emojis. Not one.
- "just checking in", "circling back to see if", "any update on" as an opener.
  Filler tells the reader you have nothing to say.
- restating what you are about to do before doing it.
- a sign-off, a subject line, or quotes around the message.

LENGTH: one or two sentences. Three at the absolute most, and only when the
third is the out.

NAME EVERY COMPANY YOU ARE GIVEN, comma-separated inside the sentence. Do not
summarise them as "a few accounts" — the person needs to know which.

ADDRESS THE OWNER BY NAME ONCE, at the start, if you are given one. Never twice.

Write the message and nothing else."""


def _exemplars_from_policy(text: str) -> str:
    """The "Voice exemplars" section of sales_policy.md, verbatim.

    Pulled out of the policy rather than duplicated here so there is exactly ONE
    place the team edits the bot's voice. Returns "" when the section is absent
    — the message generator then runs on the rules alone, which is worse but not
    broken, and the miss is logged so a renamed heading is visible.
    """
    if not text:
        return ""
    marker = "### Voice exemplars"
    start = text.find(marker)
    if start < 0:
        return ""
    rest = text[start + len(marker):]
    # Up to the next heading of the same level or higher, or the horizontal rule
    # that closes the Tone section.
    end = len(rest)
    for stop in ("\n## ", "\n### ", "\n---"):
        found = rest.find(stop)
        if found >= 0:
            end = min(end, found)
    return rest[:end].strip()


def proactive_voice_prompt(*, recent_openers=None) -> str:
    """The full system prompt for composing ONE proactive message.

    THE STRATEGY FIRST, then the TONE SETTINGS, then the voice rules, then the
    exemplars read live out of the policy file, then the honesty rules that are
    not negotiable in any voice.

    `recent_openers` is the last few openings the bot used, so the composer can
    be told not to start with any of them again. Repeating an opening is the
    single clearest tell that a human is not writing these.

    The strategy belongs here for the same reason it belongs in an answer: a
    nudge is a claim about what matters this week, and the document that decides
    what matters this week is the plan. A composer that has the voice but not
    the plan writes a warm, well-shaped chase about an account the strategy
    dropped a month ago.
    """
    # TONE FIRST, AFTER THE STRATEGY. The five dials are read from the
    # environment on every call — a change to SALEY_WARMTH is in force on the
    # next message with no restart — and they come before the fixed voice rules
    # so a setting reads as the specific instruction and the voice block as the
    # standing one.
    import tone as _tone

    parts = [
        strategy_preamble(),
        _tone.prompt_block(recent_openers=recent_openers),
        "\n\n",
        PROACTIVE_VOICE,
    ]

    exemplars = _exemplars_from_policy(load_policy())
    if exemplars:
        parts.append(
            "\n\n=== VOICE EXEMPLARS (match their length, warmth and shape — never "
            "their content; these are examples of HOW to write, not WHAT to say) ===\n"
            + exemplars
        )
    else:
        log.warning(
            "[persona] sales_policy.md has no '### Voice exemplars' section, so "
            "proactive messages are composed from the voice rules alone. Check the "
            "heading has not been renamed."
        )

    parts.append(
        "\n\n=== NOT NEGOTIABLE ===\n"
        "Everything you write must be true of the facts you were given. Do not "
        "invent a company, a date, a number or a person. Do not claim anything "
        "happened that you were not told happened. If the facts are thin, say less "
        "— a short honest nudge beats a warm invented one."
    )
    return "".join(parts)


def fallback_proactive_message(text: str) -> str:
    """What goes out when the model is unreachable.

    The caller has already built a complete sentence from a template (see
    `drip.compose_fallback`); this exists so there is one named place that says
    what happens on a model outage. A proactive message must never be lost to an
    API blip — losing polish is acceptable, losing the nudge is not.
    """
    return (text or "").strip()


# -- the strategy doc: THE CORE BRAIN ----------------------------------------
#
# Same mechanism as the policy below, same reasoning, one difference: this one
# is loaded into EVERY model call rather than only the reply paths. A prompt
# that shapes what the bot says about a deal should also shape what it says when
# it writes a nudge about that deal, and the two drifting apart is how a bot
# ends up chasing something the plan dropped last month.

_strategy_cache: dict = {"key": None, "text": ""}

_STRATEGY_MISSING_NOTE = (
    "NOTE FOR YOU: the sales STRATEGY document ({path}) is not readable right "
    "now. It is normally the document you think with, so you are working without "
    "it. DO NOT INVENT A STRATEGY, do not describe targets, motions or segments "
    "as though you had read them, and do not fill the gap from memory. If "
    "someone asks about the plan, say the strategy doc is missing and name the "
    "path so they can fix it."
)

# The marker every strategy block starts with. `LLM._create` and the query
# engine look for it before prepending, so a prompt built from
# `system_preamble()` — which already carries the block — is not given a second
# copy. One string, checked in both places, so the two cannot disagree.
STRATEGY_MARKER = "=== SALES STRATEGY"


def load_strategy(path: Optional[str] = None) -> str:
    """The current text of sales_strategy.md, re-read whenever the file changes.

    Returns "" when there is no readable strategy file — callers get the
    missing-file note from `strategy_preamble()` instead, so a deleted strategy
    degrades into "I can't see the plan" rather than into the bot quietly
    inventing one and stating it with confidence.
    """
    path = (path or config.STRATEGY_DOC_FILE or "").strip()
    if not path:
        return ""
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        if _strategy_cache["key"] is not None:
            log.warning(
                "[persona] strategy file %r became unreadable; dropping the cached copy. "
                "Every prompt from here on says the strategy is missing.", path,
            )
            _strategy_cache.update({"key": None, "text": ""})
        return ""

    if key == _strategy_cache["key"]:
        return _strategy_cache["text"]

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
    except OSError:
        log.exception("[persona] could not read strategy file %r", path)
        return ""

    _strategy_cache.update({"key": key, "text": text})
    log.info("[persona] loaded STRATEGY from %s (%d chars)", path, len(text))
    return text


def strategy_preamble() -> str:
    """The strategy block that fronts EVERY model call. Never empty.

    When the doc is missing this returns the missing-file note instead, because
    silence is the one thing it must not return: a prompt with no strategy block
    at all reads, to a model, exactly like a prompt whose strategy had nothing
    to say.

    TRUNCATION IS DECLARED, NOT HIDDEN. The doc rides in every system prompt, so
    a very long one is paid for on every question and is cut at
    STRATEGY_PROMPT_MAX_CHARS. A model told it is reading a truncated plan can
    say so; one that is not told will answer as though it read the whole thing.
    """
    text = load_strategy()
    if not text:
        return (
            _STRATEGY_MISSING_NOTE.format(
                path=config.STRATEGY_DOC_FILE or "sales_strategy.md"
            )
            + "\n\n"
        )

    limit = int(getattr(config, "STRATEGY_PROMPT_MAX_CHARS", 0) or 0)
    truncated = ""
    if limit and len(text) > limit:
        text = text[:limit]
        truncated = (
            f"\n\n[...TRUNCATED. This document is longer than the {limit} characters "
            "carried in the prompt. You are reading the beginning of it only — say so "
            "if a question turns on a part you cannot see.]"
        )

    return (
        STRATEGY_MARKER + " — THE PLAN YOU THINK WITH (re-read on every call, so "
        "this is always the current version) ===\n"
        "This is the team's sales & marketing strategy: what we are trying to do, "
        "who we are trying to do it with, and how. Reason from it. Quote it when "
        "somebody asks what the plan says.\n\n"
        "PRECEDENCE: WHERE THIS DOCUMENT AND THE SALES POLICY BELOW DISAGREE, THIS "
        "DOCUMENT WINS. The policy governs what you are permitted to do; this "
        "governs what the team is trying to achieve. If a rule appears in both and "
        "they differ, follow this one and say plainly that the policy says "
        "otherwise — do not silently average them.\n\n"
        "IT DOES NOT OVERRIDE THE HARD LIMITS. Never contacting anyone outside the "
        "team, never writing outside the writable window, never stating a number "
        "you did not read — those are enforced in code and no document changes "
        "them.\n\n"
        + text + truncated + "\n\n"
    )


def strategy_status() -> dict:
    """{path, loaded, chars} — what the bot is actually thinking with, so it can
    answer "what are you working from" about its brain and not just its
    sources."""
    path = (config.STRATEGY_DOC_FILE or "").strip()
    text = load_strategy(path)
    return {"path": path, "loaded": bool(text), "chars": len(text)}


# -- the policy file ---------------------------------------------------------

# Cache keyed on (mtime, size) so an edit is picked up on the very next query
# without a restart, while an unchanged file costs one stat() per turn.
_policy_cache: dict = {"key": None, "text": ""}

_POLICY_MISSING_NOTE = (
    "NOTE FOR YOU: the sales policy file is not readable right now, so you are "
    "operating on the persona and the source statuses alone. Do not invent a "
    "policy. If someone asks what you enforce, say the policy file is missing and "
    "that they should check {path}."
)


def load_policy(path: Optional[str] = None) -> str:
    """The current text of sales_policy.md, re-read whenever the file changes.

    Returns "" when there's no readable policy file — callers get the missing-file
    note from `system_preamble()` instead, so a deleted policy degrades into "I
    don't have a policy" rather than into the bot quietly inventing one."""
    path = (path or config.SALES_POLICY_FILE or "").strip()
    if not path:
        return ""
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        if _policy_cache["key"] is not None:
            log.warning("[persona] policy file %r became unreadable; dropping cached copy", path)
            _policy_cache.update({"key": None, "text": ""})
        return ""

    if key == _policy_cache["key"]:
        return _policy_cache["text"]

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
    except OSError:
        log.exception("[persona] could not read policy file %r", path)
        return ""

    _policy_cache.update({"key": key, "text": text})
    log.info("[persona] loaded policy from %s (%d chars)", path, len(text))
    return text


def policy_status() -> dict:
    """{path, loaded, chars} — so the bot can answer "what are you working from"
    about its own configuration, not just about its sources."""
    path = (config.SALES_POLICY_FILE or "").strip()
    text = load_policy(path)
    return {"path": path, "loaded": bool(text), "chars": len(text)}


# -- prompt assembly ---------------------------------------------------------


def cos_preamble() -> str:
    """The voice block, or "" when the persona is disabled. Kept separate from
    the policy: turning the voice off must not turn the policy off."""
    if not config.COS_PERSONA_ENABLED:
        return ""
    return COS_PERSONA + "\n\n"


def system_preamble(*, include_sources: bool = True) -> str:
    """The full front matter for any reply-path system prompt: voice, then the
    STRATEGY, then the live policy, then what the bot can actually see right now.

    Every path that speaks to a human builds on this, so the policy and the
    source statuses can never apply to one reply and not another. `include_sources`
    is False only for prompts that do no source reasoning at all (the commitment
    detector), where the statuses would be noise.
    """
    parts = [cos_preamble()]

    # THE STRATEGY FIRST, THEN THE POLICY. Order is not cosmetic: the precedence
    # rule is stated inside the strategy block and reads as an instruction about
    # what follows it.
    parts.append(strategy_preamble())

    policy = load_policy()
    if policy:
        parts.append(
            "=== SALES POLICY (the operating policy you work under; it is re-read on "
            "every question, so this is always the current version. Where it conflicts "
            "with the STRATEGY above, the strategy wins) ===\n"
            + policy
            + "\n\n"
        )
    else:
        parts.append(
            _POLICY_MISSING_NOTE.format(path=config.SALES_POLICY_FILE or "sales_policy.md")
            + "\n\n"
        )

    if include_sources:
        parts.append(sources.describe_for_prompt() + "\n\n")

    # THE CITATION RULE, restated after the sources rather than only inside the
    # voice block. It is a factual-integrity rule, not a style one, and the
    # commitment detector (include_sources=False) is the one path that does no
    # source reasoning and so has nothing to cite.
    if include_sources:
        parts.append(CITATION_RULE + "\n\n")

    return "".join(parts)


CITATION_RULE = """=== CITING MEETINGS (mandatory) ===
Any line of yours shaped by meeting knowledge — a hold, a decision, a commitment,
anything you learned from a meeting note rather than from a spreadsheet cell —
NAMES THE MEETING AND ITS DATE, in brackets, at the end of that line:

    "Acme is on hold until the pilot lands (Sales Bot Discussion, 2 Sep)"

The tools hand you the exact string in a `citation` field. Use it verbatim; do
not shorten it, do not paraphrase the meeting's name, and do not reconstruct one
from a date. If a fact came from a meeting and you have no citation for it, you
do not have the fact — say you can't see it rather than asserting it uncited.

This is not required for a claim read off the GTM sheets: those already cite the
tab and the cell. It IS required in every other case, including when you are
agreeing with something the person asking already said."""


# -- reply-path prompts ------------------------------------------------------

CAPABILITY_PROMPT = """Someone has asked what you can do, what you have access to,
or what you're for. Answer it HONESTLY from two things and nothing else: the SALES
POLICY above (what you're for and what you enforce) and the SOURCE STATUS block
above (what you can actually see right now).

REQUIREMENTS:
- Say what you can do in plain prose, grounded in the policy. Two or three
  sentences.
- Then STATE THE SOURCE STATUSES EXPLICITLY. Name every source that is AWAITING
  ACCESS and say what it means you can't answer yet — this is the most useful part
  of the reply and you must not skip it or soften it. Someone deciding whether to
  trust your next answer needs to know your blind spots before they ask.
- If the policy file is missing, say that too.
- No emojis. No bulleted command menu. No "just ask me anything!". Do not offer
  capabilities the policy doesn't give you or sources you can't reach.
- Keep it under about 1200 characters."""


SOCIAL_REPLY_PROMPT = f"""You are replying in Discord to a message that isn't an
answerable question — a greeting, a thank-you, small talk, or something too vague
to act on. Reply as {NAME}, in your own voice.

You'll be told the KIND:
- "greeting" — "hi", "morning", "thanks". Answer in ONE line. Greet them by first
  name if you know it, and say in the same breath what you could look into.
- "unclear" — the message is addressed to you but you can't tell what's being
  asked, or you looked and found nothing. Say so plainly, without blame, and
  offer what you can check. Never scold and never imply they used wrong syntax —
  you don't have a syntax.

HARD RULES:
- Sound like a colleague, not a help menu. No command lists, no "Try `...`"
  blocks, no backtick examples.
- SHORT. A greeting gets one line. Never pad.
- NO EMOJIS.
- Be honest about what you can see: don't offer to check something whose source
  is awaiting access, and don't promise to do work, send anything, or contact
  anyone.
- Don't invent facts, names, deals, or numbers — you haven't looked anything up.
- Output ONLY the reply text. No preamble, no code fences, no headers."""


CHASE_NUDGE_PROMPT = f"""You are {NAME}. Someone told a sales channel they'd come
back with something, the time they gave themselves has passed, and nothing has
come back. You are reminding them.

You'll be given: who promised (as a mention token), WHAT they promised, how long
ago, and a link to their message. Write the reminder.

HARD RULES:
- ONE short line. Two at the absolute most. This lands in a busy channel.
- Address them using the EXACT token you're given (e.g. <@123>) — paste it
  verbatim. Do not rewrite it into a name or an @handle of your own, and do not
  add any other mention.
- Reference what they actually said, concretely: "you said the Acme deck would go
  out yesterday — has it?" Never a generic "following up on your pending item".
- It's a QUESTION, and a straight one. You're asking, not chasing. No scolding, no
  "as per my last message", no implying they've blocked anyone, no new deadline.
- NO EMOJIS.
- You are REMINDING a human. You have not done anything about it yourself and you
  are not about to — never offer to send it, write it, or contact anyone.
- Include the link at the end if you're given one, in plain form.
- Output ONLY the reminder text. No preamble, no code fences, no headers."""


COMMITMENT_PROMPT = """You read one message from a sales team's Discord channel and
decide ONE thing: did this person just commit to coming back with something?

A COMMITMENT is a promise of a FUTURE deliverable from the SPEAKER — a document, a
message, an answer, a booking, a number — that someone is now waiting on:
  "I'll send Acme the deck tomorrow"          → yes: the Acme deck, tomorrow
  "will follow up with them after the call"   → yes: the follow-up, no time given
  "pricing goes out by EOD"                   → yes: the pricing, by end of day
  "let me check with Sid and come back"       → yes: an answer from Sid, no time
  "I'll book the intro for next week"         → yes: the intro call, next week
  "give me an hour and I'll have the numbers" → yes: the numbers, in ~60 min

NOT a commitment:
- Work in progress with nothing owed back: "on it", "drafting it now", "looking".
  Someone doing their job is not someone who owes the channel an update.
- Something already delivered: "sent", "shared above", "done", "booked".
- An ask OF someone else: "can you send them the deck?" — that's their promise to
  make, not the speaker's.
- Hypotheticals and intentions with no deliverable: "we should probably follow up",
  "might be worth a call".
- Pleasantries: "will do", "sure", "noted". An acknowledgement is not a
  deliverable. If you cannot name WHAT is owed, it is not a commitment.
- Anything a CUSTOMER or outside party is said to owe US. You only track what a
  member of this team promised.

"what": the thing being awaited, as a short noun phrase that can be quoted
straight back in a reminder — "the Acme deck", "pricing for Globex", "an answer
from Sid on the discount". NOT a restatement of their sentence, never first
person. Empty string when nothing concrete is owed.

"due_minutes": how long they gave THEMSELVES, in minutes, from now:
- "in an hour" → 60; "in 30 mins" → 30; "by EOD"/"today" → 480; "tonight" → 600;
  "tomorrow" → 1440; "this week"/"by EOW" → 4320; "next week" → 10080.
- null when they named NO time at all. Do not guess.

Be conservative: when in doubt, is_commitment=false. A missed promise costs less
than nagging someone about a promise they never made.

Output STRICT JSON only (no preamble, no markdown fences):
{
  "is_commitment": true | false,
  "what":          "short noun phrase of what is owed — \\"\\" if none",
  "due_minutes":   integer | null,
  "confidence":    0.0-1.0
}
"""


QUERY_PARSE_PROMPT = """You classify ONE Discord message addressed to a sales
assistant. You do not answer it. Output STRICT JSON only.

{
  "message_kind": "greeting" | "capability" | "question" | "other",
  "is_query":     true | false,
  "confidence":   0.0-1.0
}

- "greeting"   — hello / thanks / small talk. No question in it.
- "capability" — asking what you can do, what you have access to, what you're for,
                 what data you can see. ("what can you do", "what do you have
                 access to", "can you see the pipeline sheet?")
- "question"   — anything they want an actual answer to: a deal, a number, a
                 person, a meeting, the strategy, what happened in a channel.
- "other"      — a statement, an instruction, or chatter that isn't addressed to
                 you as a question.

"is_query" is true for "question" and "capability". FOLLOW-UPS: if recent
conversation is shown and this message continues it ("what about Globex?", "and
last week?"), it is a question even when it has no question mark.

When torn between "question" and "other", choose "question" — answering honestly
costs little, brushing off a real question costs a lot."""


def fallback_social_reply(kind: str, requester: str = "") -> str:
    """Deterministic reply used ONLY when the model call on the social path
    fails. Still first person, still direct, still no emojis — a failure path must
    not reintroduce a chirpy "here are my commands" template."""
    who = (requester or "").strip().split()[0] if (requester or "").strip() else ""
    hi = f"Hi {who}" if who else "Hi"
    if kind == "greeting":
        return f"{hi} — what do you need? I can look through the sales channels and the meeting notes."
    return (
        f"{hi} — I'm not sure what you're after there, and I didn't find anything to go on. "
        "Say a bit more and I'll dig into it."
    )


def fallback_capability_reply() -> str:
    """Deterministic capability answer for when the model call fails. Built from
    the SAME live source statuses the model would have been given, so it is just
    as honest about the blind spots — that honesty is the whole point of this
    answer and it must survive an API failure."""
    lines = [
        f"I'm {NAME}, the sales and marketing chief of staff for this team. "
        "I read the sales channels, track what people commit to, and answer "
        "questions from what I can actually see. Anything overdue, stalled or "
        "waiting on us goes into one digest a day rather than pinging you "
        "through it."
    ]
    statuses = sources.status_report()
    connected = [s for s in statuses if s["status"] == sources.CONNECTED]
    stale = [s for s in statuses if s["status"] == sources.DEGRADED]
    waiting = [s for s in statuses if s["status"] not in sources.USABLE]
    if connected:
        lines.append("Connected: " + ", ".join(s["label"] for s in connected) + ".")
    if stale:
        # Readable, so it belongs in what I CAN see — but never without the
        # caveat, or a stale answer reads as a current one.
        lines.append(
            "Readable but going stale: "
            + ", ".join(f"{s['label']} ({s['detail']})" for s in stale)
        )
    if waiting:
        lines.append(
            "Still waiting on access to: "
            + ", ".join(f"{s['label']} ({s['detail']})" for s in waiting)
        )
        lines.append("Until then I can't answer anything that depends on those.")
    if not policy_status()["loaded"]:
        lines.append("My policy file is also missing, so I'm working from defaults.")
    return "\n".join(lines)

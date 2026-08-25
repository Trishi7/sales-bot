"""The bot's voice, and the POLICY it answers under.

Two things live here, and the split matters:

  THE PERSONA (`COS_PERSONA`) — hard-coded voice rules. Direct, brief, no emojis,
  first person. This is style, and style shouldn't need a file edit.

  THE POLICY (`sales_policy.md`) — what the bot is FOR: the role, what it
  enforces, its hard limits. That is Sid's to write and to change, so it lives in
  a markdown file at the repo root and is RE-READ ON EVERY QUERY. Edit the file,
  ask the next question, and the new policy is already in force — no restart, no
  redeploy. `load_policy()` caches on the file's mtime+size, so re-reading costs
  a stat() call, not a disk read, on every turn.

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

This persona changes ONLY your voice. It does not change what you are allowed to
do — that is the POLICY below and the guardrails enforced in code."""


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
    live policy, then what the bot can actually see right now.

    Every path that speaks to a human builds on this, so the policy and the
    source statuses can never apply to one reply and not another. `include_sources`
    is False only for prompts that do no source reasoning at all (the commitment
    detector), where the statuses would be noise.
    """
    parts = [cos_preamble()]

    policy = load_policy()
    if policy:
        parts.append(
            "=== SALES POLICY (the operating policy you work under; it is re-read on "
            "every question, so this is always the current version) ===\n"
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

    return "".join(parts)


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

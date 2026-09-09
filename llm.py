"""The bot's small, single-shot LLM calls.

This is NOT the query path — open questions go through `query_engine.QueryEngine`,
which runs a tool-use loop. What lives here are the four short calls around it:

  parse_query()       is this a greeting, a "what can you do", or a real question?
  social_reply()      the voice on the non-answer paths (greeting / unclear).
  capability_reply()  the honest "what can you do" answer, built from the policy
                      and the LIVE source statuses.
  detect_commitment() is this someone promising to come back with something?
  chase_nudge()       the persona-voiced reminder text for an overdue promise.
                      NO LONGER ON THE CHASE PATH: an overdue promise is a
                      deterministic line in the daily digest (digest.py), because
                      forty model calls to build one message would be slow, and
                      unpredictable in a message people are meant to skim. Kept
                      for a one-off, hand-asked reminder.

Two rules run through all of them:

  EVERY REPLY-PATH CALL CARRIES THE POLICY. The system prompt is built from
  `persona.system_preamble()`, which re-reads sales_policy.md and appends the
  current source statuses. A path that spoke without them would be a path
  operating under a different policy than the rest of the bot.

  NOTHING HERE EVER RAISES INTO THE CALLER. A failed model call degrades to a
  deterministic fallback (a nudge that must still go out, a reply that must still
  be sendable) or to None (a verdict we simply don't have, which callers treat as
  "don't act"). An API blip is not allowed to take the bot down or to silently
  swallow a chase.

The prompts themselves all live in persona.py, next to the voice they're written
in — so changing how the bot sounds means editing one file, not five.
"""
import asyncio
import json
import logging
import re
from typing import Optional

from anthropic import Anthropic

import config
import persona
from followups import fallback_nudge
from persona import (
    CAPABILITY_PROMPT,
    CHASE_NUDGE_PROMPT,
    COMMITMENT_PROMPT,
    QUERY_PARSE_PROMPT,
    SOCIAL_REPLY_PROMPT,
    fallback_capability_reply,
    fallback_social_reply,
)

log = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort: strip code fences and parse. Returns None on failure, which
    callers treat as "no verdict" rather than as a false one."""
    cleaned = _JSON_FENCE.sub("", text or "").strip()
    if not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        cleaned = cleaned[start : end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning("[llm] model returned non-JSON: %s; raw=%r", e, (text or "")[:200])
        return None


def _text_of(resp) -> str:
    """Concatenate the text blocks of a response, fences stripped."""
    blocks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return _JSON_FENCE.sub("", "\n".join(blocks)).strip()


# The shapes a proactive message must never have. Checked rather than trusted:
# the voice rules say "no headers, no bullets, no emojis, one thought", and a
# model that ignores them produces exactly the message the drip was built to
# stop sending.
_PROACTIVE_BANNED = (
    "\n- ", "\n* ", "\n1. ",
    "**", "##", "•",
)


def _proactive_problem(text: str) -> str:
    """Why this composed message cannot be sent, or "" when it is fine."""
    if not text:
        return "empty"
    if len(text) > 600:
        return f"too long ({len(text)} chars)"
    for token in _PROACTIVE_BANNED:
        if token in text:
            return f"contains {token!r} — headers and bullets are not the voice"
    if any(ord(ch) > 0x2100 for ch in text):
        return "contains an emoji or symbol"
    if text.count(chr(10) * 2) >= 2:
        return "more than two paragraphs — one thought per message"
    return ""


SHEET_UPDATE_PROMPT = """You read ONE message a sales colleague sent, and decide
what — if anything — it says about a row in the outreach sheet.

You output JSON only. No prose, no fences.

{
  "intent": "update" | "snooze" | "undo" | "none",
  "company": "<the company the message is about, or \"\" if it is the one in context>",
  "poc": "<the person, or \"\">",
  "fields": [
    {"role": "<one of the roles below>",
     "value": "<what to put in the cell, formatted as the sheet formats it>",
     "supersedes": true|false,
     "quote": "<the words in their message that say this>"}
  ],
  "confidence": 0.0-1.0
}

THE ROLES YOU MAY USE, and what each holds:
  first_contact_type  how first contact was made: Email / LinkedIn / Call / WhatsApp
  first_contacted     the date first contact went out
  connected           the date the connection was accepted
  dm_sent_date        the date the DM went out
  response            whether they replied: Y / N / P / Awaited
  meeting_status      Booked / Done / No-show / Rescheduled / Cancelled
  meeting_date        the date of the meeting
  next_steps          the next action, in their words, short
  other_updates       a note worth keeping that fits nowhere else
  package_sent        Yes / No, or the date it went
  assets_shared       Yes / No
  prospect_stage      Lead / Demo / Quote / Dead / Unresponsive
  closure             a closure probability, as a percentage
  deal_size           the deal value
  deal_status         In Progress / On Hold / Won / Lost

DATES: today's date is given below. Resolve "this morning", "yesterday",
"Tuesday", "the 3rd" against it and output DD-MM-YYYY. Never output a relative
phrase. If you cannot resolve a date confidently, leave the field out entirely.

"supersedes" IS THE MOST IMPORTANT FIELD YOU SET. It means: this message
CLEARLY REPLACES whatever is already in that cell. Set it true only when they
said so — "moved to Friday", "actually it went Tuesday", "changed to 60%",
"no, it was cancelled". Set it FALSE when they are simply stating a fact that
might already be recorded. A false here means an existing value is left alone,
which is always recoverable; a true here overwrites something a person typed.

WHAT COUNTS AS AN UPDATE. "Sent this morning" after being asked about a DM is an
update. "Yeah I'll get to it" is not — it promises, it does not report. "Thanks"
is not. When in doubt, intent "none": a missed update costs one more nudge, a
wrong one costs somebody's data.

intent "snooze" when they ask you to come back later ("follow up in 15 days",
"remind me Saturday"). Put nothing in fields; the caller parses the timing.
intent "undo" when they are asking you to reverse what you just did.
intent "none" for anything else, INCLUDING a question — a question is answered,
not written down.

CONTACT DETAILS. If they give you an email address, a phone number or a
LinkedIn link, DO include it, with role "email", "phone" or "linkedin". You are
not setting those cells — the caller never writes them — but reporting them is
how the bot can say "I have got that, could you add it yourself" instead of
silently losing it."""


class LLM:
    """Thin wrapper over the Anthropic client for this bot's short calls.

    The SDK is synchronous, so every call is dispatched with `asyncio.to_thread`
    — a model call must never block the Discord gateway heartbeat.
    """

    def __init__(self, api_key: str, model: str) -> None:
        self._client = Anthropic(api_key=api_key)
        self._model = model

    async def _create(self, *, system: str, prompt: str, max_tokens: int):
        return await asyncio.to_thread(
            self._client.messages.create,
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )

    # -- routing ------------------------------------------------------------

    async def parse_query(
        self,
        *,
        text: str,
        requester: Optional[str] = None,
        history: Optional[list[dict]] = None,
    ) -> Optional[dict]:
        """Route ONE message: greeting / capability / question / other.

        Returns {"message_kind", "is_query", "confidence"} or None on failure —
        and None is handled by the caller falling back to a question-shape
        heuristic, so an API blip degrades into "try to answer it" rather than
        into silence.

        `history` (recent turns in this channel) is shown ONLY so an elliptical
        follow-up ("what about Globex?") is recognised as a question. This is a
        pure router: it never speaks to the user, so no persona is applied.
        """
        history_block = ""
        turns = [t for t in (history or []) if (t or {}).get("question")]
        if turns:
            lines: list[str] = []
            for t in turns[-4:]:
                q = str(t.get("question") or "").strip()
                a = str(t.get("answer") or "").strip()
                if q:
                    lines.append(f"- Q: {q}")
                if a:
                    lines.append(f"  A: {a.splitlines()[0][:200]}")
            history_block = (
                "Recent conversation in this channel (oldest first), for resolving a "
                "follow-up only:\n" + "\n".join(lines) + "\n\n"
            )

        prompt = (
            history_block
            + f"Who is asking: {requester or '(unknown)'}\n\n"
            f'Their message:\n"""\n{text}\n"""'
        )
        log.info("[llm.parse] routing %r (history=%d)", (text or "")[:120], len(turns))
        try:
            resp = await self._create(system=QUERY_PARSE_PROMPT, prompt=prompt, max_tokens=200)
        except Exception:
            log.exception("[llm.parse] call raised; no verdict")
            return None

        parsed = _extract_json(_text_of(resp))
        if not parsed:
            return None

        kind = str(parsed.get("message_kind") or "question").strip().lower()
        if kind not in ("greeting", "capability", "question", "other"):
            kind = "question"
        try:
            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        out = {
            "message_kind": kind,
            # A capability ask IS a query; trust the kind over a contradictory flag.
            "is_query": bool(parsed.get("is_query")) or kind in ("question", "capability"),
            "confidence": confidence,
        }
        log.info("[llm.parse] → kind=%s is_query=%s conf=%.2f", kind, out["is_query"], confidence)
        return out

    # -- speaking paths ------------------------------------------------------

    async def social_reply(
        self, *, kind: str, text: str = "", requester: Optional[str] = None
    ) -> str:
        """The voice on a non-answer path. Always returns something sendable: on
        any failure it falls back to `persona.fallback_social_reply`, which is
        still first-person, still direct, still emoji-free."""
        kind = (kind or "unclear").strip().lower()
        if kind not in ("greeting", "unclear"):
            kind = "unclear"
        log.info("[llm.social] kind=%s requester=%r", kind, requester)

        prompt = (
            f"Message kind: {kind}\n"
            f"Who is speaking to you: {requester or '(unknown)'}\n\n"
            f'Their message:\n"""\n{text}\n"""\n\n'
            "Reply to them now, in your own voice, per the rules above."
        )
        try:
            resp = await self._create(
                system=persona.system_preamble() + SOCIAL_REPLY_PROMPT,
                prompt=prompt,
                max_tokens=250,
            )
        except Exception:
            log.exception("[llm.social] call raised; using deterministic fallback")
            return fallback_social_reply(kind, requester or "")

        reply = _text_of(resp)
        if not reply:
            log.warning("[llm.social] empty reply; using deterministic fallback")
            return fallback_social_reply(kind, requester or "")
        return reply

    async def proactive_message(self, *, prompt: str, fallback: str) -> tuple[str, bool]:
        """Compose ONE proactive message in the warm-sales-head voice.

        Returns (text, used_model). `fallback` is a complete, sendable sentence
        the caller has already built from a template — this method NEVER returns
        empty, and never raises. A model outage must cost polish and never the
        message: a nudge that did not go out because an API was slow is a nudge
        nobody knows was missed.

        The system prompt is `persona.proactive_voice_prompt()`, which reads the
        ten voice exemplars live out of sales_policy.md — so rewriting the bot's
        proactive voice is a markdown edit, not a deploy.

        The reply is sanity-checked before it is trusted. A model that returns a
        bulleted list, a header, or four paragraphs has not followed the voice
        rules, and shipping it would teach the team that the rules are
        decorative. In that case the deterministic fallback goes out instead.
        """
        if not config.DRIP_LLM_COMPOSE:
            return fallback, False
        try:
            resp = await self._create(
                system=persona.proactive_voice_prompt(),
                prompt=prompt,
                max_tokens=220,
            )
        except Exception:
            log.exception("[llm.proactive] call raised; sending the template instead")
            return fallback, False

        text = (_text_of(resp) or "").strip().strip('"').strip()
        problem = _proactive_problem(text)
        if problem:
            log.warning(
                "[llm.proactive] rejected the composed message (%s); sending the "
                "template instead. Got: %r", problem, text[:160],
            )
            return fallback, False
        log.info("[llm.proactive] composed %d chars", len(text))
        return text, True

    async def extract_sheet_update(
        self, *, text: str, today: str, company_hint: str = "",
        poc_hint: str = "", asked_about: str = "", requester: Optional[str] = None,
    ) -> Optional[dict]:
        """What ONE message says about a sheet row. None when the call failed.

        `company_hint` / `asked_about` are the CONTEXT of the message the person
        replied to — the drip nudge's companies and what it asked about. They
        are what makes "sent this morning" resolvable at all: on its own that
        sentence names no company and no column.

        RETURNS A PROPOSAL, NOT A DECISION. Everything it suggests still goes
        through `sheetwrite.plan_writes`, which enforces the tiers, the
        restricted bands, the fill rule and the terminal-word gate. This method
        cannot write anything and must never be the only thing standing between
        a sentence and a cell.

        None on failure, and the caller treats that as "no update" — an
        extraction the model could not do is not an extraction the bot should
        guess at.
        """
        prompt = (
            f"Today is {today}.\n"
            f"Who is speaking: {requester or '(unknown)'}\n"
            + (f"The row in context: {company_hint}"
               + (f" / {poc_hint}" if poc_hint else "") + "\n" if company_hint else "")
            + (f"What I had asked them about: {asked_about}\n" if asked_about else "")
            + f'\nTheir message:\n"""\n{text}\n"""'
        )
        log.info("[llm.sheet] extracting from %r (context=%r)",
                 (text or "")[:120], company_hint or "-")
        try:
            resp = await self._create(
                system=SHEET_UPDATE_PROMPT, prompt=prompt, max_tokens=700,
            )
        except Exception:
            log.exception("[llm.sheet] call raised; treating as no update")
            return None

        parsed = _extract_json(_text_of(resp))
        if not parsed:
            log.warning("[llm.sheet] no JSON back; treating as no update")
            return None

        intent = str(parsed.get("intent") or "none").strip().lower()
        if intent not in ("update", "snooze", "undo", "none"):
            intent = "none"
        fields = []
        for entry in (parsed.get("fields") or []):
            role = str((entry or {}).get("role") or "").strip()
            value = str((entry or {}).get("value") or "").strip()
            if not role or not value:
                continue
            fields.append({
                "role": role, "value": value,
                "supersedes": bool((entry or {}).get("supersedes")),
                "quote": str((entry or {}).get("quote") or "").strip()[:200],
            })
        try:
            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        out = {
            "intent": intent,
            "company": str(parsed.get("company") or "").strip(),
            "poc": str(parsed.get("poc") or "").strip(),
            "fields": fields,
            "confidence": confidence,
        }
        log.info(
            "[llm.sheet] -> intent=%s company=%r fields=%s conf=%.2f",
            intent, out["company"], [f["role"] for f in fields], confidence,
        )
        return out

    async def research_brief(self, *, material: str) -> str:
        """Write ONE research brief from the gathered material. Copy material.

        The system prompt is `research.BRIEF_PROMPT` plus the team's own policy
        and strategy text, so the LANES the brief maps work onto are the ones
        written down rather than ones the model invented. Nothing here sends
        anything, and the brief is never written to the sheet.

        On any failure it returns a plain statement of that, not an empty string
        — a brief that silently came back blank would be read as "there was
        nothing to find about this person", which is a different and wrong claim.
        """
        import research

        log.info("[llm.brief] composing from %d chars of material", len(material or ""))
        try:
            resp = await self._create(
                system=persona.system_preamble(include_sources=False)
                + research.BRIEF_PROMPT,
                prompt=material,
                max_tokens=1600,
            )
        except Exception:
            log.exception("[llm.brief] call raised")
            return (
                "I could not write the brief just now — the model call failed. The "
                "material I gathered is fine; try again in a moment."
            )
        text = (_text_of(resp) or "").strip()
        if not text:
            log.warning("[llm.brief] empty brief")
            return (
                "I gathered the material but the model returned nothing. That is a "
                "failure on my side, not an absence of information about them."
            )
        return text

    async def capability_reply(
        self, *, text: str = "", requester: Optional[str] = None
    ) -> str:
        """"What can you do?" — answered from the policy and the LIVE source
        statuses, both of which `persona.system_preamble()` puts in front of the
        model. The prompt REQUIRES naming every source that's awaiting access.

        On failure this falls back to `persona.fallback_capability_reply`, which
        builds the same statement from the same statuses without a model — the
        honesty of this answer is the point of it, and it must survive an
        outage."""
        log.info("[llm.capability] requester=%r", requester)
        prompt = (
            f"Who is asking: {requester or '(unknown)'}\n\n"
            f'Their message:\n"""\n{text}\n"""\n\n'
            "Answer them now, per the rules above."
        )
        try:
            resp = await self._create(
                system=persona.system_preamble() + CAPABILITY_PROMPT,
                prompt=prompt,
                max_tokens=600,
            )
        except Exception:
            log.exception("[llm.capability] call raised; using deterministic fallback")
            return fallback_capability_reply()

        reply = _text_of(resp)
        if not reply:
            log.warning("[llm.capability] empty reply; using deterministic fallback")
            return fallback_capability_reply()
        return reply

    async def chase_nudge(
        self, *, mention: str, what: str, when: str, jump_url: str = ""
    ) -> str:
        """The reminder for an overdue promise: "<@123> — you said the Acme deck
        would go out yesterday. Has it?"

        `mention` is pasted verbatim; it was produced by `guardrails.mention_for`,
        so it is already roster-checked. Always returns something sendable — a
        chase that silently doesn't fire is the one failure this feature cannot
        have, so any failure falls back to `followups.fallback_nudge`.

        NOT CALLED BY THE SWEEPER any more — overdue promises are lines in the
        daily digest, written deterministically. See the module docstring."""
        log.info("[llm.nudge] composing for %s about %r", mention, (what or "")[:80])
        item = {
            "person_id": None,
            "person_name": mention,
            "what": what,
            "promised_at": "",
            "jump_url": jump_url,
        }
        prompt = (
            f"Who promised (paste this token verbatim): {mention}\n"
            f"What they promised: {what}\n"
            f"When they promised it: {when}\n"
            f"Link to their message: {jump_url or '(none)'}\n\n"
            "Write the reminder now, in your own voice, per the rules above."
        )
        try:
            resp = await self._create(
                system=persona.system_preamble() + CHASE_NUDGE_PROMPT,
                prompt=prompt,
                max_tokens=250,
            )
        except Exception:
            log.exception("[llm.nudge] call raised; using deterministic fallback")
            return fallback_nudge(item)

        nudge = _text_of(resp)
        if not nudge:
            log.warning("[llm.nudge] empty reply; using deterministic fallback")
            return fallback_nudge(item)

        # The model was told to paste the token verbatim. If it paraphrased the
        # mention away, the person never gets pinged — so put it back on the front.
        if mention.startswith("<@") and mention not in nudge:
            log.info("[llm.nudge] model dropped the mention token; prepending it")
            nudge = f"{mention} — {nudge}"
        return nudge

    # -- extraction ----------------------------------------------------------

    async def detect_commitment(
        self, *, text: str, author: str, channel: str
    ) -> Optional[dict]:
        """Is this someone promising to come back with something — and if so,
        WHAT, and by when?

        Returns {"is_commitment", "what", "due_minutes", "confidence"} or None on
        failure. `what` is phrased as the thing awaited ("the Acme deck") so it
        can be quoted straight back in a reminder; `due_minutes` is how long they
        gave themselves, or None when they named no time.

        Pure extraction — nothing here is spoken, so no persona and no source
        statuses; only the commitment rules. A None verdict means "don't track
        it", which is the safe direction.
        """
        log.info("[llm.commitment] author=%s channel=#%s text=%r", author, channel, (text or "")[:120])
        prompt = (
            f"Channel: #{channel}\n"
            f"Speaker: {author}\n\n"
            f'Their message:\n"""\n{text}\n"""\n\n'
            "Is this a commitment to come back with something? Decide now."
        )
        try:
            resp = await self._create(system=COMMITMENT_PROMPT, prompt=prompt, max_tokens=250)
        except Exception:
            log.exception("[llm.commitment] call raised; not tracking this one")
            return None

        parsed = _extract_json(_text_of(resp))
        if not parsed:
            return None

        is_commitment = bool(parsed.get("is_commitment", False))
        what = str(parsed.get("what") or "").strip()
        try:
            confidence = float(parsed.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        due_raw = parsed.get("due_minutes")
        try:
            due_minutes = int(due_raw) if due_raw is not None else None
        except (TypeError, ValueError):
            log.debug("[llm.commitment] due_minutes=%r unusable; treating as unstated", due_raw)
            due_minutes = None

        # Nothing to chase without a WHAT: the reminder would read "any update
        # on… something?", which is worse than staying quiet.
        if is_commitment and not what:
            log.info("[llm.commitment] is_commitment=true but no 'what'; not tracking")
            is_commitment = False

        out = {
            "is_commitment": is_commitment,
            "what": what,
            "due_minutes": due_minutes,
            "confidence": max(0.0, min(1.0, confidence)),
        }
        log.info(
            "[llm.commitment] → is_commitment=%s what=%r due_minutes=%s conf=%.2f",
            out["is_commitment"], out["what"][:80], out["due_minutes"], out["confidence"],
        )
        return out

"""The sales & marketing Chief of Staff bot.

Scoped to the sales channels, it does three things and nothing else:

  ANSWERS QUESTIONS. In the ask channel every message is a potential question; in
  the other sales channels an explicit @-mention is required. Questions go to the
  read-only tool-use engine (query_engine.py), which is handed Discord tools
  scoped to the sales channels plus the meeting-notes tools, and which answers
  under the policy in sales_policy.md — re-read on every question.

  CHASES DEADLINES. When someone commits to something in a sales channel ("I'll
  send Acme the deck tomorrow"), that becomes a CHASE. Once it's overdue the bot
  asks them about it, in the channel the promise was made in, capped by
  COS_NUDGE_WINDOW_HOURS / COS_NUDGE_MAX_ATTEMPTS. After the cap it stops asking
  and flags it once instead. Stopping is deliberate: a bot that keeps asking gets
  muted, and a muted bot enforces nothing.

  KEEPS ITS STATE READABLE. state/summary.json is rewritten at startup and daily;
  state/audit.jsonl gets a line for every action. Both are documented in the
  README as a contract for a future supervisor process (COSA).

WHAT IT DOES NOT DO. There is no triage, no classification-to-ticket pipeline, no
approval channel and nothing to approve — this bot's only output is a Discord
message in a sales channel. It files nothing, tracks nothing in another system,
and contacts nobody outside Discord.

THE HARD RULES LIVE IN guardrails.py, not here and not in a prompt: never DM,
never @-mention anyone off the team roster, read and post only in
SALES_CHANNEL_IDS. Every send in this file goes through `guardrails.send()`,
which enforces all three and writes the audit line. `on_message` drops anything
from outside the scope before it is even looked at.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord

import config
import followups
import guardrails
import notes
import persona
import query
import sources
import state
from db import DB
from llm import LLM
from memory import ConversationMemory
from query_engine import QueryEngine

log = logging.getLogger(__name__)

# ✅ on a nudge closes that chase — "handled, stop asking".
CLOSE_EMOJI = "✅"

# Interrogative / imperative openers that mark a message as a question worth
# handing to the engine. Used only as a LAST-RESORT guard, so a real question
# never falls through to the capability text because the router under-classified
# a phrasing it didn't recognise.
_QUESTION_LEAD_RE = re.compile(
    r"^\s*(what|who|whose|whom|when|where|why|which|how|is|are|was|were|do|does|"
    r"did|can|could|should|would|will|has|have|had|show|list|tell|give|find|"
    r"lookup|look\s+up|search|status|update|remind|any)\b",
    re.IGNORECASE,
)

# Phrasings that are asking what the bot IS or can SEE. Checked before the model
# router so this question is answered honestly even when the router is down —
# it's the question whose wrong answer does the most damage, because someone
# deciding whether to trust the bot is asking it.
_CAPABILITY_RE = re.compile(
    r"(what\s+(can|do)\s+you\s+(do|see|have|know)|what\s+are\s+you\s+for|"
    r"who\s+are\s+you|what\s+(access|sources|data)\s+do\s+you\s+have|"
    r"can\s+you\s+(see|read|access)\s+(the|my|our)|what'?s?\s+your\s+(job|role))",
    re.IGNORECASE,
)


def _display(user) -> str:
    return (
        getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or str(user)
    )


def _looks_like_question(text: str) -> bool:
    """Heuristic: does this read like a question worth handing to the engine?
    Deliberately liberal — the engine answers honestly ("I couldn't find…") when
    nothing applies, so a false positive is cheap while a false negative bounces
    a real question with boilerplate."""
    t = (text or "").strip()
    if not t:
        return False
    if "?" in t:
        return True
    return bool(_QUESTION_LEAD_RE.match(t))


def _split_for_discord(body: str, limit: int = config.QUERY_REPLY_CHUNK) -> list[str]:
    """Split `body` into Discord-sendable chunks of at most `limit` chars,
    breaking ONLY on line boundaries so a chunk never cuts mid-sentence. A single
    line longer than `limit` is hard-wrapped as a last resort. Always returns at
    least one chunk."""
    body = body or ""
    if len(body) <= limit:
        return [body]

    chunks: list[str] = []
    current = ""
    for line in body.split("\n"):
        # An oversize line can't share a chunk: flush what we have, then emit it
        # in full-width slices, carrying any remainder into `current`.
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                piece = line[i : i + limit]
                if len(piece) == limit:
                    chunks.append(piece)
                else:
                    current = piece
            continue
        # +1 accounts for the "\n" re-inserted between lines.
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


class SalesBot(discord.Client):
    def __init__(self) -> None:
        log.info("[bot.init] setting up Discord intents (message_content, reactions)")
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        super().__init__(intents=intents)

        log.info("[bot.init] opening SQLite DB at %s", config.DB_PATH)
        self.db = DB(config.DB_PATH)
        log.info("[bot.init] constructing LLM (model=%s)", config.MODEL)
        self.llm = LLM(config.ANTHROPIC_API_KEY, config.MODEL)
        log.info("[bot.init] constructing QueryEngine (model=%s)", config.MODEL)
        self.query_engine = QueryEngine(config.ANTHROPIC_API_KEY, config.MODEL)

        self.memory = ConversationMemory(
            max_turns=config.QUERY_MEMORY_TURNS,
            ttl_minutes=config.QUERY_MEMORY_TTL_MINUTES,
        )
        self._sweeper: Optional[asyncio.Task] = None
        log.info(
            "[bot.init] %s ready to connect. sales_channels=%s ask_channel=%s roster=%d",
            config.COS_NAME, config.SALES_CHANNEL_IDS, config.SALES_ASK_CHANNEL_ID,
            len(config.TEAM_ROSTER_IDS),
        )

    # -- lifecycle ---------------------------------------------------------

    async def on_ready(self) -> None:
        log.info(
            "[bot] connected as %s (id=%s), in %d guild(s)",
            self.user, getattr(self.user, "id", "?"), len(self.guilds),
        )

        # Report what the bot can actually SEE, at INFO, on every boot. A channel
        # configured but invisible is the most common misconfiguration, and it is
        # silent otherwise — the bot simply never answers and nobody knows why.
        for cid in guardrails.readable_channel_ids():
            chan = self.get_channel(cid)
            if chan is None:
                log.warning(
                    "[bot] sales channel %s is NOT visible — the bot's role may lack "
                    "View Channel there, or the id is wrong. It will be skipped.", cid,
                )
            else:
                log.info("[bot] reading #%s (%s)", getattr(chan, "name", "?"), cid)

        for src in sources.status_report():
            log.info("[bot] source %s: %s — %s", src["key"], src["status"], src["detail"])

        pol = persona.policy_status()
        log.info(
            "[bot] policy %s (%s, %d chars) — re-read on every question",
            "loaded" if pol["loaded"] else "MISSING", pol["path"], pol["chars"],
        )

        self._write_state_summary(trigger="startup")
        state.audit(
            "startup",
            reason="bot connected to Discord and rewrote its state summary",
            channels=len(config.SALES_CHANNEL_IDS),
            visible_channels=len(guardrails.readable_channels(self)),
        )

        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop())
            log.info(
                "[bot] chase sweeper started (every %d min)",
                config.COS_FOLLOWUP_CHECK_INTERVAL_MINUTES,
            )

    # -- incoming messages -------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        """THE READ GATE. Anything outside the sales channels is dropped here,
        before it is parsed, stored, or reasoned about — the bot behaves as if
        those channels do not exist. That's the code half of the scoping rule;
        the server-side half (no View Channel) is in DEPLOY.md."""
        if message.author.bot:
            return
        if not guardrails.may_read(message.channel.id):
            # Not logged at INFO: with the role configured correctly this should
            # never fire, and if the role is wrong it would be every message.
            log.debug(
                "[bot] ignoring message %s from channel %s — outside SALES_CHANNEL_IDS",
                message.id, message.channel.id,
            )
            return

        # A reply to a nudge (or to the original promise) means they came back.
        # Checked first, and it does NOT consume the message: their reply may
        # itself be a question, and it deserves an answer.
        await self._maybe_close_chase(message)

        if self._is_query_trigger(message):
            handled = await self._handle_query(message)
            if handled:
                return

        # Not addressed to the bot: the only thing left to do is notice a promise.
        await self._maybe_track_commitment(message)

    async def on_raw_message_edit(self, payload) -> None:
        """An edited message that becomes a question should be answered — people
        fix a typo and expect a reply. Scope is re-checked here rather than
        trusted from the original event."""
        channel_id = (payload.data or {}).get("channel_id")
        if not channel_id or not guardrails.may_read(int(channel_id)):
            return
        channel = self.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.DiscordException:
            return
        if message.author.bot or not self._is_query_trigger(message):
            return
        log.info("[bot] edited message %s re-fired as a query", message.id)
        await self._handle_query(message)

    async def on_raw_reaction_add(self, payload) -> None:
        """✅ on a nudge closes that chase — "handled, stop asking"."""
        if str(payload.emoji) != CLOSE_EMOJI:
            return
        if payload.user_id == getattr(self.user, "id", None):
            return
        if not guardrails.may_read(payload.channel_id):
            return
        try:
            item = self.db.find_chase_by_reminder(payload.message_id)
        except Exception:
            log.exception("[bot] chase lookup failed for reaction on %s", payload.message_id)
            return
        if not item:
            return
        self._close_chase(item, why="someone marked the reminder as handled")

    # -- query routing -----------------------------------------------------

    def _is_query_trigger(self, message: discord.Message) -> bool:
        """Should this be handled as a question?

        In the ASK channel, every human message is a potential question — the
        @-mention requirement is dropped there. In every other sales channel an
        explicit @-mention is required, so the bot stays out of the way of people
        talking to each other."""
        if config.is_ask_channel(message.channel.id):
            return True
        return self._is_self_mentioned_explicitly(message)

    def _is_self_mentioned_explicitly(self, message: discord.Message) -> bool:
        """True only when the bot is @-mentioned in the message TEXT. A reply-ping
        (which Discord auto-adds to `message.mentions`) doesn't count — replying
        to the bot's nudge shouldn't be read as a new question."""
        if self.user is None or not message.content:
            return False
        me = self.user.id
        return f"<@{me}>" in message.content or f"<@!{me}>" in message.content

    def _strip_self_mention(self, content: str) -> str:
        if not content or self.user is None:
            return (content or "").strip()
        me = self.user.id
        text = content
        for pat in (f"<@{me}>", f"<@!{me}>"):
            text = text.replace(pat, "")
        return text.strip()

    async def _handle_query(self, message: discord.Message) -> bool:
        """Route and answer a message addressed to the bot.

        Order, with the boilerplate last:
          - a bare @-mention → a greeting, not a malformed query;
          - a CAPABILITY question → the honest answer, from the policy and the
            live source statuses (this is checked BEFORE the model router, so it
            still works when the router fails);
          - a GREETING → a short persona reply;
          - anything question-shaped, or any follow-up in an ongoing exchange →
            the engine;
          - only if the engine made no progress at all → a persona-voiced nudge.

        Returns True when the message was handled. READ-ONLY throughout.
        """
        text = self._strip_self_mention(message.content)
        requester = _display(message.author)
        log.info(
            "[query] msg=%s from %s in #%s: %r",
            message.id, requester, getattr(message.channel, "name", "?"), text[:160],
        )

        if not text:
            # A bare "@bot" is a wave, not a malformed question.
            await self._send_social(message, "greeting", text="")
            return True

        # Capability asks are answered from a regex first: this is the question
        # whose wrong answer costs the most, so it must not depend on the router.
        if _CAPABILITY_RE.search(text):
            log.info("[query] msg=%s → capability (matched directly)", message.id)
            await self._send_capability(message, text)
            return True

        history = self.memory.recent(message.channel.id)
        parsed = await self.llm.parse_query(text=text, requester=requester, history=history)

        if parsed is None:
            # Router unavailable. Don't bounce someone on an infrastructure
            # failure: if it reads like a question, let the engine try.
            if _looks_like_question(text) or history:
                log.info("[query] msg=%s → engine (router down, question-shaped)", message.id)
                if await self._answer_with_engine(message, text, history=history):
                    return True
            await self._send_social(message, "unclear", text=text)
            return True

        kind = parsed["message_kind"]

        if kind == "capability":
            log.info("[query] msg=%s → capability (router)", message.id)
            await self._send_capability(message, text)
            return True

        if kind == "greeting":
            log.info("[query] msg=%s → greeting", message.id)
            await self._send_social(message, "greeting", text=text)
            return True

        # A question, anything question-shaped, or a follow-up in an ongoing
        # exchange (a bare "and Globex?" continues the conversation and must be
        # answered, not re-questioned).
        if kind == "question" or parsed["is_query"] or _looks_like_question(text) or history:
            log.info("[query] msg=%s → engine (kind=%s)", message.id, kind)
            if await self._answer_with_engine(message, text, history=history):
                return True
            log.info("[query] msg=%s engine made no progress → persona nudge", message.id)
            await self._engine_no_progress_reply(message, text=text, history=history)
            return True

        # "other" — a statement, not addressed to the bot as a question. In the
        # ask channel say something rather than ignoring them; elsewhere they
        # explicitly @-mentioned the bot, so a reply is owed either way.
        await self._send_social(message, "unclear", text=text)
        return True

    async def _answer_with_engine(
        self, message: discord.Message, text: str, *, history: Optional[list[dict]] = None
    ) -> bool:
        """Answer via the read-only tool-use engine. Returns False only when the
        engine produced nothing at all — an honest "I couldn't find anything" IS
        an answer and returns True."""
        try:
            reply = await self.query_engine.answer(
                question=text,
                requester_name=_display(message.author),
                tools=self._discord_tools(message) + self._notes_tools(text),
                history=history,
            )
        except Exception:
            log.exception("[query] engine raised")
            reply = None

        if not reply:
            log.info("[query] msg=%s engine produced no answer", message.id)
            return False

        await self._reply(message, reply, reason="answered a question in the sales channel")
        # Remember the exchange so the next question here can build on it.
        self.memory.record(message.channel.id, text, reply)
        return True

    async def _engine_no_progress_reply(
        self, message: discord.Message, *, text: str, history: list
    ) -> None:
        """The engine held every tool and still returned nothing. On a FOLLOW-UP
        (there is history) we've already engaged — reply honestly that the search
        came up empty rather than re-asking them to narrow, so a thread can never
        get stuck in a narrow-it-down loop. On a first turn, one persona nudge."""
        if history:
            await self._reply(
                message,
                "I looked and couldn't find anything concrete on that. Give me a company, "
                "a person, or a date and I'll go again.",
                reason="engine found nothing on a follow-up question",
            )
            return
        await self._send_social(message, "unclear", text=text)

    # -- speaking ----------------------------------------------------------

    async def _reply(self, message: discord.Message, body: str, *, reason: str) -> None:
        """Reply in the same channel, no @-ping on the author.

        Discord drops anything past ~2000 chars, so a long answer is split on
        line boundaries and sent in order: the first as a reply, the rest as
        plain sends so it reads top to bottom. Every chunk goes through
        `guardrails.send`, so scope, roster and the audit log all apply."""
        chunks = _split_for_discord(body or "…")

        if len(chunks) > config.QUERY_REPLY_MAX_MESSAGES:
            log.info(
                "[reply] %d chunks; clipping to %d", len(chunks), config.QUERY_REPLY_MAX_MESSAGES
            )
            chunks = chunks[: config.QUERY_REPLY_MAX_MESSAGES]
            note = "\n\n…(truncated — ask something narrower for the rest)"
            last = chunks[-1]
            if len(last) + len(note) > config.QUERY_REPLY_CHUNK:
                last = last[: config.QUERY_REPLY_CHUNK - len(note)]
            chunks[-1] = last + note

        for i, chunk in enumerate(chunks):
            sent = await guardrails.send(
                message.channel,
                chunk,
                reason=reason,
                kind="reply",
                reply_to=message if i == 0 else None,
                extra={"part": i + 1, "parts": len(chunks)},
            )
            if sent is None:
                # Refused or failed: stop rather than posting a partial answer
                # out of order.
                return

    async def _send_social(self, message: discord.Message, kind: str, text: str = "") -> None:
        """A non-answer reply — greeting or "I couldn't follow that" — in the
        bot's own voice, so these paths sound like the same colleague as a real
        answer. Looks nothing up."""
        reply = await self.llm.social_reply(
            kind=kind,
            text=text or self._strip_self_mention(message.content),
            requester=_display(message.author),
        )
        await self._reply(message, reply, reason=f"{kind} reply")

    async def _send_capability(self, message: discord.Message, text: str) -> None:
        """"What can you do?" — answered from the policy and the LIVE source
        statuses, naming every source that's still awaiting access."""
        reply = await self.llm.capability_reply(text=text, requester=_display(message.author))
        await self._reply(
            message, reply, reason="explained capabilities and current source access"
        )

    # -- tools handed to the engine ----------------------------------------

    def _discord_tools(self, message: discord.Message) -> list[dict]:
        """Read-only Discord tools, ALL scoped to the sales channels.

        The scoping isn't in these handlers: every one calls into query.py, which
        iterates `guardrails.readable_channel_ids()` and takes no channel list of
        its own. A tool here can narrow within the sales channels; it cannot
        reach outside them.
        """
        requester_name = _display(message.author)

        async def _recent_sales_activity(inp: dict):
            name = str(inp.get("name") or "").strip()
            if name.lower() in ("", "me", "my", "myself", "i", "mine"):
                name = requester_name
            try:
                days = int(inp.get("days") or config.QUERY_DISCORD_LOOKBACK_DAYS)
            except (TypeError, ValueError):
                days = config.QUERY_DISCORD_LOOKBACK_DAYS
            return await query.person_recent_messages(self, name=name, days=max(1, days))

        async def _recent_channel_activity(inp: dict):
            try:
                days = int(inp.get("days") or config.QUERY_CHANNEL_SCAN_DEFAULT_DAYS)
            except (TypeError, ValueError):
                days = config.QUERY_CHANNEL_SCAN_DEFAULT_DAYS
            return await query.channel_recent_activity(
                self, days=days, channel=str(inp.get("channel") or "").strip() or None
            )

        async def _search_channel_history(inp: dict):
            author = str(inp.get("author") or "").strip()
            if author.lower() in ("me", "my", "myself", "i", "mine"):
                author = requester_name
            return await query.search_channel_history(
                self,
                terms=inp.get("terms") or [],
                channel=str(inp.get("channel") or "").strip() or None,
                days_back=inp.get("days_back") or 14,
                author=author or None,
                context_window=inp.get("context_window", 4),
            )

        async def _open_chases(inp: dict):
            """What the bot is currently waiting on. Answers "what's outstanding?"
            from the bot's OWN state rather than from a source it can't reach."""
            try:
                items = self.db.list_open_chases()
            except Exception:
                log.exception("[tools] list_open_chases failed")
                return {"error": "I couldn't read my own chase list just now."}
            return {
                "count": len(items),
                "chases": [
                    {
                        "person": i["person_name"],
                        "what": i["what"],
                        "promised_at": i["promised_at"],
                        "due_at": i["due_at"],
                        "reminders_sent": i["reminders_sent"],
                        "jump_url": i["jump_url"],
                    }
                    for i in items
                ],
                "note": (
                    "These are commitments people made in the sales channels that I'm "
                    "still waiting on. Nothing here comes from a CRM or a spreadsheet."
                ),
            }

        return [
            {
                "schema": {
                    "name": "recent_sales_activity",
                    "description": (
                        "Recent SALES-CHANNEL posts by ONE named person (their identity is "
                        "resolved first), newest first, each with a 'done_signal' flag for "
                        "'sent / booked / went out'-style phrasing. Use it for 'what has X been "
                        "doing', 'did X follow up', 'has X said anything about Acme'. Returns "
                        "{person, messages:[{channel, timestamp, text, jump_url, done_signal}]} "
                        "— or {ambiguous, candidates} when the name matches several people, in "
                        "which case ASK which one rather than picking. 'done_signal' is a HINT, "
                        "not a conclusion: 'haven't sent it yet' also matches, so read the text. "
                        "Only the sales channels are visible to this tool."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Person's name, or 'me' for the asker."},
                            "days": {"type": "integer", "description": "Look-back window in days (default from config)."},
                        },
                        "required": ["name"],
                    },
                },
                "handler": _recent_sales_activity,
            },
            {
                "schema": {
                    "name": "recent_channel_activity",
                    "description": (
                        "Digest of what happened across the SALES channels over a time window "
                        "— NOT scoped to a person. USE THIS for 'what's been going on', "
                        "'anything I missed', 'what's the team working on this week' — any "
                        "question naming NO person (for one person use recent_sales_activity). "
                        "Returns {days, channels[], people[], message_count, returned, "
                        "truncated, messages[...]} newest first. 'days' is CLAMPED to the "
                        "configured maximum and the result reports the window actually used — "
                        "quote that one. The message list is capped (see 'truncated') while the "
                        "counts cover everything scanned. Pass 'channel' to narrow to one sales "
                        "channel. SUMMARISE who discussed what; never dump the list."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "days": {"type": "integer", "description": "Look-back window in days (default from config, clamped)."},
                            "channel": {"type": "string", "description": "Optional sales-channel name or id to narrow to."},
                        },
                        "required": [],
                    },
                },
                "handler": _recent_channel_activity,
            },
            {
                "schema": {
                    "name": "search_channel_history",
                    "description": (
                        "KEYWORD-search the SALES channels' history and get each matching "
                        "message back WITH the messages around it. With no CRM connected, what "
                        "the team said in channel IS the record — search it WHENEVER a question "
                        "concerns whether something HAPPENED, was SENT, was AGREED or was "
                        "DISCUSSED ('did we ever send Acme the pricing?', 'what did we agree "
                        "with them?', 'has anyone followed up?'). "
                        "Pass 'terms' — a few distinct keywords from the question (e.g. "
                        "['acme','pricing','proposal']); a message matches if ANY term is in "
                        "it. START NARROW (the likely channel, the question's own words) and "
                        "widen — drop 'channel', raise 'days_back', add synonyms — only if that "
                        "comes back empty. 'author' restricts which messages count as MATCHES "
                        "(pass 'me' to find the asker's own past message); the surrounding "
                        "context still includes everyone, so the replies come back too. "
                        "'context_window' (default 4) is how many messages either side to "
                        "include — keep it, because a reply posted without a reply-to is only "
                        "interpretable next to the message it follows. "
                        "Results come back in DATE ORDER (oldest first): read them as a "
                        "timeline — what was promised, then what actually happened. Bodies are "
                        "TRUNCATED SNIPPETS and both the match count and window are capped, so "
                        "when 'truncated' is true say so rather than implying you saw "
                        "everything. If nothing matches, report what you searched (terms, "
                        "channel, window) — an empty search is evidence, not silence."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "terms": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Keywords from the question, e.g. ['acme','pricing']. ANY match counts.",
                            },
                            "channel": {"type": "string", "description": "Sales-channel name or id to search. Omit for all of them."},
                            "days_back": {"type": "integer", "description": "Look-back window in days (default 14, clamped — see the result)."},
                            "author": {"type": "string", "description": "Only count messages from this person as matches (or 'me')."},
                            "context_window": {"type": "integer", "description": "Messages either side of each match (default 4, clamped)."},
                        },
                        "required": ["terms"],
                    },
                },
                "handler": _search_channel_history,
            },
            {
                "schema": {
                    "name": "open_chases",
                    "description": (
                        "The commitments I am currently waiting on — things people said in the "
                        "sales channels that they'd come back with, which haven't come back "
                        "yet. Use for 'what's outstanding', 'what are you chasing', 'what has "
                        "anyone promised'. Returns {count, chases:[{person, what, promised_at, "
                        "due_at, reminders_sent, jump_url}]}. This is MY OWN tracking of what "
                        "was said in channel — it is not a pipeline, a CRM, or a task list, and "
                        "you must not present it as one."
                    ),
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                },
                "handler": _open_chases,
            },
        ]

    def _notes_tools(self, question_text: str) -> list[dict]:
        """Read-only tools over the Drive-synced meeting notes (the
        `sales_meeting_notes` source). Blocking file/subprocess work is offloaded
        to threads so the gateway heartbeat is never held up.

        Every handler distinguishes NOT CONFIGURED from NO NOTE FOR THAT DAY, and
        the tool descriptions make the model say which it hit. Reporting a folder
        the bot can't see as "nothing was discussed" is the failure this guards
        against."""

        async def _not_configured() -> Optional[dict]:
            if await asyncio.to_thread(notes.is_configured):
                return None
            status = sources.SALES_MEETING_NOTES.status()
            return {
                "enabled": False,
                "configured": False,
                "note": status["detail"],
            }

        async def _freshness() -> dict:
            latest_date, mtime = await asyncio.to_thread(notes.freshness)
            return {"latest_date_on_file": latest_date, "synced_file_mtime": mtime}

        async def _list_meeting_notes(inp: dict):
            not_cfg = await _not_configured()
            if not_cfg:
                return not_cfg
            try:
                days = int(inp.get("days") or 30)
            except (TypeError, ValueError):
                days = 30
            found = await asyncio.to_thread(notes.list_notes, max(1, days))
            return {
                "enabled": True,
                "configured": True,
                "notes": found,
                "freshness": await _freshness(),
            }

        async def _read_meeting_note(inp: dict):
            not_cfg = await _not_configured()
            if not_cfg:
                return not_cfg

            # Sync on demand, but only for clearly recent questions and only when
            # a sync command is configured. Best-effort: we read regardless, and
            # report whether the sync RAN and whether it SUCCEEDED so a failed
            # sync is reported as "data may be stale" rather than hidden.
            sync_ran = sync_ok = False
            if config.NOTES_SYNC_CMD and notes.wants_recent(question_text):
                sync_ran = True
                sync_ok = await asyncio.to_thread(notes.sync_now, config.NOTES_SYNC_CMD)

            note = await asyncio.to_thread(
                notes.read_note, inp.get("date") or None, inp.get("label") or None
            )
            freshness = await _freshness()

            if not note:
                return {
                    "enabled": True,
                    "configured": True,
                    "found": False,
                    "sync_ran": sync_ran,
                    "sync_ok": sync_ok,
                    "freshness": freshness,
                    "note": (
                        "No matching meeting note on file"
                        + (" (even after an on-demand sync)" if sync_ran else "")
                        + (
                            " — the on-demand sync FAILED, so the data may be stale"
                            if sync_ran and not sync_ok
                            else ""
                        )
                        + "."
                    ),
                }

            same_day = await asyncio.to_thread(notes.labels_on, note.get("date"))
            others = [lbl for lbl in same_day if lbl != (note.get("label") or "")]
            return {
                "enabled": True,
                "configured": True,
                "found": True,
                "sync_ran": sync_ran,
                "sync_ok": sync_ok,
                "freshness": freshness,
                "other_meetings_that_day": others,
                "meeting_note": note,
            }

        return [
            {
                "schema": {
                    "name": "list_meeting_notes",
                    "description": (
                        "List recent sales meeting notes as [{date, label, title, path}], "
                        "newest first, plus a 'freshness' block. 'label' names WHICH meeting "
                        "('Pipeline review', 'Acme call') and may be null when the note wasn't "
                        "labelled. Use when the question is vague about which meeting, or asks "
                        "what notes exist. If configured=false, notes access isn't set up — say "
                        "exactly that; do NOT say nothing was discussed."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "days": {"type": "integer", "description": "Look-back window in days (default 30)."}
                        },
                        "required": [],
                    },
                },
                "handler": _list_meeting_notes,
            },
            {
                "schema": {
                    "name": "read_meeting_note",
                    "description": (
                        "Read ONE sales meeting note. Returns {found, meeting_note:{date, "
                        "label, title, summary, decisions[], next_steps:[{owner_name, task}], "
                        "raw}}, plus 'freshness', 'sync_ran'/'sync_ok' (did an on-demand sync "
                        "run and succeed) and 'other_meetings_that_day' (mention them if "
                        "relevant). "
                        "Pass 'date' (YYYY-MM-DD) computed by YOU from today's date for "
                        "'today' / 'yesterday' / a weekday / an explicit date; OMIT it for the "
                        "most recent note on file. Pass 'label' only when they name a meeting "
                        "('the pipeline review') — it matches as a substring. "
                        "When you use this you MUST state which note you read (e.g. 'the "
                        "pipeline review, 14 Aug') and the freshness line. If found=false, say "
                        "that note isn't on file and name the most recent one that IS — NEVER "
                        "answer from a different day's note as if it were the one asked for, "
                        "and never imply a meeting didn't happen. A next_steps entry with "
                        "owner_name null is UNOWNED: report it that way, don't assign it."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "YYYY-MM-DD; omit for the most recent on file."},
                            "label": {"type": "string", "description": "Which meeting, e.g. 'pipeline review'. Substring match."},
                        },
                        "required": [],
                    },
                },
                "handler": _read_meeting_note,
            },
        ]

    # -- chasing: spotting a promise ---------------------------------------

    async def _maybe_track_commitment(self, message: discord.Message) -> None:
        """Spot "I'll send Acme the deck tomorrow" and start waiting on it.

        Two gates before the LLM ever sees it: a cheap keyword prefilter
        (followups.looks_like_commitment) and a dedup check, so ordinary channel
        chatter costs nothing. The model's verdict then has to clear
        COS_FOLLOWUP_MIN_CONFIDENCE — a false chase is worse than a missed one,
        because it nags someone about a promise they never made."""
        if not config.COS_FOLLOWUP_ENABLED:
            return
        text = (message.content or "").strip()
        if not followups.looks_like_commitment(text):
            return
        try:
            if self.db.find_chase_by_source(message.id):
                log.debug("[chase] msg=%s already tracked", message.id)
                return
        except Exception:
            log.exception("[chase] dedup lookup failed; skipping msg=%s", message.id)
            return

        log.info("[chase] msg=%s looks like a commitment; asking the model", message.id)
        verdict = await self.llm.detect_commitment(
            text=text,
            author=_display(message.author),
            channel=getattr(message.channel, "name", "?"),
        )
        if not verdict or not verdict["is_commitment"]:
            log.info("[chase] msg=%s is not a commitment; not tracking", message.id)
            return
        if verdict["confidence"] < config.COS_FOLLOWUP_MIN_CONFIDENCE:
            log.info(
                "[chase] msg=%s confidence %.2f < %.2f; not tracking",
                message.id, verdict["confidence"], config.COS_FOLLOWUP_MIN_CONFIDENCE,
            )
            return

        promised_at = message.created_at or followups.now_utc()
        due_at = followups.due_at_from(
            verdict["due_minutes"],
            default_minutes=config.COS_FOLLOWUP_DEFAULT_DUE_MINUTES,
            promised_at=promised_at,
        )
        try:
            created = self.db.record_chase(
                channel_id=message.channel.id,
                message_id=message.id,
                jump_url=message.jump_url,
                person_id=getattr(message.author, "id", None),
                person_name=_display(message.author),
                what=verdict["what"],
                promised_at=followups.to_ts(promised_at),
                due_at=followups.to_ts(due_at),
            )
        except Exception:
            log.exception("[chase] record_chase failed for msg=%s", message.id)
            return

        if created:
            log.info(
                "[chase] TRACKING: %s owes %r — due %s (stated=%s min)",
                _display(message.author), verdict["what"],
                followups.to_ts(due_at), verdict["due_minutes"],
            )
            state.audit(
                "chase_opened",
                reason="someone committed to something in a sales channel",
                person=_display(message.author),
                what=verdict["what"],
                due_at=followups.to_ts(due_at),
                channel_id=message.channel.id,
                message_id=message.id,
            )

    async def _maybe_close_chase(self, message: discord.Message) -> None:
        """A reply to a nudge — or to the original promise — means they came back.
        Stop waiting. Does not consume the message: their reply may also be a
        question."""
        if not config.COS_FOLLOWUP_ENABLED:
            return
        ref_id = getattr(message.reference, "message_id", None) if message.reference else None
        if not ref_id:
            return
        try:
            item = self.db.find_chase_by_reminder(ref_id) or self.db.find_chase_by_source(ref_id)
        except Exception:
            log.exception("[chase] lookup failed for reply to %s", ref_id)
            return
        if item:
            self._close_chase(item, why="they replied to the promise or the reminder")

    def _close_chase(self, item: dict, *, why: str) -> None:
        try:
            self.db.set_chase_status(item["id"], "closed")
        except Exception:
            log.exception("[chase] could not close chase %d", item["id"])
            return
        log.info("[chase] CLOSED %d (%s owed %r) — %s", item["id"], item["person_name"], item["what"], why)
        state.audit(
            "chase_closed",
            reason=why,
            person=item["person_name"],
            what=item["what"],
            chase_id=item["id"],
        )

    # -- chasing: the sweeper ----------------------------------------------

    def _nudge_cooldown_minutes(self) -> int:
        """The gap the bot must leave between two nudges about ONE promise. The
        max of the two knobs, so raising either only ever makes it quieter."""
        return max(1, config.COS_NUDGE_WINDOW_HOURS) * 60

    def _max_attempts(self) -> int:
        """How many times one promise may be chased before the bot gives up and
        flags it instead."""
        return max(1, config.COS_NUDGE_MAX_ATTEMPTS)

    async def _sweep_loop(self) -> None:
        """The background tick: chase what's overdue, and rewrite the daily state
        summary. Never dies — a failing sweep logs and waits for the next tick,
        because a dead sweeper is a bot that silently stops chasing."""
        interval = max(1, config.COS_FOLLOWUP_CHECK_INTERVAL_MINUTES) * 60
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._sweep_once()
            except Exception:
                log.exception("[sweep] tick raised; continuing")
            await asyncio.sleep(interval)

    async def _sweep_once(self) -> None:
        await self._nudge_due_chases()
        self._maybe_write_daily_summary()

    async def _nudge_due_chases(self) -> None:
        """Nudge overdue promises, capped two ways: the per-promise cooldown and
        attempt limit (the rate limit), and COS_MAX_NUDGES_PER_SWEEP so a backlog
        can't flood a channel in one tick."""
        if not config.COS_FOLLOWUP_ENABLED:
            return
        now = followups.now_utc()
        try:
            items = self.db.list_due_chases(
                now=followups.to_ts(now),
                reminder_cutoff=followups.to_ts(
                    now - timedelta(minutes=self._nudge_cooldown_minutes())
                ),
            )
        except Exception:
            log.exception("[sweep] list_due_chases failed")
            return
        if not items:
            log.debug("[sweep] nothing due")
            return

        log.info("[sweep] %d chase(s) due", len(items))
        sent = 0
        for item in items:
            if item["reminders_sent"] >= self._max_attempts():
                await self._give_up_on(item)
                continue
            if sent >= max(1, config.COS_MAX_NUDGES_PER_SWEEP):
                log.info(
                    "[sweep] hit COS_MAX_NUDGES_PER_SWEEP=%d; the rest wait for the next tick",
                    config.COS_MAX_NUDGES_PER_SWEEP,
                )
                break
            if await self._nudge_chase(item):
                sent += 1

    async def _nudge_chase(self, item: dict) -> bool:
        """Post one reminder. Returns True when something actually went out.

        The rate limit is checked HERE, immediately before sending, and it fails
        CLOSED: if the log can't be read we assume we already nudged, because
        double-nudging someone is worse than missing one."""
        target_key = f"person:{item['person_id'] or item['person_name']}"
        subject_key = f"chase:{item['id']}"
        since = followups.to_ts(
            followups.now_utc() - timedelta(minutes=self._nudge_cooldown_minutes())
        )
        try:
            if self.db.was_nudged_since(
                target_key=target_key, subject_key=subject_key, kind="chase", since=since
            ):
                log.info("[sweep] chase %d nudged recently; staying quiet", item["id"])
                return False
        except Exception:
            log.exception("[sweep] rate-limit check failed for chase %d; assuming nudged", item["id"])
            return False

        channel = self.get_channel(item["channel_id"])
        if channel is None or not guardrails.may_read(item["channel_id"]):
            # The promise was made in a channel that is no longer in scope (or is
            # no longer visible). Do not chase it elsewhere — there is no
            # elsewhere.
            log.warning(
                "[sweep] channel %s for chase %d is unavailable or out of scope; skipping",
                item["channel_id"], item["id"],
            )
            return False

        mention = guardrails.mention_for(item["person_id"], item["person_name"])
        text = await self.llm.chase_nudge(
            mention=mention,
            what=item["what"],
            when=followups.humanize_age(followups.from_ts(item["promised_at"])),
            jump_url=item["jump_url"],
        )

        # Reply to the promise itself when it's still there, so the reminder sits
        # in context; otherwise post it in the channel.
        promise_msg = None
        try:
            promise_msg = await channel.fetch_message(item["message_id"])
        except discord.DiscordException:
            log.debug("[sweep] original promise %s is gone; posting standalone", item["message_id"])

        sent = await guardrails.send(
            channel,
            text,
            reason=f"chasing an overdue commitment ({item['reminders_sent'] + 1} of {self._max_attempts()})",
            kind="nudge",
            reply_to=promise_msg,
            extra={"person": item["person_name"], "what": item["what"], "chase_id": item["id"]},
        )
        if sent is None:
            return False

        try:
            await sent.add_reaction(CLOSE_EMOJI)
        except discord.DiscordException:
            log.debug("[sweep] could not add %s to nudge %s", CLOSE_EMOJI, sent.id)

        now_ts = followups.to_ts(followups.now_utc())
        try:
            self.db.mark_chase_reminded(item["id"], reminder_message_id=sent.id, at=now_ts)
            self.db.record_nudge(
                target_key=target_key,
                subject_key=subject_key,
                kind="chase",
                channel_id=item["channel_id"],
                message_id=sent.id,
                sent_at=now_ts,
            )
        except Exception:
            log.exception("[sweep] could not record nudge for chase %d", item["id"])

        log.info(
            "[sweep] NUDGED %s about %r (chase %d, attempt %d/%d)",
            item["person_name"], item["what"], item["id"],
            item["reminders_sent"] + 1, self._max_attempts(),
        )
        return True

    async def _give_up_on(self, item: dict) -> None:
        """Asked enough. Stop chasing, and FLAG it once in the channel it came
        from so the silence is visible rather than just forgotten.

        The flag names the person in plain text and does NOT ping them — they've
        already been asked the maximum number of times, and pinging again would be
        exactly the nagging the cap exists to prevent."""
        log.info(
            "[sweep] chase %d nudged %d time(s) with no answer → stale",
            item["id"], item["reminders_sent"],
        )

        if not item.get("flagged"):
            channel = self.get_channel(item["channel_id"])
            if channel is not None and guardrails.may_read(item["channel_id"]):
                who = item["person_name"]
                times = "once" if item["reminders_sent"] == 1 else f"{item['reminders_sent']} times"
                jump = f" {item['jump_url']}" if item["jump_url"] else ""
                sent = await guardrails.send(
                    channel,
                    f"I asked {who} {times} about {item['what']} with no reply, "
                    f"so I'm going to stop chasing it.{jump}",
                    reason="a chase went unanswered after every attempt; flagging it once",
                    kind="flag",
                    extra={"person": who, "what": item["what"], "chase_id": item["id"]},
                )
                if sent is not None:
                    try:
                        self.db.mark_chase_flagged(item["id"])
                    except Exception:
                        log.exception("[sweep] could not mark chase %d flagged", item["id"])

        try:
            self.db.set_chase_status(item["id"], "stale")
        except Exception:
            log.exception("[sweep] could not mark chase %d stale", item["id"])
            return
        state.audit(
            "chase_given_up",
            reason=f"no reply after {item['reminders_sent']} attempt(s); the cap is {self._max_attempts()}",
            person=item["person_name"],
            what=item["what"],
            chase_id=item["id"],
        )

    # -- state contract ----------------------------------------------------

    def _write_state_summary(self, *, trigger: str) -> None:
        """Rewrite state/summary.json. Called at startup and once a day. Best
        effort — a state file the bot couldn't write must not stop it working."""
        try:
            open_chases = self.db.list_open_chases()
        except Exception:
            log.exception("[state] could not read open chases for the summary")
            open_chases = []

        try:
            since = followups.to_ts(followups.now_utc() - timedelta(days=1))
            nudges_24h = self.db.count_nudges_since(since)
        except Exception:
            log.exception("[state] could not count recent nudges")
            nudges_24h = 0

        notable: list[dict] = []
        for src in sources.awaiting_access():
            notable.append(
                {
                    "kind": "source_unavailable",
                    "detail": f"{src['label']}: {src['detail']}",
                }
            )
        if not persona.policy_status()["loaded"]:
            notable.append(
                {
                    "kind": "policy_missing",
                    "detail": f"No readable policy file at {config.SALES_POLICY_FILE}.",
                }
            )
        invisible = [
            cid for cid in guardrails.readable_channel_ids() if self.get_channel(cid) is None
        ]
        if invisible:
            notable.append(
                {
                    "kind": "channel_not_visible",
                    "detail": (
                        f"{len(invisible)} configured sales channel(s) are not visible to the "
                        "bot — check its role has View Channel there: "
                        + ", ".join(str(c) for c in invisible)
                    ),
                }
            )
        if nudges_24h:
            notable.append(
                {"kind": "nudges_last_24h", "detail": f"{nudges_24h} reminder(s) sent in the last 24h."}
            )

        state.write_summary(
            source_statuses=sources.status_report(),
            open_chases=[
                {
                    "person": c["person_name"],
                    "what": c["what"],
                    "promised_at": c["promised_at"],
                    "due_at": c["due_at"],
                    "reminders_sent": c["reminders_sent"],
                    "channel_id": str(c["channel_id"]),
                    "jump_url": c["jump_url"],
                }
                for c in open_chases
            ],
            notable_events=notable,
            trigger=trigger,
        )

    def _maybe_write_daily_summary(self) -> None:
        """Rewrite the summary once per calendar day, at/after STATE_DAILY_HOUR.

        The last-written date lives in the `meta` table rather than in memory, so
        a restart doesn't cause a second write for the same day (or skip one)."""
        now = datetime.now(timezone.utc)
        hour = max(0, min(23, config.STATE_DAILY_HOUR))
        if now.hour < hour:
            return
        today = now.date().isoformat()
        try:
            if self.db.get_meta("state_summary_date") == today:
                return
        except Exception:
            log.exception("[state] could not read the daily-summary marker; skipping this tick")
            return

        log.info("[state] daily summary rewrite for %s", today)
        self._write_state_summary(trigger="daily")
        try:
            self.db.set_meta("state_summary_date", today)
        except Exception:
            log.exception("[state] could not record the daily-summary marker")

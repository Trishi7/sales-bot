"""The sales & marketing Chief of Staff bot.

Scoped to the sales channels, it does three things and nothing else:

  ANSWERS QUESTIONS. In the ask channel every message is a potential question; in
  the other sales channels an explicit @-mention is required. Questions go to the
  read-only tool-use engine (query_engine.py), which is handed Discord tools
  scoped to the sales channels plus the meeting-notes tools, and which answers
  under the policy in sales_policy.md — re-read on every question.

  CHASES DEADLINES — ONCE A DAY, IN ONE MESSAGE. When someone commits to
  something in a sales channel ("I'll send Acme the deck tomorrow"), that
  becomes a CHASE. Overdue chases, deadlines due today or tomorrow, replied-to
  rows nobody answered, stalled and dead-deal rows and anything past
  COS_NUDGE_MAX_ATTEMPTS all go out together in THE DAILY DIGEST at
  SALES_DIGEST_TIME — one message, grouped sections, hot first, and per-owner
  grouping so a person with four overdue items is pinged once. See digest.py.

  THIS BOT SENDS EXACTLY ONE UNPROMPTED MESSAGE A DAY. That is a property of the
  code, not a setting: `_maybe_post_daily_digest` is the only proactive send
  path left in this file. The individual reminder, chase, escalation and
  hygiene-flag sends were deleted. The two things that are still immediate are
  answers rather than interruptions — a REPLY to a question, and the ask-time
  deadline announcement in `_set_deadline_for`, which is what someone asked for
  and carries the "shout to change" consent mechanism.

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
import deadlines as dl
import digest
import followups
import gtm_sheet
import guardrails
import mapping_sheet
import notes
import persona
import query
import sources
import state
import tracker
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
        self._notes_syncer: Optional[asyncio.Task] = None
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
        visible, invisible = [], []
        for cid in guardrails.readable_channel_ids():
            chan = self.get_channel(cid)
            if chan is None:
                invisible.append(cid)
                log.warning(
                    "[bot] sales channel %s is NOT visible — the bot's role may lack "
                    "View Channel there, or the id is wrong. It will be skipped.", cid,
                )
            else:
                visible.append(cid)
                log.info("[bot] reading #%s (%s)", getattr(chan, "name", "?"), cid)
        log.info(
            "[bot] channel scope: %d of %d configured sales channel(s) visible%s",
            len(visible), len(visible) + len(invisible),
            f"; UNREACHABLE: {invisible}" if invisible else "",
        )

        # The GTM sheets. Probed once here, on a thread (the API call blocks), so
        # a missing share surfaces as one actionable startup line instead of as a
        # confusing "no deals" answer hours later.
        await asyncio.to_thread(self._check_sheets)

        # The researcher/buyer mapping sheet. Checked separately from the
        # playbook because it is a separate source with a separate failure mode:
        # the playbook can be perfectly readable while the mapping isn't shared,
        # and reporting that as one healthy "spreadsheet" would hide it.
        await asyncio.to_thread(self._check_mapping_sheet)

        # The meeting notes. Sync BEFORE the source statuses below, so what they
        # report is what the sync actually left on disk. A sync failure is not
        # fatal by design: it is logged once with the fix, the source reports
        # DEGRADED, and the bot keeps running on whatever it already has.
        await asyncio.to_thread(notes.ensure_dir)
        if config.NOTES_SYNC_CMD:
            await asyncio.to_thread(notes.sync_now, reason="startup")
            if self._notes_syncer is None or self._notes_syncer.done():
                self._notes_syncer = asyncio.create_task(self._notes_sync_loop())
                log.info(
                    "[bot] meeting-notes sync started (every %d min)",
                    max(1, config.NOTES_SYNC_MINUTES),
                )
        else:
            log.info(
                "[bot] NOTES_SYNC_CMD is unset — meeting notes are read from %s as-is and "
                "never refreshed.", config.NOTES_DIR or "(no folder configured)",
            )

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

    def _check_sheets(self) -> dict:
        """STARTUP CHECK for both GTM spreadsheets. Blocking; called via a thread.

        Never raises. When the service account can't open a sheet, the exact
        share instruction is logged at ERROR — naming the account and the access
        level — and the source reports awaiting-access. A missing share is a
        one-line fix, and this is the line.

        On success, each recognised tab's schema is logged once (that logging
        lives in `gtm_sheet.read`, keyed so a restart re-logs but a re-read
        doesn't).
        """
        access = gtm_sheet.SHEETS.check_access()
        email = gtm_sheet.SHEETS.service_account_email

        if email:
            log.info("[bot] GTM service account: %s", email)
        else:
            log.warning(
                "[bot] no service-account key readable — GTM sheet access is off "
                "(set GOOGLE_SERVICE_ACCOUNT_JSON)."
            )

        for which in (gtm_sheet.ORIGINAL, gtm_sheet.COPY):
            info = access.get(which, {})
            if info.get("ok"):
                log.info(
                    "[bot] GTM %s sheet reachable: %r (%d tabs)",
                    which, info.get("title"), len(info.get("tabs") or []),
                )
            else:
                log.error(
                    "[bot] GTM %s sheet NOT reachable: %s", which, info.get("error")
                )
                if info.get("remedy"):
                    log.error("[bot] ACTION REQUIRED: %s", info["remedy"])

        # Read once so the schema summary is logged and the cache is warm before
        # the first question arrives.
        if access.get(gtm_sheet.ORIGINAL, {}).get("ok"):
            try:
                tabs = gtm_sheet.SHEETS.read(gtm_sheet.ORIGINAL, force=True)
                for kind, tab in tabs.items():
                    log.info("[bot] GTM schema: %s", tab.schema_line())
                if not tabs:
                    log.warning(
                        "[bot] the GTM Playbook is readable but none of its tabs matched a "
                        "known kind (tracker / positioning matrix / prospect priority). "
                        "Set GTM_COLUMN_MAP if the headers have been renamed."
                    )
            except gtm_sheet.SheetAccessError as e:
                log.error("[bot] could not read the GTM Playbook: %s", e)

        state.audit(
            "sheet_access_check",
            reason="startup verification of GTM spreadsheet access",
            service_account=email or None,
            original_ok=bool(access.get(gtm_sheet.ORIGINAL, {}).get("ok")),
            copy_ok=bool(access.get(gtm_sheet.COPY, {}).get("ok")),
            write_target=config.SHEET_WRITE_TARGET,
        )
        return access

    def _check_mapping_sheet(self) -> dict:
        """STARTUP CHECK for the researcher/buyer mapping sheet. Blocking; called
        via a thread. Never raises.

        Same contract as `_check_sheets`: on failure the exact share line is
        logged at ERROR, naming the service account and asking for VIEWER — which
        is all this sheet ever needs, because the bot never writes to it.

        On success it also logs the RULES it loaded, not just the row counts. The
        legend is the rule set every recommendation from this sheet has to obey,
        so "did the legend parse, and is the sheet already past its own refresh
        window" is startup information, not a detail: a mapping sheet that reads
        fine but whose DEPARTURES row went missing will happily recommend someone
        who left three months ago, and this is where that gets noticed.
        """
        access = mapping_sheet.MAPPING.check_access()
        email = mapping_sheet.MAPPING.service_account_email

        if not access.get("ok"):
            log.error("[bot] mapping sheet NOT reachable: %s", access.get("error"))
            if access.get("remedy"):
                log.error("[bot] ACTION REQUIRED: %s", access["remedy"])
            state.audit(
                "mapping_access_check",
                reason="startup verification of researcher-mapping access",
                service_account=email or None, ok=False,
                error=access.get("error"), write_target="none (read-only sheet)",
            )
            return access

        log.info(
            "[bot] mapping sheet reachable (READ-ONLY): %r (%d tabs)",
            access.get("title"), len(access.get("tabs") or []),
        )
        try:
            tabs = mapping_sheet.MAPPING.read(force=True)
            for tab in tabs.values():
                log.info("[bot] mapping schema: %s", tab.schema_line())
            if mapping_sheet.RESEARCHERS not in tabs:
                log.warning(
                    "[bot] the mapping sheet is readable but no tab matched the "
                    "researcher mapping (a Researcher + Tier + Confidence table). No "
                    "'who do we pitch at X' question can be answered. Set "
                    "GTM_MAPPING_COLUMN_MAP if the headers were renamed."
                )
        except gtm_sheet.SheetAccessError as e:
            log.error("[bot] could not read the mapping sheet: %s", e)
            return access

        legend = mapping_sheet.MAPPING.legend()
        departures = mapping_sheet.MAPPING.departures()
        log.info(
            "[bot] mapping legend: built=%s refresh=~%dw lanes=%s tiers=%s",
            legend.built, legend.refresh_weeks,
            ",".join(sorted(legend.lanes)) or "(none)",
            ",".join(sorted(legend.tiers)) or "(none)",
        )
        log.info(
            "[bot] mapping exclusions: %d non-buyer(s) %s, %d budget-gate failure(s) %s",
            len(legend.non_buyers), sorted(legend.non_buyers) or "",
            len(legend.budget_gate), sorted(legend.budget_gate) or "",
        )
        if departures.people:
            log.info(
                "[bot] mapping DEPARTURES list loaded: %d person(s) who must never be "
                "recommended", len(departures.people),
            )
        else:
            log.warning(
                "[bot] the mapping sheet has NO readable DEPARTURES row — the "
                "do-not-pitch check cannot run. Answers will say so, but someone should "
                "check the Edge Map tab."
            )

        if legend.built:
            age = (dl.today_ist() - legend.built).days
            if age > legend.stale_after_days():
                log.warning(
                    "[bot] the mapping sheet's research pass is %d days old (built %s), "
                    "past its own ~%d-week refresh rule — EVERY recommendation from it "
                    "now carries a re-verify-role caveat.",
                    age, legend.built.isoformat(), legend.refresh_weeks,
                )
            else:
                log.info(
                    "[bot] mapping research pass is %d days old; its ~%d-week refresh "
                    "rule bites in %d day(s).",
                    age, legend.refresh_weeks, legend.stale_after_days() - age,
                )

        state.audit(
            "mapping_access_check",
            reason="startup verification of researcher-mapping access",
            service_account=email or None, ok=True,
            tabs=len(access.get("tabs") or []),
            departures_loaded=len(departures.people),
            legend_built=legend.built.isoformat() if legend.built else None,
            write_target="none (read-only sheet)",
        )
        return access

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
        """✅ on the PROMISE closes that chase — "handled, stop asking".

        It used to be ✅ on the nudge, and there are no nudges any more: an
        overdue promise is one line in a digest covering many of them, so a ✅
        on the digest couldn't say WHICH one was handled and would close an
        arbitrary chase. Reacting to the promise itself is unambiguous, and the
        digest's OVERDUE lines are what tell people which promises to go find.

        `find_chase_by_reminder` is still tried second, so a ✅ on one of the
        individual nudges sent before this change still closes its chase.
        """
        if str(payload.emoji) != CLOSE_EMOJI:
            return
        if payload.user_id == getattr(self.user, "id", None):
            return
        if not guardrails.may_read(payload.channel_id):
            return
        try:
            item = (
                self.db.find_chase_by_source(payload.message_id)
                or self.db.find_chase_by_reminder(payload.message_id)
            )
        except Exception:
            log.exception("[bot] chase lookup failed for reaction on %s", payload.message_id)
            return
        if not item:
            return
        self._close_chase(item, why="someone marked the promise as handled")

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
                tools=(
                    self._discord_tools(message)
                    + self._notes_tools(text)
                    + self._sheet_tools(message)
                    + self._mapping_tools()
                ),
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

        async def _sync(reason_question: str) -> dict:
            """Refresh the notes before answering. Forced when the question is
            about a recent meeting, otherwise only when the folder is stale.
            Blocking work goes to a thread; a failed sync degrades the answer
            (the model is told), it never blocks it."""
            try:
                return await asyncio.to_thread(notes.sync_for_question, reason_question)
            except Exception:
                log.exception("[notes] pre-answer sync raised; answering from what's on disk")
                return {"ran": True, "ok": False, "forced": False, "configured": True,
                        "degraded": True, "error": "the sync raised", "remedy": None}

        def _filter_facts() -> dict:
            """What the exclusion filter did to the synced folder — so the model
            can say "27 docs came down, 25 loaded, 2 standups excluded" instead of
            a bare "no notes"."""
            st = notes.sync_status()
            undated = max(0, st["docs_seen"] - st["notes_seen"])
            return {
                "docs_on_disk": st["docs_seen"],
                "notes_loaded": st["docs_loaded"],
                "notes_excluded_as_standups": st["docs_excluded"],
                "files_without_a_date": undated,
                "exclude_patterns": st["exclude_patterns"],
                "last_successful_sync": st["last_success"],
                "sync_degraded": st["degraded"],
                "sync_problem": st["error"],
                "note": (
                    "The filter is EXCLUDE-based: EVERY synced meeting note is loaded "
                    "except those whose title matches exclude_patterns (the recurring "
                    "product standups), which stay on disk unread. So notes_loaded=0 "
                    "with docs_on_disk>0 does NOT mean nothing was discussed — it means "
                    + (
                        "everything that came down was either a standup or had no "
                        "parseable date. Say which, using the counts above."
                        if not st["degraded"]
                        else "everything on disk is a standup or undated AND the sync is "
                        "currently failing, so a real note may not have come down — "
                        "say both."
                    )
                ),
            }

        async def _list_meeting_notes(inp: dict):
            not_cfg = await _not_configured()
            if not_cfg:
                return not_cfg
            sync = await _sync(question_text)
            try:
                days = int(inp.get("days") or 30)
            except (TypeError, ValueError):
                days = 30
            found = await asyncio.to_thread(notes.list_notes, max(1, days))
            return {
                "enabled": True,
                "configured": True,
                "sync": sync,
                "notes_filter": _filter_facts(),
                "notes": found,
                "freshness": await _freshness(),
            }

        async def _read_meeting_note(inp: dict):
            not_cfg = await _not_configured()
            if not_cfg:
                return not_cfg

            # Sync first. `sync_for_question` forces a pull for a question about a
            # recent meeting and otherwise honours NOTES_SYNC_MINUTES, so asking
            # twice in a row doesn't run rclone twice. Best-effort: we read
            # regardless, and report whether the sync RAN and whether it SUCCEEDED
            # so a failed sync is reported as "data may be stale" rather than hidden.
            sync = await _sync(question_text)

            note = await asyncio.to_thread(
                notes.read_note, inp.get("date") or None, inp.get("label") or None
            )
            freshness = await _freshness()
            facts = _filter_facts()

            if not note:
                return {
                    "enabled": True,
                    "configured": True,
                    "found": False,
                    "sync": sync,
                    "notes_filter": facts,
                    "freshness": freshness,
                    "note": (
                        "No matching meeting note on file"
                        + (" (even after an on-demand sync)" if sync.get("ran") else "")
                        + (
                            " — the sync FAILED, so the data may be stale"
                            if sync.get("ran") and not sync.get("ok")
                            else ""
                        )
                        + (
                            f". {facts['docs_on_disk']} synced doc(s) are on disk but none "
                            f"were loaded ({facts['notes_excluded_as_standups']} excluded as "
                            f"standups, {facts['files_without_a_date']} with no parseable "
                            "date), so none were read."
                            if facts["docs_on_disk"] and not facts["notes_loaded"]
                            else "."
                        )
                    ),
                }

            same_day = await asyncio.to_thread(notes.labels_on, note.get("date"))
            others = [lbl for lbl in same_day if lbl != (note.get("label") or "")]
            return {
                "enabled": True,
                "configured": True,
                "found": True,
                "sync": sync,
                "notes_filter": facts,
                "freshness": freshness,
                "other_meetings_that_day": others,
                "meeting_note": note,
            }

        return [
            {
                "schema": {
                    "name": "list_meeting_notes",
                    "description": (
                        "List recent meeting notes as [{date, label, title, path}], "
                        "newest first, plus 'freshness', 'sync' (was the folder refreshed just "
                        "now, did it succeed) and 'notes_filter' (docs on disk vs loaded vs "
                        "excluded). 'label' names WHICH meeting ('Pipeline review', 'Acme "
                        "call') and may be null when the note wasn't labelled. Use when the "
                        "question is vague about which meeting, or asks what notes exist. "
                        "EVERY synced meeting note is listed EXCEPT the recurring product "
                        "standups (titles matching notes_filter.exclude_patterns), which are "
                        "deliberately kept out. If configured=false, notes access isn't set "
                        "up; if notes is empty while notes_filter.docs_on_disk is not, read "
                        "the counts — they say how many were excluded as standups and how "
                        "many had no parseable date. Say which of those it is — do NOT say "
                        "nothing was discussed."
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
                        "raw}}, plus 'freshness', 'sync' (did an on-demand sync run and "
                        "succeed — if sync.ok is false, say the notes may be stale and pass on "
                        "sync.remedy), 'notes_filter' (docs on disk vs loaded vs excluded) and "
                        "'other_meetings_that_day' (mention them if relevant). "
                        "Pass 'date' (YYYY-MM-DD) computed by YOU from today's date for "
                        "'today' / 'yesterday' / a weekday / an explicit date; OMIT it for the "
                        "most recent note on file. Pass 'label' only when they name a meeting "
                        "('the pipeline review') — it matches as a substring. "
                        "When you use this you MUST state which note you read (e.g. 'the "
                        "pipeline review, 14 Aug') and the freshness line. If found=false, say "
                        "that note isn't on file and name the most recent one that IS — NEVER "
                        "answer from a different day's note as if it were the one asked for, "
                        "and never imply a meeting didn't happen. A next_steps entry with "
                        "owner_name null is UNOWNED: report it that way, don't assign it. "
                        "Every synced meeting note is readable here EXCEPT the recurring "
                        "product standups (titles matching notes_filter.exclude_patterns). "
                        "When notes_filter.notes_loaded is 0 and docs_on_disk is not, quote "
                        "the counts — notes_excluded_as_standups and files_without_a_date say "
                        "exactly where everything went — rather than reporting silence."
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

    # -- tools: the GTM spreadsheet ----------------------------------------

    def _sheet_tools(self, message: discord.Message) -> list[dict]:
        """Read-only tools over the GTM Playbook, plus the one deadline-SETTING
        tool.

        Reads are live (cached 60s) and every result carries a `staleness` note
        when it came from cache after an API failure, so an answer can never
        silently present hours-old data as current.

        Every tool returns the TAB TITLE and, per row, the COMPANY — the prompt
        requires answers to cite both, and a tool result that omitted them would
        make that impossible to honour.
        """

        def _tab_or_error(kind: str):
            """(tab, error_dict). The error dict is the tool result to return
            when the sheet can't be read — it names the fix, so the reply can
            too."""
            try:
                tab = gtm_sheet.SHEETS.tab(kind)
            except gtm_sheet.SheetAccessError as e:
                return None, {
                    "available": False,
                    "error": str(e),
                    "fix": e.remedy,
                    "note": (
                        "I can't read the GTM Playbook right now, and I have no cached "
                        "copy. Say this plainly — do NOT answer the question from memory "
                        "or from another source as if it were the sheet."
                    ),
                }
            if tab is None:
                return None, {
                    "available": False,
                    "error": f"the GTM Playbook has no recognised {kind} tab",
                    "fix": "Check the tab still exists, or set GTM_COLUMN_MAP if its headers were renamed.",
                }
            return tab, None

        def _row_out(row: dict, tab) -> dict:
            """One row as the model should see it: the mapped roles that are
            actually filled, the extras, and the sheet row number. Empty cells
            are listed in `empty_fields` rather than omitted — the prompt
            requires saying when a cell is blank, which it can only do if it
            knows which ones are."""
            filled, empty = {}, []
            for role in tab.role_to_col:
                value = (row.get(role) or "").strip()
                if value:
                    filled[role] = value
                else:
                    empty.append(role)
            extras = {k: v for k, v in (row.get("_extra") or {}).items() if str(v).strip()}
            return {
                "sheet_row": row.get("_row"),
                "fields": filled,
                "empty_fields": empty,
                "other_columns": extras,
            }

        async def _lookup_company(inp: dict):
            name = str(inp.get("company") or "").strip()
            if not name:
                return {"error": "lookup_company needs a 'company'."}
            tab, err = await asyncio.to_thread(_tab_or_error, gtm_sheet.TRACKER)
            if err:
                return err
            matches = tab.find_company(name)
            out = {
                "available": True,
                "tab": tab.title,
                "query": name,
                "match_count": len(matches),
                "rows": [_row_out(r, tab) for r in matches[:5]],
                "staleness": gtm_sheet.SHEETS.staleness_note(tab),
            }
            if not matches:
                out["note"] = (
                    f"No row in {tab.title!r} matches {name!r}. Say that plainly — it means "
                    "we have no tracked outreach for them, NOT that the deal is cold. Do "
                    "not invent a row."
                )
            else:
                # Deadlines live in SQLite, not the sheet, so surface them here.
                known = self.db.list_deadlines_for(matches[0].get("company", name))
                if known:
                    out["deadlines"] = [
                        {"kind": d["kind"], "due_date": d["due_date"],
                         "rule": d["rule"], "source": d["source"], "status": d["status"]}
                        for d in known
                    ]
            return out

        async def _query_tracker(inp: dict):
            tab, err = await asyncio.to_thread(_tab_or_error, gtm_sheet.TRACKER)
            if err:
                return err
            rows = list(tab.rows)
            today = dl.today_ist()

            filt = str(inp.get("filter") or "all").strip().lower()
            if filt == "no_response":
                rows = [r for r in rows if not tracker.has_responded(r) and tracker.is_open(r)]
            elif filt == "responded":
                rows = [r for r in rows if tracker.has_responded(r)]
            elif filt == "never_contacted":
                rows = [
                    r for r in rows
                    if not (r.get("first_contacted") or "").strip()
                    and not (r.get("intro_date") or "").strip()
                ]
            elif filt == "no_next_step":
                rows = [r for r in rows if not (r.get("next_steps") or "").strip()]
            elif filt == "no_poc":
                rows = [r for r in rows if not (r.get("poc") or "").strip()]
            elif filt == "meetings":
                rows = [r for r in rows if (r.get("meeting_date") or "").strip()]
            elif filt == "open":
                rows = [r for r in rows if tracker.is_open(r)]

            industry = str(inp.get("industry") or "").strip().lower()
            if industry:
                rows = [r for r in rows if industry in (r.get("industry") or "").lower()]
            use_case = str(inp.get("use_case") or "").strip().lower()
            if use_case:
                rows = [r for r in rows if use_case in (r.get("use_case") or "").lower()]

            try:
                limit = max(1, min(60, int(inp.get("limit") or 25)))
            except (TypeError, ValueError):
                limit = 25

            return {
                "available": True,
                "tab": tab.title,
                "filter": filt,
                "total_rows_in_tab": len(tab.rows),
                "match_count": len(rows),
                "returned": min(len(rows), limit),
                "truncated": len(rows) > limit,
                "today_ist": dl.iso(today),
                "rows": [_row_out(r, tab) for r in rows[:limit]],
                "staleness": gtm_sheet.SHEETS.staleness_note(tab),
            }

        async def _priority_list(inp: dict):
            # SEVERAL tabs are legitimately prospect lists (a master lead list, a
            # Fortune-500 list, a vertical list). Answering "which P1s…" from
            # only one of them would silently omit most of the pipeline, so this
            # searches all of them and labels every row with the tab it came from.
            try:
                tabs = await asyncio.to_thread(
                    gtm_sheet.SHEETS.tabs_of, gtm_sheet.PRIORITY
                )
            except gtm_sheet.SheetAccessError as e:
                return {"available": False, "error": str(e), "fix": e.remedy}
            tabs = [t for t in tabs if t.rows]
            if not tabs:
                return {
                    "available": False,
                    "error": "the GTM Playbook has no prospect-priority tab with rows in it",
                }

            want = str(inp.get("priority") or "").strip()
            wanted_band = gtm_sheet.parse_priority(want) if want else None

            rows = []
            for tab in tabs:
                for r in tab.rows:
                    band = gtm_sheet.parse_priority(r.get("priority"))
                    if wanted_band is not None and band != wanted_band:
                        continue
                    rows.append({**_row_out(r, tab), "tab": tab.title, "priority_band": band})

            out = {
                "available": True,
                "tab": ", ".join(t.title for t in tabs),
                "tabs_searched": [{"tab": t.title, "rows": len(t.rows)} for t in tabs],
                "priority_filter": wanted_band,
                "match_count": len(rows),
                "returned": min(len(rows), 60),
                "truncated": len(rows) > 60,
                "rows": rows[:60],
                "staleness": gtm_sheet.SHEETS.staleness_note(tabs[0]),
                "note": (
                    "Prospects live across SEVERAL tabs. Every row above carries the 'tab' "
                    "it came from — cite it, and quote match_count rather than implying the "
                    "list is complete when truncated is true."
                ),
            }

            # "Which P1s have we never contacted" needs both tabs, so join here
            # rather than making the model do it across two calls.
            if inp.get("cross_check_contacted"):
                tracker_tab, terr = await asyncio.to_thread(_tab_or_error, gtm_sheet.TRACKER)
                if terr:
                    out["contact_cross_check"] = terr
                else:
                    # Index the tracker ONCE. The naive version re-scanned 886
                    # tracker rows for each of ~500 prospects; this is the same
                    # answer for one pass instead of half a million comparisons.
                    touched_keys = set()
                    for h in tracker_tab.rows:
                        if (h.get("first_contacted") or "").strip() or (
                            h.get("intro_date") or ""
                        ).strip():
                            key = self.db.company_key(h.get("company", ""))
                            if key:
                                touched_keys.add(key)

                    never, contacted = [], []
                    for r in rows:
                        company = r["fields"].get("company", "")
                        if not company:
                            continue
                        key = self.db.company_key(company)
                        (contacted if key in touched_keys else never).append(company)

                    out["contact_cross_check"] = {
                        "tracker_tab": tracker_tab.title,
                        "never_contacted_count": len(never),
                        "contacted_count": len(contacted),
                        "never_contacted": never[:60],
                        "contacted": contacted[:30],
                        "truncated": len(never) > 60 or len(contacted) > 30,
                        "note": (
                            "'never_contacted' means no First Contacted and no intro date in "
                            f"{tracker_tab.title!r}, or no row there at all. Company names are "
                            "matched loosely (case, punctuation and legal suffixes ignored), so "
                            "'Acme Pvt Ltd' and 'acme' are one company. Quote the COUNTS — the "
                            "lists are capped."
                        ),
                    }
            return out

        async def _positioning(inp: dict):
            tab, err = await asyncio.to_thread(_tab_or_error, gtm_sheet.POSITIONING)
            if err:
                return err
            want = str(inp.get("company_type") or inp.get("use_case") or "").strip().lower()
            rows = tab.rows
            if want:
                rows = [
                    r for r in tab.rows
                    if want in (r.get("company_type") or "").lower()
                    or want in (r.get("icp") or "").lower()
                    or want in (r.get("use_case") or "").lower()
                    or want in (r.get("label") or "").lower()
                    or want in (r.get("problem") or "").lower()
                ] or tab.rows  # nothing matched → give the whole matrix, don't guess
            return {
                "available": True,
                "tab": tab.title,
                "query": want,
                "match_count": len(rows),
                "rows": [_row_out(r, tab) for r in rows[:12]],
                "staleness": gtm_sheet.SHEETS.staleness_note(tab),
                "note": (
                    "Use this to answer 'what do we pitch to <company type>': match on "
                    "Company Type / ICP, then give the Use Case, Problem Statement, "
                    "Offering and Business Impact as written. Quote the matrix; don't "
                    "invent positioning."
                ),
            }

        async def _row_flags(inp: dict):
            tab, err = await asyncio.to_thread(_tab_or_error, gtm_sheet.TRACKER)
            if err:
                return err
            flagged = tracker.all_flags(tab.rows)
            return {
                "available": True,
                "tab": tab.title,
                "counts": {
                    kind: sum(1 for f in flagged if f["_flag"] == kind)
                    for kind in (tracker.FLAG_HOT, tracker.FLAG_STALLED, tracker.FLAG_DEAD)
                },
                "flags": [
                    {
                        "flag": tracker.FLAG_LABELS.get(f["_flag"], f["_flag"]),
                        "company": f.get("company", ""),
                        "poc": f.get("poc", ""),
                        "why": f.get("_why", ""),
                        "sheet_row": f.get("_row"),
                    }
                    for f in flagged[:30]
                ],
                "staleness": gtm_sheet.SHEETS.staleness_note(tab),
            }

        async def _set_deadline(inp: dict):
            company = str(inp.get("company") or "").strip()
            kind = str(inp.get("kind") or dl.KIND_OUTREACH).strip()
            if not company:
                return {"error": "set_deadline needs a 'company'."}
            if kind not in dl.KINDS:
                kind = dl.KIND_OUTREACH
            return await self._set_deadline_for(
                company=company, kind=kind, channel=message.channel, unprompted=False
            )

        async def _list_deadlines(inp: dict):
            company = str(inp.get("company") or "").strip()
            items = (
                self.db.list_deadlines_for(company) if company
                else self.db.list_open_deadlines()
            )
            return {
                "today_ist": dl.iso(dl.today_ist()),
                "count": len(items),
                "deadlines": [
                    {
                        "company": d["company"], "kind": d["kind"],
                        "due_date": d["due_date"], "rule": d["rule"],
                        "source": d["source"], "status": d["status"],
                        "chases_sent": d["chases_sent"],
                        "sheet_cell": d["sheet_cell"], "sheet_target": d["sheet_target"],
                    }
                    for d in items[:60]
                ],
                "note": (
                    "source='human' means someone typed the date into the sheet and it "
                    "wins over anything I derive; source='bot' means I set it from the "
                    "cadence and it can be changed by saying so."
                ),
            }

        return [
            {
                "schema": {
                    "name": "lookup_company",
                    "description": (
                        "Everything the GTM outreach tracker holds about ONE company, plus any "
                        "deadlines I'm holding for it. USE THIS FIRST for 'where are we with "
                        "X', 'what did we send X', 'who's the PoC at X', 'has X replied'. "
                        "Returns {tab, match_count, rows:[{sheet_row, fields, empty_fields, "
                        "other_columns}], deadlines, staleness}. CITE THE TAB AND THE COMPANY "
                        "in your answer. 'empty_fields' lists cells that are BLANK — say so "
                        "when the answer depends on one ('no Meeting Date is recorded'); never "
                        "fill a blank in from elsewhere. If match_count is 0 there is no such "
                        "row: say that, don't invent one. If 'staleness' is non-empty, include "
                        "it — the data came from cache after an API failure."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {"company": {"type": "string", "description": "Company name; partial is fine."}},
                        "required": ["company"],
                    },
                },
                "handler": _lookup_company,
            },
            {
                "schema": {
                    "name": "query_tracker",
                    "description": (
                        "Filtered slices of the outreach tracker, for questions about MANY "
                        "rows: 'who hasn't replied', 'which rows have no next step', 'who has "
                        "no PoC', 'what meetings are booked', 'who have we never contacted'. "
                        "'filter' is one of: all, open, no_response, responded, never_contacted, "
                        "no_next_step, no_poc, meetings. Narrow further with 'industry' or "
                        "'use_case'. Returns rows in the same shape as lookup_company plus "
                        "match_count/truncated — quote match_count rather than implying you "
                        "listed everything, and cite the tab."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "filter": {
                                "type": "string",
                                "enum": ["all", "open", "no_response", "responded",
                                         "never_contacted", "no_next_step", "no_poc", "meetings"],
                                "description": "Which slice of the tracker.",
                            },
                            "industry": {"type": "string", "description": "Optional industry substring."},
                            "use_case": {"type": "string", "description": "Optional use-case substring."},
                            "limit": {"type": "integer", "description": "Max rows to return (default 25, cap 60)."},
                        },
                        "required": [],
                    },
                },
                "handler": _query_tracker,
            },
            {
                "schema": {
                    "name": "prospect_priority",
                    "description": (
                        "The prospect-priority tab: scored companies with a P1/P2/P3 band and "
                        "a rationale. Pass 'priority' ('P1') to filter. Set "
                        "'cross_check_contacted' true for questions like 'which P1s have we "
                        "never contacted' — it joins against the outreach tracker and returns "
                        "never_contacted / contacted lists. Cite BOTH tabs when you use the "
                        "cross-check, and quote the rationale as written rather than "
                        "summarising it into something the sheet doesn't say."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "priority": {"type": "string", "description": "P1 / P2 / P3. Omit for all."},
                            "cross_check_contacted": {
                                "type": "boolean",
                                "description": "Join against the tracker to find who has never been contacted.",
                            },
                        },
                        "required": [],
                    },
                },
                "handler": _priority_list,
            },
            {
                "schema": {
                    "name": "positioning_matrix",
                    "description": (
                        "The positioning matrix — use cases A–I with Label, Use Case, Problem "
                        "Statement, Offering, Company Type, ICP and Business Impact. THIS is "
                        "how you answer 'what do we pitch to <company type>', 'what's the "
                        "use case for <industry>', 'what's our offering for X'. Pass "
                        "'company_type' or 'use_case' to narrow; with no match you get the "
                        "whole matrix, so pick the closest row and SAY which one you picked. "
                        "Quote the matrix's own words — this is the team's agreed positioning "
                        "and paraphrasing it into something else is how off-message pitches "
                        "happen."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "company_type": {"type": "string", "description": "Company type / ICP to match."},
                            "use_case": {"type": "string", "description": "Use case or label to match."},
                        },
                        "required": [],
                    },
                },
                "handler": _positioning,
            },
            {
                "schema": {
                    "name": "row_flags",
                    "description": (
                        "Rows the hygiene rules currently flag: HOT (replied, no follow-up "
                        "since), STALLED (open, no response, last touch older than the "
                        "cadence) and DEAD-DEAL (no next step and no future date). Use for "
                        "'what needs attention', 'what's slipping', 'anything urgent'. Each "
                        "flag carries the reason it fired, in terms of the cells it read — "
                        "repeat that reason so the team can check it."
                    ),
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                },
                "handler": _row_flags,
            },
            {
                "schema": {
                    "name": "set_deadline",
                    "description": (
                        "SET a deadline for a company when none exists, and announce it in "
                        "channel. Use this whenever someone asks about a deadline, a due date "
                        "or 'when should we follow up' and there ISN'T one — you have the "
                        "authority to set it rather than asking them to pick a date. 'kind' is "
                        "outreach_followup (default), reply_chase, or meeting_prep. The date "
                        "is derived from the strategy doc's cadence when readable, else the "
                        "configured working-day defaults, in IST. Returns the date, the rule "
                        "used and whether it was announced and mirrored to the sheet. If it "
                        "comes back action='kept_human', a person's own date already exists "
                        "and wins — report THEIR date. Do NOT also repeat the announcement in "
                        "your reply; it has already been posted."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string", "description": "The company the deadline is for."},
                            "kind": {
                                "type": "string",
                                "enum": ["outreach_followup", "reply_chase", "meeting_prep"],
                                "description": "Which deadline. Defaults to outreach_followup.",
                            },
                        },
                        "required": ["company"],
                    },
                },
                "handler": _set_deadline,
            },
            {
                "schema": {
                    "name": "list_deadlines",
                    "description": (
                        "Deadlines I'm holding — all open ones, or just one company's. Use for "
                        "'what's due', 'when are we following up with X', 'what deadlines are "
                        "there'. These live in my own store, not in the sheet, so they are "
                        "authoritative even when the sheet write failed. 'source' tells you "
                        "whether a person set it (wins, never overwritten) or I did."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {"company": {"type": "string", "description": "Optional; omit for everything open."}},
                        "required": [],
                    },
                },
                "handler": _list_deadlines,
            },
        ]

    def _mapping_tools(self) -> list[dict]:
        """READ-ONLY tools over the researcher/buyer mapping sheet.

        There is no write tool here and there is no set_* handler, because the
        bot never writes to that sheet. The whole surface is lookups.

        THE RULES TRAVEL WITH THE DATA. Every researcher row goes through
        `mapping_sheet.MAPPING.enrich()` before the model sees it, so the
        departure check, the staleness verdict computed against today, the org's
        exclusion flags and the row's own Watch-outs arrive attached to the pitch
        hook rather than in a separate paragraph the model might not read. A tool
        that returned a bare hook would be a tool that let the sheet's own rules
        be skipped, and the sheet is explicit that they must not be.
        """

        def _unavailable(e) -> dict:
            return {
                "available": False,
                "error": str(e),
                "fix": getattr(e, "remedy", ""),
                "note": (
                    "I can't read the researcher/buyer mapping sheet and have no cached "
                    "copy. Say this plainly — do NOT answer 'who should we pitch' from "
                    "memory, from the outreach tracker, or from general knowledge about "
                    "who works where. This sheet is the only place that mapping exists."
                ),
            }

        def _rules_block() -> dict:
            """The legend's rules, restated as instructions, on EVERY result.

            Repeated per call on purpose: this is what the answer has to obey, and
            a rule stated once in a system prompt loses to a row of data quoted in
            a tool result ten turns later.
            """
            legend = mapping_sheet.MAPPING.legend()
            return {
                "icp_lanes": legend.lanes,
                "tiers": legend.tiers,
                "confidence_means": legend.confidence_rule,
                "you_must": [
                    "CITE person + org + TIER + CONFIDENCE every single time you name "
                    "someone from this sheet. Never one without the other.",
                    "Tier and Confidence are INDEPENDENT by design — Tier is buyer fit, "
                    "Confidence is evidence quality. Never merge them into one score and "
                    "never let a high Confidence imply a high Tier.",
                    "If a row's 'staleness.caveat' is non-empty, include it verbatim in "
                    "your answer. That is the sheet's own refresh rule, computed against "
                    "today's date.",
                    "If a row has 'do_not_recommend', that person has LEFT — never "
                    "recommend them, and say where they went.",
                    "If a row or org has 'do_not_pitch', they are NOT a pitch target. Say "
                    "what the sheet says and why they're excluded; do not soften it into "
                    "a maybe.",
                    "If 'watch_outs' is non-empty, state it in the same breath as the "
                    "hook — never quote a hook without its watch-out.",
                    "Quote 'why_them' as the sheet wrote it. It is the agreed hook; "
                    "paraphrasing it is how an off-message pitch happens.",
                ],
                "departures_check_available": mapping_sheet.MAPPING.departures_known(),
            }

        def _enriched(rows: list[dict], limit: int) -> list[dict]:
            return [mapping_sheet.MAPPING.enrich(r) for r in rows[:limit]]

        def _staleness_note() -> str:
            tab = mapping_sheet.MAPPING.tab(mapping_sheet.RESEARCHERS)
            return mapping_sheet.MAPPING.staleness_note(tab) if tab else ""

        async def _who_to_pitch(inp: dict):
            org = str(inp.get("org") or "").strip()
            lane = str(inp.get("lane") or "").strip()
            tier = str(inp.get("tier") or "").strip()
            person = str(inp.get("person") or "").strip()
            try:
                limit = max(1, min(25, int(inp.get("limit") or 10)))
            except (TypeError, ValueError):
                limit = 10

            if not any((org, lane, tier, person)):
                return {"error": "who_to_pitch needs at least one of: org, lane, tier, person."}

            def _work():
                M = mapping_sheet.MAPPING
                rows = M.researchers()
                applied = []
                if person:
                    rows = M.find_person(person)
                    applied.append(f"person={person!r}")
                if org:
                    org_rows = M.for_org(org)
                    rows = [r for r in rows if r in org_rows] if person else org_rows
                    applied.append(f"org={org!r}")
                if lane:
                    lane_rows = M.by_lane(lane)
                    rows = [r for r in rows if r in lane_rows]
                    applied.append(f"lane={lane!r}")
                if tier:
                    tier_rows = M.by_tier(tier)
                    rows = [r for r in rows if r in tier_rows]
                    applied.append(f"tier={tier!r}")
                return rows, applied

            try:
                rows, applied = await asyncio.to_thread(_work)
            except gtm_sheet.SheetAccessError as e:
                return _unavailable(e)

            out = {
                "available": True,
                "source": "the researcher/buyer mapping sheet (read-only)",
                "filters_applied": applied,
                "match_count": len(rows),
                "returned": min(len(rows), limit),
                "truncated": len(rows) > limit,
                "researchers": _enriched(rows, limit),
                "rules": _rules_block(),
                "staleness": _staleness_note(),
            }

            # An org with a row but no PEOPLE is a different answer from an org
            # the sheet has never heard of, and both are different from an org
            # that is deliberately excluded. Never collapse the three.
            if org and not rows:
                flags = mapping_sheet.MAPPING.org_flags(org)
                org_row = mapping_sheet.MAPPING.org_row(org)
                if flags:
                    out["note"] = (
                        f"The mapping sheet has NO researcher for {org} and flags the org "
                        f"itself: " + "; ".join(f"{f['label']} — {f['why']}" for f in flags)
                        + ". They are not a pitch target. Say that and why."
                    )
                elif org_row:
                    out["note"] = (
                        f"{org} IS listed in the sheet's org coverage but has no mapped "
                        f"researchers. The account note says: "
                        f"{org_row.get('notes') or '(nothing recorded)'}. Report that — it "
                        f"is not the same as 'we have no one there'."
                    )
                else:
                    out["note"] = (
                        f"{org!r} does not appear in the mapping sheet at all — neither as "
                        f"a researcher's company nor in the org coverage list. Say exactly "
                        f"that. Do NOT suggest anyone from another org as a substitute, and "
                        f"do not name a researcher from general knowledge."
                    )
            elif not rows:
                out["note"] = (
                    "Nothing in the mapping sheet matches those filters. Report the "
                    "filters you used and say nothing matched; do not widen silently."
                )
            else:
                # Every row already carries its own do_not_pitch, but a caller
                # skimming a list can miss a per-row field. When EVERY match is at
                # an excluded org there is no valid recommendation to make here at
                # all, and that belongs at the top of the result, not inside row 1.
                returned = out["researchers"]
                blocked = [r for r in returned if r.get("do_not_pitch")]
                if blocked and len(blocked) == len(returned):
                    out["note"] = (
                        "EVERY match is at an org the sheet excludes from pitching "
                        f"({', '.join(sorted({r['company'] for r in blocked}))}). There is "
                        "no valid recommendation to give here: say what the sheet says and "
                        "why they're excluded, and do not offer them as a target anyway."
                    )
                departed = [r for r in returned if r.get("departed")]
                if departed:
                    out["departed_in_results"] = [
                        {"researcher": r["researcher"], "company": r["company"],
                         "departure": r["departure"]}
                        for r in departed
                    ]
            return out

        async def _mapping_rules(inp: dict):
            try:
                legend = await asyncio.to_thread(mapping_sheet.MAPPING.legend)
                departures = await asyncio.to_thread(mapping_sheet.MAPPING.departures)
            except gtm_sheet.SheetAccessError as e:
                return _unavailable(e)
            described = legend.describe()
            described["departures"] = {
                "known": mapping_sheet.MAPPING.departures_known(),
                "count": len(departures.people),
                "people": departures.all(),
                "note": departures.note,
                "rule": (
                    "Anyone on this list has LEFT the org the mapping sheet lists them "
                    "under. Never recommend them for outreach there; say where they went."
                ),
            }
            described["today"] = dl.iso(dl.today_ist())
            described["staleness_rule_now"] = (
                f"Rows are stale once the evidence behind them is older than "
                f"{legend.stale_after_days()} days, measured against today. Every stale "
                f"row carries an explicit re-verify-role caveat."
            )
            described["read_only"] = (
                "This sheet is READ-ONLY. I never write to it, and I cannot update a "
                "row for anyone — say so if asked."
            )
            return {"available": True, "legend": described}

        async def _mapping_coverage(inp: dict):
            org = str(inp.get("org") or "").strip()
            only_gaps = bool(inp.get("only_gaps"))
            try:
                M = mapping_sheet.MAPPING
                if org:
                    row = await asyncio.to_thread(M.org_row, org)
                    if row is None:
                        return {
                            "available": True, "org": org, "found": False,
                            "note": (
                                f"{org!r} is not in the mapping sheet's org coverage list. "
                                f"Say that plainly — it means the mapping exercise never "
                                f"covered them, not that they are a bad account."
                            ),
                        }
                    people = await asyncio.to_thread(M.for_org, org)
                    return {
                        "available": True,
                        "org": row.get("company", org),
                        "found": True,
                        "industry": row.get("industry", ""),
                        "researchers_mapped": row.get("researchers_mapped", ""),
                        "tier_counts": {"T1": row.get("t1", ""), "T2": row.get("t2", ""),
                                        "T3": row.get("t3", "")},
                        "account_notes": row.get("notes", ""),
                        "org_flags": await asyncio.to_thread(M.org_flags, org),
                        "researchers": _enriched(people, 10),
                        "rules": _rules_block(),
                        "staleness": _staleness_note(),
                    }
                gaps = await asyncio.to_thread(M.orgs_without_researchers)
                all_orgs = await asyncio.to_thread(M.org_rows)
            except gtm_sheet.SheetAccessError as e:
                return _unavailable(e)

            out = {
                "available": True,
                "orgs_in_coverage_list": len(all_orgs),
                "orgs_without_researchers_count": len(gaps),
                "orgs_without_researchers": gaps[:40],
                "truncated": len(gaps) > 40,
                "note": (
                    "'No mapped researchers' means the mapping exercise found nobody "
                    "worth naming there — the account note usually says why (too big, "
                    "wrong modality, watch-only). QUOTE the account note; it is the "
                    "reason, and 'no researchers' without it reads as an oversight when "
                    "it was usually a decision."
                ),
            }
            if not only_gaps:
                out["all_orgs"] = [
                    {"company": r.get("company", ""), "industry": r.get("industry", ""),
                     "researchers_mapped": r.get("researchers_mapped", ""),
                     "account_notes": r.get("notes", "")}
                    for r in all_orgs[:60]
                ]
                out["all_orgs_truncated"] = len(all_orgs) > 60
            return out

        async def _mapping_edges(inp: dict):
            query_text = str(inp.get("query") or "").strip()
            try:
                edges = await asyncio.to_thread(mapping_sheet.MAPPING.edges, query_text)
            except gtm_sheet.SheetAccessError as e:
                return _unavailable(e)
            return {
                "available": True,
                "query": query_text,
                "count": len(edges),
                "edges": edges[:20],
                "truncated": len(edges) > 20,
                "note": (
                    "These are WARM-INTRO PATHS — shared labs, shared investors, alumni "
                    "networks — not pitch targets. The DEPARTURES record is deliberately "
                    "NOT in this list; ask for the mapping rules if you need it. Anyone "
                    "you name from here still has to be checked against the mapping rows "
                    "before you suggest pitching them."
                ),
            }

        async def _cross_check(inp: dict):
            """THE CROSS-SOURCE ANSWER: the tracker says who we're talking to, the
            mapping says who we SHOULD be talking to. Both, side by side, with
            every rule applied to the mapping half."""
            company = str(inp.get("company") or "").strip()
            if not company:
                return {"error": "cross_check_outreach needs a 'company'."}

            out: dict = {"available": True, "company": company}

            # The tracker half. A failure here must not take the mapping half
            # down with it — half an answer, clearly labelled, beats none.
            try:
                tab = await asyncio.to_thread(gtm_sheet.SHEETS.tab, gtm_sheet.TRACKER)
                matches = tab.find_company(company) if tab else []
                out["tracker"] = {
                    "available": True,
                    "tab": tab.title if tab else "",
                    "match_count": len(matches),
                    "rows": [
                        {
                            "company": r.get("company", ""),
                            "poc": r.get("poc", ""),
                            "poc_designation": r.get("poc_designation", ""),
                            "response": r.get("response", ""),
                            "last_followed_up": r.get("last_followed_up", ""),
                            "next_steps": r.get("next_steps", ""),
                            "sheet_row": r.get("_row"),
                        }
                        for r in matches[:3]
                    ],
                    "staleness": gtm_sheet.SHEETS.staleness_note(tab) if tab else "",
                }
                if not matches:
                    out["tracker"]["note"] = (
                        f"No outreach tracker row for {company!r} — we have no tracked "
                        f"conversation there. Say so; the mapping half below is then a "
                        f"cold-open suggestion, not a next step on a live thread."
                    )
            except gtm_sheet.SheetAccessError as e:
                out["tracker"] = {"available": False, "error": str(e), "fix": e.remedy}

            # The mapping half.
            try:
                M = mapping_sheet.MAPPING
                people = await asyncio.to_thread(M.for_org, company)
                org_row = await asyncio.to_thread(M.org_row, company)
                flags = await asyncio.to_thread(M.org_flags, company)
                out["mapping"] = {
                    "available": True,
                    "match_count": len(people),
                    "researchers": _enriched(people, 8),
                    "org_flags": flags,
                    "account_notes": (org_row or {}).get("notes", ""),
                    "rules": _rules_block(),
                    "staleness": _staleness_note(),
                }
                if flags:
                    out["mapping"]["do_not_pitch"] = (
                        f"{company} is flagged as: "
                        + "; ".join(f"{f['label']} ({f['why']})" for f in flags)
                        + ". Whatever the tracker says, this org is not a pitch target — "
                          "flag that to the team rather than suggesting a researcher."
                    )
                elif not people:
                    out["mapping"]["note"] = (
                        f"The mapping sheet has no researcher for {company}. Say that "
                        f"rather than naming someone from anywhere else."
                    )
            except gtm_sheet.SheetAccessError as e:
                out["mapping"] = {"available": False, "error": str(e), "fix": e.remedy}

            # The PoC we're already talking to, checked against DEPARTURES. This
            # is the highest-value line this tool produces: the tracker has no
            # idea the person it names has changed jobs.
            try:
                departures = await asyncio.to_thread(mapping_sheet.MAPPING.departures)
                poc_checks = []
                for row in (out.get("tracker", {}).get("rows") or []):
                    poc = (row.get("poc") or "").strip()
                    if not poc:
                        continue
                    rec = departures.lookup(poc)
                    poc_checks.append({
                        "poc": poc,
                        "on_departures_list": bool(rec),
                        "departure": rec or {},
                        "warning": (
                            f"The tracker's PoC {poc} is on the mapping sheet's DEPARTURES "
                            f"list ({rec.get('move') or 'no move recorded'}). They have "
                            f"left — flag this to the team; the tracker row is out of date."
                        ) if rec else "",
                    })
                out["poc_departure_check"] = {
                    "ran": mapping_sheet.MAPPING.departures_known(),
                    "checks": poc_checks,
                    "note": (
                        "" if mapping_sheet.MAPPING.departures_known() else
                        "The DEPARTURES list could not be read, so this check did NOT run. "
                        "Say so — do not imply the PoC is still in post."
                    ),
                }
            except gtm_sheet.SheetAccessError:
                out["poc_departure_check"] = {
                    "ran": False,
                    "checks": [],
                    "note": (
                        "The DEPARTURES list could not be read, so this check did NOT run. "
                        "Say so — do not imply the PoC is still in post."
                    ),
                }

            out["how_to_answer"] = (
                "Answer in one shape: we're talking to <PoC> at <org> (tracker, <date>); "
                "the mapping suggests <researcher> (<Tier>, <Confidence>, lane <x>) — hook: "
                "<why_them>. Carry every caveat the mapping half attached: staleness, "
                "watch-outs, departures, org flags. Cite BOTH sheets by name."
            )
            return out

        return [
            {
                "schema": {
                    "name": "who_to_pitch",
                    "description": (
                        "THE RESEARCHER/BUYER MAPPING — who to pitch at an org, in an ICP "
                        "lane, or at a tier. This is the ONLY place that mapping exists: "
                        "use it for 'who do we pitch at <org>', 'who do we pitch at <org> "
                        "for <lane>', 'give me T1 evals champions', 'pitch hook for "
                        "<person>', 'who covers red-teaming'. Filters combine (org + lane "
                        "+ tier). 'lane' takes a letter (a/b/c/d) OR a phrase ('evals', "
                        "'red-teaming', 'post-training', 'agent trajectories'). "
                        "EVERY returned row already has the sheet's rules applied to it: "
                        "'tier' and 'confidence' (INDEPENDENT — quote BOTH, never merge "
                        "them), 'staleness.caveat' (include it verbatim when non-empty — "
                        "it is the sheet's own re-verify rule computed against today), "
                        "'do_not_recommend' (that person has LEFT — never recommend them, "
                        "say where they went), 'do_not_pitch' (the org is a competitor, a "
                        "channel partner, or fails the budget gate — not a target), and "
                        "'watch_outs' (state it alongside the hook, never after it). "
                        "Quote 'why_them' as written; it is the agreed hook. If a filter "
                        "matches nothing, read the 'note' — 'no researcher there', 'not in "
                        "the sheet at all' and 'org is excluded' are three different "
                        "answers and must not be collapsed. NEVER name a researcher this "
                        "tool didn't return. The bot is read-only on this sheet."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "org": {"type": "string", "description": "Company / lab name; partial is fine."},
                            "lane": {
                                "type": "string",
                                "description": (
                                    "ICP lane: a letter (a/b/c/d) or a phrase such as "
                                    "'evals', 'post-training', 'red-teaming', 'agent "
                                    "trajectories'."
                                ),
                            },
                            "tier": {"type": "string", "description": "T1, T2 or T3."},
                            "person": {"type": "string", "description": "A researcher's name."},
                            "limit": {"type": "integer", "description": "Max rows (default 10, cap 25)."},
                        },
                        "required": [],
                    },
                },
                "handler": _who_to_pitch,
            },
            {
                "schema": {
                    "name": "mapping_rules",
                    "description": (
                        "The mapping sheet's LEGEND and the rules it imposes: the four ICP "
                        "lanes and what each means, the T1/T2/T3 definitions, why "
                        "Confidence and Tier are independent, when the sheet was built and "
                        "how stale that makes it TODAY, the DEPARTURES do-not-pitch list, "
                        "and the flagged non-buyers and budget-gate failures. Call this "
                        "when someone asks what a lane or a tier MEANS, what the tiers "
                        "are, whether a person has left, why an org is excluded, or how "
                        "current the mapping is. Also worth calling before a broad "
                        "recommendation so you quote the definitions as the sheet writes "
                        "them rather than approximating them."
                    ),
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                },
                "handler": _mapping_rules,
            },
            {
                "schema": {
                    "name": "mapping_coverage",
                    "description": (
                        "The mapping sheet's ORG COVERAGE list: every org it looked at, how "
                        "many researchers it mapped there, the per-tier counts and the "
                        "account note. THIS is how you answer 'which orgs have no mapped "
                        "researchers' — set only_gaps true. Pass 'org' for one account's "
                        "coverage plus its people. ALWAYS quote the account note when "
                        "reporting a gap: a zero is nearly always a decision (too big, "
                        "wrong data modality, watch-only, competitor) rather than an "
                        "oversight, and reporting the zero without the reason misrepresents "
                        "the sheet."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "org": {"type": "string", "description": "One org's coverage. Omit for the whole list."},
                            "only_gaps": {
                                "type": "boolean",
                                "description": "Return only orgs with no mapped researchers.",
                            },
                        },
                        "required": [],
                    },
                },
                "handler": _mapping_coverage,
            },
            {
                "schema": {
                    "name": "mapping_edges",
                    "description": (
                        "The Edge Map: warm-intro paths between orgs and people — shared "
                        "labs, shared investors, alumni networks, co-author pairs. Use for "
                        "'how do we get to <org>', 'do we have a way in', 'who connects X "
                        "and Y'. Pass 'query' to filter by an org, a person or a lab. These "
                        "are ROUTES, not targets: anyone you name from here still has to be "
                        "checked with who_to_pitch before you suggest pitching them. The "
                        "DEPARTURES record is not returned here — it is in mapping_rules."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Org, person or lab to filter by. Omit for all."},
                        },
                        "required": [],
                    },
                },
                "handler": _mapping_edges,
            },
            {
                "schema": {
                    "name": "cross_check_outreach",
                    "description": (
                        "CROSS-SOURCE: joins the outreach tracker (who we are ACTUALLY "
                        "talking to at an org) with the researcher mapping (who we SHOULD "
                        "be talking to). Use for 'we're talking to <PoC> at <org> — who "
                        "else should we be hitting', 'is <org> being worked at the right "
                        "level', 'are we pitching the right person at <org>'. Returns the "
                        "tracker row (PoC, response, last touch, next steps), the mapped "
                        "researchers with every mapping rule applied, the org's exclusion "
                        "flags, and — the part nothing else does — a check of the tracker's "
                        "own PoC against the DEPARTURES list, which is how you catch that "
                        "the person we've been chasing has changed jobs. Cite BOTH sheets "
                        "by name. If 'poc_departure_check.ran' is false, the check did not "
                        "run and you must say so rather than implying the PoC is current."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string", "description": "The org, as the tracker names it."},
                        },
                        "required": ["company"],
                    },
                },
                "handler": _cross_check,
            },
        ]

    # -- deadline authority -------------------------------------------------

    async def _set_deadline_for(
        self,
        *,
        company: str,
        kind: str,
        channel,
        unprompted: bool,
        anchor=None,
        row: Optional[dict] = None,
    ) -> dict:
        """Set a deadline, mirror it to the sheet, announce it, audit it.

        THE ONE IMMEDIATE ANNOUNCEMENT THIS BOT STILL MAKES, and the documented
        exception to "everything proactive goes in the daily digest". It is not
        an interruption: someone just asked when the follow-up for X is, and the
        announcement is the answer, carrying the "shout to change" invitation
        that makes the date consent-based rather than imposed. Holding it for
        the next morning's digest would answer a question a day late.

        `unprompted=True` is now unreachable from any scheduled path — the
        sweep used to set a date on its own for a dead-deal row and announce it,
        and that was exactly the kind of scattered proactive message the digest
        replaced. The dead-deal rows are named in the digest's HYGIENE section
        instead, and a person (or the bot, when asked) sets the date. The
        parameter and its wording are kept for that ask-time case.

        The order matters. SQLite is written FIRST and is authoritative: the
        deadline exists even if the sheet write or the announcement fails. The
        sheet write is a mirror and is allowed to fail. The announcement is what
        makes it consent-based rather than imposed, so it is never skipped when a
        date was actually set.

        Returns a dict the tool layer can hand straight back to the model.
        """
        today = dl.today_ist()

        # The strategy doc's cadence outranks the defaults — when we can read it.
        strategy_text = None
        try:
            if sources.STRATEGY_DOC.connected:
                strategy_text = sources.STRATEGY_DOC.text()
        except Exception:
            log.debug("[deadline] strategy doc unreadable; using defaults", exc_info=True)

        # Anchor the rule to the row's own dates where we have them: a follow-up
        # is due N days after the LAST TOUCH, not N days after someone asked.
        sheet_row_no = None
        if row is None:
            try:
                tab = gtm_sheet.SHEETS.tab(gtm_sheet.TRACKER)
                matches = tab.find_company(company) if tab else []
                row = matches[0] if matches else None
            except gtm_sheet.SheetAccessError:
                row = None
        if row is not None:
            sheet_row_no = row.get("_row")
            company = (row.get("company") or company).strip() or company
            if anchor is None:
                if kind == dl.KIND_MEETING_PREP:
                    anchor = dl.parse_date(row.get("meeting_date"))
                else:
                    anchor, _col = tracker.last_touch(row)

        due, rule, _n = dl.compute_due(kind, anchor=anchor, strategy_text=strategy_text)

        # THE CONFLICT RULE: a human date already in the sheet wins outright.
        # Adopt it into SQLite and stop — no new date, no write, no announcement.
        if row is not None:
            existing_cell = (row.get("bot_deadline") or "").strip()
            if existing_cell and not dl.is_bot_written(existing_cell):
                human_date = dl.parse_date(existing_cell)
                if human_date:
                    self.db.upsert_deadline(
                        company=company, kind=kind, due_date=dl.iso(human_date),
                        rule="entered by a person in the sheet", source="human",
                        sheet_row=sheet_row_no, sheet_target="original",
                        channel_id=getattr(channel, "id", None),
                    )
                    log.info(
                        "[deadline] %s already has a human date (%s); adopting it, not overwriting",
                        company, dl.iso(human_date),
                    )
                    state.audit(
                        "deadline_adopted_human",
                        reason="a person had already entered a date in the sheet; theirs wins",
                        company=company, kind=kind, due_date=dl.iso(human_date),
                    )
                    return {
                        "action": "kept_human", "company": company, "kind": kind,
                        "due_date": dl.iso(human_date),
                        "note": "A person had already set this date in the sheet. Theirs wins; I adopted it.",
                    }

        result = self.db.upsert_deadline(
            company=company, kind=kind, due_date=dl.iso(due), rule=rule, source="bot",
            sheet_row=sheet_row_no, sheet_target=config.SHEET_WRITE_TARGET,
            channel_id=getattr(channel, "id", None),
        )
        action = result["action"]
        record = result["deadline"]

        if action in ("kept_human", "unchanged"):
            # Nothing changed, so nothing is announced. Announcing an unchanged
            # deadline is exactly the noise that gets a bot muted.
            return {
                "action": action, "company": company, "kind": kind,
                "due_date": record["due_date"], "rule": record["rule"],
                "source": record["source"],
                "note": (
                    "This deadline already existed; I didn't change it or announce anything."
                    if action == "unchanged"
                    else "A person's own date already exists and wins."
                ),
            }

        # Mirror to the sheet. Best-effort by construction.
        sheet_result = {"ok": False, "cell": "", "error": "no tracker row to write to"}
        if sheet_row_no and config.SHEET_WRITE_TARGET != "off":
            sheet_result = await asyncio.to_thread(
                gtm_sheet.SHEETS.write_deadline_cell,
                row=int(sheet_row_no),
                value=dl.sheet_cell_value(due, rule=rule),
                # The row number came from the ORIGINAL; the write goes to
                # SHEET_WRITE_TARGET. Pass the company so the write is refused
                # rather than misplaced if the two sheets have drifted.
                expect_company=company,
            )
            if sheet_result["ok"]:
                self.db.mark_deadline(
                    record["id"], sheet_cell=sheet_result["cell"],
                    sheet_target=sheet_result["target"],
                )
            else:
                log.warning(
                    "[deadline] sheet mirror failed for %s: %s", company, sheet_result["error"]
                )
            state.audit(
                "sheet_write",
                reason=f"mirroring the {kind} deadline for {company}",
                company=company, cell=sheet_result.get("cell"),
                target=sheet_result.get("target"), ok=sheet_result["ok"],
                error=sheet_result.get("error") or None,
                value=dl.sheet_cell_value(due, rule=rule),
            )

        # Announce. This is the consent mechanism, so it happens whenever a date
        # was actually set — even if the sheet write failed.
        label = dl.KINDS.get(kind, {}).get("label", "the next step")
        text = dl.announcement(
            thing=f"the {label}", company=company, due=due, rule=rule,
            mentions=dl.notify_mentions(), unprompted=unprompted,
        )
        if not sheet_result["ok"] and config.SHEET_WRITE_TARGET != "off":
            text += f" (I couldn't write it to the sheet: {sheet_result['error']}; I'm holding it here.)"

        sent = await guardrails.send(
            channel, text,
            reason=f"setting a {kind} deadline for {company} ({'unprompted' if unprompted else 'asked'})",
            kind="deadline",
            extra={"company": company, "due_date": dl.iso(due), "deadline_kind": kind},
        )
        if sent is not None:
            self.db.mark_deadline(record["id"], announced=True)

        state.audit(
            "deadline_set",
            reason=rule + (" (set unprompted for a row with no next date)" if unprompted else ""),
            company=company, kind=kind, due_date=dl.iso(due),
            sheet_cell=sheet_result.get("cell") or None,
            sheet_ok=sheet_result["ok"], announced=sent is not None,
        )
        log.info(
            "[deadline] SET %s %s = %s (%s) sheet=%s announced=%s",
            company, kind, dl.iso(due), rule,
            sheet_result.get("cell") or "no", sent is not None,
        )
        return {
            "action": action, "company": company, "kind": kind,
            "due_date": dl.iso(due), "due_date_human": dl.format_date(due),
            "rule": rule, "source": "bot",
            "announced": sent is not None,
            "sheet_write": sheet_result,
            "note": "I've already announced this in the channel — don't repeat the announcement.",
        }

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
    # There is no per-promise cooldown helper any more. It paced individual
    # nudges, and there are none: a chase is a line in the daily digest, so the
    # digest's own once-a-day guarantee is the pacing, and it is stricter than
    # COS_NUDGE_WINDOW_HOURS ever was.

    def _max_attempts(self) -> int:
        """How many times one promise may be chased before it stops being the
        owner's problem and moves to the digest's ESCALATIONS section. An
        "attempt" is an appearance in the digest's OVERDUE section."""
        return max(1, config.COS_NUDGE_MAX_ATTEMPTS)

    async def _notes_sync_loop(self) -> None:
        """Pull the Drive meeting notes on a timer. Separate from the chase
        sweeper on purpose: an rclone that hangs for its whole timeout must not
        delay a chase, and a failing sweep must not stop the notes going stale.

        Never dies. `notes.sync_now` swallows its own failures, so a tick that
        raises here is something unexpected — log it and take the next one."""
        interval = max(1, config.NOTES_SYNC_MINUTES) * 60
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(notes.sync_now, reason="scheduled")
            except Exception:
                log.exception("[notes] scheduled sync raised; continuing")

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
        """One tick.

        The ONLY thing a tick can put into Discord is the daily digest, and only
        once a day. Every other proactive path — the deadline reminder, the
        deadline chase, the promise nudge, the give-up flag, the row-hygiene
        flag, the weekly funnel post — was removed, not disabled behind a knob,
        so there is nothing here that could start speaking again by accident.

        Guarded so a failing digest never stops the state summary being written.
        """
        try:
            await self._maybe_post_daily_digest()
        except Exception:
            log.exception("[digest] tick raised; continuing")
        self._maybe_write_daily_summary()

    # ======================================================================
    # THE ONE DAILY DIGEST
    # ======================================================================
    # Everything below is the entire proactive voice of this bot. What used to
    # be six kinds of message scattered through the day is one message, once a
    # day, in grouped sections, hot first.
    #
    # IF YOU ARE ADDING SOMETHING THE BOT SHOULD TELL THE TEAM: add a section
    # here. Do not add a `guardrails.send`. The value of this design is entirely
    # in the fact that there is exactly one of them.
    #
    # The exceptions, both of which are answers rather than interruptions:
    #   - a REPLY to a question someone asked (`_reply`);
    #   - the ask-time deadline announcement in `_set_deadline_for`, which is
    #     the answer to "when's the follow-up for X?" and carries the "shout to
    #     change" consent mechanism.

    def _deadline_owner_mention(self, item: dict) -> str:
        """How to address whoever owns a deadline. Falls back to the notify list
        rather than pinging nobody in particular."""
        if item.get("owner_id"):
            return guardrails.mention_for(item["owner_id"], item.get("owner_name", ""))
        return ""

    @staticmethod
    def _owner_key(mention: str, name: str) -> str:
        """The grouping key for the OVERDUE section. Prefers the mention token
        (an id, which can't be changed by its owner); falls back to the name so
        two overdue items owned by the same un-pingable person still share one
        line instead of producing two."""
        return (mention or "").strip() or (name or "").strip().lower()

    # -- posting -----------------------------------------------------------

    async def _maybe_post_daily_digest(self) -> None:
        """Post the digest if it is time and it hasn't gone out today.

        THE ONCE-A-DAY GUARANTEE is the `sales_digest_date` marker in SQLite,
        written after the message posts. It is a DATE, not a timer, so a
        redeploy at 10:05 reads back "already sent today" and stays quiet —
        which is the whole point of persisting it. If the marker can't be read
        we do NOT post: a duplicate digest is worse than a missed one, and the
        failure is loud in the log either way.

        "At or after the digest time" rather than "in that minute", because the
        sweeper ticks every COS_FOLLOWUP_CHECK_INTERVAL_MINUTES and an
        exact-minute test would simply never fire. A consequence worth knowing:
        on a day where the 10:00 digest would have been empty and something
        turns up at 14:00, the digest goes out at 14:00. That is still one
        digest, and holding a hot lead for twenty hours to protect a schedule
        would be the wrong trade.
        """
        if not config.SALES_DIGEST_ENABLED:
            return

        now = dl.now_ist()
        hour, minute = config.digest_time_ist()
        if not digest.is_due(now, hour=hour, minute=minute):
            return

        today = now.date()
        marker = dl.iso(today)
        try:
            if self.db.get_meta("sales_digest_date") == marker:
                return
        except Exception:
            log.exception(
                "[digest] could not read the once-a-day marker; staying quiet rather than "
                "risking a second digest"
            )
            return

        channel_id = config.digest_channel_id()
        channel = self.get_channel(channel_id) if channel_id else None
        if channel is None or not guardrails.may_read(channel_id):
            log.warning(
                "[digest] no reachable sales channel (%s) to post the daily digest in",
                channel_id,
            )
            return

        collected = await self._collect_digest(today=today)
        sections = collected["sections"]

        if not digest.total_items(sections):
            # EMPTY DAY = NO DIGEST. The marker is deliberately NOT set, so if
            # something turns up this afternoon it still gets said today.
            log.info("[digest] nothing outstanding for %s — no digest today", marker)
            return

        self._age_digest_items(sections, on_date=marker)

        body = digest.render(
            day=today,
            sections=sections,
            unowned_mention=dl.notify_mentions(),
            escalate_mention=self._escalate_mention(),
            staleness=collected.get("staleness", ""),
        )

        sent_first = await self._post_digest(channel, body, marker=marker)
        if sent_first is None:
            log.warning("[digest] the digest for %s was refused or failed to post", marker)
            return

        self._apply_digest_effects(
            collected["effects"], channel_id=channel_id, message_id=sent_first.id
        )

        try:
            self.db.set_meta("sales_digest_date", marker)
        except Exception:
            log.exception(
                "[digest] POSTED but could not record the once-a-day marker for %s — a "
                "restart today may post a second digest",
                marker,
            )

        counts = digest.counts(sections)
        state.audit(
            "daily_digest",
            reason="the one scheduled proactive message of the day",
            date=marker,
            channel_id=channel_id,
            message_id=sent_first.id,
            items=digest.total_items(sections),
            **{f"n_{k}": v for k, v in counts.items()},
        )
        log.info(
            "[digest] POSTED %s — hot=%d deadlines=%d overdue=%d escalations=%d hygiene=%d",
            marker,
            counts[digest.SECTION_HOT], counts[digest.SECTION_DEADLINES],
            counts[digest.SECTION_OVERDUE], counts[digest.SECTION_ESCALATIONS],
            counts[digest.SECTION_HYGIENE],
        )

        try:
            cutoff = dl.iso(today - timedelta(days=30))
            dropped = self.db.prune_digest_items(before_date=cutoff)
            if dropped:
                log.info("[digest] pruned %d carry-forward row(s) last seen before %s",
                         dropped, cutoff)
        except Exception:
            log.exception("[digest] could not prune the carry-forward table")

    def _escalate_mention(self) -> str:
        """Who the ESCALATIONS section is addressed to. Empty when ESCALATE_TO_ID
        is unset — the section still posts, it just isn't addressed to anyone,
        which is honest rather than silently dropping the escalations."""
        if not config.ESCALATE_TO_ID:
            return ""
        return guardrails.mention_for(
            config.ESCALATE_TO_ID,
            str(config.ROSTER_DISPLAY_NAMES.get(str(config.ESCALATE_TO_ID)) or ""),
        )

    async def _post_digest(self, channel, body: str, *, marker: str):
        """Send the digest, splitting on line boundaries if it exceeds Discord's
        limit.

        The parts are CONSECUTIVE PARTS OF ONE DIGEST, not separate messages:
        they go out back to back, in order, and each is audited with its part
        number. Nothing is clipped — a digest that dropped its HYGIENE section
        to fit would be lying about what it found, and the per-section caps have
        already bounded the length honestly.

        Returns the FIRST message (the one the audit record and the chase
        bookkeeping point at), or None when nothing went out at all.
        """
        chunks = _split_for_discord(body)
        first = None
        for i, chunk in enumerate(chunks):
            sent = await guardrails.send(
                channel,
                chunk,
                reason=f"the daily sales digest for {marker}"
                       + (f" (part {i + 1} of {len(chunks)})" if len(chunks) > 1 else ""),
                kind="daily_digest",
                extra={"date": marker, "part": i + 1, "parts": len(chunks)},
            )
            if sent is None:
                # A refused or failed part. Stop rather than posting the rest
                # out of order; the first part (if it went) still stands.
                log.warning(
                    "[digest] part %d of %d failed; stopping there", i + 1, len(chunks)
                )
                break
            if first is None:
                first = sent
        return first

    # -- building ----------------------------------------------------------

    async def _collect_digest(self, *, today) -> dict:
        """Gather every outstanding item into sections.

        Each source is independently guarded: an unreadable sheet must not cost
        the team its deadline reminders, and a database hiccup must not hide the
        hot rows. A section that couldn't be built is simply absent — the digest
        never asserts "nothing stalled" when it means "I couldn't look".

        Returns {"sections": {...}, "effects": [...], "staleness": str}. The
        EFFECTS are applied only after the message actually posts, so a refused
        send never burns a chase attempt.
        """
        sections: dict[str, list[dict]] = {key: [] for key in digest.SECTION_ORDER}
        effects: list[dict] = []
        staleness = ""

        if config.COS_FOLLOWUP_ENABLED:
            for label, build in (
                ("deadlines", lambda: self._collect_deadlines(today=today)),
                ("chases", lambda: self._collect_chases(today=today)),
            ):
                try:
                    build_sections, build_effects = build()
                except Exception:
                    log.exception("[digest] could not collect %s; that section is omitted", label)
                    continue
                for key, items in build_sections.items():
                    sections[key].extend(items)
                effects.extend(build_effects)
        else:
            log.info("[digest] COS_FOLLOWUP_ENABLED=false — no deadlines or chases in the digest")

        try:
            sheet_sections, staleness = await self._collect_sheet_sections(today=today)
            for key, items in sheet_sections.items():
                sections[key].extend(items)
        except Exception:
            log.exception("[digest] could not collect the sheet sections; they are omitted")

        # Cap each section, saying how many were left out. Escalations are NOT
        # capped: there are never many, and an escalation that scrolled off is
        # the one thing in here nobody would notice was missing.
        limit = max(1, config.SALES_DIGEST_MAX_PER_SECTION)
        for key, what in (
            (digest.SECTION_HOT, "hot"),
            (digest.SECTION_DEADLINES, "deadline"),
            (digest.SECTION_OVERDUE, "overdue"),
            (digest.SECTION_HYGIENE, "hygiene"),
        ):
            sections[key] = digest.clip(sections[key], limit, what=what)

        return {"sections": sections, "effects": effects, "staleness": staleness}

    def _collect_deadlines(self, *, today) -> tuple[dict, list[dict]]:
        """Deadlines, split across DEADLINES / OVERDUE / ESCALATIONS.

        Due today or on the next working day is a reminder to the owner. Past
        due is a chase, with its age in working days. Past
        COS_NUDGE_MAX_ATTEMPTS chases it stops being the owner's problem and
        becomes a decision for whoever ESCALATE_TO_ID names.
        """
        out = {k: [] for k in digest.SECTION_ORDER}
        effects: list[dict] = []
        tomorrow = dl.add_working_days(today, 1)

        for item in self.db.list_open_deadlines(limit=500):
            due = dl.parse_date(item["due_date"])
            if due is None:
                log.warning(
                    "[digest] deadline %s for %s has an unreadable date %r; skipping",
                    item["id"], item["company"], item["due_date"],
                )
                continue

            label = dl.KINDS.get(item["kind"], {}).get("label", "next step")
            mention = self._deadline_owner_mention(item)
            owner_key = self._owner_key(mention, item.get("owner_name", ""))

            if due > tomorrow:
                continue

            if due >= today:
                when = "today" if due == today else "tomorrow"
                out[digest.SECTION_DEADLINES].append({
                    "key": f"deadline:{item['id']}",
                    "text": (
                        f"{item['company']} — the {label} is due {when} "
                        f"({dl.format_date(due)})."
                    ),
                    "owner_mention": mention,
                    "owner_key": owner_key,
                })
                if not item["reminded"]:
                    effects.append({"type": "deadline_reminded", "id": item["id"]})
                continue

            overdue = digest.working_days_overdue(due, today, dl=dl)
            if item["chases_sent"] >= self._max_attempts():
                out[digest.SECTION_ESCALATIONS].append({
                    "key": f"deadline:{item['id']}",
                    "text": (
                        f"{item['company']} — the {label} was due "
                        f"{dl.format_date(due)} ({digest.overdue_phrase(overdue)}) and "
                        f"I've asked {item['chases_sent']} time(s) with nothing back."
                    ),
                    "owner_mention": "",
                    "owner_key": owner_key,
                })
                if not item["escalated"]:
                    effects.append({
                        "type": "deadline_escalated",
                        "id": item["id"],
                        "company": item["company"],
                        "kind": item["kind"],
                        "due_date": item["due_date"],
                        "chases_sent": item["chases_sent"],
                    })
                continue

            out[digest.SECTION_OVERDUE].append({
                "key": f"deadline:{item['id']}",
                "text": f"{item['company']} {label} {digest.overdue_phrase(overdue)}",
                "owner_mention": mention,
                "owner_key": owner_key,
            })
            effects.append({
                "type": "deadline_attempt",
                "id": item["id"],
                "company_key": item["company_key"],
                "company": item["company"],
                "kind": item["kind"],
                "due_date": item["due_date"],
            })

        return out, effects

    def _collect_chases(self, *, today) -> tuple[dict, list[dict]]:
        """Overdue promises ("I'll send Acme the deck tomorrow"), split between
        OVERDUE and ESCALATIONS.

        `reminder_cutoff` is passed as NOW, which switches off the per-promise
        cooldown that used to pace individual nudges. It has nothing left to
        pace: the digest itself is once a day, and that is a stricter limit than
        COS_NUDGE_WINDOW_HOURS ever was. The ATTEMPT CAP still applies — an
        appearance in OVERDUE is one attempt.
        """
        out = {k: [] for k in digest.SECTION_ORDER}
        effects: list[dict] = []
        now = followups.now_utc()
        now_ts = followups.to_ts(now)

        items = self.db.list_due_chases(now=now_ts, reminder_cutoff=now_ts)
        for item in items:
            mention = guardrails.mention_for(item["person_id"], item["person_name"])
            owner_key = self._owner_key(mention, item["person_name"])
            try:
                due_date = followups.from_ts(item["due_at"]).astimezone(dl.IST).date()
            except Exception:
                due_date = today
            overdue = digest.working_days_overdue(due_date, today, dl=dl)
            what = (item["what"] or "what they promised").strip()

            if item["reminders_sent"] >= self._max_attempts():
                jump = f" {item['jump_url']}" if item["jump_url"] else ""
                out[digest.SECTION_ESCALATIONS].append({
                    "key": f"chase:{item['id']}",
                    "text": (
                        f"{item['person_name']} owed {what} "
                        f"({digest.overdue_phrase(overdue)}); asked "
                        f"{item['reminders_sent']} time(s) with no reply, so I've "
                        f"stopped chasing it.{jump}"
                    ),
                    "owner_mention": "",
                    "owner_key": owner_key,
                })
                if not item.get("flagged"):
                    effects.append({
                        "type": "chase_escalated",
                        "id": item["id"],
                        "person": item["person_name"],
                        "what": what,
                        "reminders_sent": item["reminders_sent"],
                    })
                continue

            out[digest.SECTION_OVERDUE].append({
                "key": f"chase:{item['id']}",
                "text": f"{what} {digest.overdue_phrase(overdue)}",
                "owner_mention": mention,
                "owner_key": owner_key,
            })
            effects.append({
                "type": "chase_attempt",
                "id": item["id"],
                "person_id": item["person_id"],
                "person_name": item["person_name"],
                "what": what,
                "attempt": item["reminders_sent"] + 1,
            })

        return out, effects

    async def _collect_sheet_sections(self, *, today) -> tuple[dict, str]:
        """HOT and HYGIENE, read live from the tracker.

        `tracker.all_flags` already dedups a row across the three finders and
        orders them hot → dead → stalled, so a row appears exactly once. Here
        that single list is split: HOT leads the digest on its own, because a
        prospect who replied and got silence is a different order of problem
        from a row with an untidy Next Steps cell, and burying it under the
        hygiene rows is how it gets missed.
        """
        out = {k: [] for k in digest.SECTION_ORDER}
        if not config.SHEET_FLAGS_ENABLED:
            return out, ""

        try:
            tab = await asyncio.to_thread(gtm_sheet.SHEETS.tab, gtm_sheet.TRACKER)
        except gtm_sheet.SheetAccessError as e:
            log.info("[digest] no tracker to read flags from: %s", e)
            return out, ""
        if tab is None:
            return out, ""

        staleness = ""
        try:
            staleness = gtm_sheet.SHEETS.staleness_note(tab) or ""
        except Exception:
            log.debug("[digest] no staleness note available", exc_info=True)

        for row in tracker.all_flags(tab.rows, today=today):
            kind = row.get("_flag")
            company = (row.get("company") or "").strip()
            key = (
                f"{kind}:{self.db.company_key(company)}:"
                f"{self.db.company_key(str(row.get('poc') or row.get('_row') or ''))}"
            )
            section = (
                digest.SECTION_HOT if kind == tracker.FLAG_HOT else digest.SECTION_HYGIENE
            )
            out[section].append({
                "key": key,
                "text": tracker.digest_flag_line(row),
                "owner_mention": "",
                "owner_key": "",
            })

        # The weekly funnel numbers, on their weekday only. They used to be
        # their own scheduled post; a second unprompted message a week is still
        # a second unprompted message, so they ride along here instead.
        if config.WEEKLY_DIGEST_ENABLED:
            weekday = max(0, min(6, config.WEEKLY_DIGEST_WEEKDAY))
            if today.weekday() == weekday:
                try:
                    metrics = tracker.funnel_metrics(
                        tab.rows, since=today - timedelta(days=7), today=today
                    )
                    out[digest.SECTION_FUNNEL] = [
                        {"text": line} for line in tracker.funnel_lines(metrics)
                    ]
                except Exception:
                    log.exception("[digest] could not compute the weekly funnel metrics")

        return out, staleness

    def _age_digest_items(self, sections: dict, *, on_date: str) -> None:
        """Stamp each item with how many digests it has now appeared in.

        This is CARRY-FORWARD: an unresolved item comes back tomorrow wearing
        its age, and one that got resolved during the day just isn't collected
        any more. Idempotent per day, so a retry after a refused send doesn't
        age everything twice.

        AN ITEM'S KEY IS THE THING, NOT THE SECTION. A deadline that was "due
        tomorrow" on Monday, "due today" on Tuesday and overdue on Wednesday is
        one item on its third day — keying it per section would restart the age
        at every transition, and an age that resets exactly when a problem gets
        worse is worse than useless.

        An item the store can't age is shown WITHOUT an age rather than dropped
        — losing the marker is a cosmetic failure, losing the item is not.
        """
        for key in digest.ITEM_SECTIONS:
            for item in sections.get(key) or []:
                item_key = item.get("key")
                if not item_key or item.get("overflow"):
                    item["age"] = 1
                    continue
                try:
                    item["age"] = self.db.note_digest_item(
                        item_key=item_key, section=key, on_date=on_date
                    )
                except Exception:
                    log.exception("[digest] could not age item %s", item_key)
                    item["age"] = 1

    # -- effects (applied only once the digest has actually posted) ---------

    def _apply_digest_effects(
        self, effects: list[dict], *, channel_id: int, message_id: int
    ) -> None:
        """Record what the digest just did: attempts spent, escalations raised.

        Ordering is deliberate — this runs AFTER the send. A digest that was
        refused (out-of-scope channel, Discord outage) must not burn a chase
        attempt, or a week of outages would silently escalate everything.

        `reminder_message_id` is deliberately NOT set to the digest's message
        id: one message covers many chases, so pointing all of them at it would
        make a ✅ on the digest close an arbitrary one. Closing a chase is done
        by replying to (or ✅-ing) the promise itself, which is unambiguous.
        """
        now_ts = followups.to_ts(followups.now_utc())

        for eff in effects:
            kind = eff.get("type")
            try:
                if kind == "deadline_reminded":
                    self.db.mark_deadline(eff["id"], reminded=True)

                elif kind == "deadline_attempt":
                    self.db.mark_deadline(eff["id"], bump_chases=True)
                    self.db.record_nudge(
                        target_key=f"deadline:{eff['company_key']}",
                        subject_key=f"deadline:{eff['id']}",
                        kind="deadline",
                        channel_id=channel_id,
                        message_id=message_id,
                        sent_at=now_ts,
                    )
                    state.audit(
                        "deadline_chase",
                        reason="carried in the daily digest's OVERDUE section",
                        company=eff["company"], kind=eff["kind"],
                        due_date=eff["due_date"],
                    )

                elif kind == "deadline_escalated":
                    self.db.mark_deadline(eff["id"], escalated=True)
                    state.audit(
                        "deadline_escalated",
                        reason=(
                            f"no response after {eff['chases_sent']} chase(s); the cap is "
                            f"{self._max_attempts()}"
                        ),
                        company=eff["company"], kind=eff["kind"],
                        due_date=eff["due_date"],
                    )

                elif kind == "chase_attempt":
                    # No reminder id: see the docstring.
                    self.db.mark_chase_reminded(
                        eff["id"], reminder_message_id=None, at=now_ts
                    )
                    self.db.record_nudge(
                        target_key=f"person:{eff['person_id'] or eff['person_name']}",
                        subject_key=f"chase:{eff['id']}",
                        kind="chase",
                        channel_id=channel_id,
                        message_id=message_id,
                        sent_at=now_ts,
                    )
                    state.audit(
                        "chase_nudged",
                        reason=(
                            f"carried in the daily digest's OVERDUE section "
                            f"(attempt {eff['attempt']} of {self._max_attempts()})"
                        ),
                        person=eff["person_name"], what=eff["what"], chase_id=eff["id"],
                    )

                elif kind == "chase_escalated":
                    self.db.mark_chase_flagged(eff["id"])
                    state.audit(
                        "chase_given_up",
                        reason=(
                            f"no reply after {eff['reminders_sent']} attempt(s); the cap "
                            f"is {self._max_attempts()}. It stays in the digest's "
                            f"ESCALATIONS section until it is resolved."
                        ),
                        person=eff["person"], what=eff["what"], chase_id=eff["id"],
                    )

                else:
                    log.warning("[digest] unknown effect %r; ignoring", kind)
            except Exception:
                log.exception("[digest] could not apply effect %r for id %s", kind, eff.get("id"))

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
        # Readable but going stale — a different fact from unavailable, and one a
        # supervisor has to see: the answers keep coming, they just get older.
        for src in sources.degraded():
            notable.append(
                {
                    "kind": "source_degraded",
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

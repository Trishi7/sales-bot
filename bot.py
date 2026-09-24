"""The sales & marketing Chief of Staff bot.

Scoped to the sales channels, it does three things and nothing else:

  ANSWERS QUESTIONS — ONLY WHEN TAGGED. In EVERY sales channel, including
  SALES_ASK_CHANNEL_ID, the bot answers only when it is @-mentioned in the
  message text or when someone replies directly to one of its own messages. A
  message that tags somebody else is never answered. Questions go to the
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

WHETHER TO ANSWER AT ALL is `should_respond()` — ONE function, the only thing in
this file that can authorise a reply to a human message. Both entry points that
can produce one (`on_message` and `on_raw_message_edit`) call it and nothing
else; there is no per-channel variant and no second copy of the rule. It logs
every verdict as `[gate] <responded|ignored> msg=<id> reason=<...>`.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord

import activation
import approvals
import cadence
import focus
import config
import deadlines as dl
import digest
import drip
import drive
import events as events_mod
import evidence
import followups
import gtm_sheet
import guardrails
import mapping_sheet
import meetings
import leave
import nextaction
import rules
import simulation
import notes
import persona
import prep
import query
import research
import sheetwrite
import sources
import state
import tone
import strategy
import todos
import tracker
from db import DB
from llm import LLM
from memory import ConversationMemory
from query_engine import QueryEngine

log = logging.getLogger(__name__)

# ✅ on a nudge closes that chase — "handled, stop asking".
CLOSE_EMOJI = "✅"

# Every mention token Discord puts in message TEXT: <@id>, <@!id> (a nickname
# mention) and <@&id> (a role). Used by the tag gate to tell "they tagged me"
# from "they tagged someone else", which is never answered.
_MENTION_TOKEN_RE = re.compile(r"<@[!&]?(\d+)>")

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
        # The to-do sheet's id lives in the database, so that source needs a
        # handle to report its own status honestly.
        sources.TODO_SHEET.bind(self.db)
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
        # The last `todos.ensure()` result: whether the sheet exists, its link,
        # and which addresses it actually reached. Held so the digest can carry
        # the link and so "@bot show the to-dos" can explain a failed share
        # instead of reporting an empty list.
        self._todo_state: dict = {}
        # DRY-RUN MODE (`python -m main --dry-run-digest`). When true the digest
        # can be BUILT but nothing may be written anywhere: no Discord send, no
        # carry-forward ageing, no sheet created, no row appended. It exists so
        # "what will Monday's digest look like" can be answered without a
        # Monday, and it would be worthless if answering the question changed
        # the thing being asked about.
        self._dry_run: bool = False
        # The date whose digest we have already reported as suppressed. The
        # sweeper ticks every few minutes, so without this the kill switch would
        # write the same line into pm2's log a few hundred times a day and the
        # silence would be buried in the noise announcing it. One line per
        # skipped digest, per day.
        self._digest_suppressed_on: str = ""
        # The day the proposal sweep last ran. ONE sweep per day, in the
        # first slot — a per-process marker rather than a database row,
        # because a restart re-running it once is harmless (the nudged
        # proposals are already marked and drop out of the next query)
        # while a missed one would leave the queue growing unmentioned.
        self._swept_proposals_on: str = ""
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

        # THE TO-DO SHEET. Created on first run and shared with the team, on a
        # thread because both are blocking HTTP calls. The LINK is not posted
        # here: it rides the next daily digest, because this bot sends exactly
        # one unprompted message a day and a "here is a spreadsheet" post would
        # be a second one. `needs_announcement` is persisted, so a restart
        # between the create and that digest doesn't lose the link.
        await asyncio.to_thread(self._ensure_todo_sheet)

        # THE STRATEGY DOC. Probed once here so "the plan is unreadable" is a
        # startup line with the fix in it, rather than a surprise inside an
        # answer hours later. Nothing here posts.
        await asyncio.to_thread(self._check_strategy_doc)

        for src in sources.status_report():
            log.info("[bot] source %s: %s — %s", src["key"], src["status"], src["detail"])

        pol = persona.policy_status()
        log.info(
            "[bot] policy %s (%s, %d chars) — re-read on every question",
            "loaded" if pol["loaded"] else "MISSING", pol["path"], pol["chars"],
        )

        # THE SHEET-WORLD REPORT, at boot. It reads the CANONICAL "Outreach
        # PoCs" tab and LOGS the four things that fail silently: which tab was
        # found, its full discovered schema, how many of its rows are ACTIVE,
        # and which named columns sit inside the writable window between the
        # restricted bands. It sends NOTHING — the digest still posts at
        # SALES_DIGEST_TIME and only then.
        #
        # It replaced the phase-1 cadence dry run, which printed which rows each
        # lettered rule fired on. Those rules are retired, so that report would
        # now be a page of zeroes; these four numbers are what actually decides
        # whether the bot can see anything today.
        await self._log_sheet_world()

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

    async def _log_sheet_world(self) -> None:
        """Log WHAT THE BOT IS READING at boot: tab, schema, activation, window.

        READ-ONLY and SEND-FREE. Four things fail silently on a live sheet and
        all four are printed here, on the first restart, rather than surfacing a
        week later as a digest that says nothing:

          - the canonical tab was renamed, so there is no tab at all;
          - a column was renamed, so a role is unmapped;
          - the activation columns are colour-coded or empty, so every row reads
            as inactive and the bot has nothing to talk about;
          - the restricted bands have drifted, so the writable window points at
            a column somebody is using.
        """
        # THE RULES FIRST, because everything below is only interesting if the
        # schedule that consumes it loaded. A broken bot_rules.yaml means the
        # bot says nothing on its own initiative, and that has to be the first
        # thing in the boot log, not the last.
        try:
            await asyncio.to_thread(rules.log_startup)
        except Exception:
            log.exception("[rules] the rules file could not be reported at boot")

        roles = []
        try:
            # WHICH TAB GOT WHICH ROLE, before anything else.
            roles = await asyncio.to_thread(gtm_sheet.SHEETS.role_assignment)
            for kind, label, titles in roles:
                log.info("[gtm.roles] %-18s %-52s -> %s", kind, label, titles)
        except Exception:
            log.info("[gtm] could not report the tab roles", exc_info=True)

        tab = None
        try:
            tab = await asyncio.to_thread(gtm_sheet.SHEETS.pocs_tab)
        except Exception:
            log.info("[gtm] the canonical tab could not be read at boot", exc_info=True)

        result = None
        if config.CADENCE_ENABLED:
            try:
                result = await self._run_cadence(today=dl.today_ist())
            except Exception:
                log.exception(
                    "[cadence] the startup pass failed; the digest is unaffected"
                )

        activated = 0
        try:
            activated = len(await asyncio.to_thread(self.db.activated_row_keys))
        except Exception:
            log.debug("[activation] could not count the explicit activations", exc_info=True)

        # THE QUEUE, COMPUTED AND LOGGED, SENT NOWHERE. Printing it at boot is
        # how a threshold nobody has tuned, or a column that turns out to be
        # colour-coded, is visible on the first restart rather than in a week of
        # a queue that was quietly empty.
        queue = None
        try:
            queue = await self._run_next_actions(today=dl.today_ist())
        except Exception:
            log.exception("[nextaction] the startup queue failed; nothing else is affected")
        if queue is not None:
            try:
                for line in nextaction.preview_text(queue).splitlines():
                    log.info("[nextaction.preview] %s", line)
            except Exception:
                log.exception("[nextaction] could not render the startup preview")
            state.audit(
                "next_action_queue",
                reason="startup: the computed queue. NOTHING was sent.",
                active_rows=queue.get("rows", 0),
                inactive_rows=queue.get("inactive", 0),
                actions=len(queue.get("actions") or []),
                by_type={k: len(v) for k, v in (queue.get("by_type") or {}).items()},
                silent=queue.get("silent", {}),
                sent=False,
            )

        try:
            text = cadence.boot_report_text(
                result,
                source=(result or {}).get("source", ""),
                staleness=(result or {}).get("staleness", ""),
                roles=roles, tab=tab, activated=activated,
            )
        except Exception:
            log.exception("[gtm] could not render the sheet-world report")
            return
        for line in text.splitlines():
            log.info("[sheet.world] %s", line)
        state.audit(
            "sheet_world",
            reason="startup: which tab, which columns, how many rows are active",
            tab=getattr(tab, "title", ""),
            tab_roles={kind: titles for kind, _label, titles in roles},
            active_rows=(result or {}).get("rows", 0),
            inactive_rows=(result or {}).get("inactive", 0),
            excluded_rejected=(result or {}).get("excluded", 0),
            explicit_activations=activated,
            restricted_ranges=config.RESTRICTED_COLUMN_RANGES,
            writable_window=config.writable_window_label(),
            sheet_health_lines=len((result or {}).get("updates_all") or []),
        )

    def _ensure_todo_sheet(self) -> dict:
        """STARTUP: make sure "Membrane Sales To-Dos" exists and is SHARED.

        Blocking; called via a thread. Never raises — a Drive outage on boot
        must not stop the bot connecting, and the next boot retries.

        THE SHARE IS THE HALF THAT MATTERS. A spreadsheet the service account
        creates is owned by the service account and lives in a Drive no human
        can browse, so an unshared sheet is invisible rather than merely
        awkward. Every address that failed is logged at ERROR with its fix.
        """
        if not todos.enabled():
            log.info("[todos] TODO_SHEET_ENABLED=false — no to-do sheet is created or read")
            self._todo_state = {"ok": False, "error": "TODO_SHEET_ENABLED=false"}
            return self._todo_state
        if self._dry_run:
            log.info("[todos] dry run — not creating or sharing anything")
            self._todo_state = {"ok": False, "error": "dry run: nothing was created"}
            return self._todo_state
        try:
            result = todos.ensure(self.db)
        except Exception as e:
            log.exception("[todos] the startup check raised; the bot continues without it")
            self._todo_state = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            return self._todo_state

        self._todo_state = result
        if not result["ok"]:
            log.error(
                "[todos] the to-do sheet is NOT usable: %s %s",
                result["error"], result["remedy"],
            )
            return result
        log.info(
            "[todos] %s — %s (shared with %s%s). The link posts with the next daily "
            "digest, not as a message of its own.",
            "CREATED" if result["created"] else "already existed",
            result["url"], ", ".join(result["shared"]) or "nobody",
            f"; FAILED for {[f['email'] for f in result['failed']]}" if result["failed"] else "",
        )
        for failure in result["failed"]:
            log.error(
                "[todos] %s cannot open the to-do sheet: %s %s",
                failure["email"], failure["error"], failure.get("remedy", ""),
            )
        return result

    def _check_strategy_doc(self) -> dict:
        """STARTUP CHECK for the strategy doc. Blocking; called via a thread.

        Reports three things a human can act on: whether it is readable, HOW
        CURRENT it is, and whether it has a target section the outreach-vs-plan
        check can run against. Never raises.
        """
        try:
            readable, detail = strategy.status_detail()
        except Exception as e:
            log.exception("[strategy] the startup check raised")
            return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
        if readable:
            log.info("[strategy] %s", detail)
            if strategy.is_stale():
                log.warning("[strategy] %s", strategy.currency_line())
        else:
            log.warning("[strategy] NOT readable — %s", detail)
        return {"ok": readable, "detail": detail}

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

        info = access.get(gtm_sheet.ORIGINAL, {})
        if info.get("ok"):
            log.info(
                "[bot] GTM Playbook reachable: %r (%d tabs)",
                info.get("title"), len(info.get("tabs") or []),
            )
        else:
            log.error("[bot] GTM Playbook NOT reachable: %s", info.get("error"))
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
            playbook_ok=bool(access.get(gtm_sheet.ORIGINAL, {}).get("ok")),
            writes_enabled=config.SHEET_WRITES_ENABLED,
            writable_window=config.writable_window_label(),
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

        # THE ONE GATE. `should_respond` is the only thing in this file that can
        # authorise a reply, and it logs its verdict either way.
        if await self.should_respond(message):
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
        # The SAME gate as on_message — including its author-is-a-human check, so
        # nothing is re-implemented here.
        if not await self.should_respond(message):
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

    async def should_respond(self, message: discord.Message) -> bool:
        """THE ONE GATE. Every reply this bot can produce to a human message is
        decided here and nowhere else. There is no second gate, no per-channel
        rule, and no exemption for SALES_ASK_CHANNEL_ID — that channel is the
        bot's POSTING home (the digest, the deadline announcements) and nothing
        more. Its incoming messages are treated exactly like every other sales
        channel's.

        True ONLY when BOTH hold:

          (a) the author is a human — never another bot, and never this bot
              itself; AND
          (b) the bot is EXPLICITLY @-mentioned (`self.user in message.mentions`,
              backed by the literal <@id> / <@!id> token in the message text), OR
              the message is a DIRECT REPLY to one of the BOT'S OWN messages
              (`message.reference` resolving to a message the bot authored).

        Never a trigger, whatever else the message contains:

          - @here / @everyone. `message.mention_everyone` is a broadcast at the
            channel, not a tag of this bot, and it is checked before anything
            that could say yes.
          - a message that tags somebody ELSE — even one that also tags the bot
            or replies to it. Another person was asked; the bot answering over
            them is exactly the noise this gate exists to prevent.

        Everything else is people talking to each other. No reply, and no typing
        indicator — this bot never opens one, so an ignored message costs a log
        line and nothing else.

        Every decision is logged at INFO as
        `[gate] <responded|ignored> msg=<id> reason=<...>`, so the reason a
        message did or did not get an answer is readable in production without
        a repro.
        """
        mid = getattr(message, "id", 0)

        def verdict(ok: bool, reason: str) -> bool:
            log.info(
                "[gate] %s msg=%s reason=%s",
                "responded" if ok else "ignored", mid, reason,
            )
            return ok

        # (a) A human wrote it. Other bots and — critically — this bot's own
        # messages can never trigger an answer; a bot replying to itself is a
        # loop, not a conversation.
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return verdict(False, "ignored")
        me = getattr(self.user, "id", None)
        if me is not None and getattr(author, "id", None) == me:
            return verdict(False, "ignored")

        # @here / @everyone is addressed at the room. It is NOT a bot mention,
        # and it is rejected before the mention test so it can never be read as
        # one.
        if getattr(message, "mention_everyone", False):
            return verdict(False, "ignored")

        # Someone else was tagged: it is their message to answer.
        if self._tags_someone_else(message):
            return verdict(False, "ignored")

        # (b) Reply-to-bot is tested BEFORE the mention test, because Discord
        # silently adds the replied-to author to `message.mentions` — so a bare
        # reply to the bot would otherwise be logged as "mentioned" when what
        # actually happened is a reply. This is also the path the deadline
        # chasing runs on: a reply to a nudge or to the digest reaches the bot
        # by design.
        if await self._is_reply_to_self(message):
            return verdict(True, "reply-to-bot")

        if self._is_self_mentioned_explicitly(message):
            return verdict(True, "mentioned")

        return verdict(False, "ignored")

    def _tags_someone_else(self, message: discord.Message) -> bool:
        """True when the message TEXT tags anyone but the bot — another person, a
        role, @everyone or @here.

        Only the text is read. Discord adds an invisible mention of the author
        being replied to, and that is not somebody being tagged; reading
        `message.mentions` here would silence every reply to a person."""
        content = message.content or ""
        if not content:
            return False
        if "@everyone" in content or "@here" in content:
            return True
        me = str(getattr(self.user, "id", "") or "")
        for match in _MENTION_TOKEN_RE.finditer(content):
            if match.group(0).startswith("<@&"):
                return True  # a role is never the bot
            if match.group(1) != me:
                return True
        return False

    async def _is_reply_to_self(self, message: discord.Message) -> bool:
        """True when the message is a direct reply to one of the BOT's own
        messages — its answer, the digest, a deadline announcement. Replying is
        how you talk to it without typing its name, and it is what the
        deadline chasing has always run on.

        The parent comes from what Discord already sent along where possible and
        is fetched otherwise; a deleted or unreadable parent is not a trigger."""
        me = getattr(self.user, "id", None)
        ref = message.reference
        if me is None or ref is None:
            return False
        resolved = getattr(ref, "resolved", None)
        parent = resolved if isinstance(resolved, discord.Message) else ref.cached_message
        if parent is None:
            if not ref.message_id:
                return False
            try:
                parent = await message.channel.fetch_message(ref.message_id)
            except discord.DiscordException:
                log.debug(
                    "[bot] msg=%s replies to %s, which could not be read — not "
                    "treating it as a reply to me", message.id, ref.message_id,
                )
                return False
        return getattr(parent.author, "id", None) == me

    def _is_self_mentioned_explicitly(self, message: discord.Message) -> bool:
        """True when the bot is EXPLICITLY @-mentioned.

        Two sources agree before this says yes-ish: `self.user in
        message.mentions` (Discord's own parse) and the literal <@id> / <@!id>
        token in the message TEXT. Either is enough, but the text token is what
        makes the distinction meaningful, because `message.mentions` also
        contains people the author never typed — Discord adds the author of a
        replied-to message to it. That reply case is decided by
        `_is_reply_to_self`, which checks who actually WROTE the parent, and
        `should_respond` tests it first for exactly this reason.

        @here / @everyone never reaches here: `should_respond` rejects
        `message.mention_everyone` before calling this."""
        me = getattr(self.user, "id", None)
        if me is None:
            return False
        for user in getattr(message, "mentions", None) or ():
            if getattr(user, "id", None) == me:
                return True
        content = message.content or ""
        return f"<@{me}>" in content or f"<@!{me}>" in content

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

        # THE WRITE PATH, TRIED FIRST. Only two things reach it — a reply to one
        # of the bot's own messages, or an explicit @-mention command — and it
        # declines anything that is not clearly an update, a snooze or an undo,
        # so a question addressed to the bot falls straight through to the
        # engine below. It is first because "sent this morning" is an ANSWER,
        # and routing it to the question engine would produce a reply about the
        # sheet instead of a change to it.
        # SIMULATIONS AND TEST HELPERS, BEFORE EVERYTHING. "simulate monday" is
        # neither an update nor a question, and feeding it to the extractor
        # would have it cheerfully read "monday" as a company. The handler is
        # SILENT outside the test channel, so this costs a regex in the real
        # channels and nothing else.
        if await self._handle_simulation(message, text):
            return True

        if await self._maybe_apply_sheet_update(message, text):
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

        # "other" — a statement, not a question. They still addressed the bot
        # directly (an @-mention, or a reply to something it said), so a reply
        # is owed either way.
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
                    + self._todo_tools()
                    + self._strategy_tools()
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
            # THE CITATION, precomputed per note. Rule 2: anything shaped by a
            # meeting names that meeting, and handing the model the exact string
            # is what stops it inventing a shorter one.
            listed = [dict(m, citation=meetings.citation(m)) for m in found]
            return {
                "enabled": True,
                "configured": True,
                "sync": sync,
                "notes_filter": _filter_facts(),
                "notes": listed,
                "freshness": await _freshness(),
                "citation_rule": (
                    "Every claim you take from one of these notes must name it: "
                    "\"...(<citation>)\". Use the note's own citation field verbatim."
                ),
            }

        async def _meeting_facts(inp: dict):
            """Holds, decisions and commitments, each carrying its citation.

            Companies come from the TRACKER, so a hold can only ever be reported
            against an account that is actually in the pipeline — the bot cannot
            invent one out of a sentence in a note.
            """
            not_cfg = await _not_configured()
            if not_cfg:
                return not_cfg
            await _sync(question_text)
            companies = await self._tracker_company_names()
            try:
                days = int(inp.get("days") or config.MEETING_FACTS_DAYS)
            except (TypeError, ValueError):
                days = config.MEETING_FACTS_DAYS
            found = await asyncio.to_thread(
                meetings.facts, companies=companies, days=max(1, days)
            )
            want_kind = str(inp.get("kind") or "").strip().lower()
            if want_kind:
                found = [f for f in found if f["kind"] == want_kind]
            want_company = str(inp.get("company") or "").strip()
            if want_company:
                key = gtm_sheet.normalise_header(want_company)
                found = [
                    f for f in found
                    if any(gtm_sheet.normalise_header(c) == key for c in f["companies"])
                ]
            return {
                "enabled": True,
                "configured": True,
                "window_days": days,
                "facts": [
                    {
                        "kind": f["kind"], "text": f["text"],
                        "companies": f["companies"], "citation": f["citation"],
                        "date": f["date"],
                    }
                    for f in found[:30]
                ],
                "notes_filter": _filter_facts(),
                "citation_rule": (
                    "Quote each fact with its citation in brackets. If the list is "
                    "empty, say no meeting note says that — do NOT infer a hold from "
                    "the tracker or from the absence of activity."
                ),
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
                "citation": meetings.citation(note),
                "citation_rule": (
                    "MANDATORY: every line you write from this note carries the "
                    "citation above, in brackets, e.g. \"Acme is on hold "
                    "(<citation>)\". A decision, a hold or a commitment quoted "
                    "without its meeting is a bug, not a style choice."
                ),
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
            {
                "schema": {
                    "name": "meeting_facts",
                    "description": (
                        "Holds, decisions and commitments taken from the meeting notes, "
                        "EACH WITH THE MEETING THAT PRODUCED IT. Use this for 'is X on "
                        "hold', 'what did we decide about X', 'who committed to what', "
                        "and whenever you are about to say a company is paused, parked "
                        "or deprioritised. Pass 'company' to scope it to one account. "
                        "Every item has a 'citation' — print it in brackets after the "
                        "claim, e.g. 'Acme is on hold (Sales Bot Discussion, 2 Sep)'. A "
                        "meeting-derived claim with no citation is WRONG: if an item has "
                        "no citation, do not make the claim."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string",
                                        "description": "Scope to one company (optional)."},
                            "kind": {"type": "string",
                                     "description": "hold | decision | commitment"},
                            "days": {"type": "integer",
                                     "description": "Look-back window in days."},
                        },
                        "required": [],
                    },
                },
                "handler": _meeting_facts,
            },
        ]

    # -- tools: the to-do sheet --------------------------------------------

    def _todo_tools(self) -> list[dict]:
        """"@bot show the to-dos" — ALWAYS the link plus the open items.

        Both halves, every time. The link alone is a shrug; the items alone
        leave the asker unable to edit anything, and editing is the whole point
        of a sheet the humans own. So the tool returns both and the description
        tells the model to print both.
        """

        async def _show_todos(inp: dict) -> dict:
            if not todos.enabled():
                return {
                    "available": False,
                    "note": "TODO_SHEET_ENABLED is off, so there is no to-do sheet.",
                }
            if not todos.sheet_id(self.db):
                ensured = await asyncio.to_thread(self._ensure_todo_sheet)
                if not ensured.get("ok"):
                    return {
                        "available": False,
                        "error": ensured.get("error", ""),
                        "fix": ensured.get("remedy", ""),
                        "note": (
                            "The to-do sheet does not exist yet and I could not create "
                            "it. Say that plainly and give the fix."
                        ),
                    }
            try:
                limit = int(inp.get("limit") or config.TODO_SHOW_MAX)
            except (TypeError, ValueError):
                limit = config.TODO_SHOW_MAX
            data = await asyncio.to_thread(todos.open_items, self.db, limit=limit)
            if not data.get("ok"):
                return {
                    "available": False,
                    "error": data.get("error", ""),
                    "fix": data.get("remedy", ""),
                    "link": data.get("url", ""),
                    "note": "Say I couldn't read the sheet, and give the link anyway.",
                }
            shared = (self._todo_state or {}).get("shared") or config.TEAM_SHARE_EMAILS
            return {
                "available": True,
                "title": config.TODO_SHEET_TITLE,
                "link": data.get("url", ""),
                "shared_with": list(shared),
                "open_total": data.get("open_total", 0),
                "items": [
                    {
                        "n": r["n"], "task": r["task"], "owner": r["owner"] or None,
                        "source_meeting": r["source_meeting"] or None,
                        "date_raised": r["date_raised"] or None,
                        "due": r["due"] or None, "status": r["status"],
                    }
                    for r in data.get("rows") or []
                ],
                "note": (
                    "ALWAYS give the link AND the open items — both, every time. Each "
                    "item's source_meeting is the meeting it was committed in: quote it "
                    "in brackets after the item, e.g. 'send the deck (Sales Bot "
                    "Discussion, 2 Sep)'. An item with no source_meeting came from the "
                    "sheet by hand — say nothing about where it came from rather than "
                    "guessing. Status and Notes belong to the team; I never edit them."
                ),
            }

        async def _refresh_todos(_inp: dict) -> dict:
            """Run the extraction WITHOUT writing. Someone asking "what would go
            on the to-do sheet" must not silently trigger a write — the write
            happens on TODO_REFRESH_DAY, in the digest, and nowhere else."""
            companies = await self._tracker_company_names()
            found = await asyncio.to_thread(
                meetings.action_items, days=config.TODO_NOTES_DAYS, companies=companies
            )
            return {
                "available": True,
                "window_days": config.TODO_NOTES_DAYS,
                "candidates": [
                    {
                        "task": f["task"], "owner": f["owner"] or None,
                        "source_meeting": f["source_meeting"],
                        "date_raised": f["date_raised"], "due": f["due"] or None,
                    }
                    for f in found[: config.TODO_MAX_NEW_PER_REFRESH]
                ],
                "note": (
                    "These are commitments read out of the meeting notes. NOTHING WAS "
                    "WRITTEN — the sheet is appended on "
                    + config.TODO_REFRESH_DAY.capitalize()
                    + " as part of the daily digest. Every one of these carries its "
                    "source_meeting and you must quote it alongside the task."
                ),
            }

        return [
            {
                "schema": {
                    "name": "show_todos",
                    "description": (
                        "The team to-do sheet: its LINK and the OPEN items on it. Use "
                        "this for 'show the to-dos', 'what's on the to-do list', 'what "
                        "do I owe', 'action items'. Always print the link and the items "
                        "together."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer",
                                      "description": "How many open items to return."},
                        },
                        "required": [],
                    },
                },
                "handler": _show_todos,
            },
            {
                "schema": {
                    "name": "todo_candidates",
                    "description": (
                        "Action items in this week's meeting notes that COULD go on the "
                        "to-do sheet, each with the meeting it was committed in. Read-"
                        "only — it never writes to the sheet. Use it for 'what came out "
                        "of this week's meetings' or 'what's not on the list yet'."
                    ),
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                },
                "handler": _refresh_todos,
            },
        ]

    # -- tools: the strategy doc -------------------------------------------

    def _strategy_tools(self) -> list[dict]:
        """The plan itself, how current it is, and where outreach diverges.

        Two tools rather than one because they answer different questions and
        fail differently: "what does the plan say" needs the doc, "are we doing
        it" needs the doc AND the tracker, and an answer must never imply the
        second when it could only do the first.
        """

        async def _read_strategy(_inp: dict) -> dict:
            data = await asyncio.to_thread(strategy.read)
            if not data["ok"]:
                return {
                    "available": False,
                    "error": data.get("error", ""),
                    "fix": data.get("remedy", ""),
                    "note": (
                        "I cannot read the strategy doc. Say so in those words and give "
                        "the fix. Do NOT answer what the strategy is from anywhere else."
                    ),
                }
            body = data["text"]
            clipped = len(body) > 6000
            return {
                "available": True,
                "name": data.get("name", ""),
                "link": data.get("url", ""),
                "last_revised": data.get("modified"),
                "age_days": await asyncio.to_thread(strategy.age_days),
                "stale": await asyncio.to_thread(strategy.is_stale),
                "stale_after_days": config.STRATEGY_STALE_DAYS,
                "currency_line": await asyncio.to_thread(strategy.currency_line),
                "targets": [t["phrase"] for t in await asyncio.to_thread(strategy.targets)],
                "text": body[:6000] + ("\n…(truncated)" if clipped else ""),
                "truncated": clipped,
                "note": (
                    "This is the human-owned plan; I have read-only access and never "
                    "edit it. If stale is true, say the plan hasn't been revised in "
                    "age_days days BEFORE quoting it — a quote from a plan nobody has "
                    "touched in a month has to carry that."
                ),
            }

        async def _plan_vs_outreach(inp: dict) -> dict:
            data = await asyncio.to_thread(strategy.read)
            if not data["ok"]:
                return {
                    "available": False, "error": data.get("error", ""),
                    "fix": data.get("remedy", ""),
                    "note": "I can't check outreach against a plan I can't read.",
                }
            try:
                days = max(1, int(inp.get("days") or 7))
            except (TypeError, ValueError):
                days = 7
            try:
                tab = await asyncio.to_thread(gtm_sheet.SHEETS.tab, gtm_sheet.POCS)
            except Exception:
                tab = None
            if tab is None:
                return {
                    "available": False,
                    "error": "the outreach tracker could not be read",
                    "note": "Say I could read the plan but not the tracker, so I can't "
                            "compare them.",
                }
            today = dl.today_ist()
            check = await asyncio.to_thread(
                strategy.plan_check, list(tab.rows),
                since=today - timedelta(days=days), today=today,
            )
            return {
                "available": bool(check["ok"]),
                "reason": check.get("reason", ""),
                "window_days": days,
                "rows_touched": check.get("touched", 0),
                "targets": [t["phrase"] for t in check.get("targets") or []],
                "off_plan": check.get("off_plan") or [],
                "in_plan_nothing_sent": check.get("unworked") or [],
                "lines": check.get("lines") or [],
                "note": (
                    "Both directions matter and neither is an accusation: a plan can be "
                    "out of date and the pipeline right. Give the counts. If available "
                    "is false, say WHY (reason) rather than reporting 'no drift'."
                ),
            }

        return [
            {
                "schema": {
                    "name": "strategy_doc",
                    "description": (
                        "The sales & marketing strategy doc: what it says, when it was "
                        "last revised, and the targets it names. Use it for 'what's the "
                        "plan', 'what are we targeting', 'is the strategy current'."
                    ),
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                },
                "handler": _read_strategy,
            },
            {
                "schema": {
                    "name": "outreach_vs_plan",
                    "description": (
                        "Compare where outreach actually went against the targets the "
                        "strategy doc names, in both directions. Use it for 'are we on "
                        "plan', 'has outreach drifted', 'are we working the right "
                        "segments'."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "days": {"type": "integer",
                                     "description": "Window in days (default 7)."},
                        },
                        "required": [],
                    },
                },
                "handler": _plan_vs_outreach,
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
            tab, err = await asyncio.to_thread(_tab_or_error, gtm_sheet.POCS)
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
            tab, err = await asyncio.to_thread(_tab_or_error, gtm_sheet.POCS)
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
                tracker_tab, terr = await asyncio.to_thread(_tab_or_error, gtm_sheet.POCS)
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

        # THE cadence_list AND cold_list TOOLS ARE RETIRED WITH THE RULES THAT
        # FED THEM. `cadence_list` answered "what did the digest hold back" for
        # the phase-1 lettered rules; `cold_list` answered "who never connected"
        # for the phase-1 cold ceiling. Both rules are gone, so both tools would
        # now return an empty list with a confident description attached — which
        # is worse than not having them, because a tool that always says
        # "nothing" reads as "nothing is wrong".
        #
        # `sheet_status` below is what replaces them, and it answers the question
        # people were really asking through them: what is this bot looking at,
        # and why is it quiet.

        async def _sheet_status(inp: dict) -> dict:
            """WHAT THE BOT IS READING — EVERY tab, rows, activation, write window.

            READ-ONLY. The verification tool for the whole sheet layer: it lists
            every tab discovered in the playbook with its row count and what the
            bot reads it AS, names the canonical tab, counts its rows and its
            ACTIVE rows separately, and lists the named columns inside the
            writable window between the restricted bands.

            EVERY TAB, INCLUDING THE ONES IT DOES NOT READ. A tab reported as
            "not read" is the fastest possible diagnosis of a renamed sheet —
            far faster than noticing, three days later, that a reminder stopped
            arriving. The five context tabs are found by NAME, so a rename
            silently un-finds them and nothing else would say so.

            THE TWO ROW COUNTS ARE THE POINT. "886 rows, 12 active" is the
            honest answer to "why has the digest gone quiet", and it is an
            answer nobody could give while the only visible number was the row
            count.
            """
            try:
                tab = await asyncio.to_thread(gtm_sheet.SHEETS.pocs_tab)
            except gtm_sheet.SheetAccessError as e:
                return {
                    "ok": False,
                    "reason": f"I could not read the spreadsheet: {e}. {e.remedy}".strip(),
                }
            except Exception:
                log.exception("[sheet-status] the canonical tab could not be read")
                return {"ok": False, "reason": "the spreadsheet could not be read just now"}
            if tab is None:
                return {
                    "ok": False,
                    "reason": (
                        "There is no tab named "
                        + (" / ".join(repr(t) for t in config.GTM_POCS_TAB_TITLES)
                           or "(nothing configured)")
                        + " in the GTM Playbook. That tab is the canonical source and it "
                        "is found by NAME, so nothing proactive has anything to run "
                        "against until it exists or GTM_POCS_TAB_TITLES is pointed at "
                        "the right title. Say this plainly."
                    ),
                }

            active, inactive = await asyncio.to_thread(
                self._split_active, tab.rows, "a sheet status question"
            )
            try:
                explicit = await asyncio.to_thread(self.db.list_activated_rows, 25)
            except Exception:
                log.debug("[sheet-status] could not list the activations", exc_info=True)
                explicit = []

            window = gtm_sheet.SHEETS.writable_window_columns(tab)

            try:
                discovered = await asyncio.to_thread(gtm_sheet.SHEETS.discovered_tabs)
            except Exception:
                log.debug("[sheet-status] could not list the discovered tabs",
                          exc_info=True)
                discovered = []

            # The headers themselves are NOT carried into the answer: on a
            # twenty-tab playbook that is several thousand characters of noise
            # in a prompt, and the counts plus the kind are what the question is
            # actually asking. The full headers are in the [gtm.schema] log.
            tabs_found = [
                {
                    "tab": d["title"],
                    "read_as": d["kind_label"] or d["kind"] or "not read",
                    "rows": d["rows"],
                    "columns": d["columns"],
                    "hidden": d["hidden"],
                    "unmapped_columns": len(d["unmapped_headers"]),
                }
                for d in discovered
            ]

            return {
                "ok": True,
                "tab": tab.title,
                "tabs_found": tabs_found,
                "tabs_found_count": len(tabs_found),
                "tabs_read_count": sum(1 for d in discovered if d["kind"]),
                "tabs_not_read": [d["title"] for d in discovered if not d["kind"]],
                "context_tabs_note": (
                    "The Deliverables Checklist, Master Pipeline, Sales Packages, "
                    "AI Events & Summits and goal-setting tabs are found BY NAME and "
                    "are READ-ONLY — no write path can address them. If one is listed "
                    "as 'not read', its title no longer matches the *_TAB_TITLES "
                    "setting that finds it, and everything built on it has gone quiet."
                ),
                "spreadsheet": "the GTM Playbook (GTM_SHEET_ORIGINAL_ID)",
                "total_rows": len(tab.rows),
                "active_rows": len(active),
                "inactive_rows": len(inactive),
                "activation_rule": (
                    "A row is ACTIVE only when a first-contact date OR a connection "
                    "date is present. Inactive rows are never mentioned, chased or "
                    "counted in anything I say unprompted — but I still answer about "
                    "one in full if you ask me by name."
                ),
                "explicit_activations": [
                    {"company": r["company"], "poc": r["poc"], "reason": r["reason"],
                     "since": r["created_at"]}
                    for r in explicit
                ],
                "columns_discovered": len(tab.headers),
                "restricted_column_ranges": config.RESTRICTED_COLUMN_RANGES,
                "writable_window": config.writable_window_label() or "(none)",
                "writable_window_columns": window,
                "writes_enabled": config.SHEET_WRITES_ENABLED,
                "undo_window_hours": config.SHEET_WRITE_UNDO_HOURS,
                "staleness": gtm_sheet.SHEETS.staleness_note(tab),
                "note": (
                    "Reading is unrestricted — the restricted bands are a WRITE lock "
                    "only. The phase-1 cadence rules (a-j) are retired, so the digest's "
                    "cadence sections carry sheet-health lines and nothing else until a "
                    "phase-2 rule set exists."
                ),
            }

        async def _activate_rows(inp: dict) -> dict:
            """Activate the rows an explicit mention-request names.

            THE ONE EXCEPTION TO THE ACTIVATION RULE, and the only thing in this
            file that can make an undated row visible to the proactive features.
            It WRITES — to SQLite, never to the sheet — so it reports exactly
            what it changed and never implies more.

            A name that matches no row is returned as `not_found` rather than
            silently dropped: "I activated them" when one of the three people
            named does not exist in the sheet is the kind of quiet inaccuracy
            that gets a bot distrusted.
            """
            org = str((inp or {}).get("org") or "").strip()
            names = [str(n).strip() for n in ((inp or {}).get("names") or []) if str(n).strip()]
            reason = str((inp or {}).get("reason") or "").strip() or (
                f"explicit request for {org or 'unnamed org'}"
            )
            if not org and not names:
                return {
                    "ok": False,
                    "reason": (
                        "I need an organisation (and optionally the people) to activate. "
                        "Ask again naming the company."
                    ),
                }

            try:
                tab = await asyncio.to_thread(gtm_sheet.SHEETS.pocs_tab)
            except Exception:
                log.exception("[activation] the canonical tab could not be read")
                return {"ok": False, "reason": "the spreadsheet could not be read just now"}
            if tab is None:
                return {
                    "ok": False,
                    "reason": "there is no canonical Outreach PoCs tab to activate rows on",
                }

            matched = activation.matching_rows(tab.rows, org=org, names=names)
            if not matched:
                return {
                    "ok": False,
                    "org": org, "names": names,
                    "reason": (
                        f"No row on {tab.title!r} matches "
                        + (f"{names!r} at " if names else "")
                        + f"{org!r}. I have not activated anything. Say so rather than "
                        "guessing at a different spelling."
                    ),
                }

            today_iso = dl.iso(dl.today_ist())
            activated: list[dict] = []
            already_dated: list[dict] = []
            already_activated: list[dict] = []
            for row in matched:
                entry = {
                    "company": gtm_sheet.clean_cell(row.get("company")),
                    "poc": gtm_sheet.clean_cell(row.get("poc")),
                    "sheet_row": row.get("_row"),
                    "why": activation.why_active(row),
                }
                if activation.has_activating_date(row):
                    already_dated.append(entry)
                    continue
                made = await asyncio.to_thread(
                    lambda r=row, e=entry: self.db.activate_row(
                        row_key=activation.row_key(r),
                        company=e["company"], poc=e["poc"],
                        reason=reason, on_date=today_iso,
                    )
                )
                (activated if made else already_activated).append(entry)

            not_found = [
                n for n in names
                if not any(
                    gtm_sheet.normalise_header(n)
                    in gtm_sheet.normalise_header(r.get("poc", ""))
                    for r in matched
                )
            ]
            log.info(
                "[activation] explicit request for %r: %d activated, %d already dated, "
                "%d already activated, %d not found",
                org, len(activated), len(already_dated), len(already_activated),
                len(not_found),
            )
            state.audit(
                "rows_activated",
                reason=reason,
                org=org, names=names,
                activated=[f"{e['company']}/{e['poc']}" for e in activated],
                already_active=len(already_dated) + len(already_activated),
                not_found=not_found,
            )
            return {
                "ok": True,
                "tab": tab.title,
                "org": org,
                "activated": activated,
                "already_active_by_date": already_dated,
                "already_activated_before": already_activated,
                "not_found": not_found,
                "note": (
                    "Activated rows are now visible to the proactive features and stay "
                    "that way across restarts. Rows listed under "
                    "'already_active_by_date' needed no activation — they already carry "
                    "a first-contact or connection date. Nothing was written to the "
                    "spreadsheet; this is my own record."
                ),
            }

        # THE row_flags TOOL IS RETIRED WITH THE FLAGS BEHIND IT. HOT / STALLED
        # / DEAD-DEAL were the old per-row "what next" logic and the NEXT-ACTION
        # STATE MACHINE replaces them. The four tools below are what answer the
        # questions row_flags used to: `cadence_preview` for "what needs
        # attention", `next_action` for one company, and the two that let a
        # person change what the queue will say.

        async def _research_brief(inp: dict) -> dict:
            """Gather the material and write the brief. Sends nothing, writes nothing."""
            person = str((inp or {}).get("person") or "").strip()
            org = str((inp or {}).get("org") or "").strip()
            if not person:
                return {"ok": False, "reason": "research_brief needs a person's name."}

            tab, err = await asyncio.to_thread(_tab_or_error, gtm_sheet.POCS)
            if err:
                return err
            matched = activation.matching_rows(tab.rows, org=org, names=[person])
            if not matched and org:
                matched = activation.matching_rows(tab.rows, org=person)
            if not matched:
                return {
                    "ok": False, "person": person, "org": org,
                    "reason": (
                        f"I have no row for {person}"
                        + (f" at {org}" if org else "")
                        + ". I only brief on people who are already on the tab — I do "
                          "not search for someone I have never heard of."
                    ),
                }
            if len(matched) > 1:
                names = ", ".join(
                    gtm_sheet.clean_cell(r.get("company")) or "?" for r in matched[:6]
                )
                return {
                    "ok": False, "person": person,
                    "reason": f"There are {len(matched)} rows matching that ({names}) — "
                              f"which org do you mean?",
                }

            row = matched[0]
            gathered = await asyncio.to_thread(lambda: research.gather(row))

            # THE LANES COME FROM THE TEAM'S OWN DOCUMENTS, read at brief time.
            lanes = persona.load_policy()
            try:
                if sources.STRATEGY_DOC.connected:
                    lanes += "\n\n=== STRATEGY DOC ===\n" + (
                        await asyncio.to_thread(sources.STRATEGY_DOC.text)
                    )[:12000]
            except Exception:
                log.debug("[research] no strategy doc for the lanes", exc_info=True)

            refusal = research.refusal_note(gathered["links_refused"])
            material = "\n\n".join(filter(None, [
                f"PERSON: {gathered['person']}\nORG: {gathered['org']}",
                "WHAT THE SHEET HOLDS:\n" + "\n".join(
                    f"  {k}: {v}" for k, v in gathered["sheet_facts"].items()
                ) if gathered["sheet_facts"] else "",
                "LINKEDIN: " + gathered["linkedin"]["note"],
                ("REFUSED LINKS (put this line in the brief verbatim): " + refusal)
                if refusal else "",
                "NO RESEARCH LINKS ARE ON THIS ROW — say so rather than inventing work."
                if gathered["no_links"] else "",
                *[
                    f"FETCHED {f['url']} (from the {f['column']} column):\n"
                    + (f["text"][:6000] if f["ok"] else f"could not read it: {f['error']}")
                    for f in gathered["links_fetched"]
                ],
                "=== OUR LANES (the team's own words — use these) ===\n" + lanes[:20000],
            ]))

            brief = "(no model available)"
            if self.llm is not None:
                brief = await self.llm.research_brief(material=material)

            state.audit(
                "research_brief",
                reason=f"brief requested on {gathered['person']}",
                person=gathered["person"], org=gathered["org"],
                fetched=[f["url"] for f in gathered["links_fetched"] if f["ok"]],
                refused=[r.get("url") or r.get("why") for r in gathered["links_refused"]],
                linkedin_available=gathered["linkedin"]["available"],
                sent_anything=False, written_to_sheet=False,
            )
            log.info(
                "[research] brief on %s (%s): %d link(s) fetched, %d refused, "
                "linkedin=%s. Nothing sent, nothing written.",
                gathered["person"], gathered["org"],
                sum(1 for f in gathered["links_fetched"] if f["ok"]),
                len(gathered["links_refused"]), gathered["linkedin"]["available"],
            )
            return {
                "ok": True,
                "sent_anything": False,
                "written_to_sheet": False,
                "person": gathered["person"],
                "org": gathered["org"],
                "brief": brief,
                "links_fetched": [
                    {"url": f["url"], "column": f["column"], "ok": f["ok"],
                     "error": f["error"]}
                    for f in gathered["links_fetched"]
                ],
                "refusal_line": refusal,
                "linkedin": gathered["linkedin"],
                "no_links_on_row": gathered["no_links"],
                "note": (
                    "COPY MATERIAL. Hand the brief back as a draft for them to edit — it "
                    "is never sent and never written to the sheet. Repeat the "
                    "refusal_line verbatim if it is non-empty, and say plainly if "
                    "LinkedIn access is pending."
                ),
            }

        async def _cadence_preview(inp: dict) -> dict:
            """TODAY'S QUEUE, computed and returned. NOTHING IS SENT.

            The whole point of this tool is that it is safe: it runs the same
            state machine the digest would, and hands the result back to the
            asker instead of to a channel. It can be run with the digest kill
            switch off, on a Sunday, mid-deploy.
            """
            result = await self._run_next_actions(today=dl.today_ist())
            if result is None:
                return {
                    "ok": False,
                    "reason": (
                        "The queue could not be computed — either NEXT_ACTION_ENABLED is "
                        "off, or there is no canonical Outreach PoCs tab to read. Say "
                        "which rather than implying there is nothing to do."
                    ),
                }
            want_type = str((inp or {}).get("type") or "").strip().lower()
            want_owner = gtm_sheet.normalise_header((inp or {}).get("owner") or "")
            actions = result.get("actions") or []
            if want_type:
                # Matches the trigger name or the rule id, because somebody
                # asking for "R5" and somebody asking for "prospects" mean the
                # same thing and neither should get an empty list.
                actions = [
                    a for a in actions
                    if a["type"] == want_type
                    or str(a.get("rule_id", "")).lower() == want_type
                ]
            if want_owner:
                actions = [
                    a for a in actions
                    if want_owner in gtm_sheet.normalise_header(a.get("owner") or "")
                ]
            cap = max(1, config.NEXT_ACTION_PREVIEW_MAX)
            log.info(
                "[nextaction] preview requested%s -> %d action(s). Nothing sent.",
                f" (type={want_type or '-'} owner={want_owner or '-'})"
                if (want_type or want_owner) else "",
                len(actions),
            )
            # GROUPED BY RULE. Each section names the rule that produced it and
            # carries the reason line the evaluator wrote, so the output can be
            # checked against bot_rules.yaml line by line without the code.
            by_rule: dict = {}
            for a in actions[:cap]:
                by_rule.setdefault(a.get("rule_id") or "—", []).append(a)

            rules_status = rules.status()
            return {
                "ok": True,
                "sent_anything": False,
                "tab": getattr(result.get("tab"), "title", ""),
                "today": dl.iso(result.get("today")),
                "weekday": (result.get("today") or dl.today_ist()).strftime("%A"),
                "rules_file": rules_status.get("path", ""),
                "rules_loaded": rules_status.get("count", 0),
                "active_rows": result.get("rows", 0),
                "inactive_rows": result.get("inactive", 0),
                "total_actions": len(result.get("actions") or []),
                "shown": min(len(actions), cap),
                "filtered_to": {"rule": want_type, "owner": want_owner},
                "bands_in_order": [
                    nextaction.BAND_LABELS[b] for b in sorted(nextaction.BAND_LABELS)
                ],
                # WHICH RULES RAN AND WHICH DID NOT, AND WHY. "R4 produced
                # nothing" and "R4 does not run on a Thursday" look identical in
                # a list that shows only what fired, and only one of them is
                # worth investigating.
                "rules_run": result.get("rules_run", []),
                "by_rule": {
                    rule_id: {
                        "rule": rule_id,
                        "name": items[0].get("rule_name", ""),
                        "items": len(items),
                        "max_items_per_post": items[0].get("max_items_per_post"),
                        "destination": items[0].get("destination"),
                        "counts_toward_cap": items[0].get("counts_toward_cap"),
                        "lines": [
                            {
                                "company": a["company"], "poc": a["poc"],
                                "owner": a["owner"] or "(unassigned)",
                                "due_date": a["due_iso"],
                                "overdue_days": a["overdue_days"],
                                "text": a["text"], "why": a["why"],
                                "web_pending": a.get("web_pending", False),
                                "sheet_row": a["sheet_row"],
                            }
                            for a in items
                        ],
                    }
                    for rule_id, items in by_rule.items()
                },
                "queue": [
                    {
                        "rule": a.get("rule_id", ""), "rule_name": a.get("rule_name", ""),
                        "type": a["type"], "label": a["label"],
                        "owner": a["owner"] or "(unassigned)",
                        "company": a["company"], "poc": a["poc"],
                        "due_date": a["due_iso"], "overdue_days": a["overdue_days"],
                        "priority": a["priority"], "priority_label": a["priority_label"],
                        "text": a["text"], "why": a["why"],
                        "web_pending": a.get("web_pending", False),
                        "sheet_row": a["sheet_row"],
                    }
                    for a in actions[:cap]
                ],
                "truncated": len(actions) > cap,
                "deduped": [
                    {"company": d.get("company"), "poc": d.get("poc"),
                     "rule": d.get("rule_id"), "kept_by": d.get("dropped_for"),
                     "why": d.get("dropped_why")}
                    for d in (result.get("deduped") or [])[:20]
                ],
                "web_pending_count": result.get("web_pending", 0),
                "web_pending_note": (
                    "Items marked web_pending come from rules that need web research "
                    "this bot cannot do yet (R1, R2, R3, R6's email search, R8 and R10's "
                    "news, R11). They are SHOWN rather than skipped so a configured rule "
                    "never looks like a quiet week — say which ones are waiting."
                ),
                "no_action_for": result.get("silent", {}),
                "no_action_labels": nextaction.SILENT_LABELS,
                "stopped_rows": result.get("stopped", [])[:20],
                "snoozed_rows": result.get("snoozed", [])[:20],
                "staleness": result.get("staleness", ""),
                "note": (
                    "GROUP THE ANSWER BY RULE and name each rule — 'R6 Connected, no DM "
                    "yet: 4' — then give each line's reason, which says which cells "
                    "produced it. ONE CONTACT IS NAMED AT MOST ONCE A DAY: anything in "
                    "'deduped' was selected by a later rule and dropped in favour of an "
                    "earlier one, and it is worth saying so. Quote 'rules_run' for the "
                    "rules that did not run today and why. NOTHING WAS SENT — this is a "
                    "preview. Quote the 'no_action_for' counts too; a queue that lists "
                    "only what it found looks complete when it is not."
                ),
            }

        async def _next_action_for(inp: dict) -> dict:
            """The one next action for a named company, or WHY there isn't one."""
            company = str((inp or {}).get("company") or "").strip()
            poc = str((inp or {}).get("poc") or "").strip()
            if not company:
                return {"ok": False, "reason": "next_action needs a 'company'."}
            result = await self._run_next_actions(today=dl.today_ist())
            if result is None:
                return {
                    "ok": False,
                    "reason": (
                        "The queue could not be computed — either NEXT_ACTION_ENABLED is "
                        "off, or there is no canonical Outreach PoCs tab."
                    ),
                }
            tab = result.get("tab")
            matched = activation.matching_rows(
                getattr(tab, "rows", []) or [], org=company, names=[poc] if poc else None
            )
            if not matched:
                return {
                    "ok": False, "company": company,
                    "reason": (
                        f"No row on {getattr(tab, 'title', 'the tab')!r} matches "
                        f"{company!r}" + (f" / {poc!r}" if poc else "")
                        + ". Say so rather than guessing at a different spelling."
                    ),
                }

            active_keys = {a["row_key"] for a in (result.get("actions") or [])}
            by_key = {a["row_key"]: a for a in (result.get("actions") or [])}
            snoozes = await asyncio.to_thread(self.db.snoozes)
            scheduled = await asyncio.to_thread(self.db.scheduled_reminders_by_row)

            out: list = []
            for row in matched:
                key = activation.row_key(row)
                if key in active_keys:
                    a = by_key[key]
                    out.append({
                        "company": a["company"], "poc": a["poc"],
                        "action": a["type"], "label": a["label"],
                        "owner": a["owner"] or "(unassigned)",
                        "due_date": a["due_iso"], "overdue_days": a["overdue_days"],
                        "priority": a["priority"], "priority_label": a["priority_label"],
                        "text": a["text"], "why": a["why"], "sheet_row": a["sheet_row"],
                    })
                    continue
                # No action: say WHICH kind of nothing this is.
                if not activation.is_active(row):
                    reason, detail = "inactive", activation.why_active(row)
                else:
                    # The two gates, asked directly. There is no longer a
                    # single "next action for this row" to ask for — a row can
                    # be selected by several rules, or by none — so the honest
                    # question is why it is SILENT, which is what the gates
                    # answer.
                    _ok, silent, _until = nextaction.row_gate(
                        row, today=result.get("today") or dl.today_ist(),
                        snoozes=snoozes,
                    )
                    silent = silent or nextaction.SILENT_NOT_DUE
                    reason = silent
                    detail = nextaction.SILENT_LABELS.get(silent, silent)
                    if silent == nextaction.SILENT_STOPPED:
                        detail = nextaction.stop_reason(row) + " — never chased again"
                    elif silent == nextaction.SILENT_SNOOZED:
                        entry = snoozes.get(activation.row_key(row)) or {}
                        detail = (
                            f"snoozed until {entry.get('until_date')}"
                            + (f" ({entry.get('note')})" if entry.get("note") else "")
                        )
                out.append({
                    "company": gtm_sheet.clean_cell(row.get("company")),
                    "poc": gtm_sheet.clean_cell(row.get("poc")),
                    "action": None, "no_action_because": reason, "detail": detail,
                    "sheet_row": row.get("_row"),
                })
            return {
                "ok": True,
                "sent_anything": False,
                "tab": getattr(tab, "title", ""),
                "company": company,
                "rows": out,
                "note": (
                    "Exactly one action per row, or a specific reason for none. "
                    "'stopped' means the row is finished and will never be chased again; "
                    "'snoozed' means somebody asked me to wait; 'not_due' means the "
                    "cadence has not come round yet. Those are three different answers."
                ),
            }

        async def _snooze_row(inp: dict) -> dict:
            """Snooze a row until a date. WRITES to SQLite, never to the sheet."""
            company = str((inp or {}).get("company") or "").strip()
            poc = str((inp or {}).get("poc") or "").strip()
            note = str((inp or {}).get("note") or "").strip()
            if not company:
                return {"ok": False, "reason": "snooze_row needs a 'company'."}

            today = dl.today_ist()
            until = None
            raw_date = str((inp or {}).get("date") or "").strip()
            if raw_date:
                until = dl.parse_date(raw_date)
                if until is None:
                    return {
                        "ok": False,
                        "reason": f"I could not read {raw_date!r} as a date. Use YYYY-MM-DD.",
                    }
            else:
                try:
                    days = int((inp or {}).get("days") or 0)
                except (TypeError, ValueError):
                    days = 0
                if days < 1:
                    return {
                        "ok": False,
                        "reason": "Tell me how long: either 'days' or an explicit 'date'.",
                    }
                until = today + timedelta(days=days)

            tab, err = await asyncio.to_thread(_tab_or_error, gtm_sheet.POCS)
            if err:
                return err
            matched = activation.matching_rows(
                tab.rows, org=company, names=[poc] if poc else None
            )
            if not matched:
                return {
                    "ok": False, "company": company,
                    "reason": (
                        f"No row on {tab.title!r} matches {company!r}"
                        + (f" / {poc!r}" if poc else "")
                        + ". Nothing was snoozed."
                    ),
                }

            snoozed: list = []
            for row in matched:
                result = await asyncio.to_thread(
                    lambda r=row: self.db.set_snooze(
                        row_key=activation.row_key(r),
                        company=gtm_sheet.clean_cell(r.get("company")),
                        poc=gtm_sheet.clean_cell(r.get("poc")),
                        until_date=dl.iso(until), note=note,
                        on_date=dl.iso(today),
                    )
                )
                snoozed.append({
                    "company": gtm_sheet.clean_cell(row.get("company")),
                    "poc": gtm_sheet.clean_cell(row.get("poc")),
                    "action": result["action"], "previous_date": result["previous"],
                    "sheet_row": row.get("_row"),
                })
            state.audit(
                "rows_snoozed", reason=note or "no reason given",
                company=company, poc=poc, until=dl.iso(until), rows=len(snoozed),
            )
            log.info("[nextaction] snoozed %d row(s) at %r until %s",
                     len(snoozed), company, dl.iso(until))
            return {
                "ok": True,
                "sent_anything": False,
                "until": dl.iso(until),
                "until_pretty": dl.format_date(until),
                "rows": snoozed,
                "note": (
                    "Those rows produce no action until that date. On and after it, the "
                    "action comes back with its due date set to the date that was asked "
                    "for — so if the date passes it shows as overdue rather than "
                    "disappearing. Nothing was written to the spreadsheet."
                ),
            }

        async def _schedule_reminder(inp: dict) -> dict:
            """A one-off reminder at a specific time. The weekend exception."""
            company = str((inp or {}).get("company") or "").strip()
            poc = str((inp or {}).get("poc") or "").strip()
            what = str((inp or {}).get("what") or "").strip()
            when = str((inp or {}).get("time") or "").strip()
            raw_date = str((inp or {}).get("date") or "").strip()
            if not company or not what or not raw_date:
                return {
                    "ok": False,
                    "reason": "schedule_reminder needs a 'company', a 'date' and a 'what'.",
                }
            due = dl.parse_date(raw_date)
            if due is None:
                return {
                    "ok": False,
                    "reason": f"I could not read {raw_date!r} as a date. Use YYYY-MM-DD.",
                }

            tab, err = await asyncio.to_thread(_tab_or_error, gtm_sheet.POCS)
            if err:
                return err
            matched = activation.matching_rows(
                tab.rows, org=company, names=[poc] if poc else None
            )
            row = matched[0] if matched else None
            row_key = activation.row_key(row) if row is not None else ""
            today = dl.today_ist()
            reminder_id = await asyncio.to_thread(
                lambda: self.db.add_scheduled_reminder(
                    row_key=row_key,
                    company=(gtm_sheet.clean_cell(row.get("company")) if row is not None
                             else company),
                    poc=(gtm_sheet.clean_cell(row.get("poc")) if row is not None else poc),
                    due_date=dl.iso(due), due_time=when, what=what,
                    on_date=dl.iso(today),
                )
            )
            state.audit(
                "reminder_scheduled", reason=what, company=company, poc=poc,
                due_date=dl.iso(due), due_time=when, matched_a_row=bool(row),
            )
            weekend = due.weekday() >= 5
            return {
                "ok": True,
                "sent_anything": False,
                "id": reminder_id,
                "company": company, "poc": poc,
                "due_date": dl.iso(due), "due_pretty": dl.format_date(due),
                "time": when, "what": what,
                "matched_sheet_row": row.get("_row") if row is not None else None,
                "is_weekend": weekend,
                "note": (
                    ("That is a Saturday/Sunday and I have LEFT IT THERE — scheduled "
                     "reminders keep the date they were asked for; every other date I "
                     "compute is moved to the Monday. " if weekend else "")
                    + "It appears in 'cadence preview' on the day. I do not send it on my "
                    "own, and nothing was written to the spreadsheet."
                    + ("" if row is not None else
                       " I could not match that to a row on the tab, so it is recorded "
                       "against the company name alone — say so.")
                ),
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
                    "name": "sheet_status",
                    "description": (
                        "WHAT I AM ACTUALLY READING. Use this for 'sheet status', 'what "
                        "sheet are you on', 'which tab', 'which tabs can you see', 'how "
                        "many rows can you see', 'what can you write', 'why aren't you "
                        "chasing X'. Returns EVERY tab discovered in the playbook with "
                        "its row count and what I read it as (`tabs_found`), the tabs I "
                        "do NOT read (`tabs_not_read`), then for the canonical tab: "
                        "total rows, ACTIVE rows (a row is active only when a "
                        "first-contact date or a LinkedIn connected date is present — "
                        "inactive rows are invisible to everything I say unprompted, "
                        "though I will still answer about one by name), the restricted "
                        "column bands I may never write to, and the NAMED columns inside "
                        "the writable window between them. LIST THE TABS: name each one "
                        "with its row count, and say plainly which ones I am not "
                        "reading. Quote the canonical tab name and both counts; if "
                        "active is far below total, say so — that is usually the answer "
                        "to 'why is the digest so quiet'."
                    ),
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                },
                "handler": _sheet_status,
            },
            {
                "schema": {
                    "name": "activate_rows",
                    "description": (
                        "ACTIVATE rows that have no first-contact or connection date, so "
                        "they become visible to the proactive features. This is the ONE "
                        "exception to the activation rule and it exists for exactly this "
                        "request: 'set connection reminders for the others at <org>', "
                        "'start chasing the rest of the people at <org>', 'include "
                        "<person> at <org>'. Pass 'org' always; pass 'names' to activate "
                        "particular PoCs rather than every row at that org. The "
                        "activation is REMEMBERED across restarts. Report back exactly "
                        "which rows were activated, which were already active and why, "
                        "and name anything asked for that does not exist rather than "
                        "implying it was activated."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "org": {
                                "type": "string",
                                "description": "The company / organisation named in the request.",
                            },
                            "names": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Optional PoC names. Omit for 'the others at <org>', "
                                    "which means every row at that org."
                                ),
                            },
                            "reason": {
                                "type": "string",
                                "description": "The request in the asker's own words, for the record.",
                            },
                        },
                        "required": ["org"],
                    },
                },
                "handler": _activate_rows,
            },
            {
                "schema": {
                    "name": "research_brief",
                    "description": (
                        "A RESEARCH BRIEF on ONE person, as COPY MATERIAL for whoever "
                        "asked. Use for 'brief me on <person>', 'brief me on <person> "
                        "(<org>)', 'what should I say to <person>', 'who is <person> and "
                        "what do we open with'. Returns who they are, how much their role "
                        "weighs (someone who can say yes vs someone who has to ask), "
                        "which of their work maps to our lanes, an angle, and a DRAFT "
                        "message. "
                        "IT IS NEVER SENT AND NEVER WRITTEN TO THE SHEET — hand it back "
                        "as a draft for them to edit. "
                        "It fetches ONLY the URLs already on that person's row, and only "
                        "from the allowed domains; if it refused any link, REPEAT the "
                        "refusal line verbatim so nobody reads a partial brief as a "
                        "complete one. If it says LinkedIn access is pending, SAY THAT — "
                        "a career history quietly missing reads as 'they have none'."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "person": {"type": "string", "description": "The PoC's name."},
                            "org": {"type": "string", "description": "Their company, if they said one."},
                        },
                        "required": ["person"],
                    },
                },
                "handler": _research_brief,
            },
            {
                "schema": {
                    "name": "cadence_preview",
                    "description": (
                        "TODAY'S DUE ITEMS FROM THE TWELVE RULES, grouped by rule. Use "
                        "this for 'cadence preview', 'rules preview', 'what's the queue', "
                        "'what needs doing today', 'what needs attention', 'what's "
                        "slipping', 'anything urgent', 'what would you chase'. NOTHING IS "
                        "SENT by this: it is a read-only preview. ANSWER GROUPED BY RULE "
                        "— name each rule ('R5 Prospects to contact: 5 items') and give "
                        "each line's reason, which names the cells that produced it, so "
                        "the team can check the output against bot_rules.yaml. Quote "
                        "'rules_run' for the rules that did NOT run today and why — 'R4 "
                        "found nothing' and 'R4 does not run on a Thursday' look the same "
                        "in a list of what fired and only one is worth chasing. Say which "
                        "items are 'web_pending' (rules needing web research that does "
                        "not exist yet) and name anything in 'deduped' — one contact is "
                        "named at most once a day, so a later rule can lose to an earlier "
                        "one. Also quote the counts for rows that produced NOTHING "
                        "(stopped, snoozed, nothing due)."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "description": (
                                    "Optional: one rule's trigger to filter to — "
                                    "ai_news, news_company_screen, events, deliverables, "
                                    "prospects, li_no_dm, dm_no_meeting, meeting_prep, "
                                    "meeting_followup, closure_support, "
                                    "new_pipeline_company, sales_packages."
                                ),
                            },
                            "owner": {
                                "type": "string",
                                "description": "Optional: one owner, as the sheet names them.",
                            },
                        },
                        "required": [],
                    },
                },
                "handler": _cadence_preview,
            },
            {
                "schema": {
                    "name": "next_action",
                    "description": (
                        "THE ONE NEXT ACTION for a named company (or one PoC at it). Use "
                        "for 'what's next for X', 'what should I do about X', 'why aren't "
                        "you chasing X', 'when is X due'. Returns exactly one action per "
                        "row — type, owner, due date, priority band — or, when a row "
                        "produces none, WHY: stopped (0% / Dead / Unresponsive / Won / "
                        "Lost, and never chased again), snoozed until a date, no readable "
                        "date to count from, or simply nothing due yet. Those four are "
                        "different answers and you must give the specific one rather than "
                        "'nothing'."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string", "description": "Company name; partial is fine."},
                            "poc": {"type": "string", "description": "Optional PoC name to narrow to one row."},
                        },
                        "required": ["company"],
                    },
                },
                "handler": _next_action_for,
            },
            {
                "schema": {
                    "name": "snooze_row",
                    "description": (
                        "SNOOZE a row: 'follow up in 5 days', 'come back to Acme on the "
                        "20th', 'leave them alone until next month'. The row produces NO "
                        "action until that date; on and after it, the row's action comes "
                        "back with its due date set to the date that was asked for. Pass "
                        "either 'days' or 'date' (YYYY-MM-DD). This WRITES to my own "
                        "records — never to the spreadsheet. Report the date back so the "
                        "person can correct it."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string", "description": "Company name; partial is fine."},
                            "poc": {"type": "string", "description": "Optional PoC, to snooze one row rather than all of them at that company."},
                            "days": {"type": "integer", "description": "Calendar days from today."},
                            "date": {"type": "string", "description": "An explicit date, YYYY-MM-DD."},
                            "note": {"type": "string", "description": "The request in the asker's own words."},
                        },
                        "required": ["company"],
                    },
                },
                "handler": _snooze_row,
            },
            {
                "schema": {
                    "name": "schedule_reminder",
                    "description": (
                        "A ONE-OFF REMINDER at a specific time: 'remind me about Acme on "
                        "Saturday morning', 'ping me about the Beta quote on the 14th at "
                        "10'. Unlike every other date I compute, THIS ONE IS NEVER MOVED "
                        "OFF A WEEKEND — somebody asking for a Saturday has decided about "
                        "their own Saturday. It takes precedence over the row's ordinary "
                        "follow-up but not over a positive reply. This WRITES to my own "
                        "records, never to the spreadsheet, and it does not send anything "
                        "on its own — it appears in 'cadence preview' on the day."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string", "description": "Company the reminder is about."},
                            "poc": {"type": "string", "description": "Optional PoC."},
                            "date": {"type": "string", "description": "The date asked for, YYYY-MM-DD."},
                            "time": {"type": "string", "description": "Free text as asked: 'morning', '10:00'."},
                            "what": {"type": "string", "description": "What to be reminded about, in their words."},
                        },
                        "required": ["company", "date", "what"],
                    },
                },
                "handler": _schedule_reminder,
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
                tab = await asyncio.to_thread(gtm_sheet.SHEETS.tab, gtm_sheet.POCS)
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
                tab = gtm_sheet.SHEETS.tab(gtm_sheet.POCS)
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

        # THE CONFLICT RULE, now read off the row's own MEETING DATE rather than
        # off a bot-owned column. The "Next Deadline (bot)" column is retired
        # with the sandbox era, so there is no cell that is "the bot's date" to
        # compare against — but a date a person typed still wins, and the place
        # they type one is the sheet's own date columns.
        if row is not None:
            existing_cell = (row.get("meeting_date") or "").strip()
            if existing_cell:
                human_date = dl.parse_date(existing_cell)
                if human_date and kind == dl.KIND_MEETING_PREP:
                    self.db.upsert_deadline(
                        company=company, kind=kind, due_date=dl.iso(human_date),
                        rule="taken from the meeting date in the sheet", source="human",
                        sheet_row=sheet_row_no, sheet_target="original",
                        channel_id=getattr(channel, "id", None),
                    )
                    log.info(
                        "[deadline] %s has a meeting date in the sheet (%s); adopting it",
                        company, dl.iso(human_date),
                    )
                    state.audit(
                        "deadline_adopted_human",
                        reason="a person had already entered the date in the sheet; theirs wins",
                        company=company, kind=kind, due_date=dl.iso(human_date),
                    )
                    return {
                        "action": "kept_human", "company": company, "kind": kind,
                        "due_date": dl.iso(human_date),
                        "note": "A person had already set this date in the sheet. Theirs wins; I adopted it.",
                    }

        result = self.db.upsert_deadline(
            company=company, kind=kind, due_date=dl.iso(due), rule=rule, source="bot",
            sheet_row=sheet_row_no, sheet_target="sqlite",
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

        # NO SHEET MIRROR. The deadline lives in SQLite and is announced here;
        # there is no bot-owned column to copy it into any more, and inventing a
        # place for it inside the team's own writable window would be the bot
        # deciding which of THEIR columns means "the bot's deadline".
        #
        # Writes into that window happen only through the two triggers in
        # sheetwrite.py — a reply to the bot, or an explicit command — because
        # those are the two cases where a human has actually said what should be
        # in the cell.

        # Announce. This is the consent mechanism, so it happens whenever a date
        # was actually set — even if the sheet write failed.
        label = dl.KINDS.get(kind, {}).get("label", "the next step")
        text = dl.announcement(
            thing=f"the {label}", company=company, due=due, rule=rule,
            mentions=dl.notify_mentions(), unprompted=unprompted,
        )
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

        The ONLY thing a tick can put into Discord is a DRIP MESSAGE, and at
        most DAILY_MESSAGE_CAP of those a weekday. Every other proactive path —
        the daily digest, the deadline reminder, the deadline chase, the promise
        nudge, the give-up flag, the row-hygiene flag, the weekly funnel post —
        was removed, not disabled behind a knob, so there is nothing here that
        could start speaking again by accident.

        Guarded so a failing drip never stops the state summary being written.
        """
        try:
            await self._maybe_send_drip()
        except Exception:
            log.exception("[drip] tick raised; continuing")
        self._maybe_write_daily_summary()

    # ======================================================================
    # WRITING TO THE SHEET
    # ======================================================================
    # THE ONLY PLACE A CELL CHANGES. Two triggers, and nothing else in this file
    # can reach `gtm_sheet.write_cells`:
    #
    #   (a) a team member REPLIES to one of the bot's own messages;
    #   (b) a team member @-mentions the bot with an explicit command.
    #
    # There is no scheduled write, no write from the sweep, and no inference
    # from a passing remark. A cell changes because a person addressed the bot
    # and said something that answers what that cell holds.
    #
    # The tiers, the fill rule, the restricted bands and the terminal-word gate
    # are all in sheetwrite.py, which is pure — this method does the I/O and the
    # talking, and it applies whatever that module decided, never more.

    async def _maybe_apply_sheet_update(
        self, message: discord.Message, text: str
    ) -> bool:
        """Try to treat this message as an update / snooze / undo.

        Returns True when it was handled (and answered). False means "this is
        not a write" and the caller carries on to the question engine — which is
        the common case, because most things said to the bot are questions.
        """
        # A FOCUS COMMAND, BEFORE ANYTHING ELSE. "Prioritise only AI Voice
        # Agents" is not an update and must not be fed to the extractor, which
        # would cheerfully read "AI Voice Agents" as a company and propose
        # something nobody asked for.
        if await self._maybe_focus_command(message, text):
            return True

        # AN ANSWER TO A PROPOSAL. Checked before the extractor for the same
        # reason: "yes" is not an update, and a bare "no" fed to an extractor is
        # an invitation to invent one.
        if await self._maybe_vote_on_proposal(message, text):
            return True

        is_reply = await self._is_reply_to_self(message)
        trigger = sheetwrite.TRIGGER_REPLY if is_reply else sheetwrite.TRIGGER_COMMAND

        # CONTEXT FROM THE MESSAGE THEY REPLIED TO. "Sent this morning" names no
        # company and no column; the nudge it answers names both. Without this
        # the extractor would have to guess, and guessing is how a correct value
        # lands in the wrong row.
        context = await self._drip_context_for(message) if is_reply else {}

        try:
            parsed = await self.llm.extract_sheet_update(
                text=text,
                today=dl.iso(dl.today_ist()),
                company_hint=context.get("companies", ""),
                poc_hint=context.get("poc", ""),
                asked_about=context.get("asked_about", ""),
                requester=_display(message.author),
            )
        except Exception:
            log.exception("[sheetwrite] extraction raised; treating as not an update")
            return False
        # ACCEPTING A RECORD-OFFER. "Yes" carries no fields, so the extractor
        # correctly reports nothing — but the message it replies to proposed
        # something specific, and that proposal is on the drip row. This is the
        # only place a bare affirmative can produce a write, and only ever the
        # write that was already shown to them in the offer.
        if is_reply and context.get("offer") and sheetwrite.is_affirmative(text):
            return await self._apply_pending_offer(message, context, text)

        if parsed is None or parsed["intent"] == "none":
            return False

        if parsed["intent"] == "undo":
            await self._undo_last_sheet_write(message)
            return True

        if parsed["intent"] == "snooze":
            return await self._apply_snooze(message, text, parsed, context)

        return await self._apply_sheet_update(message, text, parsed, context, trigger)

    async def _drip_context_for(self, message: discord.Message) -> dict:
        """What the message being replied to was about.

        Looks the parent Discord message id up in `drip_sends`, which records
        the companies and the action type of every drip message. A reply to
        something else the bot said (an answer, an echo) simply has no context
        and the extractor works from the reply alone.
        """
        ref = message.reference
        parent_id = getattr(ref, "message_id", None) if ref else None
        if not parent_id:
            return {}
        try:
            row = await asyncio.to_thread(self.db.find_drip_by_message_id, str(parent_id))
        except Exception:
            log.debug("[sheetwrite] could not look up the drip context", exc_info=True)
            return {}
        if not row:
            return {}
        return {
            "companies": str(row.get("companies") or ""),
            "asked_about": str(row.get("action_type") or ""),
            "poc": "",
            "offer": str(row.get("offer") or ""),
        }

    async def _maybe_vote_on_proposal(
        self, message: discord.Message, text: str
    ) -> bool:
        """Is this message a yes or a no to a proposal? Handle it if so.

        FINDS THE PROPOSAL TWO WAYS, in order of confidence: the message it
        REPLIES to, then the newest open one. A reply is unambiguous; a bare
        "yes" in the channel is a guess, and the echo names what was applied so
        a wrong guess is visible at once rather than silent.

        A NON-APPROVER GETS A POLITE NO AND THE PROPOSAL STAYS OPEN. They were
        trying to help; the answer is that this particular thing needs Sid or
        Vaishnavi, not that they did something wrong.
        """
        vote = approvals.read_vote(text)
        if not vote:
            return False

        proposal = None
        ref = getattr(getattr(message, "reference", None), "message_id", None)
        if ref:
            proposal = await asyncio.to_thread(
                self.db.open_proposal_for_message, str(ref)
            )
        if proposal is None:
            proposal = await asyncio.to_thread(self.db.latest_open_proposal)
        if proposal is None:
            return False

        author = _display(message.author)
        if not config.is_approver(getattr(message.author, "id", 0)):
            await self._reply(
                message, approvals.not_an_approver_reply(author),
                reason="a non-approver answered a proposal",
            )
            state.audit(
                "proposal_vote_refused",
                reason="only SALES_APPROVER_IDS may approve a write",
                proposal_key=proposal["proposal_key"], voter=author, vote=vote,
            )
            return True

        now = dl.now_ist().isoformat(timespec="seconds")
        await asyncio.to_thread(
            lambda: self.db.record_vote(
                proposal_key=proposal["proposal_key"],
                voter_id=int(getattr(message.author, "id", 0) or 0),
                voter_label=author, vote=vote, voted_at=now,
                message_id=str(message.id),
            )
        )
        fresh = await asyncio.to_thread(self.db.proposal, proposal["proposal_key"])
        decision, why, decided_by = approvals.decide((fresh or {}).get("votes") or [])
        state.audit(
            "proposal_vote",
            reason=why, proposal_key=proposal["proposal_key"],
            voter=author, vote=vote, decision=decision,
        )

        if decision == approvals.WAIT:
            await self._reply(message, "Noted — holding until someone can approve it.",
                              reason="vote recorded, still waiting")
            return True

        await asyncio.to_thread(
            lambda: self.db.close_proposal(
                proposal_key=proposal["proposal_key"],
                status="applied" if decision == approvals.APPLY else "declined",
                decision=why, decided_by=decided_by or author, decided_at=now,
            )
        )

        if decision == approvals.DECLINE:
            # A REVERSAL IS SAID OUT LOUD. If somebody else had already said
            # yes, the person who said it needs to know it did not happen —
            # silence here would leave them believing the sheet had changed.
            others = [
                v for v in ((fresh or {}).get("votes") or [])
                if v.get("vote") == approvals.VOTE_YES
                and str(v.get("voter_label")) != str(decided_by)
            ]
            line = f"Leaving that one then — {why}. Nothing has changed in the sheet."
            if others:
                line = (
                    f"Not doing that one: {why}. "
                    f"({', '.join(str(o.get('voter_label')) for o in others)} had said "
                    "yes, so to be clear — the sheet is unchanged.)"
                )
            await self._reply(message, line, reason="a proposal was declined")
            log.info("[approvals] %s DECLINED — %s",
                     proposal["proposal_key"], why)
            return True

        await self._apply_approved_write(
            message, fresh or proposal, decided_by=decided_by or author, why=why,
        )
        return True

    async def _maybe_focus_command(
        self, message: discord.Message, text: str
    ) -> bool:
        """Set, show or clear the prospecting focus. Only approvers may set it."""
        parsed = focus.parse(text)
        if not parsed:
            return False

        today = dl.today_ist()
        author = _display(message.author)

        if parsed["action"] == focus.SHOW:
            live = await asyncio.to_thread(self.db.active_focus, today=dl.iso(today))
            await self._reply(message, focus.describe(live, today=today),
                              reason="showing the current focus")
            return True

        if not config.is_approver(getattr(message.author, "id", 0)):
            await self._reply(message, focus.not_allowed_reply(author),
                              reason="a non-approver tried to change the focus")
            state.audit(
                "focus_refused",
                reason="only SALES_APPROVER_IDS may set or clear the focus",
                who=author, command=parsed["raw"],
            )
            return True

        if parsed["action"] == focus.CLEAR:
            cleared = await asyncio.to_thread(
                lambda: self.db.clear_focus(on_date=dl.iso(today), by=author)
            )
            if cleared:
                await self._reply(
                    message,
                    f"Cleared the focus on {cleared.get('value')} — back to sheet order.",
                    reason="focus cleared",
                )
                state.audit("focus_cleared", reason=f"cleared by {author}",
                            value=cleared.get("value"), who=author)
            else:
                await self._reply(message, "There was no focus set.",
                                  reason="nothing to clear")
            return True

        ends = focus.expiry(on=today, days=parsed["days"])
        await asyncio.to_thread(
            lambda: self.db.set_focus(
                field="", value=parsed["value"], raw=parsed["raw"], set_by=author,
                set_by_id=int(getattr(message.author, "id", 0) or 0),
                set_on=dl.iso(today), expires_on=dl.iso(ends),
            )
        )
        await self._reply(
            message,
            focus.confirmation(parsed["value"], days=parsed["days"], ends=ends),
            reason="focus set",
        )
        state.audit(
            "focus_set", reason=f"set by {author}", value=parsed["value"],
            days=parsed["days"], expires_on=dl.iso(ends), who=author,
        )
        log.info("[focus] %s set a focus on %r until %s",
                 author, parsed["value"], dl.iso(ends))
        return True

    async def _apply_pending_offer(
        self, message: discord.Message, context: dict, text: str
    ) -> bool:
        """Apply the record-offer this reply is saying yes to.

        THE OFFER IS THE PROPOSAL, NOT THE REPLY. The bot writes exactly what it
        showed them and nothing else — the affirmative is consent to a specific
        change they have already read, which is the only way a one-word reply can
        safely produce a write at all.

        It goes through `plan_writes` like every other update, so the tiers, the
        bands, the fill rule and the ceiling all still apply. An offer cannot
        reach a cell an ordinary reply could not.
        """
        try:
            offer = json.loads(context.get("offer") or "{}")
        except (TypeError, ValueError):
            log.warning("[convert] the stored offer was unreadable; ignoring it")
            return False
        fields = offer.get("fields") or []
        if not fields:
            return False
        parsed = {
            "intent": "update", "company": offer.get("company", ""),
            "poc": "", "fields": fields, "confidence": 1.0,
        }
        log.info(
            "[convert] %s accepted the record-offer for %r",
            _display(message.author), offer.get("company", ""),
        )
        state.audit(
            "offer_accepted",
            reason=f"{_display(message.author)} said yes to a record-offer",
            company=offer.get("company", ""),
            fields=[f.get("role") for f in fields],
            reply=text[:200],
        )
        return await self._apply_sheet_update(
            message, text, parsed, context, sheetwrite.TRIGGER_REPLY,
        )

    async def _resolve_write_row(self, parsed: dict, context: dict):
        """(tab, row) for the row an update is about, or (None, reason).

        THE COMPANY MUST RESOLVE TO EXACTLY ONE ROW. Two rows at the same
        company is normal — the tab holds one row per PoC — so an update that
        names only a company and matches several is REFUSED with a question
        rather than applied to whichever came first. A correct value in the
        wrong person's row is worse than no value.
        """
        try:
            tab = await asyncio.to_thread(gtm_sheet.SHEETS.pocs_tab)
        except Exception:
            log.exception("[sheetwrite] the canonical tab could not be read")
            return None, "I could not read the sheet just now."
        if tab is None:
            return None, "I have no Outreach PoCs tab to write to."

        company = parsed.get("company") or ""
        poc = parsed.get("poc") or ""
        if not company and context.get("companies"):
            # A reply with no company named: the nudge it answers had exactly
            # one, or the reply is ambiguous and we say so.
            names = [c.strip() for c in context["companies"].split(",") if c.strip()]
            if len(names) == 1:
                company = names[0]
            elif names:
                return None, (
                    "That message was about " + context["companies"]
                    + " — which one do you mean?"
                )
        if not company:
            return None, "I could not tell which company you meant."

        matched = activation.matching_rows(
            tab.rows, org=company, names=[poc] if poc else None
        )
        if not matched:
            return None, f"I have no row for {company}" + (f" / {poc}" if poc else "") + "."
        if len(matched) > 1:
            people = ", ".join(
                gtm_sheet.clean_cell(r.get("poc")) or "(no name)" for r in matched[:6]
            )
            return None, (
                f"There are {len(matched)} rows for {company} ({people}) — "
                f"which person do you mean?"
            )
        return (tab, matched[0]), ""

    async def _apply_sheet_update(
        self, message: discord.Message, text: str, parsed: dict,
        context: dict, trigger: str,
    ) -> bool:
        """Plan, write, record and echo ONE update."""
        resolved, why = await self._resolve_write_row(parsed, context)
        if resolved is None:
            await self._reply(message, why, reason="could not resolve the row to update")
            return True
        tab, row = resolved

        plan = sheetwrite.plan_writes(
            tab=tab, row=row, fields=parsed["fields"], trigger=trigger, reply_text=text,
        )
        company = gtm_sheet.clean_cell(row.get("company"))
        poc = gtm_sheet.clean_cell(row.get("poc"))

        if not plan["writes"]:
            # Nothing writable. Still ANSWER — silence after somebody told the
            # bot something reads as the bot ignoring them.
            line = sheetwrite.echo_line(
                company=company, poc=poc, applied=[], asks=plan["asks"],
                skipped=plan["skipped"], undo_hours=config.SHEET_WRITE_UNDO_HOURS,
            )
            if plan["asks"] or plan["skipped"]:
                state.audit(
                    "sheet_write_declined",
                    reason="nothing in that message was mine to write",
                    company=company, poc=poc, trigger=trigger,
                    asks=plan["asks"], skipped=plan["skipped"],
                    requested_by=_display(message.author),
                )
                await self._reply(message, line or "Noted.", reason="update acknowledged")
                return True
            return False

        if not config.SHEET_WRITES_ENABLED:
            preview = sheetwrite.echo_line(
                company=company, poc=poc, applied=plan["applied"], asks=plan["asks"],
                skipped=plan["skipped"], undo_hours=config.SHEET_WRITE_UNDO_HOURS,
            )
            await self._reply(
                message,
                preview + " (Sheet writing is off right now, so I have not actually "
                          "changed anything.)",
                reason="sheet writes disabled",
            )
            return True

        # PERMISSION BEFORE EVERY WRITE. The plan is complete and nothing has
        # been written: the bot now SAYS what it would change and waits for a
        # yes from an approver. `_apply_approved_write` is the only path that
        # reaches `write_cells` from a reply.
        return await self._propose_write(
            message, plan=plan, tab=tab, row=row, company=company, poc=poc,
            trigger=trigger, reply_text=text,
        )

    async def _propose_write(self, message, *, plan: dict, tab, row: dict,
                             company: str, poc: str, trigger: str,
                             reply_text: str) -> bool:
        """Post the exact proposed change and record it. WRITES NOTHING.

        THE ORIGINAL REPLY TEXT IS STORED WITH IT, and that is not bookkeeping.
        `sheetwrite.said_terminal_words` is matched against what the HUMAN
        WROTE, deliberately — once the write waits behind a yes, "yes" is the
        message in hand and contains no terminal word at all. Re-deriving the
        plan from the approval would silently disarm the one gate that stops a
        row being marked Dead by inference.
        """
        proposed = approvals.proposal_text(
            company=company, poc=poc, applied=plan["applied"],
        )
        extra = ""
        if plan["asks"]:
            extra = " " + sheetwrite.echo_line(
                company=company, poc=poc, applied=[], asks=plan["asks"],
                skipped=[], undo_hours=config.SHEET_WRITE_UNDO_HOURS,
            )
        body = f"{proposed} ({approvals.who_can_approve()}.){extra}"

        sent = await self._reply(message, body, reason="proposing a sheet write")
        key = f"prop:{message.id}"
        opened = await asyncio.to_thread(
            lambda: self.db.open_proposal(
                proposal_key=key, kind="cell_update", tab=tab.title,
                sheet_row=int(row["_row"]), row_key=activation.row_key(row),
                company=company, poc=poc,
                payload={"writes": plan["writes"], "applied": plan["applied"],
                         "asks": plan["asks"], "skipped": plan["skipped"]},
                reply_text=reply_text, trigger=trigger, proposed_text=proposed,
                requested_by=_display(message.author),
                channel_id=int(getattr(message.channel, "id", 0) or 0),
                message_id=str(getattr(sent, "id", "") or message.id),
                created_at=dl.now_ist().isoformat(timespec="seconds"),
            )
        )
        state.audit(
            "write_proposed",
            reason="permission before every write: nothing is written until an "
                   "approver says yes",
            proposal_key=key, company=company, poc=poc, trigger=trigger,
            cells=plan["writes"], proposed=proposed,
            requested_by=_display(message.author), recorded=opened,
        )
        log.info(
            "[approvals] proposed %d cell(s) on %s (%s) — waiting for an approver",
            len(plan["writes"]), company, key,
        )
        return True

    async def _apply_approved_write(self, message, proposal: dict, *,
                                    decided_by: str, why: str) -> None:
        """Apply a proposal an approver said yes to. THE ONLY WRITE PATH."""
        payload = proposal.get("payload") or {}
        writes = payload.get("writes") or {}
        company = proposal.get("company") or ""
        poc = proposal.get("poc") or ""
        if not writes:
            await self._reply(message, "There was nothing left to write on that one.",
                              reason="approved proposal had no cells")
            return

        result = await asyncio.to_thread(
            lambda: gtm_sheet.SHEETS.write_cells(
                row=int(proposal.get("sheet_row") or 0), values=writes,
                expect_company=company,
                reason=f"approved by {decided_by}: {proposal.get('trigger') or 'reply'}",
            )
        )
        if not result["ok"]:
            await self._reply(
                message,
                f"I could not write that: {result['error'] or 'the sheet refused it'}. "
                f"Nothing has changed.",
                reason="sheet write failed",
            )
            state.audit(
                "sheet_write_failed", reason=result["error"], company=company,
                poc=poc, trigger=proposal.get("trigger") or "", values=writes,
                requested_by=proposal.get("requested_by") or "",
            )
            return

        batch_id = f"{message.id}"
        written_at = dl.now_ist().isoformat(timespec="seconds")
        await asyncio.to_thread(
            lambda: self.db.record_sheet_write(
                batch_id=batch_id, tab=proposal.get("tab") or "",
                sheet_row=int(proposal.get("sheet_row") or 0),
                row_key=proposal.get("row_key") or "", company=company, poc=poc,
                cells=result["written"], trigger=proposal.get("trigger") or "",
                requested_by=decided_by, source_msg=str(message.id),
                written_at=written_at,
            )
        )
        state.audit(
            "sheet_write",
            reason=f"approved by {decided_by} — {why}",
            batch_id=batch_id, tab=proposal.get("tab") or "",
            sheet_row=int(proposal.get("sheet_row") or 0),
            company=company, poc=poc, trigger=proposal.get("trigger") or "",
            proposal_key=proposal.get("proposal_key") or "",
            cells=[{"cell": c["cell"], "header": c["header"],
                    "old": c["old"], "new": c["new"]} for c in result["written"]],
            refused=(proposal.get("payload") or {}).get("skipped") or [],
            asks=(proposal.get("payload") or {}).get("asks") or [],
            requested_by=proposal.get("requested_by") or "",
            approved_by=decided_by,
        )

        applied = [
            a for a in (payload.get("applied") or [])
            if a.get("role") in {c["role"] for c in result["written"]}
        ]
        line = sheetwrite.echo_line(
            company=company, poc=poc, applied=applied,
            asks=payload.get("asks") or [], skipped=payload.get("skipped") or [],
            undo_hours=config.SHEET_WRITE_UNDO_HOURS,
        )
        await self._reply(message, line, reason="echoing an approved sheet write")
        log.info(
            "[approvals] %s approved %s — wrote %d cell(s) on row %s (%s)",
            decided_by, proposal.get("proposal_key"), len(result["written"]),
            proposal.get("sheet_row"), company,
        )

    async def _undo_last_sheet_write(self, message: discord.Message) -> None:
        """Revert the most recent write, if it is still inside the window.

        ANY TEAM MEMBER MAY UNDO ANY WRITE. Not just whoever caused it: the
        person who notices a wrong cell is usually not the person who typed the
        sentence that produced it, and making them find that person first is how
        a wrong value stays in the sheet all week.
        """
        now_iso = dl.now_ist().isoformat(timespec="seconds")
        try:
            batch = await asyncio.to_thread(
                lambda: self.db.latest_undoable_write(
                    within_hours=config.SHEET_WRITE_UNDO_HOURS, now_iso=now_iso,
                )
            )
        except Exception:
            log.exception("[sheetwrite] could not look up the last write")
            batch = None
        if not batch:
            await self._reply(
                message,
                f"I have not changed anything in the last "
                f"{config.SHEET_WRITE_UNDO_HOURS}h that I can put back.",
                reason="nothing to undo",
            )
            return

        cells = batch["cells"]
        first = cells[0]
        restore = [
            {"role": c["role"], "old": c["old_value"], "cell": c["cell"],
             "header": c["header"], "new": c["new_value"]}
            for c in cells
        ]
        result = await asyncio.to_thread(
            lambda: gtm_sheet.SHEETS.undo_cells(
                row=int(first["sheet_row"]), cells=restore,
                expect_company=str(first["company"]),
            )
        )
        if not result["ok"]:
            await self._reply(
                message,
                f"I could not put that back: {result['error'] or 'the sheet refused it'}. "
                f"The cells are as they were after my change.",
                reason="undo failed",
            )
            state.audit(
                "sheet_undo_failed", reason=result["error"],
                batch_id=batch["batch_id"], company=first["company"],
                requested_by=_display(message.author),
            )
            return

        await asyncio.to_thread(
            lambda: self.db.mark_sheet_write_undone(
                batch_id=batch["batch_id"], undone_by=_display(message.author),
                undone_at=now_iso,
            )
        )
        state.audit(
            "sheet_undo",
            reason=f"undo requested by {_display(message.author)}",
            batch_id=batch["batch_id"], tab=first["tab"],
            sheet_row=int(first["sheet_row"]), company=first["company"],
            poc=first["poc"],
            cells=[{"cell": c["cell"], "header": c["header"],
                    "restored_to": c["old_value"], "was": c["new_value"]}
                   for c in cells],
            age_hours=round(batch["age_hours"], 2),
            requested_by=_display(message.author),
        )
        what = " and ".join(
            f"{c['header'].lower()} back to {c['old_value'] or 'empty'}" for c in cells
        )
        await self._reply(
            message,
            f"Done — I put {first['company']}'s {what}.",
            reason="echoing an undo",
        )
        log.info(
            "[sheetwrite] UNDO by %s: %d cell(s) on row %s restored",
            _display(message.author), len(cells), first["sheet_row"],
        )

    async def _apply_snooze(
        self, message: discord.Message, text: str, parsed: dict, context: dict
    ) -> bool:
        """"follow up in 15 days" / "remind me Saturday 6pm about X".

        Parsed HERE rather than by the model: a date is a thing a regex can be
        held to, and a snooze quietly entered for the wrong day would make the
        bot go silent about an account for reasons nobody could reconstruct.
        """
        plan = sheetwrite.parse_snooze(text, today=dl.today_ist())
        if plan is None:
            return False
        resolved, why = await self._resolve_write_row(parsed, context)
        if resolved is None:
            await self._reply(message, why, reason="could not resolve the row to snooze")
            return True
        _tab, row = resolved
        company = gtm_sheet.clean_cell(row.get("company"))
        poc = gtm_sheet.clean_cell(row.get("poc"))
        today_iso = dl.iso(dl.today_ist())

        if plan["kind"] == "scheduled":
            await asyncio.to_thread(
                lambda: self.db.add_scheduled_reminder(
                    row_key=activation.row_key(row), company=company, poc=poc,
                    due_date=dl.iso(plan["date"]), due_time=plan["time"],
                    what=plan["about"] or text.strip()[:200],
                    requested_by=_display(message.author), on_date=today_iso,
                )
            )
            state.audit(
                "reminder_scheduled", reason=plan["quote"], company=company, poc=poc,
                due_date=dl.iso(plan["date"]), due_time=plan["time"],
                requested_by=_display(message.author),
            )
        else:
            await asyncio.to_thread(
                lambda: self.db.set_snooze(
                    row_key=activation.row_key(row), company=company, poc=poc,
                    until_date=dl.iso(plan["date"]), note=plan["quote"],
                    requested_by=_display(message.author), on_date=today_iso,
                )
            )
            state.audit(
                "rows_snoozed", reason=plan["quote"], company=company, poc=poc,
                until=dl.iso(plan["date"]), rows=1,
                requested_by=_display(message.author),
            )
        await self._reply(
            message,
            sheetwrite.snooze_confirmation(plan, company=company),
            reason="confirming a snooze",
        )
        return True

    # ======================================================================

    # ======================================================================
    # THE DRIP — the entire proactive voice of this bot
    # ======================================================================
    # THE ONE DAILY DIGEST IS RETIRED. What used to be one long message of
    # grouped sections at 10:00 is now at most DAILY_MESSAGE_CAP short messages
    # across a weekday, time-spaced, ONE PER (ACTION TYPE x OWNER).
    #
    # IF YOU ARE ADDING SOMETHING THE BOT SHOULD TELL THE TEAM: add an action
    # TYPE in nextaction.py. Do not add a `guardrails.send`. The value of this
    # design is entirely in the fact that there is exactly one send path and a
    # hard cap on how often it runs.
    #
    # THE KILL SWITCH IS UNCHANGED — same name, same semantics, same log line.
    # `SALES_DIGEST_ENABLED=false` (the current server state) suppresses all of
    # this. The drip inherits the switch; it does not get one of its own, because
    # an operator who turned the bot off should not have to learn a new variable
    # to keep it off.
    #
    # The exceptions, both of which are answers rather than interruptions and
    # neither of which is spaced or capped:
    #   - a REPLY to a question someone asked (`_reply`) — IMMEDIATE, always;
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

    # -- sending -----------------------------------------------------------

    async def _maybe_send_drip(self) -> None:
        """Send whichever drip slots are due, and no more than that.

        THE WHOLE DAY IS RE-PLANNED ON EVERY TICK, deterministically, and the
        slots already recorded in `drip_sends` fall away. That is the restart
        guard: a redeploy at 11:40 recomputes the identical schedule, sees slots
        1 and 2 in SQLite, and resumes at slot 3. It replaces the digest's
        single `sales_digest_date` marker, which only had to answer "did today's
        one message go out".

        THE KILL SWITCH LIVES HERE AND NOWHERE ELSE, exactly as it did for the
        digest: read live, checked after the cheap gates and before anything is
        composed or sent, logged once a day in the same words. Nothing is
        written when it is off, so there is no state to unwind to end the
        silence and no backlog to replay when it comes back.

        EMPTY QUEUE MEANS SILENCE. There is no "nothing to report" message and
        there must never be one.
        """
        now = dl.now_ist()
        today = now.date()
        marker = dl.iso(today)

        if not drip.is_sending_day(today):
            return

        hour, minute = config.drip_start_ist()
        if not digest.is_due(now, hour=hour, minute=minute):
            return

        # THE RESTART GUARD, read before anything else that costs money. Fails
        # CLOSED: if we cannot tell what has already gone out, we send nothing.
        # A duplicate nudge is worse than a missed one.
        try:
            already = self.db.drip_sent_today(marker)
        except Exception:
            log.exception(
                "[drip] could not read today's sent slots; staying quiet rather than "
                "risking a duplicate message"
            )
            return
        if len(already) >= max(0, config.DAILY_MESSAGE_CAP):
            return

        # THE SPACING HOLDS EVEN WHEN CATCHING UP. After a quiet morning — the
        # kill switch off until 14:00, a long outage, a clock jump — slots 1, 2
        # and 3 are all past their planned times at once. Sending "everything
        # that is due" would then put three messages into one hour on three
        # consecutive sweep ticks, which is precisely what the spacing exists to
        # prevent. So the gap is measured from the LAST ACTUAL SEND, not from
        # the planned time, and a backlog drains at the drip's own pace.
        floor = drip.min_gap_minutes()
        last_sent = self._last_drip_sent_at(already)
        if last_sent is not None:
            waited = (now - last_sent).total_seconds() / 60.0
            if waited < floor:
                return

        # THE KILL SWITCH. Same name, same live read, same line in the log as
        # the digest used — see config.digest_enabled().
        if not config.digest_enabled():
            if self._digest_suppressed_on != marker:
                self._digest_suppressed_on = marker
                log.info("[digest] suppressed — SALES_DIGEST_ENABLED=false")
            return

        planned = await self._plan_drip(today=today, already=already)
        if planned is None:
            return

        due = [m for m in planned["messages"] if m["send_at"] <= now]
        if not due:
            return

        # LIVE TEST MODE REDIRECTS THE REAL OUTPUT. Normal state, real timing,
        # real composition — only the audience changes. It is not a simulation
        # and does not pretend to be: slots are claimed, events are marked sent,
        # proposals are recorded. Point DB_PATH at a *_test.db first.
        channel_id = (
            config.test_channel_id() if config.SALES_TEST_MODE
            else config.digest_channel_id()
        )
        if config.SALES_TEST_MODE and not channel_id:
            log.error(
                "[test-mode] SALES_TEST_MODE=true but SALES_TEST_CHANNEL_ID is unset, "
                "so there is nowhere to redirect to. Staying quiet rather than posting "
                "test output into the real sales channel."
            )
            return
        channel = self.get_channel(channel_id) if channel_id else None
        if channel is None or not guardrails.may_read(channel_id):
            log.warning(
                "[drip] no reachable sales channel (%s) to send in", channel_id
            )
            return

        # THE PROPOSAL SWEEP RIDES THE FIRST SLOT, once a working day. It is
        # not a rule and does not take a slot — the day's messages are unchanged
        # by it — but it needs a moment somebody is already reading the channel,
        # and the first slot is that moment.
        #
        # BEFORE the message, not after: an approval the sweep prompts is worth
        # more the earlier it lands, and the two posts arriving together reads
        # as one glance rather than two interruptions.
        if due[0].get("slot") == 1 and self._swept_proposals_on != marker:
            self._swept_proposals_on = marker
            try:
                await self._sweep_proposals(today=today, channel=channel)
            except Exception:
                log.exception(
                    "[approvals] the proposal sweep failed; the day's messages are "
                    "unaffected"
                )

        # ONE MESSAGE PER TICK. The next slot is not due yet by construction —
        # the gap is at least MESSAGE_GAP_MIN_MINUTES and the sweeper ticks far
        # more often than that — but sending one and returning makes it
        # impossible for a backlog (a long outage, a clock jump) to arrive as a
        # burst.
        message = due[0]
        await self._send_drip_message(
            channel, message, marker=marker, channel_id=channel_id
        )

    @staticmethod
    def _last_drip_sent_at(already: list):
        """When the most recent drip message actually went out today, or None.

        Reads the recorded `sent_at`, not the planned time: the guard above is
        about how long ago the channel last heard from this bot, which is a fact
        about the clock rather than about the schedule.
        """
        best = None
        for row in already or []:
            raw = str((row or {}).get("sent_at") or "").strip()
            if not raw:
                continue
            try:
                when = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=dl.IST)
            if best is None or when > best:
                best = when
        return best

    async def _plan_drip(self, *, today, already: list):
        """Today's plan, or None when there is nothing to plan against.

        Pure up to this point: it reads the queue and the two SQLite clocks and
        hands them to `drip.plan`, which sends nothing.
        """
        queue = await self._run_next_actions(today=today)
        if queue is None:
            return None
        actions = list(queue.get("actions") or [])

        # EVENTS AND THE WEEKLY LINE RIDE THE SAME QUEUE. They are appended as
        # ordinary actions so the drip groups, ranks, spaces and caps them
        # exactly like everything else — a feature with its own send path would
        # be re-introducing the problem the drip exists to solve.
        # THE WEB HALF, before the drip groups and caps anything. An item that
        # got its research reads differently from one that did not, and the
        # composer needs to know which it is holding.
        try:
            actions = await self._research_items(actions, today=today)
        except Exception:
            log.exception(
                "[websearch] the research pass failed; the items go out with their "
                "placeholders rather than not going out"
            )

        actions.extend(await self._event_actions(today=today))
        funnel = await self._funnel_action(today=today)
        if funnel is not None:
            actions.append(funnel)

        if not actions and not already:
            # EMPTY QUEUE = SILENCE. Logged once so a quiet day is visibly a
            # quiet day rather than a broken one.
            log.info("[drip] nothing due for %s — no messages today", dl.iso(today))
            return None
        history = await asyncio.to_thread(self.db.drip_group_history)
        return await asyncio.to_thread(
            lambda: drip.plan(
                actions, day=today, history=history, already_sent=already
            )
        )

    async def _event_actions(self, *, today) -> list:
        """Events whose single T-minus reminder is due. [] when off or unreadable.

        The dedup lookup FAILS CLOSED (see `db.event_reminder_sent`): if the
        table cannot be read the event is treated as already reminded and stays
        quiet. This is a once-forever message, so a duplicate is the exact thing
        it exists to prevent and silence is the recoverable direction.
        """
        if not config.EVENTS_ENABLED:
            return []
        try:
            tab = await asyncio.to_thread(gtm_sheet.SHEETS.tab, gtm_sheet.EVENTS)
        except Exception:
            log.info("[events] no events tab to read", exc_info=True)
            return []
        if tab is None:
            return []
        try:
            return await asyncio.to_thread(
                lambda: events_mod.due_events(
                    tab.rows, today=today, already_sent=self.db.event_reminder_sent,
                )
            )
        except Exception:
            log.exception("[events] could not compute the due events")
            return []

    async def _funnel_action(self, *, today):
        """The one Friday numbers line, or None. Off by default."""
        if not config.WEEKLY_FUNNEL_ENABLED:
            return None
        rows, _tab = await self._active_rows_of_canonical_tab("the weekly funnel line")
        if not rows:
            return None
        try:
            counts = await asyncio.to_thread(
                lambda: tracker.funnel_metrics(
                    rows, since=today - timedelta(days=7), today=today
                )
            )
        except Exception:
            log.exception("[funnel] could not compute the weekly counts")
            return None
        return events_mod.funnel_action(counts, today=today)

    async def _convert_if_already_done(self, message: dict) -> Optional[dict]:
        """Has the nudged thing already happened? Returns the evidence, or None.

        SUPPRESS-OR-CONVERT, and the name matters: evidence does NOT silence the
        message, it changes what the message is. A suppressed nudge leaves the
        row wrong AND tells nobody, and a bot that goes quiet is
        indistinguishable from one that has broken.

        Both sources the answer path already uses are reused here — the synced
        meeting notes and the sales-channel history — rather than re-implemented.
        """
        if not config.SUPPRESS_OR_CONVERT_ENABLED:
            return None
        if not evidence.stage_of(message.get("type")):
            # A type with no notion of "already done" (mark unresponsive, a
            # parked deal's pulse) is never converted. Guessing there would
            # produce an offer to record something the bot invented.
            return None

        days = max(1, int(config.NOTES_LOOKBACK_DAYS))

        # ONE (company, poc) PAIR PER ROW in the group. The PoC matters as much
        # as the company: people write "meeting booked with Sahaj", not "meeting
        # booked with Sahaj Labs", and searching only on the sheet's full company
        # string is how the first version of this found nothing.
        seen: set = set()
        pairs: list = []
        for action in (message.get("actions") or []):
            key = (action.get("company", ""), action.get("poc", ""))
            if key[0] and key not in seen:
                seen.add(key)
                pairs.append(key)
        for company in (message.get("companies") or []):
            if company and not any(p[0] == company for p in pairs):
                pairs.append((company, ""))

        for company, poc in pairs[:6]:
            history: list = []
            try:
                found = await query.search_channel_history(
                    self, terms=evidence.identifiers(company, poc)[:4],
                    days_back=days, context_window=0,
                )
                history = found.get("matches") or []
            except Exception:
                log.info("[convert] channel search failed for %r", company, exc_info=True)

            try:
                hit = await asyncio.to_thread(
                    lambda c=company, p=poc, h=history: evidence.gather(
                        action_type=message["type"], company=c, poc=p,
                        notes_module=notes, history=h,
                    )
                )
            except Exception:
                log.exception("[convert] evidence gathering failed for %r", company)
                hit = None
            if hit:
                hit["company"] = company
                hit["poc"] = poc
                return hit
        return None

    async def _send_drip_message(self, channel, message: dict, *, marker: str,
                                 channel_id: int) -> None:
        """Compose ONE message and send it. The only proactive send in this bot.

        THE SLOT IS CLAIMED BEFORE THE SEND, not after. `record_drip_send` is
        guarded by UNIQUE (on_date, slot), so two ticks racing on the same slot
        cannot both get through — the loser stops here rather than sending a
        duplicate. The cost is that a failed send burns its slot for the day,
        which is the right way round: the drip speaks less on a bad day rather
        than more.
        """
        # IS THE OWNER OFF TODAY? Asked BEFORE the message is addressed, so a
        # nudge to somebody on holiday becomes a nudge to whoever is covering
        # rather than noise they come back to a week later.
        #
        # FAILS OPEN. An unreadable leave channel or a model outage resolves to
        # "everybody is in" and the owner is addressed as usual — the cost is
        # one nudge to somebody away, and the cost of failing the other way
        # would be silently redirecting all of the team's work to Vaishnavi
        # every time the API blinked.
        original_owner = str(message.get("owner") or "")
        leave_note = ""
        try:
            away = await leave.who_is_away(self, self.llm)
        except Exception:
            log.exception("[leave] the leave check failed; addressing the owner as usual")
            away = {}
        # THE TEST OVERRIDE, merged OVER the real read. "pretend Kushal is on
        # leave today" has to win, because the whole point is to see the cover
        # path without waiting for somebody to actually go away.
        override = simulation.leave_override()
        if override:
            away = {**away, **override}
            log.info("[leave] %d test override(s) applied: %s",
                     len(override), ", ".join(v["name"] for v in override.values()))
        if away and original_owner:
            covering, why = leave.address_to(original_owner, away)
            if covering and covering != original_owner:
                leave_note = why
                message = dict(message)
                message["owner"] = covering
                message["owner_key"] = gtm_sheet.normalise_header(covering)
                message["covering_for"] = original_owner
                log.info("[leave] slot %s: %s", message.get("slot"), why)
                state.audit(
                    "addressed_cover",
                    reason=why, date=marker, slot=message.get("slot"),
                    original_owner=original_owner, addressed=covering,
                )

        # HOW THE MESSAGE NAMES ITS OWNER is decided once, here, and threaded
        # into both composers. It used to be prefixed to whatever came back,
        # which produced "Vaishnavi Vaishnavi - ..." the moment the roster gate
        # fell back to a plain name — two things naming the same person, neither
        # aware of the other.
        address = self._drip_mention(message)

        # SUPPRESS-OR-CONVERT, immediately before the send and not before. The
        # queue is planned hours ahead; the evidence has to be as fresh as the
        # message, or the bot would chase something the team recorded at 11am
        # because it decided at 10.
        hit = None
        try:
            hit = await self._convert_if_already_done(message)
        except Exception:
            log.exception("[convert] the check failed; sending the nudge as a task")

        offer_json = ""
        if hit:
            body, used_model = evidence.offer_text(
                company=hit.get("company", ""), poc=hit.get("poc", ""),
                hit=hit, address=address,
            ), False
            fields = evidence.proposed_fields(hit)
            offer_json = json.dumps({
                "company": hit.get("company", ""), "fields": fields,
            })
            await asyncio.to_thread(
                lambda: self.db.record_conversion(
                    on_date=marker, group_key=message["group_key"],
                    action_type=message["type"], company=hit.get("company", ""),
                    owner_label=message.get("owner", ""), source=hit.get("source", ""),
                    evidence=hit.get("quote", ""), citation=hit.get("citation", ""),
                    offered=offer_json,
                )
            )
            state.audit(
                "nudge_converted",
                reason="evidence says this already happened; offering to record it",
                date=marker, slot=message["slot"], action_type=message["type"],
                company=hit.get("company", ""), source=hit.get("source", ""),
                evidence=hit.get("quote", ""), citation=hit.get("citation", ""),
                offered=[f["role"] for f in fields],
            )
            log.info(
                "[convert] slot %d: %s x %s CONVERTED to a record-offer — %s (%s)",
                message["slot"], message["type"], message.get("owner") or "-",
                hit.get("quote", "")[:120], hit.get("citation", ""),
            )
        else:
            fallback = drip.compose_fallback(message, address=address)
            body, used_model = fallback, False
            # THE LAST FEW OPENINGS, so the composer can be told what not to
            # start with. Read here rather than inside `llm` because it is a
            # database hit and that class does no I/O beyond the model call.
            openers = await asyncio.to_thread(self.db.recent_openers, 5)
            if self.llm is not None:
                try:
                    body, used_model = await self.llm.proactive_message(
                        prompt=drip.compose_prompt(message, address=address),
                        fallback=fallback,
                        recent_openers=openers,
                    )
                except Exception:
                    log.exception("[drip] composing failed; sending the template instead")
                    body, used_model = fallback, False

            # A REPEATED OPENING IS CAUGHT AFTER THE FACT TOO. The prompt asks
            # the composer not to reuse one; this notices when it did anyway.
            # NOT a rejection — the message is fine, it just opens the same way
            # as a recent one, and throwing away a good nudge over a turn of
            # phrase would cost more than the repetition does. It is logged so a
            # composer that ignores the rule is visible.
            if used_model:
                opened = tone.opener_of(body)
                if opened and opened in set(openers):
                    log.warning(
                        "[tone] slot %s reused the opening %r despite being told not "
                        "to. Sending it anyway — a repeated opening is worse than a "
                        "template, but not worse than no message.",
                        message.get("slot"), opened,
                    )

        # THE TAGS GO ON LAST AND IN ONE PLACE. The model composes the BODY and
        # is never asked to write a mention token: `guardrails.sanitize` strips
        # any it invents, so a model-written tag would vanish silently and the
        # message would go out addressed to nobody.
        if leave_note:
            body = f"{body}\n_({leave_note}.)_"
        body = drip.with_tags(
            body,
            owner_id=config.roster_id_for_name(message.get("owner") or ""),
            owner_name=message.get("owner") or "",
        )

        # IN TEST MODE A DM IS SHOWN, NOT SENT, exactly as in a simulation —
        # and the whole message carries the prefix so nothing in the test
        # channel can be mistaken for something the team received.
        if config.SALES_TEST_MODE:
            if str(message.get("destination") or "") in ("dm", "escalation"):
                body = simulation.dm_line(message.get("owner") or "the owner", body)
            body = simulation.prefix(body)

        claimed = await asyncio.to_thread(
            lambda: self.db.record_drip_send(
                on_date=marker, slot=int(message["slot"]),
                group_key=message["group_key"], action_type=message["type"],
                owner_key=message["owner_key"], owner_label=message["owner"],
                companies=", ".join(message["companies"]),
                stage=message["stage"], planned_at=message["send_at_hhmm"],
                channel_id=channel_id, message_id=None,
                sent_at=dl.now_ist().isoformat(timespec="seconds"),
            )
        )
        if not claimed:
            return

        sent = await guardrails.send(
            channel, body,
            reason=(f"drip slot {message['slot']} for {marker}: "
                    f"{message['type']} x {message['owner'] or 'the team'}"),
            kind="drip_message",
            extra={
                "date": marker, "slot": message["slot"], "type": message["type"],
                "owner": message["owner"], "stage": message["stage"],
                "companies": message["companies"], "composed_by_model": used_model,
            },
        )
        if sent is None:
            log.warning(
                "[drip] slot %d for %s was refused or failed to send. The slot is "
                "spent for today — the drip says less on a bad day rather than more.",
                message["slot"], marker,
            )
            return

        # THE OPENING, banked so the next few messages start differently.
        # Recorded AFTER the send, like every other ledger in this file: an
        # opening that never went out should not constrain the ones that do.
        try:
            await asyncio.to_thread(
                lambda: self.db.record_opener(
                    tone.opener_of(body), rule_id=message.get("rule_id", ""),
                    sent_at=dl.now_ist().isoformat(timespec="seconds"),
                )
            )
        except Exception:
            log.debug("[tone] could not bank the opening", exc_info=True)

        # THE MESSAGE ID, recorded now that there is one. A reply to this
        # message is one of only two things that may write to the sheet, and
        # this is what lets that reply find out which companies it was about.
        await asyncio.to_thread(
            lambda: self.db.attach_drip_message_id(
                on_date=marker, slot=int(message["slot"]), message_id=sent.id,
            )
        )
        if offer_json:
            # THE PENDING OFFER, so a reply of "yes" has something concrete to
            # apply. Without it the extractor would have to invent what "yes"
            # meant, which is the one thing it must never do about a write.
            await asyncio.to_thread(
                lambda: self.db.set_drip_offer(
                    on_date=marker, slot=int(message["slot"]), offer=offer_json,
                )
            )

        # AN EVENT'S ONE REMINDER IS RECORDED ONLY ONCE IT HAS ACTUALLY LANDED.
        # Recorded before the send, a refused message would burn the event's
        # single reminder forever — and "forever" is not a word to be careless
        # with.
        for action in message.get("actions") or []:
            key = action.get("event_key")
            if not key:
                continue
            await asyncio.to_thread(
                lambda a=action, k=key: self.db.record_event_reminder(
                    event_key=k, event=a.get("company", ""),
                    event_date=a.get("event_date", ""),
                    location=a.get("location", ""), sent_on=marker,
                )
            )

        state.audit(
            "drip_message",
            reason="one proactive message: one action type, one owner",
            date=marker, slot=message["slot"], channel_id=channel_id,
            message_id=sent.id, action_type=message["type"],
            owner=message["owner"] or "(unassigned)", stage=message["stage"],
            companies=message["companies"], planned_at=message["send_at_hhmm"],
            composed_by_model=used_model,
        )
        log.info(
            "[drip] SENT slot %d/%d at %s (planned %s) — %s x %s — %s [%s%s]",
            message["slot"], config.DAILY_MESSAGE_CAP,
            dl.now_ist().strftime("%H:%M"), message["send_at_hhmm"],
            message["type"], message["owner"] or "(unassigned)",
            ", ".join(message["companies"]), message["stage"],
            ", model" if used_model else ", template",
        )

    def _drip_mention(self, message: dict) -> str:
        """How a drip message addresses its owner.

        The roster gate decides whether that is a ping or a plain name; an
        unowned group falls back to the notify line rather than pinging nobody
        in particular. Deliberately at most ONE mention per message — the
        grouping rule already guarantees one owner, so a second mention would
        mean the grouping had failed.
        """
        mention, _key = self._cadence_owner({"owner": message.get("owner", "")})
        return mention or dl.notify_mentions()

    async def dry_run_drip(self, *, today) -> str:
        """Plan a day and RENDER it without sending. For `--dry-run-drip`.

        The replacement for `--dry-run-digest`, and it answers a better
        question: not "what would the one message say" but "how many messages,
        to whom, about what, at what times". It connects to nothing, sends
        nothing, and writes nothing.
        """
        planned = await self._plan_drip(today=today, already=[])
        if planned is None:
            return (
                "Nothing to send today. (An empty queue means silence — there is no "
                "'nothing to report' message.)"
            )
        report = drip.contract_report(planned)
        lines = [drip.preview_text(planned), "", "VOLUME CONTRACT (plan section 8)"]
        lines.append(
            f"  messages {report['messages']}/{report['cap']} "
            f"({'ok' if report['within_cap'] else 'OVER CAP'}) · "
            f"gaps {report['gaps_minutes']} min, floor {report['gap_floor_minutes']} "
            f"({'ok' if report['spacing_ok'] else 'TOO CLOSE'}) · "
            f"grouping {'ok' if report['grouping_ok'] else 'MIXED TYPES OR OWNERS'} · "
            f"{report['rolled']} rolling to tomorrow"
        )
        return "\n".join(lines)

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

    # -- the phase-1 cadence -----------------------------------------------

    def _cadence_owner(self, item: dict) -> tuple[str, str]:
        """(mention, owner_key) for one cadence item.

        THE TRACKER HAS NO OWNER COLUMN, so this resolves in two steps:
          1. the row's own owner cell, if a column ever appears or GTM_COLUMN_MAP
             names one. It holds a NAME, resolved against the roster;
          2. SALES_DEFAULT_OWNER_ID — Vaishnavi, who works every row today.

        A ping needs BOTH an id and roster membership: `guardrails.mention_for`
        is the roster gate, and it fails closed by naming the person in plain
        text instead. A row that resolves to nobody groups under "" and the
        renderer addresses it to the deadline-notify list.

        Sheet-health lines (the data-quality flags) are about the spreadsheet
        rather than about a prospect, so they are addressed to the default owner
        too — somebody has to fix the formula.
        """
        name = str(item.get("owner") or "").strip()
        if name:
            uid = config.roster_id_for_name(name)
            return guardrails.mention_for(uid, name), gtm_sheet.normalise_header(name)
        uid = int(config.SALES_DEFAULT_OWNER_ID or 0)
        if not uid:
            return "", ""
        display = (config.ROSTER_DISPLAY_NAMES or {}).get(str(uid)) or "the row owner"
        return guardrails.mention_for(uid, str(display)), f"uid:{uid}"

    def _cadence_stall_clock(self, today):
        """A stall_days(row) callable backed by the bot's own next-step history.

        The sheet has no history, so rule (i) reads the nextstep_state table:
        every row's Next Steps text is recorded each day, and the clock is how
        long the CURRENT text has read the same. A database error degrades to
        zero — rule (i) simply does not fire — rather than taking the digest down.
        """
        on_date = dl.iso(today)

        def stall_days(row: dict) -> int:
            try:
                key = self.db.nextstep_key(
                    str(row.get("company") or ""), str(row.get("poc") or "")
                )
                return self.db.track_next_steps(
                    row_key=key, text=str(row.get("next_steps") or ""), on_date=on_date
                )
            except Exception:
                log.debug("[cadence] next-step clock failed for a row", exc_info=True)
                return 0

        return stall_days

    def _cadence_mapping_lookup(self):
        """A mapping_lookup(company) for rule (g): alternative PoCs at an org.

        Names come back WITH their caveats folded into the string — a mapped
        researcher quoted without their staleness and departure checks is exactly
        the mistake that sheet's legend warns about, and a digest line has no room
        for a second sentence of qualification.
        """
        def lookup(company: str) -> list:
            try:
                rows = mapping_sheet.MAPPING.for_org(company) or []
            except Exception:
                log.debug("[cadence] mapping lookup failed for %r", company, exc_info=True)
                return []
            out = []
            for raw in rows[:3]:
                try:
                    person = mapping_sheet.MAPPING.enrich(raw)
                except Exception:
                    continue
                name = str(person.get("researcher") or raw.get("researcher") or "").strip()
                if not name:
                    continue
                caveats = []
                staleness = person.get("staleness") or {}
                if isinstance(staleness, dict) and staleness.get("note"):
                    caveats.append(str(staleness["note"]))
                if person.get("departed"):
                    caveats.append("may have left")
                for flag in person.get("flags") or []:
                    label = flag.get("label") if isinstance(flag, dict) else str(flag)
                    if label:
                        caveats.append(str(label))
                note = "; ".join(caveats)
                out.append(name + (" (" + note + ")" if note else ""))
            return out

        return lookup

    async def _run_cadence(
        self, *, today, limit=None, urgent_limit=None, update_limit=None
    ) -> Optional[dict]:
        """THE ONE PROACTIVE PASS over the canonical "Outreach PoCs" tab.

        None when the tab cannot be read. This is the ONLY place the proactive
        pass is computed, and it computes nothing else: it returns
        cadence.run()'s result and the caller decides whether that becomes a
        digest section or an answer to a question.

        ACTIVATION IS APPLIED HERE, BEFORE cadence.run() SEES ANYTHING. A row
        with no first-contact date and no connection date is not passed on, so
        no downstream code has to remember to exclude it — the filter is one
        call in one place rather than a rule every future feature must
        re-implement correctly. The count of what was filtered travels with the
        result so the digest can say so.

        THE MASTER TAB is still read, but only as the source of the
        misaligned-row sheet-health flag. The phase-1 master/tracker cross-check
        that used to be its other job is retired.
        """
        if not config.CADENCE_ENABLED:
            log.info("[cadence] CADENCE_ENABLED=false — the cadence sections are omitted")
            return None
        try:
            tab, source = await asyncio.to_thread(gtm_sheet.SHEETS.cadence_tab)
        except gtm_sheet.SheetAccessError as e:
            log.info("[cadence] no sheet to run the proactive pass against: %s", e)
            return None
        except Exception:
            log.exception("[cadence] the canonical tab could not be read")
            return None
        if tab is None:
            log.error(
                "[cadence] no tab is named %s, so there is nothing proactive to run "
                "today. See the [gtm.roles] lines for what each tab was read as.",
                " / ".join(repr(t) for t in config.GTM_POCS_TAB_TITLES) or "(nothing)",
            )
            return None

        staleness = ""
        try:
            staleness = gtm_sheet.SHEETS.staleness_note(tab) or ""
        except Exception:
            log.debug("[cadence] no staleness note available", exc_info=True)

        # THE ACTIVATION GATE. Everything downstream of this line sees ACTIVE
        # rows only, so nothing downstream can mention, chase or count a row
        # that was never started.
        active, inactive_rows = await asyncio.to_thread(
            self._split_active, tab.rows, "the daily proactive pass"
        )

        # The master tab: the rows the misaligned-row sheet-health flag reads.
        # Its absence costs that one flag and nothing else.
        master_rows = None
        try:
            master = await asyncio.to_thread(gtm_sheet.SHEETS.tab, gtm_sheet.MASTER)
            master_rows = list(master.rows) if master else None
        except Exception:
            log.info("[cadence] no master tab for the sheet-health flag", exc_info=True)

        # Broken formulas, from EVERY tab the bot recognises — a #REF! in the
        # researcher lines is worth one line even though nothing reads it. NOT
        # activation-filtered: a broken formula is a property of the tab, and
        # whoever has to fix it needs the whole count.
        errors: dict = {}
        try:
            for kind in (gtm_sheet.POCS, gtm_sheet.MASTER, gtm_sheet.RESEARCHER_LINES,
                         gtm_sheet.PIPELINE, gtm_sheet.FUNNEL, gtm_sheet.POSITIONING):
                for t in await asyncio.to_thread(gtm_sheet.SHEETS.tabs_of, kind):
                    if t.error_cells:
                        errors[t.title] = t.error_cells
        except Exception:
            log.debug("[cadence] could not collect the error cells", exc_info=True)

        result = await asyncio.to_thread(
            lambda: cadence.run(
                active, today=today,
                mapping_lookup=self._cadence_mapping_lookup(),
                stall_days=self._cadence_stall_clock(today),
                limit=limit,
                urgent_limit=urgent_limit,
                update_limit=update_limit,
                master_rows=master_rows,
                error_cells_by_tab=errors,
                quality_seen=self.db.quality_flag_seen,
                inactive=len(inactive_rows),
            )
        )
        result["source"] = source + " tab " + repr(tab.title)
        result["staleness"] = staleness
        result["tab"] = tab
        result["total_rows"] = len(tab.rows)
        return result

    # -- the next-action queue ---------------------------------------------

    async def _run_next_actions(self, *, today) -> Optional[dict]:
        """THE QUEUE: one next action per ACTIVE row. None when it cannot run.

        READ-ONLY AND SEND-FREE, and that is the point of this whole layer. It
        reads the canonical tab, applies the activation gate, reads the snoozes
        and the explicitly scheduled reminders out of SQLite, and hands all of
        it to `nextaction.run`, which is pure. Nothing here posts, and nothing
        here writes — so it is safe to run on every boot and on every question
        with the digest kill switch off.
        """
        if not config.NEXT_ACTION_ENABLED:
            log.info(
                "[nextaction] NEXT_ACTION_ENABLED=false — no queue is computed. "
                "'cadence preview' will say so rather than returning an empty list."
            )
            return None
        try:
            tab, _source = await asyncio.to_thread(gtm_sheet.SHEETS.cadence_tab)
        except gtm_sheet.SheetAccessError as e:
            log.info("[nextaction] no sheet to compute the queue from: %s", e)
            return None
        except Exception:
            log.exception("[nextaction] the canonical tab could not be read")
            return None
        if tab is None:
            return None

        active, inactive = await asyncio.to_thread(
            self._split_active, tab.rows, "the twelve rules"
        )
        snoozes = await asyncio.to_thread(self.db.snoozes)
        scheduled = await asyncio.to_thread(self.db.scheduled_reminders_by_row)

        # THE OTHER FOUR TABS. Seven of the twelve rules are not about an
        # Outreach PoCs row at all — R4 reads the checklist, R12 the packages,
        # R3 the events tab, R2 and R11 the Master Pipeline. All four are
        # read-only and all four are read here rather than inside the engine,
        # which reads no sheet by design.
        deliverables = await self._rule_tab_rows(gtm_sheet.DELIVERABLES, "R4")
        packages = await self._rule_tab_rows(gtm_sheet.PACKAGES, "R12")
        events = await self._rule_tab_rows(gtm_sheet.EVENTS, "R3")
        pipeline = await self._rule_tab_rows(gtm_sheet.RESEARCHER_LINES, "R2/R11")
        pipeline_companies = [
            gtm_sheet.clean_cell(r.get("company")) for r in pipeline
            if gtm_sheet.clean_cell(r.get("company"))
        ]

        # R11'S SNAPSHOT IS A WRITE, AND IT HAPPENS HERE. The engine cannot take
        # it: computing the queue would advance the state the queue is derived
        # from, and every `cadence preview` would consume the newness it was
        # meant to be showing. So it is taken once per tick, here, and the
        # result is passed in as ordinary input.
        try:
            new_companies = await asyncio.to_thread(
                self.db.pipeline_snapshot, pipeline_companies, today=dl.iso(today),
            )
        except Exception:
            log.exception("[rules] the pipeline snapshot failed; R11 produces nothing today")
            new_companies = []

        # R5's cross-day state, and R9's ladder. Both are read-only here.
        iso_week = "%d-W%02d" % today.isocalendar()[:2]
        try:
            week_companies = await asyncio.to_thread(
                self.db.prospect_week_companies, iso_week
            )
            prospect_repeats = await asyncio.to_thread(self.db.prospect_repeats)
        except Exception:
            log.exception("[rules] R5's weekly state could not be read")
            week_companies, prospect_repeats = [], {}
        try:
            meeting_followups = await asyncio.to_thread(self.db.meeting_followups)
        except Exception:
            log.exception("[rules] R9's ladder could not be read")
            meeting_followups = {}

        result = await asyncio.to_thread(
            lambda: nextaction.run(
                today=today, rows=active, snoozes=snoozes, scheduled=scheduled,
                deliverables=deliverables, packages=packages, events=events,
                pipeline_companies=pipeline_companies, new_companies=new_companies,
                prospect_repeats=prospect_repeats, week_companies=week_companies,
                meeting_followups=meeting_followups, inactive=len(inactive),
            )
        )
        result["tab"] = tab
        result["total_rows"] = len(tab.rows)
        try:
            result["staleness"] = gtm_sheet.SHEETS.staleness_note(tab) or ""
        except Exception:
            result["staleness"] = ""
        return result

    async def _research_items(self, items: list, *, today) -> list:
        """Fill in the web half of every item that is waiting on it.

        WHICH ITEMS. Only those the rules marked `web_pending` — R1, R2, R3,
        R6's email search, R8 and R10's news, and R11. Everything else is left
        exactly as the engine produced it.

        THE BUDGET IS CHECKED BEFORE EACH CALL AND BANKED AFTER IT. Before,
        because a call made with nothing left is a call that bills anyway;
        after, because the number that counts is what the API reported billing
        (`usage.server_tool_use.web_search_requests`) and an errored search is
        not billed. Reserving up front would spend a budget on searches that
        never happened.

        WHEN THE BUDGET IS SPENT THE ITEMS SURVIVE. Each keeps its text and its
        place in the queue and carries "web research unavailable today — the
        daily search budget is spent". Dropping them instead would make a
        configured rule look exactly like a quiet week, which is the failure
        this whole layer exists to avoid.

        NOTHING HERE ACTS ON WHAT IT READS. The result becomes text and links on
        an item. No row is written, no message is sent, no rule is re-evaluated
        because of a page's contents — web content is data, and the only thing
        downstream of this is a human reading a message.
        """
        import websearch

        pending = [i for i in (items or []) if i.get("web_pending")]
        if not pending:
            return items or []
        if not websearch.enabled():
            for item in pending:
                item["research_note"] = websearch.unavailable_note(
                    "WEB_SEARCH_ENABLED is off"
                )
            return items

        marker = dl.iso(today)
        budget = max(0, int(config.WEB_SEARCH_DAILY_BUDGET))

        for item in pending:
            left = await asyncio.to_thread(self.db.web_search_budget_left, marker)
            if left <= 0:
                used = await asyncio.to_thread(self.db.web_searches_today, marker)
                item["research_note"] = websearch.unavailable_note(
                    websearch.budget_note(used=used, budget=budget)
                )
                continue

            query = websearch.RULE_QUERIES.get(item.get("rule") or "")
            if not query:
                continue

            result = await self.llm.web_research(
                rule=item.get("rule_id") or item.get("rule") or "?",
                prompt=query + "\n\n" + await self._research_context(item),
                max_uses=min(int(config.WEB_SEARCH_MAX_USES), left),
            )
            await asyncio.to_thread(
                lambda: self.db.record_web_search(
                    on_date=marker, rule_id=item.get("rule_id") or "?",
                    searches=int(result.get("searches") or 0),
                    errors=len(result.get("errors") or []),
                )
            )
            if result.get("ok"):
                item["research"] = result.get("text") or ""
                item["sources"] = result.get("sources") or []
                item["research_note"] = ""
                item["web_pending"] = False
                # THE PLACEHOLDER COMES OUT OF THE TEXT once the research it was
                # standing in for has arrived. Leaving it would put "web
                # research pending" in a message that carries the research.
                item["text"] = str(item.get("text") or "").replace(
                    f" [{websearch.WEB_PENDING}]", ""
                )
            else:
                item["research_note"] = result.get("note") or \
                    websearch.unavailable_note()

        done = sum(1 for i in pending if not i.get("web_pending"))
        log.info(
            "[websearch] %s: researched %d of %d pending item(s); %d search(es) left "
            "of %d today",
            marker, done, len(pending),
            await asyncio.to_thread(self.db.web_search_budget_left, marker), budget,
        )
        return items

    async def _research_context(self, item: dict) -> str:
        """What the search needs to know about THIS item, and nothing more.

        R1 and R2 get the companies and people already on the sheet, because
        both rules are explicitly about ranking against what we already track —
        R1 prioritises news about them, R2 screens for companies that are NOT
        among them. Everything else gets its own row.
        """
        rule = item.get("rule") or ""
        bits: list = []
        if item.get("company"):
            bits.append(f"Company: {item['company']}")
        if item.get("poc"):
            bits.append(f"Person: {item['poc']}"
                        + (f" ({item['poc_designation']})"
                           if item.get("poc_designation") else ""))
        if rule in ("ai_news", "news_company_screen"):
            known = await self._known_companies()
            if known:
                bits.append(
                    "Companies and people already on our sheet:\n"
                    + ", ".join(known[:120])
                )
        return "\n".join(bits) or "(no further context)"

    async def _known_companies(self) -> list:
        """Every company name the playbook tracks, for R1 and R2's ranking."""
        names: list = []
        seen: set = set()
        for kind in (gtm_sheet.POCS, gtm_sheet.RESEARCHER_LINES):
            try:
                tab = await asyncio.to_thread(gtm_sheet.SHEETS.tab, kind)
            except Exception:
                continue
            for row in (tab.rows if tab else []):
                name = gtm_sheet.clean_cell(row.get("company"))
                key = gtm_sheet.normalise_header(name)
                if name and key not in seen:
                    seen.add(key)
                    names.append(name)
        return names

    async def _sweep_proposals(self, *, today, channel) -> bool:
        """The once-a-working-day nudge-and-drop for proposals nobody answered.

        Runs in the FIRST drip slot, once, and posts ONE combined message
        however many proposals are waiting. Returns True when it posted.

        TWO AGES, IN THIS ORDER AND NOT THE OTHER. Proposals past their DROP age
        are closed FIRST, then what remains past the NUDGE age is listed — so a
        proposal that is old enough for both is dropped rather than nudged and
        then dropped on the same afternoon.

        IT DOES NOT COUNT AGAINST THE DAILY CAP. A day that spent all its slots
        on rules and therefore never mentioned four pending approvals would be a
        day the approvals queue grew invisibly.

        NOTHING IS SAID ON A DAY WITH NOTHING PENDING. Silence is the correct
        output of an empty queue, and a daily "no approvals outstanding" post is
        the fastest way to teach a team to skim.
        """
        marker = dl.iso(today)
        nudge_age = max(0, int(config.PROPOSAL_NUDGE_AFTER_DAYS))
        drop_age = max(0, int(config.PROPOSAL_DROP_AFTER_DAYS))

        # WORKING DAYS, NOT CALENDAR DAYS. A proposal made on Friday afternoon
        # is not stale on Saturday, and dropping it on Monday morning because
        # two calendar days passed over a weekend nobody worked would be the
        # bot punishing people for the weekend.
        nudge_before = dl.iso(dl.subtract_working_days(today, nudge_age))
        drop_before = dl.iso(dl.subtract_working_days(today, drop_age + nudge_age))

        # (1) DROP what has already been nudged and is still unanswered.
        try:
            droppable = await asyncio.to_thread(
                lambda: self.db.stale_proposals(before_iso=drop_before, nudged=True)
            )
        except Exception:
            log.exception("[approvals] the drop sweep could not read its proposals")
            droppable = []

        for proposal in droppable:
            if proposal is None:
                continue
            await asyncio.to_thread(
                lambda p=proposal: self.db.close_proposal(
                    proposal_key=p["proposal_key"], status="expired",
                    decision="nobody answered after one nudge",
                    decided_by="", decided_at=dl.now_ist().isoformat(timespec="seconds"),
                )
            )
            state.audit(
                "proposal_dropped",
                reason="nobody answered after one nudge; not mentioned again",
                proposal_key=proposal["proposal_key"],
                company=proposal.get("company", ""), poc=proposal.get("poc", ""),
                proposed=proposal.get("proposed_text", ""),
                requested_by=proposal.get("requested_by", ""),
                nudged_on=proposal.get("nudged_on", ""),
            )
            log.info(
                "[approvals] dropped %s (%s) — nudged on %s, still unanswered. Nothing "
                "was written and it will not be mentioned again.",
                proposal["proposal_key"], proposal.get("company") or "?",
                proposal.get("nudged_on") or "?",
            )

        # (2) NUDGE what is past the nudge age and has never been nudged.
        try:
            pending = await asyncio.to_thread(
                lambda: self.db.stale_proposals(before_iso=nudge_before, nudged=False)
            )
        except Exception:
            log.exception("[approvals] the nudge sweep could not read its proposals")
            pending = []
        pending = [p for p in pending if p]

        if not pending:
            if droppable:
                log.info("[approvals] sweep: %d dropped, nothing left to nudge",
                         len(droppable))
            return False

        body = approvals.pending_text(pending)
        # THE SWEEP TAGS VAISHNAVI AND SID and nobody else: these are approvals,
        # and only they can give one. Tagging the person who ASKED would be
        # tagging somebody who cannot answer.
        body = drip.with_tags(body)
        sent = await guardrails.send(
            channel, body,
            reason=f"one nudge for {len(pending)} proposal(s) still awaiting approval",
            kind="proposal_sweep",
            extra={"date": marker, "pending": len(pending),
                   "dropped": len(droppable)},
        )
        if sent is None:
            log.warning(
                "[approvals] the pending-approvals message was refused or failed. The "
                "proposals are NOT marked nudged, so they will be offered again "
                "tomorrow rather than dropped unmentioned."
            )
            return False

        for proposal in pending:
            await asyncio.to_thread(
                lambda p=proposal: self.db.mark_proposal_nudged(
                    p["proposal_key"], on_date=marker
                )
            )
        state.audit(
            "proposals_nudged",
            reason="one combined nudge for everything awaiting approval",
            date=marker, count=len(pending), dropped=len(droppable),
            keys=[p["proposal_key"] for p in pending],
        )
        log.info(
            "[approvals] sweep: nudged %d proposal(s) in one message, dropped %d. This "
            "message does not count against the daily cap.",
            len(pending), len(droppable),
        )
        return True

    # -- SIMULATION --------------------------------------------------------

    async def _handle_simulation(self, message, text: str) -> bool:
        """Route a simulation or test-helper command. True when handled.

        THE CHANNEL GATE IS SILENT. A "simulate week" typed in the real sales
        channel does nothing at all — not a refusal, because a refusal there is
        itself a message the team has to read.
        """
        cid = getattr(message.channel, "id", 0)
        uid = getattr(message.author, "id", 0)

        helper = await self._handle_test_helper(message, text)
        if helper:
            return True

        parsed = simulation.parse(text)
        if parsed is None:
            return False

        allowed, why = simulation.may_run(cid, uid)
        if not allowed:
            if why:
                await self._reply(message, why, reason="simulation refused")
                return True
            return False

        try:
            await self._run_simulation(message, parsed)
        except Exception:
            log.exception("[sim] the simulation failed")
            await self._reply(
                message,
                "The simulation failed partway through. Nothing was changed — it "
                "runs on a throwaway copy of the database — but the log has the "
                "detail.",
                reason="simulation error",
            )
        return True

    async def _handle_test_helper(self, message, text: str) -> bool:
        """`pretend X is on leave`, `clear leave`, `advance clock`, `reset`."""
        cid = getattr(message.channel, "id", 0)
        uid = getattr(message.author, "id", 0)
        low = (text or "").lower()
        if not any(w in low for w in
                   ("on leave", "clear leave", "advance clock", "reset clock",
                    "reset test state")):
            return False

        allowed, why = simulation.may_run(cid, uid)
        if not allowed:
            if why:
                await self._reply(message, why, reason="test helper refused")
                return True
            return False

        m = simulation._LEAVE_RE.search(text)
        if m:
            await self._reply(message, simulation.pretend_on_leave(m.group("who")),
                              reason="leave override set")
            state.audit("test_leave_override", reason="set by a test command",
                        who=m.group("who"), by=_display(message.author))
            return True

        if simulation._CLEAR_LEAVE_RE.search(text):
            await self._reply(message, simulation.clear_leave_override(),
                              reason="leave override cleared")
            return True

        m = simulation._ADVANCE_RE.search(text)
        if m:
            ok, line = simulation.set_clock_offset(int(m.group("n")))
            await self._reply(message, line, reason="clock advanced")
            if ok:
                state.audit("test_clock_advanced", reason="advanced by a test command",
                            days=int(m.group("n")), by=_display(message.author))
            return True

        if simulation._RESET_CLOCK_RE.search(text):
            await self._reply(message, simulation.reset_clock(), reason="clock reset")
            return True

        if simulation._RESET_STATE_RE.search(text):
            await self._reply(message, await self._reset_test_state(message),
                              reason="test state reset")
            return True

        return False

    async def _reset_test_state(self, message) -> str:
        """Wipe the test database. REFUSES unless DB_PATH names a *_test.db.

        THE NAME CHECK IS THE WHOLE SAFETY. "reset test state" typed against a
        live bot would delete every deadline, snooze, proposal and event
        reminder the team depends on, and there is no undo. Requiring the path
        to END IN `_test.db` means an operator has to have deliberately pointed
        the bot at a test database before the command does anything at all.
        """
        path = str(config.DB_PATH or "")
        if not path.endswith("_test.db"):
            return (
                f"No — DB_PATH is {path!r}, which is not a *_test.db. I will not wipe "
                "a database that might be the real one. Point DB_PATH at something "
                "like ./sales_bot_test.db first."
            )
        try:
            self.db.close() if hasattr(self.db, "close") else None
        except Exception:
            pass
        try:
            if os.path.exists(path):
                os.remove(path)
            self.db = DB(path)
        except Exception as e:
            log.exception("[sim] could not reset the test database")
            return f"I could not reset it ({type(e).__name__}). Nothing was changed."
        state.audit("test_state_reset", reason="wiped by a test command",
                    path=path, by=_display(message.author))
        log.warning("[sim] test database %s wiped by %s", path, _display(message.author))
        return f"Wiped {path} and started a fresh one."

    async def _run_simulation(self, message, parsed: dict) -> None:
        """Run one simulation and post it. Changes nothing."""
        mode = parsed["mode"]
        if mode == simulation.MODE_WEEK:
            days = simulation.week_of()
            rule = ""
        elif mode == simulation.MODE_RULE:
            when = simulation.next_date_for_rule(parsed["rule"])
            if when is None:
                await self._reply(
                    message,
                    f"{parsed['rule']} is not a rule I have, or it is disabled in "
                    "bot_rules.yaml.",
                    reason="unknown rule",
                )
                return
            days, rule = [when], parsed["rule"]
        else:
            days, rule = [parsed["date"]], ""

        channel = message.channel
        fast = bool(parsed["fast"])

        opening = (
            f"{config.SIMULATION_PREFIX} Simulating "
            + (f"the week of {days[0].strftime('%d %b')}" if len(days) > 1
               else days[0].strftime("%a %d %b"))
            + (f", {rule} only" if rule else "")
            + f" · {'fast' if fast else 'compressed real'} spacing"
            + " · nothing will be written"
        )
        await guardrails.send(channel, opening, reason="simulation opening",
                              kind="simulation")

        # ONE SANDBOX FOR THE WHOLE RUN, so a simulated week behaves like a
        # week: Tuesday sees what Monday did. Discarded at the end either way.
        with simulation.SandboxDB(config.DB_PATH) as sandbox, simulation.simulating():
            real_db = self.db
            self.db = sandbox
            try:
                for day in days:
                    await self._simulate_one_day(
                        channel, day, rule=rule, fast=fast,
                        is_week=len(days) > 1,
                    )
            finally:
                self.db = real_db

    async def _simulate_one_day(self, channel, day, *, rule: str, fast: bool,
                                is_week: bool) -> None:
        """One simulated day: header, the messages, the footer."""
        import asyncio as _asyncio

        if is_week:
            await guardrails.send(
                channel, simulation.header(day),
                reason="simulation day header", kind="simulation",
            )

        skipped: list = []
        notes: list = []

        if not drip.is_sending_day(day) and day.weekday() != 6:
            await guardrails.send(
                channel,
                simulation.footer({
                    "day_label": day.strftime("%a %d %b"), "sent": 0,
                    "skipped": [f"{day.strftime('%A')} is silent — the drip does not "
                                "send at the weekend"],
                }),
                reason="simulation footer", kind="simulation",
            )
            return

        queue = await self._run_next_actions(today=day)
        if queue is None:
            await guardrails.send(
                channel,
                simulation.footer({
                    "day_label": day.strftime("%a %d %b"), "sent": 0,
                    "skipped": ["the rules engine is off (NEXT_ACTION_ENABLED) or "
                                "there is no canonical tab to read"],
                }),
                reason="simulation footer", kind="simulation",
            )
            return

        actions = list(queue.get("actions") or [])
        for entry in (queue.get("rules_run") or []):
            if not entry.get("ran"):
                skipped.append(f"{entry['id']} {entry['name']} — {entry['why']}")
            elif not entry.get("items"):
                skipped.append(f"{entry['id']} {entry['name']} — ran, found nothing")

        if rule:
            before = len(actions)
            actions = [a for a in actions if str(a.get("rule_id", "")).upper() == rule]
            notes.append(f"filtered to {rule}: {len(actions)} of {before} item(s)")

        actions.extend(await self._event_actions(today=day))
        planned = await asyncio.to_thread(
            lambda: drip.plan(actions, day=day, history={}, already_sent=[])
        )

        messages = planned.get("messages") or []
        cap = config.message_cap_for(day)
        counted = sum(1 for m in messages if m.get("counts_toward_cap", True))
        pace = simulation.pace_seconds(fast=fast, count=max(1, len(messages)))

        for i, msg in enumerate(messages):
            body = await self._simulated_body(msg, day=day)
            await guardrails.send(
                channel, body,
                reason=f"simulated slot {msg.get('slot')} for {dl.iso(day)}",
                kind="simulation",
            )
            # A SIMULATED SEND MARKS THE SANDBOX, so a simulated WEEK behaves
            # like a week. Without this the events dedup — which is written
            # only on a real send — never learns, and a conference reminded on
            # Monday is reminded again every day to Friday. The write lands in
            # the throwaway copy and is discarded with it, so the real
            # `event_reminders` table is untouched.
            for action in (msg.get("actions") or []):
                key = action.get("event_key")
                if not key:
                    continue
                await asyncio.to_thread(
                    lambda a=action, k=key: self.db.record_event_reminder(
                        event_key=k, event=a.get("company", ""),
                        event_date=a.get("event_date", ""),
                        location=a.get("location", ""), sent_on=dl.iso(day),
                    )
                )
            if i < len(messages) - 1 and pace:
                await _asyncio.sleep(pace)

        # THE SWEEP RIDES SLOT 1 IN REAL LIFE, so it does here too — and it is
        # rendered rather than sent, like everything else.
        sweep_line = await self._simulated_sweep(day)
        if sweep_line:
            await guardrails.send(channel, sweep_line,
                                  reason="simulated approvals sweep",
                                  kind="simulation")
            notes.append("the pending-approvals message rides slot 1 and takes no "
                         "cap slot")

        # TWO GROUPS OF THE SAME RULE ROLL SEPARATELY — the drip groups by
        # (rule x owner), so "R4 Deliverables checklist" can legitimately
        # appear twice. Naming the owner and the companies is what makes the
        # two lines readable as two different things rather than a bug.
        def _label(g, default):
            head = f"{g.get('rule_id', '?')} {g.get('rule_name') or g.get('type')}"
            who = str(g.get("owner") or "").strip()
            names = [str(c) for c in (g.get("companies") or []) if c]
            bits = []
            if who:
                bits.append(who)
            if names:
                bits.append(names[0] if len(names) == 1
                           else f"{names[0]} +{len(names) - 1}")
            if bits:
                head += " (" + ", ".join(bits) + ")"
            return head + " — " + str(g.get("why") or default)

        rolled = [_label(g, "over the cap") for g in (planned.get("rolled") or [])]
        held = [_label(g, "held") for g in (planned.get("held") or [])]

        await guardrails.send(
            channel,
            simulation.footer({
                "day_label": day.strftime("%a %d %b"),
                "sent": len(messages), "counted": counted, "cap": cap,
                "times": [m.get("send_at_hhmm", "") for m in messages],
                "rolled": rolled, "skipped": skipped + held, "notes": notes,
            }),
            reason="simulation footer", kind="simulation",
        )

    async def _simulated_body(self, msg: dict, *, day) -> str:
        """Compose one simulated message exactly as the real one would be.

        THE MODEL COMPOSES IT. A simulation that showed the template would be
        showing something the team will never receive — the point is to see the
        actual wording, and the actual wording comes from the model.

        A DM RUNG IS RENDERED, NOT SENT: "[DM to Vaishnavi] ...".
        """
        owner = str(msg.get("owner") or "")
        address = self._drip_mention(msg)
        fallback = drip.compose_fallback(msg, address=address)
        body, _used = fallback, False
        if self.llm is not None:
            try:
                body, _used = await self.llm.proactive_message(
                    prompt=drip.compose_prompt(msg, address=address),
                    fallback=fallback,
                    recent_openers=await asyncio.to_thread(self.db.recent_openers, 5),
                )
            except Exception:
                log.exception("[sim] composing failed; showing the template")

        body = drip.with_tags(
            body, owner_id=config.roster_id_for_name(owner), owner_name=owner,
        )
        if str(msg.get("destination") or "") in ("dm", "escalation"):
            body = simulation.dm_line(owner or "the owner", body)
        return simulation.prefix(simulation.strip_mentions(body))

    async def _simulated_sweep(self, day) -> str:
        """The pending-approvals message, rendered rather than sent."""
        try:
            cutoff = dl.iso(dl.subtract_working_days(
                day, config.PROPOSAL_NUDGE_AFTER_DAYS))
            pending = await asyncio.to_thread(
                lambda: self.db.stale_proposals(before_iso=cutoff, nudged=False)
            )
        except Exception:
            log.exception("[sim] could not read the proposal queue")
            return ""
        pending = [p for p in pending if p]
        if not pending:
            return ""
        body = drip.with_tags(approvals.pending_text(pending))
        return simulation.prefix(simulation.strip_mentions(body))

    async def _rule_tab_rows(self, kind: str, for_rules: str) -> list:
        """One read-only context tab's rows, or [] with a log line.

        [] RATHER THAN AN EXCEPTION, and deliberately: a missing Sales Packages
        tab must cost R12 and nothing else. The rule then produces no items and
        `cadence preview` reports it as "ran, found nothing" — which is not the
        same as "the tab is gone", so the miss is logged here by rule id.
        """
        try:
            tab = await asyncio.to_thread(gtm_sheet.SHEETS.tab, kind)
        except gtm_sheet.SheetAccessError as e:
            log.info("[rules] %s: the %s tab could not be read (%s)", for_rules, kind, e)
            return []
        except Exception:
            log.exception("[rules] %s: the %s tab could not be read", for_rules, kind)
            return []
        if tab is None:
            log.warning(
                "[rules] %s: there is no %s tab in the playbook, so that rule produces "
                "nothing. It is found BY NAME — check the matching *_TAB_TITLES setting "
                "against the [gtm.schema] log.", for_rules, kind,
            )
            return []
        return list(tab.rows)

    # -- row activation ----------------------------------------------------

    def _split_active(self, rows: list, why: str) -> tuple[list, list]:
        """(active, inactive) for one set of canonical-tab rows.

        THE SINGLE GATE EVERY PROACTIVE PATH GOES THROUGH. It is a method rather
        than a bare call to activation.split so that reading the explicit
        activations — which is a database hit, and which FAILS CLOSED — happens
        in exactly one place. A path that forgot to read them would quietly
        ignore an instruction somebody gave out loud.

        Blocking (it touches SQLite); call it from a thread.
        """
        try:
            keys = self.db.activated_row_keys()
        except Exception:
            log.exception(
                "[activation] could not read the explicit activations; falling back to "
                "the date rule alone for %s", why,
            )
            keys = frozenset()
        active, inactive = activation.split(list(rows or []), activated_keys=keys)
        log.info(
            "[activation] %s: %d of %d row(s) ACTIVE, %d inactive (no first-contact or "
            "connection date — invisible to proactive output)%s",
            why, len(active), len(rows or []), len(inactive),
            f"; {len(keys)} explicit activation(s) in force" if keys else "",
        )
        return active, inactive

    async def _active_rows_of_canonical_tab(self, why: str) -> tuple[list, object]:
        """(active rows, tab) for the canonical tab. ([], None) when unreadable.

        The async front door to `_split_active`, used by the proactive
        collectors that read the tab directly rather than through the cadence.
        """
        try:
            tab = await asyncio.to_thread(gtm_sheet.SHEETS.pocs_tab)
        except Exception:
            log.info("[activation] no canonical tab for %s", why, exc_info=True)
            return [], None
        if tab is None:
            return [], None
        active, _inactive = await asyncio.to_thread(self._split_active, tab.rows, why)
        return active, tab


    # THE MEETING-PREP BRIEF COLLECTOR IS REMOVED, not disabled.
    #
    # It selected its meetings from phase-1 rule (h) items ("a meeting inside
    # MEETING_PREP_DAYS"), and that rule is retired with the rest of the
    # phase-1 set — so the collector had no input left and would have run every
    # digest to produce nothing. prep.py still builds a brief and is unchanged;
    # what is gone is the PROACTIVE TRIGGER that chose which meeting got one.
    # Re-wiring it is a phase-2 rule against the "Outreach PoCs" tab, not a
    # resurrection of the phase-1 rule that fed it.
    #
    # The `prep_brief_sent` effect and its SQLite dedup are deliberately left in
    # place: they cost nothing while nothing emits the effect, and throwing away
    # the record of which meetings were already briefed would mean re-briefing
    # all of them the day a phase-2 rule turns this back on.
    # `_age_digest_items` is REMOVED. It stamped every item with "(3rd day)" out
    # of the `digest_items` table — a marker that only means something in a list
    # a reader scans top to bottom. A drip message is one thought about one
    # subject; "(3rd day)" on it would be the bot keeping score out loud, which
    # is the opposite of the voice. The table stays, unread, so the history is
    # not thrown away.

    # THE DIGEST EFFECTS ARE REMOVED WITH THE DIGEST.
    #
    # `_apply_digest_effects` applied, after a successful post, the things that
    # must not happen for a message nobody saw: a spent chase attempt, an
    # escalation raised, a prep brief marked written, a sheet-health flag marked
    # reported. It existed because ONE message carried all of them and the
    # write had to wait for that one send to succeed.
    #
    # The drip sends up to three independent messages, each recorded in
    # `drip_sends` the moment it lands, so "did this go out" is answered
    # per-message by the slot row rather than by a batch of deferred effects.
    # The underlying SQLite state (chases, quality_flags, prep_briefs) is
    # untouched — what is gone is the batching, not the records.

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

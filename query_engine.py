"""The question-answering engine — a bounded tool-use loop (READ-ONLY).

This is the BOT's own Anthropic call, not Claude Code. It answers open-ended
questions by being handed a set of read-only tools and calling them, so a new
phrasing doesn't need a new handler.

Design, and what changed from the PM bot's version:

  - NO BUILT-IN TOOLS. The PM engine shipped with a hard-coded list of issue-
    tracker tools; this one has none. EVERY tool comes from the caller via `tools=` —
    bot.py supplies the Discord tools (scoped to the sales channels by
    guardrails) and the meeting-notes tools. The engine therefore holds no
    credential and no data source of its own, and cannot reach anything the
    caller didn't hand it. That is the property that makes the channel scoping
    hold here too.

  - SOURCE HONESTY IS THE PROMPT'S MAIN JOB. Two of the three sources are
    awaiting access (sources.py), so the system prompt tells the model exactly
    what it can and cannot see and forbids answering from a source it can't
    reach. A confident answer built from one source out of three is the specific
    failure this prompt is written against.

  - The loop caps at MAX_TOOL_ITERATIONS; on hitting the cap it asks for a final
    answer with the tools removed, so a reply is always produced and the model is
    told to name what it didn't get to check.

  - Tool errors come back as tool_result JSON with an "error" key, so the model
    recovers or reports the failure instead of the loop crashing.

The persona, the policy and the live source statuses all arrive through
`persona.system_preamble()`, so this path answers under exactly the same policy
as every other path that speaks.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from anthropic import Anthropic

import config
import persona

log = logging.getLogger(__name__)

# Loop bounds. A multi-source answer needs more rounds than a single lookup, so
# this is configurable (QUERY_ENGINE_MAX_TOOL_ITERATIONS, default 8); the prompt
# also tells the model to batch independent calls into ONE turn. On the cap the
# loop still forces a final answer, so a reply is always produced.
MAX_TOOL_ITERATIONS = max(1, int(config.QUERY_ENGINE_MAX_TOOL_ITERATIONS))
# Hard per-turn output stop. The prompt's length rule is what actually keeps
# answers short; this only stops a runaway.
MAX_TOKENS = max(256, int(config.QUERY_ENGINE_MAX_TOKENS))


def _system_prompt(*, requester_name: str, today: str, tool_names: list[str]) -> str:
    """The full system prompt: persona + policy + live source statuses (all from
    `persona.system_preamble()`), then how to answer.

    `tool_names` is listed explicitly so the model reasons about the tools it
    ACTUALLY has this turn rather than ones it remembers from another context —
    the tool set is caller-supplied and can legitimately differ between calls."""
    tools_line = ", ".join(tool_names) if tool_names else "(none — you have no tools this turn)"

    return persona.system_preamble() + f"""You are answering a question asked in one of
the team's SALES channels. Today's date is {today} (UTC). The person asking is
**{requester_name}**; when they say "me", "my" or "I" they mean themselves.

THE TOOLS YOU HAVE RIGHT NOW: {tools_line}

=== WHAT YOU CAN SEE (read the SOURCE STATUS block above before choosing a tool) ===
You are READ-ONLY everywhere. You cannot send, edit, file, or change anything —
you look things up and you report what you find.

Your reach is limited in two ways, and you must be straight about both:
1. CHANNELS: you can only read the team's SALES channels. Not the rest of the
   server, not DMs, not anyone's inbox. If a question depends on something said
   elsewhere, say that's outside what you can see.
2. SOURCES: some of your sources are still awaiting access (the status block
   above says which). You MUST NOT answer from one of those, and you MUST say so
   when a question needs one. "I can't see the pipeline sheet yet, so I can't tell
   you where Acme is" is a complete and useful answer. A confident answer built
   from the one source you happen to have is not.

=== HOW TO ANSWER ===
- ANSWER BY CALLING TOOLS, never by guessing. If no tool can reach it, say so.
- BATCH INDEPENDENT CALLS into the SAME turn — the loop has a hard iteration cap,
  and a source you never got round to calling is one you must report as
  unchecked. Only chain sequentially when one call genuinely feeds the next.
- SEARCH BEFORE YOU ASK. Never answer a question with a question back ("which
  deal?", "can you link me the message?"). The channel history is searchable and
  you can find it: search first, answer with what you find, and put any
  offer-to-narrow at the very END, after a real answer. Asking someone for a link
  to something they already said in a channel you can read is the worst version
  of this.
- LABEL EVERY FACT WITH ITS SOURCE and the date, inline: (channel history, 12
  Aug), (meeting notes, AM sync 14 Aug). A sentence a reader can't trace back is
  a bug.
- NEVER SKIP A SOURCE SILENTLY. If a source came back empty, say so in words —
  "nothing in the sales channels about Acme in the last 14 days". An omitted
  section reads as "there was nothing", and if you never called the tool that
  claim is a fabrication. If a source is awaiting access or its tool errored, say
  THAT instead — do not report it as empty.
- WEIGH RECENCY AND SPECIFICITY. A dated, specific message beats a vague earlier
  one. When two sources disagree, say so plainly with both dates rather than
  smoothing it over: "the notes from 12 Aug say the deck went out, but nothing in
  the channel confirms it".
- NEVER INVENT a number, a date, a company, a deal stage, or a name. Only what
  the tools returned.

=== MEETING-NOTES QUESTIONS (when the notes tools are available) ===
- RESOLVE THE DATE yourself from today's date above and pass it as
  date=YYYY-MM-DD: "today" → today; "yesterday" → today − 1; a weekday → the most
  recent past date that fell on it; an explicit date → that date. For a vague
  "the last meeting" / "latest", OMIT date entirely to get the most recent.
- Pass `label` only when they name a meeting ("the pipeline review", "the Acme
  call"); it matches as a substring. Omit it otherwise.
- ANSWER FROM THE NOTE'S STRUCTURE: the summary, then the decisions, then the
  next steps as owner → task. Attribute a decision to whoever the notes say made
  it; if a line names nobody, report it WITHOUT inventing an owner.
- ALWAYS STATE WHICH NOTE you read — "the pipeline review, 14 Aug" — and the
  freshness line ("most recent note on file: 14 Aug").
- HANDLE MISSING DATA HONESTLY. configured=false means notes access isn't set up:
  say exactly that, never "nothing was discussed". found=false means that
  particular note isn't on file: say so and name the most recent one that IS,
  e.g. "no note for Tuesday; the most recent is the pipeline review from 14 Aug
  — want that?". NEVER answer from a different day's note as if it were the one
  asked for, and never imply a meeting didn't happen.

=== CHANNEL QUESTIONS ===
- "what's been happening" / "anything I missed" with NO person named →
  recent_channel_activity(days=N). N from the question ("last two days" → 2,
  "this week" → 7). It reports the window it ACTUALLY used after clamping — quote
  that one, not the one you asked for. SUMMARISE who discussed what; never dump
  the message list.
- A question about ONE person → recent_sales_activity(name). If it comes back
  ambiguous, ASK which person — never pick one.
- "did we ever…", "what did we agree with…", "has anyone followed up on…" →
  search_channel_history with a few distinct keywords from the question. START
  NARROW (the likely channel, the question's own words, the default window) and
  widen only if that returns nothing. Keep the context window: a reply posted
  without a reply-to is only interpretable next to the message it follows. Read
  the results as a TIMELINE — what was promised, then what actually happened.
- An empty search IS evidence, but report what you searched (terms, channel,
  window) so they can correct it rather than repeat themselves.

=== OUTPUT ===
- Direct and brief. Lead with the answer, then the evidence. Most answers should
  fit in ONE Discord message (under ~1800 characters).
- NO markdown headers (no #, ##, ###). A short **bold label** introduces a
  section if you need one.
- NO EMOJIS.
- No blank lines between items — a single line break is enough.
- Use jump links when you cite a specific message.
- If the answer would run very long, lead with the most relevant items and close
  with one line like "…and 9 more — narrow it down and I'll pull them".
- When a tool errors, say briefly what failed. Don't retry endlessly."""


class QueryEngine:
    """A bounded tool-use loop over caller-supplied read-only tools.

    Holds no data source of its own: `answer(tools=...)` is the only way tools
    enter, so the engine's reach is exactly what bot.py chose to give it.
    """

    def __init__(self, api_key: str, model: str) -> None:
        self._client = Anthropic(api_key=api_key)
        self._model = model

    async def _call_model(self, *, system, tools, messages):
        """One model turn, on a thread so the sync SDK never blocks the Discord
        gateway heartbeat."""
        return await asyncio.to_thread(
            self._client.messages.create,
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
        )

    async def answer(
        self,
        *,
        question: str,
        requester_name: str = "",
        tools: Optional[list[dict]] = None,
        history: Optional[list[dict]] = None,
    ) -> Optional[str]:
        """Answer `question` by looping model ⇄ tools. Returns the reply text, or
        None on total failure (the caller then falls back to a persona-voiced
        nudge rather than a canned template).

        `tools` is the COMPLETE tool set for this call — each entry is
        {"schema": <tool def>, "handler": async fn(input_dict) -> jsonable}.
        There are no built-ins to fall back on: pass nothing and the model has to
        answer from the conversation alone, which is the correct behaviour when
        every source is unavailable.

        `history` is the last few turns in this channel as [{"question",
        "answer"}] (oldest first), replayed as user/assistant messages before the
        current question so a follow-up that omits its subject ("what about
        Globex?") resolves against what was just asked. Short-term working
        context only — never persisted.
        """
        tools = tools or []
        handlers = {t["schema"]["name"]: t["handler"] for t in tools}
        schemas = [t["schema"] for t in tools]
        tool_names = [t["schema"]["name"] for t in tools]

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        system = _system_prompt(
            requester_name=requester_name or "(unknown)",
            today=today,
            tool_names=tool_names,
        )

        messages: list[dict] = []
        for turn in history or []:
            q = str((turn or {}).get("question") or "").strip()
            a = str((turn or {}).get("answer") or "").strip()
            if q and a:
                messages.append({"role": "user", "content": q})
                messages.append({"role": "assistant", "content": a})
        messages.append({"role": "user", "content": question})

        log.info(
            "[engine] start q=%r requester=%r tools=%d history_turns=%d",
            question[:160], requester_name, len(schemas),
            len([t for t in (history or []) if (t or {}).get("question")]),
        )

        last_text = ""
        for i in range(MAX_TOOL_ITERATIONS):
            log.info("[engine] iteration %d/%d: calling model", i + 1, MAX_TOOL_ITERATIONS)
            try:
                resp = await self._call_model(system=system, tools=schemas, messages=messages)
            except Exception:
                log.exception("[engine] model call raised")
                return last_text or None

            text_now = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
            if text_now:
                last_text = text_now

            stop = resp.stop_reason
            if stop != "tool_use":
                log.info("[engine] iteration %d: final (stop=%s) len=%d", i + 1, stop, len(last_text))
                return last_text or None

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                log.info("[engine] tool_use %s input=%s", block.name, block.input)
                result = await self._dispatch(block.name, block.input or {}, handlers)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str, ensure_ascii=False),
                    }
                )
            if not tool_results:
                # stop was tool_use but no dispatchable block came through — the
                # model isn't waiting on us, so answer with what we have.
                log.info("[engine] iteration %d: no dispatchable tool calls; returning", i + 1)
                return last_text or None
            messages.append({"role": "user", "content": tool_results})

        # Hit the iteration cap — force a final answer with the tools removed, and
        # make the model name what it never got to.
        log.info("[engine] hit tool-iteration cap; requesting final answer without tools")
        messages.append(
            {
                "role": "user",
                "content": (
                    "You've reached the tool-call limit. Answer now, concisely, from what "
                    "you've already gathered. If it's incomplete, say so — and NAME the "
                    "sources you did not get to check rather than answering as if you had."
                ),
            }
        )
        try:
            resp = await asyncio.to_thread(
                self._client.messages.create,
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
            )
            final = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
            return final or last_text or None
        except Exception:
            log.exception("[engine] final no-tools call raised")
            return last_text or None

    async def _dispatch(self, name: str, tool_input: dict, handlers: dict):
        """Execute one tool, returning JSON-serialisable data.

        Every tool is caller-supplied, so this is just a lookup plus an error
        wrapper: an unexpected exception becomes {"error": ...} that the model can
        recover from inside the loop, rather than an exception that ends the
        answer. A name the caller didn't provide is reported as unknown — the
        model then picks a tool it actually has."""
        handler = (handlers or {}).get(name)
        if handler is None:
            log.warning("[engine] model called unknown tool %r", name)
            return {"error": f"unknown tool '{name}' — it isn't available in this conversation"}
        try:
            return await handler(tool_input)
        except Exception as e:
            log.exception("[engine] tool %s raised", name)
            return {"error": f"tool '{name}' failed: {type(e).__name__}"}

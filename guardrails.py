"""HARD GUARDRAILS — enforced in code, not in persona text.

Every outbound message and every channel read in this bot goes through this
module. That is the whole point: the rules below are not advice given to a model
in a prompt, they are a chokepoint the code cannot route around. A prompt can be
argued with; `send()` cannot.

THE RULES
    1. NEVER DM ANYONE. Not a fallback, not "just this once", not for a nudge
       that failed to post in channel. `send()` refuses any destination that
       isn't a guild text channel, and there is no DM path anywhere in the code.
    2. NEVER MESSAGE ANYONE OFF THE TEAM ROSTER. The bot may only @-mention users
       in TEAM_ROSTER_IDS. Everyone else is named in plain text, never pinged —
       `mention_for()` is the only way a mention token is ever produced, and
       `sanitize()` strips any stray <@id> the model invented.
    3. POST ONLY IN SALES_CHANNEL_IDS. Enforced at BOTH ends: `may_read()` gates
       every history scan and every incoming message, `send()` gates every post.
       A channel that isn't in the list is invisible in one direction and
       unreachable in the other.
    4. EVERY ACTION IS LOGGED. `send()` writes an audit record (state/audit.jsonl)
       for every message that goes out, with a timestamp and a reason. A refusal
       is logged too — a blocked send is exactly the event an operator wants to
       see.

The second layer, and the more important one, is server-side: the bot's Discord
role must be denied View Channel on every non-sales channel (see DEPLOY.md).
Code scoping is defence in depth, not the only defence.
"""
import logging
import re
from typing import Optional

import discord

import config
import state

log = logging.getLogger(__name__)

# Any <@123> / <@!123> mention token in model-written text. Used by sanitize() to
# strip mentions the model invented — the ONLY legitimate source of a mention
# token is mention_for(), which checks the roster first.
_MENTION_RE = re.compile(r"<@!?(\d+)>")
# @everyone / @here can't be produced by mention_for() at all; a model that types
# one in prose would ping the whole server, so it is defanged unconditionally.
_MASS_MENTION_RE = re.compile(r"@(everyone|here)\b", re.IGNORECASE)


class GuardrailViolation(Exception):
    """Raised when code attempts something the guardrails forbid outright (a DM,
    a send to a non-sales channel). Callers are expected NOT to catch this and
    retry differently — the correct response to a violation is to not send."""


# -- read side ----------------------------------------------------------------


def may_read(channel_id) -> bool:
    """May the bot read this channel at all? The gate for every incoming message
    and every history scan (query.py). Anything outside SALES_CHANNEL_IDS is
    treated as if it does not exist."""
    return config.is_sales_channel(channel_id)


def readable_channel_ids() -> list[int]:
    """The channel ids any scan may walk. Scans iterate THIS, never a caller-
    supplied list, so a new scan can't accidentally widen the scope."""
    return list(config.SALES_CHANNEL_IDS)


def readable_channels(client: discord.Client) -> list:
    """Resolved, in-scope channel objects the bot can actually see right now.
    Channels it can't see (no View Channel, deleted, wrong guild) are skipped —
    that is the server-side layer doing its job, not an error."""
    out = []
    for cid in readable_channel_ids():
        chan = client.get_channel(cid)
        if chan is None:
            log.debug("[guardrails] channel %s is not visible to the bot; skipping", cid)
            continue
        out.append(chan)
    return out


# -- addressing ---------------------------------------------------------------


def mention_for(user_id, display_name: str = "") -> str:
    """THE only way this bot produces an @-mention.

    Returns a real `<@id>` token ONLY when the person is on the team roster and
    we have their numeric id. For anyone else it returns their plain-text name,
    so the message still reads naturally and still reaches a human eye in the
    channel — it just doesn't ping someone the bot has no business pinging.

    Roster membership is checked on the ID, not the name: a display name can be
    changed by its owner, an id cannot.
    """
    name = (display_name or "").strip()
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        uid = 0

    if uid and uid in config.TEAM_ROSTER_IDS:
        return f"<@{uid}>"

    if uid:
        log.info(
            "[guardrails] user %s (%r) is not on TEAM_ROSTER_IDS — naming them in plain "
            "text instead of pinging",
            uid, name or "?",
        )
        name = name or str(config.ROSTER_DISPLAY_NAMES.get(str(uid)) or "").strip()
    return name or "there"


def sanitize(text: str) -> str:
    """Strip every mention token the bot did not itself authorise.

    Model-written text (a nudge, an answer) can contain a `<@123>` the model
    copied out of a scanned message or simply invented. Those are removed here:
    a mention that didn't come from `mention_for()` never had a roster check.
    An off-roster id is replaced with the person's known display name where we
    have one, and dropped otherwise. @everyone/@here are always defanged.
    """
    def _replace(m: re.Match) -> str:
        uid = m.group(1)
        try:
            if int(uid) in config.TEAM_ROSTER_IDS:
                return m.group(0)  # on the roster — a legitimate ping
        except ValueError:
            pass
        known = str(config.ROSTER_DISPLAY_NAMES.get(str(uid)) or "").strip()
        log.info("[guardrails] stripped off-roster mention <@%s> from outbound text", uid)
        return known or "someone"

    cleaned = _MENTION_RE.sub(_replace, text or "")
    # Zero-width joiner between @ and the keyword: still readable, no longer a ping.
    cleaned = _MASS_MENTION_RE.sub(lambda m: "@​" + m.group(1), cleaned)
    return cleaned


# -- send side ----------------------------------------------------------------


def _channel_id_of(destination) -> Optional[int]:
    """The channel id of a send destination, or None when it isn't a channel we
    can identify (a User/Member — i.e. a DM — has no channel id here)."""
    cid = getattr(destination, "id", None)
    if isinstance(destination, (discord.User, discord.Member)):
        return None
    try:
        return int(cid) if cid is not None else None
    except (TypeError, ValueError):
        return None


def _is_dm(destination) -> bool:
    """True for anything that would open or use a private channel. Checked
    explicitly rather than inferred, because 'it wasn't in the channel list' and
    'it was a DM' deserve different log lines."""
    if isinstance(destination, (discord.User, discord.Member, discord.DMChannel)):
        return True
    if isinstance(destination, getattr(discord, "GroupChannel", ()) or ()):
        return True
    return getattr(destination, "guild", "missing") is None


async def send(
    destination,
    text: str,
    *,
    reason: str,
    kind: str = "message",
    reply_to: Optional[discord.Message] = None,
    extra: Optional[dict] = None,
) -> Optional[discord.Message]:
    """The ONLY way this bot puts text into Discord.

    `reason` is required and is written to the audit log — an action nobody can
    explain afterwards is not an action this bot is allowed to take.

    Refuses, loudly and without falling back to any other destination:
      - a DM or any non-guild channel (rule 1),
      - any channel outside SALES_CHANNEL_IDS (rule 3).

    Returns the sent message, or None when the send was refused or Discord
    rejected it. Never raises on a Discord failure — a failed post is logged and
    audited, not propagated into the sweeper.
    """
    if _is_dm(destination):
        log.error(
            "[guardrails] REFUSED: attempt to DM %r (kind=%s reason=%s). This bot never DMs.",
            getattr(destination, "id", destination), kind, reason,
        )
        state.audit(
            "send_refused",
            reason="hard guardrail: this bot never sends DMs",
            kind=kind,
            attempted_reason=reason,
            destination=str(getattr(destination, "id", destination)),
        )
        return None

    channel_id = _channel_id_of(destination)
    if channel_id is None or not config.is_sales_channel(channel_id):
        log.error(
            "[guardrails] REFUSED: attempt to post in channel %s, which is not in "
            "SALES_CHANNEL_IDS (kind=%s reason=%s).",
            channel_id, kind, reason,
        )
        state.audit(
            "send_refused",
            reason="hard guardrail: channel is outside SALES_CHANNEL_IDS",
            kind=kind,
            attempted_reason=reason,
            channel_id=channel_id,
        )
        return None

    body = sanitize(text or "").strip()
    if not body:
        log.info("[guardrails] nothing to send (empty body) for kind=%s reason=%s", kind, reason)
        return None

    try:
        if reply_to is not None:
            # mention_author=False: replying should not itself ping the author.
            sent = await reply_to.reply(body, mention_author=False)
        else:
            sent = await destination.send(body)
    except discord.DiscordException:
        log.exception(
            "[guardrails] Discord rejected the send to channel %s (kind=%s)", channel_id, kind
        )
        state.audit(
            "send_failed",
            reason=reason,
            kind=kind,
            channel_id=channel_id,
            error="discord rejected the send",
        )
        return None

    log.info(
        "[guardrails] SENT kind=%s channel=%s msg=%s reason=%s len=%d",
        kind, channel_id, sent.id, reason, len(body),
    )
    state.audit(
        "message_sent",
        reason=reason,
        kind=kind,
        channel_id=channel_id,
        message_id=sent.id,
        jump_url=getattr(sent, "jump_url", None),
        preview=body[:200],
        **(extra or {}),
    )
    return sent

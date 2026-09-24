"""HARD GUARDRAILS — enforced in code, not in persona text.

Every outbound message and every channel read in this bot goes through this
module. That is the whole point: the rules below are not advice given to a model
in a prompt, they are a chokepoint the code cannot route around. A prompt can be
argued with; `send()` cannot.

THE RULES
    1. DM ONLY THROUGH THE NARROW EXCEPTION, and never otherwise. The default is
       still a flat refusal: `send()` rejects any destination that is not a
       guild text channel unless the caller passes an explicit `dm_reason` that
       `may_dm()` recognises. There are exactly two recognised reasons, both
       from the strategy doc:

         (a) an item at least DM_OVERDUE_DAYS overdue, to the person who owns
             it — past its deadline, or past its first reminder;
         (b) R9's second and third meeting follow-ups (DM_MEETING_FOLLOWUP_RUNGS).

       And four conditions on both, every one of them checked in code:
         - SALES_DMS_ENABLED must be on. It is OFF by default.
         - the recipient must be in TEAM_ROSTER_IDS. A DM is the one path where
           "outside the team" would be invisible to everyone but the recipient.
         - the same item must not have gone to the channel the same day.
         - the DM is written to state/audit.jsonl with its reason, like
           everything else — more so, because nobody else can see it.

       ANY OTHER DM IS STILL REFUSED, loudly, exactly as before. A caller with
       no `dm_reason`, an unrecognised one, or a reason that fails its own check
       gets None and an audit record.

    2. NEVER MESSAGE ANYONE OFF THE TEAM ROSTER. The bot may only @-mention users
       in TEAM_ROSTER_IDS. Everyone else is named in plain text, never pinged —
       `mention_for()` is the only way a mention token is ever produced, and
       `sanitize()` strips any stray <@id> the model invented.
    3. POST ONLY IN SALES_CHANNEL_IDS. Enforced at BOTH ends, and the two ends
       are deliberately separate functions: `may_read()` gates every history
       scan and every incoming message, `send()` gates every post.

       THE READ SIDE ADMITS ONE MORE CHANNEL: HOLIDAY_CHANNEL_ID, the leave
       channel. The bot reads it to find out whether the person it is about to
       address is off today. THE SEND SIDE DOES NOT ADMIT IT — `send()` still
       checks SALES_CHANNEL_IDS alone, so there is no code path that posts
       there. That asymmetry is the whole reason reading and writing were ever
       two functions instead of one `may_use()`.
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
    and every history scan (query.py).

    SALES_CHANNEL_IDS, PLUS THE LEAVE CHANNEL AND NOTHING ELSE. Anything outside
    those is treated as if it does not exist.

    The leave channel is readable so the bot can find out whether the person it
    is about to address is off today. It is NOT postable: `send()` checks
    `config.is_sales_channel` directly and does not consult this function. If
    you are adding a channel here, check whether you also meant to make it
    sendable — you almost certainly did not.
    """
    if config.is_sales_channel(channel_id):
        return True
    return is_leave_channel(channel_id)


def is_leave_channel(channel_id) -> bool:
    """True for HOLIDAY_CHANNEL_ID, the one read-only non-sales channel.

    Separate from `may_read` so a caller can ask which KIND of readable channel
    it has. The leave reader uses it to refuse to read anything else, which
    means a mis-set HOLIDAY_CHANNEL_ID reads the wrong channel rather than
    every channel.
    """
    holiday = int(getattr(config, "HOLIDAY_CHANNEL_ID", 0) or 0)
    if not holiday:
        return False
    try:
        return int(channel_id) == holiday
    except (TypeError, ValueError):
        return False


def readable_channel_ids() -> list[int]:
    """The channel ids a SALES scan may walk. Scans iterate THIS, never a
    caller-supplied list, so a new scan can't accidentally widen the scope.

    THE LEAVE CHANNEL IS NOT IN HERE. It is readable (`may_read`) but it is not
    a sales channel, and a scan looking for what the team said about a deal has
    no business walking it. The leave reader addresses it by id, on purpose.
    """
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


# THE TWO RECOGNISED DM REASONS. A caller passes one of these as `dm_reason`;
# anything else — including None — is refused. Strings rather than booleans so
# the audit record says WHY, and so a third reason cannot be added by accident.
DM_REASON_OVERDUE = "overdue_escalation"
DM_REASON_MEETING_FOLLOWUP = "meeting_followup_rung"
DM_REASONS = (DM_REASON_OVERDUE, DM_REASON_MEETING_FOLLOWUP)


def may_dm(user_id, *, dm_reason: str = "", overdue_days: int = 0,
           rung: int = 0, item_in_channel_today: bool = False) -> tuple:
    """(allowed, why) for one DM. The whole exception, in one place.

    EVERY CONDITION IS CHECKED HERE AND THE REASON IS RETURNED EITHER WAY, so a
    refusal can be logged with the thing that caused it rather than as a bare
    False. A DM nobody can explain afterwards is exactly the action this bot
    must not take, and that applies to the ones it declines as much as the ones
    it sends.
    """
    if not config.SALES_DMS_ENABLED:
        return False, "SALES_DMS_ENABLED is off — the DM ban is fully in force"

    reason = str(dm_reason or "").strip()
    if reason not in DM_REASONS:
        return False, (
            f"{reason or '(none)'} is not a recognised DM reason; the only two are "
            + " and ".join(DM_REASONS)
        )

    if not config.may_dm(user_id):
        return False, (
            f"user {user_id} is not in TEAM_ROSTER_IDS — this bot never contacts "
            "anyone outside the team, and a DM is the one path where that would be "
            "invisible to everybody but the recipient"
        )

    if item_in_channel_today and not config.DM_SAME_DAY_AS_CHANNEL:
        return False, (
            "this item already went to the channel today; the same thing in a DM as "
            "well reads as the bot asking twice"
        )

    if reason == DM_REASON_OVERDUE:
        need = max(0, int(config.DM_OVERDUE_DAYS))
        if int(overdue_days or 0) < need:
            return False, (
                f"{int(overdue_days or 0)} day(s) overdue is below DM_OVERDUE_DAYS "
                f"({need})"
            )
        return True, f"{int(overdue_days)} day(s) overdue, at or past DM_OVERDUE_DAYS ({need})"

    rungs = list(config.DM_MEETING_FOLLOWUP_RUNGS or ())
    if int(rung or 0) not in rungs:
        return False, (
            f"meeting-follow-up rung {int(rung or 0)} is not one of the DM rungs "
            f"({', '.join(str(r) for r in rungs) or 'none'})"
        )
    return True, f"R9 follow-up rung {int(rung)}, which is a DM rung"


async def send(
    destination,
    text: str,
    *,
    reason: str,
    kind: str = "message",
    reply_to: Optional[discord.Message] = None,
    extra: Optional[dict] = None,
    dm_reason: str = "",
    overdue_days: int = 0,
    rung: int = 0,
    item_in_channel_today: bool = False,
    item_key: str = "",
) -> Optional[discord.Message]:
    """The ONLY way this bot puts text into Discord.

    `reason` is required and is written to the audit log — an action nobody can
    explain afterwards is not an action this bot is allowed to take.

    Refuses, loudly and without falling back to any other destination:
      - a DM, UNLESS `dm_reason` names one of the two recognised exceptions and
        every condition on it holds (rule 1, `may_dm`),
      - any channel outside SALES_CHANNEL_IDS (rule 3). The leave channel is
        readable but NOT sendable, and that is checked here by asking
        `config.is_sales_channel` directly rather than `may_read`.

    THE DM ARGUMENTS ARE ALL EXPLICIT AND ALL DEFAULT TO REFUSAL. A caller that
    does not know about the exception cannot trip it: no `dm_reason` means no
    DM, exactly as before this existed.

    Returns the sent message, or None when the send was refused or Discord
    rejected it. Never raises on a Discord failure — a failed post is logged and
    audited, not propagated into the sweeper.
    """
    if _is_dm(destination):
        recipient = getattr(destination, "id", destination)
        allowed, why = may_dm(
            recipient, dm_reason=dm_reason, overdue_days=overdue_days,
            rung=rung, item_in_channel_today=item_in_channel_today,
        )
        if not allowed:
            log.error(
                "[guardrails] REFUSED: attempt to DM %r (kind=%s reason=%s). %s",
                recipient, kind, reason, why,
            )
            state.audit(
                "send_refused",
                reason="hard guardrail: " + why,
                kind=kind,
                attempted_reason=reason,
                dm_reason=dm_reason or "(none)",
                destination=str(recipient),
                item_key=item_key,
            )
            return None

        # ALLOWED — AND AUDITED BEFORE IT IS SENT, not after. A DM that fails
        # mid-send still happened as far as intent goes, and nobody but the
        # recipient can see one. The record of WHY it was permitted is the only
        # thing standing between this exception and an unreviewable channel.
        log.warning(
            "[guardrails] DM PERMITTED to %r (kind=%s reason=%s): %s",
            recipient, kind, reason, why,
        )
        state.audit(
            "dm_permitted",
            reason=reason,
            kind=kind,
            dm_reason=dm_reason,
            why_allowed=why,
            destination=str(recipient),
            overdue_days=int(overdue_days or 0),
            rung=int(rung or 0),
            item_key=item_key,
        )
        body = sanitize(text or "").strip()
        if not body:
            log.info("[guardrails] nothing to DM (empty body) for kind=%s", kind)
            return None
        try:
            sent = await destination.send(body)
        except discord.DiscordException:
            log.exception("[guardrails] Discord rejected the DM to %r (kind=%s)",
                          recipient, kind)
            state.audit("dm_failed", reason=reason, kind=kind,
                        destination=str(recipient), item_key=item_key)
            return None
        state.audit(
            "dm_sent", reason=reason, kind=kind, dm_reason=dm_reason,
            destination=str(recipient), message_id=str(getattr(sent, "id", "")),
            item_key=item_key,
        )
        return sent

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

"""THE STATE CONTRACT — what a supervising process (COSA) can read about this bot.

Two files under STATE_DIR (default ./state), both plain UTF-8, both written by
this bot and read by anyone:

  summary.json   A SNAPSHOT, rewritten wholesale at startup and once a day. The
                 answer to "what is this bot, and what is it sitting on right
                 now" without talking to it. Always valid JSON: it is written to
                 a temp file and atomically replaced, so a reader never catches
                 it half-written.

  audit.jsonl    An APPEND-ONLY log, one JSON object per line, of every action
                 the bot took — every message sent, every nudge, every flag, and
                 every refusal — each with a UTC timestamp and a reason. Never
                 rewritten, never reordered. A reader can tail it.

Both schemas are documented in README.md ("State contract"). Treat them as a
published interface: ADD fields freely, but don't rename or repurpose existing
ones without updating the README, because something else is parsing them.

Everything here is best-effort and never raises into the caller. A bot that
crashes because it couldn't write its own audit line is worse than one that logs
the failure and keeps working — but the failure IS logged at ERROR, because a
silently missing audit trail is its own kind of incident.
"""
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

import config

log = logging.getLogger(__name__)

# Bump when a field changes meaning, so a consumer can tell contracts apart.
SCHEMA_VERSION = 1

SUMMARY_FILENAME = "summary.json"
AUDIT_FILENAME = "audit.jsonl"


def _utcnow_iso() -> str:
    """UTC, second precision, explicit 'Z'. Every timestamp in both files uses
    this exact shape so a consumer needs one parser."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _state_dir() -> str:
    return (config.STATE_DIR or "./state").strip() or "./state"


def _ensure_dir() -> Optional[str]:
    """Create STATE_DIR if needed and return it; None when it can't be created
    (in which case the caller degrades to logging only)."""
    path = _state_dir()
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        log.exception("[state] cannot create state dir %r; state files will not be written", path)
        return None


def summary_path() -> str:
    return os.path.join(_state_dir(), SUMMARY_FILENAME)


def audit_path() -> str:
    return os.path.join(_state_dir(), AUDIT_FILENAME)


# -- audit.jsonl --------------------------------------------------------------


def audit(event: str, *, reason: str, **fields) -> None:
    """Append ONE action record to audit.jsonl.

    `event` is the action's type (message_sent, nudge_sent, chase_opened,
    chase_closed, chase_given_up, flag_raised, send_refused, …) and `reason` is
    why it happened, in plain words. Both are mandatory: this log exists so a
    human — or COSA — can reconstruct what the bot did and why, and a record
    without a reason fails that test.

    Extra keyword fields ride along verbatim (channel_id, message_id, person,
    what, attempt, …). Anything unserialisable is coerced to its string form
    rather than losing the record.
    """
    record = {
        "ts": _utcnow_iso(),
        "bot": config.COS_NAME,
        "event": str(event),
        "reason": str(reason),
    }
    for key, value in (fields or {}).items():
        if value is None:
            continue
        record[key] = value

    directory = _ensure_dir()
    if directory is None:
        log.error("[state][audit-unwritten] %s", record)
        return

    line = json.dumps(record, ensure_ascii=False, default=str)
    try:
        # Line-buffered append. Each write is one complete line terminated by
        # "\n", so a tailing reader never sees a partial record.
        with open(os.path.join(directory, AUDIT_FILENAME), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        log.exception("[state] could not append to audit log; the record was: %s", line)


# -- summary.json -------------------------------------------------------------


def write_summary(
    *,
    source_statuses: list[dict],
    open_chases: list[dict],
    notable_events: list[dict],
    trigger: str,
) -> Optional[str]:
    """Rewrite summary.json wholesale. Returns the path written, or None.

    `trigger` records WHY this rewrite happened ("startup" | "daily") so a reader
    can tell a fresh boot from a scheduled refresh.

    Written to a temp file in the same directory and atomically renamed over the
    old one, so a concurrent reader always sees a complete document — either the
    previous snapshot or the new one, never a mix.
    """
    directory = _ensure_dir()
    if directory is None:
        return None

    doc = {
        "schema_version": SCHEMA_VERSION,
        "bot_name": config.COS_NAME,
        "generated_at": _utcnow_iso(),
        "trigger": str(trigger),
        "scope": {
            # What the bot is allowed to touch — the supervisor's first question.
            "sales_channel_ids": [str(c) for c in config.SALES_CHANNEL_IDS],
            "ask_channel_id": str(config.SALES_ASK_CHANNEL_ID or ""),
            "roster_size": len(config.TEAM_ROSTER_IDS),
            "never_dms": True,
        },
        "sources": list(source_statuses or []),
        "open_chases": list(open_chases or []),
        "notable_events": list(notable_events or []),
        "counts": {
            "sources_connected": sum(
                1 for s in (source_statuses or []) if s.get("status") == "connected"
            ),
            "sources_awaiting_access": sum(
                1 for s in (source_statuses or []) if s.get("status") == "awaiting-access"
            ),
            # Readable but going stale (e.g. the notes folder is fine, the rclone
            # sync filling it is failing). Counted separately from both of the
            # above: a supervisor that sees only connected/awaiting would read a
            # degraded source as healthy.
            "sources_degraded": sum(
                1 for s in (source_statuses or []) if s.get("status") == "degraded"
            ),
            "open_chases": len(open_chases or []),
            "notable_events": len(notable_events or []),
        },
    }

    target = os.path.join(directory, SUMMARY_FILENAME)
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".summary-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2, default=str)
                f.write("\n")
            os.replace(tmp, target)
        except BaseException:
            # Don't leave a half-written temp behind on any failure path.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        log.exception("[state] could not write %s", target)
        return None

    log.info(
        "[state] wrote %s (trigger=%s sources=%d open_chases=%d notable=%d)",
        target, trigger, len(source_statuses or []), len(open_chases or []),
        len(notable_events or []),
    )
    return target


def read_summary() -> Optional[dict]:
    """The last snapshot written, or None when there isn't one / it's unreadable.
    Provided so this process can answer questions about its own state without a
    second source of truth."""
    try:
        with open(summary_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.debug("[state] no readable summary.json yet", exc_info=True)
        return None

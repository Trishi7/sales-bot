"""SQLite-backed state for the sales bot (config.DB_PATH, default sales_bot.db).

A FRESH database, not the PM bot's. There is no approval queue here, no ticket
mapping and no classification record, because this bot files nothing — its only
output is a Discord message. Three tables:

1. `chases` — the deadline chasing this bot exists to do. Someone said in a sales
   channel "I'll send Acme the deck tomorrow"; that is a promise with an implied
   due time and nobody tracking it. A row records who owes what, by when, how
   many times we've asked, and how it ended. The ONLY effect a row can ever have
   is a reminder posted in the channel the promise was made in.

2. `nudges` — THE RATE LIMIT, and the reason chasing is capped from day one. The
   bot posts unprompted @-mentions on the chase path; what stops that becoming a
   nag is that every nudge must check this log before it goes out and record
   itself after. `(target_key, subject_key, kind)` identifies "this person, about
   this promise, for this reason" and is never repeated inside
   COS_NUDGE_WINDOW_HOURS. See `was_nudged_since` / `record_nudge`.

3. `meta` — the bot's own bookkeeping (e.g. the date the daily state summary was
   last written), so a restart doesn't redo daily work.

All timestamps are UTC 'YYYY-MM-DD HH:MM:SS' strings — the same shape SQLite's
CURRENT_TIMESTAMP writes — so they sort and compare correctly as plain strings.
"""
import logging
import sqlite3
from contextlib import contextmanager
from typing import Optional

log = logging.getLogger(__name__)


SCHEMA = """
-- One row per commitment the bot is waiting on. `due_at` is when we may FIRST
-- nudge, derived from what the person said ("tomorrow" → +1 day).
-- status: open   — still waiting
--         closed — they came back (replied, or someone ✅'d the nudge)
--         stale  — chased COS_NUDGE_MAX_ATTEMPTS times with no answer; we stop.
--                  Stopping is deliberate: a bot that keeps asking forever is a
--                  bot people mute.
CREATE TABLE IF NOT EXISTS chases (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          TEXT NOT NULL,
    message_id          TEXT NOT NULL UNIQUE,
    jump_url            TEXT,
    person_id           TEXT,
    person_name         TEXT NOT NULL,
    what                TEXT NOT NULL,
    promised_at         TIMESTAMP NOT NULL,
    due_at              TIMESTAMP NOT NULL,
    status              TEXT NOT NULL,
    reminders_sent      INTEGER NOT NULL DEFAULT 0,
    last_reminder_id    TEXT,
    last_reminded_at    TIMESTAMP,
    flagged             INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_chase_due ON chases(status, due_at);
CREATE INDEX IF NOT EXISTS ix_chase_reminder ON chases(last_reminder_id);

-- One row per nudge the bot has POSTED. This table IS the rate limit; see the
-- module docstring. `target_key` identifies the audience ("person:<id_or_name>")
-- and `subject_key` the thing being chased ("chase:<id>").
CREATE TABLE IF NOT EXISTS nudges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_key    TEXT NOT NULL,
    subject_key   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    channel_id    TEXT,
    message_id    TEXT,
    sent_at       TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_nudges_key
    ON nudges(target_key, subject_key, kind, sent_at);

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


# Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` won't
# alter an existing table, so an already-created sales_bot.db needs them
# back-filled. (table, column, DDL) — each added only if missing. Idempotent.
_MIGRATIONS: list[tuple[str, str, str]] = []


class DB:
    def __init__(self, path: str) -> None:
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)
        log.info("[db] ready at %s", path)

    @staticmethod
    def _migrate(c) -> None:
        """Add any columns introduced after a table first shipped, so an existing
        DB is upgraded in place. Safe on every startup."""
        for table, column, ddl in _MIGRATIONS:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                log.info("[db] migrating: adding %s.%s", table, column)
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # -- chases (the commitments the bot is waiting on) --------------------
    # Read/propose only: a chase produces a REMINDER to a human in the channel
    # the promise was made in, and nothing else.

    def record_chase(
        self,
        *,
        channel_id: int,
        message_id: int,
        jump_url: str,
        person_id: Optional[int],
        person_name: str,
        what: str,
        promised_at: str,
        due_at: str,
    ) -> bool:
        """Remember that `person_name` promised `what`, chaseable from `due_at`.

        Keyed on the promising message, so re-processing that message (an edit, a
        restart replay) can never create a second chase for one promise. Returns
        True when a new row was inserted, False when we already had it."""
        with self.conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO chases
                    (channel_id, message_id, jump_url, person_id, person_name,
                     what, promised_at, due_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    str(channel_id),
                    str(message_id),
                    jump_url,
                    str(person_id) if person_id else None,
                    person_name,
                    what,
                    str(promised_at),
                    str(due_at),
                ),
            )
            return cur.rowcount > 0

    def list_due_chases(self, *, now: str, reminder_cutoff: str) -> list[dict]:
        """Chases ready to be nudged: due at/before `now`, and either never
        nudged or last nudged at/before `reminder_cutoff` (the cooldown). Both
        are UTC 'YYYY-MM-DD HH:MM:SS'. Oldest-due first, so the most overdue
        promise is surfaced before a fresher one."""
        with self.conn() as c:
            rows = c.execute(
                """
                SELECT * FROM chases
                WHERE status = 'open'
                  AND due_at <= ?
                  AND (last_reminded_at IS NULL OR last_reminded_at <= ?)
                ORDER BY due_at ASC
                """,
                (str(now), str(reminder_cutoff)),
            ).fetchall()
            return [self._chase_row(r) for r in rows]

    def list_open_chases(self, limit: int = 50) -> list[dict]:
        """Every open chase, oldest-due first — the "open chases" block in
        state/summary.json, and the honest answer to "what are you waiting on"."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM chases WHERE status = 'open' ORDER BY due_at ASC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [self._chase_row(r) for r in rows]

    def find_chase_by_reminder(self, reminder_message_id: int) -> Optional[dict]:
        """The chase a given nudge message is about — so a ✅ on that nudge, or a
        reply to it, closes the right one."""
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM chases WHERE last_reminder_id = ? AND status = 'open'",
                (str(reminder_message_id),),
            ).fetchone()
            return self._chase_row(row) if row else None

    def find_chase_by_source(self, message_id: int) -> Optional[dict]:
        """The chase created FROM a given message — so a reply to the original
        promise ("here's that deck") closes it too."""
        with self.conn() as c:
            row = c.execute(
                "SELECT * FROM chases WHERE message_id = ? AND status = 'open'",
                (str(message_id),),
            ).fetchone()
            return self._chase_row(row) if row else None

    def mark_chase_reminded(
        self, chase_id: int, *, reminder_message_id: Optional[int], at: str
    ) -> None:
        with self.conn() as c:
            c.execute(
                """
                UPDATE chases
                SET reminders_sent   = reminders_sent + 1,
                    last_reminder_id = ?,
                    last_reminded_at = ?,
                    updated_at       = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    str(reminder_message_id) if reminder_message_id else None,
                    str(at),
                    chase_id,
                ),
            )

    def set_chase_status(self, chase_id: int, status: str) -> None:
        """'closed' (they came back / someone ✅'d it) or 'stale' (asked enough;
        stop). Anything else is a caller bug and is logged as such."""
        if status not in ("open", "closed", "stale"):
            log.warning("[db] unexpected chase status %r for chase %s", status, chase_id)
        with self.conn() as c:
            c.execute(
                "UPDATE chases SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, chase_id),
            )

    def mark_chase_flagged(self, chase_id: int) -> None:
        """Record that this chase has been surfaced as a FLAG (it went stale and
        was reported in-channel), so the flag is raised exactly once."""
        with self.conn() as c:
            c.execute(
                "UPDATE chases SET flagged = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (chase_id,),
            )

    # -- nudges (THE rate limit) -------------------------------------------
    # The bot posts unprompted @-mentions on the chase path. These two methods
    # are the guardrail that keeps that from becoming a nag: every nudge is
    # checked against the log before it is sent, and recorded after.

    def was_nudged_since(
        self, *, target_key: str, subject_key: str, kind: str, since: str
    ) -> bool:
        """Have we already nudged THIS person about THIS thing for THIS reason
        since `since` (a UTC cooldown boundary)? True → stay quiet.

        Callers must fail CLOSED on an exception — treat an unreadable log as
        "assume we did", because nudging twice is worse than missing one."""
        with self.conn() as c:
            row = c.execute(
                """
                SELECT 1 FROM nudges
                WHERE target_key = ? AND subject_key = ? AND kind = ? AND sent_at >= ?
                LIMIT 1
                """,
                (str(target_key), str(subject_key), str(kind), str(since)),
            ).fetchone()
            return row is not None

    def record_nudge(
        self,
        *,
        target_key: str,
        subject_key: str,
        kind: str,
        channel_id: Optional[int],
        message_id: Optional[int],
        sent_at: str,
    ) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO nudges
                    (target_key, subject_key, kind, channel_id, message_id, sent_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(target_key),
                    str(subject_key),
                    str(kind),
                    str(channel_id) if channel_id else None,
                    str(message_id) if message_id else None,
                    str(sent_at),
                ),
            )

    def count_nudges_since(self, since: str) -> int:
        """How many nudges went out since `since` — reported in the daily state
        summary so a supervisor can see the chase volume without reading the
        whole audit log."""
        with self.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM nudges WHERE sent_at >= ?", (str(since),)
            ).fetchone()
            return int(row["n"]) if row else 0

    # -- meta (the bot's own bookkeeping) ----------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE
                SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, str(value)),
            )

    # -- row mapping -------------------------------------------------------

    @staticmethod
    def _chase_row(row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "channel_id": int(row["channel_id"]),
            "message_id": int(row["message_id"]),
            "jump_url": row["jump_url"] or "",
            "person_id": int(row["person_id"]) if row["person_id"] else None,
            "person_name": row["person_name"],
            "what": row["what"],
            "promised_at": row["promised_at"],
            "due_at": row["due_at"],
            "status": row["status"],
            "reminders_sent": int(row["reminders_sent"] or 0),
            "last_reminder_id": (
                int(row["last_reminder_id"]) if row["last_reminder_id"] else None
            ),
            "last_reminded_at": row["last_reminded_at"],
            "flagged": bool(row["flagged"]),
        }

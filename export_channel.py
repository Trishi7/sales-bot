"""EXPORT ONE DISCORD CHANNEL — its messages and every thread under it — to CSV, JSONL and Markdown.

    ./venv/bin/python export_channel.py <channel_id> [--out DIR] [--since DD-MM-YYYY] [--until DD-MM-YYYY]
                                        [--no-threads] [--format csv|jsonl|md|all]

A ONE-OFF TOOL, NOT A BOT FEATURE. It logs in with the bot's own token (DISCORD_TOKEN, loaded from .env by
python-dotenv exactly as config.py does), reads, writes the files, and exits. It is a plain discord.Client with
the bot's intents, NOT the SalesBot loop: no handler runs, nothing is answered, nothing is scheduled.

READ-ONLY. It never sends a message, never adds a reaction, never touches the sheet or the database. The only
things it writes are the export files under --out (default ./exports, which is gitignored).

THE TOKEN NEVER APPEARS in a print, a log line, a file or an error: every error text goes through `_scrub`
before it is printed, and the token is never formatted into anything.

WHAT IT READS
  - the channel itself: channel.history(limit=None, oldest_first=True) (a forum channel has no history of its
    own; each post is a thread);
  - every thread of it (unless --no-threads): the active ones (channel.threads), the archived public ones and,
    when the bot is allowed to list them, the archived private ones. A thread it cannot read is skipped and
    counted.
  - --since / --until are DAY-FIRST dates (03-04-2026 is 3 April) on message.created_at: --since from 00:00 UTC
    that day, --until to the end of that day UTC.

MEMORY: every record is written to the open files as it arrives. Nothing keeps the history in a list.

EXIT CODES: 0 done; 1 bad arguments, no token or a login failure; 2 the bot cannot see or read the channel.
"""
import argparse
import asyncio
import contextlib
import csv
import json
import os
import re
import sys
from datetime import datetime, time, timedelta, timezone

FIELDS = ["id", "created_at", "edited_at", "author_id", "author_name", "author_display", "is_bot",
          "content", "clean_content", "attachments", "embeds_count", "reply_to_id", "thread_id",
          "thread_name", "pinned", "reactions", "jump_url"]
FORMATS = ("csv", "jsonl", "md")
PROGRESS_EVERY = 500

_TOKEN = {"value": ""}           # held only to scrub it out of error text; never printed


def _scrub(text) -> str:
    """Any text with the token taken out — the last line of defence for error messages."""
    out = str(text or "")
    token = _TOKEN["value"]
    if token:
        out = out.replace(token, "[token]")
    return out


def _say(*parts) -> None:
    print(_scrub(" ".join(str(p) for p in parts)), flush=True)


# -- dates ---------------------------------------------------------------------------------------------------

def parse_day(text: str):
    """A DAY-FIRST date ("03-04-2026", "3/4/2026", "03.04.2026") -> a date. ValueError when unreadable."""
    m = re.fullmatch(r"\s*(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\s*", str(text or ""))
    if not m:
        raise ValueError(f"{text!r} is not a date in DD-MM-YYYY form")
    day, month, year = (int(g) for g in m.groups())
    return datetime(year, month, day).date()


def window(since: str = "", until: str = "") -> tuple:
    """(start, end) as aware UTC datetimes, either one None. --since is that day's 00:00 UTC; --until is the
    last instant of that day UTC."""
    start = datetime.combine(parse_day(since), time.min, tzinfo=timezone.utc) if since else None
    end = (datetime.combine(parse_day(until), time.min, tzinfo=timezone.utc) + timedelta(days=1)
           - timedelta(microseconds=1)) if until else None
    if start and end and start > end:
        raise ValueError("--since is after --until")
    return start, end


def in_window(created_at, start, end) -> bool:
    """Is a message's created_at inside [start, end]? A naive datetime is read as UTC (discord.py's are aware)."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if start and created_at < start:
        return False
    if end and created_at > end:
        return False
    return True


# -- one message -> one record ---------------------------------------------------------------------------------

def _iso(dt) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def record(message, thread=None) -> dict:
    """The export record for one message. `thread` is the thread it was read from, or None for the channel."""
    author = message.author
    ref = getattr(message, "reference", None)
    return {
        "id": str(message.id),
        "created_at": _iso(message.created_at),
        "edited_at": _iso(getattr(message, "edited_at", None)),
        "author_id": str(getattr(author, "id", "")),
        "author_name": str(getattr(author, "name", "")),
        "author_display": str(getattr(author, "display_name", "") or getattr(author, "name", "")),
        "is_bot": bool(getattr(author, "bot", False)),
        "content": message.content or "",
        "clean_content": getattr(message, "clean_content", "") or "",
        "attachments": [{"filename": a.filename, "url": a.url} for a in (message.attachments or [])],
        "embeds_count": len(getattr(message, "embeds", None) or []),
        "reply_to_id": str(ref.message_id) if ref is not None and getattr(ref, "message_id", None) else "",
        "thread_id": str(thread.id) if thread is not None else "",
        "thread_name": str(thread.name) if thread is not None else "",
        "pinned": bool(getattr(message, "pinned", False)),
        "reactions": ";".join(f"{r.emoji}:{r.count}" for r in (getattr(message, "reactions", None) or [])),
        "jump_url": getattr(message, "jump_url", "") or "",
    }


# -- the files -------------------------------------------------------------------------------------------------

def safe_name(text: str) -> str:
    out = re.sub(r"[^\w.-]+", "-", str(text or "channel"), flags=re.UNICODE).strip("-.")
    return out[:80] or "channel"


def file_stem(channel_name: str, channel_id, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{safe_name(channel_name)}_{channel_id}_{now.strftime('%Y%m%d-%H%M')}"


class Writers:
    """The open export files. `write` appends one record to every chosen format, as it arrives."""

    def __init__(self, out_dir: str, stem: str, formats=FORMATS, *, title: str = ""):
        os.makedirs(out_dir, exist_ok=True)
        self.paths = {}
        self._files = {}
        self._csv = None
        self._md_thread = None
        for fmt in formats:
            path = os.path.join(out_dir, f"{stem}.{fmt}")
            self.paths[fmt] = path
            # newline="" for CSV so the csv module controls line endings and a newline inside a message stays
            # inside its quoted field.
            self._files[fmt] = open(path, "w", encoding="utf-8", newline="" if fmt == "csv" else None)
        if "csv" in self._files:
            self._csv = csv.DictWriter(self._files["csv"], fieldnames=FIELDS, quoting=csv.QUOTE_MINIMAL)
            self._csv.writeheader()
        if "md" in self._files and title:
            self._files["md"].write(f"# {title}\n\n")

    def write(self, rec: dict) -> None:
        if "jsonl" in self._files:
            self._files["jsonl"].write(json.dumps(rec, ensure_ascii=False) + "\n")
        if self._csv is not None:
            row = dict(rec)
            row["attachments"] = "; ".join(f"{a['filename']} {a['url']}" for a in rec["attachments"])
            self._csv.writerow(row)
        if "md" in self._files:
            self._files["md"].write(md_line(rec, self) + "\n")

    def thread_heading(self, name: str) -> str:
        """The heading line for a new thread (written once, before its first message)."""
        return f"\n### Thread: {name}\n"

    def close(self) -> None:
        for f in self._files.values():
            with contextlib.suppress(Exception):
                f.close()


def md_line(rec: dict, writers=None) -> str:
    """`[DD-MM-YYYY HH:MM] author_display: content`, attachments as `(attachment: filename url)`. A thread's
    first message is preceded by its `### Thread: <name>` heading. A multi-line message stays on one line (its
    line breaks become " / ")."""
    when = datetime.fromisoformat(rec["created_at"]).strftime("%d-%m-%Y %H:%M")
    text = " / ".join(part for part in (rec["content"] or "").splitlines() if part.strip())
    for a in rec["attachments"]:
        text = (text + " " if text else "") + f"(attachment: {a['filename']} {a['url']})"
    line = f"[{when}] {rec['author_display']}: {text}"
    if writers is not None and rec["thread_id"] and rec["thread_id"] != writers._md_thread:
        writers._md_thread = rec["thread_id"]
        line = writers.thread_heading(rec["thread_name"]) + line
    return line


# -- reading -----------------------------------------------------------------------------------------------------

class Tally:
    def __init__(self, say=_say):
        self.main = 0
        self.in_threads = 0
        self.threads_read = 0
        self.threads_skipped = 0
        self.notes = []
        self._say = say

    @property
    def total(self) -> int:
        return self.main + self.in_threads

    def tick(self) -> None:
        if self.total and self.total % PROGRESS_EVERY == 0:
            self._say(f"  ... {self.total} messages exported")


def missing_permission(channel, me) -> str:
    """"View Channel" / "Read Message History" when the bot lacks it on this channel, else ""."""
    perms_for = getattr(channel, "permissions_for", None)
    if perms_for is None or me is None:
        return ""
    perms = perms_for(me)
    if not getattr(perms, "view_channel", True):
        return "View Channel"
    if not getattr(perms, "read_message_history", True):
        return "Read Message History"
    return ""


async def _threads_of(channel, tally: Tally):
    """Every thread of `channel`, once each: active, archived public, archived private (when allowed)."""
    import discord

    seen = set()
    for t in list(getattr(channel, "threads", None) or []):
        if t.id not in seen:
            seen.add(t.id)
            yield t
    lister = getattr(channel, "archived_threads", None)
    if lister is None:
        return
    for private in (False, True):
        kind = "private" if private else "public"
        try:
            async for t in (lister(private=True, limit=None) if private else lister(limit=None)):
                if t.id not in seen:
                    seen.add(t.id)
                    yield t
        except (discord.Forbidden, discord.HTTPException) as e:
            tally.notes.append(f"archived {kind} threads not listed ({_scrub(getattr(e, 'text', '') or e)}; "
                               f"{'needs Manage Threads' if private else 'needs Read Message History'})")


async def export(channel, writers: Writers, *, start=None, end=None, threads=True, say=_say) -> Tally:
    """Read `channel` (and its threads) into `writers`. Returns the counts. Raises discord.Forbidden only when
    the channel's own history cannot be read."""
    import discord

    tally = Tally(say)
    history = getattr(channel, "history", None)
    if history is not None:
        async for message in history(limit=None, oldest_first=True):
            if not in_window(message.created_at, start, end):
                continue
            writers.write(record(message))
            tally.main += 1
            tally.tick()
    if not threads:
        return tally
    async for thread in _threads_of(channel, tally):
        try:
            async for message in thread.history(limit=None, oldest_first=True):
                if not in_window(message.created_at, start, end):
                    continue
                writers.write(record(message, thread))
                tally.in_threads += 1
                tally.tick()
            tally.threads_read += 1
        except (discord.Forbidden, discord.NotFound):
            tally.threads_skipped += 1
            say(f"  skipped thread {getattr(thread, 'name', thread.id)!r}: no access")
    return tally


# -- the run -----------------------------------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Export one Discord channel (and its threads). Read-only.")
    p.add_argument("channel_id", type=int)
    p.add_argument("--out", default="./exports")
    p.add_argument("--since", default="", help="DD-MM-YYYY, from 00:00 UTC that day")
    p.add_argument("--until", default="", help="DD-MM-YYYY, to the end of that day UTC")
    p.add_argument("--no-threads", action="store_true")
    p.add_argument("--format", default="all", choices=("csv", "jsonl", "md", "all"))
    return p.parse_args(argv)


def intents():
    """THE BOT'S OWN INTENTS (bot.py SalesBot.__init__): the defaults (guilds among them), message_content and
    reactions. bot.py does not set members, so neither does this."""
    import discord

    i = discord.Intents.default()
    i.message_content = True
    i.reactions = True
    return i


def _no_access(channel_id, perm: str) -> int:
    _say(f"ERROR: the bot cannot read channel {channel_id}: it is missing the {perm} permission there "
         "(give the bot's role View Channel and Read Message History on that channel).")
    return 2


async def run(args, token: str) -> int:
    import discord

    try:
        start, end = window(args.since, args.until)
    except ValueError as e:
        _say(f"ERROR: {e}")
        return 1
    formats = FORMATS if args.format == "all" else (args.format,)
    client = discord.Client(intents=intents())
    result = {"code": 1}

    async def job():
        try:
            channel = client.get_channel(args.channel_id)
            if channel is None:
                try:
                    channel = await client.fetch_channel(args.channel_id)
                except discord.Forbidden:
                    result["code"] = _no_access(args.channel_id, "View Channel")
                    return
                except discord.NotFound:
                    _say(f"ERROR: channel {args.channel_id} was not found, or the bot cannot see it "
                         "(missing the View Channel permission).")
                    result["code"] = 2
                    return
            guild = getattr(channel, "guild", None)
            perm = missing_permission(channel, getattr(guild, "me", None))
            if perm:
                result["code"] = _no_access(args.channel_id, perm)
                return
            name = getattr(channel, "name", None) or str(args.channel_id)
            kind = "forum channel" if isinstance(channel, discord.ForumChannel) else "channel"
            writers = Writers(args.out, file_stem(name, args.channel_id), formats,
                              title=f"#{name} ({args.channel_id})")
            _say(f"exporting {kind} #{name} ({args.channel_id})"
                 + (f" from {args.since}" if args.since else "") + (f" to {args.until}" if args.until else ""))
            try:
                tally = await export(channel, writers, start=start, end=end, threads=not args.no_threads)
            except discord.Forbidden:
                result["code"] = _no_access(args.channel_id, "Read Message History")
                return
            finally:
                writers.close()
            _say("\ndone")
            _say(f"  messages in the main channel: {tally.main}")
            _say(f"  threads read:                 {tally.threads_read}")
            _say(f"  threads skipped:              {tally.threads_skipped}")
            _say(f"  messages in threads:          {tally.in_threads}")
            for note in tally.notes:
                _say(f"  note: {note}")
            for fmt, path in writers.paths.items():
                _say(f"  {fmt:<5} {os.path.abspath(path)}")
            result["code"] = 0
        except Exception as e:  # noqa: BLE001 — one clear line, never a traceback that could carry the token
            _say(f"ERROR: {type(e).__name__}: {_scrub(e)}")
            result["code"] = 1
        finally:
            await client.close()

    @client.event
    async def on_ready():
        await job()

    try:
        await client.start(token)
    except discord.LoginFailure:
        _say("ERROR: Discord rejected the bot token (DISCORD_TOKEN in .env).")
        return 1
    except Exception as e:  # noqa: BLE001
        _say(f"ERROR: could not connect to Discord: {type(e).__name__}: {_scrub(e)}")
        return 1
    return result["code"]


def main(argv=None) -> int:
    args = parse_args(argv)
    # THE SAME LOADER AND THE SAME VARIABLE AS config.py: python-dotenv, then DISCORD_TOKEN.
    from dotenv import load_dotenv

    load_dotenv()
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        _say("ERROR: DISCORD_TOKEN is not set (it is read from .env, as the bot reads it).")
        return 1
    _TOKEN["value"] = token
    return asyncio.run(run(args, token))


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())

"""export_channel.py, dry: a fake channel with fake messages and threads, no Discord login, no token.

Checks the record fields, the CSV / JSONL / Markdown writers, the day-first date filter, threads (read, skipped,
archived private not listable), the forum shape (no history of its own), the missing-permission check, and that
the script has no way to send, react or write to the sheet or the database.
"""
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import discord  # noqa: E402

import export_channel as ex  # noqa: E402


def utc(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def msg(mid, when, text="hello", *, author="Ada", bot=False, attachments=(), reply_to=None, reactions=(),
        edited=None, pinned=False, embeds=0):
    return SimpleNamespace(
        id=mid, created_at=when, edited_at=edited, content=text, clean_content=text.replace("<@1>", "@Ada"),
        author=SimpleNamespace(id=100 + len(author), name=author.lower(), display_name=author, bot=bot),
        attachments=[SimpleNamespace(filename=f, url=f"https://cdn.example/{f}") for f in attachments],
        embeds=[object()] * embeds,
        reference=SimpleNamespace(message_id=reply_to) if reply_to else None,
        pinned=pinned,
        reactions=[SimpleNamespace(emoji=e, count=c) for e, c in reactions],
        jump_url=f"https://discord.com/channels/1/2/{mid}",
    )


def history_of(messages, *, forbidden=False):
    def history(limit=None, oldest_first=True):
        assert limit is None and oldest_first is True

        async def gen():
            if forbidden:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
            for m in messages:
                yield m
        return gen()
    return history


class FakeThread(SimpleNamespace):
    pass


def thread(tid, name, messages, *, forbidden=False):
    return FakeThread(id=tid, name=name, history=history_of(messages, forbidden=forbidden))


def channel(messages, *, active=(), archived=(), private_forbidden=True, forum=False):
    def archived_threads(private=False, limit=None):
        assert limit is None

        async def gen():
            if private and private_forbidden:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
            for t in ([] if private else archived):
                yield t
        return gen()

    ch = SimpleNamespace(id=2, name="sales", threads=list(active), archived_threads=archived_threads)
    if not forum:
        ch.history = history_of(messages)
    return ch


MAIN = [
    msg(1, utc(2026, 4, 2, 23, 59), "before the window"),
    msg(2, utc(2026, 4, 3, 0, 0), "line one\nline two, with \"quotes\"", attachments=["deck.pdf"],
        reactions=[("👍", 2), ("🎉", 1)], pinned=True, embeds=1),
    msg(3, utc(2026, 4, 5, 23, 59), "hi <@1>", author="Saley", bot=True, reply_to=2, edited=utc(2026, 4, 6)),
    msg(4, utc(2026, 4, 6, 0, 0), "after the window"),
]


def run(ch, tmp_path, **kw):
    w = ex.Writers(str(tmp_path), "sales_2_20260415-1200", title="#sales (2)")
    said = []
    try:
        tally = asyncio.run(ex.export(ch, w, say=said.append, **kw))
    finally:
        w.close()
    return tally, w.paths, said


# -- dates ------------------------------------------------------------------------------------------------------

def test_dates_are_day_first_and_cover_whole_utc_days():
    assert ex.parse_day("03-04-2026").isoformat() == "2026-04-03"
    assert ex.parse_day("3/4/2026").isoformat() == "2026-04-03"
    start, end = ex.window("03-04-2026", "05-04-2026")
    assert start == utc(2026, 4, 3, 0, 0)
    assert end == datetime(2026, 4, 5, 23, 59, 59, 999999, tzinfo=timezone.utc)
    assert ex.in_window(utc(2026, 4, 3, 0, 0), start, end)
    assert ex.in_window(utc(2026, 4, 5, 23, 59), start, end)
    assert not ex.in_window(utc(2026, 4, 2, 23, 59), start, end)
    assert not ex.in_window(utc(2026, 4, 6, 0, 0), start, end)
    assert ex.in_window(utc(2020, 1, 1), None, None)
    for bad in ("2026-04-03", "04/2026", "31-02-2026"):
        with pytest.raises(ValueError):
            ex.window(bad, "")
    with pytest.raises(ValueError):
        ex.window("05-04-2026", "03-04-2026")


# -- the files --------------------------------------------------------------------------------------------------

def test_the_writers_and_the_filter_on_a_fake_channel(tmp_path):
    start, end = ex.window("03-04-2026", "05-04-2026")
    tally, paths, _ = run(channel(MAIN), tmp_path, start=start, end=end, threads=False)
    assert (tally.main, tally.in_threads) == (2, 0)

    lines = open(paths["jsonl"], encoding="utf-8").read().splitlines()
    recs = [json.loads(l) for l in lines]
    assert [r["id"] for r in recs] == ["2", "3"]
    assert all(list(r) == ex.FIELDS for r in recs)
    first, second = recs
    assert first["created_at"] == "2026-04-03T00:00:00+00:00"
    assert first["content"] == "line one\nline two, with \"quotes\""
    assert first["attachments"] == [{"filename": "deck.pdf", "url": "https://cdn.example/deck.pdf"}]
    assert (first["reactions"], first["pinned"], first["embeds_count"]) == ("👍:2;🎉:1", True, 1)
    assert (second["is_bot"], second["reply_to_id"], second["edited_at"]) == (True, "2", "2026-04-06T12:00:00+00:00")
    assert (second["content"], second["clean_content"]) == ("hi <@1>", "hi @Ada")
    assert second["jump_url"].endswith("/3") and second["thread_id"] == ""

    with open(paths["csv"], encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == ex.FIELDS
    assert [r["id"] for r in rows] == ["2", "3"]
    assert rows[0]["content"] == "line one\nline two, with \"quotes\""        # the newline stayed inside its field
    assert rows[0]["attachments"] == "deck.pdf https://cdn.example/deck.pdf"
    assert rows[1]["is_bot"] == "True"

    md = open(paths["md"], encoding="utf-8").read().splitlines()
    assert md[0] == "# #sales (2)"
    assert ("[03-04-2026 00:00] Ada: line one / line two, with \"quotes\" (attachment: deck.pdf "
            "https://cdn.example/deck.pdf)") in md
    assert "[05-04-2026 23:59] Saley: hi <@1>" in md


def test_threads_are_read_skipped_and_headed(tmp_path):
    active = thread(10, "Acme follow-up", [msg(11, utc(2026, 4, 4), "in a thread")])
    locked = thread(20, "Private", [], forbidden=True)
    old = thread(30, "Old thread", [msg(31, utc(2026, 3, 1), "archived"), msg(32, utc(2026, 4, 4), "kept")])
    start, end = ex.window("03-04-2026", "")
    tally, paths, said = run(channel(MAIN, active=[active, locked], archived=[old]), tmp_path, start=start, end=end)
    assert (tally.main, tally.in_threads, tally.threads_read, tally.threads_skipped) == (3, 2, 2, 1)
    assert any("Private" in s for s in said)
    assert any("private threads not listed" in n for n in tally.notes)
    recs = [json.loads(l) for l in open(paths["jsonl"], encoding="utf-8")]
    assert [(r["id"], r["thread_id"], r["thread_name"]) for r in recs if r["thread_id"]] == \
        [("11", "10", "Acme follow-up"), ("32", "30", "Old thread")]
    md = open(paths["md"], encoding="utf-8").read()
    assert md.index("### Thread: Acme follow-up") < md.index("in a thread") < md.index("### Thread: Old thread")
    assert md.count("### Thread:") == 2


def test_no_threads_flag(tmp_path):
    t = thread(10, "x", [msg(11, utc(2026, 4, 4))])
    tally, _, _ = run(channel(MAIN, active=[t]), tmp_path, threads=False)
    assert (tally.main, tally.threads_read, tally.in_threads) == (4, 0, 0)


def test_a_forum_channel_is_its_posts(tmp_path):
    post = thread(40, "Post one", [msg(41, utc(2026, 4, 4), "first post")])
    tally, _, _ = run(channel([], active=[post], forum=True), tmp_path)
    assert (tally.main, tally.threads_read, tally.in_threads) == (0, 1, 1)


def test_progress_every_500(tmp_path):
    many = [msg(i, utc(2026, 4, 4)) for i in range(1, 1001)]
    _, _, said = run(channel(many), tmp_path, threads=False)
    assert [s for s in said if "exported" in s] == ["  ... 500 messages exported", "  ... 1000 messages exported"]


def test_file_names_and_formats(tmp_path):
    stem = ex.file_stem("sales talk!", 123, datetime(2026, 10, 15, 9, 5, tzinfo=timezone.utc))
    assert stem == "sales-talk_123_20261015-0905"
    w = ex.Writers(str(tmp_path / "new" / "dir"), stem, ("csv",))
    w.close()
    assert list(w.paths) == ["csv"] and os.path.exists(w.paths["csv"])
    args = ex.parse_args(["42"])
    assert (args.out, args.format, args.no_threads, args.since) == ("./exports", "all", False, "")


# -- access, and read-only ----------------------------------------------------------------------------------------

def test_missing_permission_is_named():
    def ch(**perms):
        return SimpleNamespace(permissions_for=lambda me: SimpleNamespace(**perms))
    me = object()
    assert ex.missing_permission(ch(view_channel=False, read_message_history=False), me) == "View Channel"
    assert ex.missing_permission(ch(view_channel=True, read_message_history=False), me) == "Read Message History"
    assert ex.missing_permission(ch(view_channel=True, read_message_history=True), me) == ""


def test_the_history_of_the_channel_itself_forbidden_is_raised(tmp_path):
    ch = channel([])
    ch.history = history_of([], forbidden=True)
    with pytest.raises(discord.Forbidden):
        run(ch, tmp_path)


def test_no_token_means_a_clear_error_and_no_login(monkeypatch, capsys):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    assert ex.main(["42"]) == 1
    assert "DISCORD_TOKEN is not set" in capsys.readouterr().out


def test_the_token_is_scrubbed_from_error_text():
    ex._TOKEN["value"] = "abc.def.secret"
    try:
        assert ex._scrub("failed with abc.def.secret here") == "failed with [token] here"
    finally:
        ex._TOKEN["value"] = ""


def test_it_is_read_only_and_uses_the_bots_intents():
    src = open(os.path.join(REPO, "export_channel.py"), encoding="utf-8").read()
    for forbidden in (".send(", "add_reaction", ".reply(", ".edit(", ".delete(", "import gtm_sheet", "sheetwrite",
                      "import db", "from db", "sqlite3", "import bot", "from bot", "print(token", "log.info(token"):
        assert forbidden not in src, forbidden
    i = ex.intents()
    assert (i.message_content, i.reactions, i.guilds, i.members) == (True, True, True, False)


def test_exports_are_gitignored():
    lines = open(os.path.join(REPO, ".gitignore"), encoding="utf-8").read().splitlines()
    assert "exports/" in lines

"""NFT2-1062 — the pure cases: the title table, the routing table, the D4 sentences,
the standup floor, split_visible. No files, no network, no sheet.

Written from docs/plans/NFT2-1062.md sections 2.3, 2.5, 2.6 and 2.7. The offline
end-to-end checks (the real sync, the quarantine, the tools) are in
verify_notes_scope.py.
"""
import pytest

import notes
import toolsets

FLOOR = {"am sync", "pm sync", "nfthing kick-off", "nfthing wrap-up", "standup", "stand-up",
         "daily sync"}


def _norm(items):
    return {notes._norm_title(p) for p in items}


# -- 2.3 the title table ------------------------------------------------------

STANDUP_TITLES = [
    "NFThing Kick-off ( AM Sync) – 2026／10／05 10：30 IST – Notes by Gemini",
    "NFThing Wrap-up ( PM Sync) – 2026／10／05 18：30 IST – Notes by Gemini",
    "AM-Sync 2026-10-05", "am  sync", "AM_SYNC", "Daily Stand-up", "daily stand up",
    "AM Standup 2026-10-05", "Daily sync 2026-10-05",
]
SALES_TITLES = [
    "Sales sync – 2026／10／05 12：00 IST", "Team sync 2026-10-05", "Acme sync 2026-10-05",
    "PM Call – 2026／08／19 11：20 IST – Notes by Gemini",
    "Program sync 2026-10-05", "NFThing Kickoff – 2026／10／05",
    "Acme discovery call – 2026／10／05 15：00 IST",
]


@pytest.mark.parametrize("title", STANDUP_TITLES)
def test_standup_titles_are_refused(title):
    assert notes.is_standup_title(title) is True


@pytest.mark.parametrize("title", SALES_TITLES)
def test_sales_titles_are_not_refused(title):
    assert notes.is_standup_title(title) is False


def test_a_bare_sync_is_not_in_the_guard():
    assert "sync" not in notes.standup_patterns()
    assert notes.is_standup_title("Sales sync") is False


def test_the_floor_is_exactly_the_seven():
    assert _norm(notes._STANDUP_FLOOR) == _norm(FLOOR)
    assert len(notes._STANDUP_FLOOR) == 7


def test_the_floor_survives_an_empty_or_old_config(monkeypatch):
    import config
    for value in ([], ["AM sync", "PM sync"]):
        monkeypatch.setattr(config, "NOTES_EXCLUDE_TITLE_PATTERNS", value, raising=False)
        assert _norm(FLOOR) <= set(notes.standup_patterns())
        assert notes.is_standup_title("NFThing Kick-off ( AM Sync) – 2026／10／05") is True


def test_config_adds_to_the_floor(monkeypatch):
    import config
    monkeypatch.setattr(config, "NOTES_EXCLUDE_TITLE_PATTERNS", ["board meeting"], raising=False)
    assert notes.is_standup_title("Board Meeting 2026-10-05") is True
    assert notes.is_standup_title("AM sync") is True


# -- 2.5 the D4 sentences, byte for byte ---------------------------------------

def test_the_three_sentences_are_exact():
    assert notes.SAY_NOT_CONNECTED == "Meeting notes aren't connected to me yet."
    assert notes.SAY_UNREACHABLE == (
        "I can't reach the sales notes folder right now, so I haven't checked the notes.")
    assert notes.SAY_EMPTY.format(folder="Saley – Sales Notes") == (
        "There are no sales meeting notes in the Saley – Sales Notes folder yet.")
    for s in (notes.SAY_NOT_CONNECTED, notes.SAY_UNREACHABLE, notes.SAY_EMPTY):
        assert "’" not in s            # straight apostrophes only


def test_no_code_default_or_example_names_the_folder():
    """The folder's name lives in DEPLOY.md and docs/SOURCES.md only (plan section 0)."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for fname in ("notes.py", "config.py", ".env.example"):
        text = open(os.path.join(root, fname), encoding="utf-8").read()
        assert "Saley – Sales Notes" not in text, fname
        assert "Saley - Sales Notes" not in text, fname
    example = open(os.path.join(root, ".env.example"), encoding="utf-8").read().splitlines()
    assert "NOTES_SOURCE_FOLDER=" in example
    assert "NOTES_SYNC_CMD=" in example
    assert "NOTES_REQUIRE_TITLE_TAGS=" in example
    assert ("NOTES_EXCLUDE_TITLE_PATTERNS=AM sync,PM sync,NFThing Kick-off,NFThing Wrap-up,"
            "standup,stand-up,daily sync") in example


# -- 2.6 the routing table -------------------------------------------------------

ROUTES = [
    ("what do we need to do today?", ["today"]),
    ("what do I need to do today", ["today"]),
    ("what should we do today", ["today"]),
    ("what's on today", ["today"]),
    ("whats on today", ["today"]),
    ("what do we need to do today about the Acme meeting?", ["today"]),
    ("what are the sales objectives for today?", []),
    ("what's the latest AI news today?", ["web", "news"]),
    ("show the to-dos", ["todos"]),
    ("what is on the to-do list", ["todos"]),
    ("what do I owe", ["todos"]),
    ("any action items?", ["todos"]),
    ("what came out of the call with Acme", ["notes"]),
    ("is Acme on hold", ["notes"]),
    ("sync the sheet", ["sheet"]),
    ("Sure.", []),
]


@pytest.mark.parametrize("question,groups", ROUTES)
def test_routing_table(question, groups):
    assert toolsets.route(question) == groups


def test_routing_membership_rows():
    assert "notes" in toolsets.route("what did we decide in yesterday's meeting?")
    r = toolsets.route("action items from the Acme call")
    assert "notes" in r and "todos" not in r
    r = toolsets.route("what do we need to do about Acme?")
    assert "notes" not in r and "today" not in r


def test_the_words_to_do_alone_no_longer_mean_notes():
    assert "notes" not in toolsets.route("what do we need to do")
    assert "todos" not in toolsets.route("what do we need to do about Acme?")


def test_groups():
    assert toolsets.GROUPS["today"] == ("show_todos",)
    assert toolsets.GROUPS["notes"] == ("list_meeting_notes", "read_meeting_note", "meeting_facts")
    assert toolsets.GROUPS["todos"] == ("show_todos", "todo_candidates")


def test_today_is_exclusive():
    assert toolsets.route("what do we need to do today on the sheet and the Acme meeting?") == ["today"]


def test_today_offers_only_show_todos():
    names = sorted({n for v in toolsets.GROUPS.values() for n in v} | {"cadence_preview", "x"})
    tools = [{"schema": {"name": n, "description": "d"}, "handler": None} for n in names]
    picked, groups, _why = toolsets.select(tools, "what do we need to do today?")
    assert [t["schema"]["name"] for t in picked] == ["show_todos"]


# -- 2.7 split_visible -----------------------------------------------------------

def _row(task, source, raised, row, open_=True):
    return {"n": str(row), "task": task, "owner": "A", "source_meeting": source,
            "date_raised": raised, "due": "", "status": "Open" if open_ else "Done",
            "notes": "", "open": open_, "row": row}


def test_split_visible(monkeypatch):
    import meetings
    import todos
    allowed = {notes._norm_title("Acme discovery call, 5 Oct"): {"2026-10-05"}}
    monkeypatch.setattr(meetings, "allowed_citations", lambda: allowed)
    rows = [
        _row("shown", "Acme discovery call, 5 Oct", "2026-10-05", 2),
        _row("standup", "NFThing Kick-off ( AM Sync), 7 Jul", "2026-07-07", 3),
        _row("pmcall", "PM Call, 19 Aug", "2026-08-19", 4),
        _row("blank", "", "2026-10-06", 5),                                   # D7, settled by the human (was Q1)
        _row("wrongdate", "Acme discovery call, 5 Oct", "2026-09-01", 6),
    ]
    shown, hidden = todos.split_visible(rows)
    assert [r["task"] for r in shown] == ["shown"]
    why = {r["task"]: r["reason"] for r in hidden}
    assert why == {"standup": "standup", "pmcall": "source_not_an_allowed_note",
                   "blank": "no_source_meeting",                              # D7, settled by the human (was Q1)
                   "wrongdate": "source_not_an_allowed_note"}


def test_split_visible_with_no_allowed_notes_hides_everything(monkeypatch):
    import meetings
    import todos
    monkeypatch.setattr(meetings, "allowed_citations", lambda: {})
    shown, hidden = todos.split_visible([_row("x", "Acme discovery call, 5 Oct", "2026-10-05", 2)])
    assert shown == [] and len(hidden) == 1


# -- addendum A1: a title tag is matched as a word --------------------------------

def test_tag_is_matched_as_a_word():
    """"[Sales]" is the word "sales": brackets carry no meaning, so it matches ordinary
    titles. Pinned so nobody later assumes the brackets are literal."""
    for title in ("Sales sync \u2013 2026-10-06", "Acme sales call \u2013 2026-10-06"):
        assert notes._title_matches(title, [notes._norm_title("[Sales]")]) is not None
        assert notes._title_matches(title, [notes._norm_title("[SalesNotes]")]) is None
    for title in ("[SalesNotes] Acme call \u2013 2026-10-06", "salesnotes \u2013 Acme call"):
        assert notes._title_matches(title, [notes._norm_title("[SalesNotes]")]) is not None

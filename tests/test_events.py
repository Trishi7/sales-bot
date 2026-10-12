"""R3 "AI events & summits" (rebuilt 14 Oct 2026), under pytest.

The checks themselves live in tests/events_checks.py and are shared with
verify_events.py; here each group is one test and every failed check is
reported.

OFFLINE: rows through the real parser, the real rule, renderer and suggestion
gates, and a real bot behind a fake Discord with a throwaway database; the
discovery search answers from a fixed text. conftest installs the offline
guard, and every R3 setting is pinned to .env.example rather than read from
this machine's .env.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import events_checks as evc  # noqa: E402


def _run(fn, *, pin=True) -> None:
    failed = []
    seen = []

    def check(name, got, want=True):
        seen.append(name)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    if pin:
        with evc.pinned():
            fn(check)
    else:
        fn(check)
    assert seen, "the group made no checks at all"
    assert not failed, "\n".join(failed)


def test_who_is_picked_block_a_block_b_or_skipped():
    _run(evc.who_is_picked)


def test_the_dates_parse_and_an_assumed_year_is_said():
    _run(evc.dates)


def test_the_tab_as_it_is_today_is_suggestions_only():
    _run(evc.the_live_tab)


def test_the_message_three_lines_three_blocks_one_numbering():
    def both(check):
        evc.the_message(check)
        evc.links_and_never(check)
    _run(both)


def test_the_suggestions_three_places_and_the_ceilings():
    _run(evc.the_suggestions)


def test_the_bot_fills_only_the_room_left_and_writes_nothing_without_a_yes():
    _run(evc.the_bot, pin=False)


def test_the_settings_and_the_boot_checks():
    _run(evc.settings, pin=False)

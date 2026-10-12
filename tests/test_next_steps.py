"""R13 "Next steps" as a reminder (rebuilt 12 Oct 2026), under pytest.

The checks themselves live in tests/next_steps_checks.py and are shared with
verify_next_steps.py; here each group is one test and every failed check is
reported.

OFFLINE: made-up rows through the real parser, the real rule and renderer, the
real state table in a throwaway database, and for the send path a real bot
behind a fake Discord. conftest installs the offline guard, and every R13
setting is pinned to .env.example rather than read from this machine's .env.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import next_steps_checks as nsx  # noqa: E402


def _run(fn, *, pin=True) -> None:
    failed = []
    seen = []

    def check(name, got, want=True):
        seen.append(name)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    if pin:
        with nsx.pinned():
            fn(check)
    else:
        fn(check)
    assert seen, "the group made no checks at all"
    assert not failed, "\n".join(failed)


def test_every_connected_row_is_in_dated_or_not():
    _run(nsx.who_is_in)


def test_what_each_person_is_reminded_about_row_by_row():
    _run(nsx.what_is_asked)


def test_the_message_is_the_agreed_lines_and_one_name_a_line():
    _run(nsx.the_message)


def test_the_order_is_seven_tiers_then_the_oldest_connection():
    _run(nsx.the_order)


def test_the_three_week_drop_is_the_only_ending():
    """Day 20 named, day 21 in the drop line, day 22 absent; a step change resets it."""
    _run(nsx.the_drop)


def test_the_send_path_records_state_and_replies_are_not_read():
    _run(nsx.the_bot, pin=False)


def test_no_rendered_message_names_a_column():
    """Runs the groups that render messages first, so there is something to sweep."""
    with nsx.pinned():
        quiet = lambda *a, **k: None  # noqa: E731
        nsx.RENDERED.clear()
        nsx.who_is_in(quiet)
        nsx.the_message(quiet)
        nsx.the_drop(quiet)
    _run(nsx.no_column_names, pin=False)


def test_the_settings_the_boot_check_and_the_rules_file():
    _run(nsx.settings, pin=False)

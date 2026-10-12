"""R12 "Sales packages" (redesigned 12 Oct 2026), under pytest.

The checks themselves live in tests/packages_checks.py and are shared with
verify_packages.py; here each group is one test and every failed check is
reported.

OFFLINE: rows through the real parser, the real rule and renderer, and a real
bot behind a fake Discord with a throwaway database for the send, the reply
that gives a date and the one reminder sender. conftest installs the offline
guard, and every R12 setting is pinned to .env.example rather than read from
this machine's .env.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import packages_checks as pkc  # noqa: E402


def _run(fn, *, pin=True) -> None:
    failed = []
    seen = []

    def check(name, got, want=True):
        seen.append(name)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    if pin:
        with pkc.pinned():
            fn(check)
    else:
        fn(check)
    assert seen, "the group made no checks at all"
    assert not failed, "\n".join(failed)


def test_who_is_picked_ready_is_not_yes_one_per_name():
    _run(pkc.who_is_picked)


def test_the_order_highest_completion_first_blank_last():
    _run(pkc.the_order)


def test_the_message_is_the_agreed_text_for_one_package_or_seven():
    _run(pkc.the_message)


def test_a_date_in_a_reply_is_recorded_and_a_newer_one_replaces_it():
    _run(pkc.the_dates, pin=False)


def test_the_reminder_fires_once_or_closes_quietly():
    _run(pkc.the_reminder, pin=False)


def test_the_settings_and_the_startup_check():
    _run(pkc.settings, pin=False)

"""R7 "No meeting booked yet", the escalation behind R13 (rebuilt 13 Oct 2026), under pytest.

The checks themselves live in tests/dm_no_meeting_checks.py and are shared with
verify_dm_no_meeting.py; here each group is one test and every failed check is
reported.

OFFLINE: made-up rows through the real parser, the real rule and renderer, the
real rotation table in a throwaway database, and for the send path a real bot
behind a fake Discord. conftest installs the offline guard, and every R7
setting is pinned to .env.example rather than read from this machine's .env.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dm_no_meeting_checks as dnm  # noqa: E402


def _run(fn, *, pin=True) -> None:
    failed = []
    seen = []

    def check(name, got, want=True):
        seen.append(name)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    # R7 IS OFF SINCE 16 OCT (RULES-OCT16): its own checks run with it turned back on.
    with dnm.r7_back_on():
        if pin:
            with dnm.pinned():
                fn(check)
        else:
            fn(check)
    assert seen, "the group made no checks at all"
    assert not failed, "\n".join(failed)


def test_r7_is_off_in_the_shipped_file_t9_t11():
    failed = []

    def check(name, got, want=True):
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    dnm.switched_off(check)
    assert not failed, "\n".join(failed)


def test_who_is_picked_four_columns_in_order():
    _run(dnm.who_is_picked)


def test_on_hold_is_out_for_this_rule_only():
    _run(dnm.on_hold_and_stops)


def test_the_rotation_walks_the_queue_and_nobody_is_dropped():
    _run(dnm.the_rotation)


def test_a_person_rule_13_names_the_same_day_is_skipped():
    _run(dnm.behind_rule_13)


def test_the_message_and_what_is_never_in_it():
    def both(check):
        dnm.the_message(check)
        dnm.never_in_the_message(check)
    _run(both)


def test_the_bot_records_only_a_real_send_and_reads_no_reply():
    _run(dnm.the_bot, pin=False)


def test_the_settings_and_the_boot_checks():
    _run(dnm.settings, pin=False)

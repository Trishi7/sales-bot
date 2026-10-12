"""R8 "Meeting preparation" (rebuilt 12 Oct 2026), under pytest.

The checks themselves live in tests/meeting_prep_checks.py and are shared with
verify_meeting_prep.py; here each group is one test and every failed check is
reported.

OFFLINE: rows through the real parser, the real rule and renderer, and a real
bot behind a fake Discord with a throwaway database for the plan, the news
lookup, the send, the touch record, the reply that asks for a reminder and the
one reminder sender. conftest installs the offline guard, and every R8 setting
is pinned to .env.example rather than read from this machine's .env.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import meeting_prep_checks as mpc  # noqa: E402


def _run(fn, *, pin=True) -> None:
    failed = []
    seen = []

    def check(name, got, want=True):
        seen.append(name)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    if pin:
        with mpc.pinned():
            fn(check)
    else:
        fn(check)
    assert seen, "the group made no checks at all"
    assert not failed, "\n".join(failed)


def test_one_column_is_read_and_nothing_else_gates_a_meeting():
    _run(mpc.who_is_picked)


def test_the_four_shapes_are_the_agreed_text_for_one_meeting_or_thirty():
    _run(mpc.the_message)


def test_a_touch_goes_out_once_and_a_moved_date_is_handled():
    _run(mpc.the_memory)


def test_the_news_line_is_read_from_the_store_and_never_holds_the_post():
    _run(mpc.the_news, pin=False)


def test_two_posts_a_day_at_their_own_times_weekends_included():
    _run(mpc.the_day, pin=False)


def test_a_reminder_is_set_reanchored_and_refused_after_the_meeting():
    _run(mpc.the_reminder, pin=False)


def test_the_setting_and_the_boot_line():
    _run(mpc.settings, pin=False)

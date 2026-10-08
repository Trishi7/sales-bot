"""REPLIES-OCT8 under pytest: replies to "give me a moment", the "today" answer, the grouped Next steps post, the
test clock (docs/plans/REPLIES-OCT8.md).

The checks themselves live in tests/oct8_checks.py and are shared with verify_replies_oct8.py; here each group is
one test and every failed check is reported.

OFFLINE: conftest installs the offline guard (a real Sheets or Drive call fails the test); the meeting notes, the
channel history and the news store are fixtures; no model is called.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import oct8_checks as oc  # noqa: E402


def _run(fn) -> None:
    failed, seen = [], []

    def check(name, got, want=True):
        seen.append(name)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    out = fn(check)
    if asyncio.iscoroutine(out):
        asyncio.run(out)
    assert seen, "the group made no checks at all"
    assert not failed, "\n".join(failed)


def test_take_ur_time_under_an_interim_line_is_one_reaction_and_no_second_answer():
    """ "take ur time", "sure take ur time", "no rush", "ok no rush thanks": replied to an interim line, to the
    answer and to a quiet line. One reaction, no text, no model call."""
    _run(oc.interim_replies)


def test_a_real_question_under_an_interim_line_is_answered_as_its_own_question():
    _run(oc.question_under_interim)


def test_no_rush_under_an_open_offer_does_not_decline_it():
    _run(oc.no_rush_is_not_a_no)


def test_the_8_oct_1121_exchange_gives_exactly_one_news_answer():
    _run(oc.the_exchange)


def test_what_is_the_team_working_on_today():
    """A meeting note, channel messages, a deliverable due in 2 working days and a registration closing tomorrow
    all appear; the posts are at most 2 lines; no AI news, time, "scheduled" or rule number; nothing gives one line."""
    _run(oc.today)


def test_the_ways_today_is_asked_all_reach_the_same_answer():
    _run(oc.today_routes)


def test_the_next_steps_post_is_grouped_by_ask_and_done_still_works():
    _run(oc.next_steps)


def test_a_pretend_day_ends_when_the_real_day_changes():
    _run(oc.the_clock)


def test_the_new_setting_is_in_the_contract():
    _run(oc.settings)

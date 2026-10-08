"""The order of the day's posts (NFT2-1069, the team's decisions of 8 Oct 2026), under pytest.

The checks themselves live in tests/day_order_checks.py and are shared with
verify_day_order.py; here each group is one test and every failed check is
reported.

OFFLINE, ON FIXED DATES: the real rule evaluators, the real planner and the
real rules file on a made-up sheet. conftest installs the offline guard (a real
Sheets or Drive call fails the test), nothing is sent, no model is called, and
the schedule settings are pinned to .env.example rather than read from this
machine's .env.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import day_order_checks as doc  # noqa: E402


def _run(fn) -> None:
    failed = []
    seen = []

    def check(name, got, want=True):
        seen.append(name)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    fn(check)
    assert seen, "the group made no checks at all"
    assert not failed, "\n".join(failed)


def test_every_weekday_with_every_rule_due_gives_the_exact_times():
    """Mon 14/16/18/20 R4 R7 R1 R10; Tue R5 R6 R2 R1; Wed R11 R1 R3; Thu R5 R1 R12;
    Fri R2 R6 R1. Rule 13 at 15:00, R8 and R9 at 10:00, none moving a spaced post."""
    _run(doc.every_weekday)


def test_a_rule_with_nothing_to_post_takes_no_slot():
    """Monday with no deliverables, with nobody for R7, with only AI news; and a
    P1 that appears after the day was planned takes the next free slot."""
    _run(doc.moving_up)


def test_the_gap_never_shrinks_and_nothing_lands_after_2000():
    _run(doc.the_gap)


def test_rule_7_on_mondays_never_lists_a_person_rule_13_covers():
    _run(doc.rule_7)


def test_rule_11_asks_on_the_next_wednesday_and_on_no_other_day():
    _run(doc.rule_11)


def test_ai_news_covers_from_the_previous_posts_slot():
    """Tuesday's 20:00 post covers from Monday's 18:00 slot; Monday's from Friday's."""
    _run(doc.news_window)


def test_startup_refuses_weekdays_that_disagree_with_daily_order():
    _run(doc.startup)


def test_the_weekend_is_unchanged():
    _run(doc.weekend)


def test_the_defaults_and_the_retired_setting():
    _run(doc.settings)


def test_the_wording_for_the_sheet_and_the_strategy():
    _run(doc.wording)


@pytest.mark.parametrize("day", doc.WEEK, ids=lambda d: d.strftime("%a"))
def test_one_planned_day_prints_for_the_report(day):
    """The table the test report carries: one line a post, in time order."""
    with doc.pinned():
        lines = doc.day_table(day)
    assert len(lines) == len(doc.SPACED[day]) + len(doc.FIXED)
    assert [line[:5] for line in lines] == sorted(line[:5] for line in lines)

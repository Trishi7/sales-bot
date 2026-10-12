"""R5 "PoCs to contact" (rebuilt 11 Oct 2026), under pytest.

The checks themselves live in tests/prospects_checks.py and are shared with
verify_prospects_priority.py; here each group is one test and every failed
check is reported.

OFFLINE: made-up sheet rows through the real parser, the real ordering
(`prospects.py`), the real rule and renderer, and for the messages a real bot
behind a fake Discord with a throwaway database. conftest installs the offline
guard (a real Sheets or Drive call fails the test), no model is called, and
every R5 setting is pinned to .env.example rather than read from this machine's
.env.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import prospects_checks as pc  # noqa: E402


def _run(fn, *, pin=True) -> None:
    failed = []
    seen = []

    def check(name, got, want=True):
        seen.append(name)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    if pin:
        with pc.pinned():
            fn(check)
    else:
        fn(check)
    assert seen, "the group made no checks at all"
    assert not failed, "\n".join(failed)


def test_the_six_rules_stack_and_ties_go_to_fewer_left_then_sheet_order():
    """warm + voice beats warm alone; warm alone beats everything else together;
    loose industry matching; a company matching nothing is queued last and counted."""
    _run(pc.scoring)


def test_people_are_ordered_by_seniority_then_signals_then_sheet_order():
    """The new role order, matched as whole words; a long or multi-line
    Designation prints no bracket."""
    _run(pc.people)


def test_the_month_end_lines_only_on_the_last_slot_and_only_when_nothing_moved():
    _run(pc.monthly)


def test_a_company_is_never_dropped():
    """It leaves the open slot only when everyone there is contacted or a person
    says to move on. Guards `prospects.resolve`."""
    _run(pc.never_dropped)


def test_a_person_rule_13_keeps_is_deduped_and_five_are_still_posted():
    _run(pc.dedup)


def test_the_messages_and_the_replies_to_them():
    """One company open; Thursday repeats Tuesday; "yes" lists the rest from 6;
    "move on" switches and the paused company returns; no skip line anywhere;
    "who's left at X?" gives the post's order."""
    _run(pc.messages, pin=False)

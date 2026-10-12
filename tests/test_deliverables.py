"""R4 "Deliverables to chase" (rebuilt 12 Oct 2026), under pytest.

The checks themselves live in tests/deliverables_checks.py and are shared with
verify_deliverables.py; here each group is one test and every failed check is
reported.

OFFLINE: rows through the real parser, the real rule and renderer, and a real
bot behind a fake Discord with a throwaway database for the plan, the send and
the live sweep's cutoff. conftest installs the offline guard, and every R4
setting is pinned to .env.example rather than read from this machine's .env.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import deliverables_checks as dc  # noqa: E402


def _run(fn, *, pin=True) -> None:
    failed = []
    seen = []

    def check(name, got, want=True):
        seen.append(name)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    if pin:
        with dc.pinned():
            fn(check)
    else:
        fn(check)
    assert seen, "the group made no checks at all"
    assert not failed, "\n".join(failed)


def test_two_conditions_select_a_row_and_only_completed_drops_it():
    _run(dc.the_selection)


def test_earliest_deadline_first_sheet_order_on_a_tie_no_deadline_last():
    _run(dc.the_order)


def test_the_message_is_the_agreed_text_with_no_overdue_no_link_and_no_close():
    _run(dc.the_message)


def test_monday_only_through_the_real_sender_and_never_after_the_cutoff():
    _run(dc.the_day, pin=False)


def test_the_changed_defaults_and_the_retired_setting():
    _run(dc.settings, pin=False)

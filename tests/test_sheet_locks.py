"""The write lock on Outreach PoCs, by role (12 Oct 2026), under pytest.

The checks themselves live in tests/sheet_locks_checks.py and are shared with
verify_sheet_locks.py; here each group is one test and every failed check is
reported.

OFFLINE: header rows through the real parser, the real lock and the real write
paths over a worksheet that records what it is asked to write. conftest
installs the offline guard, and the lock settings are pinned to .env.example
rather than read from this machine's .env.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sheet_locks_checks as slc  # noqa: E402


def _run(fn) -> None:
    failed = []
    seen = []

    def check(name, got, want=True):
        seen.append(name)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    with slc.pinned():
        fn(check)
    assert seen, "the group made no checks at all"
    assert not failed, "\n".join(failed)


def test_the_same_roles_are_locked_in_every_header_order():
    _run(slc.resolving)


def test_a_restricted_role_with_no_column_refuses_every_write():
    _run(slc.failing_closed)


def test_a_letter_range_only_adds_and_a_new_row_never_opens_a_locked_block():
    _run(slc.ranges_and_new_rows)


def test_the_email_cell_and_the_signed_new_row_still_work():
    _run(slc.the_two_exceptions)


def test_the_startup_log_and_the_settings():
    _run(slc.the_log_and_the_settings)

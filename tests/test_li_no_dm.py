"""R6 "Connected, no DM yet" (rebuilt 12 Oct 2026), under pytest.

The checks themselves live in tests/li_no_dm_checks.py and are shared with
verify_li_no_dm.py; here each group is one test and every failed check is
reported.

OFFLINE: made-up rows through the real parser, the real rule, email work,
renderer and sender in a real bot behind a fake Discord. The email search and
the DNS lookup are stand-ins that answer from a table. conftest installs the
offline guard, and every R6 setting is pinned to .env.example.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import li_no_dm_checks as lnd  # noqa: E402


def _run(fn, *, pin=False) -> None:
    failed = []
    seen = []

    def check(name, got, want=True):
        seen.append(name)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    if pin:
        with lnd.pinned():
            fn(check)
    else:
        fn(check)
    assert seen, "the group made no checks at all"
    assert not failed, "\n".join(failed)


def test_who_is_picked_reads_the_whole_tab_and_the_dm_cell():
    _run(lnd.who_is_picked, pin=True)


def test_the_four_blocks_numbered_straight_through():
    _run(lnd.the_message)


def test_five_plain_and_five_with_email_news():
    _run(lnd.the_caps)


def test_the_three_cheap_checks_and_when_one_cannot_run():
    """Gina Neff; a personal address; dnspython missing."""
    _run(lnd.gina_and_the_checks)


def test_no_drop_no_rotation_and_the_all_clear():
    _run(lnd.repeats_and_all_clear)


def test_the_add_goes_through_one_proposal_and_an_approvers_yes():
    _run(lnd.the_add)


def test_never_an_address_from_the_sheet_or_a_column_name():
    """Runs the groups that render messages first, so there is something to sweep."""
    quiet = lambda *a, **k: None  # noqa: E731
    lnd.RENDERED.clear()
    lnd.the_message(quiet)
    lnd.the_caps(quiet)
    lnd.gina_and_the_checks(quiet)
    # RULES-OCT16: several of those cases now post nothing at all, so the posts of the rotation and the
    # all-clear are swept too.
    lnd.repeats_and_all_clear(quiet)
    _run(lnd.never_in_the_message)


def test_the_settings_the_boot_check_and_the_rules_file():
    _run(lnd.settings)

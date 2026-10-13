"""R14 "Reconnect Date follow-up" (NFT2-1086), under pytest.

The checks themselves live in tests/reconnect_checks.py and are shared with
verify_reconnect.py; here each group is one test and every failed check is
reported.

OFFLINE: rows through the real parser, the real rule and renderer, and a real
bot behind a fake Discord with a throwaway database for the plan, the send and
the "yes". conftest installs the offline guard, and every setting is pinned
rather than read from this machine's .env. The shape of the real tab is a
fixture; nothing reads the real sheet.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import reconnect_checks as rcx  # noqa: E402


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


def test_01_to_06_which_rule_wins():
    _run(rcx.which_rule_wins)


def test_07_to_16_date_formats():
    _run(rcx.date_formats)


def test_17_to_22_timing():
    _run(rcx.timing)


def test_23_to_30_the_post_the_yes_and_the_wrong_channel():
    _run(rcx.the_post)


def test_31_to_35_stopping():
    _run(rcx.stopping)


def test_the_measured_shape_of_the_tab():
    _run(rcx.measured_shape)


def test_edge_cases_the_table_does_not_number():
    _run(rcx.edge_cases)


def test_the_reconnect_date_column_is_read_never_written():
    _run(rcx.sheet_lock)


def test_settings_rule_and_wording():
    _run(rcx.settings)

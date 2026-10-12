"""R10 "Closure support" (rebuilt 13 Oct 2026), under pytest.

The checks themselves live in tests/closure_support_checks.py and are shared
with verify_closure_support.py; here each group is one test and every failed
check is reported.

OFFLINE: made-up rows through the real parser, the real rule and renderer, and
a real bot behind a fake Discord with a throwaway database for the news lookup,
the send and the marker. conftest installs the offline guard, and every R10
setting is pinned to .env.example rather than read from this machine's .env.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import closure_support_checks as csc  # noqa: E402


def _run(fn, *, pin=True) -> None:
    failed = []
    seen = []

    def check(name, got, want=True):
        seen.append(name)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    if pin:
        with csc.pinned():
            fn(check)
    else:
        fn(check)
    assert seen, "the group made no checks at all"
    assert not failed, "\n".join(failed)


def test_who_is_picked_a_closure_stage_strictly_above_fifty():
    _run(csc.who_is_picked)


def test_the_empty_columns_notice_or_nothing_at_all():
    _run(csc.notice_or_silence)


def test_the_list_person_first_highest_first_no_cap_no_rotation():
    _run(csc.the_list)


def test_the_news_line_is_read_from_the_store_since_the_last_post():
    _run(csc.the_news, pin=False)


def test_the_marker_is_r10s_own_row_and_only_a_live_deals_post_moves_it():
    _run(csc.the_marker, pin=False)


def test_no_deals_post_names_a_column_and_the_settings():
    def both(check):
        csc.the_news(check)                      # fills the list of rendered posts the sweep reads
        with csc.pinned():
            csc.never_in_the_message(check)
        csc.settings(check)
    _run(both, pin=False)

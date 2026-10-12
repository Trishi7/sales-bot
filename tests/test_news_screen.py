"""R2 "New companies in the news" (rebuilt 12 Oct 2026), under pytest.

The checks themselves live in tests/news_screen_checks.py and are shared with
verify_news_screen.py; here each group is one test and every failed check is
reported.

OFFLINE: a scripted model, made-up sheets (281 + 212 companies, 382 known)
through the real parser, the real feed store and ledger in a throwaway
database, a real bot behind a fake Discord. conftest installs the offline
guard, and every R2 setting is pinned to .env.example rather than read from
this machine's .env.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import news_screen_checks as nsc  # noqa: E402


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


def test_the_full_name_decides_and_a_partial_match_is_a_question():
    """Nudge Security is not Nudge - AI Commerce Discovery; Google is not Google DeepMind."""
    with nsc.pinned():
        _run(nsc.matching)


def test_the_window_runs_from_the_last_post_and_never_past_the_previous_scheduled_day():
    _run(nsc.window)


def test_all_382_companies_are_known_and_a_tracked_company_is_dropped_in_code():
    """The ElevenLabs and Yellow.ai cases of 11 Oct; an unreadable sheet cancels the message."""
    _run(nsc.known_list)


def test_three_outcomes_two_verdicts_masked_links_and_five_at_most():
    _run(nsc.outcomes_and_message)


def test_only_a_live_post_moves_the_marker_and_unanswered_companies_come_back():
    _run(nsc.marker_and_repeats)


def test_the_reply_is_read_by_company_name_and_nothing_is_written_on_a_doubt():
    _run(nsc.replies)


def test_the_settings_and_the_env_contract():
    _run(nsc.settings)

"""NEWS-ANSWER-OCT16 "a question gets the real top 5" (T1 to T16), under pytest.

The checks live in tests/news_window_checks.py and are shared with
verify_news_window.py, which also prints the T1-T16 table; here each group is
one test and every failed check is reported.

T16 (the templates) is NOT run here: it re-renders every post with
tools/render_templates.py, which takes minutes, and the suite is slow enough.
verify_news_window.py runs it every time.

OFFLINE: conftest installs the offline guard, the feeds are canned, the scorer
is a stand-in and no model is called.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import news_window_b_checks as window_b  # noqa: E402
import news_window_checks as window  # noqa: E402


def _run(fn) -> None:
    failed = []

    def check(name, got, want=True):
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    out = fn(check)
    if asyncio.iscoroutine(out):
        asyncio.run(out)
    assert not failed, "\n".join(failed)


def test_the_window_the_choice_the_tail_t1_to_t7():
    _run(window.pure)


def test_through_the_bot_t8_to_t15():
    _run(window.through_the_bot)


def test_a_named_day_the_window_b1_to_b10_b19():
    _run(window_b.windows)


def test_a_named_day_through_the_bot_b11_to_b18():
    _run(window_b.through_the_bot)

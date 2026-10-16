"""NEWS-PATCH-OCT16 "the Gemini 4 miss" (T1 to T14), under pytest.

The checks live in tests/news_patch_checks.py and are shared with
verify_news_patch.py, which also prints the T1-T14 table; here each group is
one test and every failed check is reported.

T14 (the templates) is NOT run here: it re-renders every post with
tools/render_templates.py, which takes minutes, and the suite is slow enough.
verify_news_patch.py runs it every time.

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

import news_patch_checks as patch  # noqa: E402


def _run(fn) -> None:
    failed = []

    def check(name, got, want=True):
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    out = fn(check)
    if asyncio.iscoroutine(out):
        asyncio.run(out)
    assert not failed, "\n".join(failed)


def test_one_story_many_outlets_t1_to_t3():
    _run(patch.story_keys)


def test_flag_discipline_t4_t5():
    _run(patch.flags)


def test_score_everything_t6_to_t8():
    _run(patch.scoring)


def test_breaking_budget_t9_t10():
    _run(patch.budget)


def test_every_hour_t11_t12():
    _run(patch.every_hour)


def test_health_line_t13():
    _run(patch.health)


def test_research_published_t15_to_t18():
    _run(patch.research)

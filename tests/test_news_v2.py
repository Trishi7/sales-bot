"""NFT2-1087 "Saley AI News Flow" (T1 to T83), under pytest.

The checks themselves live in tests/news_v2_checks.py and are shared with
verify_news_v2.py, which also prints the T1-T83 table; here each group is one
test and every failed check is reported.

OFFLINE: conftest installs the offline guard (a real Sheets or Drive call
fails the test), the feeds and arXiv are canned, the scorer is a stand-in and
no model is called.
"""
import asyncio
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import news_v2_checks as v2  # noqa: E402

# Every T a group's checks were named for, across the whole module run.
COVERED: set = set()
_T_RE = re.compile(r"^((?:T\d+)(?:/T\d+)*)\b")


def _run(fn) -> None:
    failed = []

    def check(name, got, want=True):
        found = _T_RE.match(str(name))
        for t in (found.group(1).split("/") if found else []):
            COVERED.add(int(t[1:]))
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    out = fn(check)
    if asyncio.iscoroutine(out):
        asyncio.run(out)
    assert not failed, "\n".join(failed)


def test_the_name_list_t1_to_t11():
    _run(v2.names)


def test_collection_t12_to_t20_and_t72():
    _run(v2.collection)


def test_flag_and_score_t21_to_t28():
    _run(v2.flags)


def test_breaking_t29_to_t41_t79_t80():
    _run(v2.breaking)


def test_the_daily_post_t42_to_t56_t75_to_t78_t83():
    _run(v2.daily)


def test_rendering_t57_to_t63_t81():
    _run(v2.rendering)


def test_researchers_t64_to_t70():
    _run(v2.researchers)


def test_no_story_line_is_bold_t82():
    """Asked of every message the groups above sent, so it runs after them."""
    _run(v2.no_bold_lines)


def test_config_t71_t73_t74():
    _run(v2.settings)


def test_every_case_t1_to_t83_is_covered():
    """A test case nobody wrote a check for is a failure, not a pass."""
    missing = sorted(set(range(1, 84)) - COVERED)
    assert not missing, "no check is named for: " + ", ".join(f"T{n}" for n in missing)

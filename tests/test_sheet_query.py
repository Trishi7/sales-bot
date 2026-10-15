"""NFT2-1095 "Saley cannot answer general sheet questions" (T1 to T17, E1 to E10), under pytest.

The checks live in tests/sheet_query_checks.py and are shared with verify_sheet_query.py, which also prints the
T1-T17 table; here each group is one test and every failed check is reported. T17's long runs (the other verify
scripts and this very suite) are not repeated from inside pytest.

OFFLINE: conftest installs the offline guard (a real Sheets or Drive call fails the test), the sheet is the 13 Oct
fixture, and no model is called.
"""
import asyncio
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sheet_query_checks as sq  # noqa: E402

COVERED: set = set()
_T_RE = re.compile(r"^((?:[TE]\d+)(?:/[TE]\d+)*)\b")


def _run(fn, *a) -> None:
    failed = []

    def check(name, got, want=True):
        found = _T_RE.match(str(name))
        for t in (found.group(1).split("/") if found else []):
            COVERED.add(t)
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    out = fn(check, *a)
    if asyncio.iscoroutine(out):
        asyncio.run(out)
    assert not failed, "\n".join(failed)


def test_columns_and_queries_t1_to_t9_t13():
    _run(sq.columns_and_queries)


def test_the_15_oct_question_through_the_engine_t10():
    _run(sq.engine)


def test_read_only_t11():
    _run(sq.read_only)


def test_query_tracker_unchanged_t12():
    _run(sq.tracker_filters)


def test_two_columns_the_master_pipeline_unreadable_tabs_t14_t15():
    _run(sq.tabs_and_columns)


def test_env_example_and_the_self_tests_t16_t17():
    _run(sq.settings)


def test_every_case_is_covered():
    """A test case nobody wrote a check for is a failure, not a pass."""
    want = {f"T{n}" for n in range(1, 18)} | {f"E{n}" for n in range(1, 11)}
    missing = sorted(want - COVERED, key=lambda t: (t[0], int(t[1:])))
    assert not missing, "no check is named for: " + ", ".join(missing)

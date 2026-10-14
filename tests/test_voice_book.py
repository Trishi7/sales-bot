"""NFT2-1090 "Saley Voice v2" (T1 to T66), under pytest.

The checks live in tests/voice_book_checks.py and are shared with verify_voice_book.py, which also prints
the T1-T66 table; here each group is one test and every failed check is reported.

OFFLINE: conftest installs the offline guard (a real Sheets or Drive call fails the test), the speaking
paths run over a stand-in model client, and no model is called.
"""
import asyncio
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import voice_book_checks as vb  # noqa: E402

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


def test_the_voice_book_t1_to_t7_t52_t53():
    _run(vb.book)


def test_greetings_and_small_talk_t8_to_t14():
    _run(vb.greetings)


def test_empathy_and_the_context_t15_to_t27_t37_to_t39():
    _run(vb.empathy_checks)


def test_the_checks_in_code_t28_to_t36_t40_to_t44():
    _run(vb.word_checks)


def test_seeing_the_prompt_t45_to_t51():
    _run(vb.prompts)


def test_questions_not_votes_t57_to_t65():
    _run(vb.votes)


def test_config_the_templates_and_the_judge_t54_t55_t56_t66():
    _run(vb.settings)


def test_every_case_t1_to_t66_is_covered():
    """A test case nobody wrote a check for is a failure, not a pass."""
    missing = sorted(set(range(1, 67)) - COVERED)
    assert not missing, "no check is named for: " + ", ".join(f"T{n}" for n in missing)

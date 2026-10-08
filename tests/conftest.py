"""Shared fixtures. No sheet, no network, no Discord.

Every test in this directory runs offline. The two that touch a database use a
temporary file that is deleted afterwards; nothing here can reach the real
playbook or the real `sales_bot.db`.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


# The canonical tab's real headers on 7 Oct 2026, A-AF (32 columns). Used by the append and row-key tests.
POCS_HEADERS = [
    "Sr No", "Company/Uni", "Industry", "Name", "Designation", "Email id",
    "Based (Sept 2026)", "Research Paper Link", "LI Url", "First Contact",
    "First Contact Type", "First Contact Date", "Sid - LI Addition",
    "LI Connected Date", "LI DM Sent", "LI DM Date", "Next Steps",
    "1st Email Sent", "1st Email Date", "2nd Email Sent", "2nd Email Date",
    "3rd Email Sent", "3rd Email Date", "Meeting Date", "Meeting Status",
    "Notes/Remarks", "Package", "Prospect Status", "Closure Prob%",
    "Estd. Deal Size (USD)", "Deal Status", "Priority",
]

# The layout before 7 Oct 2026 (A-X, S "Next Steps/Notes"). Kept so the header-mapping tests can
# prove the old row still maps, and the scripts that pin it (verify_s3's own row) have a copy.
POCS_HEADERS_PRE_7OCT = [
    "Sr No", "Company/Uni", "Industry", "Name", "Designation", "Email id",
    "Based", "Research Paper Link", "LI Url", "First Contact",
    "First Contact Type", "First Contact Date", "Sid - LI Addition",
    "LI Connected Date", "LI DM Sent", "LI DM Date", "Meeting Date",
    "Meeting Status", "Next Steps/Notes", "Package", "Prospect Status",
    "Closure Prob%", "Estd. Deal Size (USD)", "Deal Status",
]

PIPELINE_HEADERS = [
    "Sr no.", "Company", "Industry", "Geography", "Approx. Funding",
    "Outreach Line - Researchers", "Dates",
]


@pytest.fixture
def db():
    """A throwaway database, deleted afterwards."""
    import db as dbmod

    path = os.path.join(tempfile.mkdtemp(prefix="saley-test-"), "t.db")
    yield dbmod.DB(path)
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.fixture
def pocs_tab():
    """A parsed Outreach PoCs tab with three contacts at one company."""
    import time

    import deadlines as dl
    import gtm_sheet

    def row(n, name, title):
        r = [""] * len(POCS_HEADERS)  # widened to the 7 Oct layout (32 cells)
        r[0], r[1], r[2], r[3], r[4], r[6] = n, "Wispr Flow", "AI Voice Agents", name, title, "SF"
        return r

    values = [POCS_HEADERS, row("1", "Tanay Kothari", "Co-Founder"),
              row("2", "Sahaj Garg", "CTO"), row("3", "Ariya Rastrow", "Chief Scientist")]
    return gtm_sheet.SHEETS._parse_values("Outreach PoCs", values, read_at=dl.real_epoch())


@pytest.fixture
def pipeline_tab():
    """A parsed Master Pipeline tab with two companies on it."""
    import time

    import deadlines as dl
    import gtm_sheet

    values = [PIPELINE_HEADERS] + [
        ["1", "Wispr Flow", "AI Voice Agents", "US", "$56M", "…", ""],
        ["2", "PolyAI", "AI Voice Agents", "UK", "$50M", "…", ""],
    ]
    return gtm_sheet.SHEETS._parse_values("Master Pipeline", values, read_at=dl.real_epoch())


@pytest.fixture
def tone_env():
    """Clear the tone dials before and after, so tests do not leak into each
    other. Tone reads the environment on every call, which is exactly what
    makes it leaky in a test suite."""
    keys = ("SALEY_WARMTH", "SALEY_FORMALITY", "SALEY_EMOJI",
            "SALEY_LENGTH", "SALEY_HUMOUR")
    saved = {k: os.environ.pop(k, None) for k in keys}
    yield os.environ
    for k in keys:
        os.environ.pop(k, None)
        if saved[k] is not None:
            os.environ[k] = saved[k]


def pytest_addoption(parser):
    """tests/test_tone_samples.py executes tools/tone_samples.py (against a fake client, in a sandbox copy
    of the repo). That is off unless asked for: `pytest --run-tone-samples`. A command-line option, not an
    environment variable, so .env.example has nothing to list."""
    parser.addoption("--run-tone-samples", action="store_true", default=False,
                     help="also run the tests that execute tools/tone_samples.py (fake model, sandbox copy)")


def pytest_configure(config):
    config.addinivalue_line("markers", "tone_samples_run: executes tools/tone_samples.py; needs --run-tone-samples")


import offline_guard  # noqa: E402  (tests/ is on sys.path under pytest's rootdir import mode)


@pytest.fixture(autouse=True)
def _offline_guard():
    """No test reaches the real Sheets or Drive, and every prompt a test builds sees canned source
    statuses. A blocked attempt fails the test at teardown even if the code under test swallowed the
    exception. See tests/offline_guard.py."""
    guard = offline_guard.install()
    guard.calls.clear()
    yield guard
    calls = list(guard.calls)
    guard.calls.clear()
    assert not calls, f"live Sheets/Drive calls attempted during the test: {calls}"

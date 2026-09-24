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


# The canonical tab's real headers, A-X. Used by the append and row-key tests.
POCS_HEADERS = [
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

    import gtm_sheet

    values = [POCS_HEADERS] + [
        ["1", "Wispr Flow", "AI Voice Agents", "Tanay Kothari", "Co-Founder",
         "", "SF", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
        ["2", "Wispr Flow", "AI Voice Agents", "Sahaj Garg", "CTO",
         "", "SF", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
        ["3", "Wispr Flow", "AI Voice Agents", "Ariya Rastrow", "Chief Scientist",
         "", "SF", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
    ]
    return gtm_sheet.SHEETS._parse_values("Outreach PoCs", values, read_at=time.time())


@pytest.fixture
def pipeline_tab():
    """A parsed Master Pipeline tab with two companies on it."""
    import time

    import gtm_sheet

    values = [PIPELINE_HEADERS] + [
        ["1", "Wispr Flow", "AI Voice Agents", "US", "$56M", "…", ""],
        ["2", "PolyAI", "AI Voice Agents", "UK", "$50M", "…", ""],
    ]
    return gtm_sheet.SHEETS._parse_values("Master Pipeline", values, read_at=time.time())


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

"""NFT2-1065 — the pure cases: routing, link classification, the offer wording,
the capability text, the config defaults. No network, no sheet, no model.

Written from docs/plans/NFT2-1065.md sections 2 and 4 (T2, T3, T4, T6, T19), not from the
builder's code. The end-to-end cases (a scripted model, a fake search backend, the proposal
flow) are in verify_profile_lookup.py and verify_replay_oct6.py.
"""
import importlib.util
import logging
import os
import re
import shutil
import sys
import tempfile

import pytest

import approvals
import links
import persona
import query_engine
import toolsets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STEP5 = "research profiles for Sigil Wen"                       # reconstructed (fixture has no quote)
STEP8 = ("LinkedIn and research profile links for Janajit Bagchi and Suryansh Shukla "
         "(ARTPARK India)")                                       # reconstructed
STEP9 = "can you find their LI profile links from the web?"      # verbatim
PARAPHRASES = [
    "find LinkedIn links for the PoCs at ARTPARK India",
    "get me the LinkedIn for the ARTPARK India PoCs",
    "find the LinkedIn and research profile for these researchers",
]


def _tools(*extra):
    names = sorted({n for names in toolsets.GROUPS.values() for n in names}
                   | {"cadence_preview", "mystery", "web_search", "fetch_page"} | set(extra))
    return [{"schema": {"name": n, "description": "d"}, "handler": None} for n in names]


def _offered(text, previous=""):
    picked, groups, _why = toolsets.select(_tools(), text, previous=previous)
    return [t["schema"]["name"] for t in picked], groups


# -- T1 (pure half): the web pair is offered whatever the wording routes to ---------

@pytest.mark.parametrize("text", [STEP5, STEP8, STEP9] + PARAPHRASES)
def test_t1_web_pair_is_offered_for_every_wording(text):
    names, _groups = _offered(text)
    assert "web_search" in names and "fetch_page" in names


@pytest.mark.parametrize("text", [STEP5, STEP8, STEP9] + PARAPHRASES)
def test_t1_profile_wordings_route_to_profile(text):
    assert "profile" in toolsets.route(text)


def test_step_texts_route_to_profile():
    for text in (STEP5, STEP8, STEP9):
        assert "profile" in toolsets.route(text), text
    assert "web" in toolsets.route(STEP9)


def test_always_constant():
    assert toolsets.ALWAYS == ("web_search", "fetch_page")


def test_a_sheet_question_now_has_the_web_pair():
    names, groups = _offered("where are we with Acme in the pipeline?")
    assert "sheet" in groups
    assert {"web_search", "fetch_page"} <= set(names)


# -- T3: a follow-up that inherits a non-web route still has web_search -----------

def test_t3_follow_up_inheriting_sheet_has_web_search():
    names, groups = _offered("and Suryansh?", previous="where are we with ARTPARK?")
    assert "sheet" in groups
    assert "web_search" in names


# -- T4: today stays exclusive ----------------------------------------------------

def test_t4_today_offers_exactly_show_todos():
    names, groups = _offered("what do we need to do today?")
    assert names == ["show_todos"]
    assert groups == ["today"]


def test_t4_a_follow_up_inheriting_today_has_no_web_pair():
    names, _groups = _offered("and for tomorrow?", previous="what do we need to do today?")
    assert "web_search" not in names and "fetch_page" not in names


def test_is_exclusive():
    assert toolsets.is_exclusive(["today"]) is True
    assert toolsets.is_exclusive(["sheet"]) is False
    assert toolsets.is_exclusive([]) is False


def test_profile_group_has_the_four_tools():
    assert set(toolsets.GROUPS["profile"]) == {"web_search", "fetch_page", "lookup_company",
                                               "propose_poc_add"}
    assert "propose_poc_add" in toolsets.GROUPS["web"]
    assert "propose_poc_add" in toolsets.GROUPS["people"]


def test_one_liners():
    for name in ("web_search", "fetch_page", "propose_poc_add"):
        line = toolsets.ONE_LINE[name]
        assert 0 < len(line) <= 260, name
    assert "public profile" in toolsets.ONE_LINE["web_search"].lower()
    assert "never fetched" in toolsets.ONE_LINE["fetch_page"].lower()
    assert "writes nothing" in toolsets.ONE_LINE["propose_poc_add"].lower()


# -- T6: classifying a link --------------------------------------------------------

@pytest.mark.parametrize("url,kind", [
    ("https://www.linkedin.com/in/janajit-bagchi", "profile"),
    ("https://in.linkedin.com/in/janajit-bagchi-1a2b3c/", "profile"),
    ("https://linkedin.com/in/x", "profile"),
    ("https://www.linkedin.com/posts/janajit-bagchi_ai-activity-123", "post"),
    ("https://www.linkedin.com/feed/update/urn:li:activity:1", "post"),
    ("https://www.linkedin.com/pulse/some-article", "post"),
    ("https://www.linkedin.com/company/artpark", "company"),
    ("https://www.linkedin.com/school/iisc", "company"),
    ("https://scholar.google.com/citations?user=abc", "other"),
    ("https://example.com/in/janajit", "other"),
    ("https://notlinkedin.com/in/janajit", "other"),
])
def test_profile_kind(url, kind):
    assert links.profile_kind(url) == kind


@pytest.mark.parametrize("a,b,same", [
    ("https://www.linkedin.com/in/x", "https://linkedin.com/in/x/", True),
    ("http://LinkedIn.com/in/x", "https://www.linkedin.com/in/x", True),
    ("https://linkedin.com/in/x?trk=1", "https://linkedin.com/in/x", True),
    ("https://linkedin.com/in/x.", "https://linkedin.com/in/x", True),
    ("https://linkedin.com/in/x", "https://linkedin.com/in/y", False),
    ("https://linkedin.com/in/x-123", "https://linkedin.com/in/x", False),
])
def test_same_url(a, b, same):
    assert links.same_url(a, b) is same


# -- the offer wording (assert against the function, not a string) -----------------

def test_a_t1_the_humans_exact_sentences():
    assert approvals.row_add_offer(["Janajit Bagchi", "Suryansh Shukla"], "Outreach PoCs") == (
        "Want me to add Janajit Bagchi and Suryansh Shukla to Outreach PoCs? "
        "I'll only add them once one of you says yes.")
    assert approvals.row_add_offer(["Janajit Bagchi"], "Outreach PoCs") == (
        "Want me to add Janajit Bagchi to Outreach PoCs? I'll only add them once one of you says yes.")


def test_offer_names_and_tab_and_says_it_asks_first():
    one = approvals.row_add_offer(["Janajit Bagchi"], "Outreach PoCs")
    two = approvals.row_add_offer(["Janajit Bagchi", "Suryansh Shukla"], "Outreach PoCs")
    three = approvals.row_add_offer(["A B", "C D", "E F"], "Outreach PoCs")
    assert "Janajit Bagchi" in one and "Outreach PoCs" in one
    assert "Janajit Bagchi and Suryansh Shukla" in two
    assert "A B, C D and E F" in three
    assert one != two
    for text in (one, two, three):
        assert "yes" in text.lower() and "only" in text.lower()      # asks first; writes only on a yes
        assert "<@" not in text                      # Q3: "one of you", no mentions


def test_offer_question_for_the_proposal():
    q = approvals.row_add_question(["Janajit Bagchi", "Suryansh Shukla"], "Outreach PoCs")
    assert "Janajit Bagchi and Suryansh Shukla" in q and "Outreach PoCs" in q
    assert q.rstrip().endswith("?")


# -- T2: the capability text --------------------------------------------------------

def test_t2_engine_prompt_tells_the_model_to_trust_its_tool_list():
    text = query_engine._engine_text(requester_name="V", today="2026-10-07",
                                     tool_names=["web_search", "fetch_page", "show_todos"])
    assert "NEVER say you lack a tool that is on it" in text
    assert "PUBLIC PROFILE LINKS" in text
    assert "web_search, fetch_page, show_todos" in text


def test_t2_profile_section_is_absent_without_web_search():
    text = query_engine._engine_text(requester_name="V", today="2026-10-07",
                                     tool_names=["show_todos"])
    assert "PUBLIC PROFILE LINKS" not in text


def test_t2_profile_section_carries_the_rules():
    text = query_engine._engine_text(requester_name="V", today="2026-10-07",
                                     tool_names=["web_search"])
    low = text.lower()
    assert "not their profile" in low                   # a post that mentions them
    assert "show both" in low                           # two people, same name
    assert "not checked yet" in low
    assert "not found in public search" in low
    assert "never open linkedin.com" in low or "never opens linkedin.com" in low


def test_t2_persona_no_longer_has_the_bare_cannot_do_line():
    assert "If someone asks for something you cannot do, say so in one sentence and stop." \
        not in persona.COS_PERSONA
    assert "no tool you were given this turn can do it" in persona.COS_PERSONA


def test_t2_nothing_says_there_is_no_web_search():
    bad = re.compile(
        r"(don'?t|do not|cannot|can'?t|no)\s+(have\s+)?(a\s+)?(web\s*search|linkedin\s+(lookup|search))",
        re.I)
    for fname in ("persona.py", "query_engine.py", "sales_policy.md", "sales_strategy.md"):
        text = open(os.path.join(ROOT, fname), encoding="utf-8").read()
        hits = [m.group(0) for m in bad.finditer(text)]
        # the only legitimate mentions are instructions about what to say WHEN search is
        # switched off; none of these four files has that
        assert hits == [], (fname, hits)


def test_t2_strategy_and_policy_allow_the_public_search():
    strategy = open(os.path.join(ROOT, "sales_strategy.md"), encoding="utf-8").read()
    policy = open(os.path.join(ROOT, "sales_policy.md"), encoding="utf-8").read()
    assert "Public search for a LinkedIn profile link is in scope" in strategy
    assert "Public search for a LinkedIn profile link is in scope" in policy
    assert "Scrape LinkedIn" not in strategy
    assert "Never send connection requests" in policy or "never send connection requests" in policy.lower()
    assert "never fetch linkedin.com" in policy.lower()


# -- T19: config defaults and the warning ------------------------------------------

def _config_without_dotenv():
    """config.py imported from a bare folder, so no .env can supply a value."""
    d = tempfile.mkdtemp(prefix="saley-cfg-")
    shutil.copy(os.path.join(ROOT, "config.py"), os.path.join(d, "config_bare.py"))
    spec = importlib.util.spec_from_file_location("config_bare", os.path.join(d, "config_bare.py"))
    mod = importlib.util.module_from_spec(spec)
    old = os.getcwd()
    keep = {k: os.environ.pop(k, None) for k in ("WEB_QUESTION_MAX_SEARCHES",
                                                  "QUERY_ENGINE_MAX_TOOL_ITERATIONS")}
    try:
        os.chdir(d)
        spec.loader.exec_module(mod)
    finally:
        os.chdir(old)
        for k, v in keep.items():
            if v is not None:
                os.environ[k] = v
    return mod


FOUR = ("WEB_QUESTION_MAX_SEARCHES=4", "QUERY_ENGINE_MAX_TOOL_ITERATIONS=7",
        "WEB_QUESTION_EXTENDED_SEARCHES=6", "QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS=9")


def test_t19_defaults_in_env_example():
    lines = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read().splitlines()
    for want in FOUR:
        assert want in lines, want


def test_t19_code_defaults():
    """config.py copied to a bare folder, so neither a .env nor the process environment decides."""
    cfg = _config_without_dotenv()
    assert (cfg.WEB_QUESTION_MAX_SEARCHES, cfg.QUERY_ENGINE_MAX_TOOL_ITERATIONS,
            cfg.WEB_QUESTION_EXTENDED_SEARCHES, cfg.QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS) == (4, 7, 6, 9)


def test_t19_extended_is_never_below_base():
    """A-T13: the extended value is max(base, extended); equal to the base switches the extension off."""
    import config
    assert config.WEB_QUESTION_EXTENDED_SEARCHES >= config.WEB_QUESTION_MAX_SEARCHES


def _warnings(monkeypatch, caplog, **values):
    import config
    for k, v in values.items():
        monkeypatch.setattr(config, k, v)
    with caplog.at_level(logging.WARNING):
        config.validate()
    return [r.getMessage() for r in caplog.records
            if "TOOL_ITERATIONS" in r.getMessage() and "below" in r.getMessage()]


def test_t19_warns_for_the_base_pair(monkeypatch, caplog):
    msgs = _warnings(monkeypatch, caplog, WEB_QUESTION_MAX_SEARCHES=4, QUERY_ENGINE_MAX_TOOL_ITERATIONS=5,
                     WEB_QUESTION_EXTENDED_SEARCHES=6, QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS=9)
    assert any("QUERY_ENGINE_MAX_TOOL_ITERATIONS" in m for m in msgs)


def test_t19_warns_for_the_extended_pair(monkeypatch, caplog):
    msgs = _warnings(monkeypatch, caplog, WEB_QUESTION_MAX_SEARCHES=4, QUERY_ENGINE_MAX_TOOL_ITERATIONS=7,
                     WEB_QUESTION_EXTENDED_SEARCHES=8, QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS=9)
    assert any("QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS" in m for m in msgs)


def test_t19_no_warning_at_the_defaults(monkeypatch, caplog):
    msgs = _warnings(monkeypatch, caplog, WEB_QUESTION_MAX_SEARCHES=4, QUERY_ENGINE_MAX_TOOL_ITERATIONS=7,
                     WEB_QUESTION_EXTENDED_SEARCHES=6, QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS=9)
    assert msgs == []


# -- the QuestionLimits object itself (A2.2): pure ------------------------------------

def _limits(**kw):
    base = dict(searches=4, rounds=7, ext_searches=6, ext_rounds=9, label="msg=1")
    base.update(kw)
    return query_engine.QuestionLimits(**base)


def test_limits_extend_once_and_never_twice(caplog):
    lim = _limits()
    with caplog.at_level(logging.INFO):
        assert lim.extend(hit="search", detail="q5") is True
        assert (lim.searches, lim.rounds, lim.extended) == (6, 9, True)
        assert lim.extend(hit="round", detail="q9") is False
        assert lim.extend(hit="search", detail="q9") is False
    lines = [r.getMessage() for r in caplog.records if "LIMIT EXTENDED ONCE" in r.getMessage()]
    assert len(lines) == 1
    assert "searches 4 -> 6" in lines[0] and "tool rounds 7 -> 9" in lines[0] and "search limit" in lines[0]


def test_limits_extension_off_when_extended_equals_base(caplog):
    lim = _limits(ext_searches=4, ext_rounds=7)
    with caplog.at_level(logging.INFO):
        assert lim.extend(hit="search", detail="q") is False
    assert (lim.searches, lim.rounds, lim.extended) == (4, 7, False)
    assert not [r for r in caplog.records if "LIMIT EXTENDED" in r.getMessage()]


def test_limits_extended_below_base_is_clamped_to_base():
    lim = _limits(ext_searches=2, ext_rounds=3)
    assert lim.extend(hit="round") is False and (lim.searches, lim.rounds) == (4, 7)


def test_two_questions_two_extensions():
    a, b = _limits(label="msg=1"), _limits(label="msg=2")
    assert a.extend(hit="search") is True and b.extend(hit="round") is True


# -- "what can you do?" must not deny web search either (plan 2.4.2, checklist 11) --------

def test_capability_line_on_when_search_is_available(monkeypatch):
    import search_backend
    import websearch
    monkeypatch.setattr(websearch, "enabled", lambda: True)
    monkeypatch.setattr(websearch, "server_side", lambda: False)
    monkeypatch.setattr(search_backend, "available", lambda: (True, ""))
    line = persona.web_capability_line()
    assert line.startswith("Web search: ON")
    assert "never open linkedin.com" in line.lower()


def test_capability_line_off_says_why(monkeypatch):
    import search_backend
    import websearch
    monkeypatch.setattr(websearch, "enabled", lambda: True)
    monkeypatch.setattr(websearch, "server_side", lambda: False)
    monkeypatch.setattr(search_backend, "available", lambda: (False, "no backend reachable"))
    line = persona.web_capability_line()
    assert line.startswith("Web search: OFF") and "no backend reachable" in line


def test_capability_line_never_raises(monkeypatch):
    import websearch

    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(websearch, "enabled", boom)
    assert persona.web_capability_line().startswith("Web search: OFF")


def test_the_deterministic_capability_reply_carries_the_line(monkeypatch):
    import config
    import search_backend
    monkeypatch.setattr(config, "GOOGLE_SERVICE_ACCOUNT_JSON", "")   # offline: no source probe reaches the sheet
    import websearch
    monkeypatch.setattr(websearch, "enabled", lambda: True)
    monkeypatch.setattr(websearch, "server_side", lambda: False)
    monkeypatch.setattr(search_backend, "available", lambda: (True, ""))
    assert "Web search: ON" in persona.fallback_capability_reply()
    assert "Web search: ON" in persona.capability_tail()


# -- A-T15: the offline guard does what the addendum says ------------------------------

def test_a_t15_the_guard_records_and_raises(_offline_guard):
    import gspread
    import offline_guard
    before = len(_offline_guard.calls)
    with pytest.raises(offline_guard.LiveCallBlocked):
        gspread.authorize(None)
    assert len(_offline_guard.calls) == before + 1
    _offline_guard.calls.clear()                  # this one was deliberate; the fixture must not fail the test


def test_a_t15_source_statuses_are_canned_and_probe_nothing():
    import sources
    rows = sources.status_report()
    assert rows and all(r["status"] == sources.CONNECTED and "canned" in r["detail"] for r in rows)


# -- builder's last two edits: a warning when an extended value is SET below its base (config) ----------

def _warn(monkeypatch, caplog, **values):
    import config
    for k, v in values.items():
        monkeypatch.setattr(config, k, v)
    with caplog.at_level(logging.INFO):
        config.validate()
    return [r.getMessage() for r in caplog.records]


def test_extended_set_below_base_warns_for_searches(monkeypatch, caplog):
    msgs = _warn(monkeypatch, caplog, WEB_QUESTION_MAX_SEARCHES=8, WEB_QUESTION_EXTENDED_SEARCHES_RAW=6,
                 WEB_QUESTION_EXTENDED_SEARCHES=8, QUERY_ENGINE_MAX_TOOL_ITERATIONS=11,
                 QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS_RAW=11, QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS=11)
    assert any("WEB_QUESTION_EXTENDED_SEARCHES=6 is below WEB_QUESTION_MAX_SEARCHES=8" in m for m in msgs)
    assert not any("QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS=" in m and "is below" in m for m in msgs)


def test_extended_set_below_base_warns_for_rounds(monkeypatch, caplog):
    msgs = _warn(monkeypatch, caplog, WEB_QUESTION_MAX_SEARCHES=4, WEB_QUESTION_EXTENDED_SEARCHES_RAW=6,
                 WEB_QUESTION_EXTENDED_SEARCHES=6, QUERY_ENGINE_MAX_TOOL_ITERATIONS=12,
                 QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS_RAW=9, QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS=12)
    assert any("QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS=9 is below QUERY_ENGINE_MAX_TOOL_ITERATIONS=12" in m
               for m in msgs)
    assert not any("WEB_QUESTION_EXTENDED_SEARCHES=" in m and "is below" in m for m in msgs)


def test_both_warn_together_and_neither_at_the_defaults(monkeypatch, caplog):
    msgs = _warn(monkeypatch, caplog, WEB_QUESTION_MAX_SEARCHES=8, WEB_QUESTION_EXTENDED_SEARCHES_RAW=6,
                 WEB_QUESTION_EXTENDED_SEARCHES=8, QUERY_ENGINE_MAX_TOOL_ITERATIONS=12,
                 QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS_RAW=9, QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS=12)
    assert sum("is below" in m and "EXTENDED" in m for m in msgs) == 2


def test_no_below_base_warning_at_the_defaults_and_the_info_line(monkeypatch, caplog):
    caplog.clear()
    msgs = _warn(monkeypatch, caplog, WEB_QUESTION_MAX_SEARCHES=4, WEB_QUESTION_EXTENDED_SEARCHES_RAW=6,
                 WEB_QUESTION_EXTENDED_SEARCHES=6, QUERY_ENGINE_MAX_TOOL_ITERATIONS=7,
                 QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS_RAW=9, QUERY_ENGINE_EXTENDED_TOOL_ITERATIONS=9)
    assert not any("is below" in m and "EXTENDED" in m for m in msgs)
    assert "[config] one question: up to 4 web search(es) and 7 tool round(s); one extension to 6 and 9." in msgs


def test_a_base_above_the_extension_keeps_its_caps():
    lim = query_engine.QuestionLimits(searches=8, rounds=12, ext_searches=6, ext_rounds=9)
    assert lim.extend(hit="search") is False and (lim.searches, lim.rounds) == (8, 12)
    lim = query_engine.QuestionLimits(searches=8, rounds=7, ext_searches=6, ext_rounds=9)
    assert lim.extend(hit="round") is True and (lim.searches, lim.rounds) == (8, 9)

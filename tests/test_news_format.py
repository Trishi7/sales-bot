"""The 8 Oct news template and news answer (docs/plans/NEWS-OCT8.md), under pytest.

The checks themselves live in tests/news_checks.py and are shared with
verify_news_format.py and the 8 Oct section of verify_replay_oct6.py; here
each group is one test and every failed check is reported.

OFFLINE: conftest installs the offline guard (a real Sheets or Drive call
fails the test), the stories are made up, and no model is called.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import news  # noqa: E402
import news_checks  # noqa: E402


def _run(fn) -> None:
    failed = []

    def check(name, got, want=True):
        if got != want:
            failed.append(f"{name}: got {got!r}, want {want!r}")

    out = fn(check)
    if asyncio.iscoroutine(out):
        asyncio.run(out)
    assert not failed, "\n".join(failed)


def test_the_template_in_every_mode_the_cap_and_one_message():
    _run(news_checks.pure)


def test_answers_in_the_channel():
    _run(news_checks.answers)


def test_the_daily_post_and_its_follow_up_skip_what_an_answer_gave():
    _run(news_checks.daily)


def test_the_8_oct_question_replayed():
    _run(news_checks.replay)


def test_what_counts_as_a_plain_news_question():
    for ask in ("any AI news?", "@Saley any AI news?".replace("@Saley ", ""),
                "what's in the news", "whats in the news today", "top 5 headlines",
                "top 5 AI headlines", "what is in the news forthis hour?",
                "what's new in AI?", "AI news", "latest AI news please"):
        assert news.plain_question(ask), ask
    for ask in ("any news on ElevenLabs?", "news about voice agents",
                "what's the latest on Acme?", "where are we with Acme?",
                "any AI news this week on evals", "sure", "", "who are the PoCs at Acme?",
                "remind me about the news on Friday"):
        assert not news.plain_question(ask), ask


def test_an_answer_picks_by_score_and_lists_by_time():
    def story(name, imp, hour):
        return {"headline": name, "url": f"https://a.example/{name}", "url_key": name,
                "headline_key": name, "importance": imp, "kind": "industry",
                "published_at": f"2026-10-08T{hour:02d}:00:00Z"}

    pool = [story("old five", 5, 1), story("new three", 3, 9), story("mid four", 4, 5),
            story("new four", 4, 8), story("old three", 3, 2), story("mid five", 5, 6),
            story("old four", 4, 3)]
    got = news.choose_answer(pool, is_sent=lambda s: False)
    assert [s["headline"] for s in got["stories"]] == [
        "new four", "mid five", "mid four", "old four", "old five"]
    assert got["repeat"] is False and got["unsent"] == 7
    sent = {"old five", "mid five", "new four", "mid four", "old four"}
    got = news.choose_answer(pool, is_sent=lambda s: s["headline"] in sent)
    assert [s["headline"] for s in got["stories"]] == ["new three", "old three"]
    got = news.choose_answer(pool, is_sent=lambda s: True)
    assert got["repeat"] is True and len(got["stories"]) == 5
    assert news.choose_answer(pool, is_sent=lambda s: False, cap=99)["stories"].__len__() == 5


def test_a_story_an_answer_gave_does_not_use_up_the_daily_posts_topic_allowance(tmp_path):
    from db import DB

    db = DB(str(tmp_path / "n_test.db"))
    rows = [{"url_key": f"k{i}", "url": f"https://a.example/{i}", "headline": f"Story {i}",
             "headline_key": f"story {i}", "topic": "evals", "importance": 4,
             "kind": "industry"} for i in range(3)]
    db.record_news_stories(rows[:2], on_date="2026-10-08", rule_id="R1", kind=news.MODE_ANSWER)
    assert db.news_story_seen("k0", "", since_iso="2026-09-01")["kind"] == "answer"
    assert db.news_topic_count_today("evals", "2026-10-08") == 0
    assert db.news_topics_this_week("2026-W41") == []
    db.record_news_stories(rows[2:], on_date="2026-10-08", rule_id="R1", kind=news.MODE_MAIN)
    assert db.news_topic_count_today("evals", "2026-10-08") == 1

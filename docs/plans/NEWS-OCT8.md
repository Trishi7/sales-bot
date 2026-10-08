# NEWS-OCT8 — one template for every news message, and a news answer that never talks about the schedule

Built by one session (no agent team), 8 Oct 2026, on HEAD `4db1983`. Nothing committed.
Report: `docs/test-reports/NEWS-OCT8.md`. Sheet wording for Vaishnavi: `docs/rules-for-the-sheet-oct8.md`.

## 0. What was asked, and the six decisions

The human, 8 Oct: every AI news message is "headlines in bold with a link", bulleted, with spacing between the points,
"when asked in the channel, daily news messages, breaking news, even test simulations". PoC news keeps its own line.
An answer must never say it is "scheduled" or "AI news since 2pm"; it gives the best 5, and does not send again what was
already sent unless nothing else is left.

| # | Decision (final) | Where it is built |
|---|---|---|
| 1 | Industry line: `- **<Headline>** ([<Outlet>](<url>))`, blank line between items. PoC line unchanged apart from the `- ` marker | `news.story_line`, `news.render` |
| 2 | Follow-up: only stories scored above 4, at most 5. No news message carries more than 5 | `NEWS_OVERFLOW_MIN_IMPORTANCE` 3→5, `NEWS_OVERFLOW_MAX_ITEMS` 8→5, `news.MAX_PER_MESSAGE = 5` |
| 3 | Answers: the 5 highest-scored stories not sent before, listed newest first | `news.choose_answer`, `bot._news_answer` |
| 4 | Fewer than 5 unsent: only those. Repeat only when not one unsent story is left; then the top 5 again | `news.choose_answer` |
| 5 | The daily post skips stories given in an earlier answer | answers are recorded in `news_stories` with `kind=answer`; `news_story_seen` counts every kind |
| 6 | The daily post keeps the top 2 PoC stories first | `news.choose_main`, unchanged |

## 1. What the code did before (verified)

- `news.render` (news.py) wrote `• Headline — what. [site](<url>)` for every story, with `**Breaking AI news**` /
  `**More AI news today**` above a breaking post and the follow-up. The daily post's heading came from
  `drip.HEADINGS["R1"]` = `AI news, {day}`.
- The follow-up took everything at importance 3 or more that did not fit, up to 8 (`config.py`), so most days it was a
  second, longer post of middling stories.
- **The answer was written by the model.** `todays_news` (bot.py) returned up to 12 `items`, sorted with the stories
  ALREADY POSTED FIRST (`group 0`), each with a `posted` flag, plus a `window` string ("since Wed 2 PM"). The engine's
  "FORMAT LIKE THE DAILY POST" block asked the model to copy the shape. On 8 Oct 11:22 that produced "All of today's
  stories were already posted at 2 PM. Here's what ran:", lines shaped "Title — VOI.ID", and a link cut in half.
- Nothing recorded what an answer had given, so asking twice gave the same list, and the daily post repeated it.
- A daily post was split by `drip.split_on_lines` at `QUERY_REPLY_CHUNK` (1,900). Five Google News links average about
  330 characters each in the local feed store (2,246 items measured, read-only), so a five-story post is often past
  1,900 and the second message was a bare list with no heading. That is the "arrived as two messages" case.
- The breaking sender passed `cap=99` and rendered every important story in one message.

## 2. The template (news.py)

```
**AI News, Thu 8 Oct**

- **State AGs launch investigations into OpenAI AI safety** ([Reuters](<url>))

- Synthflow raises $20M Series A — what happened. (Synthflow AI — on Master Pipeline) [inc42.com](<url>)
```

- `HEADING_LABELS` and `heading(mode, day)`: `AI News` (daily post, answer), `More AI News`, `Breaking AI News`, each
  bold with the day written the bot's way ("Thu 8 Oct"). `drip.HEADINGS["R1"]` is `AI News, {day}` so the daily post,
  whose heading the drip sender adds above the tags line, reads the same.
- `clean_headline(story)`: drops a trailing " - Outlet" / " | Outlet" when the tail names the story's source or site
  (or is a short pipe tail); drops one trailing full stop; removes mention tokens and asterisks; **never cuts**. A dash
  tail that matches nothing is kept ("GPT-5 - what we know so far").
- `outlet(story)`: a Google News item's `source` (a strapline source is cut back to the name in front); a direct feed
  item's `links.site_name`; else the host without "www.". Direct items do NOT use `source`: it is the feed's title
  ("AI News & Artificial Intelligence | TechCrunch").
- `story_line`: industry as above; PoC exactly the old line with `- ` for `• `.
- `unique`: one story once per message — same `url_key`, same `headline_key`, or same cleaned headline.
- `render(stories, mode, day)`: at most `MAX_PER_MESSAGE` (5) stories whatever it is handed; blank line between them;
  MODE_MAIN returns the bullets alone (the drip sender adds heading and tags), the other modes carry their heading.
  A story whose single line is over 1,500 characters is left out and logged (a link is never shortened).
- `layout(text)`: for the daily post once heading and tags are on: heading, tags line, ONE blank line, the bullets.
- `split_message(text, limit=2000)`: one message whenever it fits; past the limit it splits between stories and every
  later part opens with the heading plus "(continued)". The tags line is not repeated.

## 3. The answer (bot.py, news.py, query_engine.py, toolsets.py)

- `news.plain_question(text)`: a question that asks for news and names nothing else ("any AI news?", "what's in the
  news", "top 5 headlines", "what is in the news forthis hour?"). Words only, no model.
- `_route_query`: a plain news question is answered by `_answer_plain_news` before the sheet-update extractor and the
  model router: **no engine call, no router call, no extractor call**. Route name `news`.
- `_news_answer(topic, days)`: candidates are the feed-store rows collected since the previous daily post or in the
  last 24 hours, whichever is longer, scored 3 or more. "Sent before" = `db.news_story_seen` inside `NEWS_REPEAT_DAYS`
  (main, overflow, breaking, answer). `news.choose_answer` picks; `news.render(MODE_ANSWER)` writes. It returns no
  window and no posted flags; the window is only logged.
- `_record_news_answer`: after the reply is sent, the stories are recorded with `kind=answer`. A repeat is not
  re-recorded. `db.news_topic_count_today` / `news_topics_this_week` exclude `kind=answer`, so somebody asking about a
  topic in the morning does not use up that topic's place in the daily post.
- A question that names a company, person or subject still goes to the engine. `todays_news` renders the same block
  into the turn's sink (`_NEWS_SINK`, a context variable, so two people asking at once cannot get each other's list)
  and tells the model only `{"added_to_reply": true, "stories": n}`. `_answer_with_engine` puts the block at the top of
  the reply. When `todays_news` was the only tool used, the model's text is dropped (it never saw the stories); when
  it also searched or read the sheet, its text follows the list. Sent by `_reply_news` (one message; split only
  between stories).
- `query_engine.py` news block rewritten: the list is put in the reply for you; never say when or whether anything was
  posted; `web_search` stays the fallback and a story listed from it takes the same line shape, at most 5.
- `NEWS_QUESTION_MAX_ITEMS` 12 → 5. Nothing to report: `news.quiet_line`, as before.

## 4. The senders (bot.py)

- Daily post (`_send_drip_message`, so a real day, a test day and a simulation alike): for R1, `news.layout` then
  `news.split_message(limit=2000 - tag room)`. The stored body gets the same layout.
- Follow-up (`_post_news_overflow`): at most 5, heading with the day, `news.split_message`.
- Breaking (`_maybe_breaking_news`): the 5 highest-scored when more than 5 are important; the rest are not recorded, so
  the next daily post still has them; heading with the day; `news.split_message`.

## 5. Every place a list of stories goes out

| Place | After this change |
|---|---|
| Daily post, live / SALES_TEST_MODE / test day / simulation | the template (one sender for all four) |
| "More AI News" follow-up | the template |
| Breaking post | the template |
| Answer to a plain news question | the template, by code |
| Answer to a topic news question | the template, by code, the model's text only when it used another tool |
| Stories the engine lists from `web_search` | the same line shape, by the engine prompt (model-written) |
| `SEARCH_BACKEND=anthropic` main sweep (`_main_sweep_server`) | `news.render`, so the template |
| R2 "Companies in the news" (`news.render_screen`) | **unchanged**: it lists companies with what they do and why they fit, not headlines. See question Q1 |
| Cadence preview | shows R1 as one queue line ("AI news: … [web research pending]"), no stories: unchanged |
| "What are we doing today?" (`_todays_objectives`) | left alone, as instructed (it leaves the AI news out) |
| `tools/tone_samples.py` | its canned `todays_news` result is the OLD shape. See question Q3 |

## 6. Settings

- CHANGED default: `NEWS_OVERFLOW_MIN_IMPORTANCE` 3 → 5, `NEWS_OVERFLOW_MAX_ITEMS` 8 → 5 (config.py, .env.example).
- NEW: none. RETIRED: none.
- This laptop's `.env.agent` shows `NEWS_OVERFLOW_ENABLED=false`, `NEWS_OVERFLOW_MIN_IMPORTANCE=4`,
  `NEWS_OVERFLOW_MAX_ITEMS=3`: the laptop posts no follow-up at all today, and the two values are set explicitly, so the
  new defaults change nothing here until the lines are edited.

## 7. Rule wording

`bot_rules.yaml` `global_rules.ai_news` and R1's `sheet_wording` and comment; README ("News questions…", "The news
template", "The follow-up"); `sales_strategy.md` rule 1; `sales_policy.md` R1 exemplar (the file stays under its
16,000-character cap: 15,873); `docs/rules-for-the-sheet-oct8.md`. The posting time is not touched.

## 8. Tests

New: `tests/news_cases.py` (scenarios), `tests/news_checks.py` (the checks, shared), `tests/test_news_format.py`,
`verify_news_format.py`, and an "8 Oct 11:22" section in the shared replay harness `verify_replay_oct6.py`.
Updated pins (never deleted): `news.py` self-test, `drip.py` self-test, `verify_news_question.py`,
`verify_news_feed.py`, `verify_news_events.py`, `verify_s2.py`, `verify_layouts.py` (live-only, edited by reading),
`tests/replies_cases.py` (I2), `tests/replies_cases3.py` (N1), `tests/replay_1063.py` (step 7, the 7 Oct exchange).
Each is listed with before and after in the report.

## 9. Questions for the human (the default built is in brackets)

- **Q1. R2 "Companies in the news".** Your brief lists it among the places a story list goes out. It lists companies,
  two lines each (what they do, why they fit), not headlines, and you said the PoC template stays. [Left as it is.]
- **Q2. "No model call at all" for a plain news question.** No engine, router or extractor call is made. The light
  scorer is still called once when stories nobody has rated yet are in the window (as before), because otherwise a
  story collected since the last hourly check could not be given. [Scorer kept; zero calls when everything is rated.]
- **Q3. The tone-samples page.** `tools/tone_samples.py` cans the old `todays_news` result for its news question, so
  that sample would show a model-written list. A plain news question no longer reaches the model at all. [Not changed
  here; the sample question should be dropped or replaced before the page is made for Kushal.]
- **Q4. More than 5 breaking stories at once.** The 5 highest-scored go; the rest wait for the next daily post or the
  next check. [Built so.]
- **Q5. A story line longer than 1,500 characters** (a tracking link of that length exists in the store) is left out
  and logged, because it cannot fit one Discord message and a link is never shortened. [Built so.]
- **Q6. The heading on a split message** reads "**AI News, Thu 8 Oct (continued)**". [Built so.]
- **Q7. An answer on a pretend (test) day** is headed with the pretend day and reads that day's news, as every other
  path does. The 8 Oct oddity was a leftover test day; `back to today` clears it. [Unchanged.]

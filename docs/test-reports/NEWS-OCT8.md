# NEWS-OCT8 test report — one news template, and a news answer that never talks about the schedule

8 Oct 2026. Base commit `4db1983` plus this change, uncommitted. Plan: `docs/plans/NEWS-OCT8.md`.
Built and tested by one session (no agent team). Everything below was run OFFLINE with `ANTHROPIC_API_KEY=x`:
no model call, no network, no real Sheets or Drive call (`tests/offline_guard.py` fails a run that attempts one), no
Discord. **Nothing has been checked in the live channel**: that is the checklist in section 8.

## 1. Result in one line

Every new check passes, every script that was green before is green, and the scripts that were red before this change
are red on the same lines (compared against an untouched copy of `HEAD`, section 5). "Every verify_*.py and pytest
stays green" therefore holds for everything that was green; the older red set is unchanged and is not from this work.

## 2. Commands and results (final tree)

| Command | Result |
|---|---|
| `python -m pytest -q tests/` | 881 passed, 25 skipped, 1 failed (the known `TestPostingWindow::test_a_heavy_day_shrinks_the_gap_but_not_below_the_floor`, `assert 120 < 120`, the laptop's own gap settings) |
| `python verify_news_format.py` (new) | ALL PASSED |
| `python -m pytest -q tests/test_news_format.py` (new, 7 tests) | 7 passed |
| `python verify_replay_oct6.py` (with the new 8 Oct 11:22 section) | ALL PASSED |
| `python verify_news_question.py`, `verify_news_feed.py`, `verify_news_events.py`, `verify_s2.py` | ALL PASSED |
| `python verify_replies.py`, `verify_interim.py`, `verify_tokens.py`, `verify_answer_voice.py`, `verify_notes_scope.py`, `verify_profile_lookup.py --as-shipped`, `verify_rule13.py`, `verify_approvals.py`, `verify_s1.py --no-ddg`, `verify_s3.py`, `verify_points.py`, `verify_research_timing.py`, `verify_parity.py --fixture-only`, `verify_search_backend.py --only a --stub` | ALL PASSED |
| `verify_tone.py --offline`, `verify_voice.py --offline`, `verify_voice_profile.py --offline` | ALL PASSED |
| `verify_llm_audit.py` | exit 0 (prints a table) |
| `python -m news`, `-m rules`, `-m toolsets`, `-m wording` | ALL PASSED |
| `python drip.py` | 6 FAILED, the same six cap/gap/slot lines as before (the laptop's gap settings); the heading check passes with the new label |
| `python -m py_compile` on every changed file | clean |

The 25 skips are the gated tests that execute `tools/tone_samples.py` (`--run-tone-samples`); not part of this change.

## 3. What the new checks cover

`verify_news_format.py` and `tests/test_news_format.py` share `tests/news_checks.py`; scenarios are in
`tests/news_cases.py` (a whole bot behind fake Discord, a throwaway database of made-up stories on a pinned day, every
news setting pinned to `.env.example`).

| What was asked | Check | Result |
|---|---|---|
| `news.render` in all modes, exact text for 5 stories (industry and PoC, a Google News title ending " - Reuters", a direct-feed item, a headline ending in a full stop, a pipe tail, a dash that is part of the headline) | "THE TEMPLATE": the daily post, with heading and tags line, the follow-up, breaking and an answer are compared to the exact expected text | PASS |
| PoC lines unchanged apart from the marker | the PoC line equals the old line word for word | PASS |
| Follow-up: 6 qualifying give 5; an importance-4 story never goes in | 7 major stories with no room: the follow-up carries 5; the two importance-4 stories are in neither post and are given to the next asker | PASS |
| Two items about the same story give one | same headline from two outlets; same link; same headline key | PASS |
| A full daily post is one Discord message | the post is one message under 2,000 characters; five 500-character links split between stories and the second part opens `**AI News, Thu 8 Oct (continued)**` | PASS |
| 9 unsent give the 5 highest-scored, newest first | three 5s and the two newest 4s, listed newest first | PASS |
| 3 unsent give those 3 | (verify_news_question b2) three unsent, no padding; and 4 left on the second ask | PASS |
| 0 unsent give the top 5 again | third ask repeats the first five; a repeat is not recorded twice | PASS |
| An answer's stories are absent from the next answer and the next daily post | second answer shares nothing with the first; the 14:00 post and its follow-up carry none of the five the 13:55 answer gave | PASS |
| No answer contains "2 PM", "already", "scheduled", "since" or "ran" | checked on every answer, including when the scripted model writes "All of today's stories were already posted at 2 PM. Here's what ran:" (that text is not sent) | PASS |
| A plain news question makes zero model calls | engine 0, router 0, extractor 0, scorer 0, sheet reads 0; route `news` | PASS |
| The same in test mode and live | every scenario is run live then in SALES_TEST_MODE and compared: answers identical (replies never carry a tag); the daily post and follow-up identical apart from the tag on the heading line | PASS |
| The 8 Oct 11:22 question in the replay harness | `verify_replay_oct6.py`, section "8 Oct 11:22": "what is in the news forthis hour?" with a leftover test day (Wed 7 Oct): one message, five whole links, no schedule talk, asked again gives none of the five | PASS |
| Topic questions go through the engine with the same list | "any news on ElevenLabs?": the list filtered to the company, the model handed only `added_to_reply`; with another tool used, the model's line follows the list | PASS |
| Nothing to report | the quiet line, alone, no model call | PASS |
| An answer does not use up the daily post's topic allowance | `db.news_topic_count_today` and `news_topics_this_week` ignore `kind=answer` | PASS |

## 4. Before and after

The same five made-up stories through the old renderer (`git show HEAD:news.py`) and the new one.

```
=== DAILY POST — BEFORE
**AI news, Thu 8 Oct**
<@tags>
• ElevenLabs Doubles Its Valuation in Employee Tender — ElevenLabs doubled its valuation in employee tender offer. (Elevenlabs — on Master Pipeline and Outreach PoCs) [streamlinefeed.co.ke](<https://streamlinefeed.co.ke/elevenlabs-tender>)
• State AGs launch investigations into OpenAI AI safety — attorneys general in several states opened inquiries. [Reuters](<https://news.google.com/rss/articles/CBMi-state-ags?oc=5>)
• TM Forum and Accenture launch AI trust framework for telecoms — a shared framework for telecom operators. [techcrunch.com](<https://techcrunch.com/2026/10/08/tm-forum-accenture>)
• As AI gains autonomy, enterprises must retain authority | Hindustan Times — a column on agent oversight. [Hindustan Times](<https://news.google.com/rss/articles/CBMi-ht?oc=5>)
• Chipmaker unveils an inference accelerator. — a new part aimed at inference. [The Verge](<https://news.google.com/rss/articles/CBMi-chip?oc=5>)

=== DAILY POST — AFTER
**AI News, Thu 8 Oct**
<@tags>

- ElevenLabs Doubles Its Valuation in Employee Tender — ElevenLabs doubled its valuation in employee tender offer. (Elevenlabs — on Master Pipeline and Outreach PoCs) [streamlinefeed.co.ke](<https://streamlinefeed.co.ke/elevenlabs-tender>)

- **State AGs launch investigations into OpenAI AI safety** ([Reuters](<https://news.google.com/rss/articles/CBMi-state-ags?oc=5>))

- **TM Forum and Accenture launch AI trust framework for telecoms** ([techcrunch.com](<https://techcrunch.com/2026/10/08/tm-forum-accenture>))

- **As AI gains autonomy, enterprises must retain authority** ([Hindustan Times](<https://news.google.com/rss/articles/CBMi-ht?oc=5>))

- **Chipmaker unveils an inference accelerator** ([The Verge](<https://news.google.com/rss/articles/CBMi-chip?oc=5>))

=== FOLLOW-UP — BEFORE
**More AI news today**
• State AGs launch investigations into OpenAI AI safety — attorneys general in several states opened inquiries. [Reuters](<https://news.google.com/rss/articles/CBMi-state-ags?oc=5>)
• TM Forum and Accenture launch AI trust framework for telecoms — a shared framework for telecom operators. [techcrunch.com](<https://techcrunch.com/2026/10/08/tm-forum-accenture>)
• As AI gains autonomy, enterprises must retain authority | Hindustan Times — a column on agent oversight. [Hindustan Times](<https://news.google.com/rss/articles/CBMi-ht?oc=5>)

=== FOLLOW-UP — AFTER
**More AI News, Thu 8 Oct**

- **State AGs launch investigations into OpenAI AI safety** ([Reuters](<https://news.google.com/rss/articles/CBMi-state-ags?oc=5>))

=== BREAKING — BEFORE
**Breaking AI news**
• State AGs launch investigations into OpenAI AI safety — attorneys general in several states opened inquiries. [Reuters](<https://news.google.com/rss/articles/CBMi-state-ags?oc=5>)

=== BREAKING — AFTER
**Breaking AI News, Thu 8 Oct**

- **State AGs launch investigations into OpenAI AI safety** ([Reuters](<https://news.google.com/rss/articles/CBMi-state-ags?oc=5>))

=== ANSWER — AFTER
**AI News, Thu 8 Oct**

- ElevenLabs Doubles Its Valuation in Employee Tender — ElevenLabs doubled its valuation in employee tender offer. (Elevenlabs — on Master Pipeline and Outreach PoCs) [streamlinefeed.co.ke](<https://streamlinefeed.co.ke/elevenlabs-tender>)

- **State AGs launch investigations into OpenAI AI safety** ([Reuters](<https://news.google.com/rss/articles/CBMi-state-ags?oc=5>))

- **TM Forum and Accenture launch AI trust framework for telecoms** ([techcrunch.com](<https://techcrunch.com/2026/10/08/tm-forum-accenture>))

- **As AI gains autonomy, enterprises must retain authority** ([Hindustan Times](<https://news.google.com/rss/articles/CBMi-ht?oc=5>))

- **Chipmaker unveils an inference accelerator** ([The Verge](<https://news.google.com/rss/articles/CBMi-chip?oc=5>))
```

**An answer in the channel — BEFORE** (the live channel, 8 Oct 11:22, as the human reported it; written by the model):

```
All of today's stories were already posted at 2 PM. Here's what ran:
… Title — VOI.ID
… (Anthropic — on Outreach PoCs) — [ET Enterprise AI](news.google.com
```

**AFTER** is the "ANSWER — AFTER" block above: written by code, the 5 highest-scored stories not sent before, newest
first, and nothing else.

## 5. The scripts that were red before, compared with untouched HEAD

Run on a temporary `git worktree` of `HEAD` (removed afterwards) with the laptop's non-secret settings, and on the
working tree; failing lines compared with digits masked (clock values differ run to run).

| Script | HEAD | Now | Lines |
|---|---|---|---|
| `verify_monday.py` | 4 FAILED | 4 FAILED | same |
| `verify_heavy_monday.py` | 4 FAILED | 4 FAILED | same |
| `verify_clock.py` | 5 FAILED | 5 FAILED | same |
| `verify_testday.py` | 2 FAILED | 2 FAILED | same |
| `verify_testday_talk.py` | 1 FAILED | 1 FAILED | same |
| `verify_test_output.py` | 8 FAILED | 8 FAILED | same |
| `verify_reminders.py` | 12 FAILED | 12 FAILED | same |
| `verify_websearch.py --offline` | crashes (`KeyError: 'new_pipeline_company'`) | same crash | same |
| `python drip.py` | 6 FAILED | 6 FAILED | same six lines |
| pytest `TestPostingWindow` | failed | failed | same |

## 6. Existing checks that pinned the old shape (updated, none deleted)

| File | Before | After |
|---|---|---|
| `news.py` self-test | `• Headline — w. [site](<url>)`; `**More AI news today**`; `**Breaking AI news**` | the new line shape and headings with the day; plus new checks for headline cleaning, outlet names, `unique`, `layout`, `split_message`, `choose_answer`, `plain_question` |
| `drip.py` self-test | `**AI news, Tue 29 Sep**` | `**AI News, Tue 29 Sep**` |
| `verify_news_question.py` (b)–(e) | posted stories first, at most 12, `window` "since Wed 2 PM", `items` handed to the model | the 5 highest-scored unsent, newest first; fewer than 5; repeat when nothing is left; recorded as sent; nothing called `window`, `items` or `posted` is handed to the model; (a) routing and (f) unchanged |
| `verify_news_feed.py` | 5 lines starting `• `; `[TEST] **Breaking AI news**` | 5 lines `- **Headline** ([Outlet](<url>))` with blank lines between; `[TEST] **Breaking AI News, <day>**` |
| `verify_news_events.py` | bullets start `• ` | bullets start `- ` |
| `verify_s2.py` | bullets `• `; `news.OVERFLOW_HEADING`; "More AI news today" in the sheet wording and strategy | the new bullets and heading; "More AI News". Its follow-up scenario keeps an explicit bar of 3 (it tests the mechanics); the shipped bar of 5 is `verify_news_format.py`'s |
| `verify_layouts.py` | `• ` lines, `**AI news, …**`, `**Breaking AI news**` | the new shape. **Edited by reading only: this script calls the live model and was not run** |
| `tests/replies_cases.py` I2 | "any AI news?" gets an ENGINE interim line | the plain question gets no interim line and no model call; the interim-wording rule is still exercised by "any AI news about Acme?" |
| `tests/replies_cases3.py` N1 | stories read off the `todays_news` tool result | read off the reply's bold headlines; zero model calls |
| `tests/replay_1063.py` step 7 | routed to the engine, two model calls, an interim line when slow | routed to the news list, zero model calls, no interim line |
| `tests/replay_1063.py` 7 Oct exchange | "sure" replied to the interim line | there is no interim line any more (the news is answered at once), so "sure" is replied to the answer: one reaction, no vote, no reminder, R3's offer still open, as before |

## 7. Not run, and what is not covered

- **Not run:** `verify_poc_lookup.py`, `verify_layouts.py`, `verify_simulation.py` (live searches, the live model or
  the real sheet), `tools/tone_samples.py --live-model`, `python -m gtm_sheet`, and anything in the live channel.
- **Breaking post with more than 5 important stories:** the cap is tested in `news.render` (six stories give five in
  every mode) and the sender's new heading and list shape are tested end to end in `verify_news_feed.py`; the sender's
  own "take the 5 highest-scored" branch has no end-to-end case.
- **Stories the engine lists from `web_search`:** their shape is an instruction in the engine prompt, so it is the
  model's to follow; nothing in code enforces it and no offline test can show it.
- **The line that tags people in the daily post:** the layout around a tags line is tested on text (`news.layout`);
  the test world's R1 post has no tagged owner, so that line is not exercised through the sender.
- **A plain news question when stories are unrated:** the light scorer is called once (unchanged behaviour, covered by
  `verify_news_question.py` (d)); "zero model calls" is shown with everything already rated.
- **Sizes measured read-only in the laptop's test database** (2,246 stored feed items): Google News links average
  about 330 characters, the longest 2,064, which is why a five-story post could pass 1,900 characters before.

## 8. Environment variables, and the lines for both `.env` files

- NEW: none. RETIRED: none.
- CHANGED default: `NEWS_OVERFLOW_MIN_IMPORTANCE` 3 → 5; `NEWS_OVERFLOW_MAX_ITEMS` 8 → 5.

Laptop `.env` (`.env.agent` shows `NEWS_OVERFLOW_ENABLED=false`, `NEWS_OVERFLOW_MIN_IMPORTANCE=4`,
`NEWS_OVERFLOW_MAX_ITEMS=3` today, so the new defaults change nothing here until these are edited):

```
NEWS_OVERFLOW_MIN_IMPORTANCE=5
NEWS_OVERFLOW_MAX_ITEMS=5
NEWS_OVERFLOW_ENABLED=true        # only if the laptop should post the follow-up at all; it is off today
```

Server `/opt/sales-bot/.env` (not visible from here; check each):

```
NEWS_OVERFLOW_MIN_IMPORTANCE=5
NEWS_OVERFLOW_MAX_ITEMS=5
```

Then `python tools/redact_env.py` on the laptop. No database migration.

## 9. Live-channel checklist (not executed)

1. `@Saley any AI news?` → ONE message: `**AI News, <day>**`, a blank line, at most 5 bullets, each a bold headline
   then the outlet as a link, a blank line between bullets. No "2 PM", "scheduled", "already posted", "since".
2. `@Saley any AI news?` again → **none of the first answer's stories**. Keep asking: only when nothing new is left
   does it repeat, and then it gives its 5 best again.
3. Every link opens the story it names; no link is cut.
4. `@Saley any news on <a company on the sheet>?` → the same list shape, only that company's stories; a PoC story
   keeps its fuller line with "(Company — on Master Pipeline)".
5. The next 2 PM post → one message: the heading, the line that tags people, a blank line, the stories; the top PoC
   stories first; none of the stories an answer gave earlier that day.
6. If "More AI News, <day>" follows → at most 5 stories, all major. Most days there is none.
7. A breaking post → `**Breaking AI News, <day>**`, the same list shape, at most 5.
8. Test day or simulation (`make it Monday`, a simulated week) → the news messages have the same shape, with the
   `[TEST…]` tag in front of the heading.
9. Log: `[query] … → the news list (a plain news question, answered without the model)` and
   `[news] question: … giving N`.

## 10. Questions for the human

See plan section 9 (Q1–Q7): R2's company list left as it is; the light scorer still rates unrated stories for a plain
question; the tone-samples news question now shows an out-of-date path; more than 5 breaking stories; a story whose
link cannot fit a message; the "(continued)" heading; an answer on a leftover test day.
